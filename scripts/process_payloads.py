#process_payloads.py

import json
import pathlib
import requests
import os
import sys
import time
import subprocess
from datetime import datetime, timezone, timedelta

# --- Configuration des chemins ---
base_dir = pathlib.Path(__file__).parent.parent
payload_dir = base_dir / "instagram_payloads"
payload_dir.mkdir(exist_ok=True)
published_file = pathlib.Path(__file__).parent / "published.json"

errors = []
now = int(time.time())

# --- HTTP avec timeout par défaut ---
# Sans timeout, un hang réseau (Graph API, GitHub) pend le workflow GitHub
# Actions jusqu'au kill à 6h (quota consommé). Tous les appels passent par
# ces wrappers ; un timeout explicite passé par l'appelant reste prioritaire.
API_TIMEOUT = 60  # secondes


def _post(url, **kwargs):
    kwargs.setdefault("timeout", API_TIMEOUT)
    return requests.post(url, **kwargs)


def _get(url, **kwargs):
    kwargs.setdefault("timeout", API_TIMEOUT)
    return requests.get(url, **kwargs)

# --- Charger l'état des posts déjà publiés ---
if published_file.exists():
    with open(published_file) as f:
        published = set(json.load(f))
else:
    published = set()


def _thumb_url(data: dict) -> str:
    """Extrait la meilleure URL de miniature selon le type de média.

    - IMAGE   → image_url ou media_url
    - CAROUSEL → premier enfant non-vidéo (children[0] si .jpg/.png)
    - VIDEO    → vide (les .mp4 ne s'affichent pas dans Markdown GitHub)
    """
    media_type = data.get("media_type", "IMAGE").upper()
    if media_type == "CAROUSEL":
        return next(
            (u for u in data.get("children", []) if not u.lower().endswith(".mp4")),
            ""
        )
    if media_type == "VIDEO":
        return ""
    return data.get("image_url") or data.get("media_url", "")


def _thumb_cell(thumb: str, media_type: str) -> str:
    """Rendu HTML/emoji de la colonne Aperçu dans le README."""
    if thumb:
        return f"<img src='{thumb}' width='50'>"
    icons = {"VIDEO": "🎬", "CAROUSEL": "🎠"}
    return icons.get(media_type, "N/A")


def generate_dashboard(payload_dir, published_count, run_errors=None):
    """Génère un dashboard résumé par compte utilisateur.

    run_errors : liste des erreurs du run courant (affichées en haut du README si non vides).
    """
    stats_comptes = {}

    for p_file in payload_dir.glob("*.json"):
        try:
            with open(p_file) as f:
                data = json.load(f)
                compte      = data["compte"].upper()
                ts          = int(data["next_time"])
                media_type  = data.get("media_type", "IMAGE").upper()
                thumb       = _thumb_url(data)

                if compte not in stats_comptes:
                    stats_comptes[compte] = {
                        "count":      0,
                        "first":      ts,
                        "last":       ts,
                        "thumb":      thumb,
                        "thumb_type": media_type,
                    }

                stats_comptes[compte]["count"] += 1
                if ts < stats_comptes[compte]["first"]:
                    stats_comptes[compte]["first"]      = ts
                    stats_comptes[compte]["thumb"]      = thumb
                    stats_comptes[compte]["thumb_type"] = media_type
                if ts > stats_comptes[compte]["last"]:
                    stats_comptes[compte]["last"] = ts
        except Exception:
            continue

    now_str = datetime.now().strftime('%d/%m/%Y %H:%M:%S')
    md_content = "# 📊 Dashboard de Publication\n\n"

    # --- Bloc erreurs (affiché en tête si le run a échoué) ---
    if run_errors:
        md_content += f"## ❌ Erreurs du dernier run ({now_str})\n\n"
        for err in run_errors:
            md_content += f"- `{err}`\n"
        md_content += "\n"
    else:
        md_content += f"✅ **Dernier run sans erreur** — {now_str}\n\n"

    md_content += f"📦 **Total publiés historiquement :** {published_count}\n\n"

    if not stats_comptes:
        md_content += "### 🎉 Toutes les files d'attente sont vides !\n"
    else:
        md_content += "### 📱 État des comptes\n"
        md_content += "| Compte | Posts en attente | Prochaine publication | Fin de programmation | Aperçu prochain |\n"
        md_content += "| :--- | :---: | :--- | :--- | :---: |\n"

        for compte in sorted(stats_comptes.keys()):
            s = stats_comptes[compte]
            date_next = (datetime.fromtimestamp(s["first"], tz=timezone.utc) + timedelta(hours=2)).strftime('%d/%m %H:%M')
            date_last = (datetime.fromtimestamp(s["last"],  tz=timezone.utc) + timedelta(hours=2)).strftime('%d/%m %H:%M')
            count_display = f"**{s['count']}**" if s['count'] > 5 else f"[WARN] **{s['count']}**"
            thumb = _thumb_cell(s["thumb"], s["thumb_type"])
            md_content += f"| {compte} | {count_display} | {date_next} | {date_last} | {thumb} |\n"

    readme_path = pathlib.Path(__file__).parent.parent / "README.md"
    with open(readme_path, "w", encoding="utf-8") as f:
        f.write(md_content)
    print("[DOC] Dashboard résumé mis à jour.")


# ==========================================
# HELPERS PUBLICATION
# ==========================================

def publish_image(instagram_id, access_token, image_url, caption):
    """
    Publie une image sur Instagram (post classique).
    Retourne (success: bool, media_id: str | None).
    """
    media_url    = f"https://graph.facebook.com/v25.0/{instagram_id}/media"
    media_params = {
        "image_url":    image_url,
        "caption":      caption,
        "access_token": access_token
    }
    r = _post(media_url, data=media_params)
    r.raise_for_status()
    media_id = r.json()["id"]

    publish_url    = f"https://graph.facebook.com/v25.0/{instagram_id}/media_publish"
    publish_params = {"creation_id": media_id, "access_token": access_token}
    time.sleep(2)
    rp = _post(publish_url, data=publish_params)
    rp.raise_for_status()
    return True, media_id


def _poll_instagram_container(container_id, access_token, max_wait=300, poll_every=10, label=""):
    """
    Attend que le conteneur Instagram passe au statut FINISHED.
    Lève une exception si ERROR, EXPIRED ou timeout.

    Note : seul 'status_code' est utilisé — le champ 'status' est invalide pour
    cet endpoint en API v25.0 et provoque un 400 immédiat.
    Les erreurs HTTP transitoires (400/5xx) sont retentées jusqu'à MAX_HTTP_ERR fois.
    """
    status_url    = f"https://graph.facebook.com/v25.0/{container_id}"
    status_params = {
        "fields":       "status_code",
        "access_token": access_token
    }
    elapsed      = 0
    http_errors  = 0
    MAX_HTTP_ERR = 3

    while elapsed < max_wait:
        time.sleep(poll_every)
        elapsed += poll_every
        try:
            rs = _get(status_url, params=status_params)
            rs.raise_for_status()
            http_errors = 0
        except requests.HTTPError:
            http_errors += 1
            try:
                body = rs.json()
            except Exception:
                body = rs.text
            print(f"  [WARN] Erreur HTTP {rs.status_code} polling {label} ({elapsed}s) : {body}")
            if http_errors >= MAX_HTTP_ERR:
                raise RuntimeError(
                    f"Polling {label} : {MAX_HTTP_ERR} erreurs HTTP consécutives "
                    f"(code {rs.status_code}) — dernier body : {body}"
                )
            continue

        status_data = rs.json()
        status_code = status_data.get("status_code", "")
        print(f"  [WAIT] Statut {label} ({elapsed}s) : {status_code}")

        if status_code == "FINISHED":
            return
        elif status_code in ("ERROR", "EXPIRED"):
            raise RuntimeError(
                f"Traitement {label} échoué côté Meta : {status_code}"
            )

    raise TimeoutError(
        f"Délai dépassé ({max_wait}s) — le conteneur {label} n'est pas passé à FINISHED."
    )


def _publish_video_with_retry(instagram_id, access_token, container_id, label,
                               first_sleep=60, poll_every=20, max_wait=300):
    """
    Tente de publier un conteneur vidéo Instagram en réessayant jusqu'à ce qu'il
    soit prêt (subcode 2207027 = traitement en cours) ou jusqu'au timeout.

    Utilisé à la place du polling GET /{container_id} pour les Reels et Stories
    vidéo — le endpoint GET n'est pas accessible sur tous les comptes (subcode 33).
    """
    publish_url    = f"https://graph.facebook.com/v25.0/{instagram_id}/media_publish"
    publish_params = {"creation_id": container_id, "access_token": access_token}

    print(f"  [WAIT] Attente initiale {first_sleep}s pour le traitement {label}...")
    time.sleep(first_sleep)
    elapsed = first_sleep

    while elapsed < max_wait:
        try:
            rp = _post(publish_url, data=publish_params)
            rp.raise_for_status()
            return rp.json().get("id", "")
        except requests.HTTPError:
            try:
                body = rp.json()
            except Exception:
                body = rp.text
            subcode = body.get("error", {}).get("error_subcode") if isinstance(body, dict) else None
            if subcode == 2207027:
                print(f"  [WAIT] {label} encore en traitement ({elapsed}s)...")
                time.sleep(poll_every)
                elapsed += poll_every
            else:
                raise RuntimeError(
                    f"Erreur publication {label} (code {rp.status_code}) : {body}"
                )

    raise TimeoutError(
        f"Délai dépassé ({max_wait}s) — le conteneur {label} n'est pas passé à FINISHED."
    )


def _poll_facebook_video(video_id, access_token, label, max_wait=300, poll_every=15):
    """
    Attend que Meta ait fini de traiter ET de publier une vidéo Facebook.

    `upload_phase=finish` renvoie 200 dès que la requête est acceptée : le
    traitement est ASYNCHRONE. Sans ce polling, un Reel rejeté par Meta
    (ratio, durée, codec) laisse le script imprimer [OK] alors que rien
    n'apparaît sur la Page — symptôme observé sur ia_actus (onglet Reels vide
    alors que la Story, elle, partait).

    Le body de `fields=status` est imprimé à chaque tour : c'est lui qui porte
    le motif de rejet exact dans `processing_phase.errors`.

    Lève une exception si Meta signale une erreur ou au timeout.
    """
    status_url    = f"https://graph.facebook.com/v25.0/{video_id}"
    status_params = {"fields": "status", "access_token": access_token}
    elapsed       = 0
    http_errors   = 0
    MAX_HTTP_ERR  = 3

    while elapsed < max_wait:
        time.sleep(poll_every)
        elapsed += poll_every
        try:
            rs = _get(status_url, params=status_params)
            rs.raise_for_status()
            http_errors = 0
        except requests.HTTPError:
            http_errors += 1
            try:
                body = rs.json()
            except Exception:
                body = rs.text
            print(f"  [WARN] Erreur HTTP {rs.status_code} polling {label} ({elapsed}s) : {body}")
            if http_errors >= MAX_HTTP_ERR:
                raise RuntimeError(
                    f"Polling {label} : {MAX_HTTP_ERR} erreurs HTTP consécutives "
                    f"(code {rs.status_code}) — dernier body : {body}"
                )
            continue

        status = rs.json().get("status", {}) or {}
        print(f"  [WAIT] Statut {label} ({elapsed}s) : {json.dumps(status)}")

        video_status = status.get("video_status", "")
        processing   = (status.get("processing_phase") or {}).get("status", "")
        publishing   = (status.get("publishing_phase") or {}).get("status", "")

        if "error" in (video_status, processing, publishing):
            raise RuntimeError(f"Traitement {label} rejeté par Meta : {json.dumps(status)}")
        if video_status == "expired":
            raise RuntimeError(f"Session d'upload {label} expirée : {json.dumps(status)}")
        if publishing == "complete":
            return
        # video_status "ready" sans publishing_phase renseignée = ancien schéma
        if video_status == "ready" and not publishing:
            return

    raise TimeoutError(
        f"Délai dépassé ({max_wait}s) — {label} n'a jamais été confirmé publié par Meta."
    )


def publish_carousel(instagram_id, access_token, children_urls, caption):
    """Publie un carousel Instagram (2 à 10 slides).

    Workflow :
      1. Pour chaque URL : créer un item média avec is_carousel_item=true → item_id
      2. Créer le conteneur CAROUSEL avec children=[item_ids] + caption
      3. Attendre FINISHED (polling)
      4. media_publish

    Retourne (success: bool, container_id: str | None).
    """
    if len(children_urls) < 2 or len(children_urls) > 10:
        raise ValueError(f"Instagram carousel requires 2-10 slides (got {len(children_urls)})")

    item_ids = []
    media_url_endpoint = f"https://graph.facebook.com/v25.0/{instagram_id}/media"

    # 1. Upload chaque slide comme item de carousel
    # Détection extension : .mp4 → media_type=VIDEO + video_url, sinon → image_url
    # (cf. CLAUDE.md §5.12 — carousel + music produit slide_XXX_00.mp4)
    for i, child_url in enumerate(children_urls):
        is_video_child = child_url.lower().endswith(".mp4")
        if is_video_child:
            params = {
                "video_url":        child_url,
                "media_type":       "VIDEO",
                "is_carousel_item": "true",
                "access_token":     access_token,
            }
        else:
            params = {
                "image_url":        child_url,
                "is_carousel_item": "true",
                "access_token":     access_token,
            }
        r = _post(media_url_endpoint, data=params)
        r.raise_for_status()
        item_id = r.json()["id"]
        item_ids.append(item_id)
        kind = "VIDEO" if is_video_child else "IMAGE"
        print(f"  [SLIDE {i+1}/{len(children_urls)}] Item {kind} créé : {item_id}")
        if is_video_child:
            # Attente fixe : _poll_instagram_container peut retourner subcode 33 sur certains
            # comptes (même restriction que le container final). L'item n'a pas de media_publish,
            # donc on ne peut pas utiliser _publish_video_with_retry ici — on attend 90s.
            print(f"  [WAIT] Attente 90s pour le traitement de la slide vidéo {i+1}...")
            time.sleep(90)

    # 2. Créer le conteneur CAROUSEL
    container_params = {
        "media_type":   "CAROUSEL",
        "children":     ",".join(item_ids),
        "caption":      caption,
        "access_token": access_token,
    }
    rc = _post(media_url_endpoint, data=container_params)
    rc.raise_for_status()
    container_id = rc.json()["id"]
    print(f"  [CONTAINER] Carousel container créé : {container_id}")

    # 3. Attendre FINISHED + Publier
    # Note : le polling GET /{container_id} retourne error_subcode 33 sur certains comptes
    # (même restriction que les Reels). On utilise media_publish avec retry sur subcode 2207027.
    # Les carousels image sont traités rapidement (~10-30s), d'où le first_sleep court.
    _publish_video_with_retry(instagram_id, access_token, container_id,
                               label="Carousel Instagram", first_sleep=10, poll_every=10, max_wait=300)
    return True, container_id


def publish_video(instagram_id, access_token, video_url, caption):
    """
    Publie une vidéo sur Instagram en tant que Reel.

    Workflow :
      1. Créer le conteneur média avec media_type=REELS
      2. Réessayer media_publish jusqu'à ce qu'Instagram ait traité la vidéo

    Note : le polling GET /{container_id}?fields=status_code retourne error_subcode 33
    sur certains comptes (restriction d'autorisation). On utilise directement media_publish
    avec retry sur subcode 2207027 (traitement en cours).

    Retourne (success: bool, container_id: str).
    """
    media_url    = f"https://graph.facebook.com/v25.0/{instagram_id}/media"
    media_params = {
        "media_type":   "REELS",
        "video_url":    video_url,
        "caption":      caption,
        "access_token": access_token
    }
    r = _post(media_url, data=media_params)
    r.raise_for_status()
    creation_resp = r.json()
    print(f"  [PKG] Réponse création Reel : {creation_resp}")
    container_id = creation_resp["id"]
    print(f"  [PKG] Conteneur Reel Instagram créé : {container_id}")

    _publish_video_with_retry(instagram_id, access_token, container_id,
                               label="Reel Instagram", first_sleep=60, poll_every=20, max_wait=300)
    return True, container_id


def publish_video_story(instagram_id, access_token, video_url):
    """
    Publie une vidéo en Story Instagram.

    Contrainte Meta : la vidéo doit durer entre 3 et 60 secondes.
    Même approche retry que les Reels (polling GET /{container_id} non accessible).

    Retourne (success: bool).
    """
    media_url    = f"https://graph.facebook.com/v25.0/{instagram_id}/media"
    media_params = {
        "media_type":   "STORIES",
        "video_url":    video_url,
        "access_token": access_token
    }
    r = _post(media_url, data=media_params)
    r.raise_for_status()
    container_id = r.json()["id"]
    print(f"  [PKG] Conteneur Story vidéo créé : {container_id}")

    # Stories vidéo sont plus courtes — délai initial réduit
    _publish_video_with_retry(instagram_id, access_token, container_id,
                               label="Story vidéo", first_sleep=30, poll_every=15, max_wait=120)
    return True


def publish_carousel_facebook(facebook_id, access_token, children_urls, caption):
    """Publie un carousel sur une Page Facebook — en PHOTO UNIQUE (slide 0).

    Facebook n'a pas d'équivalent du carousel Instagram : un post multi-photos
    (`attached_media`) est rendu en mosaïque recadrée, dont seules 4-5 tuiles
    quasi carrées sont visibles. Nos slides sont portrait avec texte incrusté et
    se lisent EN SÉQUENCE — dans cette mosaïque le texte est rogné, illisible, et
    chaque post mange un bloc énorme du mur. On ne publie donc que le premier
    slide, en photo simple : le texte complet vit déjà dans la légende.

    children_urls doit contenir des .jpg/.png (cf. payload fb_children) ; les
    .mp4 sont ignorés en safety net (l'endpoint /photos les rejetterait).

    Retourne (success: bool, post_id: str | None).
    """
    cover = next((u for u in children_urls if not u.lower().endswith(".mp4")), None)
    if not cover:
        print(f"  [WARN FB] Aucun slide image disponible (tous en .mp4 ?) — post Facebook ignoré")
        return False, None

    photos_endpoint = f"https://graph.facebook.com/v25.0/{facebook_id}/photos"
    r = _post(
        photos_endpoint,
        data={"url": cover, "caption": caption, "access_token": access_token},
    )
    r.raise_for_status()
    post_id = r.json().get("post_id") or r.json().get("id", "")
    print(f"  [POST FB] Photo de couverture publiée : {post_id}")
    return True, post_id


def publish_video_facebook(facebook_id, access_token, video_url, caption):
    """
    Publie une vidéo sur une Page Facebook en tant que Reel (9:16, sans bandes noires).

    Workflow en 3 étapes requis par Meta :
      1. Initialiser l'upload → obtenir video_id
      2. Uploader via rupload.facebook.com (file_url en header)
      3. Publier avec upload_phase=finish + video_state=PUBLISHED

    Utilise /video_reels et non /videos — /videos affiche les vidéos portrait
    avec des bandes noires car il ne les traite pas comme des Reels.

    L'étape 3 ne fait qu'ACCEPTER la demande : la publication réelle est
    confirmée par _poll_facebook_video (cf. son docstring).

    Retourne (success: bool).
    """
    # Étape 1 : Initialiser
    r = _post(
        f"https://graph.facebook.com/v25.0/{facebook_id}/video_reels",
        data={"upload_phase": "start", "access_token": access_token}
    )
    r.raise_for_status()
    video_id = r.json()["video_id"]
    print(f"  [PKG] Reel Facebook initialisé : {video_id}")

    # Étape 2 : Upload depuis URL hébergée
    ru = _post(
        f"https://rupload.facebook.com/video-upload/v25.0/{video_id}",
        headers={
            "Authorization": f"OAuth {access_token}",
            "file_url":      video_url,
        }
    )
    ru.raise_for_status()
    print(f"  [UP] Vidéo transmise à Meta")

    # Étape 3 : Publier
    rp = _post(
        f"https://graph.facebook.com/v25.0/{facebook_id}/video_reels",
        data={
            "video_id":     video_id,
            "upload_phase": "finish",
            "video_state":  "PUBLISHED",
            "description":  caption,
            "access_token": access_token,
        }
    )
    rp.raise_for_status()
    _poll_facebook_video(video_id, access_token, label="Reel Facebook")
    print(f"  [OK] Reel Facebook publié")
    return True


def publish_video_story_facebook(facebook_id, access_token, video_url):
    """
    Publie une vidéo en Story sur une Page Facebook.

    Même workflow 3 étapes que les Reels Facebook.
    Contrainte Meta : vidéo max 60 secondes.

    Retourne (success: bool).
    """
    # Étape 1 : Initialiser
    r = _post(
        f"https://graph.facebook.com/v25.0/{facebook_id}/video_stories",
        data={"upload_phase": "start", "access_token": access_token}
    )
    r.raise_for_status()
    data     = r.json()
    video_id = data["video_id"]
    # Meta retourne parfois une upload_url directe, sinon on construit la nôtre
    upload_url = data.get("upload_url") or f"https://rupload.facebook.com/video-upload/v25.0/{video_id}"
    print(f"  [PKG] Story Facebook initialisée : {video_id}")

    # Étape 2 : Upload depuis URL hébergée
    ru = _post(
        upload_url,
        headers={
            "Authorization": f"OAuth {access_token}",
            "file_url":      video_url,
        }
    )
    ru.raise_for_status()

    # Étape 3 : Publier
    rp = _post(
        f"https://graph.facebook.com/v25.0/{facebook_id}/video_stories",
        data={
            "video_id":     video_id,
            "upload_phase": "finish",
            "access_token": access_token,
        }
    )
    rp.raise_for_status()
    print(f"  [OK] Story vidéo Facebook publiée")
    return True


# ==========================================
# THREADS
# ==========================================

_THREADS_BASE = "https://graph.threads.net/v1.0"

# Nombre d'essais avant d'abandonner une plateforme SECONDAIRE (Facebook,
# Threads). Instagram en est exempt (cf. boucle de publication).
MAX_PLATFORM_ATTEMPTS = 3


def _poll_threads_container(container_id, access_token, max_wait=180, poll_every=10, label=""):
    """
    Attend qu'un conteneur Threads passe en FINISHED.

    Même nécessité que pour Instagram : publier un conteneur encore en cours de
    transcodage renvoie une erreur peu explicite. Retourne True si prêt.
    """
    waited = 0
    while waited < max_wait:
        r = _get(
            f"{_THREADS_BASE}/{container_id}",
            params={"fields": "status,error_message", "access_token": access_token},
        )
        if r.status_code == 200:
            data   = r.json()
            status = data.get("status", "")
            if status == "FINISHED":
                return True
            if status == "ERROR":
                print(f"  [FAIL] {label} conteneur en erreur : {data.get('error_message', '')}")
                return False
        time.sleep(poll_every)
        waited += poll_every
    print(f"  [FAIL] {label} conteneur non prêt après {max_wait}s")
    return False


def _threads_container(threads_id, access_token, params, label=""):
    """Crée un conteneur Threads et retourne son id."""
    r = _post(f"{_THREADS_BASE}/{threads_id}/threads",
              data={**params, "access_token": access_token})
    r.raise_for_status()
    cid = r.json()["id"]
    print(f"  [PKG] {label} conteneur Threads : {cid}")
    return cid


def publish_threads(threads_id, access_token, media_type, media_url, children, text):
    """
    Publie sur Threads (image, vidéo ou carousel).

    Threads consomme des URLs publiques comme Instagram — les assets GitHub
    Release servent les deux, rien n'est ré-uploadé.

    `text` est la légende DÉJÀ tronquée à la limite Threads côté dashboard
    (publishing/threads.truncate_caption) : le script ne fait que la relayer,
    avec une coupe de sécurité si un vieux payload n'en portait pas.

    Retourne (success: bool).
    """
    if media_type == "CAROUSEL":
        # Chaque item devient un conteneur enfant, puis un conteneur parent les
        # agrège. Threads accepte 2 à 20 enfants — les carousels du projet sont
        # plafonnés à 10 par l'API Instagram, donc toujours dans les clous.
        child_ids = []
        for i, url in enumerate(children):
            is_video = str(url).lower().endswith(".mp4")
            params   = {
                "media_type":       "VIDEO" if is_video else "IMAGE",
                "is_carousel_item": "true",
            }
            params["video_url" if is_video else "image_url"] = url
            child_ids.append(
                _threads_container(threads_id, access_token, params, label=f"Slide {i}")
            )

        if len(child_ids) < 2:
            print("  [FAIL] Carousel Threads : moins de 2 items exploitables")
            return False

        container_id = _threads_container(
            threads_id, access_token,
            {"media_type": "CAROUSEL", "children": ",".join(child_ids), "text": text},
            label="Carousel",
        )

    elif media_type == "VIDEO":
        container_id = _threads_container(
            threads_id, access_token,
            {"media_type": "VIDEO", "video_url": media_url, "text": text},
            label="Vidéo",
        )

    else:
        container_id = _threads_container(
            threads_id, access_token,
            {"media_type": "IMAGE", "image_url": media_url, "text": text},
            label="Image",
        )

    # Meta recommande explicitement 30 s d'attente avant de publier un conteneur
    # Threads ; le polling qui suit couvre les médias plus lourds.
    time.sleep(30)
    if not _poll_threads_container(container_id, access_token, label=media_type):
        return False

    rp = _post(
        f"{_THREADS_BASE}/{threads_id}/threads_publish",
        data={"creation_id": container_id, "access_token": access_token},
    )
    rp.raise_for_status()
    return True


# ==========================================
# BOUCLE DE PUBLICATION
# ==========================================

for payload_file in payload_dir.glob("*.json"):
    print(f"\n--- Traitement de {payload_file.name} ---")
    with open(payload_file) as f:
        payload = json.load(f)

    folder    = payload["compte"]
    pub_id    = payload["pub_id"]
    caption   = payload["caption"]
    next_time = int(payload["next_time"])

    # Rétrocompatibilité : anciens payloads n'ont que image_url
    media_type = payload.get("media_type", "IMAGE").upper()
    media_url  = payload.get("media_url") or payload.get("image_url", "")
    # image_url reste disponible pour les publications Facebook et Story image
    image_url  = payload.get("image_url") or (media_url if media_type == "IMAGE" else None)

    # Vérification si déjà publié
    if pub_id in published:
        print(f"[{pub_id}] Déjà dans published.json, on passe.")
        continue

    # Vérifier si c'est le moment de publier
    if next_time > now:
        swiss_time = datetime.fromtimestamp(next_time, tz=timezone.utc) + timedelta(hours=2)
        print(f"[{pub_id}] [WAIT] Programmation future : {swiss_time.strftime('%Y-%m-%d %H:%M:%S')}")
        continue

    # Secrets
    folder_upper = folder.upper()
    access_token  = os.environ.get(f"{folder_upper}_ACCESS_TOKEN")
    instagram_id  = os.environ.get(f"{folder_upper}_INSTAGRAM_ID")
    facebook_id   = os.environ.get(f"{folder_upper}_FACEBOOK_ID")
    # Threads : token PROPRE (l'app Meta est la même, le token ne l'est pas).
    # Absents tant que le compte n'a pas activé Threads → bloc simplement sauté.
    threads_token = os.environ.get(f"{folder_upper}_THREADS_TOKEN")
    threads_id    = os.environ.get(f"{folder_upper}_THREADS_ID")

    if not access_token or not instagram_id:
        err = f"{folder}: Secrets manquants (TOKEN ou INSTA_ID)"
        print(f"[FAIL] {err}")
        errors.append(err)
        continue

    # --- État par plateforme (reprise partielle) ---
    # Persisté DANS le payload, recommité en fin de run (git add -A plus bas).
    # Le payload n'est supprimé que lorsque toutes les plateformes visées sont
    # réglées : un échec Threads seul le laisse en place, et le run suivant
    # rejoue Threads UNIQUEMENT — Instagram et Facebook sont sautés parce que
    # déjà marqués. Sans ça, un échec sur une plateforme secondaire imposait un
    # choix perdant : jeter le post (perte définitive) ou tout republier (doublon).
    done     = dict(payload.get("done") or {})
    attempts = dict(payload.get("attempts") or {})

    def _should_try(platform, capped=True):
        if done.get(platform):
            return False
        return (not capped) or attempts.get(platform, 0) < MAX_PLATFORM_ATTEMPTS

    def _record(platform, ok):
        if ok:
            done[platform] = True
        else:
            attempts[platform] = attempts.get(platform, 0) + 1

    # --- Publication Instagram Feed ---
    # `success_insta` = publié À L'INSTANT (pilote les Stories, qui ne doivent
    # pas repartir lors d'un retry ciblant une autre plateforme).
    success_insta = False
    # Instagram n'est PAS plafonné : c'est la plateforme principale, abandonner
    # au bout de N essais reviendrait à perdre le post en silence. Facebook et
    # Threads le sont (cf. MAX_PLATFORM_ATTEMPTS) — un échec permanent y est
    # possible (Reel > 60 s en Story) et ne doit pas retenir le payload ni ses
    # assets Release indéfiniment.
    if _should_try("instagram", capped=False):
        try:
            if media_type == "VIDEO":
                print(f"[{pub_id}] [VID] Publication Reel Instagram...")
                success_insta, _ = publish_video(instagram_id, access_token, media_url, caption)
            elif media_type == "CAROUSEL":
                children = payload.get("children", [])
                print(f"[{pub_id}] [CAR] Publication Carousel Instagram ({len(children)} slides)...")
                success_insta, _ = publish_carousel(instagram_id, access_token, children, caption)
            else:
                print(f"[{pub_id}] [IMG] Publication image Instagram...")
                success_insta, _ = publish_image(instagram_id, access_token, media_url, caption)

            if success_insta:
                print(f"[{pub_id}] [OK] Post Instagram publié ({media_type})")

        except Exception as e:
            err = f"{folder}: Erreur Instagram Feed {pub_id} -> {e}"
            errors.append(err)
            print(f"[FAIL] {err}")

        _record("instagram", success_insta)

    # Publié maintenant OU lors d'un run précédent : conditionne les plateformes
    # secondaires, alors que `success_insta` (ce run seulement) pilote les Stories.
    insta_ok = bool(done.get("instagram"))

    # --- Publication Story Instagram ---
    if success_insta:
        if media_type == "IMAGE" and image_url:
            try:
                story_url    = f"https://graph.facebook.com/v25.0/{instagram_id}/media"
                story_params = {
                    "image_url":    image_url,
                    "media_type":   "STORIES",
                    "access_token": access_token
                }
                rs = _post(story_url, data=story_params)
                rs.raise_for_status()
                sm_id = rs.json()["id"]
                time.sleep(5)
                _post(
                    f"https://graph.facebook.com/v25.0/{instagram_id}/media_publish",
                    data={"creation_id": sm_id, "access_token": access_token}
                ).raise_for_status()
                print(f"[{pub_id}] [OK] Story image Instagram publiée")
            except Exception as e:
                print(f"[{pub_id}] [WARN] Story image Instagram échouée (ignoré) : {e}")

        elif media_type == "VIDEO" and media_url:
            try:
                publish_video_story(instagram_id, access_token, media_url)
                print(f"[{pub_id}] [OK] Story vidéo Instagram publiée")
            except Exception as e:
                # Non bloquant : les Reels longs (>60s) ne peuvent pas être en Story
                print(f"[{pub_id}] [WARN] Story vidéo Instagram échouée (ignoré) : {e}")

        elif media_type == "CAROUSEL" and payload.get("story_url"):
            # Story dérivée du carousel (cf. influencer/CLAUDE.md §5.10 C11 + §5.12)
            # Détection extension : .mp4 → STORIES video, .jpg → STORIES image
            try:
                s_url = f"https://graph.facebook.com/v25.0/{instagram_id}/media"
                story_url_val = payload["story_url"]
                is_video_story = story_url_val.lower().endswith(".mp4")

                if is_video_story:
                    story_params = {
                        "video_url":    story_url_val,
                        "media_type":   "STORIES",
                        "access_token": access_token,
                    }
                else:
                    story_params = {
                        "image_url":    story_url_val,
                        "media_type":   "STORIES",
                        "access_token": access_token,
                    }
                rs = _post(s_url, data=story_params)
                rs.raise_for_status()
                sm_id = rs.json()["id"]
                if is_video_story:
                    # Même pattern que publish_video_story : retry sur subcode 2207027
                    _publish_video_with_retry(instagram_id, access_token, sm_id,
                                              label="Story vidéo carousel", first_sleep=30,
                                              poll_every=15, max_wait=120)
                else:
                    time.sleep(5)
                    _post(
                        f"https://graph.facebook.com/v25.0/{instagram_id}/media_publish",
                        data={"creation_id": sm_id, "access_token": access_token}
                    ).raise_for_status()
                print(f"[{pub_id}] [OK] Story carousel Instagram publiée ({'video' if is_video_story else 'image'})")
            except Exception as e:
                print(f"[{pub_id}] [WARN] Story carousel Instagram échouée (ignoré) : {e}")

    # --- Publication Facebook ---
    if insta_ok and facebook_id and _should_try("facebook"):
        # True par défaut : un media_type sans branche Facebook est un no-op,
        # pas un échec — le compter ferait boucler le retry pour rien.
        fb_ok = True
        if media_type == "IMAGE" and image_url:
            try:
                fb_url = f"https://graph.facebook.com/v25.0/{facebook_id}/photos"
                _post(
                    fb_url,
                    data={"url": image_url, "caption": caption, "access_token": access_token}
                ).raise_for_status()
                print(f"[{pub_id}] [OK] Post Facebook image publié")
            except Exception as e:
                fb_ok = False
                err = f"{folder}: Erreur Facebook image {pub_id} -> {e}"
                errors.append(err)
                print(f"[FAIL] {err}")

        elif media_type == "VIDEO" and media_url:
            try:
                publish_video_facebook(facebook_id, access_token, media_url, caption)
                print(f"[{pub_id}] [OK] Post Facebook vidéo publié")
            except Exception as e:
                fb_ok = False
                err = f"{folder}: Erreur Facebook vidéo {pub_id} -> {e}"
                errors.append(err)
                print(f"[FAIL] {err}")

        elif media_type == "CAROUSEL":
            try:
                children = payload.get("children", [])
                # fb_children remplace children quand slide 0 est un .mp4 (musique muxée)
                # — l'endpoint /photos ne prend pas de vidéo
                fb_children = payload.get("fb_children", children)
                publish_carousel_facebook(facebook_id, access_token, fb_children, caption)
                print(f"[{pub_id}] [OK] Post Facebook carousel publié (photo de couverture)")
            except Exception as e:
                fb_ok = False
                err = f"{folder}: Erreur Facebook carousel {pub_id} -> {e}"
                errors.append(err)
                print(f"[FAIL] {err}")

        _record("facebook", fb_ok)

        # --- Publication Story Facebook ---
        # Imbriquée dans la tentative Facebook : sur un retry ciblant Threads, le
        # feed Facebook est déjà marqué `done`, donc la Story ne repart pas non
        # plus. Elle reste best-effort et n'influe pas sur `fb_ok` (un Reel > 60 s
        # ne peut pas être en Story — ça ne doit pas rejouer le post du feed).
        if fb_ok and media_type == "VIDEO" and media_url:
            try:
                publish_video_story_facebook(facebook_id, access_token, media_url)
                print(f"[{pub_id}] [OK] Story vidéo Facebook publiée")
            except Exception as e:
                # Remonté au dashboard : une Story qui échoue en silence est
                # indétectable autrement.
                err = f"{folder}: Story vidéo Facebook {pub_id} -> {e}"
                errors.append(err)
                print(f"[FAIL] {err}")

    # --- Publication Threads ---
    # Conditionnée à `insta_ok` (et non à la réussite de CE run) : sur un retry
    # ciblant Threads, Instagram est déjà marqué done et n'est pas republié.
    if insta_ok and threads_token and threads_id and _should_try("threads"):
        ok_threads = False
        try:
            print(f"[{pub_id}] [THR] Publication Threads ({media_type})...")
            # Tronquée au moment de la mise en file (limite 500 côté Threads) ;
            # repli défensif pour les payloads antérieurs à cette intégration.
            threads_text = payload.get("threads_caption") or caption[:500]
            ok_threads = publish_threads(
                threads_id, threads_token, media_type,
                media_url, payload.get("children", []), threads_text,
            )
            if ok_threads:
                print(f"[{pub_id}] [OK] Post Threads publié ({media_type})")
            else:
                err = f"{folder}: Threads {pub_id} -> conteneur non publiable"
                errors.append(err)
                print(f"[FAIL] {err}")
        except Exception as e:
            err = f"{folder}: Erreur Threads {pub_id} -> {e}"
            errors.append(err)
            print(f"[FAIL] {err}")

        _record("threads", ok_threads)

    # --- Bilan : toutes les plateformes visées sont-elles réglées ? ---
    targets = ["instagram"]
    if facebook_id:
        targets.append("facebook")
    if threads_token and threads_id:
        targets.append("threads")

    def _settled(p):
        return bool(done.get(p)) or attempts.get(p, 0) >= MAX_PLATFORM_ATTEMPTS

    # Instagram n'étant pas plafonné, `_settled` n'y vaut True qu'une fois publié :
    # tant qu'il échoue, rien n'est nettoyé (comportement historique préservé).
    all_settled = all(_settled(p) for p in targets)

    if not all_settled:
        # Le payload RESTE en place, avec son état mis à jour : le run suivant
        # ne rejouera que les plateformes non abouties. Les assets Release ne
        # sont pas supprimés non plus — ils sont encore nécessaires.
        payload["done"]     = done
        payload["attempts"] = attempts
        # encoding explicite : la légende porte accents et emojis, et `open`
        # sans encodage suit la locale de la machine.
        with open(payload_file, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
        pending = [p for p in targets if not _settled(p)]
        print(f"[{pub_id}] [WAIT] Reprise au prochain run : {', '.join(pending)}")
        continue

    # --- Nettoyage (toutes plateformes réglées) ---
    # Condition volontairement `all_settled` et non `success_insta` : quand
    # Instagram a réussi lors d'un run PRÉCÉDENT et qu'on vient de rattraper
    # Threads, `success_insta` est False — s'y fier laisserait le payload sur
    # disque à jamais, rejoué toutes les 15 minutes.
    if all_settled:
        published.add(pub_id)
        with open(published_file, "w") as f:
            json.dump(sorted(list(published)), f, indent=2)

        # Supprimer payload
        payload_file.unlink()

        # Supprimer les fichiers média
        # Discriminant : storage="release" (ou URL contenant /releases/download/) = Release assets
        # Sinon : chemin legacy (fichiers locaux dans to_publish/)
        _is_release = (
            payload.get("storage") == "release"
            or "/releases/download/" in (payload.get("media_url") or "")
            or any("/releases/download/" in u for u in payload.get("children", []))
        )
        try:
            if _is_release:
                # --- Nouveau chemin : supprimer les assets via API GitHub ---
                _gh_token  = os.environ.get("GITHUB_TOKEN", "")
                _gh_repo   = os.environ.get("GITHUB_REPOSITORY", "")
                _rel_tag   = "media-storage"
                _api_base  = "https://api.github.com"
                _gh_hdrs   = {
                    "Authorization": f"token {_gh_token}",
                    "Accept": "application/vnd.github+json",
                }
                def _delete_release_asset_by_url(asset_url):
                    """Supprime le Release asset identifié par son browser_download_url."""
                    if not asset_url or not _gh_token or not _gh_repo:
                        return
                    asset_name = pathlib.Path(asset_url).name
                    # Récupérer la release
                    r = _get(
                        f"{_api_base}/repos/{_gh_repo}/releases/tags/{_rel_tag}",
                        headers=_gh_hdrs, timeout=30
                    )
                    if r.status_code == 404:
                        return
                    r.raise_for_status()
                    release_id = r.json()["id"]
                    # Lister les assets et supprimer celui dont le name correspond
                    assets_url = f"{_api_base}/repos/{_gh_repo}/releases/{release_id}/assets?per_page=100"
                    while assets_url:
                        ra = _get(assets_url, headers=_gh_hdrs, timeout=30)
                        ra.raise_for_status()
                        for asset in ra.json():
                            if asset["name"] == asset_name:
                                requests.delete(
                                    f"{_api_base}/repos/{_gh_repo}/releases/assets/{asset['id']}",
                                    headers=_gh_hdrs, timeout=30
                                )
                                print(f"[DEL] Asset Release supprimé : {asset_name}")
                                return
                        link = ra.headers.get("Link", "")
                        assets_url = None
                        for part in link.split(","):
                            if 'rel="next"' in part:
                                assets_url = part.split(";")[0].strip().strip("<>")
                                break
                if media_type == "CAROUSEL":
                    urls_to_del = list(payload.get("children", []))
                    if payload.get("story_url"):
                        urls_to_del.append(payload["story_url"])
                    # fb_children peut contenir des doublons de children -- skip les vus
                    seen = set()
                    for fb_u in payload.get("fb_children", []):
                        if fb_u not in seen and fb_u not in urls_to_del:
                            urls_to_del.append(fb_u)
                        seen.add(fb_u)
                    for u in urls_to_del:
                        _delete_release_asset_by_url(u)
                else:
                    _delete_release_asset_by_url(payload.get("media_url") or payload.get("image_url"))
            else:
                # --- Chemin legacy : supprimer les fichiers locaux dans to_publish/ ---
                if media_type == "CAROUSEL":
                    urls = list(payload.get("children", []))
                    if payload.get("story_url"):
                        urls.append(payload["story_url"])
                    for child_url in urls:
                        child_name = pathlib.Path(child_url).name
                        child_local = base_dir / folder.lower() / "to_publish" / child_name
                        if child_local.exists():
                            child_local.unlink()
                            print(f"[DEL] Fichier local supprimé : {child_name}")
                else:
                    media_name  = pathlib.Path(media_url).name
                    media_local = base_dir / folder.lower() / "to_publish" / media_name
                    if media_local.exists():
                        media_local.unlink()
                        print(f"[DEL] Fichier média local supprimé : {media_name}")
        except Exception as _del_exc:
            print(f"[WARN] Erreur nettoyage média : {_del_exc}")


# ==========================================
# FINALISATION : DASHBOARD ET GIT
# ==========================================

# 1. Générer le Dashboard avec les fichiers RESTANTS
generate_dashboard(payload_dir, len(published), run_errors=errors)

# 2. Un seul Commit & Push pour tout le run
try:
    subprocess.run(["git", "config", "user.name", "github-actions"], cwd=base_dir, check=True)
    subprocess.run(["git", "config", "user.email", "actions@github.com"], cwd=base_dir, check=True)
    subprocess.run(["git", "add", "-A"], cwd=base_dir, check=True)
    status = subprocess.run(["git", "status", "--porcelain"], cwd=base_dir, capture_output=True, text=True)
    if status.stdout:
        subprocess.run(["git", "commit", "-m", "🤖 Update dashboard & published status"], cwd=base_dir, check=True)
        subprocess.run(["git", "pull", "--no-rebase"], cwd=base_dir, check=True)
        subprocess.run(["git", "push"], cwd=base_dir, check=True)
        print("[START] GitHub mis à jour avec succès.")
    else:
        print("∅ Aucun changement à commit.")
except Exception as e:
    print(f"[WARN] Erreur Git : {e}")

# --- Rapport final ---
if errors:
    print("\n=== RÉSUMÉ DES ERREURS ===")
    for e in errors:
        print(f" - {e}")
    # Exit code 1 : force GitHub Actions à marquer le run comme échoué
    # → déclenche l'email de notification de workflow si activé dans les paramètres GitHub
    sys.exit(1)

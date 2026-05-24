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

# --- Charger l'état des posts déjà publiés ---
if published_file.exists():
    with open(published_file) as f:
        published = set(json.load(f))
else:
    published = set()


def generate_dashboard(payload_dir, published_count, run_errors=None):
    """Génère un dashboard résumé par compte utilisateur.

    run_errors : liste des erreurs du run courant (affichées en haut du README si non vides).
    """
    stats_comptes = {}

    for p_file in payload_dir.glob("*.json"):
        try:
            with open(p_file) as f:
                data = json.load(f)
                compte = data["compte"].upper()
                ts     = int(data["next_time"])

                if compte not in stats_comptes:
                    stats_comptes[compte] = {
                        "count": 0,
                        "first": ts,
                        "last":  ts,
                        "thumb": data.get("image_url") or data.get("media_url", "")
                    }

                stats_comptes[compte]["count"] += 1
                if ts < stats_comptes[compte]["first"]:
                    stats_comptes[compte]["first"] = ts
                    stats_comptes[compte]["thumb"] = data.get("image_url") or data.get("media_url", "")
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
            thumb = f"<img src='{s['thumb']}' width='50'>" if s['thumb'] else "N/A"
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
    media_url    = f"https://graph.facebook.com/v23.0/{instagram_id}/media"
    media_params = {
        "image_url":    image_url,
        "caption":      caption,
        "access_token": access_token
    }
    r = requests.post(media_url, data=media_params)
    r.raise_for_status()
    media_id = r.json()["id"]

    publish_url    = f"https://graph.facebook.com/v23.0/{instagram_id}/media_publish"
    publish_params = {"creation_id": media_id, "access_token": access_token}
    time.sleep(2)
    rp = requests.post(publish_url, data=publish_params)
    rp.raise_for_status()
    return True, media_id


def _poll_instagram_container(container_id, access_token, max_wait=300, poll_every=10, label=""):
    """
    Attend que le conteneur Instagram passe au statut FINISHED.
    Lève une exception si ERROR, EXPIRED ou timeout.

    Note : seul 'status_code' est utilisé — le champ 'status' est invalide pour
    cet endpoint en API v23.0 et provoque un 400 immédiat.
    Les erreurs HTTP transitoires (400/5xx) sont retentées jusqu'à MAX_HTTP_ERR fois.
    """
    status_url    = f"https://graph.facebook.com/v23.0/{container_id}"
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
            rs = requests.get(status_url, params=status_params)
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
    media_url_endpoint = f"https://graph.facebook.com/v23.0/{instagram_id}/media"

    # 1. Upload chaque slide comme item de carousel
    for i, child_url in enumerate(children_urls):
        params = {
            "image_url":        child_url,
            "is_carousel_item": "true",
            "access_token":     access_token,
        }
        r = requests.post(media_url_endpoint, data=params)
        r.raise_for_status()
        item_id = r.json()["id"]
        item_ids.append(item_id)
        print(f"  [SLIDE {i+1}/{len(children_urls)}] Item créé : {item_id}")

    # 2. Créer le conteneur CAROUSEL
    container_params = {
        "media_type":   "CAROUSEL",
        "children":     ",".join(item_ids),
        "caption":      caption,
        "access_token": access_token,
    }
    rc = requests.post(media_url_endpoint, data=container_params)
    rc.raise_for_status()
    container_id = rc.json()["id"]
    print(f"  [CONTAINER] Carousel container créé : {container_id}")

    # 3. Attendre FINISHED
    _poll_instagram_container(container_id, access_token, max_wait=300, label="Carousel Instagram")

    # 4. Publier
    publish_url = f"https://graph.facebook.com/v23.0/{instagram_id}/media_publish"
    rp = requests.post(publish_url, data={"creation_id": container_id, "access_token": access_token})
    rp.raise_for_status()
    return True, container_id


def publish_video(instagram_id, access_token, video_url, caption):
    """
    Publie une vidéo sur Instagram en tant que Reel.

    Workflow :
      1. Créer le conteneur média avec media_type=REELS
      2. Attendre que le statut passe à FINISHED (polling)
      3. Publier via media_publish

    Retourne (success: bool, container_id: str).
    """
    media_url    = f"https://graph.facebook.com/v23.0/{instagram_id}/media"
    media_params = {
        "media_type":   "REELS",
        "video_url":    video_url,
        "caption":      caption,
        "access_token": access_token
    }
    r = requests.post(media_url, data=media_params)
    r.raise_for_status()
    creation_resp = r.json()
    print(f"  [PKG] Réponse création Reel : {creation_resp}")
    container_id = creation_resp["id"]
    print(f"  [PKG] Conteneur Reel Instagram créé : {container_id}")

    _poll_instagram_container(container_id, access_token, max_wait=300, label="Reel Instagram")

    publish_url    = f"https://graph.facebook.com/v23.0/{instagram_id}/media_publish"
    publish_params = {"creation_id": container_id, "access_token": access_token}
    rp = requests.post(publish_url, data=publish_params)
    rp.raise_for_status()
    return True, container_id


def publish_video_story(instagram_id, access_token, video_url):
    """
    Publie une vidéo en Story Instagram.

    Contrainte Meta : la vidéo doit durer entre 3 et 60 secondes.
    Workflow identique aux Reels (polling requis avant publication).

    Retourne (success: bool).
    """
    media_url    = f"https://graph.facebook.com/v23.0/{instagram_id}/media"
    media_params = {
        "media_type":   "STORIES",
        "video_url":    video_url,
        "access_token": access_token
    }
    r = requests.post(media_url, data=media_params)
    r.raise_for_status()
    container_id = r.json()["id"]
    print(f"  [PKG] Conteneur Story vidéo créé : {container_id}")

    # Délai réduit pour les Stories (vidéos courtes)
    _poll_instagram_container(container_id, access_token, max_wait=120, label="Story vidéo")

    publish_url    = f"https://graph.facebook.com/v23.0/{instagram_id}/media_publish"
    publish_params = {"creation_id": container_id, "access_token": access_token}
    rp = requests.post(publish_url, data=publish_params)
    rp.raise_for_status()
    return True


def publish_carousel_facebook(facebook_id, access_token, children_urls, caption):
    """Publie un carousel multi-photos sur une Page Facebook.

    Workflow Facebook Graph API :
      1. Upload chaque image en mode `published=false` → récupère N `media_fbid`
      2. Créer un post avec `attached_media[i]={"media_fbid": id}` + `message=caption`

    Retourne (success: bool, post_id: str | None).
    """
    media_fbids = []

    # 1. Upload chaque slide en photo non-publiée
    photos_endpoint = f"https://graph.facebook.com/v23.0/{facebook_id}/photos"
    for i, url in enumerate(children_urls):
        photo_params = {
            "url":          url,
            "published":    "false",
            "access_token": access_token,
        }
        r = requests.post(photos_endpoint, data=photo_params)
        r.raise_for_status()
        fbid = r.json()["id"]
        media_fbids.append(fbid)
        print(f"  [SLIDE FB {i+1}/{len(children_urls)}] media_fbid={fbid}")

    # 2. Créer le post avec les attached_media référencés
    feed_endpoint = f"https://graph.facebook.com/v23.0/{facebook_id}/feed"
    feed_params = {
        "message":      caption,
        "access_token": access_token,
    }
    for i, fbid in enumerate(media_fbids):
        feed_params[f"attached_media[{i}]"] = json.dumps({"media_fbid": fbid})

    rp = requests.post(feed_endpoint, data=feed_params)
    rp.raise_for_status()
    post_id = rp.json().get("id", "")
    print(f"  [POST FB] Carousel publié : {post_id}")
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

    Retourne (success: bool).
    """
    # Étape 1 : Initialiser
    r = requests.post(
        f"https://graph.facebook.com/v23.0/{facebook_id}/video_reels",
        data={"upload_phase": "start", "access_token": access_token}
    )
    r.raise_for_status()
    video_id = r.json()["video_id"]
    print(f"  [PKG] Reel Facebook initialisé : {video_id}")

    # Étape 2 : Upload depuis URL hébergée
    ru = requests.post(
        f"https://rupload.facebook.com/video-upload/v23.0/{video_id}",
        headers={
            "Authorization": f"OAuth {access_token}",
            "file_url":      video_url,
        }
    )
    ru.raise_for_status()
    print(f"  [UP] Vidéo transmise à Meta")

    # Étape 3 : Publier
    rp = requests.post(
        f"https://graph.facebook.com/v23.0/{facebook_id}/video_reels",
        data={
            "video_id":     video_id,
            "upload_phase": "finish",
            "video_state":  "PUBLISHED",
            "description":  caption,
            "access_token": access_token,
        }
    )
    rp.raise_for_status()
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
    r = requests.post(
        f"https://graph.facebook.com/v23.0/{facebook_id}/video_stories",
        data={"upload_phase": "start", "access_token": access_token}
    )
    r.raise_for_status()
    data     = r.json()
    video_id = data["video_id"]
    # Meta retourne parfois une upload_url directe, sinon on construit la nôtre
    upload_url = data.get("upload_url") or f"https://rupload.facebook.com/video-upload/v23.0/{video_id}"
    print(f"  [PKG] Story Facebook initialisée : {video_id}")

    # Étape 2 : Upload depuis URL hébergée
    ru = requests.post(
        upload_url,
        headers={
            "Authorization": f"OAuth {access_token}",
            "file_url":      video_url,
        }
    )
    ru.raise_for_status()

    # Étape 3 : Publier
    rp = requests.post(
        f"https://graph.facebook.com/v23.0/{facebook_id}/video_stories",
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
    access_token = os.environ.get(f"{folder_upper}_ACCESS_TOKEN")
    instagram_id = os.environ.get(f"{folder_upper}_INSTAGRAM_ID")
    facebook_id  = os.environ.get(f"{folder_upper}_FACEBOOK_ID")

    if not access_token or not instagram_id:
        err = f"{folder}: Secrets manquants (TOKEN ou INSTA_ID)"
        print(f"[FAIL] {err}")
        errors.append(err)
        continue

    # --- Publication Instagram Feed ---
    success_insta = False
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

    # --- Publication Story Instagram ---
    if success_insta:
        if media_type == "IMAGE" and image_url:
            try:
                story_url    = f"https://graph.facebook.com/v23.0/{instagram_id}/media"
                story_params = {
                    "image_url":    image_url,
                    "media_type":   "STORIES",
                    "access_token": access_token
                }
                rs = requests.post(story_url, data=story_params)
                rs.raise_for_status()
                sm_id = rs.json()["id"]
                time.sleep(5)
                requests.post(
                    f"https://graph.facebook.com/v23.0/{instagram_id}/media_publish",
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
            # Story dérivée du carousel (cf. influencer/CLAUDE.md §5.10 C11)
            try:
                s_url        = f"https://graph.facebook.com/v23.0/{instagram_id}/media"
                story_params = {
                    "image_url":    payload["story_url"],
                    "media_type":   "STORIES",
                    "access_token": access_token,
                }
                rs = requests.post(s_url, data=story_params)
                rs.raise_for_status()
                sm_id = rs.json()["id"]
                time.sleep(5)
                requests.post(
                    f"https://graph.facebook.com/v23.0/{instagram_id}/media_publish",
                    data={"creation_id": sm_id, "access_token": access_token}
                ).raise_for_status()
                print(f"[{pub_id}] [OK] Story carousel Instagram publiée")
            except Exception as e:
                print(f"[{pub_id}] [WARN] Story carousel Instagram échouée (ignoré) : {e}")

    # --- Publication Facebook ---
    if success_insta and facebook_id:
        if media_type == "IMAGE" and image_url:
            try:
                fb_url = f"https://graph.facebook.com/v23.0/{facebook_id}/photos"
                requests.post(
                    fb_url,
                    data={"url": image_url, "caption": caption, "access_token": access_token}
                ).raise_for_status()
                print(f"[{pub_id}] [OK] Post Facebook image publié")
            except Exception as e:
                print(f"[{pub_id}] [WARN] Erreur Facebook image : {e}")

        elif media_type == "VIDEO" and media_url:
            try:
                publish_video_facebook(facebook_id, access_token, media_url, caption)
                print(f"[{pub_id}] [OK] Post Facebook vidéo publié")
            except Exception as e:
                print(f"[{pub_id}] [WARN] Erreur Facebook vidéo : {e}")

        elif media_type == "CAROUSEL":
            try:
                children = payload.get("children", [])
                publish_carousel_facebook(facebook_id, access_token, children, caption)
                print(f"[{pub_id}] [OK] Post Facebook carousel publié ({len(children)} photos)")
            except Exception as e:
                print(f"[{pub_id}] [WARN] Erreur Facebook carousel : {e}")

    # --- Publication Story Facebook ---
    if success_insta and facebook_id:
        if media_type == "VIDEO" and media_url:
            try:
                publish_video_story_facebook(facebook_id, access_token, media_url)
                print(f"[{pub_id}] [OK] Story vidéo Facebook publiée")
            except Exception as e:
                # Non bloquant : les Reels longs (>60s) ne peuvent pas être en Story
                print(f"[{pub_id}] [WARN] Story vidéo Facebook échouée (ignoré) : {e}")

    # --- Nettoyage si succès ---
    if success_insta:
        published.add(pub_id)
        with open(published_file, "w") as f:
            json.dump(sorted(list(published)), f, indent=2)

        # Supprimer payload
        payload_file.unlink()

        # Supprimer les fichiers média locaux
        try:
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
        except Exception:
            pass


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

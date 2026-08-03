"""
process_payloads.py — publication multi-réseaux, exécutée par GitHub Actions.

Script AUTONOME : il tourne sur le runner, copié dans le repo de publication par
`push_scripts_to_repo`. Il ne peut importer AUCUN module du projet — toute
factorisation doit rester interne à ce fichier (c'est l'une des quatre exceptions
à la règle « pas de print() » de CLAUDE.md §2).

RÉSEAUX COUVERTS
    Instagram   post image · carousel · reel      + Story
    Facebook    post image · carousel · reel      + Story
    Threads     post image · carousel · vidéo

EXIGENCES TENUES ICI (référence : CLAUDE.md §5.6bis)

 1. Reprise INDÉPENDANTE par réseau. L'état vit dans le payload (`done` /
    `attempts`), recommité en fin de run. Un échec Threads ne republie jamais
    Instagram ni Facebook ; seule la cible en échec est rejouée.

 2. Nettoyage seulement quand TOUT est réglé. Le payload et ses assets Release
    ne sont supprimés que lorsque chaque cible bloquante est publiée ou à court
    de tentatives. Les Stories, best effort, ne retiennent jamais rien.

 3. Instagram n'est JAMAIS abandonné (`max_attempts = None`). Le plafonner
    supprimerait un post sans l'avoir publié — c'est exactement la régression
    qu'une première version avait introduite.

 4. Une seule table décrit les cibles (`TARGETS`) et une seule boucle les
    exécute. Ajouter un réseau = une ligne dans TARGETS + une entrée dans
    `actions`. Les blocs par réseau copiés-collés, chacun avec son drapeau et
    son format d'erreur, ont été supprimés : c'est ce qui garantit que les
    règles 1 à 3 vaudront aussi pour le prochain réseau.

 5. Deux niveaux de signalement. `_fail` (bloquant) alimente le dashboard ET
    l'issue GitHub ouverte par le workflow ; `_warn` (best effort) n'alimente
    que le dashboard — une Story refusée parce que le reel dépasse 60 s est
    normale et ne doit pas ouvrir d'issue.

 6. Le dashboard (README du repo) est la seule fenêtre sur le runner. Il porte
    les erreurs, les avertissements, les tokens proches de l'échéance
    (TOKEN_WARN_DAYS), l'autonomie de chaque file (QUEUE_WARN_DAYS) et les
    publications en cours de reprise.

 7. Stabilité : tout appel HTTP a un timeout (API_TIMEOUT), les sondages
    abandonnent après MAX_HTTP_ERR erreurs consécutives au lieu d'épuiser leur
    budget en silence, et les sondages vérifient AVANT de dormir.

 8. L'état est persisté APRÈS CHAQUE CIBLE, pas en fin de traitement. Entre la
    publication Instagram et la fin de Threads il s'écoule parfois plusieurs
    minutes (transcodage côté Meta) : un runner tué dans cet intervalle perdait
    tout l'état, et le run suivant republiait Instagram.

 9. `DRY_RUN=1` rejoue toute l'orchestration sans appeler les APIs et sans rien
    modifier sur disque (payloads, published.json, assets Release, git). Seul
    moyen de valider la logique de reprise sur de vrais payloads sans publier.
"""

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

errors    = []   # bloquants  → dashboard + issue GitHub
warn_msgs = []   # best effort → dashboard seulement (nom non `warnings` : module standard)
now = int(time.time())

# --- Mode simulation ---
# `DRY_RUN=1` exécute toute l'orchestration — sélection des cibles, comptage des
# tentatives, décision de nettoyage, dashboard — SANS appeler les APIs et SANS
# rien modifier sur disque (payloads, published.json, assets Release, git).
# C'est le seul moyen de valider la logique de reprise sur de vrais payloads
# sans publier pour de bon.
DRY_RUN = os.environ.get("DRY_RUN", "").strip().lower() in ("1", "true", "yes", "on")
if DRY_RUN:
    print("[DRY] Mode simulation : aucune publication, aucune écriture.")

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


def _fail(msg):
    """
    Échec BLOQUANT : dashboard + issue GitHub (le workflow ouvre une issue quand
    le script sort en erreur).
    """
    errors.append(msg)
    print(f"[FAIL] {msg}")


def _warn(msg):
    """
    Échec BEST EFFORT (Stories) : visible au dashboard, mais sans issue GitHub.

    Une Story qui échoue est fréquente et légitime (un Reel de plus de 60 s ne
    peut pas être publié en Story) — la remonter comme une erreur bloquante
    ouvrirait une issue à chaque reel long. La taire complètement, à l'inverse,
    la rendait indétectable : d'où ce niveau intermédiaire.
    """
    warn_msgs.append(msg)
    print(f"[WARN] {msg}")


def _write_payload_state(payload_file, payload, state):
    """
    Persiste `done` / `attempts` dans le payload, sur disque, IMMÉDIATEMENT.

    Appelé après CHAQUE cible et non en fin de traitement : entre la publication
    Instagram et la fin de Threads il peut s'écouler plusieurs minutes (une vidéo
    est transcodée côté Meta), et un runner tué dans cet intervalle — timeout du
    job, annulation, OOM — perdait tout l'état. Le run suivant republiait alors
    Instagram : précisément le doublon que ce mécanisme existe pour empêcher.
    """
    if DRY_RUN:
        return
    state.save_into(payload)
    # encoding explicite : la légende porte accents et emojis, et `open` sans
    # encodage suit la locale de la machine.
    with open(payload_file, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)


def _is_video(url) -> bool:
    """Un média est une vidéo si son URL se termine en .mp4 (convention du projet)."""
    return str(url or "").lower().endswith(".mp4")


# ==========================================
# CIBLES DE PUBLICATION — reprise par plateforme
# ==========================================
#
# EXIGENCES (voir aussi influencer/CLAUDE.md §5.6bis) :
#   1. Chaque réseau a des tentatives INDÉPENDANTES. Un échec Threads ne
#      republie jamais Instagram ni Facebook.
#   2. Le payload (et ses assets Release) n'est nettoyé que lorsque toutes les
#      cibles BLOQUANTES sont réglées — publiées, ou à court de tentatives.
#   3. L'état vit DANS le payload (`done` / `attempts`), recommité en fin de run.
#      Il survit donc aux redémarrages du runner.
#
# Une cible est décrite ici et NULLE PART ailleurs : ajouter un réseau = une
# ligne dans TARGETS + une entrée dans `_build_actions`. Les trois anciens blocs
# copiés-collés (chacun avec son drapeau, son try/except et son format d'erreur)
# ont disparu — c'est ce qui garantit que la règle 1 vaut aussi pour le suivant.
#
#   name        clé dans done/attempts
#   label       libellé humain (logs, dashboard)
#   max_attempts nombre d'essais avant abandon ; None = illimité
#   blocking    True  → retient le nettoyage tant qu'il n'est pas réglé
#               False → best effort (Stories), n'empêche jamais le nettoyage
#   depends_on  cible qui doit être `done` avant d'essayer celle-ci
#
# Instagram est volontairement ILLIMITÉ : c'est la publication principale.
# L'abandonner au bout de N essais supprimerait le post sans l'avoir publié.
# Facebook et Threads sont plafonnés car un échec permanent y est possible
# (Reel > 60 s refusé en Story, permission révoquée) et ne doit pas retenir
# indéfiniment le payload ni ses assets.
TARGETS = [
    # name,              label,               max_attempts, blocking, depends_on
    ("instagram",        "Instagram",         None,         True,     None),
    ("instagram_story",  "Story Instagram",   1,            False,    "instagram"),
    ("facebook",         "Facebook",          3,            True,     "instagram"),
    ("facebook_story",   "Story Facebook",    1,            False,    "facebook"),
    ("threads",          "Threads",           3,            True,     "instagram"),
]

# Seuils d'alerte du dashboard.
TOKEN_WARN_DAYS = 10   # token proche de l'échéance
QUEUE_WARN_DAYS = 3    # file d'attente bientôt vide


class PayloadState:
    """
    État de publication d'un payload, persisté dans le fichier lui-même.

    `done`     : {cible: True} — publié, définitivement.
    `attempts` : {cible: n}    — essais consommés, pour le plafond.

    Objet plutôt que closures : l'ancienne version redéfinissait trois fonctions
    par itération de boucle, et sa règle de plafond était écrite à deux endroits
    qui avaient déjà divergé — Instagram était exempté à l'essai mais plafonné au
    bilan, donc trois échecs suffisaient à supprimer un post jamais publié.
    """

    def __init__(self, payload):
        self.done     = dict(payload.get("done") or {})
        self.attempts = dict(payload.get("attempts") or {})

    def settled(self, name, max_attempts):
        """Réglée = publiée, ou à court de tentatives (jamais si illimitée)."""
        if self.done.get(name):
            return True
        if max_attempts is None:
            return False
        return self.attempts.get(name, 0) >= max_attempts

    def should_try(self, name, max_attempts):
        return not self.settled(name, max_attempts)

    def record(self, name, ok):
        if ok:
            self.done[name] = True
        else:
            self.attempts[name] = self.attempts.get(name, 0) + 1

    def save_into(self, payload):
        payload["done"]     = self.done
        payload["attempts"] = self.attempts

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


def _token_expiry_rows():
    """
    Échéances de token connues, lues dans les variables du repo.

    Le runner ne peut pas DEVINER la date d'expiration : l'API Threads n'expose
    pas d'endpoint de debug, et sonder le token ne dirait que « valide / plus
    valide » — trop tard pour prévenir. Le dashboard pousse donc l'échéance en
    variable `{COMPTE}_THREADS_TOKEN_EXPIRES` au moment où il l'obtient, et le
    script se contente de la lire.

    Le token Facebook/Instagram est un Page access token permanent (§Pb4) : il
    n'a pas d'échéance à surveiller.
    """
    rows = []
    for key, value in os.environ.items():
        if not key.endswith("_THREADS_TOKEN_EXPIRES"):
            continue
        compte = key[: -len("_THREADS_TOKEN_EXPIRES")]
        try:
            expires_at = int(value)
        except (TypeError, ValueError):
            continue
        if expires_at <= 0:
            continue
        rows.append((compte, (expires_at - now) / 86400.0))
    return sorted(rows)


def _stuck_rows(payload_dir):
    """Payloads ayant déjà consommé au moins une tentative sur une cible."""
    rows = []
    for p_file in payload_dir.glob("*.json"):
        try:
            with open(p_file, encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            continue
        attempts = data.get("attempts") or {}
        if not attempts:
            continue
        done = [k for k, v in (data.get("done") or {}).items() if v]
        rows.append((
            data.get("compte", "?").upper(),
            data.get("pub_id", "?")[:8],
            ", ".join(f"{k}×{v}" for k, v in sorted(attempts.items())),
            ", ".join(sorted(done)) or "—",
        ))
    return sorted(rows)


def generate_dashboard(payload_dir, published_count, run_errors=None, run_warnings=None):
    """
    Génère le dashboard (README du repo) : état des files, échéances de tokens,
    publications en cours de reprise, erreurs et avertissements du run.

    Le README est la SEULE fenêtre sur ce qui se passe côté runner — tout ce qui
    n'y figure pas est invisible tant qu'on ne lit pas les logs Actions.
    """
    stats_comptes = {}

    for p_file in payload_dir.glob("*.json"):
        try:
            with open(p_file, encoding="utf-8") as f:
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
    md = "# 📊 Dashboard de Publication\n\n"

    # --- Erreurs bloquantes (ouvrent une issue GitHub) ---
    if run_errors:
        md += f"## ❌ Erreurs du dernier run ({now_str})\n\n"
        for err in run_errors:
            md += f"- `{err}`\n"
        md += "\n"
    else:
        md += f"✅ **Dernier run sans erreur** — {now_str}\n\n"

    # --- Avertissements best effort (Stories) ---
    if run_warnings:
        md += "## ⚠️ Avertissements (best effort, non bloquants)\n\n"
        for w in run_warnings:
            md += f"- `{w}`\n"
        md += "\n"

    # --- Échéances de tokens ---
    token_rows = _token_expiry_rows()
    expiring   = [(c, d) for c, d in token_rows if d < TOKEN_WARN_DAYS]
    if expiring:
        md += "## 🔑 Tokens à renouveler\n\n"
        for compte, days in expiring:
            if days < 0:
                md += f"- ❌ **{compte}** — token Threads **EXPIRÉ** depuis {abs(days):.0f} j\n"
            else:
                md += f"- ⚠️ **{compte}** — token Threads expire dans **{days:.0f} j**\n"
        md += "\n> Renouveler dans 👤 Compte → 🔧 Actions Threads → ♻️ Prolonger de 60 jours, "
        md += "puis 📤 Envoyer les IDs vers GitHub.\n\n"

    md += f"📦 **Total publiés historiquement :** {published_count}\n\n"

    # --- Files d'attente ---
    if not stats_comptes:
        md += "### 🎉 Toutes les files d'attente sont vides !\n\n"
    else:
        md += "### 📱 État des comptes\n"
        md += "| Compte | Posts en attente | Prochaine publication | Fin de programmation | Autonomie | Aperçu |\n"
        md += "| :--- | :---: | :--- | :--- | :---: | :---: |\n"

        low_runway = []
        for compte in sorted(stats_comptes.keys()):
            s = stats_comptes[compte]
            date_next = (datetime.fromtimestamp(s["first"], tz=timezone.utc) + timedelta(hours=2)).strftime('%d/%m %H:%M')
            date_last = (datetime.fromtimestamp(s["last"],  tz=timezone.utc) + timedelta(hours=2)).strftime('%d/%m %H:%M')
            # Autonomie = temps couvert par la programmation restante. C'est LUI
            # qui dit s'il faut relancer une génération, pas le nombre de posts :
            # 3 posts espacés de 12 h tiennent plus longtemps que 6 espacés de 2 h.
            runway = (s["last"] - now) / 86400.0
            if runway < QUEUE_WARN_DAYS:
                low_runway.append((compte, runway))
                runway_cell = f"⚠️ **{runway:.1f} j**"
            else:
                runway_cell = f"{runway:.1f} j"
            thumb = _thumb_cell(s["thumb"], s["thumb_type"])
            md += f"| {compte} | **{s['count']}** | {date_next} | {date_last} | {runway_cell} | {thumb} |\n"

        md += "\n"
        if low_runway:
            md += f"> ⚠️ **File bientôt vide** (moins de {QUEUE_WARN_DAYS} jours de programmation) : "
            md += ", ".join(f"**{c}** ({d:.1f} j)" for c, d in low_runway)
            md += " — pensez à générer de nouveaux posts.\n\n"

    # --- Publications en cours de reprise ---
    stuck = _stuck_rows(payload_dir)
    if stuck:
        md += "### 🔁 Publications en cours de reprise\n"
        md += "| Compte | Pub | Tentatives échouées | Déjà publié sur |\n"
        md += "| :--- | :--- | :--- | :--- |\n"
        for compte, pub, att, done in stuck:
            md += f"| {compte} | `{pub}` | {att} | {done} |\n"
        md += "\n> Chaque réseau est réessayé indépendamment : ce qui est déjà publié ne repart pas.\n\n"

    readme_path = pathlib.Path(__file__).parent.parent / "README.md"
    with open(readme_path, "w", encoding="utf-8") as f:
        f.write(md)
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


def _poll_threads_container(container_id, access_token, max_wait=180, poll_every=5, label=""):
    """
    Attend qu'un conteneur Threads passe en FINISHED.

    Publier un conteneur encore en cours de transcodage renvoie une erreur peu
    explicite, d'où l'attente. La boucle SONDE AVANT de dormir : un conteneur
    image, prêt en une seconde, ne coûte donc rien — c'est ce qui a permis de
    supprimer le `sleep(30)` forfaitaire que Meta recommande aux intégrations
    qui, elles, ne sondent pas (30 s brûlées par publication, et par réessai).

    Un statut non-200 répété (token révoqué, conteneur supprimé) abandonne au
    bout de MAX_HTTP_ERR au lieu d'épuiser `max_wait` en silence.
    """
    MAX_HTTP_ERR = 3
    http_err = 0
    waited   = 0
    while waited < max_wait:
        r = _get(
            f"{_THREADS_BASE}/{container_id}",
            params={"fields": "status,error_message", "access_token": access_token},
        )
        if r.status_code == 200:
            http_err = 0
            data     = r.json()
            status   = data.get("status", "")
            if status == "FINISHED":
                return True
            if status == "ERROR":
                print(f"  [FAIL] {label} conteneur en erreur : {data.get('error_message', '')}")
                return False
        else:
            http_err += 1
            print(f"  [WARN] Erreur HTTP {r.status_code} en sondant {label} ({http_err}/{MAX_HTTP_ERR})")
            if http_err >= MAX_HTTP_ERR:
                print(f"  [FAIL] {label} : {MAX_HTTP_ERR} erreurs HTTP consécutives, abandon")
                return False
        time.sleep(poll_every)
        waited += poll_every
    print(f"  [FAIL] {label} conteneur non prêt après {max_wait}s")
    return False


def _publish_ig_story(instagram_id, access_token, url, label="Story"):
    """
    Publie une Story Instagram depuis une URL publique (image ou vidéo).

    Factorise les deux blocs qui construisaient le conteneur STORIES à la main
    dans la boucle : le type est dérivé de l'extension, et une story vidéo passe
    par le retry sur subcode 2207027, comme `publish_video_story`.
    """
    is_video = _is_video(url)
    key      = "video_url" if is_video else "image_url"
    r = _post(
        f"https://graph.facebook.com/v25.0/{instagram_id}/media",
        data={key: url, "media_type": "STORIES", "access_token": access_token},
    )
    r.raise_for_status()
    sm_id = r.json()["id"]
    if is_video:
        _publish_video_with_retry(instagram_id, access_token, sm_id, label=label,
                                  first_sleep=30, poll_every=15, max_wait=120)
    else:
        time.sleep(5)
        _post(
            f"https://graph.facebook.com/v25.0/{instagram_id}/media_publish",
            data={"creation_id": sm_id, "access_token": access_token},
        ).raise_for_status()
    return True


def _threads_check(r, what):
    """
    Lève une erreur PORTANT le message de Meta, pas seulement le code HTTP.

    `raise_for_status()` ne produit que « 400 Client Error: Bad Request for url:
    … » — inexploitable, alors que Meta décrit précisément le champ fautif dans
    le corps de la réponse. Un 400 sur Threads est indiagnosticable sans lui.
    """
    if r.status_code < 400:
        return
    try:
        payload = r.json()
        detail  = json.dumps(payload.get("error", payload), ensure_ascii=False)
    except Exception:
        detail = (r.text or "")[:400]
    raise RuntimeError(f"{what} → HTTP {r.status_code} : {detail}")


def _truncate_utf8(text, limit):
    """
    Tronque à `limit` OCTETS UTF-8, sur une frontière de mot.

    Repli pour les payloads antérieurs à `threads_caption` : `text[:limit]`
    coupait à 500 *caractères*, ce qui dépasse 500 octets dès que la légende
    porte des accents (2 octets) ou des emojis (4) — soit toutes les nôtres.
    """
    raw = (text or "").encode("utf-8")
    if len(raw) <= limit:
        return text or ""
    cut   = raw[:limit].decode("utf-8", errors="ignore")
    space = cut.rfind(" ")
    return (cut[:space] if space > limit * 0.6 else cut).rstrip()


def _threads_media_params(url):
    """Type de média + clé d'URL pour un conteneur Threads, dérivés de l'extension."""
    key = "video_url" if _is_video(url) else "image_url"
    return {"media_type": "VIDEO" if _is_video(url) else "IMAGE", key: url}


def _threads_container(threads_id, access_token, params, label=""):
    """Crée un conteneur Threads et retourne son id."""
    r = _post(f"{_THREADS_BASE}/{threads_id}/threads",
              data={**params, "access_token": access_token})
    # Champs journalisés pour rendre un 400 lisible : le corps de la réponse dit
    # QUEL champ est refusé, encore faut-il savoir ce qu'on a envoyé.
    _threads_check(r, f"Conteneur {label} ({params.get('media_type')}, "
                      f"texte {len((params.get('text') or '').encode('utf-8'))} octets)")
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

    Retourne True si publié.
    """
    if media_type == "CAROUSEL":
        # Garde AVANT la boucle : la tester après aurait créé des conteneurs
        # pour rien, et consommé une tentative sur un payload voué à l'échec.
        if len(children) < 2:
            print(f"  [FAIL] Carousel Threads : {len(children)} item(s), minimum 2")
            return False

        # Chaque item devient un conteneur enfant, puis un conteneur parent les
        # agrège. Threads accepte 2 à 20 enfants — les carousels du projet sont
        # plafonnés à 10 par l'API Instagram, donc toujours dans les clous.
        child_ids = [
            _threads_container(threads_id, access_token,
                               {**_threads_media_params(url), "is_carousel_item": "true"},
                               label=f"Slide {i}")
            for i, url in enumerate(children)
        ]
        container_id = _threads_container(
            threads_id, access_token,
            {"media_type": "CAROUSEL", "children": ",".join(child_ids), "text": text},
            label="Carousel",
        )
    else:
        container_id = _threads_container(
            threads_id, access_token,
            {**_threads_media_params(media_url), "text": text},
            label="Vidéo" if _is_video(media_url) else "Image",
        )

    if not _poll_threads_container(container_id, access_token, label=media_type):
        return False

    rp = _post(
        f"{_THREADS_BASE}/{threads_id}/threads_publish",
        data={"creation_id": container_id, "access_token": access_token},
    )
    _threads_check(rp, f"Publication {media_type}")
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

    # --- État de publication (reprise partielle — voir TARGETS en tête) ---
    state = PayloadState(payload)

    children     = payload.get("children", [])
    fb_children  = payload.get("fb_children", children)
    ig_story_url = payload.get("story_url")
    # Légende tronquée à la mise en file (limite Threads, en OCTETS UTF-8).
    # Le repli sert aux payloads antérieurs à `threads_caption` et doit compter
    # en octets lui aussi : `caption[:500]` laissait passer ~700 octets sur une
    # légende accentuée avec emojis, refusée en 400 par Meta.
    threads_text = payload.get("threads_caption") or _truncate_utf8(caption, 500)

    def _do_instagram():
        if media_type == "VIDEO":
            print(f"[{pub_id}] [VID] Publication Reel Instagram...")
            ok, _ = publish_video(instagram_id, access_token, media_url, caption)
        elif media_type == "CAROUSEL":
            print(f"[{pub_id}] [CAR] Publication Carousel Instagram ({len(children)} slides)...")
            ok, _ = publish_carousel(instagram_id, access_token, children, caption)
        else:
            print(f"[{pub_id}] [IMG] Publication image Instagram...")
            ok, _ = publish_image(instagram_id, access_token, media_url, caption)
        return ok

    def _do_facebook():
        if media_type == "CAROUSEL":
            # fb_children remplace children quand slide 0 est un .mp4 (musique
            # muxée) — l'endpoint /photos ne prend pas de vidéo.
            # ⚠️ Renvoie un TUPLE (ok, post_id) et n'écrit pas d'exception en cas
            # d'échec : il FAUT le déballer. Le rendre tel quel marquait le
            # réseau publié même en échec — un tuple est toujours vrai.
            ok, _ = publish_carousel_facebook(facebook_id, access_token, fb_children, caption)
            return ok
        if media_type == "VIDEO":
            return publish_video_facebook(facebook_id, access_token, media_url, caption)
        _post(
            f"https://graph.facebook.com/v25.0/{facebook_id}/photos",
            data={"url": image_url, "caption": caption, "access_token": access_token},
        ).raise_for_status()
        return True

    # Actions applicables à CE payload. Une cible absente de ce dict n'est ni
    # tentée, ni comptée, et ne retient pas le nettoyage : « non applicable »
    # (identifiants manquants, format sans équivalent sur ce réseau) cesse d'être
    # confondu avec « publié », ce que faisait l'ancien drapeau `fb_ok = True`.
    actions = {"instagram": _do_instagram}

    if media_type == "IMAGE" and image_url:
        actions["instagram_story"] = lambda: _publish_ig_story(
            instagram_id, access_token, image_url, "Story image")
    elif media_type == "VIDEO" and media_url:
        actions["instagram_story"] = lambda: publish_video_story(
            instagram_id, access_token, media_url)
    elif media_type == "CAROUSEL" and ig_story_url:
        # Story dérivée du carousel (cf. influencer/CLAUDE.md §5.10 C11).
        actions["instagram_story"] = lambda: _publish_ig_story(
            instagram_id, access_token, ig_story_url, "Story carousel")

    if facebook_id and (media_type != "IMAGE" or image_url):
        actions["facebook"] = _do_facebook
        if media_type == "VIDEO" and media_url:
            actions["facebook_story"] = lambda: publish_video_story_facebook(
                facebook_id, access_token, media_url)

    if threads_token and threads_id:
        actions["threads"] = lambda: publish_threads(
            threads_id, threads_token, media_type, media_url, children, threads_text)

    # --- Orchestration : une seule boucle pour tous les réseaux ---
    for name, label, max_attempts, blocking, depends_on in TARGETS:
        action = actions.get(name)
        if action is None:
            continue
        if depends_on and not state.done.get(depends_on):
            continue
        if not state.should_try(name, max_attempts):
            continue

        ok = False
        try:
            if DRY_RUN:
                print(f"[{pub_id}] [DRY] {label} ({media_type}) — publication simulée")
                ok = True
            else:
                # Chaque action DOIT renvoyer un booléen franc. `bool()` et non
                # « tout sauf False » : cette seconde forme échouait du mauvais
                # côté — un helper renvoyant un tuple (publish_carousel_facebook)
                # ou oubliant son `return` était compté comme publié, donc jamais
                # réessayé. Ici un retour douteux vaut échec, donc reprise.
                ok = bool(action())
        except Exception as e:
            ok = False
            (_fail if blocking else _warn)(f"{folder}: {label} {pub_id} -> {e}")
        else:
            if ok and not DRY_RUN:
                print(f"[{pub_id}] [OK] {label} publié ({media_type})")
            elif not ok:
                (_fail if blocking else _warn)(f"{folder}: {label} {pub_id} -> échec signalé")
        state.record(name, ok)
        # Persistance IMMÉDIATE : un crash après cette ligne ne republiera pas
        # ce qui vient de partir (cf. _write_payload_state).
        _write_payload_state(payload_file, payload, state)

    # --- Bilan : les cibles BLOQUANTES sont-elles toutes réglées ? ---
    # Les cibles best effort (Stories) sont exclues : elles ont droit à un essai
    # et ne doivent jamais retenir le payload ni ses assets Release.
    pending = [
        name for name, _lbl, max_attempts, blocking, _dep in TARGETS
        if blocking and name in actions and not state.settled(name, max_attempts)
    ]

    if pending:
        # Le payload RESTE en place — son état est déjà sur disque, écrit après
        # chaque cible. Les assets Release survivent aussi : ils sont encore
        # nécessaires aux cibles non abouties.
        print(f"[{pub_id}] [WAIT] Reprise au prochain run : {', '.join(pending)}")
        continue

    # --- Nettoyage (toutes les cibles bloquantes sont réglées) ---
    # Une seule garde couvre tout ce qui suit (published.json, suppression du
    # payload, des assets Release et des fichiers locaux) : le nettoyage est
    # irréversible, c'est exactement ce qu'une simulation ne doit pas faire.
    if DRY_RUN:
        print(f"[{pub_id}] [DRY] Toutes les cibles réglées — nettoyage simulé")
        continue

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
generate_dashboard(payload_dir, len(published), run_errors=errors, run_warnings=warn_msgs)

# 2. Un seul Commit & Push pour tout le run
if DRY_RUN:
    print("[DRY] Commit et push ignorés (simulation).")
else:
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

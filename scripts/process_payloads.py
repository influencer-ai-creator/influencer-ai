import json
import pathlib
import requests
import os
import time
import subprocess
from datetime import datetime, timezone, timedelta

payload_dir = pathlib.Path(__file__).parent.parent / "instagram_payloads"
payload_dir.mkdir(exist_ok=True)
published_file = pathlib.Path(__file__).parent / "published.json"

# Charger l'état des posts déjà publiés
if published_file.exists():
    with open(published_file) as f:
        published = set(json.load(f))
else:
    published = set()

now = int(time.time())

for payload_file in payload_dir.glob("*.json"):
    print(f"Traitement de {payload_file}")
    with open(payload_file) as f:
        payload = json.load(f)

    folder = payload["compte"]
    pub_id = payload["pub_id"]
    image_url = payload["image_url"]
    caption = payload["caption"]
    next_time = int(payload["next_time"])

    # Récupérer le secret depuis l'environnement
    secret_name = f"{folder.upper()}_ACCESS_TOKEN"
    access_token = os.environ.get(secret_name)
    if not access_token:
        print(f"❌ Secret {secret_name} introuvable, post {pub_id} ignoré")
        continue

     # --- Récupérer les IDs depuis les variables GitHub ---
    instagram_id = os.environ.get(f"{folder}_INSTAGRAM_ID")
    facebook_id = os.environ.get(f"{folder}_FACEBOOK_ID")

    if pub_id in published:
        continue  # déjà publié

    # Vérifier si c'est le moment de publier
    if next_time > now:
        # Fuseau suisse : UTC+1 en hiver, UTC+2 en été
        # Pour simplifier, ici on utilise UTC+2 (CEST) ; pour gérer automatiquement l'heure d'été, utiliser pytz ou zoneinfo
        swiss_time = datetime.fromtimestamp(next_time, tz=timezone.utc) + timedelta(hours=2)

        print(f"[{pub_id}] ⏳ Pas encore l'heure (prochaine publication à {swiss_time.strftime('%Y-%m-%d %H:%M:%S')})")
        continue

    # --- Publier sur Instagram ---
    if not instagram_id:
        print(f"❌ Aucun instagram_id trouvé pour {folder}, post {pub_id} ignoré")
        continue
    else:
        media_url = f"https://graph.facebook.com/v23.0/{instagram_id}/media"
        media_params = {
            "image_url": image_url,
            "caption": caption,
            "access_token": access_token
        }
        try:
            r = requests.post(media_url, data=media_params)
            r.raise_for_status()
            media_id = r.json()["id"]
            print(f"[{pub_id}] ✅ Conteneur média Instagram créé : {media_id}")
        except requests.exceptions.RequestException as e:
            print(f"[{pub_id}] ⚠️ Erreur création média Instagram : {e}")
            continue

        publish_url = f"https://graph.facebook.com/v23.0/{instagram_id}/media_publish"
        publish_params = {"creation_id": media_id, "access_token": access_token}

        for attempt in range(1, 4):
            try:
                publish_r = requests.post(publish_url, data=publish_params)
                publish_r.raise_for_status()
                print(f"[{pub_id}] ✅ Publication Instagram envoyée : {publish_r.json()}")
                break
            except requests.exceptions.RequestException as e:
                print(f"[{pub_id}] ⚠️ Erreur publication Instagram ({attempt}/3) : {e}")
                time.sleep(5)
        else:
            print(f"[{pub_id}] ❌ Échec Instagram après 3 essais")
            continue

    # --- Publier sur Facebook ---
    if facebook_id:
        fb_url = f"https://graph.facebook.com/v23.0/{facebook_id}/photos"
        fb_params = {
            "url": image_url,
            "caption": caption,
            "access_token": access_token
        }
        for attempt in range(1, 4):
            try:
                fb_r = requests.post(fb_url, data=fb_params)
                fb_r.raise_for_status()
                print(f"[{pub_id}] ✅ Publication Facebook envoyée : {fb_r.json()}")
                break
            except requests.exceptions.RequestException as e:
                print(f"[{pub_id}] ⚠️ Erreur publication Facebook ({attempt}/3) : {e}")
                time.sleep(5)
        else:
            print(f"[{pub_id}] ❌ Échec Facebook après 3 essais")

    # --- Mettre à jour published.json ---
    published.add(pub_id)
    with open(published_file, "w") as f:
        json.dump(sorted(list(published)), f, indent=2)

        # --- Nettoyage : supprimer le payload et l'image ---
    try:
        # Supprimer le payload JSON
        payload_file.unlink()
        print(f"🗑️ Payload supprimé : {payload_file}")
    except Exception as e:
        print(f"⚠️ Impossible de supprimer le payload {payload_file} : {e}")

    try:
        # Supprimer l'image dans <compte>/to_publish/image_XXX
        compte_dir = pathlib.Path(__file__).parent.parent / folder.lower() / "to_publish"
        image_name = pathlib.Path(payload["image_url"]).name  # ex: image_239.png
        image_file = compte_dir / image_name
        if image_file.exists():
            image_file.unlink()
            print(f"🗑️ Image supprimée : {image_file}")
    except Exception as e:
        print(f"⚠️ Impossible de supprimer l'image {image_file} : {e}")

    # --- Commit & push GitHub (pull avant push pour éviter conflit) ---
    repo_path = pathlib.Path(__file__).parent.parent
    try:
        subprocess.run(["git", "config", "user.name", "github-actions"], cwd=repo_path, check=True)
        subprocess.run(["git", "config", "user.email", "actions@github.com"], cwd=repo_path, check=True)
        subprocess.run(["git", "add", "-A"], cwd=repo_path, check=True)
        subprocess.run(["git", "commit", "-m", f"update published.json after {pub_id}"], cwd=repo_path, check=False)
        subprocess.run(["git", "pull", "--no-rebase"], cwd=repo_path, check=True)
        subprocess.run(["git", "push"], cwd=repo_path, check=True)
        print(f"✅ Commit & push effectués pour {pub_id}")
    except subprocess.CalledProcessError as e:
        print(f"⚠️ Erreur lors du commit/push : {e}")



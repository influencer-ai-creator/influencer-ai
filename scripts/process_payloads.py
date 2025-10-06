import json
import pathlib
import requests
import os
import time
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
    instagram_id = payload["instagram_id"]
    next_time = int(payload["next_time"])

    if pub_id in published:
        continue  # déjà publié

    # Récupérer le secret depuis l'environnement
    secret_name = f"{folder.upper()}_ACCESS_TOKEN"
    access_token = os.environ.get(secret_name)
    if not access_token:
        print(f"❌ Secret {secret_name} introuvable, post {pub_id} ignoré")
        continue

    # Vérifier si c'est le moment de publier
    if next_time > now:
        # Fuseau suisse : UTC+1 en hiver, UTC+2 en été
        # Pour simplifier, ici on utilise UTC+2 (CEST) ; pour gérer automatiquement l'heure d'été, utiliser pytz ou zoneinfo
        swiss_time = datetime.fromtimestamp(next_time, tz=timezone.utc) + timedelta(hours=2)

        print(f"[{pub_id}] ⏳ Pas encore l'heure (prochaine publication à {swiss_time.strftime('%Y-%m-%d %H:%M:%S')})")
        continue

    # Créer conteneur média
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
        print(f"[{pub_id}] ✅ Conteneur média créé : {media_id}")
    except requests.exceptions.RequestException as e:
        print(f"[{pub_id}] ⚠️ Erreur création média : {e}")
        continue

    # Publier avec 3 retries
    publish_url = f"https://graph.facebook.com/v23.0/{instagram_id}/media_publish"
    publish_params = {"creation_id": media_id, "access_token": access_token}

    for attempt in range(1, 4):
        try:
            publish_r = requests.post(publish_url, data=publish_params)
            publish_r.raise_for_status()
            print(f"[{pub_id}] ✅ Publication envoyée : {publish_r.json()}")
            published.add(pub_id)
            with open(published_file, "w") as f:
                json.dump(sorted(list(published)), f, indent=2)
            # Commit & push l'état mis à jour
            subprocess.run(["git", "config", "user.name", "github-actions"], check=True)
            subprocess.run(["git", "config", "user.email", "actions@github.com"], check=True)
            subprocess.run(["git", "add", str(published_file)], check=True)

            # On ne veut pas échouer si aucun changement (ex: double exécution)
            subprocess.run(
                ["git", "commit", "-m", f"update published.json after {pub_id}"],
                check=False
            )
            subprocess.run(["git", "push"], check=True)
            break
        except requests.exceptions.RequestException as e:
            print(f"[{pub_id}] ⚠️ Erreur publication ({attempt}/3) : {e}")
            time.sleep(5)
    else:
        print(f"[{pub_id}] ❌ Échec après 3 essais")

# Sauvegarder l'état des posts publiés
with open(published_file, "w") as f:
    json.dump(list(published), f)



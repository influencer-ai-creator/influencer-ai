import json
import pathlib
import requests
import os
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

def generate_dashboard(payload_dir, published_count):
    """Génère un fichier README.md à la racine avec les posts restants."""
    payloads_restants = []
    
    # Scanner les publications encore en attente (triées par date)
    for p_file in sorted(payload_dir.glob("*.json")):
        try:
            with open(p_file) as f:
                data = json.load(f)
                # Heure suisse (UTC+2 pour l'été 2026)
                dt = datetime.fromtimestamp(int(data["next_time"]), tz=timezone.utc) + timedelta(hours=2)
                payloads_restants.append({
                    "compte": data["compte"],
                    "date": dt.strftime('%Y-%m-%d %H:%M'),
                    "id": data["pub_id"],
                    "image": data.get("image_url", "")
                })
        except Exception as e:
            print(f"⚠️ Erreur lecture dashboard pour {p_file}: {e}")

    # Construction du contenu Markdown
    md_content = f"# 📊 Dashboard de Publication\n\n"
    md_content += f"Dernière mise à jour : **{datetime.now().strftime('%d/%m/%Y %H:%M:%S')}** (Heure Suisse)\n\n"
    md_content += f"✅ **Posts déjà publiés au total :** {published_count}\n\n"
    
    if not payloads_restants:
        md_content += "### 🎉 Toutes les publications sont terminées !\n"
        md_content += "La file d'attente est vide.\n"
    else:
        md_content += "### ⏳ File d'attente ({})\n".format(len(payloads_restants))
        md_content += "| Compte | Date de Publication | ID | Aperçu |\n"
        md_content += "| :--- | :--- | :--- | :--- |\n"
        
        for p in payloads_restants:
            thumb = f"<img src='{p['image']}' width='60'>" if p['image'] else "N/A"
            md_content += f"| **{p['compte'].upper()}** | {p['date']} | `{p['id']}` | {thumb} |\n"

    # Écriture du README à la racine du repo
    readme_path = base_dir / "README.md"
    with open(readme_path, "w", encoding="utf-8") as f:
        f.write(md_content)
    print("📝 Dashboard README mis à jour localement.")

# ==========================================
# BOUCLE DE PUBLICATION
# ==========================================
for payload_file in payload_dir.glob("*.json"):
    print(f"\n--- Traitement de {payload_file.name} ---")
    with open(payload_file) as f:
        payload = json.load(f)

    folder = payload["compte"]
    pub_id = payload["pub_id"]
    image_url = payload["image_url"]
    caption = payload["caption"]
    next_time = int(payload["next_time"])

    # Vérification si déjà publié
    if pub_id in published:
        print(f"[{pub_id}] Déjà dans published.json, on passe.")
        continue

    # Vérifier si c'est le moment de publier
    if next_time > now:
        swiss_time = datetime.fromtimestamp(next_time, tz=timezone.utc) + timedelta(hours=2)
        print(f"[{pub_id}] ⏳ Programmation future : {swiss_time.strftime('%Y-%m-%d %H:%M:%S')}")
        continue

    # Secrets
    folder_upper = folder.upper()
    access_token = os.environ.get(f"{folder_upper}_ACCESS_TOKEN")
    instagram_id = os.environ.get(f"{folder_upper}_INSTAGRAM_ID")
    facebook_id = os.environ.get(f"{folder_upper}_FACEBOOK_ID")

    if not access_token or not instagram_id:
        err = f"{folder}: Secrets manquants (TOKEN ou INSTA_ID)"
        print(f"❌ {err}")
        errors.append(err)
        continue

    # --- Publication Instagram ---
    success_insta = False
    try:
        media_url = f"https://graph.facebook.com/v23.0/{instagram_id}/media"
        media_params = {"image_url": image_url, "caption": caption, "access_token": access_token}
        
        r = requests.post(media_url, data=media_params)
        r.raise_for_status()
        media_id = r.json()["id"]

        publish_url = f"https://graph.facebook.com/v23.0/{instagram_id}/media_publish"
        publish_params = {"creation_id": media_id, "access_token": access_token}
        
        time.sleep(2) # Sécurité traitement image
        rp = requests.post(publish_url, data=publish_params)
        rp.raise_for_status()
        print(f"[{pub_id}] ✅ Post Instagram publié")
        success_insta = True
    except Exception as e:
        err = f"{folder}: Erreur Instagram {pub_id} -> {e}"
        errors.append(err)
        print(f"❌ {err}")

    # --- Publication Story (Optionnel) ---
    if success_insta:
        try:
            story_url = f"https://graph.facebook.com/v23.0/{instagram_id}/media"
            story_params = {"image_url": image_url, "media_type": "STORIES", "access_token": access_token}
            rs = requests.post(story_url, data=story_params)
            rs.raise_for_status()
            sm_id = rs.json()["id"]
            time.sleep(5)
            requests.post(f"https://graph.facebook.com/v23.0/{instagram_id}/media_publish", 
                          data={"creation_id": sm_id, "access_token": access_token})
            print(f"[{pub_id}] ✅ Story publiée")
        except:
            print(f"[{pub_id}] ⚠️ Story échouée (ignoré)")

    # --- Publication Facebook ---
    if success_insta and facebook_id:
        try:
            fb_url = f"https://graph.facebook.com/v23.0/{facebook_id}/photos"
            requests.post(fb_url, data={"url": image_url, "caption": caption, "access_token": access_token}).raise_for_status()
            print(f"[{pub_id}] ✅ Post Facebook publié")
        except Exception as e:
            print(f"[{pub_id}] ⚠️ Erreur Facebook: {e}")

    # --- Nettoyage si succès ---
    if success_insta:
        published.add(pub_id)
        with open(published_file, "w") as f:
            json.dump(sorted(list(published)), f, indent=2)
        
        # Supprimer payload
        payload_file.unlink()
        
        # Supprimer image locale
        try:
            image_name = pathlib.Path(image_url).name
            image_local = base_dir / folder.lower() / "to_publish" / image_name
            if image_local.exists():
                image_local.unlink()
                print(f"🗑️ Image locale supprimée")
        except: pass

# ==========================================
# FINALISATION : DASHBOARD ET GIT
# ==========================================

# 1. Générer le Dashboard avec les fichiers RESTANTS
generate_dashboard(payload_dir, len(published))

# 2. Un seul Commit & Push pour tout le run
try:
    subprocess.run(["git", "config", "user.name", "github-actions"], cwd=base_dir, check=True)
    subprocess.run(["git", "config", "user.email", "actions@github.com"], cwd=base_dir, check=True)
    subprocess.run(["git", "add", "-A"], cwd=base_dir, check=True)
    # Check si changements avant de commit
    status = subprocess.run(["git", "status", "--porcelain"], cwd=base_dir, capture_output=True, text=True)
    if status.stdout:
        subprocess.run(["git", "commit", "-m", "🤖 Update dashboard & published status"], cwd=base_dir, check=True)
        subprocess.run(["git", "pull", "--no-rebase"], cwd=base_dir, check=True)
        subprocess.run(["git", "push"], cwd=base_dir, check=True)
        print("🚀 GitHub mis à jour avec succès.")
    else:
        print("∅ Aucun changement à commit.")
except Exception as e:
    print(f"⚠️ Erreur Git : {e}")

# --- Rapport final ---
if errors:
    print("\n=== RÉSUMÉ DES ERREURS ===")
    for e in errors: print(f" - {e}")
    # Optionnel : lever une erreur pour marquer le job comme 'failed' sur GitHub
    # raise SystemExit(1)
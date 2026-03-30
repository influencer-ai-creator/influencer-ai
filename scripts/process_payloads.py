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
    """Génère un dashboard résumé par compte utilisateur."""
    stats_comptes = {}
    
    # Parcourir tous les fichiers pour agréger par compte
    for p_file in payload_dir.glob("*.json"):
        try:
            with open(p_file) as f:
                data = json.load(f)
                compte = data["compte"].upper()
                ts = int(data["next_time"])
                
                if compte not in stats_comptes:
                    stats_comptes[compte] = {
                        "count": 0,
                        "first": ts,
                        "last": ts,
                        "thumb": data.get("image_url", "")
                    }
                
                stats_comptes[compte]["count"] += 1
                if ts < stats_comptes[compte]["first"]:
                    stats_comptes[compte]["first"] = ts
                    stats_comptes[compte]["thumb"] = data.get("image_url", "") # Image du prochain post
                if ts > stats_comptes[compte]["last"]:
                    stats_comptes[compte]["last"] = ts
        except:
            continue

    # Construction du Markdown
    md_content = f"# 📊 Dashboard de Publication\n\n"
    md_content += f"Dernière mise à jour : **{datetime.now().strftime('%d/%m/%Y %H:%M:%S')}**\n\n"
    md_content += f"✅ **Total publiés historiquement :** {published_count}\n\n"
    
    if not stats_comptes:
        md_content += "### 🎉 Toutes les files d'attente sont vides !\n"
    else:
        md_content += "### 📱 État des comptes\n"
        md_content += "| Compte | Posts en attente | Prochaine publication | Fin de programmation | Aperçu prochain |\n"
        md_content += "| :--- | :---: | :--- | :--- | :---: |\n"
        
        # Trier par nom de compte
        for compte in sorted(stats_comptes.keys()):
            s = stats_comptes[compte]
            # Conversion des dates
            date_next = (datetime.fromtimestamp(s["first"], tz=timezone.utc) + timedelta(hours=2)).strftime('%d/%m %H:%M')
            date_last = (datetime.fromtimestamp(s["last"], tz=timezone.utc) + timedelta(hours=2)).strftime('%d/%m %H:%M')
            
            # Alerte visuelle si peu de posts restants
            count_display = f"**{s['count']}**" if s['count'] > 5 else f"⚠️ **{s['count']}**"
            
            thumb = f"<img src='{s['thumb']}' width='50'>" if s['thumb'] else "N/A"
            
            md_content += f"| {compte} | {count_display} | {date_next} | {date_last} | {thumb} |\n"

    # Écriture
    readme_path = pathlib.Path(__file__).parent.parent / "README.md"
    with open(readme_path, "w", encoding="utf-8") as f:
        f.write(md_content)
    print("📝 Dashboard résumé mis à jour.")

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
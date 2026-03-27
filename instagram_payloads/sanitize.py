import os
import json

def process_json_files():
    # Liste tous les fichiers .json du dossier courant
    files = [f for f in os.listdir('.') if f.endswith('.json')]
    
    if not files:
        print("Aucun fichier JSON trouvé.")
        return

    for filename in files:
        try:
            with open(filename, 'r', encoding='utf-8') as f:
                data = json.load(f)

            modified = False

            # 1. Traitement de la clé "compte"
            if "compte" in data and isinstance(data["compte"], str):
                if not data["compte"].islower():
                    print(f"[{filename}] Compte: {data['compte']} -> {data['compte'].lower()}")
                    data["compte"] = data["compte"].lower()
                    modified = True

            # 2. Traitement de la clé "image_url" (Tout en minuscules si majuscule détectée)
            if "image_url" in data and isinstance(data["image_url"], str):
                # .islower() renvoie False s'il y a au moins une majuscule
                if not data["image_url"].islower():
                    print(f"[{filename}] URL: Conversion totale en minuscules.")
                    data["image_url"] = data["image_url"].lower()
                    modified = True
            
            # Sauvegarde si au moins une des deux clés a été modifiée
            if modified:
                with open(filename, 'w', encoding='utf-8') as f:
                    json.dump(data, f, indent=4, ensure_ascii=False)
            else:
                print(f"[{filename}] Déjà conforme.")

        except Exception as e:
            print(f"Erreur sur {filename}: {e}")

if __name__ == "__main__":
    process_json_files()
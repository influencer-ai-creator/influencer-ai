import os
import json
import sys

def ajouter_media_type_json(dossier_source):
    # Extension du chemin pour gérer les caractères spéciaux (comme les espaces)
    dossier_abs = os.path.abspath(dossier_source)

    if not os.path.exists(dossier_abs):
        print(f"❌ Erreur : Le dossier '{dossier_abs}' est introuvable.")
        return

    fichiers = [f for f in os.listdir(dossier_abs) if f.endswith(".json")]
    
    if not fichiers:
        print("ℹ️ Aucun fichier .json trouvé dans ce dossier.")
        return

    for nom_fichier in fichiers:
        chemin_complet = os.path.join(dossier_abs, nom_fichier)
        
        try:
            with open(chemin_complet, 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            # Injection de la clé
            if isinstance(data, dict):
                data["media_type"] = "IMAGE"
            elif isinstance(data, list):
                for item in data:
                    if isinstance(item, dict):
                        item["media_type"] = "IMAGE"
            
            with open(chemin_complet, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=4, ensure_ascii=False)
            
            print(f"✅ Mis à jour : {nom_fichier}")
            
        except Exception as e:
            print(f"⚠️ Erreur sur {nom_fichier} : {e}")

if __name__ == "__main__":
    # Permet de passer le dossier en argument : python3 script.py /mon/dossier
    if len(sys.argv) > 1:
        ajouter_media_type_json(sys.argv[1])
    else:
        print("Usage: python3 script.py <chemin_du_dossier>")
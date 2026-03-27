import os
import json

def process_json_files():
    # Liste tous les fichiers du répertoire actuel
    files = [f for f in os.listdir('.') if f.endswith('.json')]
    
    if not files:
        print("Aucun fichier JSON trouvé.")
        return

    for filename in files:
        try:
            # Lecture du fichier
            with open(filename, 'r', encoding='utf-8') as f:
                data = json.load(f)

            # Modification de la valeur si la clé existe
            if "compte" in data and isinstance(data["compte"], str):
                original_value = data["compte"]
                new_value = original_value.lower()
                
                if original_value != new_value:
                    data["compte"] = new_value
                    
                    # Réécriture du fichier avec les modifications
                    with open(filename, 'w', encoding='utf-8') as f:
                        json.dump(data, f, indent=4, ensure_ascii=False)
                    print(f"Modifié : {filename} ({original_value} -> {new_value})")
                else:
                    print(f"Ignoré : {filename} (déjà en minuscules)")
            else:
                print(f"Passé : {filename} (clé 'compte' absente)")

        except Exception as e:
            print(f"Erreur lors du traitement de {filename}: {e}")

if __name__ == "__main__":
    process_json_files()
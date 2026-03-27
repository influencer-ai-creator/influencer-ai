import json
import pathlib
from playwright.sync_api import sync_playwright
import requests
import os

# --- Configuration ---
base_dir = pathlib.Path(__file__).parent.parent
payload_dir = base_dir / "instagram_payloads"
test_output_dir = base_dir / "test_stories"
test_output_dir.mkdir(exist_ok=True)

# 1. Le Template HTML/CSS (Pixel Perfect)
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="fr">
<head>
    <meta charset="UTF-8">
    <title>Story Template</title>
    <script src="https://cdn.jsdelivr.net/npm/@twemoji/api@latest/dist/twemoji.min.js" crossorigin="anonymous"></script>
    <style>
        body {{
            margin: 0;
            padding: 0;
            width: 1080px;
            height: 1920px;
            /* Fond avec dégradé subtil */
            background: radial-gradient(circle, #1a1a1a 0%, #0a0a0a 100%);
            color: white;
            display: flex;
            flex-direction: column;
            align-items: center;
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
            overflow: hidden;
        }}

        .image-container {{
            width: 1080px;
            display: flex;
            justify-content: center;
            /* [FIX] Coller l'image en haut (align-items: flex-start) */
            align-items: flex-start;
            margin-bottom: 0; /* Pas de marge en bas de l'image */
        }}

        .image-container img {{
            width: 1080px;
            height: auto;
            object-fit: cover;
        }}

        .caption-container {{
            flex-grow: 1; /* Prend tout l'espace restant */
            width: 960px; /* Marges de 60px de chaque côté */
            
            /* [MODIF] Positionnement du texte : centré verticalement sous l'image */
            display: flex;
            justify-content: center;
            align-items: center; 
            text-align: center;
            
            margin-top: 40px; /* Espace après l'image */
            margin-bottom: 150px; /* Marge de sécurité verticale : évite la barre de réponse Instagram */
        }}

        .caption-text {{
            font-size: 50px; /* [MODIF] Réduction de taille pour la sécurité */
            line-height: 1.3; /* [MODIF] Espacement compact pour gagner de la place */
            font-weight: 600;
            white-space: pre-wrap;
            word-wrap: break-word;
        }}

        /* Style indispensable pour Twemoji */
        img.emoji {{
            height: 1em;
            width: 1em;
            margin: 0 .05em 0 .1em;
            vertical-align: -0.1em;
        }}
    </style>
</head>
<body>
    <div class="image-container">
        <img src="{image_url}" alt="Post Image">
    </div>
    <div class="caption-container">
        <div id="caption" class="caption-text">{caption}</div>
    </div>
    
    <script>
        window.onload = function() {{
            var captionEl = document.getElementById('caption');
            twemoji.parse(captionEl, {{
                folder: 'svg', 
                ext: '.svg'
            }});
        }};
    </script>
</body>
</html>
"""

def generate_story_with_html(image_url, caption, output_path):
    print(f"--- 🛠️ Génération (Pixel Perfect) : {output_path.name} ---")

    # Protection basique contre le HTML
    # 1. On standardise les retours à la ligne (Windows \r\n vers Linux \n)
    normalized_caption = caption.replace('\r\n', '\n')
    
    # 2. On protège le HTML et on force les balises <br>
    safe_caption = normalized_caption.replace('<', '&lt;').replace('>', '&gt;').replace('\n', '<br>')

    filled_html = HTML_TEMPLATE.format(
        image_url=image_url,
        caption=safe_caption
    )

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(viewport={'width': 1080, 'height': 1920})
            
            page.set_content(filled_html)
            
            # networkidle est crucial pour attendre le chargement de Twemoji
            page.wait_for_load_state("networkidle")
            
            page.screenshot(path=str(output_path), type="jpeg", quality=95)
            
            browser.close()
        
        print(f"✅ Succès ! Story générée : {output_path.name}")
    
    except Exception as e:
        print(f"❌ Erreur lors de la génération HTML : {e}")

# --- Run ---
payloads = list(payload_dir.glob("*.json"))
if payloads:
    for payload_file in payloads:
        with open(payload_file, 'r') as f:
            data = json.load(f)
        out_name = f"story_{data.get('pub_id', 'unknown')}.jpg"
        generate_story_with_html(data["image_url"], data["caption"], test_output_dir / out_name)

print(f"\n🚀 Test terminé. Vérifie les images dans : {test_output_dir}")
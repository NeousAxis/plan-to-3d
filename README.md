# plan-to-3d

Transformer un **plan 2D** (image, PDF, SVG) en **modèle 3D interactif** — sans
SketchUp, sans abonnement, 100 % logiciels libres.

Sorties pour chaque plan :
- `model.glb` — modèle 3D au standard glTF 2.0 (importable dans Blender,
  FreeCAD, SketchUp Free, n'importe quelle visionneuse glTF en ligne, ou sur
  téléphone)
- `viewer.html` — visionneuse autonome (rotation / zoom / déplacement +
  étiquettes des pièces). Le GLB est embarqué en base64 : **un double-clic
  suffit**, aucune installation, aucune connexion.
- `preview.png` — aperçu isométrique (option `--preview`)

## Comment ça marche

```
fichier plan → ingest.py → PNG → [lecture du plan] → building.json
             → generate.py → model.glb + viewer.html + preview.png
```

- `generate.py` : **aucune dépendance** (bibliothèque standard Python seulement)
- `ingest.py` (formats non-PNG) + aperçu PNG : quelques paquets pip courants

## Installation

```bash
python3 -m pip install -r requirements.txt   # Pillow, pymupdf, matplotlib
```

## Utilisation

Le flux complet est piloté par le skill (voir `SKILL.md`) : on donne un plan,
l'assistant le lit, écrit le `building.json`, puis génère la 3D. Manuellement :

```bash
# 1. Normaliser le fichier d'entrée en image(s)
python3 ingest.py mon_plan.pdf --out work/ingested

# 2. (l'assistant lit l'image et écrit building.json selon schema.json)

# 3. Générer la 3D
python3 generate.py building.json --out work/output --preview
```

## Formats d'entrée supportés

| Type | Extensions | Traitement |
|---|---|---|
| Images | png, jpg, jpeg, webp, gif, bmp, tif, tiff | Pillow (orientation EXIF corrigée) |
| PDF | pdf | PyMuPDF (1 PNG par page) |
| SVG | svg | cairosvg (si installé) |
| CAO vectorielle | dxf, dwg | exporter d'abord en PDF/PNG |

## Format du `building.json`

Voir `schema.json` et l'exemple `examples/demo_house.json`. En bref : des murs
(segments avec épaisseur/hauteur), des ouvertures (portes/fenêtres référençant
l'index d'un mur), une dalle, une toiture (plate/à deux pans/aucune) et des
étiquettes de pièces.

## Exemples fournis

- `examples/demo_house.json` — petite maison à toit à deux pans (test du moteur)
- `examples/real_plan_1.json` — interprétation d'un vrai plan d'appartement
  (`examples/real_plan_1.jpg`)

## Limites

Le résultat est une **maquette volumétrique fidèle**, pas un relevé CAO exact.
La précision dépend des cotes inscrites sur le plan. Le `building.json` reste
modifiable : corriger un mur et régénérer prend quelques secondes.

## Installer comme skill Claude Code

Copier ce dossier dans `~/.claude/skills/plan-to-3d/`. Le `SKILL.md` (avec son
entête `name` / `description`) sera alors détecté automatiquement.

# CLAUDE.md — plan-to-image session handoff

> **Lu automatiquement par Claude au démarrage de toute session dans ce repo.**
> Mis à jour 2026-05-31. État après le pivot complet 3D → image générative.

## Ce qu'est ce projet

**Skill `plan-to-image`** : convertit un plan 2D d'architecture en **renders intérieurs photoréalistes**, un PNG par pièce, en composant des prompts pro (style, matériaux, lighting, mobilier, palette, vue caméra) et en les envoyant à un modèle FLUX gratuit.

Pivot fait après 4 jours de tentatives sur une version géométrique 3D (Three.js + Cycles) qui plafonnait en qualité visuelle. Le **legacy 3D** est conservé sous `legacy_3d/`.

## Architecture actuelle

```
plan.png ─► plan_analyzer.py ─► spec.json
                   │ (CF Mistral 24B vision, free)
                   │  extrait rooms + dimensions + layout + mobilier
                   ▼
       [USER édite spec.json si besoin]
                   │
                   ▼
        plan_to_image.py ─► 8 PNG + gallery.html
                   │  Pollinations FLUX primary + CF FLUX schnell fallback
                   │  Quota guard CF (hard block à 40/jour, JAMAIS de paid)
```

## Fichiers clés

| Fichier | Rôle |
|---|---|
| `plan_to_image.py` | Génère les images. CLI : `spec.json → PNG/gallery` |
| `plan_analyzer.py` | Analyse un plan via Mistral Vision et écrit le spec auto |
| `vocabulary.json` | Catalogue pro (styles, matériaux, mobilier, lighting, marques F&B/Vitra/Wegner/etc.) |
| `examples/apartment_2br_image.json` | Spec de référence transcrit du plan apt 2BR de l'user |
| `SKILL.md` | Doc de la skill, à jour avec le pivot |
| `ROADMAP.md` | Roadmap historique (3D → image) |
| `legacy_3d/` | Tout le code 3D Three.js + Cycles conservé pour référence |
| `docs/samples/` | 9 renders de référence (8 pièces apt + 1 CF) |

## Providers d'images (gratuits, ne jamais switcher en payant)

| Provider | Modèle | Auth | Quota | Qualité |
|---|---|---|---|---|
| **Pollinations.ai** | FLUX (≈ dev) | anonyme | non documenté, en pratique illimité | Magazine AD |
| **Cloudflare Workers AI** | `@cf/black-forest-labs/flux-1-schnell` | `wrangler login` (OAuth auto-refresh) | **HARD-BLOCK à 40/jour** | Bon |
| **CF Vision** | `@cf/mistralai/mistral-small-3.1-24b-instruct` | idem | même quota | Excellent pour analyser plans |

**Account ID Cloudflare** : `b2068a4d683aef98e2edcf4662eb0699`
**Token** : lu auto depuis `~/.wrangler/config/default.toml`. Si expiré → `wrangler login`.

### Quota guard
Compteur local `~/.cache/plan_to_image/cf_quota.json`. Warning à 75%, HARD BLOCK à 100%. Override possible avec `P2I_BYPASS_QUOTA=1` (uniquement pour debug — ne JAMAIS le mettre par défaut, l'user a explicitement INTERDIT de basculer en payant).

## Conventions du spec JSON

```jsonc
{
  "project": "<nom du projet>",
  "global": {
    "style":      "japandi",          // clé de vocabulary.styles, ou texte libre
    "atmosphere": "warm",
    "tech":       "architectural photography, AD-style, ..."
  },
  "rooms": [
    {
      "name":       "Séjour",
      // === Géométrie (lue sur le plan, en TÊTE du prompt FLUX) ===
      "dimensions": "rectangular, 6.50 × 4.30 m, 27.95 m², 2.6 m ceiling",
      "layout":     "two windows on north wall, door on west wall",
      "placement":  "L-shaped sofa centred facing north windows, ...",
      "viewpoint":  "view from the SW corner looking NE diagonally",
      // === Design (vocabulaire pro) ===
      "floor":      "smoked oak chevron parquet",
      "walls":      "lime plaster, hand-troweled, warm white",
      "ceiling":    "matte white plaster, recessed downlights",
      "lighting":   "Noguchi Akari pendant, golden hour sun",
      "furniture":  "Camaleonda sofa, Noguchi coffee table, Saarinen Tulip",
      "colors":     "F&B Cornforth White, terracotta accents",
      "extra":      "abstract canvas on south wall, dried branches"
    }
  ]
}
```

## Commandes essentielles

```bash
cd /Users/cyrilleger/planto3dsource

# 1. Analyser un plan → spec auto
python3 plan_analyzer.py ~/Downloads/plan.png --style japandi

# 2. Générer les images depuis un spec
python3 plan_to_image.py examples/apartment_2br_image.json
# → work/<project>/{room1.png, room2.png, ..., gallery.html}

# Options utiles :
#   --only "Séjour"          # 1 seule pièce
#   --seed 333               # changer la composition
#   --provider cloudflare    # forcer CF (sinon auto Pollinations puis CF fallback)
#   --width 1280 --height 720
```

## Règles apprises lors du travail (à appliquer)

1. **Géométrie en TÊTE du prompt** (le code le fait dans `build_prompt`). FLUX donne le plus de poids au début → c'est ce qui fait que le rendu respecte le plan.
2. **Max 3-4 meubles** par petite pièce sinon FLUX produit des hybrides moches (matelas qui flotte, canapé/desk fusionnés). Cf. chambre 2 fix dans commit `cda39f2`.
3. **Négations explicites** marchent : `"NO chair, NO desk, NO double bed"`.
4. **Bed-frame explicite** (`"solid oak frame visible, ~35 cm tall"`) sinon ça fait du matelas posé au sol.
5. **Viewpoint frontal** quand un objet est hero, pas diagonal (qui éparpille).
6. **Cotes précises** : reproduire à 2 décimales (5.36 × 3.72) plutôt qu'arrondir.

## Roadmap restante (ordre validé par l'user)

- ✅ **D** — 10 palettes tableau (legacy, hors scope nouveau pipeline)
- ✅ **A** — calibration bake (legacy)
- ✅ **C** — polish textures (legacy)
- ✅ **B** — lightmap embedded (legacy, partiel)
- ✅ **Pivot** vers plan-to-image
- ✅ **#1** — quota guard CF + hard block
- ✅ **#4** (en cours) — plan-to-spec auto via CF Mistral Vision. Pipeline OK, **reste à valider sur le plan réel de l'user**.
- ⬜ **#2** — vue 360° immersive (FLUX equirectangular pano + Three.js sphere viewer). Le plus gros wow, 2 j.
- ⬜ **#3** — UI HTML pour éditer le spec visuellement (dropdowns vocab, preview live).
- ⬜ **#5** — ControlNet sur le plan pour vraie contrainte géométrique (Replicate / local SD).

## Question architecture en suspens

L'user a demandé : **peut-on mixer plan-to-image avec une vue immersive 3D pour "entrer dans l'image" ?** Trois pistes documentées dans la dernière réponse :
- (A) Pano 360° + sphère Three.js — la voie naturelle, ~2 j (recommandé)
- (B) Multi-vues + transitions, plan cliquable, ~1 j
- (C) 3D auto-généré depuis image (WonderJourney, NeRF…) — expérimental, lourd

## État au moment du handoff

- Pipeline complet fonctionnel : analyzer + generator + quota + fallback.
- 8 pièces de l'apartment_2br rendues dans `docs/samples/` (style japandi).
- Chambre 2 fixée après plainte user (commit `cda39f2`).
- **Reste à faire** : tester `plan_analyzer.py` sur le vrai plan dropped par l'user (il doit le glisser dans le repo, ex. `~/Downloads/plan.png`), puis enchaîner la roadmap dans l'ordre (#4 finalisation, #2 immersif, #3 UI, #5 ControlNet).

## Préférences user (à respecter)

- **Honnêteté absolue** sur ce qui marche / ne marche pas (l'user déteste les "done" faux).
- **JAMAIS** basculer en plan payant CF sans demande explicite.
- **Verifier visuellement** chaque output avant de dire "done".
- Vocabulaire d'archi/déco pro (pas de termes amateurs).
- L'user est francophone, les noms de pièces / labels en français.

## Repo

- GitHub : `https://github.com/NeousAxis/plan-to-3d` (nom historique, contient plan-to-image)
- Local : `/Users/cyrilleger/planto3dsource`
- Dernier commit : voir `git log -5 --oneline`

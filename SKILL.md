---
name: plan-to-3d
description: >-
  Convert a 2D floor plan (raster image) into a FAITHFUL 3D model with a
  first-person virtual visit (PlanCAD web app), plus optional photoreal
  interior renders. The chain is fully free and installation-free for the
  user: openings are detected by a specialized model running in the BROWSER
  (public page), geometry is solved from the printed dimensions, an overlay
  audit against the source image is MANDATORY before showing any 3D, and
  the result opens in cad.html (2D + 3D + walkthrough). Use whenever the
  user provides a floor plan and wants a 3D model, a virtual visit, a DXF,
  or photoreal renders.
---

# plan-to-3d

Turn a 2D plan into a faithful 3D + virtual visit, then (optionally)
AD-grade interior renders. **Fidelity first: never hand-transcribe a plan,
never show a 3D that has not passed the overlay audit.**

```
plan.png ─► 1. rooms+dims  (plan_reader / printed dims solver)
         ─► 2. openings    (detections from the PUBLIC browser page or local model)
         ─► 3. AUDIT       (plan_audit overlay vs source image — MANDATORY)
         ─► 4. 3D + visite (web/cad.html : 2D+3D synchro, Visiter, ?shot=tour)
         ─► 5. (option) photoreal renders per room (FLUX, free)
```

## Zero-install path (what the user's friends use)

- **Detection online** : https://neousaxis.github.io/plan-to-3d/detect.html
  → drop the plan image, detection runs in THEIR browser (ONNX 13 MB from
  `NeousAxis/plan-openings-onnx`), button « Télécharger le JSON ».
- **Viewer online** : https://neousaxis.github.io/plan-to-3d/cad.html
  → drag-drop any `plan.json` : 2D pro + 3D dollhouse + « Visiter »
  (first-person walkthrough, ZQSD + mouse).

## Full chain (what YOU run, in order)

```bash
# 1. rooms + dimensions (dimensioned plans only; else ask the user for one cote)
python3 plan_reader.py read plan.png -o web/plans/x.json

# 2. openings from detections (detections.json = downloaded from detect.html)
python3 tools/plan_openings.py web/plans/x.json plan.png \
        --detections detections.json -o web/plans/x.json
#    (without --detections: runs the local model if work/_seg_venv exists)

# 3. MANDATORY audit — look at the overlay, fix, re-audit until it matches
python3 tools/plan_audit.py web/plans/x.json plan.png -o overlay.png

# 4. 3D + virtual visit (+ batch captures)
cd web && python3 -m http.server 8777          # http://localhost:8777/cad.html?plan=plans/x.json
python3 tools/shot_server.py &                  # then open ...&shot=tour → 1 PNG dollhouse + 1/room
```

Rules learned the hard way (sessions #3-#5): plans without printed dims →
do NOT trust guessed numbers, ask for one known dimension; the audit
overlay (rooms red, doors green, windows blue, furniture orange, entry
magenta) is the only accepted proof of fidelity; `MODEL.entry` carries the
front door; label-only HALL rooms get floor/walls/spots automatically.

## Optional step 5 — photoreal renders (plan-to-image)

Turn each room into an AD-grade image with **FLUX via Pollinations.ai**
(free, anonymous, no API key), composing prompts from `vocabulary.json`.
Never call these renders "faithful": they are dressing, the geometry truth
lives in the 3D above.

### 1. Read the plan

Look at it carefully. Identify:
- the **list of rooms** and their function (séjour, chambre, salle de bain,
  cuisine, hall, dressing…)
- any **stated style** or constraints the user gave (palette, materials,
  brand references like "Vitra", "Farrow & Ball"…)
- the user's mood — bright airy minimalism vs. moody industrial vs. warm
  bohemian, etc.

### 2. Author a spec JSON

Follow the shape in `examples/apartment_2br_image.json`:

```jsonc
{
  "project": "Apartment 2BR — Japandi",
  "global": {
    "style":      "japandi",          // key into vocabulary.styles, or free text
    "atmosphere": "warm",             // key into vocabulary.atmospheres
    "view":       "wide_corner",      // key into vocabulary.camera_views
    "tech":       "architectural photography, AD-style, ultra-photoreal, …"
  },
  "rooms": [
    {
      "name":      "Séjour",          // becomes the image file name
      "floor":     "smoked oak chevron parquet",
      "walls":     "lime plaster, hand-troweled, warm white",
      "ceiling":   "matte white plaster, recessed plaster-in downlights",
      "lighting":  "Isamu Noguchi Akari pendant over coffee table, cove LED 2700K, late golden-hour sun",
      "furniture": "low-slung bouclé sofa in ivory, Noguchi walnut coffee table, dining area with Saarinen Tulip + 4 wishbone CH24 chairs",
      "colors":    "Farrow & Ball Cornforth White, accents in terracotta brûlée",
      "extra":     "large abstract canvas on the back wall, dried branch in stone pot",
      "view":      "wide_corner"      // override of global
    }
  ]
}
```

Every field is optional except `name`. Anything you write in the field is
inserted verbatim into the FLUX prompt; you can also pass a **vocabulary
key** (e.g. `"style": "japandi"`) and the skill expands it from
`vocabulary.json`.

### 3. Generate

```
python3 plan_to_image.py spec.json [--out work/imgs]
                                   [--width 1024] [--height 768]
                                   [--model flux | flux-realism | turbo]
                                   [--seed 42]
                                   [--only "Séjour"]
```

Output: one PNG per room + a self-contained `gallery.html`. Each card in
the gallery shows the prompt that produced it, so the user can read it,
copy-edit any room, and re-run with `--only "<room>"`.

### 4. Iterate

If the user doesn't like a room, change the relevant fields in the spec
(e.g. swap `floor` from oak to travertine, or `style` from japandi to
brutalist), then:

```
python3 plan_to_image.py spec.json --only "<room name>"
```

Same `--seed` keeps the framing similar; bump the seed to re-roll.

## Style cheat-sheet (`vocabulary.json` keys)

- **Styles:** `japandi`, `scandi`, `wabi_sabi`, `biophilic`,
  `mid_century_modern`, `contemporary_minimal`, `brutalist`,
  `industrial_loft`, `art_deco`, `mediterranean`, `boho`, `luxe_modern`
- **Atmospheres:** `serene`, `warm`, `airy`, `moody`, `fresh`, `lived_in`
- **Camera views:** `wide_corner`, `intimate_35mm`, `low_hero`,
  `axial_one_point`, `human_85mm`

For materials / lighting / furniture / colours, the vocabulary file lists
~10 high-quality choices each (real product names + brands). Either pick
one verbatim or use it as inspiration for your own line.

## Notes

- **No API key.** Pollinations.ai is free and anonymous. If the service is
  ever down, swap the URL in `plan_to_image.py` `fetch_image()` for any
  OpenAI-compatible image endpoint (Together AI, CloudFlare Workers AI,
  Replicate FLUX, etc.).
- **Each image is one HTTP call**, ~10–30 s on FLUX. 8 rooms ≈ 2–4 min.
- **Honesty.** This is generative art, not a survey. The result is what a
  great interior designer + photographer might produce *given* the room
  name and your brief — it's not a geometric rendering of the exact plan.
  Wall positions, window placement, exact dimensions WON'T match the plan.
  The deliverable is "what the room could look like", not "what it
  measures". Tell the user that.

## Legacy 3D pipeline

The previous geometric `plan-to-3d` (interactive Three.js viewer +
optional Blender Cycles render) is kept under `legacy_3d/`. Use it when
the deliverable requires geometric fidelity (wall positions, doors,
furniture placement to scale) rather than photoreal beauty:

```
python3 legacy_3d/generate.py building.json --out work/viewer
python3 legacy_3d/bake.py     building.json   # optional Cycles still
```

See `legacy_3d/SKILL.md` (if present) or the project's git history for
that workflow.

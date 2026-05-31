---
name: plan-to-image
description: >-
  Convert a 2D building/floor plan into photoreal interior renders, one per
  room, by composing pro-grade archviz prompts (style, materials, lighting,
  furniture, palette, camera) and shipping them to a free image model
  (Pollinations.ai FLUX). Use whenever the user provides a floor plan and
  wants magazine-quality interior images per room — no rendering engine, no
  3D modelling. The user can edit each room's prompt parts and re-generate.
---

# plan-to-image

Turn a 2D plan into AD-grade interior renders. The pipeline is:

```
plan file  ->  [you read it]  ->  spec.json (per-room prompt parts)
           ->  plan_to_image.py  ->  one PNG per room + gallery.html
```

This skill replaced the old geometric `plan-to-3d` pipeline (kept under
`legacy_3d/` for reference — it produces an interactive 3D viewer but never
reached photoreal quality without a lot of plumbing). The new approach uses
**FLUX via Pollinations.ai** (free, anonymous, no API key) and composes
prompts from a structured **`vocabulary.json`** of architecture-and-design
terminology (styles, materials, lighting, furniture, colours, camera shots).

## When to use

The user hands over a plan (image, PDF, screenshot) and wants believable
interior renders — typical asks: "show me what this could look like",
"design this apartment in japandi style", "give me a photoreal preview".
Each room becomes one image.

## Workflow

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

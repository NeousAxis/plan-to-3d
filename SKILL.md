---
name: plan-to-3d
description: >-
  Convert a 2D building/floor plan into an interactive 3D model. Use whenever
  the user provides a floor plan (image PNG/JPG/WEBP/TIFF, PDF, or SVG) and
  wants a 3D / SketchUp-like visualization. Produces a standards-compliant GLB
  model plus a self-contained HTML viewer (orbit/zoom/pan, room labels) and an
  optional PNG preview. No paid software, no internet required to view.
---

# plan-to-3d

Turn any 2D plan into a 3D massing model. The pipeline is:

```
plan file  ->  ingest.py  ->  PNG(s)  ->  [you read them]  ->  building.json
           ->  generate.py  ->  model.glb + viewer.html + preview.png
```

`generate.py` uses the Python standard library only. `ingest.py` and the PNG
preview use a few common pip packages (see requirements.txt).

## When to use

The user hands over a plan ("voici mon plan", a path, a screenshot, a PDF) and
wants a 3D result. Each time a new plan arrives, repeat the workflow below.

## Workflow

### 0. Locate the scripts and (optionally) install deps

This skill lives in its own folder (`generate.py`, `ingest.py`, `schema.json`,
`examples/`). The core (`generate.py`) needs nothing extra. For ingestion of
non-PNG inputs and for preview rendering:

```
python3 -m pip install -r requirements.txt
```

### 1. Normalize the input to image(s)

```
python3 ingest.py "<PLAN_FILE>" --out work/ingested
```

It prints the produced PNG path(s), one per line. Handles raster images, PDF
(one PNG per page), and SVG. For `.dxf`/`.dwg` it tells the user to export to
PDF/PNG first. A plain PNG/JPG can also be read directly without ingest.

### 2. Read the plan (vision)

Open each PNG with the Read tool and extract, carefully:

- **Scale.** Look for explicit room dimensions ("4.50 x 2.08m", "12' x 8'6\""),
  a scale bar, or a total area ("120 m²"). Convert feet/inches to metres
  (1 ft = 0.3048 m, 1 in = 0.0254 m). If nothing is given, assume a typical
  ceiling height (2.6 m residential) and lay rooms out proportionally from
  pixel measurements, and TELL the user the model is unscaled/approximate.
- **Outer envelope.** Trace the exterior walls as a closed polygon.
- **Interior partitions.** Each interior wall as a straight segment.
- **Openings.** Doors (sill 0) and windows (sill ~0.9 m) with their position
  along the host wall and width.
- **Rooms.** Name + an interior anchor point for each labelled room.
- **Interior fixtures and furniture.** Look for these symbols and add an entry
  for each in `furniture[]` (see step 3). Don't skip them — interiors are what
  make the 3D readable. Estimate position (centre of the symbol) and rotation
  (degrees; 0 means the item's "width" runs along plan X). Typical symbols:
    - **Bed** — rectangle with a thin pillow band at one short side. Single
      ~0.9×2.0 m, double ~1.6×2.0, king ~1.8×2.0.
    - **Sofa** — long rectangle (2.0–2.5 m), often with cushion subdivisions
      and a thicker back along one long side. Depth ~0.9 m.
    - **Armchair** — small square ~0.9×0.9 with a rounded back arc.
    - **Dining table + chairs** — central rectangle/circle (~1.6×0.9) ringed
      by 4–8 small squares (chairs).
    - **Coffee table** — small low rectangle in front of a sofa.
    - **Desk** — rectangle ~1.4×0.7, usually against a wall with a chair.
    - **Kitchen counter / island** — long thin shape (depth ~0.6) along walls.
      Embedded circles/ovals = sink; square with 4 small circles = hob/stove;
      square ~0.6×0.6 = oven/dishwasher.
    - **Fridge** — square ~0.7×0.7 at the end of a counter (often labelled
      "REF" or with a diagonal split symbol).
    - **Wardrobe / closet** — long thin rectangle against a wall (depth ~0.6),
      often with sliding-door arrows or a diagonal indicating door swing.
    - **Bookshelf** — thin rectangle (depth ~0.3) against a wall.
    - **TV** — very thin rectangle on a wall.
    - **Toilet** — pill / keyhole shape ~0.4×0.65 against a wall.
    - **Sink (bathroom)** — small rectangle/oval ~0.6×0.45 against a wall.
    - **Bathtub** — long rounded rectangle ~1.7×0.75.
    - **Shower** — square ~0.9×0.9 with a diagonal cross or drain dot.
    - **Stairs** — a ladder of parallel lines (treads) with an arrow showing
      the up direction. Use `type: "stairs"`, `size: [tread_width, total_run]`,
      `height: total_rise`, `rotation` along the climb direction. The
      generator auto-picks ~17 cm per step unless you set `steps`.
  If a symbol is ambiguous, prefer a sensible default over skipping it —
  an empty 3D room is the worst outcome.

Set up a metre coordinate system with origin at the bottom-left of the
footprint, X to the right, Y upward in plan.

### 3. Author `building.json`

Follow `schema.json`. Minimal shape:

```json
{
  "meta": {"name": "...", "units": "m", "wall_height": 2.6, "wall_thickness": 0.2},
  "walls": [{"start": [x, y], "end": [x, y], "thickness": 0.2, "type": "exterior"}],
  "openings": [{"wall": 0, "kind": "door|window", "distance": 1.5,
                "width": 0.9, "sill": 0.0, "height": 2.1}],
  "slab": {"enabled": true, "thickness": 0.15},
  "roof": {"type": "flat|gable|none", "height": 1.5, "overhang": 0.3, "ridge_axis": "x"},
  "rooms": [{"name": "Living room", "at": [x, y]}],
  "furniture": [
    {"type": "double_bed",   "at": [6.5, 2.0], "rotation": 0},
    {"type": "sofa",         "at": [2.5, 1.2], "rotation": 0},
    {"type": "dining_table", "at": [2.5, 4.2], "rotation": 0},
    {"type": "chair",        "at": [1.7, 4.2], "rotation": 0},
    {"type": "kitchen_counter","at": [4.2, 5.6], "size": [3.4, 0.6], "rotation": 0},
    {"type": "fridge",       "at": [4.6, 5.6], "rotation": 0},
    {"type": "stove",        "at": [3.4, 5.6], "rotation": 0},
    {"type": "kitchen_sink", "at": [5.5, 5.6], "rotation": 0},
    {"type": "toilet",       "at": [7.5, 5.5], "rotation": 0},
    {"type": "bathtub",      "at": [7.0, 4.0], "rotation": 90},
    {"type": "stairs",       "at": [6.0, 3.0], "size": [1.0, 3.2],
     "height": 2.7, "rotation": 90}
  ]
}
```

Rules of thumb:
- `openings[].wall` is the index of the wall in the `walls` array.
- Use `"distance"` (metres from the wall start) OR `"position"` (0..1 fraction).
- Exterior walls ~0.2-0.30 m thick; interior ~0.08-0.12 m.
- Flat roof for an intermediate apartment floor; gable for a standalone house;
  `none` to leave the top open (dollhouse).
- One `rooms[]` entry per labelled space; `at` is any point inside the room.
- `furniture[]` is optional but strongly encouraged. Each item is a known
  type (see schema.json for the full list) plus an `at` centre point. The
  generator has built-in default size/height/colour for every type, so
  `{"type": "sofa", "at": [x, y]}` already produces a credible sofa. Override
  `size: [w, d]` and `height` when the plan shows a clearly non-standard item
  (e.g. an L-shaped sofa modelled as two boxes, or a kingsize bed). Set
  `rotation` (degrees) so the long side runs the way the symbol does on the
  plan. Pull items at least 5–10 cm away from walls — overlapping walls
  causes z-fighting in the viewer.
- Stairs use `type: "stairs"`, `size: [tread_width, total_run]`,
  `height: total_rise` (top floor level). Add `"steps": N` only if you want
  to force a specific count; otherwise the generator picks ~17 cm risers.

### 4. Generate

```
python3 generate.py building.json --out work/output --preview
```

Outputs `model.glb`, `viewer.html`, and (with `--preview`) `preview.png`.

### 5. Verify, then deliver

- Read `preview.png`. Check the footprint, room count, and that doors/windows
  landed on the right walls. The preview's roof is drawn semi-transparent and
  matplotlib has no real depth buffer, so minor face-ordering specks are normal
  — the GLB/HTML viewer renders perfectly.
- If something is off, fix `building.json` and regenerate. Iterate.
- Hand over `viewer.html` (double-click to open, fully self-contained — the GLB
  is embedded as base64) and `model.glb` (import into Blender, FreeCAD,
  SketchUp Free, or any online glTF viewer). Send the files to the user.

## Multi-floor buildings

Model each floor as its own `building.json` and generate separately, or stack
them: offset a floor's geometry by giving its walls a higher base by treating
each floor as a separate spec and combining the GLBs in the viewer. The
simplest reliable approach is one model per floor.

## Honesty

This produces a faithful **massing/interpretation**, not a surveyed CAD model.
Accuracy depends on the dimensions printed on the plan. Always keep the
`building.json` so the user can correct any wall and regenerate in seconds.

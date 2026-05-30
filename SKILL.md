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

Turn any 2D plan into a furnished, SketchUp-style 3D model. The pipeline is:

```
plan file  ->  ingest.py  ->  PNG(s)  ->  [you read them]  ->  building.json
           ->  generate.py  ->  model.glb + viewer.html + preview.png
```

`generate.py` uses the Python standard library only. `ingest.py` and the PNG
preview use a few common pip packages (see requirements.txt).

## Resolution target

The goal is a clean, **furnished dollhouse render** — walls, glass partitions,
columns, and recognisable multi-part furniture (desks with monitors, office
chairs, sofas, conference tables with chairs, kitchens, plants), lit with soft
shadows and physically-based materials. An empty shell of bare walls is NOT
acceptable: the value is in reading the *layout in use*. Whenever a plan shows
furniture, model it. When in doubt, add the item with default dimensions rather
than leaving the room empty.

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
  Office / workplace plans add these:
    - **Workstation bench** — a block of desks pushed together, usually two
      rows back-to-back, each desk with a chair. Model the whole block with one
      `type: "desk_bench"`, `size: [block_w, block_d]`, `"seats": N` (desks per
      row). The builder lays out N desks per side, each with a monitor and an
      office chair. One bench per cluster — don't place 12 separate desks.
    - **Single desk** — `office_desk` (renders desktop + monitor + keyboard) for
      a workplace; `desk` for a plain writing desk. Pair with `office_chair`.
    - **Conference / meeting table** — long table ringed by many chairs. Use
      `conference_table` or `meeting_table`; the builder auto-adds a chair row
      on each long side, scaled to the length. `round_table` does the same with
      chairs around the rim.
    - **Glass partition** — thin double line, often dashed, enclosing a meeting
      room. Model it as a WALL with `"type": "glass"` (not as furniture); it
      renders as a translucent pane between thin frame rails.
    - **Structural column** — a solid filled square or circle in the open floor,
      on a regular grid. Use `type: "column"` (`"round": true` for circular),
      `height` = full ceiling height.
    - **Plant** — a circle with a leafy/star outline. Use `plant` or
      `plant_large`.
    - **Lounge** — `sofa` / `sofa_l` + `armchair` + `coffee_table` + a `rug`.
    - **Lockers / storage / credenza / shelving** — long thin rectangles
      against walls: `cabinet`, `wardrobe`, `lockers`, `credenza`, `shelving`.
  Lobby / reception plans add these:
    - **Reception desk** — `reception_desk` (wood body + stone counter + ledge).
    - **Bench** — `bench` (seat on metal legs), often in a row along a wall.
    - **Framed artwork** — `artwork` on a wall: set `rotation` so its width runs
      along the wall, `z` = bottom height, `height` = artwork height. Rendered
      with a colourful abstract canvas (placeholder for real art).
    - **Light fixtures** — `sconce` (wall globe, set `z`≈1.8), `downlight`
      (recessed ceiling spot, set `z`≈ceiling−0.1, lay them on a grid),
      `pendant` (hanging), `floor_lamp`. They glow AND cast real light pools.
      **Sconces hang off a wall, not in mid-air.** Place `at` on the wall
      surface and set `rotation` so the sconce's local +Y points INTO the
      wall: rotation 0 = wall along plan +Y (item pokes south); rotation 90
      = wall on the west side (item pokes east); rotation 180 = wall on the
      south; rotation 270 / -90 = wall on the east. The backplate stays
      flush with the wall and the globe projects ~r metres into the room.
    - **Dropped ceiling** — `ceiling` panel (`size`, `z`≈height−0.1); place
      `downlight`s just under it. Toggle it with the Plafond layer.
    - **Plants / planters** — `plant`, `plant_large`, `planter`.
  If a symbol is ambiguous, prefer a sensible default over skipping it —
  an empty 3D room is the worst outcome.

Set up a metre coordinate system with origin at the bottom-left of the
footprint, X to the right, Y upward in plan.

### 3. Author `building.json`

Follow `schema.json`. Minimal shape:

```json
{
  "meta": {"name": "...", "units": "m", "wall_height": 2.6, "wall_thickness": 0.2},
  "walls": [
    {"start": [x, y], "end": [x, y], "thickness": 0.2, "type": "exterior"},
    {"start": [x, y], "end": [x, y], "type": "glass"}
  ],
  "openings": [{"wall": 0, "kind": "door|window", "distance": 1.5,
                "width": 0.9, "sill": 0.0, "height": 2.1}],
  "slab": {"enabled": true, "thickness": 0.15},
  "roof": {"type": "flat|gable|none", "height": 1.5, "overhang": 0.3, "ridge_axis": "x"},
  "rooms": [{"name": "Open office", "at": [x, y]}],
  "furniture": [
    {"type": "desk_bench",       "at": [4, 3], "size": [3.0, 1.6], "seats": 3},
    {"type": "office_desk",      "at": [8, 3], "rotation": 90},
    {"type": "conference_table", "at": [15, 2], "size": [3.2, 1.1]},
    {"type": "round_table",      "at": [15, 10], "size": [1.8, 1.8]},
    {"type": "sofa",             "at": [2.6, 11], "size": [2.4, 0.95]},
    {"type": "coffee_table",     "at": [2.6, 10]},
    {"type": "rug",              "at": [2.8, 10.4], "size": [3.0, 2.2]},
    {"type": "kitchen_counter",  "at": [9.5, 11.6], "size": [3.2, 0.6]},
    {"type": "fridge",           "at": [11.4, 11.5]},
    {"type": "column",           "at": [6.4, 4.5], "round": true},
    {"type": "tv",               "at": [17.85, 2], "rotation": 90, "z": 1.05},
    {"type": "plant_large",      "at": [12.4, 6.2]},
    {"type": "stairs",           "at": [6, 3], "size": [1.0, 3.2], "height": 2.7, "rotation": 90}
  ]
}
```

Worked examples: `examples/lobby.json` (reception hall: wood-clad wall, artwork,
columns, sconces, downlit ceiling, terrazzo, benches), `examples/office_floor.json`
(open-plan office), `examples/demo_house.json` (residential).

A wall can carry a `"finish"` material, e.g.
`{"start": [...], "end": [...], "finish": "wood"}` for a wood-clad feature wall
(any furniture material name works: `wood`, `stone`, `concrete`, …).

Rules of thumb:
- `openings[].wall` is the index of the wall in the `walls` array.
- Use `"distance"` (metres from the wall start) OR `"position"` (0..1 fraction).
- Exterior walls ~0.2-0.30 m thick; interior ~0.08-0.12 m. A wall with
  `"type": "glass"` becomes a translucent partition (meeting-room walls).
- Flat roof for an office floor or an intermediate apartment floor; gable for a
  standalone house; `none` to leave the top open. Either way the viewer has a
  **Hide roof** button, so a roof never blocks the interior.
- One `rooms[]` entry per labelled space; `at` is any point inside the room.
- `furniture[]` is the heart of the result — **populate it generously.** Each
  item is a known type (full list in `schema.json`) plus an `at` centre point.
  Every type has a built-in component builder with default size/height, so
  `{"type": "sofa", "at": [x, y]}` already yields a multi-part sofa, an
  `office_desk` comes with monitor + keyboard, a `conference_table` comes with
  its chairs. Override `size: [w, d]` and `height` only for clearly
  non-standard items. Set `rotation` (degrees) so the item's front (-Y / open
  side) faces the way the symbol does. Keep items ~5–10 cm off walls to avoid
  z-fighting.
- `desk_bench` / `workstation`: `"seats": N` desks per row (two rows
  back-to-back). One bench per cluster of desks on the plan.
- `conference_table`, `meeting_table`, `round_table`, `dining_table` auto-add
  their chairs — do NOT also place individual chairs around them.
- `column` (`"round": true` for circular) for structural pillars; set `height`
  to the ceiling height.
- Stairs use `type: "stairs"`, `size: [tread_width, total_run]`,
  `height: total_rise` (top floor level). Add `"steps": N` only if you want
  to force a specific count; otherwise the generator picks ~17 cm risers.

### 4. Generate

```
python3 generate.py building.json --out work/output --preview
```

Outputs `model.glb`, `viewer.html`, and (with `--preview`) `preview.png`.

### 5. Verify, then deliver

- ALWAYS open `viewer.html` and look before claiming done. It renders with
  textured PBR materials (wood, terrazzo, marble, plaster, fabric, abstract
  art), soft shadows, ambient (IBL) lighting, tone mapping, and real light
  fixtures that cast pools. UI:
    - a **Calques** (layers) panel with checkboxes — Toit, Plafond, Murs, Verre,
      Sol, Mobilier, Luminaires, Étiquettes (rows for empty layers auto-hide);
    - view buttons **Iso**, **Dessus** (top), **Visite** (eye-level walkthrough;
      then **ZQSD / WASD / arrows** to walk).
  Check the footprint, that furniture sits in the right rooms, faces the right
  way, and doesn't overlap walls. From the console: `window.__viewer.setLayer
  ('roof', false)` / `.walk()` / `.topDown()` / `.iso()`.
- `preview.png` (with `--preview`, needs matplotlib) is a rough flat-shaded
  check only; the HTML viewer is the real deliverable quality.
- If something is off, fix `building.json` and regenerate. Iterate until the
  interior reads clearly.
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

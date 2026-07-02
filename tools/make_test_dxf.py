#!/usr/bin/env python3
"""Generate a small, realistic architectural test plan as DXF.

Purpose: give `plan_dxf.py` a known-ground-truth file so we can verify the
parser end-to-end without needing the user to supply a real .dwg/.dxf.

Conventions this test file follows (a sane subset of how real CAD plans are
drawn, so the parser learns useful heuristics):

  layer ROOMS    closed LWPOLYLINE per room footprint (the inner face of walls)
  layer LABELS   one TEXT per room, insertion point inside its footprint
  layer WINDOWS  a short LINE segment sitting on the wall that carries a window
  layer DOORS    an ARC (door swing) whose centre sits on the wall with the door

Units are millimetres ($INSUNITS = 4), the architectural default.

Layout (axis N = +Y / up, E = +X / right), a compact 3-room + hall flat:

        N
   +---------+---------+
   | CHAMBRE | S.D.B   |
   |   1     |         |
   +----+----+----+----+
   | HALL    | SEJOUR  |
   |         |         |
   +---------+---------+
        S
"""
import ezdxf

MM = 1000.0  # 1 m in this file's units (millimetres)


def rect(x0, y0, x1, y1):
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]


def build():
    doc = ezdxf.new("R2010", setup=True)
    doc.units = 4  # millimetres
    msp = doc.modelspace()
    for lyr, col in [("ROOMS", 7), ("LABELS", 3), ("WINDOWS", 5), ("DOORS", 1)]:
        if lyr not in doc.layers:
            doc.layers.add(lyr, color=col)

    # ── rooms: (name, x0,y0,x1,y1) in metres → mm ──────────────────────
    rooms = [
        ("CHAMBRE 1", 0.0, 3.0, 3.4, 6.0),   # top-left  (N-W)
        ("S.D.B",     3.4, 3.0, 6.0, 6.0),   # top-right (N-E)
        ("HALL",      0.0, 0.0, 3.0, 3.0),   # bot-left  (S-W)
        ("SEJOUR",    3.0, 0.0, 6.0, 3.0),   # bot-right (S-E)
    ]
    for name, x0, y0, x1, y1 in rooms:
        pts = [(x * MM, y * MM) for x, y in rect(x0, y0, x1, y1)]
        msp.add_lwpolyline(pts, close=True, dxfattribs={"layer": "ROOMS"})
        cx, cy = (x0 + x1) / 2 * MM, (y0 + y1) / 2 * MM
        t = msp.add_text(name, dxfattribs={"layer": "LABELS", "height": 200})
        t.set_placement((cx, cy))

    # ── windows: a LINE lying along the exterior wall it belongs to ────
    # CHAMBRE 1 → window on N wall (top edge, y=6.0)
    msp.add_line((0.8 * MM, 6.0 * MM), (2.4 * MM, 6.0 * MM),
                 dxfattribs={"layer": "WINDOWS"})
    # S.D.B → window on E wall (right edge, x=6.0)
    msp.add_line((6.0 * MM, 3.6 * MM), (6.0 * MM, 5.2 * MM),
                 dxfattribs={"layer": "WINDOWS"})
    # SEJOUR → large window (bay) on S wall (bottom edge, y=0.0)
    msp.add_line((3.6 * MM, 0.0 * MM), (5.6 * MM, 0.0 * MM),
                 dxfattribs={"layer": "WINDOWS"})

    # ── doors: an ARC whose centre sits on the wall with the doorway ───
    # CHAMBRE 1 door → S wall of chambre (y=3.0), opens from HALL
    msp.add_arc((1.5 * MM, 3.0 * MM), radius=0.8 * MM, start_angle=0,
                end_angle=90, dxfattribs={"layer": "DOORS"})
    # S.D.B door → W wall (x=3.4), opens from HALL/SEJOUR side
    msp.add_arc((3.4 * MM, 4.5 * MM), radius=0.8 * MM, start_angle=270,
                end_angle=360, dxfattribs={"layer": "DOORS"})
    # SEJOUR door → W wall (x=3.0), opens from HALL (the entry from hall)
    msp.add_arc((3.0 * MM, 1.5 * MM), radius=0.8 * MM, start_angle=270,
                end_angle=360, dxfattribs={"layer": "DOORS"})

    return doc


if __name__ == "__main__":
    import os, sys
    out = sys.argv[1] if len(sys.argv) > 1 else "work/_dxf/test_plan.dxf"
    os.makedirs(os.path.dirname(out), exist_ok=True)
    build().saveas(out)
    print(f"✓ test DXF → {out}")

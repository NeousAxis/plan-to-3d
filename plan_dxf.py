#!/usr/bin/env python3
"""plan_dxf.py — parse a real DXF/DWG floor plan into the EXACT geometry.json
that the plan-to-image pipeline already consumes.

Why this exists
---------------
Until now the geometry was *guessed* from a raster plan by a multi-agent vision
pass (plan_extract.py) — good, but fallible on windows, dimensions, odd rooms.
A CAD file carries the geometry *exactly*: wall coordinates to the millimetre,
openings, room labels. This parser reads that and emits the same schema, so the
rest of the pipeline (build_flux_prompt / --mode geometric) is unchanged — it
just gets ground truth instead of an estimate.

    plan.dxf ─► plan_dxf.py ─► geometry.json ─► (to_render_spec) ─► FLUX

We reuse ezdxf (DXF) — we never re-implement a CAD kernel. DWG is read by first
converting to DXF (ODA File Converter / LibreDWG `dwg2dxf`), see --help.

Heuristics (deliberately forgiving — real plans vary):
  rooms    closed LWPOLYLINE/POLYLINE, preferably on a layer whose name contains
           room/piece/space/local; else any closed loop with plausible area.
  labels   TEXT/MTEXT whose insertion point falls inside a room polygon.
  windows  entities on a layer containing window/fenetre/baie/glazing.
  doors    entities on a layer containing door/porte, or ARC door-swings.
  wall     N/S/E/W assigned by which edge of the room bbox the opening hugs.
  entry    the wall carrying a door (first/most-interior); null if none found.

Usage:
    python3 plan_dxf.py parse plan.dxf                 # -> plan.geometry.json
    python3 plan_dxf.py parse plan.dxf -o geometry.json
    python3 plan_dxf.py parse plan.dxf --dump          # + human summary
"""
import argparse
import json
import os
import sys

try:
    import ezdxf
except ImportError:
    sys.exit("ezdxf not installed → pip3 install ezdxf --break-system-packages")


# ── unit handling ──────────────────────────────────────────────────────────
# $INSUNITS code → metres-per-unit. Covers the values seen in the wild.
_UNIT_TO_M = {
    0: 0.001,   # unitless → assume mm (architectural default)
    1: 0.0254,  # inches
    2: 0.3048,  # feet
    4: 0.001,   # millimetres
    5: 0.01,    # centimetres
    6: 1.0,     # metres
    8: 1e-6,    # microns
    9: 0.0009144,  # yards? (rare) -- treated approximately
}


def units_to_m(doc):
    """Best-effort metres-per-drawing-unit factor."""
    code = doc.header.get("$INSUNITS", 0)
    return _UNIT_TO_M.get(code, 0.001)


# ── geometry helpers ───────────────────────────────────────────────────────
def poly_points(e):
    """Return list of (x, y) for a (LW)POLYLINE, ignoring bulges."""
    t = e.dxftype()
    if t == "LWPOLYLINE":
        return [(p[0], p[1]) for p in e.get_points("xy")]
    if t == "POLYLINE":
        return [(v.dxf.location.x, v.dxf.location.y) for v in e.vertices]
    return []


def is_closed(e, pts):
    if e.dxftype() == "LWPOLYLINE":
        if e.closed:
            return True
    if e.dxftype() == "POLYLINE":
        if e.is_closed:
            return True
    # geometrically closed (first ≈ last)
    return len(pts) >= 3 and _dist(pts[0], pts[-1]) < 1e-6


def _dist(a, b):
    return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5


def bbox(pts):
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return min(xs), min(ys), max(xs), max(ys)


def polygon_area(pts):
    """Shoelace, absolute."""
    n = len(pts)
    s = 0.0
    for i in range(n):
        x0, y0 = pts[i]
        x1, y1 = pts[(i + 1) % n]
        s += x0 * y1 - x1 * y0
    return abs(s) / 2.0


def point_in_poly(pt, pts):
    """Ray casting."""
    x, y = pt
    inside = False
    n = len(pts)
    j = n - 1
    for i in range(n):
        xi, yi = pts[i]
        xj, yj = pts[j]
        if ((yi > y) != (yj > y)) and \
           (x < (xj - xi) * (y - yi) / (yj - yi + 1e-30) + xi):
            inside = not inside
        j = i
    return inside


def is_rectangle(pts, tol=0.02):
    """True if the loop is an axis-aligned 4-corner rectangle (within tol as a
    fraction of its own size)."""
    # drop a duplicate closing vertex if present
    p = pts[:-1] if len(pts) > 1 and _dist(pts[0], pts[-1]) < 1e-6 else pts
    if len(p) != 4:
        return False
    x0, y0, x1, y1 = bbox(p)
    w, h = x1 - x0, y1 - y0
    scale = max(w, h) or 1.0
    corners = {(round((x - x0) / scale, 3) < 0.02 or abs((x - x1)) / scale < 0.02,
                round((y - y0) / scale, 3) < 0.02 or abs((y - y1)) / scale < 0.02)
               for x, y in p}
    # every vertex must sit on both an x-extreme and a y-extreme
    return all(a and b for a, b in corners)


# ── wall / compass assignment ──────────────────────────────────────────────
def entity_anchor(e):
    """A representative (x, y) for an opening entity."""
    t = e.dxftype()
    if t == "LINE":
        a, b = e.dxf.start, e.dxf.end
        return ((a.x + b.x) / 2, (a.y + b.y) / 2)
    if t == "ARC" or t == "CIRCLE":
        c = e.dxf.center
        return (c.x, c.y)
    if t == "INSERT":
        p = e.dxf.insert
        return (p.x, p.y)
    if t in ("LWPOLYLINE", "POLYLINE"):
        pts = poly_points(e)
        if pts:
            x0, y0, x1, y1 = bbox(pts)
            return ((x0 + x1) / 2, (y0 + y1) / 2)
    if t in ("TEXT", "MTEXT"):
        p = e.dxf.insert
        return (p.x, p.y)
    return None


def nearest_wall(anchor, room_bb):
    """Which wall (N/S/E/W) of room_bb the anchor hugs."""
    x, y = anchor
    x0, y0, x1, y1 = room_bb
    d = {"W": abs(x - x0), "E": abs(x - x1),
         "S": abs(y - y0), "N": abs(y - y1)}
    return min(d, key=d.get)


def dist_to_room(anchor, room_bb, pad):
    """Approx distance from anchor to a room's outline (0 if inside/on it)."""
    x, y = anchor
    x0, y0, x1, y1 = room_bb
    dx = max(x0 - x, 0, x - x1)
    dy = max(y0 - y, 0, y - y1)
    return (dx * dx + dy * dy) ** 0.5


# ── layer classification ───────────────────────────────────────────────────
def _has(name, *needles):
    n = (name or "").lower()
    return any(k in n for k in needles)


ROOM_LAYER = ("room", "piece", "pièce", "space", "local", "surface", "area")
WIN_LAYER = ("window", "fenetre", "fenêtre", "baie", "glaz", "vitr", "menuis")
DOOR_LAYER = ("door", "porte", "opening", "ouvert")


# ── main parse ─────────────────────────────────────────────────────────────
def parse(path):
    doc = ezdxf.readfile(path)
    msp = doc.modelspace()
    m = units_to_m(doc)

    # 1) collect candidate room loops
    loops = []  # (pts_m, area_m2, layer)
    for e in msp.query("LWPOLYLINE POLYLINE"):
        pts = poly_points(e)
        if len(pts) < 3 or not is_closed(e, pts):
            continue
        pts_m = [(x * m, y * m) for x, y in pts]
        area = polygon_area(pts_m)
        if area < 0.5 or area > 100000:  # ignore titleblocks / specks
            continue
        loops.append((pts_m, area, e.dxf.layer, is_rectangle(pts_m)))

    # prefer loops on a room-ish layer; else keep all plausible loops
    roomish = [l for l in loops if _has(l[2], *ROOM_LAYER)]
    chosen = roomish if roomish else loops
    # drop a loop that fully contains another chosen loop (outer building shell)
    chosen = _drop_enclosing(chosen)

    rooms = []
    for pts_m, area, layer, rect in chosen:
        bb = bbox(pts_m)
        x0, y0, x1, y1 = bb
        rooms.append({
            "name": None,
            "_pts": pts_m, "_bb": bb,
            "dims": {"w_m": round(x1 - x0, 2), "d_m": round(y1 - y0, 2),
                     "area_m2": round(area, 2), "ceiling_m": None},
            "openings": [], "fixtures": [],
            "shape": "rect" if rect else "irregular",
            "shape_note": "" if rect else "non-rectangular footprint from DXF",
            "entry": None,
        })

    # 2) attach labels (TEXT/MTEXT) by point-in-polygon
    for e in msp.query("TEXT MTEXT"):
        anchor = entity_anchor(e)
        if not anchor:
            continue
        anchor_m = (anchor[0] * m, anchor[1] * m)
        txt = (e.plain_text() if e.dxftype() == "MTEXT" else e.dxf.text).strip()
        if not txt:
            continue
        for r in rooms:
            if point_in_poly(anchor_m, r["_pts"]):
                # keep the first / shortest plausible label
                if r["name"] is None or len(txt) < len(r["name"]):
                    r["name"] = txt
                break

    # fallback names
    for i, r in enumerate(rooms, 1):
        if not r["name"]:
            r["name"] = f"PIECE {i}"

    # 3) openings: windows + doors → assign to a room + wall.
    #    A door on a shared wall is ambiguous by position alone (its centre
    #    sits on the party wall), so for door-swing ARCs we sample a point on
    #    the swing: the room that CONTAINS it is the room the door opens into.
    def _swing_point(e):
        import math
        c = e.dxf.center
        a0, a1 = e.dxf.start_angle, e.dxf.end_angle
        if a1 < a0:
            a1 += 360.0
        mid = math.radians((a0 + a1) / 2.0)
        r = e.dxf.radius * 0.5
        return ((c.x + r * math.cos(mid)) * m, (c.y + r * math.sin(mid)) * m)

    def assign(e, kind):
        anchor = entity_anchor(e)
        if not anchor:
            return
        anchor_m = (anchor[0] * m, anchor[1] * m)
        best = None
        # door-swing ARC → pick the room the swing opens into
        if kind == "door" and e.dxftype() == "ARC":
            sp = _swing_point(e)
            best = next((r for r in rooms if point_in_poly(sp, r["_pts"])), None)
        if best is None:
            best = min(rooms, key=lambda r: dist_to_room(anchor_m, r["_bb"], 0),
                       default=None)
        if best is None:
            return
        wall = nearest_wall(anchor_m, best["_bb"])
        best["openings"].append({"kind": kind, "wall": wall})
        if kind == "door" and best["entry"] is None:
            best["entry"] = wall

    for e in msp:
        lyr = e.dxf.layer
        t = e.dxftype()
        if _has(lyr, *WIN_LAYER):
            assign(e, "window")
        elif _has(lyr, *DOOR_LAYER) or (t == "ARC" and _has(lyr, *DOOR_LAYER)):
            assign(e, "door")
        elif t == "ARC":
            # bare door-swing arcs on a generic layer: radius 0.6–1.2 m
            r_m = e.dxf.radius * m
            if 0.6 <= r_m <= 1.3:
                assign(e, "door")

    # 4) dedupe openings per room (same kind+wall counted once)
    for r in rooms:
        seen = set()
        uniq = []
        for o in r["openings"]:
            k = (o["kind"], o["wall"])
            if k not in seen:
                seen.add(k)
                uniq.append(o)
        r["openings"] = uniq

    # scale record: the largest known real dimension we can vouch for
    scale = {}
    if rooms:
        big = max(rooms, key=lambda r: r["dims"]["area_m2"])
        scale[f"{big['name']} width"] = f"{big['dims']['w_m']:.2f} m (from DXF)"

    # strip private fields
    for r in rooms:
        r.pop("_pts", None)
        r.pop("_bb", None)

    return {"rooms": rooms, "scale": scale,
            "_source": {"file": os.path.basename(path),
                        "units_m_per_unit": m, "rooms_found": len(rooms)}}


def _drop_enclosing(loops):
    """Remove a loop that geometrically contains ≥2 other loops (building shell
    drawn as one big outline around the flat)."""
    if len(loops) < 3:
        return loops
    keep = []
    for i, (pts_i, area_i, lyr_i, rect_i) in enumerate(loops):
        bb_i = bbox(pts_i)
        contained = 0
        for j, (pts_j, area_j, _, _) in enumerate(loops):
            if i == j or area_j >= area_i:
                continue
            cx = sum(p[0] for p in pts_j) / len(pts_j)
            cy = sum(p[1] for p in pts_j) / len(pts_j)
            if point_in_poly((cx, cy), pts_i):
                contained += 1
        if contained >= 2:
            continue  # this is the shell
        keep.append((pts_i, area_i, lyr_i, rect_i))
    return keep or loops


def summarise(geo):
    lines = [f"Source: {geo['_source']['file']}  "
             f"({geo['_source']['units_m_per_unit']} m/unit, "
             f"{geo['_source']['rooms_found']} rooms)"]
    for r in geo["rooms"]:
        d = r["dims"]
        wins = ",".join(o["wall"] for o in r["openings"] if o["kind"] == "window") or "—"
        drs = ",".join(o["wall"] for o in r["openings"] if o["kind"] == "door") or "—"
        lines.append(
            f"  • {r['name']:<12} {d['w_m']}×{d['d_m']} m = {d['area_m2']} m²  "
            f"[{r['shape']}]  win:{wins}  door:{drs}  entry:{r['entry'] or '—'}")
    return "\n".join(lines)


# ── plan.json (web/cad.html model, rects + doors/windows) → real DXF ────────
# Closes the loop: raster plan → plan.json → .dxf openable in ARES/AutoCAD.
# Layer conventions match parse() so the round-trip export→parse is testable.
_DOOR_W_M = 0.9
_WIN_W_M = 1.4


def _wall_geo(rect, wall):
    """(anchor, unit-dir, length, into-room normal) in plan coords (y down)."""
    x0, y0, x1, y1 = rect
    if wall == "N":
        return (x0, y0), (1, 0), x1 - x0, (0, 1)
    if wall == "S":
        return (x0, y1), (1, 0), x1 - x0, (0, -1)
    if wall == "W":
        return (x0, y0), (0, 1), y1 - y0, (1, 0)
    return (x1, y0), (0, 1), y1 - y0, (-1, 0)  # E


def export_dxf(plan_path, out):
    import math
    plan = json.load(open(plan_path, encoding="utf-8"))
    rooms = [r for r in plan.get("rooms", []) if r.get("rect")]
    if not rooms:
        sys.exit("plan has no rooms with rect[] — layout positions required "
                 "(use the web/cad.html model format)")
    ymax = max(r["rect"][3] for r in rooms)
    MM = 1000.0

    def P(x, y):  # plan coords (y down, metres) → DXF (y up, mm)
        return (x * MM, (ymax - y) * MM)

    doc = ezdxf.new("R2010", setup=True)
    doc.units = 4  # millimetres
    msp = doc.modelspace()
    for lyr, col in [("ROOMS", 7), ("LABELS", 3), ("WINDOWS", 5), ("DOORS", 1)]:
        if lyr not in doc.layers:
            doc.layers.add(lyr, color=col)

    for r in rooms:
        x0, y0, x1, y1 = r["rect"]
        msp.add_lwpolyline([P(x0, y0), P(x1, y0), P(x1, y1), P(x0, y1)],
                           close=True, dxfattribs={"layer": "ROOMS"})
        t = msp.add_text(r["name"], dxfattribs={"layer": "LABELS", "height": 200})
        t.set_placement(P((x0 + x1) / 2, (y0 + y1) / 2))
        for o in r.get("windows", []):
            a, u, L, n = _wall_geo(r["rect"], o["wall"])
            c, h = o["at"] * L, _WIN_W_M / 2
            msp.add_line(P(a[0] + u[0] * (c - h), a[1] + u[1] * (c - h)),
                         P(a[0] + u[0] * (c + h), a[1] + u[1] * (c + h)),
                         dxfattribs={"layer": "WINDOWS"})
        for o in r.get("doors", []):
            a, u, L, n = _wall_geo(r["rect"], o["wall"])
            c, h = o["at"] * L, _DOOR_W_M / 2
            hinge = (a[0] + u[0] * (c - h), a[1] + u[1] * (c - h))
            # directions in DXF coords (flip y): closed leaf = along wall,
            # open leaf = into the room → 90° swing arc between the two
            vt = (u[0], -u[1])
            vn = (n[0], -n[1])
            a0 = math.degrees(math.atan2(vt[1], vt[0])) % 360
            a1 = math.degrees(math.atan2(vn[1], vn[0])) % 360
            if (a1 - a0) % 360 > 180:
                a0, a1 = a1, a0
            msp.add_arc(P(*hinge), radius=_DOOR_W_M * MM,
                        start_angle=a0, end_angle=a1,
                        dxfattribs={"layer": "DOORS"})

    d = os.path.dirname(out)
    if d:
        os.makedirs(d, exist_ok=True)
    doc.saveas(out)
    return len(rooms)


def main():
    ap = argparse.ArgumentParser(
        description="Parse a DXF floor plan into geometry.json, or export a "
                    "plan.json layout into a real DXF.",
        epilog="DWG? Convert first: `dwg2dxf plan.dwg` (LibreDWG) or the free "
               "ODA File Converter, then parse the .dxf.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("parse", help="parse a .dxf into geometry.json")
    p.add_argument("dxf")
    p.add_argument("-o", "--out", default=None)
    p.add_argument("--dump", action="store_true", help="print a human summary")
    pe = sub.add_parser("export", help="plan.json (rect layout) -> .dxf "
                                       "(openable in ARES/AutoCAD)")
    pe.add_argument("plan")
    pe.add_argument("-o", "--out", default=None)
    args = ap.parse_args()

    if args.cmd == "parse":
        geo = parse(args.dxf)
        out = args.out or os.path.splitext(args.dxf)[0] + ".geometry.json"
        json.dump(geo, open(out, "w", encoding="utf-8"),
                  ensure_ascii=False, indent=2)
        print(f"✓ geometry → {out}  ({len(geo['rooms'])} rooms)")
        if args.dump:
            print(summarise(geo))
    elif args.cmd == "export":
        out = args.out or os.path.splitext(args.plan)[0] + ".dxf"
        n = export_dxf(args.plan, out)
        print(f"✓ DXF → {out}  ({n} rooms)")


if __name__ == "__main__":
    main()

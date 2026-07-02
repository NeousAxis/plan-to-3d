#!/usr/bin/env python3
"""plan_reader.py — AUTOMATIC raster plan → plan.json (the cad.html model).

This is the missing front of the pipeline: no more hand-typed geometry.

    plan.png ─► CF Mistral vision (free, no card, no Google)
             ─► strict regex-parseable lines (one fact per line)
             ─► plan_raster solver  (printed area ÷ known side = other side)
             ─► packer              (bbox fractions → snapped rects)
             ─► plan.json           (single source of truth for web/cad.html,
                                     plan_dxf.py export, plan_to_image.py)

Design rules (learned the hard way in sessions #2/#3):
  * The vision model NEVER free-forms JSON. It emits one strict line per fact;
    every non-conforming line is rejected and counted (plan_extract.py style).
  * Dimensions are never taken from the model's guesses: printed areas + one
    printed side per room go through the deterministic solver.
  * Positions come from coarse bboxes (fractions of the drawing) and are then
    SNAPPED: sizes forced to solved dims, edges clustered and aligned, clamped
    to the printed envelope. The model only needs to be roughly right.

Usage:
    python3 plan_reader.py read plan.png -o web/plans/monplan.json
    python3 plan_reader.py read plan.png --dry            # show parsed facts only
    python3 plan_reader.py read --from-lines lines.txt -o out.json
                                                          # offline test / fixture
"""
import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from plan_raster import solve_room  # deterministic: area ÷ side  # noqa: E402

# ── the strict contract given to the vision model ───────────────────────────
VISION_PROMPT = """\
You are reading an architectural floor plan image. Output ONLY lines in the
EXACT formats below, one fact per line, nothing else (no prose, no markdown).

ENVELOPE: <total_width_m> x <total_depth_m>
CHAIN TOP: <m> | <m> | ...
CHAIN BOTTOM: <m> | <m> | ...
CHAIN LEFT: <m> | <m> | ...
CHAIN RIGHT: <m> | <m> | ...
ROOM: <NAME> | area <m2> | bbox <x0>,<y0>,<x1>,<y1>
SIDE: <NAME> | <w_or_d> <metres>
DOOR: <NAME> | <N_S_E_or_W> | <position_0_to_1>
WINDOW: <NAME> | <N_S_E_or_W> | <position_0_to_1>

Rules:
- CHAIN lines: copy each printed dimension chain VERBATIM, in reading order
  (TOP/BOTTOM chains left→right, LEFT/RIGHT chains top→bottom), one number per
  segment separated by |. These are the most important lines — copy exactly
  what is printed, nothing more. Omit a CHAIN line if that side has no chain.
- ENVELOPE from the printed dimension chains (sum of the top chain x sum of a
  side chain), metres, 2 decimals.
- ROOM: one line per labelled room. area = the m2 figure PRINTED on the plan.
  bbox = the room's approximate rectangle as FRACTIONS (0..1) of the drawn
  building extent, x left→right, y top→bottom. If a room is an irregular
  circulation (hall/corridor), write "bbox -".
- SIDE: a room side you can read from the printed dimension chains ONLY
  (w = horizontal width, d = vertical depth). One line per known side.
  NEVER estimate a side visually — printed numbers only.
- CRITICAL: if the plan prints NO m2 figure for a room, write "area -".
  If the plan has NO dimension chains, output NO SIDE lines at all and
  "ENVELOPE: - x -". Inventing numbers is the worst possible failure;
  missing data is expected and fine.
- DOOR/WINDOW: wall as compass letter (N=top, S=bottom, W=left, E=right of
  that room), position = fraction along that wall (0 = W/N end, 1 = E/S end),
  0.5 if unsure. Doors are the swing arcs; windows are the thin wall breaks.
- Room names EXACTLY as printed. Every number with a dot, 2 decimals.
"""

# dedicated micro-prompt: label positions only (one info per pass — far more
# reliable than asking one mega-call for everything, cf. plan_extract.py)
LABELS_PROMPT = """\
You are reading an architectural floor plan image. Locate the PRINTED NAME of
every room. Output ONLY lines in this exact format, one per room, nothing else:

LABEL: <NAME> | <x>,<y>

where x,y is the centre of the room's printed name as FRACTIONS (0..1) of the
full image, x from the LEFT edge, y from the TOP edge (y=0 is the TOP).
Room names exactly as printed. Numbers with 2 decimals.
"""

def completeness_prompt(names):
    """Third focused pass: hunt for rooms the main passes missed (tiny rooms
    like WC are dropped surprisingly often)."""
    return f"""\
You are reading an architectural floor plan image. These rooms were already
identified: {", ".join(sorted(names))}.
Search the plan carefully for any OTHER labelled room that is NOT in this
list — especially SMALL rooms: WC, toilet, storage, cellier, placard, pantry.
For each missed room output ONLY these lines (nothing else):

ROOM: <NAME> | area <m2_printed_or_-> | bbox -
LABEL: <NAME> | <x>,<y>

LABEL x,y = centre of the room's printed name as fractions (0..1) of the full
image, y=0 at the TOP. If no room was missed, output exactly: NONE
"""


_NUM = r"(\d+(?:[.,]\d+)?)"
RE = {
    "envelope": re.compile(
        rf"^ENVELOPE:\s*(?:{_NUM}|-)\s*[x×]\s*(?:{_NUM}|-)\s*$", re.I),
    "room": re.compile(
        rf"^ROOM:\s*(?P<name>[^|]+?)\s*\|\s*area\s*(?:(?P<area>{_NUM})\s*(?:m²|m2)?|-)\s*\|\s*bbox\s*"
        rf"(?:(?P<bb>{_NUM}\s*,\s*{_NUM}\s*,\s*{_NUM}\s*,\s*{_NUM})|-)\s*$", re.I),
    "chain": re.compile(
        rf"^CHAIN\s+(?P<side>TOP|BOTTOM|LEFT|RIGHT):\s*(?P<vals>{_NUM}"
        rf"(?:\s*\|\s*{_NUM})*)\s*$", re.I),
    "side": re.compile(
        rf"^SIDE:\s*(?P<name>[^|]+?)\s*\|\s*(?P<axis>[wd])\s*{_NUM}\s*$", re.I),
    "label": re.compile(
        rf"^LABEL:\s*(?P<name>[^|]+?)\s*\|\s*{_NUM}\s*,\s*{_NUM}\s*$", re.I),
    "door": re.compile(
        rf"^DOOR:\s*(?P<name>[^|]+?)\s*\|\s*(?P<wall>[NSEW])\s*(?:\|\s*{_NUM})?\s*$", re.I),
    "window": re.compile(
        rf"^WINDOW:\s*(?P<name>[^|]+?)\s*\|\s*(?P<wall>[NSEW])\s*(?:\|\s*{_NUM})?\s*$", re.I),
}


def _f(s):
    return float(str(s).replace(",", "."))


def _key(name):
    return re.sub(r"\s+", " ", name.strip().upper())


def parse_lines(text):
    """Strict parse → facts dict + report. Non-conforming lines rejected."""
    env = None
    chains = {}  # TOP/BOTTOM/LEFT/RIGHT → [m, m, ...]
    rooms = {}   # key → {name, area, bbox|None, sides{}, doors[], windows[]}
    ok = bad = 0
    rejected = []

    def R(name):
        k = _key(name)
        return rooms.setdefault(k, {"name": k, "area": None, "bbox": None,
                                    "label": None, "sides": {},
                                    "doors": [], "windows": []})

    for raw in text.splitlines():
        ln = raw.strip()
        if not ln:
            continue
        m = RE["envelope"].match(ln)
        if m:
            env = (_f(m.group(1)), _f(m.group(2))) \
                if m.group(1) and m.group(2) else None
            ok += 1; continue
        m = RE["room"].match(ln)
        if m:
            r = R(m.group("name"))
            a = _f(m.group("area")) if m.group("area") else None
            if a is not None:            # never let a later "-" erase a value
                r["area"] = a
            if m.group("bb") and r["bbox"] is None:   # first bbox wins
                nums = [_f(x) for x in re.findall(_NUM, m.group("bb"))]
                r["bbox"] = nums[:4]
            ok += 1; continue
        m = RE["label"].match(ln)
        if m:
            r = R(m.group("name"))
            if r["label"] is None:
                r["label"] = (_f(m.group(2)), _f(m.group(3)))
            ok += 1; continue
        m = RE["chain"].match(ln)
        if m:
            chains[m.group("side").upper()] = \
                [_f(v) for v in re.findall(_NUM, m.group("vals"))]
            ok += 1; continue
        m = RE["side"].match(ln)
        if m:
            R(m.group("name"))["sides"][m.group("axis").lower()] = _f(m.group(3))
            ok += 1; continue
        matched = False
        for kind in ("door", "window"):
            m = RE[kind].match(ln)
            if m:
                at = _f(m.group(3)) if m.group(3) else 0.5
                R(m.group("name"))[kind + "s"].append(
                    {"wall": m.group("wall").upper(), "at": at})
                ok += 1; matched = True; break
        if matched:
            continue
        bad += 1; rejected.append(ln)

    # dedupe openings (multi-pass merges repeat them)
    for r in rooms.values():
        for kind in ("doors", "windows"):
            seen, uniq = set(), []
            for o in r[kind]:
                k = (o["wall"], round(o["at"], 1))
                if k not in seen:
                    seen.add(k); uniq.append(o)
            r[kind] = uniq

    return {"envelope": env, "chains": chains, "rooms": list(rooms.values())}, \
           {"ok": ok, "rejected": bad, "rejected_lines": rejected[:8]}


# ── solve dims then pack positions on the printed wall grid ─────────────────


def _wall_grid(chains):
    """Cumulative sums of the printed dimension chains = REAL wall positions."""
    xs, ys = {0.0}, {0.0}
    for side, acc in (("TOP", xs), ("BOTTOM", xs), ("LEFT", ys), ("RIGHT", ys)):
        c = 0.0
        for v in chains.get(side, []):
            c += v
            acc.add(round(c, 2))
    return sorted(xs), sorted(ys)


def _overlap(a, b):
    w = min(a[2], b[2]) - max(a[0], b[0])
    h = min(a[3], b[3]) - max(a[1], b[1])
    return max(w, 0) * max(h, 0)


def _norm_static(v, vmin, vmax, margin=0.12):
    """Map a full-image fraction onto the building interior (labels include
    the image margins/dimension bands → min..max spread ≈ the interior)."""
    if vmax - vmin < 0.05:
        return 0.5
    return margin + (1 - 2 * margin) * (v - vmin) / (vmax - vmin)


def _near(v, grid, tol=0.15):
    """Grid line closest to v within tol, else None."""
    best = None
    for g in grid:
        if abs(g - v) <= tol and (best is None or abs(g - v) < abs(best - v)):
            best = g
    return best


def _chain_spans(chains):
    """Consecutive cumulative pairs of each printed chain = real wall-to-wall
    bays. Area-only rooms may only span such a bay (kills fantasy spans)."""
    sx, sy = set(), set()
    for side, acc in (("TOP", sx), ("BOTTOM", sx), ("LEFT", sy), ("RIGHT", sy)):
        c = 0.0
        for v in chains.get(side, []):
            acc.add((round(c, 2), round(c + v, 2)))
            c += v
    return sorted(sx), sorted(sy)


def _candidates(r, dims, xs, ys, EW, ED, spans=None, anchors=None):
    """All placements of this room whose edges land on printed wall lines and
    whose size matches its known geometry (exact dims, or printed area with
    one grid span). The plan's own numbers generate the possibilities."""
    out = []
    w, d, area = dims["w_m"], dims["d_m"], r["area"]
    if w and d:
        # anchor ONE edge per axis on a wall line; the opposite edge floats at
        # the exact dimension (interior partitions are often not chain lines)
        xopts, yopts = set(), set()
        for x0 in xs:
            if x0 + w <= EW + 0.1:
                xopts.add((x0, _near(x0 + w, xs) or round(x0 + w, 2)))
        for x1 in xs:
            if x1 - w >= -0.1:
                xopts.add((_near(x1 - w, xs) or round(x1 - w, 2), x1))
        for y0 in ys:
            if y0 + d <= ED + 0.1:
                yopts.add((y0, _near(y0 + d, ys) or round(y0 + d, 2)))
        for y1 in ys:
            if y1 - d >= -0.1:
                yopts.add((_near(y1 - d, ys) or round(y1 - d, 2), y1))
        for xa, xb in xopts:
            for ya, yb in yopts:
                out.append((xa, ya, xb, yb))
    elif area:
        # spans: only real chain bays (consecutive cumulative pairs);
        # anchors: wall grid + edges of already-placed rooms (partitions)
        spx, spy = spans if spans else ([], [])
        ax = anchors[0] if anchors else xs
        ay = anchors[1] if anchors else ys
        for x0, x1 in spx:                                # x-span driven
            sp = x1 - x0
            if not 0.5 <= sp <= 9:
                continue
            o = area / sp
            if not 0.7 <= o <= 9 or max(sp, o) / min(sp, o) > 3.2:
                continue
            for y0 in ay:
                if y0 + o <= ED + 0.1:
                    out.append((x0, y0, x1, y0 + o))
            for y1 in ay:
                if y1 - o >= -0.1:
                    out.append((x0, y1 - o, x1, y1))
        for y0, y1 in spy:                                # y-span driven
            sp = y1 - y0
            if not 0.5 <= sp <= 9:
                continue
            o = area / sp
            if not 0.7 <= o <= 9 or max(sp, o) / min(sp, o) > 3.2:
                continue
            for x0 in ax:
                if x0 + o <= EW + 0.1:
                    out.append((x0, y0, x0 + o, y1))
            for x1 in ax:
                if x1 - o >= -0.1:
                    out.append((x1 - o, y0, x1, y1))
    seen, uniq = set(), []
    for c in out:
        k = tuple(round(v, 2) for v in c)
        if k not in seen:
            seen.add(k)
            uniq.append(k)
    return uniq


def _tile(solved, xs, ys, EW, ED, flip, fixed=(), spans=None, anchors=None,
          lrange=None):
    """Pick one candidate per room so that rooms never overlap (nor overlap
    the `fixed` rects), staying as close as possible to the vision hints —
    positions come from the printed grid. Exhaustive search with pruning."""
    # normalise label coordinates: they are fractions of the FULL IMAGE
    # (margins + dimension bands included) → map their min..max spread onto
    # the building interior so the systematic offset cancels out
    lxs = lrange[0] if lrange else \
        [r["label"][0] for r, _, _ in solved if r.get("label")]
    lys = lrange[1] if lrange else \
        [r["label"][1] for r, _, _ in solved if r.get("label")]
    _norm = _norm_static

    items = []
    for r, dims, _ in solved:
        # area-only rooms need a position hint (label or bbox) to be placed;
        # without one, any grid position would fit → garbage. Label-only then.
        if not (dims["w_m"] and dims["d_m"]) \
                and not r["bbox"] and not r.get("label"):
            continue
        cands = _candidates(r, dims, xs, ys, EW, ED, spans, anchors)
        if not cands:
            continue
        # position hint: the printed room-name position (a dedicated, reliable
        # vision pass) beats the room bbox (often sloppy / axis-confused)
        ctr = None
        if r.get("label"):
            # labels are trusted y-down (their prompt pins the convention and
            # it is verifiable) — the flip trial only concerns sloppy bboxes
            nx = _norm(r["label"][0], min(lxs), max(lxs))
            ny = _norm(r["label"][1], min(lys), max(lys))
            ctr = (nx * EW, ny * ED)
        elif r["bbox"]:
            b = r["bbox"]
            y0f, y1f = (1 - b[3], 1 - b[1]) if flip else (b[1], b[3])
            ctr = ((b[0] + b[2]) / 2 * EW, (y0f + y1f) / 2 * ED)
        # candidate score = distance to the position hint + a penalty when the
        # candidate's size deviates from the solved dims (a grid anchor 6 cm
        # off must not beat the exact printed size)
        w, d = dims["w_m"], dims["d_m"]

        def _score(c, _ctr=ctr, _w=w, _d=d):
            s = 0.0
            if _ctr:
                s += ((c[0] + c[2]) / 2 - _ctr[0]) ** 2 \
                     + ((c[1] + c[3]) / 2 - _ctr[1]) ** 2
            if _w and _d:
                # strong: a grid anchor a few cm off must never beat the
                # exact printed size
                s += 30 * ((c[2] - c[0] - _w) ** 2 + (c[3] - c[1] - _d) ** 2)
            return s

        scored = sorted(((c, _score(c)) for c in cands), key=lambda t: t[1])
        # keep the search tractable: exact-dims rooms have few candidates
        # anyway; area-only rooms are capped harder (hint-sorted, best first)
        cap = 40 if (w and d) else 12
        items.append((r["name"], scored[:cap]))
    items.sort(key=lambda it: len(it[1]))
    best = {"n": -1, "dev": 1e18, "asg": {}}

    def bt(i, asg, dev):
        remaining = len(items) - i
        if len(asg) + remaining < best["n"]:
            return                                  # can't beat placed count
        if dev >= best["dev"] and len(asg) + remaining <= best["n"]:
            return                                  # can't beat the score either
        if i == len(items):
            if len(asg) > best["n"] or \
               (len(asg) == best["n"] and dev < best["dev"]):
                best.update(n=len(asg), dev=dev, asg=dict(asg))
            return
        name, scored = items[i]
        for c, s in scored:
            if any(_overlap(c, p) > 0.05 for p in asg.values()) or \
               any(_overlap(c, p) > 0.05 for p in fixed):
                continue
            asg[name] = c
            bt(i + 1, asg, dev + s)
            del asg[name]
        bt(i + 1, asg, dev)                         # room may stay unplaced
    bt(0, {}, 0.0)
    return best


_CIRC = ("HALL", "DGT", "COULOIR", "CORRIDOR", "DEGAGEMENT", "DÉGAGEMENT",
         "CIRCULATION", "ENTREE", "ENTRÉE", "PALIER")


def _is_circ(name):
    return any(k in name.upper() for k in _CIRC)


def _edges(rc):
    x0, y0, x1, y1 = rc
    return {"N": (x0, y0, x1, y0), "S": (x0, y1, x1, y1),
            "W": (x0, y0, x0, y1), "E": (x1, y0, x1, y1)}


def _fix_openings(rooms_by_name, rects, EW, ED, hall_pt, circ_cells,
                  hull_tol=0.45):
    """Deterministic openings from geometry:
    - DOOR: on the interior wall with the LONGEST contact with the
      circulation region (hall + corridors = leftover cells); nearest point
      to the hall as tie-break. Architectural truth: rooms open onto the
      circulation.
    - WINDOW: only kept on exterior walls (on the envelope hull), one per
      wall; habitable rooms left without any window get one on their longest
      exterior wall."""
    def is_ext(wall, rc):
        x0, y0, x1, y1 = rc
        return {"N": y0 <= hull_tol, "S": y1 >= ED - hull_tol,
                "W": x0 <= hull_tol, "E": x1 >= EW - hull_tol}[wall]

    def contact(wall, seg):
        """(total length, centre-t of the widest run) of this wall shared
        with a circulation cell face."""
        ax, ay, bx, by = seg
        wl = max(abs(bx - ax), abs(by - ay)) or 1.0
        total, best = 0.0, None
        for cx0, cy0, cx1, cy1 in circ_cells:
            ov = 0.0; mid_t = None
            if wall in ("N", "S") and abs((cy1 if wall == "N" else cy0)
                                          - ay) < 0.03:
                lo, hi = max(ax, cx0), min(bx, cx1)
                if hi > lo:
                    ov = hi - lo; mid_t = ((lo + hi) / 2 - ax) / wl
            elif wall in ("W", "E") and abs((cx1 if wall == "W" else cx0)
                                            - ax) < 0.03:
                lo, hi = max(ay, cy0), min(by, cy1)
                if hi > lo:
                    ov = hi - lo; mid_t = ((lo + hi) / 2 - ay) / wl
            if ov > 0:
                total += ov
                if best is None or ov > best[0]:
                    best = (ov, mid_t)
        return total, (best[1] if best else None)

    def nearest_t(seg):
        ax, ay, bx, by = seg
        dx, dy = bx - ax, by - ay
        L2 = dx * dx + dy * dy
        t = 0.5 if L2 == 0 else max(0.0, min(1.0, (
            (hall_pt[0] - ax) * dx + (hall_pt[1] - ay) * dy) / L2))
        px, py = ax + t * dx, ay + t * dy
        return t, (px - hall_pt[0]) ** 2 + (py - hall_pt[1]) ** 2

    for name, rc in rects.items():
        r = rooms_by_name[name]
        if _is_circ(name):
            r["doors"], r["windows"] = [], []
            continue
        # door: interior wall — max circulation contact (door sits at the
        # centre of the widest shared run), else nearest point to the hall
        best = None
        for wall, seg in _edges(rc).items():
            if is_ext(wall, rc):
                continue
            tot, mid = contact(wall, seg)
            t, dist = nearest_t(seg)
            at = mid if (tot > 0.25 and mid is not None) else t
            key = (-round(tot, 2), dist)
            if best is None or key < best[0]:
                best = (key, wall, at)
        if best:
            r["doors"] = [{"wall": best[1],
                           "at": round(max(0.12, min(0.88, best[2])), 2)}]
        # windows: exterior walls only, one per wall (mean position)
        by_wall = {}
        for o in r.get("windows", []):
            if is_ext(o["wall"], rc):
                by_wall.setdefault(o["wall"], []).append(o["at"])
        kept = [{"wall": w, "at": round(sum(a) / len(a), 2)}
                for w, a in by_wall.items()]
        if not kept and (r.get("area") or 0) >= 7:
            ext = [(wall, s) for wall, s in _edges(rc).items()
                   if is_ext(wall, rc)]
            if ext:
                wall, _ = max(ext, key=lambda we: abs(we[1][2] - we[1][0])
                              + abs(we[1][3] - we[1][1]))
                kept = [{"wall": wall, "at": 0.5}]
        r["windows"] = kept


def _gap_fill(rects, EW, ED):
    """Cells of the envelope not covered by any room. A clean rectangular gap
    of plausible room size = a room the vision missed → surfaced as PIECE ?
    so the user sees the hole instead of silently losing a room."""
    ex = sorted({0.0, EW, *[v for rc in rects.values() for v in (rc[0], rc[2])]})
    ey = sorted({0.0, ED, *[v for rc in rects.values() for v in (rc[1], rc[3])]})
    covered = [[any(rc[0] <= (ex[i] + ex[i + 1]) / 2 <= rc[2] and
                    rc[1] <= (ey[j] + ey[j + 1]) / 2 <= rc[3]
                    for rc in rects.values())
                for j in range(len(ey) - 1)] for i in range(len(ex) - 1)]
    gaps, used = [], set()
    for i in range(len(ex) - 1):
        for j in range(len(ey) - 1):
            if covered[i][j] or (i, j) in used:
                continue
            i2 = i
            while i2 + 1 < len(ex) - 1 and not covered[i2 + 1][j] \
                    and (i2 + 1, j) not in used:
                i2 += 1
            j2 = j
            while j2 + 1 < len(ey) - 1 and all(
                    not covered[k][j2 + 1] and (k, j2 + 1) not in used
                    for k in range(i, i2 + 1)):
                j2 += 1
            for k in range(i, i2 + 1):
                for l in range(j, j2 + 1):
                    used.add((k, l))
            g = (ex[i], ey[j], ex[i2 + 1], ey[j2 + 1])
            gaps.append([round(v, 2) for v in g])
    return gaps


def _touches(a, b, min_len=0.4):
    """True if rects a and b share an edge segment of at least min_len."""
    for (e1, e2, lo, hi, olo, ohi) in (
            (a[2], b[0], a[1], a[3], b[1], b[3]),   # a.E vs b.W
            (a[0], b[2], a[1], a[3], b[1], b[3]),   # a.W vs b.E
            (a[3], b[1], a[0], a[2], b[0], b[2]),   # a.S vs b.N
            (a[1], b[3], a[0], a[2], b[0], b[2])):  # a.N vs b.S
        if abs(e1 - e2) < 0.03 and min(hi, ohi) - max(lo, olo) >= min_len:
            return True
    return False


# ── deterministic auto-furnishing ────────────────────────────────────────────
# Architectural placement rules per room type. Everything anchored on "quiet"
# walls (no door, then no window), door clearance respected. Furniture is DATA
# (goes into plan.json) so the editor/renderers stay dumb.

def _room_kind(name):
    n = name.upper()
    if "BED" in n or "CHAMBRE" in n:
        return "bedroom"
    if "LIVING" in n or "SEJOUR" in n or "SÉJOUR" in n or "SALON" in n:
        return "living"
    if "KITCHEN" in n or "CUISINE" in n:
        return "kitchen"
    if "BATH" in n or "S.D.B" in n or "SDB" in n or "BAIN" in n:
        return "bathroom"
    if "SHOWER" in n or "DOUCHE" in n:
        return "shower"
    if n.strip() == "WC" or "TOILET" in n:
        return "wc"
    if "CHANGING" in n or "DRESSING" in n or "CELLIER" in n:
        return "changing"
    return "other"


def _furnish(rooms_by_name, rects):
    for name, rc in rects.items():
        r = rooms_by_name.get(name)
        if r is None or _is_circ(name) or r.get("furniture"):
            continue
        x0, y0, x1, y1 = rc
        w, d = x1 - x0, y1 - y0
        door = None
        for o in r.get("doors", []):
            ax, ay, bx, by = _edges(rc)[o["wall"]]
            door = (ax + (bx - ax) * o["at"], ay + (by - ay) * o["at"])
        win_walls = {o["wall"] for o in r.get("windows", [])}
        door_walls = {o["wall"] for o in r.get("doors", [])}

        def clear_of_door(fr, m=0.75):
            if not door:
                return True
            dx = max(fr[0] - door[0], 0, door[0] - fr[2])
            dy = max(fr[1] - door[1], 0, door[1] - fr[3])
            return (dx * dx + dy * dy) ** 0.5 >= m

        def against(wall, fw, fd, off=0.05, at=0.5):
            """Rect of size fw (along wall) × fd (into room) against a wall."""
            if wall in ("N", "S"):
                cx = x0 + at * w
                fx0 = min(max(cx - fw / 2, x0 + 0.05), x1 - fw - 0.05)
                if wall == "N":
                    return [fx0, y0 + off, fx0 + fw, y0 + off + fd]
                return [fx0, y1 - off - fd, fx0 + fw, y1 - off]
            cy = y0 + at * d
            fy0 = min(max(cy - fd / 2, y0 + 0.05), y1 - fd - 0.05)
            if wall == "W":
                return [x0 + off, fy0, x0 + off + fw, fy0 + fd]
            return [x1 - off - fw, fy0, x1 - off, fy0 + fd]

        def quiet_walls():
            walls = ["N", "S", "W", "E"]
            return sorted(walls, key=lambda wl: (wl in door_walls) * 2
                          + (wl in win_walls))

        def place(kind, item, wall, fw, fd, h, at=0.5):
            # size along wall must fit
            span = w if wall in ("N", "S") else d
            if fw > span - 0.2:
                fw = span - 0.2
            if fw <= 0.2:
                return None
            fr = against(wall, fw, fd, at=at)
            if not clear_of_door(fr):
                return None
            fr = [round(v, 2) for v in fr]
            furn.append({"item": item, "kind": kind, "rect": fr, "h": h,
                         "wall": wall})
            return fr

        def place_any(kind, item, fw, fd, h, walls=None):
            for wl in (walls or quiet_walls()):
                if place(kind, item, wl, fw, fd, h):
                    return True
            return False

        furn = []
        kind = _room_kind(name)
        if kind == "bedroom":
            bw = 1.6 if w * d >= 14 else 1.4
            for wl in quiet_walls():
                fr = place("bed", "lit double", wl, bw, 2.05, 0.55)
                if fr:
                    # nightstands flanking the bed head
                    if wl in ("N", "S"):
                        for sx in (fr[0] - 0.55, fr[2] + 0.05):
                            if x0 + 0.05 <= sx and sx + 0.5 <= x1 - 0.05:
                                yb = fr[1] if wl == "N" else fr[3] - 0.5
                                furn.append({"item": "chevet", "kind": "nightstand",
                                             "rect": [round(sx, 2), round(yb, 2),
                                                      round(sx + 0.5, 2), round(yb + 0.5, 2)],
                                             "h": 0.5, "wall": wl})
                    else:
                        for sy in (fr[1] - 0.55, fr[3] + 0.05):
                            if y0 + 0.05 <= sy and sy + 0.5 <= y1 - 0.05:
                                xb = fr[0] if wl == "W" else fr[2] - 0.5
                                furn.append({"item": "chevet", "kind": "nightstand",
                                             "rect": [round(xb, 2), round(sy, 2),
                                                      round(xb + 0.5, 2), round(sy + 0.5, 2)],
                                             "h": 0.5, "wall": wl})
                    break
            place_any("wardrobe", "armoire", 2.2, 0.65, 2.1)
        elif kind == "living":
            main_win = next(iter(win_walls), "N")
            opp = {"N": "S", "S": "N", "W": "E", "E": "W"}[main_win]
            fr = place("sofa", "canapé", opp, 2.3, 0.95, 0.75)
            if fr:
                # coffee table in front of the sofa (toward the window)
                cx, cy = (fr[0] + fr[2]) / 2, (fr[1] + fr[3]) / 2
                dx, dy = {"N": (0, 1), "S": (0, -1), "W": (1, 0), "E": (-1, 0)}[opp]
                furn.append({"item": "table basse", "kind": "coffee",
                             "rect": [round(cx - 0.55 + dx * 1.3, 2), round(cy - 0.35 + dy * 1.3, 2),
                                      round(cx + 0.55 + dx * 1.3, 2), round(cy + 0.35 + dy * 1.3, 2)],
                             "h": 0.35})
            place("tv", "meuble TV", main_win, 1.8, 0.45, 0.5, at=0.3)
            # dining set toward the secondary window / far third
            at2 = 0.8 if w >= d else 0.5
            place_any("dining", "table à manger", 1.7, 1.0, 0.75,
                      walls=[wl for wl in quiet_walls() if wl != opp])
        elif kind == "kitchen":
            place_any("counter", "plan de travail", max(w, d) - 0.3, 0.62, 0.9)
            place_any("fridge", "réfrigérateur", 0.65, 0.68, 1.85,
                      walls=[x for x in quiet_walls()[1:]])
        elif kind == "bathroom":
            place_any("bathtub", "baignoire", 1.7, 0.78, 0.58)
            place_any("vanity", "vasque", 1.1, 0.5, 0.85)
            if w * d >= 5:
                place_any("toilet", "wc", 0.4, 0.66, 0.78)
        elif kind == "shower":
            walls = [wl for wl in quiet_walls()]
            place_any("shower", "douche", 0.95, 0.95, 0.15, walls=walls)
            place_any("vanity", "lave-mains", 0.7, 0.42, 0.85)
        elif kind == "wc":
            place_any("toilet", "wc", 0.42, 0.68, 0.78)
            place_any("vanity", "lave-mains", 0.5, 0.32, 0.85)
        elif kind == "changing":
            qws = quiet_walls()
            place("wardrobe", "penderie", qws[0], max(w, d) - 0.3, 0.6, 2.2)
            if len(qws) > 1:
                place("wardrobe", "penderie", qws[1], max(w, d) - 0.9, 0.6, 2.2)
        r["furniture"] = furn


def build_plan(facts, project="plan"):
    xs, ys = _wall_grid(facts.get("chains", {}))
    # envelope: computed from chains when available (never trust the model's sum)
    if len(xs) > 1 and len(ys) > 1:
        EW, ED = max(xs), max(ys)
    elif facts["envelope"]:
        EW, ED = facts["envelope"]
    else:
        sys.exit("no ENVELOPE and no CHAIN lines — cannot scale the plan")

    # a side value that appears verbatim in a printed chain is corroborated;
    # an uncorroborated side that contradicts the printed area was misread by
    # the vision model → drop it and let the solver recompute it (area ÷ side)
    chain_vals = [v for c in facts.get("chains", {}).values() for v in c]

    def _corrob(v):
        return any(abs(v - cv) <= 0.03 for cv in chain_vals)

    solved = []
    for r in facts["rooms"]:
        w, d, area = r["sides"].get("w"), r["sides"].get("d"), r["area"]
        if chain_vals and w and d and area and \
                abs(w * d - area) > max(0.2, 0.04 * area):
            if _corrob(w) and not _corrob(d):
                d = None
            elif _corrob(d) and not _corrob(w):
                w = None
        f = {"name": r["name"], "area_m2": area}
        if w:
            f["width_m"] = w
        if d:
            f["depth_m"] = d
        dims, warns = solve_room(f)
        solved.append((r, dims, warns))

    # circulation rooms (hall, corridors) are NOT tiled: a hall is rarely a
    # rectangle — it IS the leftover space between the rooms
    tileable = [(r, d, w) for r, d, w in solved if not _is_circ(r["name"])]
    exact = [(r, d, w) for r, d, w in tileable if d["w_m"] and d["d_m"]]
    areaonly = [(r, d, w) for r, d, w in tileable
                if not (d["w_m"] and d["d_m"]) and r["area"]]
    lxs = [r["label"][0] for r, _, _ in solved if r.get("label")] or [0, 1]
    lys = [r["label"][1] for r, _, _ in solved if r.get("label")] or [0, 1]
    lrange = (lxs, lys)

    # phase 1 — exact-dims rooms on the printed chain grid (try both bbox
    # orientations; labels are never flipped)
    ta = _tile(exact, xs, ys, EW, ED, flip=False, lrange=lrange)
    tb = _tile(exact, xs, ys, EW, ED, flip=True, lrange=lrange)
    pick = tb if (tb["n"], -tb["dev"]) > (ta["n"], -ta["dev"]) else ta
    flip = pick is tb
    if flip:
        print("· bbox orientation: y-flip detected")
    rects = {k: [round(v, 2) for v in c] for k, c in pick["asg"].items()}

    # phase 2 — area-only rooms: spans = real chain bays, anchors = wall grid
    # + the partitions of the rooms already placed
    spans = _chain_spans(facts.get("chains", {}))
    ax = sorted({*xs, *[v for rc in rects.values() for v in (rc[0], rc[2])]})
    ay = sorted({*ys, *[v for rc in rects.values() for v in (rc[1], rc[3])]})
    t2 = _tile(areaonly, xs, ys, EW, ED, flip=flip,
               fixed=list(rects.values()), spans=spans, anchors=(ax, ay),
               lrange=lrange)
    for k, c in t2["asg"].items():
        rects[k] = [round(v, 2) for v in c]
    print(f"· tiling: {len(rects)} rooms locked "
          f"({pick['n']} exact + {t2['n']} by area/bay)")

    rooms_by_name = {r["name"]: r for r, _, _ in solved}

    # ── circulation = the leftover cells, flood-filled from the hall label ──
    def _lblpt(r):
        return (_norm_static(r["label"][0], min(lxs), max(lxs)) * EW,
                _norm_static(r["label"][1], min(lys), max(lys)) * ED)

    seeds = [_lblpt(r) for r, _, _ in solved
             if _is_circ(r["name"]) and r.get("label")] or [(EW / 2, ED / 2)]

    def classify(rects_in):
        """PURE geometry pass: leftover cells → (rects', circ, room-gaps,
        logs). Replayable, so the reachability loop can test configurations."""
        r2 = {k: list(v) for k, v in rects_in.items()}
        raw = _gap_fill(r2, EW, ED)
        # wall-thickness residue (<18 cm) is never walkable — keep it out of
        # the circulation flood, it only gets absorbed or dropped
        pool = [g for g in raw if min(g[2] - g[0], g[3] - g[1]) >= 0.12]
        arts = [g for g in raw if min(g[2] - g[0], g[3] - g[1]) < 0.12]
        cc = []
        for g in pool[:]:
            if any(g[0] - 0.6 <= sx <= g[2] + 0.6 and
                   g[1] - 0.6 <= sy <= g[3] + 0.6 for sx, sy in seeds):
                cc.append(g); pool.remove(g)
        grow = True
        while grow:
            grow = False
            for g in pool[:]:
                if any(_touches(g, c, 0.25) for c in cc):
                    cc.append(g); pool.remove(g); grow = True
        pool += arts
        logs, gaps_rooms = [], []
        for g in pool:
            w, d = g[2] - g[0], g[3] - g[1]
            roomlike = (w >= 0.6 and d >= 0.6 and w * d >= 1.0
                        and max(w, d) / min(w, d) <= 3.2)
            if roomlike:
                gaps_rooms.append(g)
                continue
            artifact = min(w, d) < 0.12   # wall-thickness residue, NOT walkable
            absorbed = False
            for nm2, rc2 in r2.items():
                same_x = abs(g[0] - rc2[0]) < 0.05 and abs(g[2] - rc2[2]) < 0.05
                same_y = abs(g[1] - rc2[1]) < 0.05 and abs(g[3] - rc2[3]) < 0.05
                if same_x and (abs(g[1] - rc2[3]) < 0.05 or abs(g[3] - rc2[1]) < 0.05):
                    rc2[1], rc2[3] = min(rc2[1], g[1]), max(rc2[3], g[3])
                elif same_y and (abs(g[0] - rc2[2]) < 0.05 or abs(g[2] - rc2[0]) < 0.05):
                    rc2[0], rc2[2] = min(rc2[0], g[0]), max(rc2[2], g[2])
                else:
                    continue
                logs.append(f"· sliver {g} absorbé par {nm2}")
                absorbed = True
                break
            if not absorbed and not artifact:
                cc.append(g)              # orphan sliver → walkable
        return r2, cc, gaps_rooms, logs

    def _max_contact(rc, cc):
        best = 0.0
        for wall, seg in _edges(rc).items():
            ax, ay, bx, by = seg
            for cx0, cy0, cx1, cy1 in cc:
                if wall in ("N", "S") and abs(
                        (cy1 if wall == "N" else cy0) - ay) < 0.03:
                    best = max(best, min(bx, cx1) - max(ax, cx0))
                elif wall in ("W", "E") and abs(
                        (cx1 if wall == "W" else cx0) - ax) < 0.03:
                    best = max(best, min(by, cy1) - max(ay, cy0))
        return best

    def sealed_room(r2, cc, gaps_rooms):
        for nm2, rc2 in r2.items():
            if _max_contact(rc2, cc) < 0.3:
                return nm2
        for g in gaps_rooms:
            if _max_contact(g, cc) < 0.3:
                return f"gap{g}"
        return None

    # architectural axiom: every room reachable from the circulation. A sealed
    # room means an area-only room plugs a corridor throat → move the blocker.
    def _rectdist(a, b):
        dx = max(a[0] - b[2], 0, b[0] - a[2])
        dy = max(a[1] - b[3], 0, b[1] - a[3])
        return (dx * dx + dy * dy) ** 0.5

    cfg = classify(rects)
    bad = sealed_room(*cfg[:3])
    if bad and bad in rects:
        print(f"· ⚠ {bad} enclavée — repositionnement d'un bloqueur…")
        fixed_ok = False
        # blockers adjacent to the sealed room first — they plug its throat
        order = sorted((it for it in areaonly if it[0]["name"] in rects),
                       key=lambda it: _rectdist(rects[it[0]["name"]],
                                                rects[bad]))
        for r, dims, _ in order:
            nm2 = r["name"]
            if fixed_ok:
                break
            others = {k: v for k, v in rects.items() if k != nm2}
            lpt = _lblpt(r) if r.get("label") else None
            oc = rects[nm2]
            odist = (((oc[0] + oc[2]) / 2 - lpt[0]) ** 2 +
                     ((oc[1] + oc[3]) / 2 - lpt[1]) ** 2) ** 0.5 if lpt else 0
            for cand in _candidates(r, dims, xs, ys, EW, ED, spans, (ax, ay)):
                if any(_overlap(cand, p) > 0.05 for p in others.values()):
                    continue
                if lpt:  # a fix must not exile the room from its printed name
                    nd = (((cand[0] + cand[2]) / 2 - lpt[0]) ** 2 +
                          ((cand[1] + cand[3]) / 2 - lpt[1]) ** 2) ** 0.5
                    if nd > odist + 1.0:
                        continue
                trial = dict(others)
                trial[nm2] = [round(v, 2) for v in cand]
                tcfg = classify(trial)
                if sealed_room(*tcfg[:3]) is None:
                    rects, cfg, fixed_ok = trial, tcfg, True
                    print(f"·   {nm2} déplacée → {trial[nm2]}")
                    break
        if not fixed_ok:
            print("·   aucun repositionnement ne débloque — configuration gardée")

    rects, circ, gaps_rooms, clogs = cfg
    for ln in clogs:
        print(ln)

    # room-sized gaps: adopt a label-only room (e.g. the WC), else PIECE ?
    unplaced_lbl = {r["name"]: _lblpt(r) for r, _, _ in solved
                    if r["name"] not in rects and r.get("label")
                    and not _is_circ(r["name"])}
    gi = 0
    for g in gaps_rooms:
        w, d = g[2] - g[0], g[3] - g[1]
        area = round(w * d, 2)
        nm = None
        for cand, (px, py) in list(unplaced_lbl.items()):
            dx = max(g[0] - px, 0, px - g[2])
            dy = max(g[1] - py, 0, py - g[3])
            if (dx * dx + dy * dy) ** 0.5 <= 1.5:
                nm = cand
                del unplaced_lbl[cand]
                break
        if nm:
            for i, (r, dims, warns) in enumerate(solved):
                if r["name"] == nm:
                    solved[i] = (r, {"w_m": round(w, 2), "d_m": round(d, 2),
                                     "area_m2": area, "ceiling_m": None},
                                 ["placed from geometry gap (vision gave "
                                  "name/label only)"])
                    break
        else:
            gi += 1
            nm = f"PIECE ? {gi}"
            rooms_by_name[nm] = {"name": nm, "area": area, "sides": {},
                                 "doors": [], "windows": [], "bbox": None}
            solved.append((rooms_by_name[nm],
                           {"w_m": round(w, 2), "d_m": round(d, 2),
                            "area_m2": area, "ceiling_m": None},
                           ["gap not covered by any read room — check the plan"]))
        rects[nm] = g
        print(f"· gap → {nm}: {g} ({area} m²)")

    circ_cells = [[round(v, 2) for v in c] for c in circ]
    hall_pt = (EW / 2, ED / 2)
    if circ_cells:
        big = max(circ_cells, key=lambda c: (c[2] - c[0]) * (c[3] - c[1]))
        hall_pt = ((big[0] + big[2]) / 2, (big[1] + big[3]) / 2)
    # circulation rooms keep a label position on the biggest cell
    for r, _, _ in solved:
        if _is_circ(r["name"]):
            r["label_at"] = [round(hall_pt[0], 2), round(hall_pt[1], 2)]

    # deterministic openings (doors toward the circulation, windows on hull)
    _fix_openings(rooms_by_name, rects, EW, ED, hall_pt, circ_cells)
    print(f"· openings: doors re-aimed at the circulation, "
          f"windows filtered to the hull ({len(circ_cells)} circ cells)")

    # deterministic furnishing per room type (data → editable in plan.json)
    _furnish(rooms_by_name, rects)
    nfurn = sum(len(r.get("furniture", [])) for r in rooms_by_name.values())
    print(f"· furnishing: {nfurn} pieces placed on quiet walls")

    rooms_out = []
    for r, dims, warns in solved:
        entry = {"name": r["name"], "area_m2": r["area"]}
        if r["name"] in rects:
            entry["rect"] = rects[r["name"]]
            entry["doors"] = r["doors"]
            entry["windows"] = r["windows"]
            entry["furniture"] = r.get("furniture", [])
        else:  # circulation / unresolved → label only
            bb = r["bbox"]
            entry["label_at"] = r.get("label_at") or (
                [round((bb[0] + bb[2]) / 2 * EW, 2), round((bb[1] + bb[3]) / 2 * ED, 2)]
                if bb else [round(EW / 2, 2), round(ED / 2, 2)])
        if warns:
            entry["_warn"] = warns
        rooms_out.append(entry)

    return {"project": project, "units": "m", "wall_height": 2.5,
            "circulation": circ_cells,
            "envelope": [EW, ED], "rooms": rooms_out}


# ── vision call (reuses the repo's CF plumbing: free tier + quota guard) ────
def ask_vision(image_path, prompt=VISION_PROMPT):
    import base64
    import urllib.request
    from plan_to_image import _cf_creds, _quota_check_and_increment
    acct, tok = _cf_creds()
    if not acct or not tok:
        sys.exit("CF creds missing — run `wrangler login` once.")
    if not os.environ.get("P2I_BYPASS_QUOTA"):
        _quota_check_and_increment()
    img = base64.b64encode(open(image_path, "rb").read()).decode()
    payload = {"messages": [{"role": "user", "content": [
        {"type": "text", "text": prompt},
        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{img}"}},
    ]}], "max_tokens": 2500}
    url = (f"https://api.cloudflare.com/client/v4/accounts/{acct}"
           "/ai/run/@cf/mistralai/mistral-small-3.1-24b-instruct")
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={"Authorization": f"Bearer {tok}",
                                          "Content-Type": "application/json"},
                                 method="POST")
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = json.loads(resp.read())
    if not data.get("success"):
        raise RuntimeError(f"CF call failed: {data}")
    res = data["result"]
    return res["choices"][0]["message"]["content"] if "choices" in res \
        else res.get("response", str(res))


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    pr = sub.add_parser("read", help="plan image (or saved lines) -> plan.json")
    pr.add_argument("image", nargs="?", help="plan raster (png/jpg)")
    pr.add_argument("--from-lines", help="parse a saved lines file instead of "
                                         "calling vision (offline test)")
    pr.add_argument("-o", "--out", default=None)
    pr.add_argument("--dry", action="store_true", help="print facts, don't write")
    pr.add_argument("--project", default=None)
    pr.add_argument("--passes", type=int, default=2,
                    help="vision calls to merge (the model drops facts "
                         "randomly; 2-3 passes are far more complete). "
                         "Each pass = 1 CF quota call.")
    args = ap.parse_args()

    if args.from_lines:
        text = open(args.from_lines, encoding="utf-8").read()
        src = args.from_lines
    elif args.image:
        texts = []
        for i in range(max(1, args.passes)):
            print(f"· vision pass {i + 1}/{args.passes} …")
            texts.append(ask_vision(args.image))
        print("· labels pass (room-name positions) …")
        texts.append(ask_vision(args.image, LABELS_PROMPT))
        # completeness pass: give the model the room list, ask what's missing
        found, _ = parse_lines("\n".join(texts))
        names = [r["name"] for r in found["rooms"]]
        print("· completeness pass (missed small rooms?) …")
        texts.append(ask_vision(args.image, completeness_prompt(names)))
        text = "\n".join(texts)
        src = args.image
        # keep the raw lines next to the output for audit/repair
        raw = (args.out or args.image) + ".lines.txt"
        open(raw, "w", encoding="utf-8").write(text)
        print(f"· raw vision lines → {raw}")
    else:
        sys.exit("give a plan image or --from-lines file")

    facts, report = parse_lines(text)
    print(f"· parsed: {report['ok']} lines ok, {report['rejected']} rejected")
    for ln in report["rejected_lines"]:
        print(f"    ✗ {ln}")
    if args.dry:
        print(json.dumps(facts, ensure_ascii=False, indent=2)); return

    plan = build_plan(facts, args.project or
                      os.path.splitext(os.path.basename(src))[0])
    out = args.out or os.path.splitext(src)[0] + ".plan.json"
    json.dump(plan, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    n = sum(1 for r in plan["rooms"] if r.get("rect"))
    print(f"✓ plan.json → {out}  ({n} rooms placed, "
          f"{len(plan['rooms']) - n} label-only)")
    for r in plan["rooms"]:
        if r.get("rect"):
            x0, y0, x1, y1 = r["rect"]
            print(f"  • {r['name']:<16} [{x0:>5.2f},{y0:>5.2f} → {x1:>5.2f},{y1:>5.2f}] "
                  f"{x1-x0:.2f}×{y1-y0:.2f} m")
        else:
            print(f"  • {r['name']:<16} (label only)")
        for w in r.get("_warn", []):
            print(f"       ! {w}")


if __name__ == "__main__":
    main()

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

_NUM = r"(\d+(?:[.,]\d+)?)"
RE = {
    "envelope": re.compile(
        rf"^ENVELOPE:\s*(?:{_NUM}|-)\s*[x×]\s*(?:{_NUM}|-)\s*$", re.I),
    "room": re.compile(
        rf"^ROOM:\s*(?P<name>[^|]+?)\s*\|\s*area\s*(?:(?P<area>{_NUM})|-)\s*\|\s*bbox\s*"
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


def _near(v, grid, tol=0.15):
    """Grid line closest to v within tol, else None."""
    best = None
    for g in grid:
        if abs(g - v) <= tol and (best is None or abs(g - v) < abs(best - v)):
            best = g
    return best


def _candidates(r, dims, xs, ys, EW, ED):
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
        for i, x0 in enumerate(xs):                       # x-span driven
            for x1 in xs[i + 1:]:
                sp = x1 - x0
                if not 0.5 <= sp <= 9:
                    continue
                o = area / sp
                if not 0.7 <= o <= 9 or max(sp, o) / min(sp, o) > 3.2:
                    continue
                for y0 in ys:
                    if y0 + o <= ED + 0.1:
                        out.append((x0, y0, x1, y0 + o))
                for y1 in ys:
                    if y1 - o >= -0.1:
                        out.append((x0, y1 - o, x1, y1))
        for i, y0 in enumerate(ys):                       # y-span driven
            for y1 in ys[i + 1:]:
                sp = y1 - y0
                if not 0.5 <= sp <= 9:
                    continue
                o = area / sp
                if not 0.7 <= o <= 9 or max(sp, o) / min(sp, o) > 3.2:
                    continue
                for x0 in xs:
                    if x0 + o <= EW + 0.1:
                        out.append((x0, y0, x0 + o, y1))
                for x1 in xs:
                    if x1 - o >= -0.1:
                        out.append((x1 - o, y0, x1, y1))
    seen, uniq = set(), []
    for c in out:
        k = tuple(round(v, 2) for v in c)
        if k not in seen:
            seen.add(k)
            uniq.append(k)
    return uniq


def _tile(solved, xs, ys, EW, ED, flip):
    """Pick one candidate per room so that rooms never overlap, staying as
    close as possible to the vision bboxes (which only break ties — positions
    come from the printed grid). Exhaustive search with pruning; room counts
    are tiny. Returns {"n": placed, "dev": Σdist², "asg": {name: rect}}."""
    # normalise label coordinates: they are fractions of the FULL IMAGE
    # (margins + dimension bands included) → map their min..max spread onto
    # the building interior so the systematic offset cancels out
    lxs = [r["label"][0] for r, _, _ in solved if r.get("label")]
    lys = [r["label"][1] for r, _, _ in solved if r.get("label")]
    MARGIN = 0.12

    def _norm(v, vmin, vmax):
        if vmax - vmin < 0.05:
            return 0.5
        return MARGIN + (1 - 2 * MARGIN) * (v - vmin) / (vmax - vmin)

    items = []
    for r, dims, _ in solved:
        # area-only rooms need a position hint (label or bbox) to be placed;
        # without one, any grid position would fit → garbage. Label-only then.
        if not (dims["w_m"] and dims["d_m"]) \
                and not r["bbox"] and not r.get("label"):
            continue
        cands = _candidates(r, dims, xs, ys, EW, ED)
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
        if ctr:
            cands = sorted(cands, key=lambda c: ((c[0] + c[2]) / 2 - ctr[0]) ** 2
                           + ((c[1] + c[3]) / 2 - ctr[1]) ** 2)
        # keep the search tractable: exact-dims rooms have few candidates
        # anyway; area-only rooms are capped harder (hint-sorted, best first)
        cap = 40 if (dims["w_m"] and dims["d_m"]) else 12
        items.append((r["name"], cands[:cap], ctr))
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
        name, cands, ctr = items[i]
        for c in cands:
            if any(_overlap(c, p) > 0.05 for p in asg.values()):
                continue
            d = 0.0 if not ctr else \
                ((c[0] + c[2]) / 2 - ctr[0]) ** 2 + ((c[1] + c[3]) / 2 - ctr[1]) ** 2
            asg[name] = c
            bt(i + 1, asg, dev + d)
            del asg[name]
        bt(i + 1, asg, dev)                         # room may stay unplaced
    bt(0, {}, 0.0)
    return best


def build_plan(facts, project="plan"):
    xs, ys = _wall_grid(facts.get("chains", {}))
    # envelope: computed from chains when available (never trust the model's sum)
    if len(xs) > 1 and len(ys) > 1:
        EW, ED = max(xs), max(ys)
    elif facts["envelope"]:
        EW, ED = facts["envelope"]
    else:
        sys.exit("no ENVELOPE and no CHAIN lines — cannot scale the plan")

    solved = []
    for r in facts["rooms"]:
        f = {"name": r["name"], "area_m2": r["area"]}
        if "w" in r["sides"]:
            f["width_m"] = r["sides"]["w"]
        if "d" in r["sides"]:
            f["depth_m"] = r["sides"]["d"]
        dims, warns = solve_room(f)
        solved.append((r, dims, warns))

    # bbox y-orientation is unreliable (some models emit CAD y-up): tile both
    # ways; keep the one placing more rooms, then the one closer to the bboxes
    ta = _tile(solved, xs, ys, EW, ED, flip=False)
    tb = _tile(solved, xs, ys, EW, ED, flip=True)
    pick = tb if (tb["n"], -tb["dev"]) > (ta["n"], -ta["dev"]) else ta
    if pick is tb:
        print("· bbox orientation: y-flip detected")
    rects = {k: [round(v, 2) for v in c] for k, c in pick["asg"].items()}
    print(f"· tiling: {pick['n']} rooms locked on the printed wall grid")

    rooms_out = []
    for r, dims, warns in solved:
        entry = {"name": r["name"], "area_m2": r["area"]}
        if r["name"] in rects:
            entry["rect"] = rects[r["name"]]
            entry["doors"] = r["doors"]
            entry["windows"] = r["windows"]
            entry["furniture"] = []
        else:  # circulation / unresolved → label only, centred on bbox or plan
            bb = r["bbox"]
            entry["label_at"] = (
                [round((bb[0] + bb[2]) / 2 * EW, 2), round((bb[1] + bb[3]) / 2 * ED, 2)]
                if bb else [round(EW / 2, 2), round(ED / 2, 2)])
        if warns:
            entry["_warn"] = warns
        rooms_out.append(entry)

    return {"project": project, "units": "m", "wall_height": 2.5,
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

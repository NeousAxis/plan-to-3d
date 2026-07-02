#!/usr/bin/env python3
"""plan_extract.py — multi-agent plan reader for the plan-to-image skill.

Idea (Cyril): instead of ONE agent trying to read the whole plan as free-form
JSON (and getting the geometry wrong), split the job. Each *agent* is a single,
focused extraction pass — it pulls ONE kind of information out of the plan and
emits it in a STRICT, single-line format. A regex layer then parses each slice
deterministically and merges them into one geometry spec. No fragile free-form
JSON to mis-emit; every line either matches its regex or is flagged.

At skill runtime the "agents" are focused Claude passes over the plan image
(one per role, runnable in parallel as subagents). Each writes its slice to
`<dir>/slice_<role>.txt`. Then:

    python3 plan_extract.py merge <dir>            # parse + merge -> geometry.json
    python3 plan_extract.py spec <dir> --style japandi   # geometry -> render spec
    python3 plan_extract.py roles                  # print each agent's task + format

The split + regex is what makes it reliable: one simple job per agent, one
deterministic contract per agent.
"""

import argparse
import json
import os
import re
import sys


# ── Agent roles. Each = one focused extraction task with a strict line format
#    and the regex that parses it. `ask` is the instruction handed to the agent.
ROLES = {
    "rooms": {
        "ask": "List EVERY room / space label exactly as printed on the plan, "
               "one per line. Include service spaces (WC, DGT, cellier, porche).",
        "format": "ROOM: <label>",
        "re": re.compile(r"^ROOM:\s*(?P<name>.+?)\s*$"),
    },
    "dims": {
        "ask": "For each room give the PRINTED dimensions if shown (W x D in m) "
               "and ceiling height (HSP). One line per room. Use '?' when a value "
               "is not printed — never invent.",
        "format": "DIM: <room> | <WxD m or ?> | <area m2 or ?> | <ceiling m or ?>",
        "re": re.compile(r"^DIM:\s*(?P<room>[^|]+?)\s*\|\s*(?P<wd>[^|]+?)\s*\|"
                         r"\s*(?P<area>[^|]+?)\s*\|\s*(?P<ceil>[^|]+?)\s*$"),
    },
    "openings": {
        "ask": "For each room list each WINDOW and each DOOR and which wall it is "
               "on. Walls: N/S/E/W = top/bottom/right/left of the plan. One "
               "opening per line. A window symbol is a gap with a thin line across; "
               "a door is a gap with a quarter-circle swing arc.",
        "format": "OPEN: <room> | window|door | N|S|E|W",
        "re": re.compile(r"^OPEN:\s*(?P<room>[^|]+?)\s*\|\s*(?P<kind>window|door)"
                         r"\s*\|\s*(?P<wall>[NSEW])\s*$", re.I),
    },
    "fixtures": {
        "ask": "For each room list the furniture / equipment SYMBOLS drawn on the "
               "plan (bed, bath, WC, basin/sink, cooktop, fridge, washer 'ML', "
               "dryer 'SL', heat-pump 'PAC', wardrobe/placard) and roughly where "
               "(which wall or corner). One item per line.",
        "format": "FIX: <room> | <item> | <where>",
        "re": re.compile(r"^FIX:\s*(?P<room>[^|]+?)\s*\|\s*(?P<item>[^|]+?)\s*\|"
                         r"\s*(?P<where>.+?)\s*$"),
    },
    "shape": {
        "ask": "For each room state its footprint shape: 'rect' if rectangular, "
               "or 'irregular' plus a short note (e.g. 'angled wall on the E side "
               "shared with the salon').",
        "format": "SHAPE: <room> | rect|irregular | <note>",
        "re": re.compile(r"^SHAPE:\s*(?P<room>[^|]+?)\s*\|\s*(?P<shape>rect|"
                         r"irregular)\s*\|\s*(?P<note>.*?)\s*$", re.I),
    },
    "entry": {
        "ask": "For each room, which wall do you ENTER from (the door you walk "
               "through)? This sets the camera position. N/S/E/W.",
        "format": "ENTRY: <room> | N|S|E|W",
        "re": re.compile(r"^ENTRY:\s*(?P<room>[^|]+?)\s*\|\s*(?P<wall>[NSEW])\s*$",
                         re.I),
    },
    "scale": {
        "ask": "Give ONE known real dimension to calibrate scale: a bed marked "
               "'140x190', or any printed cote, with where it is.",
        "format": "SCALE: <what> = <value>",
        "re": re.compile(r"^SCALE:\s*(?P<what>[^=]+?)\s*=\s*(?P<val>.+?)\s*$"),
    },
}

# regex helpers used by the normaliser
_DIM_RE = re.compile(r"(\d+[.,]?\d*)\s*[x×*]\s*(\d+[.,]?\d*)")
_NUM_RE = re.compile(r"(\d+[.,]?\d*)")


def norm_room(s):
    return re.sub(r"\s+", " ", s.strip().upper())


def _num(s):
    m = _NUM_RE.search(s or "")
    return float(m.group(1).replace(",", ".")) if m else None


def parse_slice(role, text):
    """Parse one agent's raw output. Returns (rows, bad_lines)."""
    rx = ROLES[role]["re"]
    rows, bad = [], []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = rx.match(line)
        if m:
            rows.append(m.groupdict())
        else:                           # strict: any non-matching line is flagged
            bad.append(line)
    return rows, bad


def merge(slices):
    """Merge parsed slices into a geometry spec keyed by room."""
    rooms = {}

    def R(name):
        k = norm_room(name)
        rooms.setdefault(k, {"name": name.strip(), "dims": {}, "openings": [],
                             "fixtures": [], "shape": "rect", "shape_note": "",
                             "entry": None})
        return rooms[k]

    for r in slices.get("rooms", []):
        R(r["name"])
    for r in slices.get("dims", []):
        d = R(r["room"])["dims"]
        wd = _DIM_RE.search(r["wd"])
        d["w_m"] = float(wd.group(1).replace(",", ".")) if wd else None
        d["d_m"] = float(wd.group(2).replace(",", ".")) if wd else None
        d["area_m2"] = _num(r["area"])
        d["ceiling_m"] = _num(r["ceil"])
    for r in slices.get("openings", []):
        R(r["room"])["openings"].append({"kind": r["kind"].lower(),
                                         "wall": r["wall"].upper()})
    for r in slices.get("fixtures", []):
        R(r["room"])["fixtures"].append({"item": r["item"].strip(),
                                         "where": r["where"].strip()})
    for r in slices.get("shape", []):
        rm = R(r["room"]); rm["shape"] = r["shape"].lower(); rm["shape_note"] = r["note"].strip()
    for r in slices.get("entry", []):
        R(r["room"])["entry"] = r["wall"].upper()

    scale = {}
    for r in slices.get("scale", []):
        scale[r["what"].strip()] = r["val"].strip()
    return {"rooms": list(rooms.values()), "scale": scale}


def load_and_merge(directory):
    slices, report = {}, {}
    for role in ROLES:
        path = os.path.join(directory, f"slice_{role}.txt")
        if not os.path.exists(path):
            report[role] = {"rows": 0, "bad": [], "missing": True}
            continue
        rows, bad = parse_slice(role, open(path, encoding="utf-8").read())
        slices[role] = rows
        report[role] = {"rows": len(rows), "bad": bad, "missing": False}
    return merge(slices), report


# ── geometry -> render spec (feeds plan_to_image.py, esp. --mode geometric) ──
_WALL_FR = {"N": "north", "S": "south", "E": "east", "W": "west"}


def to_render_spec(geometry, style="japandi"):
    rooms = []
    for rm in geometry["rooms"]:
        d = rm["dims"]
        if d.get("w_m") and d.get("d_m"):
            dim = f"{rm['shape']}, {d['w_m']:.2f} × {d['d_m']:.2f} m"
            if d.get("area_m2"):
                dim += f", {d['area_m2']:.1f} m²"
        else:
            dim = rm["shape"]
        if d.get("ceiling_m"):
            dim += f", {d['ceiling_m']:.2f} m ceiling"
        if rm["shape"] == "irregular" and rm["shape_note"]:
            dim += f" (irregular: {rm['shape_note']})"

        wins = [f"{_WALL_FR[o['wall']]} wall" for o in rm["openings"] if o["kind"] == "window"]
        drs = [f"{_WALL_FR[o['wall']]} wall" for o in rm["openings"] if o["kind"] == "door"]
        layout = []
        if wins:
            layout.append("window on the " + ", ".join(wins))
        else:
            layout.append("NO window (interior room)")
        if drs:
            layout.append("door on the " + ", ".join(drs))

        placement = "; ".join(f"{f['item']} ({f['where']})" for f in rm["fixtures"])
        entry = _WALL_FR.get(rm["entry"], "") if rm["entry"] else ""
        viewpoint = (f"standing at the {entry} door looking in, eye-level ~1.6 m, "
                     "straight verticals") if entry else \
                    "wide-angle 24mm corner shot, eye-level ~1.6 m"
        rooms.append({
            "name": rm["name"], "dimensions": dim, "layout": ", ".join(layout),
            "placement": placement, "viewpoint": viewpoint, "furniture": placement,
            # structured geometry consumed by plan_to_image build_control_from_geom
            "_geom": {"openings": rm["openings"], "fixtures": rm["fixtures"],
                      "entry": rm["entry"], "shape": rm["shape"]},
        })
    return {"project": "Plan", "_source": "plan_extract.py multi-agent extraction",
            "global": {"style": style, "atmosphere": "warm"}, "rooms": rooms}


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("roles")
    m = sub.add_parser("merge"); m.add_argument("dir")
    s = sub.add_parser("spec"); s.add_argument("dir"); s.add_argument("--style", default="japandi")
    args = ap.parse_args()

    if args.cmd == "roles":
        for role, spec in ROLES.items():
            print(f"\n### agent: {role}")
            print(f"  task   : {spec['ask']}")
            print(f"  format : {spec['format']}")
        return

    geometry, report = load_and_merge(args.dir)
    print("── extraction report ──")
    for role, r in report.items():
        flag = "MISSING" if r["missing"] else f"{r['rows']} rows"
        print(f"  {role:9s}: {flag}" + (f"  ⚠ {len(r['bad'])} unparsed" if r["bad"] else ""))
        for b in r["bad"]:
            print(f"       ✗ {b}")

    if args.cmd == "merge":
        out = os.path.join(args.dir, "geometry.json")
        json.dump(geometry, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        print(f"\n✓ geometry → {out}  ({len(geometry['rooms'])} rooms)")
    elif args.cmd == "spec":
        spec = to_render_spec(geometry, args.style)
        out = os.path.join(args.dir, "spec.json")
        json.dump(spec, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        print(f"\n✓ render spec → {out}  (feed: python3 plan_to_image.py {out} --mode geometric)")


if __name__ == "__main__":
    main()

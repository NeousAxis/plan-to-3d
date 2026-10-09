#!/usr/bin/env python3
"""plan_raster.py — turn a *labelled* raster floor plan into an EXACT
geometry.json, with zero heavy dependencies (no OpenCV, no local OCR binary),
so the skill stays free + distributable + Google-free.

The key insight for coted plans (like the ones the user actually has):
the SURFACE AREA is printed on every room, and at least one side is given by a
dimension chain. So the missing side is not guessed — it is *computed*:

        other_side = printed_area / known_side          (exact)

Two decoupled layers
--------------------
1. READ  — extract per-room facts (name, area_m², one known side, openings) from
   the image. This is the easy part: a free vision LLM (the repo's Cloudflare
   Mistral-24B vision, no card / no Google / no local install) or a human fills a
   small `facts.json`. This file does NOT do the OCR itself — it stays
   dependency-free — it defines the schema and consumes the facts.
2. SOLVE — deterministic arithmetic (this file): facts.json → geometry.json,
   with an area cross-check printed for every room so nothing is taken on faith.

facts.json schema (per room):
    {
      "name": "KITCHEN",
      "area_m2": 8.17,               # printed on the plan (authoritative)
      "known": {"axis": "w", "m": 3.05},   # one side read from a dimension chain
      "depth_m": 2.68,               # OPTIONAL: second side if the plan gives both
      "shape": "rect",               # optional, default rect
      "openings": [{"kind":"window","wall":"E"}, {"kind":"door","wall":"N"}],
      "fixtures": [{"item":"sink","where":"S wall"}],
      "entry": "N"                   # optional, else inferred from first door
    }
plus a top-level "envelope" and "scale" echoed into geometry.json.

Usage:
    python3 plan_raster.py solve facts.json                 # -> facts.geometry.json
    python3 plan_raster.py solve facts.json -o geometry.json --check
"""
import argparse
import json
import os
import sys


def _round(x, n=2):
    return None if x is None else round(x, n)


def solve_room(f):
    """Return (dims, warnings) for one facts room."""
    warn = []
    area = f.get("area_m2")
    known = f.get("known") or {}
    kaxis = known.get("axis")          # "w" or "d"
    kside = known.get("m")
    depth_given = f.get("depth_m")
    width_given = f.get("width_m")

    w = d = None
    if width_given and depth_given:
        w, d = width_given, depth_given
    elif kside and area:
        if kaxis == "w":
            w = kside
            d = depth_given or (area / kside)
        elif kaxis == "d":
            d = kside
            w = width_given or (area / kside)
        else:
            warn.append("known.axis must be 'w' or 'd'")
    elif width_given and area:
        w, d = width_given, area / width_given
    elif depth_given and area:
        d, w = depth_given, area / depth_given
    else:
        warn.append("insufficient data: need area + one side, or both sides")

    dims = {"w_m": _round(w), "d_m": _round(d),
            "area_m2": _round(area), "ceiling_m": f.get("ceiling_m")}
    return dims, warn


def solve(facts):
    rooms_out = []
    checks = []
    for f in facts.get("rooms", []):
        dims, warn = solve_room(f)
        # area cross-check: recompute w*d vs printed area
        recomputed = None
        if dims["w_m"] and dims["d_m"]:
            recomputed = round(dims["w_m"] * dims["d_m"], 2)
        checks.append({"name": f.get("name"), "printed": f.get("area_m2"),
                       "recomputed": recomputed, "warn": warn})
        rooms_out.append({
            "name": f.get("name"),
            "dims": dims,
            "openings": f.get("openings", []),
            "fixtures": f.get("fixtures", []),
            "shape": f.get("shape", "rect"),
            "shape_note": f.get("shape_note", ""),
            "entry": f.get("entry") or _first_door_wall(f.get("openings", [])),
        })
    geo = {
        "rooms": rooms_out,
        "scale": facts.get("scale", {}),
        "_provenance": {
            "method": "labelled raster → deterministic solve (area ÷ known side)",
            "envelope": facts.get("envelope", {}),
        },
    }
    return geo, checks


def _first_door_wall(openings):
    for o in openings:
        if o.get("kind") == "door":
            return o.get("wall")
    return None


def print_checks(checks):
    print("\nArea cross-check (recomputed w×d vs printed):")
    ok = True
    for c in checks:
        p, r = c["printed"], c["recomputed"]
        if r is None:
            flag = "· (one side only — area kept as printed)"
        elif p and abs(r - p) <= max(0.4, 0.03 * p):
            flag = "OK"
        else:
            flag = f"⚠ MISMATCH (Δ={round(abs(r-p),2)})"
            ok = False
        pr = f"{p} m²" if p is not None else "—"
        rr = f"{r} m²" if r is not None else "—"
        print(f"  {c['name']:<16} printed {pr:<9} recomputed {rr:<9} {flag}")
        for w in c["warn"]:
            print(f"       ! {w}")
    print("  → all consistent." if ok else "  → review the ⚠ rows above.")
    return ok


def main():
    ap = argparse.ArgumentParser(
        description="Solve a labelled raster plan's facts into exact geometry.json.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    ps = sub.add_parser("solve", help="facts.json -> geometry.json")
    ps.add_argument("facts")
    ps.add_argument("-o", "--out", default=None)
    ps.add_argument("--check", action="store_true", help="print area cross-check")
    args = ap.parse_args()

    if args.cmd == "solve":
        facts = json.load(open(args.facts, encoding="utf-8"))
        geo, checks = solve(facts)
        out = args.out or os.path.splitext(args.facts)[0] + ".geometry.json"
        json.dump(geo, open(out, "w", encoding="utf-8"),
                  ensure_ascii=False, indent=2)
        print(f"✓ geometry → {out}  ({len(geo['rooms'])} rooms)")
        if args.check:
            print_checks(checks)


if __name__ == "__main__":
    main()

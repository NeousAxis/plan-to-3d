#!/usr/bin/env python3
"""Photoreal still renderer — author-side ("stage 5B+"). Uses Blender's
Cycles path tracer to render a single GI image of the building, then a
.png file lands next to the viewer that everyone can open. The viewer
itself stays light — the heavy lifting is done once, here.

Usage:
    python3 bake.py examples/lobby.json [--out work/lobby] [--samples 256]

Requires Blender (`brew install --cask blender` on macOS). Reads the same
JSON spec as generate.py, so you can iterate on building.json and rerun.
"""

import argparse
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def find_blender():
    for cand in ("/Applications/Blender.app/Contents/MacOS/Blender",
                 shutil.which("blender")):
        if cand and os.path.exists(cand):
            return cand
    sys.exit("error: Blender not found. Install it: "
             "brew install --cask blender")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("spec", help="building spec JSON")
    ap.add_argument("--out", default=None,
                    help="output directory (default work/<name>)")
    ap.add_argument("--samples", type=int, default=128,
                    help="Cycles samples per pixel (128 = preview, "
                         "256 = clean, 512 = museum)")
    ap.add_argument("--res", default="1920x1080")
    ap.add_argument("--cam", default=None,
                    help="camera 'ex,ey,ez,tx,ty,tz' (Blender world metres)")
    ap.add_argument("--exposure", type=float, default=0.4)
    ap.add_argument("--keep-roof", action="store_true",
                    help="render with the roof on (default: stripped so the "
                         "interior is visible)")
    args = ap.parse_args()

    spec_abs = os.path.abspath(args.spec)
    name = os.path.splitext(os.path.basename(args.spec))[0]
    out_dir = os.path.abspath(args.out or os.path.join("work", name))
    os.makedirs(out_dir, exist_ok=True)

    # 1) Build the GLB via generate.py (skip roof so we can see inside).
    glb_path = os.path.join(out_dir, "bake.glb")
    if not args.keep_roof:
        # patch the spec on the fly: drop the roof
        import json
        spec = json.load(open(spec_abs, 'r', encoding='utf-8'))
        spec.setdefault("roof", {})["type"] = "none"
        patched = os.path.join(out_dir, "spec_baked.json")
        json.dump(spec, open(patched, 'w', encoding='utf-8'), ensure_ascii=False)
        gen_spec = patched
    else:
        gen_spec = spec_abs

    cmd = [sys.executable, os.path.join(HERE, "generate.py"),
           gen_spec, "--out", out_dir, "--name", name]
    print("[bake] " + " ".join(cmd))
    subprocess.check_call(cmd)
    # generate.py writes model.glb; move/rename for the bake
    shutil.copy(os.path.join(out_dir, "model.glb"), glb_path)

    # 2) Hand off to Blender headless.
    blender = find_blender()
    out_png = os.path.join(out_dir, "render.png")
    blcmd = [blender, "-b", "-P", os.path.join(HERE, "bake_blender.py"),
             "--",
             "--glb", glb_path,
             "--spec", spec_abs,
             "--out", out_png,
             "--samples", str(args.samples),
             "--res", args.res,
             "--exposure", str(args.exposure)]
    if args.cam:
        blcmd += ["--cam", args.cam]
    print("[bake] " + " ".join(blcmd))
    subprocess.check_call(blcmd)
    print(f"\n✓ Photoreal render: {out_png}")


if __name__ == "__main__":
    main()

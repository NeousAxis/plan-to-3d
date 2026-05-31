#!/usr/bin/env python3
"""Bake GI into a 2nd-UV lightmap and embed it in the GLB.

After this, the live viewer's furniture / walls / floor carry the pre-baked
illumination as an emission overlay. The viewer renders in real time on any
hardware, but the image shows true GI bounces — closest we can get to a
photo-quality interactive scene without running a path tracer in-browser.

Usage:
    python3 bake_lightmap.py examples/lobby.json [--out work/lobby_lmap]
                                                 [--resolution 1024]
                                                 [--samples 64]

Requires Blender (`brew install --cask blender`).
"""

import argparse
import json
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
    sys.exit("error: Blender not found. Install: brew install --cask blender")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("spec")
    ap.add_argument("--out", default=None)
    ap.add_argument("--resolution", type=int, default=1024)
    ap.add_argument("--samples", type=int, default=64)
    ap.add_argument("--keep-roof", action="store_true")
    args = ap.parse_args()

    spec_abs = os.path.abspath(args.spec)
    name = os.path.splitext(os.path.basename(args.spec))[0]
    out_dir = os.path.abspath(args.out or os.path.join("work", name + "_lmap"))
    os.makedirs(out_dir, exist_ok=True)

    # 1) Build the base GLB (drop the roof unless asked for it)
    if not args.keep_roof:
        spec = json.load(open(spec_abs, 'r', encoding='utf-8'))
        spec.setdefault("roof", {})["type"] = "none"
        patched = os.path.join(out_dir, "spec_lmap.json")
        json.dump(spec, open(patched, 'w', encoding='utf-8'), ensure_ascii=False)
        gen_spec = patched
    else:
        gen_spec = spec_abs

    subprocess.check_call([sys.executable, os.path.join(HERE, "generate.py"),
                           gen_spec, "--out", out_dir, "--name", name])
    base_glb = os.path.join(out_dir, "model.glb")

    # 2) Bake lightmap and embed
    out_glb = os.path.join(out_dir, "model_lit.glb")
    blender = find_blender()
    cmd = [blender, "-b", "-P",
           os.path.join(HERE, "bake_lightmap_blender.py"),
           "--",
           "--glb", base_glb,
           "--spec", spec_abs,
           "--out", out_glb,
           "--resolution", str(args.resolution),
           "--samples", str(args.samples)]
    print("[bake_lmap] " + " ".join(cmd))
    subprocess.check_call(cmd)

    # 3) Build a viewer that uses the lit GLB
    viewer = os.path.join(out_dir, "viewer_lit.html")
    print(f"[bake_lmap] writing {viewer}")
    # Reuse the regular viewer template, but swap the GLB
    from generate import write_viewer, collect_people, resolve_art_palette
    spec_obj = json.load(open(spec_abs, 'r', encoding='utf-8'))
    art_hue, art_frame = resolve_art_palette(spec_obj)
    with open(out_glb, 'rb') as f:
        glb_bytes = f.read()
    title = spec_obj.get("meta", {}).get("name", name)
    html = write_viewer(glb_bytes,
                        [{"name": r["name"], "x": float(r["at"][0]),
                          "y": float(spec_obj.get("meta", {})
                                          .get("wall_height", 2.6) * 0.55),
                          "z": float(r["at"][1])}
                         for r in spec_obj.get("rooms", [])],
                        title,
                        collect_people(spec_obj),
                        art_hue, art_frame)
    with open(viewer, 'w', encoding='utf-8') as f:
        f.write(html)
    print(f"\n✓ Lit viewer: {viewer}")
    print(f"   Lit GLB:    {out_glb}")


if __name__ == "__main__":
    main()

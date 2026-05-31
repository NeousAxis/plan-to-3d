#!/usr/bin/env python3
"""plan-to-image: 2D plan -> photoreal interior renders via Pollinations.ai.

Pivot from the old geometric plan-to-3d (kept in legacy_3d/). The new pipeline
generates a high-quality archviz image PER ROOM by composing a structured
prompt out of the design vocabulary (vocabulary.json) and the per-room style
notes in the user's spec.

Usage:
    python3 plan_to_image.py <spec.json> [--out work/imgs]
                                         [--width 1024] [--height 768]
                                         [--model flux] [--seed 42]
                                         [--only "Séjour"]

Spec file:
    {
      "project": "Apartment 2BR",
      "global": {
        "style":      "japandi",         # key into vocabulary.styles
        "atmosphere": "warm",
        "tech":       "magazine quality, AD-style"
      },
      "rooms": [
        {
          "name":      "Séjour",
          "style":     "japandi",        # override of global
          "floor":     "smoked oak chevron parquet",
          "walls":     "lime plaster, hand-troweled, warm white",
          "lighting":  "Flos IC suspension, opal glass globe",
          "furniture": "Camaleonda modular sofa, Noguchi coffee table",
          "colors":    "Farrow & Ball Cornforth White; accent terracotta",
          "view":      "wide-angle 24mm corner shot, eye-level",
          "extra":     "art piece on wall, dried branch in stone pot"
        },
        ...
      ]
    }

No API key required (Pollinations.ai is free, anonymous). Each image lands
next to a tiny gallery.html for browsing.
"""

import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request


VOCAB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "vocabulary.json")


def load_vocab():
    with open(VOCAB_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def resolve(value, vocab_section):
    """If `value` is a key in vocab_section, return its expanded text;
    otherwise return `value` as-is."""
    if not value:
        return ""
    if isinstance(value, str) and value in vocab_section:
        return vocab_section[value]
    return value


def build_prompt(room, glb):
    """Compose one photoreal archviz prompt for a single room."""
    v = load_vocab()
    style = resolve(room.get("style") or glb.get("style"), v["styles"])
    atmos = resolve(room.get("atmosphere") or glb.get("atmosphere"),
                    v["atmospheres"])
    view  = resolve(room.get("view") or glb.get("view") or "wide_corner",
                    v["camera_views"])

    parts = []
    parts.append(f"professional interior photography of a {room.get('name','room').lower()}")
    if style:        parts.append(style)
    if room.get("floor"):     parts.append(f"floor: {room['floor']}")
    if room.get("walls"):     parts.append(f"walls: {room['walls']}")
    if room.get("ceiling"):   parts.append(f"ceiling: {room['ceiling']}")
    if room.get("lighting"):  parts.append(f"lighting: {room['lighting']}")
    if room.get("furniture"): parts.append(f"furniture: {room['furniture']}")
    if room.get("colors"):    parts.append(f"palette: {room['colors']}")
    if atmos:        parts.append(atmos)
    if room.get("extra"):     parts.append(room["extra"])
    if view:         parts.append(view)
    # Always-on tech sauce
    tech = room.get("tech") or glb.get("tech") or \
        "architectural photography, magazine quality, ultra-photoreal, " \
        "global illumination, soft contact shadows, perfectly straight verticals"
    parts.append(tech)

    return ", ".join(p for p in parts if p)


def fetch_image(prompt, width, height, model, seed, retries=3):
    """Call Pollinations and download the PNG. The service is anonymous and
    free; we retry a few times on transient errors."""
    qs = urllib.parse.urlencode({
        "model": model,
        "width": width, "height": height,
        "nologo": "true",
        "enhance": "true",
        "seed": str(seed),
    })
    encoded = urllib.parse.quote(prompt, safe="")
    url = f"https://image.pollinations.ai/prompt/{encoded}?{qs}"
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url,
                headers={"User-Agent": "plan-to-image/1.0"})
            with urllib.request.urlopen(req, timeout=90) as r:
                return r.read()
        except Exception as e:  # noqa: BLE001
            last = e
            print(f"  attempt {attempt+1}/{retries} failed: {e}")
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"Pollinations fetch failed after {retries}: {last}")


GALLERY_HTML = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<title>{title}</title>
<style>
  body{{margin:0;background:#15171a;color:#e8eaed;
       font:14px/1.5 system-ui,Segoe UI,Roboto,sans-serif}}
  header{{padding:18px 24px;background:#1c2025;border-bottom:1px solid #2a2f36}}
  h1{{margin:0 0 4px;font-size:18px}}
  .meta{{font-size:12px;color:#9aa3b0}}
  .grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(420px,1fr));
        gap:14px;padding:14px}}
  .card{{background:#1c2025;border:1px solid #2a2f36;border-radius:10px;
        overflow:hidden}}
  .card img{{display:block;width:100%;height:auto;background:#0f1114}}
  .label{{padding:10px 14px;font-size:13px}}
  .label b{{color:#fff}}
  .label small{{display:block;color:#76808e;margin-top:4px;font-size:11px}}
  details{{padding:0 14px 12px;color:#9aa3b0;font-size:12px}}
  details pre{{white-space:pre-wrap;margin:6px 0 0;background:#0f1114;
              padding:8px 10px;border-radius:6px;font-size:11px}}
</style></head><body>
<header><h1>{title}</h1><div class="meta">{n} images · model {model}</div></header>
<div class="grid">
{cards}
</div></body></html>
"""

CARD = """  <div class="card">
    <img src="{file}" alt="{name}">
    <div class="label"><b>{name}</b><small>{file}</small></div>
    <details><summary>prompt</summary><pre>{prompt}</pre></details>
  </div>
"""


def slug(s):
    return "".join(c.lower() if c.isalnum() else "_" for c in s).strip("_")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("spec", help="JSON spec describing the plan")
    ap.add_argument("--out", default=None)
    ap.add_argument("--width", type=int, default=1024)
    ap.add_argument("--height", type=int, default=768)
    ap.add_argument("--model", default="flux",
                    help="flux | flux-realism | turbo | any | …")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--only", default=None,
                    help="only generate rooms whose name matches (substring, "
                         "case-insensitive)")
    args = ap.parse_args()

    spec = json.load(open(args.spec, "r", encoding="utf-8"))
    title = spec.get("project") or os.path.splitext(
        os.path.basename(args.spec))[0]
    out_dir = os.path.abspath(args.out or os.path.join("work", slug(title)))
    os.makedirs(out_dir, exist_ok=True)
    glb = spec.get("global", {})
    rooms = spec.get("rooms", [])
    if args.only:
        rooms = [r for r in rooms if args.only.lower() in r.get("name", "").lower()]
        if not rooms:
            sys.exit(f"no rooms matched --only={args.only!r}")

    cards = []
    for r in rooms:
        name = r.get("name", "room")
        prompt = build_prompt(r, glb)
        png_name = slug(name) + ".png"
        png_path = os.path.join(out_dir, png_name)
        print(f"\n[{name}]")
        print("  prompt:", prompt[:140] + ("…" if len(prompt) > 140 else ""))
        print(f"  -> {png_path}")
        data = fetch_image(prompt, args.width, args.height, args.model,
                           args.seed)
        with open(png_path, "wb") as f:
            f.write(data)
        cards.append(CARD.format(name=name, file=png_name, prompt=prompt))

    gallery = os.path.join(out_dir, "gallery.html")
    with open(gallery, "w", encoding="utf-8") as f:
        f.write(GALLERY_HTML.format(title=title, n=len(rooms),
                                     model=args.model,
                                     cards="\n".join(cards)))
    print(f"\n✓ {len(rooms)} images")
    print(f"  gallery: {gallery}")


if __name__ == "__main__":
    main()

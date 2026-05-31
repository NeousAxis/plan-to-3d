#!/usr/bin/env python3
"""plan_analyzer.py — drag a floor plan, get a spec JSON for plan_to_image.py.

Workflow:
    python3 plan_analyzer.py path/to/plan.png [--style japandi]
                                              [--out spec.json]
                                              [--model mistral|llava]

Uses Cloudflare Workers AI multimodal endpoints (free tier) to read the plan
and emit a starter spec the user can hand-tune before rendering. Auth is
auto-discovered from the same wrangler OAuth state plan_to_image.py uses.

What the analyzer does:
1. Asks the vision LLM to extract rooms, dimensions, door/window layout,
   furniture symbols, observed floor finishes (parquet vs tile vs ...).
2. Wraps the result in the spec format plan_to_image.py expects, filling
   in style / materials / lighting from a default style preset chosen by
   the user (--style flag).
3. Writes the spec to disk so the user can edit then render with:
       python3 plan_to_image.py <spec.json>

No API keys required if you've run `wrangler login`. Falls back to LLaVA 7B
if Mistral isn't available.
"""

import argparse
import base64
import json
import os
import re
import sys
import urllib.request

# Reuse the cred + quota logic from plan_to_image
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from plan_to_image import _cf_creds, _quota_check_and_increment  # noqa: E402


VISION_PROMPT = """You are an architectural interior-design assistant analysing a 2D floor plan.

Extract the following and return ONE compact JSON object (no markdown fences, no extra prose):

{
  "project": "<descriptive name based on the plan, e.g. 'Apartment 2BR'>",
  "envelope": {"width_m": <number>, "height_m": <number>},
  "rooms": [
    {
      "name":      "<French label as written on the plan, e.g. 'Séjour', 'Cuisine', 'Chambre 1'>",
      "dimensions":"<read off the plan: 'W × D m, S m², 2.6 m ceiling'>",
      "layout":    "<where windows / doors / shared walls are, e.g. 'two windows on the north wall, door on the south wall'>",
      "placement": "<furniture symbols you see and where: 'double bed against north wall, wardrobe on west wall, ...'>",
      "floor_kind":"<best guess: 'wood' for parquet, 'tile' for ceramic/stone, 'concrete'>"
    }
  ]
}

Rules:
- Read every printed dimension you can see (cotes). Be precise.
- For each room, look at the FURNITURE SYMBOLS (bed shapes, sofas, bathtub, toilet, sink, stove, kitchen counter, dining table) and list them in `placement`.
- Floor: parquet hatching = wood, square/grid tiles = tile, plain = concrete.
- Names in French if the plan is in French, otherwise translate to French.
- Output JSON ONLY. No ``` fences, no commentary."""


# Style → defaults that get injected into each room of the spec.
# These are loose suggestions; the user is expected to edit before rendering.
STYLE_DEFAULTS = {
    "japandi": {
        "atmosphere": "warm",
        "tech": "architectural interior photography, magazine quality (Dezeen, AD), ultra-photoreal, soft natural light, straight verticals",
        "floor_wood": "european white-oak engineered planks, matte oil finish",
        "floor_tile": "honed travertine large-format tiles, warm putty",
        "walls":      "lime plaster, hand-troweled, warm white",
        "ceiling":    "matte white plaster",
        "colors":     "Farrow & Ball Cornforth White, warm neutrals, terracotta accents",
    },
    "scandi": {
        "atmosphere": "airy",
        "tech": "architectural interior photography, magazine quality, photoreal, bright Nordic daylight, straight verticals",
        "floor_wood": "wide pale-pine planks, whitewashed",
        "floor_tile": "matte white porcelain stoneware, 60×60",
        "walls":      "smooth painted plaster, pure white",
        "ceiling":    "matte white",
        "colors":     "white, pale woods, dove grey, single warm accent",
    },
    "wabi_sabi": {
        "atmosphere": "serene",
        "tech": "architectural interior photography, raw materials, soft north-facing daylight, photoreal",
        "floor_wood": "reclaimed wide oak boards, hand-finished",
        "floor_tile": "raw limestone slabs",
        "walls":      "raw earthen plaster, slight imperfections, ochre wash",
        "ceiling":    "exposed timber joists, white-washed",
        "colors":     "muted earth tones, raw woods, dusty terracotta",
    },
    "contemporary_minimal": {
        "atmosphere": "fresh",
        "tech": "architectural interior photography, contemporary minimalism, hard daylight, photoreal",
        "floor_wood": "wide smoked-oak boards, matte",
        "floor_tile": "large-format porcelain, taupe",
        "walls":      "smooth plaster, off-white",
        "ceiling":    "matte white, recessed plaster-in downlights",
        "colors":     "monochrome neutrals, single black accent",
    },
}


def call_vision(image_path, model="mistral", timeout=120):
    acct, tok = _cf_creds()
    if not acct or not tok:
        sys.exit("CF creds missing — run `wrangler login` once.")
    if not os.environ.get("P2I_BYPASS_QUOTA"):
        _quota_check_and_increment()
    img_b64 = base64.b64encode(open(image_path, "rb").read()).decode()
    if model == "mistral":
        endpoint = "@cf/mistralai/mistral-small-3.1-24b-instruct"
        payload = {
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": VISION_PROMPT},
                    {"type": "image_url",
                     "image_url": {"url": f"data:image/png;base64,{img_b64}"}},
                ],
            }],
            "max_tokens": 2000,
        }
    else:  # llava fallback
        endpoint = "@cf/llava-hf/llava-1.5-7b-hf"
        payload = {
            "image": list(open(image_path, "rb").read()),
            "prompt": VISION_PROMPT,
            "max_tokens": 2000,
        }
    url = f"https://api.cloudflare.com/client/v4/accounts/{acct}/ai/run/{endpoint}"
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {tok}",
                 "Content-Type": "application/json"},
        method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read())
    if not data.get("success"):
        raise RuntimeError(f"CF call failed: {data}")
    result = data["result"]
    # Mistral / Llama chat completions: result.choices[0].message.content
    if isinstance(result, dict) and "choices" in result:
        return result["choices"][0]["message"]["content"]
    # LLaVA: result.description, older models: result.response
    if isinstance(result, dict):
        return result.get("response") or result.get("description") or json.dumps(result)
    return str(result)


def parse_json_block(text):
    """Extract the first {…} JSON object from the model's response."""
    text = text.strip()
    # strip markdown fences if present
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    # find the outermost {...}
    start = text.find("{")
    if start < 0:
        raise ValueError("no JSON object found in model output")
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{": depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return json.loads(text[start:i + 1])
    raise ValueError("unterminated JSON object")


def to_render_spec(extracted, style):
    """Turn the raw vision-extracted JSON into the format plan_to_image.py
    wants: each room gets material/lighting defaults injected from the
    chosen style preset, with the geometry-bearing fields preserved."""
    defaults = STYLE_DEFAULTS.get(style, STYLE_DEFAULTS["japandi"])
    spec = {
        "project": extracted.get("project", "Plan"),
        "_source": "Auto-extracted via plan_analyzer.py — EDIT BEFORE RENDERING.",
        "_envelope": extracted.get("envelope"),
        "global": {
            "style":      style,
            "atmosphere": defaults["atmosphere"],
            "tech":       defaults["tech"],
        },
        "rooms": [],
    }
    for r in extracted.get("rooms", []):
        floor_kind = (r.get("floor_kind") or "wood").lower()
        floor = defaults["floor_tile"] if "tile" in floor_kind else defaults["floor_wood"]
        spec["rooms"].append({
            "name":       r.get("name", "Pièce"),
            "dimensions": r.get("dimensions", ""),
            "layout":     r.get("layout", ""),
            "placement":  r.get("placement", ""),
            "viewpoint":  "wide-angle 24mm corner shot, eye-level (~1.6 m), straight verticals",
            "floor":      floor,
            "walls":      defaults["walls"],
            "ceiling":    defaults["ceiling"],
            "lighting":   "soft natural daylight",
            "furniture":  r.get("placement", ""),  # seed the design field with the observed pieces
            "colors":     defaults["colors"],
            "extra":      "TODO — refine before rendering",
        })
    return spec


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("plan", help="path to the floor plan (PNG/JPG)")
    ap.add_argument("--out", default=None,
                    help="output spec JSON path (default: examples/<name>.json)")
    ap.add_argument("--style", default="japandi",
                    choices=list(STYLE_DEFAULTS.keys()),
                    help="design style preset baked into every room")
    ap.add_argument("--model", default="mistral", choices=["mistral", "llava"],
                    help="vision LLM (mistral = 24B, larger / better; "
                         "llava = 7B, faster fallback)")
    args = ap.parse_args()
    if not os.path.exists(args.plan):
        sys.exit(f"file not found: {args.plan}")

    print(f"[analyzer] calling {args.model} on {args.plan}…")
    raw = call_vision(args.plan, model=args.model)
    print(f"[analyzer] raw response: {raw[:200]}…")
    extracted = parse_json_block(raw)
    print(f"[analyzer] extracted {len(extracted.get('rooms', []))} rooms")
    spec = to_render_spec(extracted, args.style)

    name = os.path.splitext(os.path.basename(args.plan))[0]
    out = args.out or os.path.join("examples", f"{name}.json")
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    json.dump(spec, open(out, "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    print(f"\n✓ spec written: {out}")
    print(f"  next: edit it (style, materials, viewpoint…) then:")
    print(f"  python3 plan_to_image.py {out}")


if __name__ == "__main__":
    main()

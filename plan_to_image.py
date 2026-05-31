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

# Cloudflare Workers AI free tier is rate-limited. We track our usage in a
# tiny rolling-day log under XDG_STATE so we can warn before the user hits
# the cap and HARD-BLOCK at the cap — never automatically rolling to a
# paid plan. (Cloudflare's free quota is per-day, ~50 images of FLUX, but
# the exact ceiling depends on the account; we stay safely under 40/day.)
CF_FREE_TIER_DAILY = 40
CF_WARN_PCT = 0.75
QUOTA_PATH = os.path.expanduser("~/.cache/plan_to_image/cf_quota.json")


def _quota_load():
    if not os.path.exists(QUOTA_PATH):
        return {"day": "", "count": 0}
    try:
        return json.load(open(QUOTA_PATH, "r", encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {"day": "", "count": 0}


def _quota_today():
    return time.strftime("%Y-%m-%d", time.localtime())


def _quota_check_and_increment():
    """Returns the new count after increment. Raises if the daily cap is hit
    so we never silently flip to a paid tier."""
    q = _quota_load()
    today = _quota_today()
    if q.get("day") != today:
        q = {"day": today, "count": 0}
    if q["count"] >= CF_FREE_TIER_DAILY:
        raise RuntimeError(
            f"❌ Cloudflare free-tier cap reached ({q['count']}/"
            f"{CF_FREE_TIER_DAILY} today). Refusing to continue — the code "
            f"won't auto-roll to a PAID plan. Wait until tomorrow or set "
            f"P2I_BYPASS_QUOTA=1 to override (will still bill against the "
            f"free tier, may fail).")
    q["count"] += 1
    os.makedirs(os.path.dirname(QUOTA_PATH), exist_ok=True)
    json.dump(q, open(QUOTA_PATH, "w", encoding="utf-8"))
    used = q["count"]
    pct = used / CF_FREE_TIER_DAILY
    if pct >= CF_WARN_PCT:
        remain = CF_FREE_TIER_DAILY - used
        print(f"  ⚠️  CF quota: {used}/{CF_FREE_TIER_DAILY} today "
              f"({int(pct*100)}%) — {remain} left before HARD BLOCK")
    return used


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
    """Compose one photoreal archviz prompt for a single room.

    The plan-derived geometry (dimensions, layout, viewpoint, furniture
    placement) comes FIRST in the prompt so FLUX latches on to the spatial
    constraints — the style/material vocabulary then fills in the look.
    """
    v = load_vocab()
    style = resolve(room.get("style") or glb.get("style"), v["styles"])
    atmos = resolve(room.get("atmosphere") or glb.get("atmosphere"),
                    v["atmospheres"])
    view  = resolve(room.get("view") or glb.get("view") or "wide_corner",
                    v["camera_views"])
    name = room.get("name", "room")

    parts = []
    # ── Plan-derived geometry FIRST (FLUX weights the front of the prompt
    #    most). This is what makes the render match the user's actual plan,
    #    not a generic Pinterest japandi shot.
    parts.append(f"interior architectural photograph of {name.lower()}")
    if room.get("dimensions"):
        # e.g. "rectangular, 5.36 × 3.72 m, 19.7 m²"
        parts.append(f"room dimensions: {room['dimensions']}")
    if room.get("layout"):
        # e.g. "window on the north wall, door on the south wall, wood-clad east wall"
        parts.append(f"layout: {room['layout']}")
    if room.get("placement"):
        # e.g. "double bed centred against the north wall headboard north,
        # wardrobe full-length along the west wall, nightstand left of bed"
        parts.append(f"furniture placement: {room['placement']}")
    if room.get("viewpoint"):
        # e.g. "view from the south-west corner looking north-east towards the window"
        parts.append(f"camera viewpoint: {room['viewpoint']}")

    # ── Then the design vocabulary
    if style:                 parts.append(style)
    if room.get("floor"):     parts.append(f"floor finish: {room['floor']}")
    if room.get("walls"):     parts.append(f"wall finish: {room['walls']}")
    if room.get("ceiling"):   parts.append(f"ceiling: {room['ceiling']}")
    if room.get("lighting"):  parts.append(f"lighting: {room['lighting']}")
    if room.get("furniture"): parts.append(f"furniture pieces: {room['furniture']}")
    if room.get("colors"):    parts.append(f"palette: {room['colors']}")
    if atmos:                 parts.append(atmos)
    if room.get("extra"):     parts.append(room["extra"])
    if view and not room.get("viewpoint"):
        # use the abstract view preset only if no concrete viewpoint was given
        parts.append(view)

    # ── Always-on tech sauce
    tech = room.get("tech") or glb.get("tech") or \
        "architectural photography, magazine quality, ultra-photoreal, " \
        "global illumination, soft contact shadows, perfectly straight verticals"
    parts.append(tech)

    return ", ".join(p for p in parts if p)


def fetch_pollinations(prompt, width, height, model, seed, timeout=90):
    """Free anonymous FLUX endpoint. Returns PNG bytes or raises."""
    qs = urllib.parse.urlencode({
        "model": model,
        "width": width, "height": height,
        "nologo": "true",
        "enhance": "true",
        "seed": str(seed),
    })
    encoded = urllib.parse.quote(prompt, safe="")
    url = f"https://image.pollinations.ai/prompt/{encoded}?{qs}"
    req = urllib.request.Request(url,
        headers={"User-Agent": "plan-to-image/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _cf_creds():
    """Return (account_id, token) for Cloudflare Workers AI. Prefers env vars,
    then falls back to wrangler's stored OAuth token (auto-refreshed by
    `wrangler login`). Returns (None, None) if nothing is configured."""
    acct = os.environ.get("CLOUDFLARE_ACCOUNT_ID")
    tok  = os.environ.get("CLOUDFLARE_API_TOKEN")
    if acct and tok:
        return acct, tok
    # Fallback: read wrangler's stored oauth_token
    cfg_path = os.path.expanduser("~/.wrangler/config/default.toml")
    if not tok and os.path.exists(cfg_path):
        for line in open(cfg_path, "r", encoding="utf-8"):
            if line.strip().startswith("oauth_token"):
                tok = line.split('"')[1]
                break
    # Fallback: account id from `wrangler whoami`
    if not acct:
        try:
            import subprocess
            out = subprocess.check_output(["wrangler", "whoami"],
                stderr=subprocess.DEVNULL, timeout=8).decode("utf-8", "ignore")
            for line in out.split("\n"):
                for token in line.split():
                    t = token.strip().strip("│").strip()
                    if len(t) == 32 and all(c in "0123456789abcdef" for c in t.lower()):
                        acct = t; break
                if acct: break
        except Exception:  # noqa: BLE001
            pass
    return acct, tok


def fetch_cloudflare(prompt, width, height, model, seed, timeout=120):
    """Cloudflare Workers AI FLUX. Free tier ~50–100 images/day. Auto-reads
    creds from env vars OR `wrangler` OAuth state (run `wrangler login` once).

    HARD-BLOCKS at CF_FREE_TIER_DAILY images/day to prevent any silent
    spillover to a paid plan."""
    import base64
    acct, tok = _cf_creds()
    if not acct or not tok:
        raise RuntimeError("CF creds missing — run `wrangler login` once, or "
                           "set CLOUDFLARE_ACCOUNT_ID + CLOUDFLARE_API_TOKEN")
    if not os.environ.get("P2I_BYPASS_QUOTA"):
        _quota_check_and_increment()
    cf_model = ("@cf/black-forest-labs/flux-1-schnell"
                if model in ("flux", "flux-schnell", "schnell")
                else f"@cf/{model}")
    url = f"https://api.cloudflare.com/client/v4/accounts/{acct}/ai/run/{cf_model}"
    body = json.dumps({
        "prompt":    prompt,
        "width":     min(width, 2048),
        "height":    min(height, 2048),
        "seed":      int(seed),
        "num_steps": 8,
    }).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Authorization": f"Bearer {tok}",
        "Content-Type":  "application/json",
        "User-Agent":    "plan-to-image/1.0",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        payload = json.loads(r.read())
    if not payload.get("success"):
        raise RuntimeError(f"Cloudflare returned: {payload}")
    return base64.b64decode(payload["result"]["image"])


def fetch_image(prompt, width, height, model, seed, retries=3, provider="auto"):
    """Provider routing:
      - "auto"        : Pollinations primary, Cloudflare fallback (default)
      - "pollinations": only Pollinations
      - "cloudflare"  : only Cloudflare Workers AI
    """
    cf_acct, cf_tok = _cf_creds()
    cf_ready = bool(cf_acct and cf_tok)

    if provider == "cloudflare":
        if not cf_ready:
            raise RuntimeError("--provider cloudflare requires creds (run "
                               "`wrangler login` once)")
        return fetch_cloudflare(prompt, width, height, model, seed)

    last = None
    for attempt in range(retries):
        try:
            return fetch_pollinations(prompt, width, height, model, seed)
        except Exception as e:  # noqa: BLE001
            last = e
            print(f"  Pollinations attempt {attempt+1}/{retries} failed: {e}")
            time.sleep(2 * (attempt + 1))

    if provider == "pollinations":
        raise RuntimeError(f"Pollinations failed after {retries}: {last}")
    if cf_ready:
        print("  → falling back to Cloudflare Workers AI FLUX schnell")
        try:
            return fetch_cloudflare(prompt, width, height, model, seed)
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(
                f"Both providers failed. Pollinations: {last}; Cloudflare: {e}")
    raise RuntimeError(f"Pollinations failed after {retries}: {last}. "
                       f"Run `wrangler login` to enable the Cloudflare "
                       f"fallback (free tier).")


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
    ap.add_argument("--provider", default="auto",
                    choices=["auto", "pollinations", "cloudflare"],
                    help="auto=Pollinations + CF fallback (default)")
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
                           args.seed, provider=args.provider)
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

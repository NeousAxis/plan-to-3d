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


# ───────────────────────── geometric control mode ─────────────────────────
# Text-only FLUX ignores the plan: it invents a generic room with the right
# furniture but the wrong shape / windows. To actually honour the plan we
# draw a crude one-point-perspective "clay" sketch of each room straight from
# the spec (room area -> wall closeness, layout -> window presence + side,
# placement/furniture -> box silhouettes) and feed THAT to img2img. The
# generator then repaints it photoreal while keeping the geometry.
import re as _re

DEFAULT_NEG = ("lowres, blurry, deformed, distorted perspective, extra rooms, "
               "fisheye, warped walls, watermark, text, duplicate furniture")


def _area_m2(dimensions):
    """Pull the floor area in m² out of a dimensions string, else None."""
    if not dimensions:
        return None
    m = _re.search(r"([\d.]+)\s*m²", str(dimensions))
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            return None
    return None


def _room_blob(room, *keys):
    return " ".join(str(room.get(k, "")) for k in keys).lower()


def room_has_window(room):
    """True unless the room is explicitly windowless (cellier, WC, dressing…)."""
    blob = _room_blob(room, "layout", "placement", "dimensions", "extra")
    if any(w in blob for w in ("windowless", "no window", "borgne", "sans fen")):
        return False
    return any(w in blob for w in ("window", "glaz", "baie", "fenêtre"))


def build_control_image(room, W, H):
    """One-point-perspective control sketch for `room`, as a PIL.Image."""
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        raise RuntimeError("geometric mode needs Pillow — `pip install Pillow`")

    blob = _room_blob(room, "name", "layout", "placement", "furniture", "viewpoint")
    def has(*w):
        return any(x in blob for x in w)

    area = _area_m2(room.get("dimensions")) or 14.0
    # small area -> walls close -> big back wall (small horizontal margin)
    f = max(0.12, min(0.32, 0.12 + (area - 3) / 47.0 * 0.20))
    img = Image.new("RGB", (W, H), (238, 233, 223))
    d = ImageDraw.Draw(img)

    bwl, bwr = int(f * W), int(W - f * W)
    bwt, bwb = int(0.24 * H), int(0.70 * H)
    wall = (235, 229, 218); ceil = (244, 240, 232); floor = (212, 200, 183)
    side = (225, 218, 207); oak = (196, 167, 119); oak_d = (173, 144, 98)
    appl = (236, 236, 234); glass = (213, 224, 230); dark = (150, 140, 128)

    d.polygon([(0, 0), (W, 0), (bwr, bwt), (bwl, bwt)], fill=ceil)
    d.polygon([(0, H), (W, H), (bwr, bwb), (bwl, bwb)], fill=floor)
    d.polygon([(0, 0), (bwl, bwt), (bwl, bwb), (0, H)], fill=side)
    d.polygon([(W, 0), (bwr, bwt), (bwr, bwb), (W, H)], fill=side)
    d.polygon([(bwl, bwt), (bwr, bwt), (bwr, bwb), (bwl, bwb)], fill=wall)
    vp = ((bwl + bwr) // 2, (bwt + bwb) // 2)
    for c in [(0, 0), (W, 0), (0, H), (W, H)]:
        d.line([c, vp], fill=dark, width=1)
    d.rectangle([bwl, bwt, bwr, bwb], outline=dark, width=2)

    # floor mapping: u=0 left..1 right, v=0 front..1 back
    FL, FR, BL, BR = (0, H), (W, H), (bwl, bwb), (bwr, bwb)
    def fpt(u, v):
        bx = FL[0] + (FR[0] - FL[0]) * u
        tx = BL[0] + (BR[0] - BL[0]) * u
        by = FL[1] + (BL[1] - FL[1]) * v
        return (bx + (tx - bx) * v, by)
    def box(u, v, wu, wv, h, col, oc=None):
        b = [fpt(u - wu / 2, v - wv / 2), fpt(u + wu / 2, v - wv / 2),
             fpt(u + wu / 2, v + wv / 2), fpt(u - wu / 2, v + wv / 2)]
        dy = h * (H * 0.16) * (1.0 - 0.5 * v)
        top = [(x, y - dy) for (x, y) in b]
        d.polygon(b, fill=tuple(max(0, c - 14) for c in col))           # footprint
        d.polygon([b[0], b[1], top[1], top[0]], fill=col, outline=oc)   # front face
        d.polygon([b[1], b[2], top[2], top[1]],                          # right face
                  fill=tuple(max(0, c - 20) for c in col), outline=oc)
        d.polygon(top, fill=tuple(min(255, c + 10) for c in col), outline=oc)  # top
        return top

    # ── windows (the part text-only FLUX gets most wrong) ──
    if room_has_window(room):
        vpv = str(room.get("viewpoint", "")).lower()
        back_win = ("toward" in vpv or "facing" in vpv) and \
                   ("window" in vpv or "windows" in vpv)
        if back_win:
            wx0 = bwl + int(0.22 * (bwr - bwl)); wx1 = bwr - int(0.22 * (bwr - bwl))
            wy0 = bwt + int(0.14 * (bwb - bwt)); wy1 = bwt + int(0.66 * (bwb - bwt))
            d.rectangle([wx0, wy0, wx1, wy1], fill=glass, outline=(120, 120, 120), width=3)
            d.line([((wx0 + wx1) // 2, wy0), ((wx0 + wx1) // 2, wy1)],
                   fill=(120, 120, 120), width=2)
        else:  # window on the right-hand side wall (trapezoid in perspective)
            d.polygon([(bwr + 12, bwt + 24), (W - 28, 96),
                       (W - 28, H - 190), (bwr + 12, bwb - 36)],
                      fill=glass, outline=(120, 120, 120))
    else:  # windowless -> a ceiling light strip instead
        cx = (bwl + bwr) // 2
        d.line([(cx - 46, bwt - 28), (cx + 46, bwt - 28)], fill=(255, 245, 210), width=6)

    # ── furniture silhouettes (rough, just enough cues for img2img) ──
    if has("wardrobe", "storage", "cabinet", "placard", "joinery", "dressing", "rangement"):
        box(0.5, 0.93, 0.66, 0.10, 2.0, oak, oak_d)        # full-height units, back wall
    if has("kitchen", "counter", "island", "cabinetry", "cooktop", "worktop", "plan de travail"):
        box(0.5, 0.86, 0.78, 0.13, 0.9, oak, oak_d)        # low counter run, back
    if has("bed"):
        box(0.5, 0.60, 0.44, 0.42, 0.42, (224, 216, 203), dark)   # mattress
        box(0.5, 0.90, 0.46, 0.06, 1.0, oak, oak_d)               # headboard panel
    if has("sofa", "daybed", "canap"):
        box(0.34, 0.42, 0.52, 0.22, 0.4, (226, 219, 206), dark)
    if has("dining", "round dining", "dining table", "table ronde"):
        box(0.72, 0.52, 0.26, 0.26, 0.42, oak, oak_d)
        for du in (-0.19, 0.19):
            box(0.72 + du, 0.52, 0.08, 0.08, 0.5, oak_d, dark)
    if has("bathtub", "soaking tub", "baignoire"):
        box(0.5, 0.70, 0.52, 0.22, 0.5, appl, (185, 185, 185))
    if has("vanity", "washbasin", "vasque", "basin", "lavabo"):
        box(0.15, 0.5, 0.10, 0.46, 0.85, oak, oak_d)       # along left wall
    if has("washer", "washing", "laundry", "lave-linge"):
        t = box(0.78, 0.34, 0.16, 0.16, 0.85, appl, (120, 120, 120))
        cx = sum(p[0] for p in t) / 4; cy = sum(p[1] for p in t) / 4
        d.ellipse([cx - 22, cy - 22, cx + 22, cy + 22],
                  fill=(60, 60, 64), outline=(150, 150, 150), width=3)
    if has("toilet", "wall-hung", "wc"):
        box(0.5, 0.82, 0.16, 0.18, 0.55, appl, (160, 160, 160))
    if has("console", "sideboard", "bench", "banc"):
        box(0.5, 0.86, 0.58, 0.12, 0.85, oak, oak_d)

    return img


def fetch_cloudflare_img2img(control_png, prompt, width, height,
                             strength=0.72, negative=None, timeout=120):
    """SD-1.5 img2img on Cloudflare Workers AI — the only FREE image-to-image
    available. Repaints the control sketch photoreal while honouring its
    geometry. HARD-BLOCKS on the same daily quota as the FLUX path."""
    import base64
    acct, tok = _cf_creds()
    if not acct or not tok:
        raise RuntimeError("CF creds missing — run `wrangler login` once.")
    if not os.environ.get("P2I_BYPASS_QUOTA"):
        _quota_check_and_increment()
    endpoint = "@cf/runwayml/stable-diffusion-v1-5-img2img"
    url = f"https://api.cloudflare.com/client/v4/accounts/{acct}/ai/run/{endpoint}"
    # NB: SD-1.5 is trained at 512px. Forcing width/height to the spec's
    # 1024×768 washes the image out and transposes the aspect, squashing the
    # control sketch. We omit width/height so CF derives a native-scale output
    # from the input image (this is what the working spike did).
    body = json.dumps({
        "prompt": prompt,
        "negative_prompt": negative or DEFAULT_NEG,
        "image_b64": base64.b64encode(control_png).decode(),
        "strength": strength,
        "guidance": 7.5,
        "num_steps": 20,
    }).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Authorization": f"Bearer {tok}",
        "Content-Type": "application/json",
        "User-Agent": "plan-to-image/1.0",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
    if raw[:8] == b"\x89PNG\r\n\x1a\n":
        return raw
    data = json.loads(raw)
    res = data.get("result")
    if isinstance(res, dict) and res.get("image"):
        return base64.b64decode(res["image"])
    raise RuntimeError(f"Cloudflare img2img returned: {str(raw)[:200]}")


# ───────────────── Qwen-Image-Edit engine (frontier, keyless) ─────────────────
# SD-1.5 (the only free no-key img2img on CF) is a 2022 model — it regularises
# the control sketch and looks flat. Qwen-Image-Edit (Alibaba, Apache-2.0) is a
# 2025 frontier EDIT model that actually understands the input image (Qwen2.5-VL
# semantic + VAE appearance control). It is reachable KEYLESS via public
# HuggingFace Spaces (anonymous ZeroGPU ≈ 240 s/day per IP ≈ ~8 renders/day;
# pass HF_TOKEN for a larger free quota). We feed it our geometry-correct control
# sketch + an edit instruction → photoreal AND faithful. Falls back to SD-1.5.
QWEN_EDIT_SPACES = ["Qwen/Qwen-Image-Edit", "multimodalart/Qwen-Image-Edit-Fast"]


def build_edit_instruction(room, glb):
    """Compose the natural-language EDIT instruction for Qwen-Image-Edit."""
    style = resolve(room.get("style") or glb.get("style") or "japandi",
                    load_vocab().get("styles", {}))
    name = room.get("name", "room")
    parts = [f"Turn this rough 3D blockout sketch into a photorealistic architectural "
             f"interior photograph of a {name} in {style} style.",
             "Keep the EXACT layout, room proportions, wall angles, and the position of "
             "every window, door and furniture piece shown in the sketch."]
    if not room_has_window(room):
        parts.append("This room has NO window — do not add any window or daylight.")
    if room.get("placement"):
        parts.append("Furniture present: " + room["placement"] + ".")
    if room.get("floor"):
        parts.append("Floor: " + room["floor"] + ".")
    if room.get("walls"):
        parts.append("Walls: " + room["walls"] + ".")
    parts.append("Ultra photorealistic, magazine quality, soft natural light, straight verticals.")
    return " ".join(parts)


def fetch_qwen_edit(image_png, instruction, hf_token=None, steps=8, guidance=4.0):
    """Keyless (or free-HF-token) frontier image edit via a public Qwen-Image-Edit
    HF Space. Returns PNG bytes; raises so the caller can fall back to SD-1.5."""
    try:
        from gradio_client import Client, handle_file
    except ImportError:
        raise RuntimeError("qwen engine needs gradio_client — `pip install gradio_client`")
    import tempfile
    tf = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
    tf.write(image_png); tf.close()
    last = None
    for space in QWEN_EDIT_SPACES:
        try:
            client = Client(space, token=hf_token, verbose=False)
            res = client.predict(handle_file(tf.name), instruction, 0, True,
                                 float(guidance), float(steps), False, api_name="/infer")
            img = res[0] if isinstance(res, (list, tuple)) else res
            if isinstance(img, list) and img:
                img = img[0]
                if isinstance(img, dict) and "image" in img:
                    img = img["image"]
            path = img.get("path") if isinstance(img, dict) else img
            return open(path, "rb").read()
        except Exception as e:  # noqa: BLE001
            last = e
            continue
    raise RuntimeError(f"Qwen-Image-Edit unavailable (quota/space busy): {last}")


def _read_key(envname, filename):
    """Resolve an API key from env var, else ~/.cache/plan_to_image/<filename>."""
    v = os.environ.get(envname)
    if v:
        return v.strip()
    p = os.path.expanduser(os.path.join("~/.cache/plan_to_image", filename))
    if os.path.exists(p):
        k = open(p, encoding="utf-8").read().strip()
        return k or None
    return None


def fetch_siliconflow_edit(image_png, prompt, api_key, model="Qwen/Qwen-Image-Edit",
                           steps=30, guidance=4.0, timeout=180):
    """Qwen-Image-Edit on SiliconFlow — frontier edit model, OpenAI-style API,
    base64 image in (portable, no URL hosting), ~$0.04/image with $1 free credits
    (~25 images). Not GPU-quota throttled like HF ZeroGPU. Returns PNG bytes."""
    import base64
    b64 = base64.b64encode(image_png).decode()
    body = json.dumps({
        "model": model,
        "prompt": prompt,
        "image": f"data:image/png;base64,{b64}",
        "batch_size": 1,
        "num_inference_steps": int(steps),
        "guidance_scale": float(guidance),
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://api.siliconflow.com/v1/images/generations", data=body, method="POST",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json",
                 "User-Agent": "plan-to-image/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read())
    imgs = data.get("images") or data.get("data") or []
    url = imgs[0].get("url") if imgs and isinstance(imgs[0], dict) else None
    if not url:
        raise RuntimeError(f"SiliconFlow returned: {str(data)[:200]}")
    with urllib.request.urlopen(url, timeout=timeout) as r2:
        return r2.read()


def render_geometric(ctrl_bytes, room, glb, engine, hf_token=None, sf_key=None,
                     hf_steps=8, sd_strength=0.72, width=1024, height=1024):
    """Render one geometric room through the engine ladder; return (bytes, label).
    auto ladder (NO Google, ever): HF Qwen-Image-Edit Space (free, keyless,
    ZeroGPU-throttled) -> SD1.5 (unlimited but flat). SiliconFlow Qwen-Image-Edit
    is opt-in only (needs a key; paid w/ free credits). A forced engine raises if
    its path is unavailable."""
    instr = build_edit_instruction(room, glb)

    def siliconflow():
        if not sf_key:
            raise RuntimeError("no SILICONFLOW_KEY / siliconflow_key")
        return fetch_siliconflow_edit(ctrl_bytes, instr, sf_key), \
            "Qwen-Image-Edit (SiliconFlow)"

    def hf_qwen():
        return fetch_qwen_edit(ctrl_bytes, instr, hf_token=hf_token, steps=hf_steps), \
            "Qwen-Image-Edit (HF Space)"

    def sd15():
        neg = DEFAULT_NEG if room_has_window(room) else \
            DEFAULT_NEG + ", window, daylight window, glass wall, large open room"
        return fetch_cloudflare_img2img(ctrl_bytes, build_prompt(room, glb), width,
                                        height, strength=sd_strength, negative=neg), \
            "Cloudflare SD1.5 img2img (fallback)"

    forced = {"siliconflow": siliconflow, "qwen": hf_qwen, "sd15": sd15}
    if engine in forced:
        return forced[engine]()                      # raises if unavailable
    ladder = ([siliconflow] if sf_key else []) + [hf_qwen, sd15]
    last = None
    for fn in ladder:
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            print(f"  {fn.__name__} unavailable ({str(e)[:70]})")
            last = e
    raise RuntimeError(f"all engines failed: {last}")


# ───────── geometry-aware control sketch (uses structured plan_extract geom) ─────────
_OPP = {'N': 'S', 'S': 'N', 'E': 'W', 'W': 'E'}
_LR = {'S': ('E', 'W'), 'N': ('W', 'E'), 'E': ('N', 'S'), 'W': ('S', 'N')}  # facing->(left,right)


def _wall_letter(text):
    import re as _r
    m = _r.search(r'\b([NSEW])\b', (text or "").upper())
    if m:
        return m.group(1)
    for k, v in {'NORTH': 'N', 'SOUTH': 'S', 'EAST': 'E', 'WEST': 'W'}.items():
        if k in (text or "").upper():
            return v
    return None


def build_control_from_geom(room, geom, W, H):
    """Perspective control sketch built from STRUCTURED geometry (entry sets the
    camera; fixtures land on their real wall left/right/back; windows on the right
    wall; windowless rooms get none; irregular rooms get a canted wall)."""
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (W, H), (238, 233, 223)); d = ImageDraw.Draw(img)
    wall = (234, 228, 217); ceil = (244, 240, 232); floor = (213, 201, 184)
    side = (226, 219, 208); oak = (196, 167, 119); oak_d = (173, 144, 98)
    appl = (237, 237, 235); glass = (211, 223, 230); dark = (162, 153, 140)
    entry = (geom.get("entry") or 'S').upper()
    facing = _OPP.get(entry, 'N'); left, right = _LR.get(facing, ('E', 'W'))
    irregular = (geom.get("shape") == "irregular")
    f = 0.17; bwl, bwr = int(f * W), int(W - f * W); bwt, bwb = int(0.25 * H), int(0.69 * H)
    lean = 60 if irregular else 0
    d.polygon([(0, 0), (W, 0), (bwr, bwt), (bwl, bwt)], fill=ceil)
    d.polygon([(0, H), (W, H), (bwr, bwb), (bwl, bwb)], fill=floor)
    d.polygon([(0, 0), (bwl + lean, bwt), (bwl + lean, bwb), (0, H)], fill=side)
    d.polygon([(W, 0), (bwr, bwt), (bwr, bwb), (W, H)], fill=side)
    d.polygon([(bwl, bwt), (bwr, bwt), (bwr, bwb), (bwl, bwb)], fill=wall)
    for a, b in [((bwl, bwt), (bwr, bwt)), ((bwl, bwb), (bwr, bwb)),
                 ((bwl, bwt), (bwl, bwb)), ((bwr, bwt), (bwr, bwb)),
                 ((0, 0), (bwl + lean, bwt)), ((0, H), (bwl + lean, bwb)),
                 ((W, 0), (bwr, bwt)), ((W, H), (bwr, bwb))]:
        d.line([a, b], fill=dark, width=2)
    FL, FR, BL, BR = (0, H), (W, H), (bwl, bwb), (bwr, bwb)

    def fpt(u, v):
        bx = FL[0] + (FR[0] - FL[0]) * u; tx = BL[0] + (BR[0] - BL[0]) * u
        return (bx + (tx - bx) * v, FL[1] + (BL[1] - FL[1]) * v)

    def box(u, v, wu, wv, h, col, circ=0):
        b = [fpt(u - wu / 2, v - wv / 2), fpt(u + wu / 2, v - wv / 2),
             fpt(u + wu / 2, v + wv / 2), fpt(u - wu / 2, v + wv / 2)]
        dy = h * (H * 0.16) * (1 - 0.5 * v); top = [(x, y - dy) for (x, y) in b]
        d.polygon(b, fill=tuple(max(0, c - 14) for c in col))
        d.polygon([b[0], b[1], top[1], top[0]], fill=col, outline=oak_d)
        d.polygon([b[1], b[2], top[2], top[1]], fill=tuple(max(0, c - 20) for c in col), outline=oak_d)
        d.polygon(top, fill=tuple(min(255, c + 8) for c in col), outline=oak_d)
        for _ in range(circ):
            cx = (b[0][0] + b[1][0]) / 2; cy = (b[0][1] + top[0][1]) / 2
            d.ellipse([cx - 18, cy - 18, cx + 18, cy + 18], fill=(70, 70, 74),
                      outline=(150, 150, 150), width=3)

    wins = [o for o in geom.get("openings", []) if o.get("kind") == "window"]
    for o in wins:
        w = (o.get("wall") or "").upper()
        if w == facing:
            d.rectangle([bwl + 0.25 * (bwr - bwl), bwt + 0.15 * (bwb - bwt),
                         bwr - 0.25 * (bwr - bwl), bwt + 0.64 * (bwb - bwt)],
                        fill=glass, outline=(120, 120, 120), width=3)
        elif w == right:
            d.polygon([(bwr + 8, bwt + 14), (W - 34, bwt + 34), (W - 34, bwb + 36),
                       (bwr + 8, bwb - 12)], fill=glass, outline=(120, 120, 120))
        elif w == left:
            d.polygon([(34, bwt + 34), (bwl - 8, bwt + 14), (bwl - 8, bwb - 12),
                       (34, bwb + 36)], fill=glass, outline=(120, 120, 120))
    if not wins:
        cx = (bwl + bwr) // 2
        d.line([(cx - 48, bwt - 26), (cx + 48, bwt - 26)], fill=(255, 246, 212), width=6)

    def kind(item):
        s = (item or "").lower()
        if any(k in s for k in ('washer', 'washing', 'machine', 'dryer', 'lave-linge', 'seche', 'ml ', 'sl ')):
            return ('appl', 0.16, 0.16, 0.85, 1)
        if 'pac' in s or 'heat' in s or 'pump' in s:
            return ('tall', 0.14, 0.12, 1.6, 0)
        if 'bed' in s or 'lit' in s:
            return ('bed', 0.5, 0.42, 0.42, 0)
        if 'bath' in s or 'baign' in s:
            return ('appl', 0.5, 0.22, 0.5, 0)
        if 'wc' in s or 'toilet' in s:
            return ('appl', 0.18, 0.18, 0.55, 0)
        if any(k in s for k in ('sink', 'vasque', 'basin', 'lavabo', 'evier')):
            return ('low', 0.4, 0.4, 0.82, 0)
        if any(k in s for k in ('placard', 'wardrobe', 'cabinet', 'storage', 'joinery', 'rangement')):
            return ('tall', 0.14, 0.5, 1.8, 0)
        if any(k in s for k in ('sofa', 'canape', 'daybed')):
            return ('low', 0.5, 0.22, 0.4, 0)
        if any(k in s for k in ('cuisson', 'cooktop', 'kitchen', 'counter', 'plaque', 'plan de travail')):
            return ('low', 0.7, 0.13, 0.9, 0)
        return ('low', 0.2, 0.2, 0.6, 0)

    sides = {'back': [], 'left': [], 'right': []}
    for fx in geom.get("fixtures", []):
        w = _wall_letter(fx.get("where") or fx.get("wall") or "")
        if w == facing:
            sides['back'].append(fx)
        elif w == left:
            sides['left'].append(fx)
        elif w == right:
            sides['right'].append(fx)
    for sidename, fxs in sides.items():
        n = len(fxs)
        for i, fx in enumerate(fxs):
            t, wu, wv, h, circ = kind(fx.get("item"))
            col = appl if t == 'appl' else oak
            frac = (i + 1) / (n + 1)
            if sidename == 'left':
                u, v = 0.13, 0.25 + 0.5 * frac
            elif sidename == 'right':
                u, v = 0.87, 0.25 + 0.5 * frac
            else:
                u, v = 0.3 + 0.4 * frac, 0.9
            box(u, v, wu, wv, h, col, circ=circ)
    return img


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
    ap.add_argument("--mode", default="text", choices=["text", "geometric"],
                    help="text=FLUX from prompt (pretty, ignores plan shape); "
                         "geometric=img2img seeded by a perspective sketch built "
                         "from the spec geometry (honours the plan, CF free img2img)")
    ap.add_argument("--strength", type=float, default=0.72,
                    help="geometric mode, SD1.5 engine only: img2img denoise "
                         "strength (~0.55 faithful/flat, ~0.8 photoreal/looser)")
    ap.add_argument("--engine", default="auto",
                    choices=["auto", "siliconflow", "qwen", "sd15"],
                    help="geometric render engine (NO Google). auto ladder: HF "
                         "Qwen-Image-Edit Space (free, keyless, throttled) → SD1.5 "
                         "(flat, unlimited). qwen=force HF Qwen, sd15=force SD1.5, "
                         "siliconflow=opt-in (needs key).")
    ap.add_argument("--qwen-steps", type=int, default=8,
                    help="Qwen-Image-Edit inference steps (keep low for ZeroGPU quota)")
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

    if args.mode == "geometric":
        print(f"  mode: geometric (img2img, strength {args.strength}) — "
              f"control sketch built from spec geometry, CF free img2img")

    cards = []
    for r in rooms:
        name = r.get("name", "room")
        prompt = build_prompt(r, glb)
        png_name = slug(name) + ".png"
        png_path = os.path.join(out_dir, png_name)
        print(f"\n[{name}]")
        print("  prompt:", prompt[:140] + ("…" if len(prompt) > 140 else ""))
        print(f"  -> {png_path}")
        if args.mode == "geometric":
            # geometry-aware control if structured geom is present (from plan_extract),
            # else the heuristic spec-driven sketch
            if r.get("_geom"):
                ctrl = build_control_from_geom(r, r["_geom"], args.width, args.height)
            else:
                ctrl = build_control_image(r, args.width, args.height)
            ctrl_name = slug(name) + "_control.png"
            ctrl.save(os.path.join(out_dir, ctrl_name))
            import io
            buf = io.BytesIO(); ctrl.save(buf, "PNG"); ctrl_bytes = buf.getvalue()
            print(f"  control: {ctrl_name}  (window={room_has_window(r)})")
            data, label = render_geometric(
                ctrl_bytes, r, glb, args.engine,
                hf_token=_read_key("HF_TOKEN", "hf_token"),
                sf_key=_read_key("SILICONFLOW_KEY", "siliconflow_key"),
                hf_steps=args.qwen_steps, sd_strength=args.strength,
                width=args.width, height=args.height)
            print("  engine:", label)
        else:
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

#!/usr/bin/env python3
"""plan_seg.py — specialized floor-plan detection as the SPATIAL reader.

Calls a public HF Space running a dedicated floor-plan detection model
(Viraj2307/Floor-Plan-Detection — CubiCasa-style: rooms / doors / windows in
seconds), then deterministically extracts the detection boxes from the
annotated overlays (pure-red rectangles → connected components → bboxes) and
calibrates pixels → metres against the printed-chain envelope.

Division of labour (the honest post-mortem of session #4):
  * SPATIAL truth (where rooms/doors/windows are)  → this specialized model
  * METRIC truth (exact metres, areas cross-check) → plan_reader's chain solver
  * fallback                                        → plan_reader heuristics

Usage:
    python3 plan_seg.py detect plan.jpg              # -> plan.seg.json + dump
"""
import json
import os
import sys

SPACE = "Viraj2307/Floor-Plan-Detection"


def detect(image_path, out_dir=None, hf_token=None):
    """Run the Space → save the two overlays → return their paths + counts."""
    from gradio_client import Client, handle_file
    import shutil
    out_dir = out_dir or os.path.join(os.path.dirname(image_path) or ".",
                                      "_seg")
    os.makedirs(out_dir, exist_ok=True)
    client = Client(SPACE, token=hf_token, verbose=False)
    gallery, txt = client.predict(
        image=handle_file(image_path), zoom_factor=1.0, color_choice="Red",
        selected_layers=["Room Detection", "Doors and Windows Detection"],
        api_name="/process_floor_plan")
    paths = []
    for i, item in enumerate(gallery or []):
        img = item.get("image")
        p = img.get("path") if isinstance(img, dict) else img
        if p:
            dst = os.path.join(out_dir, f"overlay_{i}.png")
            shutil.copy(p, dst)
            paths.append(dst)
    try:
        counts = json.loads(txt)
    except Exception:  # noqa: BLE001
        counts = {"raw": txt}
    return paths, counts


# ── deterministic red-box extraction from the overlays ─────────────────────
def red_boxes(png_path, min_side=16):
    """Bounding boxes (px) of pure-red drawings (detection rectangles).
    Tiny components (the red 'room'/'door' text labels) are filtered out."""
    import numpy as np
    from PIL import Image
    im = np.asarray(Image.open(png_path).convert("RGB")).astype(int)
    mask = (im[:, :, 0] > 170) & (im[:, :, 1] < 90) & (im[:, :, 2] < 90)
    h, w = mask.shape
    seen = np.zeros_like(mask, dtype=bool)
    boxes = []
    ys, xs = np.nonzero(mask)
    px = set(zip(ys.tolist(), xs.tolist()))
    for start in list(px):
        if seen[start]:
            continue
        stack = [start]
        x0 = y0 = 10 ** 9
        x1 = y1 = -1
        seen[start] = True
        while stack:
            cy, cx = stack.pop()
            x0, x1 = min(x0, cx), max(x1, cx)
            y0, y1 = min(y0, cy), max(y1, cy)
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    ny, nx = cy + dy, cx + dx
                    if 0 <= ny < h and 0 <= nx < w and mask[ny, nx] \
                            and not seen[ny, nx]:
                        seen[ny, nx] = True
                        stack.append((ny, nx))
        if (x1 - x0) >= min_side and (y1 - y0) >= min_side:
            boxes.append((x0, y0, x1, y1))
    return boxes, (w, h)


def red_rects(png_path, min_len=22):
    """Rectangle recovery robust to TOUCHING boxes: find long horizontal and
    vertical red line segments, then pair them into axis-aligned rectangles.
    (Connected-component bboxes merge adjacent detections; line pairing
    doesn't.)"""
    import numpy as np
    from PIL import Image
    im = np.asarray(Image.open(png_path).convert("RGB")).astype(int)
    mask = (im[:, :, 0] > 170) & (im[:, :, 1] < 90) & (im[:, :, 2] < 90)
    h, w = mask.shape

    def runs_1d(line, min_len):
        out = []
        start = None
        for i, v in enumerate(line):
            if v and start is None:
                start = i
            elif not v and start is not None:
                if i - start >= min_len:
                    out.append((start, i - 1))
                start = None
        if start is not None and len(line) - start >= min_len:
            out.append((start, len(line) - 1))
        return out

    # horizontal segments grouped into edges (thick lines collapse)
    hsegs = []
    for y in range(h):
        for x0, x1 in runs_1d(mask[y], min_len):
            hsegs.append([x0, x1, y])
    hedges = []
    for s in sorted(hsegs, key=lambda s: (s[2], s[0])):
        for e in hedges:
            if abs(e[2] - s[2]) <= 3 and abs(e[0] - s[0]) <= 6 \
                    and abs(e[1] - s[1]) <= 6:
                e[2] = (e[2] + s[2]) / 2
                break
        else:
            hedges.append(list(s))
    vsegs = []
    for x in range(w):
        for y0, y1 in runs_1d(mask[:, x], min_len):
            vsegs.append([y0, y1, x])
    vedges = []
    for s in sorted(vsegs, key=lambda s: (s[2], s[0])):
        for e in vedges:
            if abs(e[2] - s[2]) <= 3 and abs(e[0] - s[0]) <= 6 \
                    and abs(e[1] - s[1]) <= 6:
                e[2] = (e[2] + s[2]) / 2
                break
        else:
            vedges.append(list(s))

    # pair top+bottom edges sharing the x-range, confirmed by side edges
    rects = []
    for i, t in enumerate(hedges):
        for b in hedges[i + 1:]:
            if abs(t[0] - b[0]) > 8 or abs(t[1] - b[1]) > 8:
                continue
            y0, y1 = min(t[2], b[2]), max(t[2], b[2])
            if y1 - y0 < min_len * 0.6:
                continue
            sides = sum(1 for v in vedges
                        if (abs(v[2] - t[0]) <= 8 or abs(v[2] - t[1]) <= 8)
                        and v[0] <= y0 + 8 and v[1] >= y1 - 8)
            if sides >= 1:
                rects.append((int(min(t[0], b[0])), int(y0),
                              int(max(t[1], b[1])), int(y1)))
    # dedupe near-identical rects
    uniq = []
    for r in rects:
        if not any(all(abs(r[k] - u[k]) <= 6 for k in range(4)) for u in uniq):
            uniq.append(r)
    return uniq, (w, h)


def _union(boxes):
    return (min(b[0] for b in boxes), min(b[1] for b in boxes),
            max(b[2] for b in boxes), max(b[3] for b in boxes))


def extract(rooms_overlay, openings_overlay, EW, ED):
    """Overlays → rooms/doors/windows boxes in METRES.
    Calibration: the union of the detected room boxes spans the building
    interior, which the printed chains say is EW × ED metres."""
    # NB: component bboxes MERGE touching detections (~80 % recovery on the
    # user's plan: 9 rooms ok, 8/10 doors, 2/4 windows). red_rects() is an
    # experimental line-pairing recovery — currently buggy, do not use yet.
    # The CLEAN fix is a custom HF Space returning raw JSON coordinates
    # instead of annotated images (see CLAUDE.md, next session).
    rb, _ = red_boxes(rooms_overlay, min_side=28)
    ob, _ = red_boxes(openings_overlay, min_side=14)
    if not rb:
        raise RuntimeError("no room boxes detected in the overlay")
    ux0, uy0, ux1, uy1 = _union(rb)
    sx = EW / (ux1 - ux0)
    sy = ED / (uy1 - uy0)

    def to_m(b):
        return [round((b[0] - ux0) * sx, 2), round((b[1] - uy0) * sy, 2),
                round((b[2] - ux0) * sx, 2), round((b[3] - uy0) * sy, 2)]

    rooms = [to_m(b) for b in rb]
    # openings overlay: windows hug the hull, doors are interior
    doors, windows = [], []
    for b in ob:
        m = to_m(b)
        cx, cy = (m[0] + m[2]) / 2, (m[1] + m[3]) / 2
        near_hull = min(cx, EW - cx) < 0.35 or min(cy, ED - cy) < 0.35
        (windows if near_hull else doors).append(m)
    return {"rooms": rooms, "doors": doors, "windows": windows,
            "envelope": [EW, ED]}


def main():
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("detect")
    d.add_argument("image")
    d.add_argument("--ew", type=float, required=True,
                   help="envelope width in metres (from printed chains)")
    d.add_argument("--ed", type=float, required=True,
                   help="envelope depth in metres")
    d.add_argument("-o", "--out", default=None)
    args = ap.parse_args()

    tok = None
    tp = os.path.expanduser("~/.cache/plan_to_image/hf_token")
    if os.path.exists(tp):
        tok = open(tp).read().strip()
    paths, counts = detect(args.image, hf_token=tok)
    print(f"· space counts: {counts}")
    if len(paths) < 2:
        sys.exit("expected 2 overlays (rooms + openings)")
    seg = extract(paths[0], paths[1], args.ew, args.ed)
    out = args.out or os.path.splitext(args.image)[0] + ".seg.json"
    json.dump(seg, open(out, "w"), indent=2)
    print(f"✓ seg → {out}")
    print(f"  rooms   : {len(seg['rooms'])}")
    for b in seg["doors"]:
        print(f"  door    : {b}")
    for b in seg["windows"]:
        print(f"  window  : {b}")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""plan_audit — contrôle de fidélité SYSTÉMATIQUE d'un plan.json contre l'image source.

Superpose le modèle (pièces, portes, fenêtres, meubles) sur l'image du plan,
calé automatiquement sur l'enveloppe des murs (plus grande composante sombre).
Tout écart saute aux yeux : c'est l'étape OBLIGATOIRE avant de montrer un 3D.

    python3 tools/plan_audit.py web/plans/apartment_2br.json "work/_plan/Plan appartement.jpg" \
        -o work/_render/audit_overlay.png

Zéro dépendance hors Pillow. Fonctionne pour n'importe quel plan raster dont les
murs sont le plus gros trait sombre de l'image (poché classique).
"""
import argparse
import json
import sys
from collections import deque

from PIL import Image, ImageDraw

DOORW, WINW = 0.9, 1.4


def detect_wall_bbox(im, dark=110, scale_to=420):
    """BBox pixel des murs.

    1) Critère couleur : poché bleu nuit (sombre ET bleu dominant, b−r ≥ 25),
       ce qui exclut les lignes de cotes et le texte (gris neutres).
    2) Repli : plus grande composante connexe sombre (plans N&B).
    """
    w, h = im.size
    px = im.load()
    x1 = y1 = 10 ** 9
    x2 = y2 = -1
    n = 0
    for y in range(0, h, 2):
        for x in range(0, w, 2):
            r, g, b = px[x, y][:3]
            if r < dark and g < dark and b - r >= 25:
                n += 1
                x1, y1 = min(x1, x), min(y1, y)
                x2, y2 = max(x2, x), max(y2, y)
    if n > 500:
        return x1, y1, x2 + 1, y2 + 1

    # repli N&B : plus grande composante connexe sombre
    f = max(1, round(w / scale_to))
    small = im.convert("L").resize((w // f, h // f))
    sp = small.load()
    sw, sh = small.size
    seen = [[False] * sw for _ in range(sh)]
    best, best_a = None, 0
    for y0 in range(sh):
        for x0 in range(sw):
            if seen[y0][x0] or sp[x0, y0] >= dark:
                continue
            q = deque([(x0, y0)])
            seen[y0][x0] = True
            x1a, y1a, x2a, y2a = x0, y0, x0, y0
            while q:
                x, y = q.popleft()
                x1a, y1a = min(x1a, x), min(y1a, y)
                x2a, y2a = max(x2a, x), max(y2a, y)
                for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    nx, ny = x + dx, y + dy
                    if 0 <= nx < sw and 0 <= ny < sh and not seen[ny][nx] and sp[nx, ny] < dark:
                        seen[ny][nx] = True
                        q.append((nx, ny))
            a = (x2a - x1a) * (y2a - y1a)
            if a > best_a:
                best_a, best = a, (x1a, y1a, x2a, y2a)
    if not best:
        sys.exit("✗ aucun mur détecté (ajuster --dark)")
    bx1, by1, bx2, by2 = (v * f for v in best)
    return bx1, by1, bx2 + f, by2 + f


def model_bbox(model):
    xs, ys = [], []
    for r in model["rooms"]:
        if r.get("rect"):
            x0, y0, x1, y1 = r["rect"]
            xs += [x0, x1]
            ys += [y0, y1]
    return min(xs), min(ys), max(xs), max(ys)


def edge(rect, wall):
    x0, y0, x1, y1 = rect
    return {"N": (x0, y0, x1, y0), "S": (x0, y1, x1, y1),
            "W": (x0, y0, x0, y1), "E": (x1, y0, x1, y1)}[wall]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("plan_json")
    ap.add_argument("image")
    ap.add_argument("-o", "--out", default="audit_overlay.png")
    ap.add_argument("--dark", type=int, default=110)
    args = ap.parse_args()

    model = json.load(open(args.plan_json))
    im = Image.open(args.image).convert("RGB")
    px1, py1, px2, py2 = detect_wall_bbox(im, args.dark)
    mx1, my1, mx2, my2 = model_bbox(model)
    sx = (px2 - px1) / (mx2 - mx1)
    sy = (py2 - py1) / (my2 - my1)
    X = lambda x: px1 + (x - mx1) * sx
    Y = lambda y: py1 + (y - my1) * sy

    ov = im.copy()
    dr = ImageDraw.Draw(ov, "RGBA")
    lw = max(2, round(im.size[0] / 700))

    print(f"calage murs : image ({px1},{py1})→({px2},{py2})  "
          f"modèle {mx2-mx1:.2f}×{my2-my1:.2f} m  échelle {sx:.1f}/{sy:.1f} px/m")
    for r in model["rooms"]:
        if not r.get("rect"):
            continue
        x0, y0, x1, y1 = r["rect"]
        dr.rectangle([X(x0), Y(y0), X(x1), Y(y1)], outline=(220, 30, 30, 255), width=lw)
        dr.text((X(x0) + 4, Y(y0) + 3), r["name"], fill=(220, 30, 30, 255))
        info = [f'{r["name"]:14s} {x1-x0:.2f}×{y1-y0:.2f}']
        for o in r.get("doors", []):
            ax, ay, bx, by = edge(r["rect"], o["wall"])
            L = ((bx - ax) ** 2 + (by - ay) ** 2) ** 0.5
            t0 = max(0.0, o["at"] - DOORW / L / 2)
            t1 = min(1.0, o["at"] + DOORW / L / 2)
            dr.line([X(ax + (bx - ax) * t0), Y(ay + (by - ay) * t0),
                     X(ax + (bx - ax) * t1), Y(ay + (by - ay) * t1)],
                    fill=(0, 170, 60, 255), width=lw * 3)
            info.append(f'porte {o["wall"]}@{o["at"]:.2f}')
        for o in r.get("windows", []):
            ax, ay, bx, by = edge(r["rect"], o["wall"])
            L = ((bx - ax) ** 2 + (by - ay) ** 2) ** 0.5
            t0 = max(0.0, o["at"] - WINW / L / 2)
            t1 = min(1.0, o["at"] + WINW / L / 2)
            dr.line([X(ax + (bx - ax) * t0), Y(ay + (by - ay) * t0),
                     X(ax + (bx - ax) * t1), Y(ay + (by - ay) * t1)],
                    fill=(30, 90, 230, 255), width=lw * 3)
            info.append(f'fenêtre {o["wall"]}@{o["at"]:.2f}')
        for f in r.get("furniture", []):
            fx0, fy0, fx1, fy1 = f["rect"]
            dr.rectangle([X(fx0), Y(fy0), X(fx1), Y(fy1)],
                         outline=(240, 140, 0, 255), width=lw)
        info += [f.get("item", "?") for f in r.get("furniture", [])]
        print("  " + " · ".join(info))

    e = model.get("entry")
    if e:
        if e["edge"] in "NS":
            y = py1 if e["edge"] == "N" else py2
            dr.line([X(e["x"] - DOORW / 2), y, X(e["x"] + DOORW / 2), y],
                    fill=(200, 0, 200, 255), width=lw * 3)
        else:
            x = px1 if e["edge"] == "W" else px2
            dr.line([x, Y(e["y"] - DOORW / 2), x, Y(e["y"] + DOORW / 2)],
                    fill=(200, 0, 200, 255), width=lw * 3)
        print(f'  entrée {e["edge"]}@{e.get("x", e.get("y"))}')

    ov.save(args.out)
    print(f"✓ overlay : {args.out}")


if __name__ == "__main__":
    main()

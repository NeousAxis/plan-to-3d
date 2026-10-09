#!/usr/bin/env python3
"""plan_openings — portes/fenêtres détectées par modèle spécialisé LOCAL (zéro compte).

YOLO entraîné sur CubiCasa5k (miroir HF karanjaWakaba, licence MIT), exécuté en
local. Les détections pixel sont converties en mètres via le calage murs de
plan_audit, puis SNAPPÉES sur le mur de pièce le plus proche du plan.json :
les portes/fenêtres ne sont plus jamais devinées à la main.

    tools/../work/_seg_venv/bin/python tools/plan_openings.py \
        web/plans/apartment_2br.json "work/_plan/Plan appartement.jpg" \
        -o web/plans/apartment_2br.json          # remplace les ouvertures

Sans -o : imprime seulement le diff (mode audit).
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from plan_audit import detect_wall_bbox, model_bbox, edge  # noqa: E402

DOOR_CLASSES = ("door",)          # 'door swing beside', 'doors', ...
WIN_CLASSES = ("window", "glass")  # 'window regular', 'glass', ...


def dets_from_file(path):
    """Détections issues de la page web publique (detect.html → detections.json).

    Zéro dépendance modèle : c'est LE chemin pour un utilisateur sans venv,
    la détection ayant tourné dans SON navigateur.
    """
    data = json.load(open(path))
    out = []
    for d in data.get("detections", []):
        if d.get("kind") not in ("door", "window"):
            continue
        x0, y0, x1, y1 = d["bbox"]
        out.append({"kind": d["kind"], "class": d.get("class", ""),
                    "conf": float(d.get("conf", 0.5)),
                    "cx": (x0 + x1) / 2, "cy": (y0 + y1) / 2,
                    "w": x1 - x0, "h": y1 - y0})
    return out


def load_detections(image_path, conf=0.25):
    from huggingface_hub import hf_hub_download
    from ultralytics import YOLO
    det = YOLO(hf_hub_download("karanjaWakaba/Yolo_detection_cubicasa", "weights/best.pt"))
    r = det.predict(image_path, conf=conf, imgsz=1024, verbose=False)[0]
    out = []
    for b in r.boxes:
        c = det.names[int(b.cls)].lower()
        x0, y0, x1, y1 = [float(v) for v in b.xyxy[0]]
        kind = ("door" if any(k in c for k in DOOR_CLASSES)
                else "window" if any(k in c for k in WIN_CLASSES) else None)
        if kind:
            out.append({"kind": kind, "class": c, "conf": float(b.conf),
                        "cx": (x0 + x1) / 2, "cy": (y0 + y1) / 2,
                        "w": x1 - x0, "h": y1 - y0})
    return out


def dedupe(dets, tol=18):
    """Fusionne les détections du même type quasi superposées (garde la + sûre)."""
    dets = sorted(dets, key=lambda d: -d["conf"])
    kept = []
    for d in dets:
        if any(k["kind"] == d["kind"] and abs(k["cx"] - d["cx"]) < tol
               and abs(k["cy"] - d["cy"]) < tol for k in kept):
            continue
        kept.append(d)
    return kept


def wall_overlap(box, rect, wall, margin=0.20):
    """Recouvrement (m) entre la bbox détectée et un mur qui la traverse.

    Retourne (longueur de recouvrement, position `at` du centre du recouvrement)
    ou None si le mur ne passe pas dans la bbox (à `margin` près).
    """
    bx0, by0, bx1, by1 = box
    ax, ay, bx, by = edge(rect, wall)
    if ay == by:  # mur horizontal à y=ay
        if not (by0 - margin <= ay <= by1 + margin):
            return None
        o0, o1 = max(bx0, min(ax, bx)), min(bx1, max(ax, bx))
        if o1 - o0 < 0.25:
            return None
        t = ((o0 + o1) / 2 - min(ax, bx)) / abs(bx - ax)
        return o1 - o0, t
    else:         # mur vertical à x=ax
        if not (bx0 - margin <= ax <= bx1 + margin):
            return None
        o0, o1 = max(by0, min(ay, by)), min(by1, max(ay, by))
        if o1 - o0 < 0.25:
            return None
        t = ((o0 + o1) / 2 - min(ay, by)) / abs(by - ay)
        return o1 - o0, t


def gap_fraction(im, X, Y, rect, wall, t0, t1):
    """Fraction de l'intervalle [t0,t1] du mur SANS poché bleu nuit dans l'image.

    Une vraie ouverture (porte/fenêtre) = le mur dessiné est interrompu là.
    C'est le départage image-vérité entre le mur de l'ouverture et le mur
    le long duquel le battant est dessiné.
    """
    ax, ay, bx, by = edge(rect, wall)
    px = im.load()
    w, h = im.size
    n = gaps = 0
    for i in range(13):
        t = t0 + (t1 - t0) * i / 12
        cx, cy = X(ax + (bx - ax) * t), Y(ay + (by - ay) * t)
        navy = False
        for ox in (-7, -4, -2, 0, 2, 4, 7):
            for oy in (-7, -4, -2, 0, 2, 4, 7):
                x, y = int(cx + ox), int(cy + oy)
                if 0 <= x < w and 0 <= y < h:
                    r, g, b = px[x, y][:3]
                    if r < 110 and g < 110 and b - r >= 25:
                        navy = True
        n += 1
        gaps += 0 if navy else 1
    return gaps / max(n, 1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("plan_json")
    ap.add_argument("image")
    ap.add_argument("-o", "--out", help="écrit le plan.json mis à jour (sinon dry-run)")
    ap.add_argument("--conf", type=float, default=0.25)
    ap.add_argument("--snap", type=float, default=0.55, help="distance max de snap (m)")
    ap.add_argument("--detections",
                    help="detections.json de la page web (detect.html) : "
                         "aucun modèle local requis")
    args = ap.parse_args()

    from PIL import Image
    model = json.load(open(args.plan_json))
    im = Image.open(args.image).convert("RGB")
    px1, py1, px2, py2 = detect_wall_bbox(im)
    mx1, my1, mx2, my2 = model_bbox(model)
    sx = (px2 - px1) / (mx2 - mx1)
    sy = (py2 - py1) / (my2 - my1)
    Xpx = lambda x: px1 + (x - mx1) * sx
    Ypx = lambda y: py1 + (y - my1) * sy

    dets = dedupe(dets_from_file(args.detections) if args.detections
                  else load_detections(args.image, args.conf))
    rooms = [r for r in model["rooms"] if r.get("rect")]
    prev_doors = {id(r): list(r.get("doors", [])) for r in rooms}
    for r in rooms:
        r["doors"], r["windows"] = [], []
    entry = None
    placed = []   # (kind, x_m, y_m, conf, room, key, o) pour dédoublonner en mètres

    for d in dets:
        box = (mx1 + (d["cx"] - d["w"] / 2 - px1) / sx,
               my1 + (d["cy"] - d["h"] / 2 - py1) / sy,
               mx1 + (d["cx"] + d["w"] / 2 - px1) / sx,
               my1 + (d["cy"] + d["h"] / 2 - py1) / sy)
        mx, my = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
        # mur retenu = celui qui traverse la bbox ET dont le poché est INTERROMPU
        # là (gap dans le dessin = la vraie ouverture, pas le battant)
        best = None
        for r in rooms:
            for wl in "NSWE":
                ov = wall_overlap(box, r["rect"], wl, margin=0.4)
                if not ov:
                    continue
                L = ((r["rect"][2] - r["rect"][0])
                     if wl in "NS" else (r["rect"][3] - r["rect"][1]))
                half = max(ov[0], 0.5) / 2 / L
                gf = gap_fraction(im, Xpx, Ypx, r["rect"], wl,
                                  max(ov[1] - half, 0), min(ov[1] + half, 1))
                score = gf * 3 + ov[0]
                if best is None or score > best[0]:
                    best = (score, r, wl, ov[1], gf)
        if best is None:
            # aucune paroi de pièce : porte d'entrée sur le hull ?
            if d["kind"] == "door" and (abs(my - my2) < 0.8 or abs(my - my1) < 0.8):
                entry = {"edge": "S" if abs(my - my2) < abs(my - my1) else "N",
                         "x": round(mx, 2)}
                print(f'  entrée détectée {entry["edge"]}@{entry["x"]} (conf {d["conf"]:.2f})')
            else:
                print(f'  ✗ {d["kind"]} ({mx:.2f},{my:.2f}) sur aucun mur, ignoré')
            continue
        _, r, wl, t, gf = best
        # porte posée sur le périmètre du bâtiment = entrée
        ax, ay, bx_, by_ = edge(r["rect"], wl)
        on_hull = (ay == by_ and (abs(ay - my1) < 0.05 or abs(ay - my2) < 0.05)) or \
                  (ax == bx_ and (abs(ax - mx1) < 0.05 or abs(ax - mx2) < 0.05))
        if d["kind"] == "door" and on_hull:
            entry = {"edge": "S" if abs(my - my2) < abs(my - my1) else "N", "x": round(mx, 2)}
            print(f'  entrée détectée {entry["edge"]}@{entry["x"]} (conf {d["conf"]:.2f})')
            continue
        # dédoublonnage EN MÈTRES : une même porte physique ne produit qu'une
        # ouverture (garde le candidat au plus grand trou de poché)
        dup = next((p for p in placed if p[0] == d["kind"]
                    and ((p[1] - mx) ** 2 + (p[2] - my) ** 2) ** 0.5 < 1.3), None)
        if dup:
            if gf <= dup[3]:
                continue
            dup[4][dup[5]].remove(dup[6])   # remplace l'ancien candidat
            placed.remove(dup)
        key = "doors" if d["kind"] == "door" else "windows"
        o = {"wall": wl, "at": round(min(max(t, 0.05), 0.95), 2)}
        if not any(x["wall"] == wl and abs(x["at"] - o["at"]) < 0.12 for x in r[key]):
            r[key].append(o)
            placed.append([d["kind"], mx, my, gf, r, key, o])
            print(f'  {r["name"]:14s} {d["kind"]:6s} {wl}@{o["at"]}  (conf {d["conf"]:.2f}, gap {gf:.2f})')

    # filet : une pièce sans porte détectée récupère ses portes précédentes
    for r in rooms:
        if not r["doors"] and prev_doors[id(r)]:
            r["doors"] = prev_doors[id(r)]
            print(f'  ⚠ {r["name"]} : aucune porte détectée, portes précédentes conservées')
    if entry:
        model["entry"] = entry
    if args.out:
        json.dump(model, open(args.out, "w"), indent=1, ensure_ascii=False)
        print(f"✓ ouvertures détectées écrites dans {args.out}")
    else:
        print("(dry-run : rien écrit, utiliser -o)")


if __name__ == "__main__":
    main()

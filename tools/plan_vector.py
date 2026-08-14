#!/usr/bin/env python3
"""plan_vector.py — vectorisation fidele d'un plan d'architecture raster.

Pur Python 3 : stdlib + Pillow. Deterministe.

Pipeline :
 1. masque des murs par couleur (bleu nuit)
 2. histogrammes de traversees fines -> axes X (murs verticaux) et Y (murs
    horizontaux) ; axes locaux supplementaires detectes sous les boites de
    portes (mur perce d'un trou avec jambages des deux cotes)
 3. inventaire des TROUS de murs par axe (runs non mures bornes par du mur) ;
    les trous chevauchant une boite de porte sont des ouvertures legitimes
 4. cellules = grille des axes ; frontiere separante si couverte
    majoritairement par mur + trous de portes legitimes + boites de detection
 5. pieces = union-find des cellules par frontieres ouvertes ; les cellules
    sous l'enveloppe basse des murs (porche/encoche sud) sont dehors
 6. polygones rectilignes aux faces interieures des murs (aucune erosion)
 7. portes/fenetres accrochees aux pieces adjacentes {wall, at} ; le span
    d'une porte est recadre sur sa boite detectee (battant+arc) : le trou
    peut depasser la boite (degagement dessine), pas le passage
 8. fenetres SANS detection : (a) trou borne d'un mur d'enveloppe non
    explique par une porte, (b) glyphe fenetre dans la bande du mur
    (recul local >= 2 px de la face interieure, baseline par segment)
"""
import argparse
import json
import statistics
import sys

from PIL import Image, ImageDraw

MAXT = 20            # epaisseur max d'un mur (px)
MINLEN = 28          # support cumule min pour qu'une colonne fasse un axe
GAPCOL = 3           # trous de colonnes toleres dans la bande d'un axe
SEP_FRAC = 0.45      # fraction couverte => frontiere separante
MAX_DOOR_HOLE = 110  # longueur max d'un trou de porte (px)
MAX_WIN_HOLE = 170   # longueur max d'un trou de fenetre (px)
MIN_ROOM_DIM = 12    # px
MIN_ROOM_AREA = 500  # px^2
MATCH_IOU = 0.30     # seuil pour compter une piece de reference "apariee"
BOX_TRIM = 4         # marge (px) pour recadrer un trou sur sa boite detectee
WIN_MIN = 20         # longueur min d'une fenetre inferee (px)
NOTCH_DEFICIT = 2    # recul min (px) de la face interieure = glyphe fenetre
DEDUP_AT = 0.08      # deux ouvertures a moins de 8% du meme mur = doublon


def is_wall(r, g, b):
    return r < 110 and g < 110 and (b - r) >= 25


def build_mask(img):
    w, h = img.size
    raw = img.tobytes()
    mask = []
    for y in range(h):
        row = bytearray(w)
        base = y * w * 3
        for x in range(w):
            i = base + 3 * x
            if is_wall(raw[i], raw[i + 1], raw[i + 2]):
                row[x] = 1
        mask.append(row)
    return mask


def histograms(mask, w, h):
    hx = [0] * w
    hy = [0] * h
    for y in range(h):
        row = mask[y]
        x = 0
        while x < w:
            if row[x]:
                x0 = x
                while x < w and row[x]:
                    x += 1
                if 2 <= x - x0 <= MAXT:
                    for c in range(x0, x):
                        hx[c] += 1
            else:
                x += 1
    for x in range(w):
        y = 0
        while y < h:
            if mask[y][x]:
                y0 = y
                while y < h and mask[y][x]:
                    y += 1
                if 2 <= y - y0 <= MAXT:
                    for r in range(y0, y):
                        hy[r] += 1
            else:
                y += 1
    return hx, hy


def axes_from_hist(hist):
    axes = []
    cur = None
    for i, v in enumerate(hist):
        if v >= MINLEN:
            if cur is not None and i - cur[-1][0] <= GAPCOL + 1:
                cur.append((i, v))
            else:
                if cur:
                    axes.append(cur)
                cur = [(i, v)]
    if cur:
        axes.append(cur)
    out = []
    for cl in axes:
        c0, c1 = cl[0][0], cl[-1][0]
        tot = sum(v for _, v in cl)
        center = sum(i * v for i, v in cl) / tot
        out.append({"c0": c0, "c1": c1, "c": int(round(center))})
    return out


class PlanVectorizer:
    def __init__(self, image_path, det_path):
        self.img = Image.open(image_path).convert("RGB")
        self.W, self.H = self.img.size
        self.mask = build_mask(self.img)
        with open(det_path) as f:
            dd = json.load(f)
        self.dets = dd.get("detections", [])
        self.doors = [d for d in self.dets if d["kind"] == "door"]
        self.windows = self._merge_windows(
            [d for d in self.dets if d["kind"] != "door"])
        self.blocker_boxes = [self._expand(d["bbox"], 3.0) for d in self.dets]

    @staticmethod
    def _expand(b, m):
        return [b[0] - m, b[1] - m, b[2] + m, b[3] + m]

    @staticmethod
    def _merge_windows(wins):
        boxes = [list(w["bbox"]) for w in wins]
        changed = True
        while changed:
            changed = False
            out = []
            while boxes:
                b = boxes.pop()
                merged = False
                for o in out:
                    if not (b[2] < o[0] or o[2] < b[0] or
                            b[3] < o[1] or o[3] < b[1]):
                        o[0] = min(o[0], b[0])
                        o[1] = min(o[1], b[1])
                        o[2] = max(o[2], b[2])
                        o[3] = max(o[3], b[3])
                        merged = True
                        changed = True
                        break
                if not merged:
                    out.append(b)
            boxes = out
        return [{"kind": "window", "bbox": b} for b in boxes]

    def in_blocker(self, x, y):
        for b in self.blocker_boxes:
            if b[0] <= x <= b[2] and b[1] <= y <= b[3]:
                return True
        return False

    # ------------------------------------------------------------ murs

    def wall_row(self, y, c0, c1):
        """Mur present ligne y dans les colonnes [c0,c1]."""
        if y < 0 or y >= self.H:
            return False
        row = self.mask[y]
        for c in range(max(0, c0), min(self.W, c1 + 1)):
            if row[c]:
                return True
        return False

    def wall_col(self, x, r0, r1):
        if x < 0 or x >= self.W:
            return False
        for r in range(max(0, r0), min(self.H, r1 + 1)):
            if self.mask[r][x]:
                return True
        return False

    # ------------------------------------------------------------ axes

    def compute_axes(self):
        hx, hy = histograms(self.mask, self.W, self.H)
        self.axx = axes_from_hist(hx)
        self.axy = axes_from_hist(hy)
        self._add_local_axes()
        self.XS = [a["c"] for a in self.axx]
        self.YS = [a["c"] for a in self.axy]

    def _hrun_len(self, x, y):
        if not self.mask[y][x]:
            return 0
        a = x
        while a > 0 and self.mask[y][a - 1]:
            a -= 1
        b = x
        while b < self.W - 1 and self.mask[y][b + 1]:
            b += 1
        return b - a + 1

    def _vrun_len(self, x, y):
        if not self.mask[y][x]:
            return 0
        a = y
        while a > 0 and self.mask[a - 1][x]:
            a -= 1
        b = y
        while b < self.H - 1 and self.mask[b + 1][x]:
            b += 1
        return b - a + 1

    def _add_local_axes(self):
        """Murs courts perces d'une porte (jambages minces des deux cotes de
        la boite) qui n'ont pas atteint MINLEN -> axes locaux. Un jambage est
        valide s'il contient un pixel de mur dont le run PERPENDICULAIRE est
        mince (sinon c'est un mur perpendiculaire qui croise la ligne)."""
        for det in self.doors:
            x0, y0, x1, y1 = det["bbox"]
            rows = []
            for y in range(int(y0) - 14, int(y1) + 15):
                if y < 1 or y >= self.H - 1:
                    continue
                left = any(self.mask[y][x] and
                           self._vrun_len(x, y) <= MAXT + 2
                           for x in range(max(0, int(x0) - 24), int(x0) - 2))
                right = any(self.mask[y][x] and
                            self._vrun_len(x, y) <= MAXT + 2
                            for x in range(int(x1) + 3,
                                           min(self.W, int(x1) + 25)))
                inside = sum(self.mask[y][c]
                             for c in range(max(0, int(x0) + 4),
                                            min(self.W, int(x1) - 3)))
                if left and right and inside <= (x1 - x0) * 0.3:
                    rows.append(y)
            self._maybe_add_axis(self.axy, rows, y0, y1)
            cols = []
            for x in range(int(x0) - 14, int(x1) + 15):
                if x < 1 or x >= self.W - 1:
                    continue
                top = any(self.mask[y][x] and
                          self._hrun_len(x, y) <= MAXT + 2
                          for y in range(max(0, int(y0) - 24), int(y0) - 2))
                bot = any(self.mask[y][x] and
                          self._hrun_len(x, y) <= MAXT + 2
                          for y in range(int(y1) + 3,
                                         min(self.H, int(y1) + 25)))
                inside = sum(self.mask[r][x]
                             for r in range(max(0, int(y0) + 4),
                                            min(self.H, int(y1) - 3)))
                if top and bot and inside <= (y1 - y0) * 0.3:
                    cols.append(x)
            self._maybe_add_axis(self.axx, cols, x0, x1)

    @staticmethod
    def _maybe_add_axis(axes, positions, b0, b1):
        if len(positions) < 3:
            return
        clusters = []
        cur = [positions[0]]
        for p in positions[1:]:
            if p - cur[-1] <= 2:
                cur.append(p)
            else:
                clusters.append(cur)
                cur = [p]
        clusters.append(cur)
        clusters = [cl for cl in clusters if len(cl) >= 3]
        if not clusters:
            return

        def key(cl):
            c = sum(cl) / len(cl)
            dist = max(b0 - 4 - c, c - b1 - 4, 0.0)
            return (dist, -len(cl))
        best = min(clusters, key=key)
        c = int(round(sum(best) / len(best)))
        for a in axes:
            if abs(a["c"] - c) <= 10:
                return
        axes.append({"c0": best[0], "c1": best[-1], "c": c})
        axes.sort(key=lambda a: a["c"])

    # ------------------------------------------------------------ trous

    def compute_holes(self):
        """Par axe : runs non mures bornes par du mur des deux cotes.
        holes_v[ia] = [(lo,hi)] le long de y ; holes_h[ja] le long de x."""
        self.holes_v = []
        for ax in self.axx:
            walled = [self.wall_row(y, ax["c0"] - 1, ax["c1"] + 1)
                      for y in range(self.H)]
            self.holes_v.append(self._bounded_runs(walled))
        self.holes_h = []
        for ay in self.axy:
            walled = [self.wall_col(x, ay["c0"] - 1, ay["c1"] + 1)
                      for x in range(self.W)]
            self.holes_h.append(self._bounded_runs(walled))
        # trous legitimes de portes (pour la separation des pieces)
        self.blocked_v = [[] for _ in self.axx]
        self.blocked_h = [[] for _ in self.axy]
        for det in self.doors:
            for orient, iax, lo, hi in self._holes_near_box(
                    det["bbox"], MAX_DOOR_HOLE):
                if orient == "V":
                    self.blocked_v[iax].append((lo, hi))
                else:
                    self.blocked_h[iax].append((lo, hi))

    @staticmethod
    def _bounded_runs(walled):
        runs = []
        n = len(walled)
        k = 0
        while k < n:
            if not walled[k]:
                r0 = k
                while k < n and not walled[k]:
                    k += 1
                r1 = k - 1
                if r0 > 0 and r1 < n - 1 and 6 <= r1 - r0 + 1 <= 250:
                    runs.append((r0, r1))
            else:
                k += 1
        return runs

    def _holes_near_box(self, bbox, maxlen):
        """Tous les trous bornes chevauchant la boite (etendue de 8px)."""
        bx0, by0, bx1, by1 = bbox
        found = []
        for ia, ax in enumerate(self.axx):
            if not (bx0 - 12 <= ax["c"] <= bx1 + 12):
                continue
            for lo, hi in self.holes_v[ia]:
                if hi - lo + 1 > maxlen:
                    continue
                ov = min(hi, by1 + 8) - max(lo, by0 - 8)
                if ov >= 5:
                    found.append(("V", ia, lo, hi))
        for ja, ay in enumerate(self.axy):
            if not (by0 - 12 <= ay["c"] <= by1 + 12):
                continue
            for lo, hi in self.holes_h[ja]:
                if hi - lo + 1 > maxlen:
                    continue
                ov = min(hi, bx1 + 8) - max(lo, bx0 - 8)
                if ov >= 5:
                    found.append(("H", ja, lo, hi))
        return found

    def find_opening(self, bbox, maxlen):
        """Meilleur trou pour une boite : recouvrement max, puis proximite."""
        bx0, by0, bx1, by1 = bbox
        best = None
        best_score = None
        for orient, iax, lo, hi in self._holes_near_box(bbox, maxlen):
            if orient == "V":
                b0, b1 = by0, by1
            else:
                b0, b1 = bx0, bx1
            ov = min(hi, b1 + 8) - max(lo, b0 - 8)
            dist = abs((lo + hi) / 2 - (b0 + b1) / 2)
            score = ov - 0.15 * dist
            if best_score is None or score > best_score:
                best_score = score
                best = (orient, iax, lo, hi)
        return best

    # ------------------------------------------------------------ cellules

    def compute_outside(self):
        w, h = self.W, self.H
        bottom = [0] * w
        for x in range(w):
            for y in range(h - 1, -1, -1):
                if self.mask[y][x]:
                    bottom[x] = y
                    break
        for b in self.blocker_boxes:
            for x in range(max(0, int(b[0])), min(w, int(b[2]) + 1)):
                if b[3] > bottom[x] and b[1] < bottom[x] + 60:
                    bottom[x] = max(bottom[x], int(min(b[3], h - 1)))
        nx, ny = len(self.XS) - 1, len(self.YS) - 1
        self.inside = {}
        for i in range(nx):
            for j in range(ny):
                x0, x1 = self.XS[i], self.XS[i + 1]
                y0, y1 = self.YS[j], self.YS[j + 1]
                cy = (y0 + y1) // 2
                cols = range(max(0, x0 + 3), min(w, x1 - 2))
                if not cols:
                    self.inside[(i, j)] = False
                    continue
                med_bottom = statistics.median([bottom[c] for c in cols])
                self.inside[(i, j)] = cy <= med_bottom + 4
        return self.inside

    @staticmethod
    def _in_intervals(ivs, p):
        for lo, hi in ivs:
            if lo <= p <= hi:
                return True
        return False

    def boundary_open(self):
        openb = {}
        nx, ny = len(self.XS) - 1, len(self.YS) - 1
        for ia in range(1, nx):
            ax = self.axx[ia]
            for j in range(ny):
                y0, y1 = self.YS[j], self.YS[j + 1]
                if y1 - y0 < 8:
                    openb[("V", ia, j)] = False
                    continue
                tot = cov = 0
                for y in range(y0 + 4, y1 - 3):
                    tot += 1
                    if self.wall_row(y, ax["c0"] - 1, ax["c1"] + 1) or \
                            self._in_intervals(self.blocked_v[ia], y):
                        cov += 1
                openb[("V", ia, j)] = (cov / tot if tot else 1.0) < SEP_FRAC
        for ja in range(1, ny):
            ay = self.axy[ja]
            for i in range(nx):
                x0, x1 = self.XS[i], self.XS[i + 1]
                if x1 - x0 < 8:
                    openb[("H", i, ja)] = False
                    continue
                tot = cov = 0
                for x in range(x0 + 4, x1 - 3):
                    tot += 1
                    if self.wall_col(x, ay["c0"] - 1, ay["c1"] + 1) or \
                            self._in_intervals(self.blocked_h[ja], x):
                        cov += 1
                openb[("H", i, ja)] = (cov / tot if tot else 1.0) < SEP_FRAC
        self.openb = openb

    def group_rooms(self):
        nx, ny = len(self.XS) - 1, len(self.YS) - 1
        parent = {}

        def find(a):
            while parent[a] != a:
                parent[a] = parent[parent[a]]
                a = parent[a]
            return a

        def union(a, b):
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[ra] = rb

        for i in range(nx):
            for j in range(ny):
                parent[(i, j)] = (i, j)
        for i in range(nx):
            for j in range(ny):
                if not self.inside[(i, j)]:
                    continue
                if i + 1 < nx and self.inside[(i + 1, j)] and \
                        self.openb.get(("V", i + 1, j)):
                    union((i, j), (i + 1, j))
                if j + 1 < ny and self.inside[(i, j + 1)] and \
                        self.openb.get(("H", i, j + 1)):
                    union((i, j), (i, j + 1))
        groups = {}
        for i in range(nx):
            for j in range(ny):
                if not self.inside[(i, j)]:
                    continue
                groups.setdefault(find((i, j)), []).append((i, j))
        self.roomcells = []
        self.roomof = {}
        for key in sorted(groups.keys()):
            cells = groups[key]
            x0 = min(self.XS[i] for i, _ in cells)
            x1 = max(self.XS[i + 1] for i, _ in cells)
            y0 = min(self.YS[j] for _, j in cells)
            y1 = max(self.YS[j + 1] for _, j in cells)
            if x1 - x0 < MIN_ROOM_DIM or y1 - y0 < MIN_ROOM_DIM:
                continue
            area = sum((self.XS[i + 1] - self.XS[i]) *
                       (self.YS[j + 1] - self.YS[j]) for i, j in cells)
            if area < MIN_ROOM_AREA:
                continue
            idx = len(self.roomcells)
            self.roomcells.append(cells)
            for c in cells:
                self.roomof[c] = idx

    # ------------------------------------------------------------ faces

    def face_v(self, ia, y0, y1, interior_east):
        ax = self.axx[ia]
        half = MAXT // 2 + 4
        c0 = max(0, max(ax["c0"] - 2, ax["c"] - half))
        c1 = min(self.W - 1, min(ax["c1"] + 2, ax["c"] + half))
        span = y1 - y0
        m = min(14, max(3, span // 3))
        vals = []
        for y in range(y0 + m, y1 - m + 1):
            cols = [c for c in range(c0, c1 + 1) if self.mask[y][c]]
            if not cols:
                continue
            vals.append(max(cols) + 1 if interior_east else min(cols))
        if vals:
            return int(statistics.median(vals))
        return ax["c1"] + 1 if interior_east else ax["c0"]

    def face_h(self, ja, x0, x1, interior_south):
        ay = self.axy[ja]
        half = MAXT // 2 + 4
        r0 = max(0, max(ay["c0"] - 2, ay["c"] - half))
        r1 = min(self.H - 1, min(ay["c1"] + 2, ay["c"] + half))
        span = x1 - x0
        m = min(14, max(3, span // 3))
        vals = []
        for x in range(x0 + m, x1 - m + 1):
            rows = [r for r in range(r0, r1 + 1) if self.mask[r][x]]
            if not rows:
                continue
            vals.append(max(rows) + 1 if interior_south else min(rows))
        if vals:
            return int(statistics.median(vals))
        return ay["c1"] + 1 if interior_south else ay["c0"]

    # ------------------------------------------------------------ polygones

    def trace_polygon(self, cells):
        cs = set(cells)
        edges = {}

        def add(a, b):
            edges.setdefault(a, []).append(b)

        for (i, j) in cs:
            if (i, j - 1) not in cs:
                add((i + 1, j), (i, j))
            if (i, j + 1) not in cs:
                add((i, j + 1), (i + 1, j + 1))
            if (i - 1, j) not in cs:
                add((i, j), (i, j + 1))
            if (i + 1, j) not in cs:
                add((i + 1, j + 1), (i + 1, j))

        loops = []
        used = set()
        for start in sorted(edges.keys()):
            for to in edges[start]:
                if (start, to) in used:
                    continue
                loop = [start]
                cur, nxt = start, to
                used.add((cur, nxt))
                broken = False
                while nxt != start:
                    loop.append(nxt)
                    cands = [t for t in edges.get(nxt, [])
                             if (nxt, t) not in used]
                    if not cands:
                        broken = True
                        break
                    if len(cands) == 1:
                        t = cands[0]
                    else:
                        dx, dy = nxt[0] - cur[0], nxt[1] - cur[1]
                        sdx = 0 if dx == 0 else (1 if dx > 0 else -1)
                        sdy = 0 if dy == 0 else (1 if dy > 0 else -1)
                        left = (sdy, -sdx)

                        def rank(t):
                            tdx, tdy = t[0] - nxt[0], t[1] - nxt[1]
                            s = (0 if tdx == 0 else (1 if tdx > 0 else -1),
                                 0 if tdy == 0 else (1 if tdy > 0 else -1))
                            if s == left:
                                return 0
                            if s == (sdx, sdy):
                                return 1
                            return 2
                        t = min(cands, key=rank)
                    used.add((nxt, t))
                    cur, nxt = nxt, t
                if not broken and len(loop) >= 4:
                    loops.append(loop)
        if not loops:
            return None
        loop = max(loops, key=len)
        simp = []
        n = len(loop)
        for k in range(n):
            a, b, c = loop[k - 1], loop[k], loop[(k + 1) % n]
            if (a[0] == b[0] == c[0]) or (a[1] == b[1] == c[1]):
                continue
            simp.append(b)
        return simp

    def polygon_pixels(self, loop):
        n = len(loop)
        coords = []
        for k in range(n):
            a, b = loop[k], loop[(k + 1) % n]
            if a[0] == b[0]:
                ia = a[0]
                y0 = self.YS[min(a[1], b[1])]
                y1 = self.YS[max(a[1], b[1])]
                south = b[1] > a[1]
                coords.append(("V", self.face_v(ia, y0, y1, south)))
            else:
                ja = a[1]
                x0 = self.XS[min(a[0], b[0])]
                x1 = self.XS[max(a[0], b[0])]
                east = b[0] > a[0]
                coords.append(("H", self.face_h(ja, x0, x1, not east)))
        pts = []
        for k in range(n):
            prev = coords[k - 1]
            cur = coords[k]
            if prev[0] == cur[0]:
                return None
            if cur[0] == "V":
                pts.append((cur[1], prev[1]))
            else:
                pts.append((prev[1], cur[1]))
        return pts

    # ------------------------------------------------------------ ouvertures

    def _attach_entry(self, rooms_out, kind, orient, iax, lo, hi):
        """Accroche une ouverture (orient,axe,span) aux pieces adjacentes."""
        c = (lo + hi) / 2
        touched = []
        if orient == "V":
            j = self._cell_index(self.YS, c)
            if j is None:
                return
            for di, wall in ((iax - 1, "E"), (iax, "W")):
                r = self.roomof.get((di, j))
                if r is not None:
                    touched.append((r, wall))
        else:
            i = self._cell_index(self.XS, c)
            if i is None:
                return
            for dj, wall in ((iax - 1, "S"), (iax, "N")):
                r = self.roomof.get((i, dj))
                if r is not None:
                    touched.append((r, wall))
        for r, wall in touched:
            room = rooms_out.get(r)
            if room is None:
                continue
            rx0, ry0, rx1, ry1 = room["rect"]
            if orient == "V":
                at = (c - ry0) / (ry1 - ry0) if ry1 > ry0 else 0.5
            else:
                at = (c - rx0) / (rx1 - rx0) if rx1 > rx0 else 0.5
            at = round(max(0.0, min(1.0, at)), 3)
            entry = {"wall": wall, "at": at,
                     "span_px": [round(lo, 1), round(hi, 1)],
                     "axis": orient}
            lst = room["doors"] if kind == "door" else room["windows"]
            if all(e["wall"] != wall or abs(e["at"] - at) > DEDUP_AT
                   for e in lst):
                lst.append(entry)

    def attach_openings(self, rooms_out):
        for kind, dets, maxlen in (("door", self.doors, MAX_DOOR_HOLE),
                                   ("window", self.windows, MAX_WIN_HOLE)):
            for det in dets:
                op = self.find_opening(det["bbox"], maxlen)
                if op is None:
                    continue
                orient, iax, lo, hi = op
                # le trou est recadre sur la boite (battant+arc) : la partie
                # du trou au-dela de la boite est du degagement dessine,
                # pas le passage lui-meme
                bx0, by0, bx1, by1 = det["bbox"]
                b0, b1 = (by0, by1) if orient == "V" else (bx0, bx1)
                tlo = max(lo, b0 - BOX_TRIM)
                thi = min(hi, b1 + BOX_TRIM)
                if thi - tlo >= 6:
                    lo, hi = tlo, thi
                self._attach_entry(rooms_out, kind, orient, iax, lo, hi)

    # --------------------------------------------- fenetres par les murs

    def _overlaps_door_box(self, orient, iax, lo, hi, margin=6):
        ax = (self.axx if orient == "V" else self.axy)[iax]
        for d in self.doors:
            bx0, by0, bx1, by1 = d["bbox"]
            if orient == "V":
                if (bx0 - margin <= ax["c"] <= bx1 + margin and
                        min(hi, by1 + margin) - max(lo, by0 - margin) >= 3):
                    return True
            else:
                if (by0 - margin <= ax["c"] <= by1 + margin and
                        min(hi, bx1 + margin) - max(lo, bx0 - margin) >= 3):
                    return True
        return False

    def _side_state(self, cell):
        """(has_room, is_inside) pour une cellule (absente = dehors)."""
        r = self.roomof.get(cell)
        return r is not None, self.inside.get(cell, False)

    def infer_windows(self, rooms_out):
        """Fenetres non couvertes par les detections, lues dans les murs.

        a) trou borne d'un mur d'enveloppe (piece d'un cote, dehors de
           l'autre), non explique par une boite de porte ;
        b) glyphe fenetre DANS la bande du mur : la face interieure du mur
           recule de >= NOTCH_DEFICIT px sur une longueur de fenetre
           (cas du mur nord de ce plan, ou la fenetre n'est pas un trou).
        """
        cands = []
        for ia in range(len(self.axx)):
            for lo, hi in self.holes_v[ia]:
                if WIN_MIN <= hi - lo + 1 <= MAX_WIN_HOLE:
                    cands.append(("V", ia, lo, hi))
        for ja in range(len(self.axy)):
            for lo, hi in self.holes_h[ja]:
                if WIN_MIN <= hi - lo + 1 <= MAX_WIN_HOLE:
                    cands.append(("H", ja, lo, hi))
        cands.extend(self._notch_windows())
        for orient, iax, lo, hi in cands:
            if self._overlaps_door_box(orient, iax, lo, hi):
                continue
            c = (lo + hi) / 2
            if orient == "V":
                j = self._cell_index(self.YS, c)
                if j is None:
                    continue
                near = self._side_state((iax - 1, j)), \
                    self._side_state((iax, j))
            else:
                i = self._cell_index(self.XS, c)
                if i is None:
                    continue
                near = self._side_state((i, iax - 1)), \
                    self._side_state((i, iax))
            (roomA, inA), (roomB, inB) = near
            # exactement un cote habite, l'autre franchement dehors
            if roomA == roomB or (roomA and inB) or (roomB and inA):
                continue
            self._attach_entry(rooms_out, "window", orient, iax, lo, hi)

    def _notch_windows(self):
        out = []
        for orient in ("H", "V"):
            axes = self.axy if orient == "H" else self.axx
            n = self.W if orient == "H" else self.H
            cells = self.XS if orient == "H" else self.YS
            for iax, ax in enumerate(axes):
                b0 = max(0, ax["c0"] - 2)
                b1 = min((self.H if orient == "H" else self.W) - 1,
                         ax["c1"] + 2)
                faces = {}
                for p in range(n):
                    if orient == "H":
                        rows = [r for r in range(b0, b1 + 1)
                                if self.mask[r][p]]
                    else:
                        rows = [r for r in range(b0, b1 + 1)
                                if self.mask[p][r]]
                    if rows:
                        faces[p] = (min(rows), max(rows))
                if len(faces) < 3 * WIN_MIN:
                    continue
                ps = sorted(faces)
                # runs de positions murees consecutives (tolerance 2px)
                runs = []
                cur = [ps[0]]
                for p in ps[1:]:
                    if p - cur[-1] <= 2:
                        cur.append(p)
                    else:
                        runs.append(cur)
                        cur = [p]
                runs.append(cur)
                for run in runs:
                    if len(run) < 3 * WIN_MIN:
                        continue
                    # baseline PAR SEGMENT de mur : un segment d'enveloppe
                    # decale de 2px par rapport a un autre n'est pas une
                    # fenetre, seule une deviation locale l'est
                    base_lo = statistics.median(faces[p][0] for p in run)
                    base_hi = statistics.median(faces[p][1] for p in run)
                    k = 0
                    while k < len(run):
                        p = run[k]
                        ci = self._cell_index(cells, p)
                        if ci is None:
                            k += 1
                            continue
                        if orient == "H":
                            (rA, iA) = self._side_state((ci, iax - 1))
                            (rB, iB) = self._side_state((ci, iax))
                        else:
                            (rA, iA) = self._side_state((iax - 1, ci))
                            (rB, iB) = self._side_state((iax, ci))
                        if rA and not rB and not iB:
                            deficit = faces[p][0] - base_lo   # face int = min
                            interior_min = True
                        elif rB and not rA and not iA:
                            deficit = base_hi - faces[p][1]   # face int = max
                            interior_min = False
                        else:
                            k += 1
                            continue
                        if deficit < NOTCH_DEFICIT:
                            k += 1
                            continue
                        j0 = k
                        while k < len(run):
                            p2 = run[k]
                            d2 = (faces[p2][0] - base_lo) if interior_min \
                                else (base_hi - faces[p2][1])
                            if d2 < NOTCH_DEFICIT:
                                break
                            k += 1
                        lo, hi = run[j0], run[k - 1]
                        L = hi - lo + 1
                        # borne des deux cotes par de la face normale
                        if (WIN_MIN <= L <= MAX_WIN_HOLE and
                                j0 > 0 and k < len(run)):
                            out.append((orient, iax, lo, hi))
        return out

    @staticmethod
    def _cell_index(coords, v):
        for k in range(len(coords) - 1):
            if coords[k] <= v < coords[k + 1]:
                return k
        return None

    # ------------------------------------------------------------ pipeline

    def run(self):
        self.compute_axes()
        self.compute_holes()
        self.compute_outside()
        self.boundary_open()
        self.group_rooms()
        rooms_map = {}
        for idx, cells in enumerate(self.roomcells):
            loop = self.trace_polygon(cells)
            if not loop:
                continue
            pts = self.polygon_pixels(loop)
            if not pts:
                continue
            xs = [p[0] for p in pts]
            ys = [p[1] for p in pts]
            rooms_map[idx] = {
                "id": "room_%d" % (idx + 1),
                "poly": [[int(x), int(y)] for x, y in pts],
                "rect": [min(xs), min(ys), max(xs), max(ys)],
                "area_px2": int(abs(shoelace(pts))),
                "doors": [],
                "windows": [],
            }
        self.attach_openings(rooms_map)
        self.infer_windows(rooms_map)
        rooms_out = sorted(rooms_map.values(), key=lambda r: -r["area_px2"])
        return rooms_out


# ---------------------------------------------------------------- geometrie

def shoelace(pts):
    s = 0.0
    n = len(pts)
    for k in range(n):
        x0, y0 = pts[k]
        x1, y1 = pts[(k + 1) % n]
        s += x0 * y1 - x1 * y0
    return s / 2.0


def clip_rect(pts, rect):
    x0, y0, x1, y1 = rect

    def clip(poly, inside, inter):
        out = []
        n = len(poly)
        for k in range(n):
            a, b = poly[k], poly[(k + 1) % n]
            ia, ib = inside(a), inside(b)
            if ia:
                out.append(a)
                if not ib:
                    out.append(inter(a, b))
            elif ib:
                out.append(inter(a, b))
        return out

    def ix(a, b, x):
        t = (x - a[0]) / (b[0] - a[0])
        return (x, a[1] + t * (b[1] - a[1]))

    def iy(a, b, y):
        t = (y - a[1]) / (b[1] - a[1])
        return (a[0] + t * (b[0] - a[0]), y)

    p = list(pts)
    for inside, inter in (
            (lambda q: q[0] >= x0, lambda a, b: ix(a, b, x0)),
            (lambda q: q[0] <= x1, lambda a, b: ix(a, b, x1)),
            (lambda q: q[1] >= y0, lambda a, b: iy(a, b, y0)),
            (lambda q: q[1] <= y1, lambda a, b: iy(a, b, y1))):
        if not p:
            return []
        p = clip(p, inside, inter)
    return p


# ---------------------------------------------------------------- metriques

def metrics(rooms_out, ref_path):
    with open(ref_path) as f:
        ref = json.load(f)
    refrooms = [r for r in ref["rooms"] if "rect" in r]
    if not rooms_out:
        print("METRICS rooms=0/%d iou=0.00 doors=0/10 windows=0/7"
              % len(refrooms))
        return 0, 0.0, 0, 0
    rx0 = min(r["rect"][0] for r in refrooms)
    ry0 = min(r["rect"][1] for r in refrooms)
    rx1 = max(r["rect"][2] for r in refrooms)
    ry1 = max(r["rect"][3] for r in refrooms)
    mx0 = min(r["rect"][0] for r in rooms_out)
    my0 = min(r["rect"][1] for r in rooms_out)
    mx1 = max(r["rect"][2] for r in rooms_out)
    my1 = max(r["rect"][3] for r in rooms_out)
    sx = (rx1 - rx0) / (mx1 - mx0)
    sy = (ry1 - ry0) / (my1 - my0)
    tx = rx0 - sx * mx0
    ty = ry0 - sy * my0

    polys = [[(sx * x + tx, sy * y + ty) for x, y in r["poly"]]
             for r in rooms_out]

    matched = 0
    ious = []
    doors_ok = 0
    wins_ok = 0
    for rr in refrooms:
        rect = rr["rect"]
        ra = (rect[2] - rect[0]) * (rect[3] - rect[1])
        best = (-1.0, None)
        for k, poly in enumerate(polys):
            pa = abs(shoelace(poly))
            cp = clip_rect(poly, rect)
            inter = abs(shoelace(cp)) if len(cp) >= 3 else 0.0
            iou = inter / (pa + ra - inter) if (pa + ra - inter) > 0 else 0.0
            if iou > best[0]:
                best = (iou, k)
        iou, k = best
        ok = iou >= MATCH_IOU
        if ok:
            matched += 1
            ious.append(iou)
        mine = rooms_out[k] if k is not None else None
        ddet = []
        for door in rr.get("doors", []):
            good = False
            if ok and mine is not None:
                for md in mine["doors"]:
                    if md["wall"] == door["wall"] and \
                            abs(md["at"] - door["at"]) <= 0.2:
                        good = True
                        break
            if good:
                doors_ok += 1
            ddet.append((door["wall"], door["at"], good))
        wdet = []
        for win in rr.get("windows", []):
            good = False
            if ok and mine is not None:
                for mw in mine["windows"]:
                    if mw["wall"] == win["wall"] and \
                            abs(mw["at"] - win["at"]) <= 0.2:
                        good = True
                        break
            if good:
                wins_ok += 1
            wdet.append((win["wall"], win["at"], good))
        sys.stderr.write(
            "  %-14s iou=%.2f -> %-8s doors=%s windows=%s\n" %
            (rr["name"], iou, mine["id"] if mine else "-", ddet, wdet))
    miou = sum(ious) / len(ious) if ious else 0.0
    print("METRICS rooms=%d/8 iou=%.2f doors=%d/10 windows=%d/7" %
          (matched, miou, doors_ok, wins_ok))
    return matched, miou, doors_ok, wins_ok


# ---------------------------------------------------------------- overlay

def draw_overlay(pv, rooms_out, path):
    im = pv.img.copy()
    dr = ImageDraw.Draw(im)
    for r in rooms_out:
        pts = [tuple(p) for p in r["poly"]]
        dr.line(pts + [pts[0]], fill=(220, 20, 20), width=3)
        cx = sum(p[0] for p in pts) / len(pts)
        cy = sum(p[1] for p in pts) / len(pts)
        dr.text((cx - 14, cy - 5), r["id"], fill=(220, 20, 20))
        for kind, col in (("doors", (0, 170, 0)), ("windows", (30, 60, 255))):
            for e in r[kind]:
                lo, hi = e["span_px"]
                rx0, ry0, rx1, ry1 = r["rect"]
                if e["axis"] == "V":
                    x = rx0 if e["wall"] == "W" else rx1
                    dr.line([(x, lo), (x, hi)], fill=col, width=5)
                else:
                    y = ry0 if e["wall"] == "N" else ry1
                    dr.line([(lo, y), (hi, y)], fill=col, width=5)
    im.save(path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("image")
    ap.add_argument("--detections", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--overlay")
    ap.add_argument("--ref")
    args = ap.parse_args()

    pv = PlanVectorizer(args.image, args.detections)
    rooms_out = pv.run()
    out = {
        "units": "px",
        "image": {"width": pv.W, "height": pv.H},
        "axes": {"x": pv.XS, "y": pv.YS},
        "rooms": rooms_out,
    }
    with open(args.out, "w") as f:
        json.dump(out, f, indent=1)
    if args.overlay:
        draw_overlay(pv, rooms_out, args.overlay)
    if args.ref:
        metrics(rooms_out, args.ref)


if __name__ == "__main__":
    main()

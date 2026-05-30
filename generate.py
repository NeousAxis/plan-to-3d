#!/usr/bin/env python3
"""
plan-to-3d generator.

Reads a building specification (JSON) describing walls, openings, a floor slab,
a roof and room labels, and produces:

  - model.glb          a binary glTF 3D model (importable in Blender, FreeCAD,
                       SketchUp Free, online viewers, phones, ...)
  - viewer.html        a self-contained interactive viewer (orbit/zoom/pan,
                       room labels) that opens with a double-click, no install
  - preview.png        an isometric preview render (only with --preview,
                       requires matplotlib)

The whole core (GLB + viewer) depends on the Python standard library only.

Usage:
  python3 generate.py building.json [--out OUT_DIR] [--preview] [--name NAME]

See schema.json for the building specification format.
"""

import argparse
import base64
import json
import math
import os
import struct
import sys

# ---------------------------------------------------------------------------
# Materials (baseColorFactor RGBA, alphaMode, doubleSided)
# ---------------------------------------------------------------------------
def _mat(color, rough=0.85, metal=0.0, alpha="OPAQUE", emissive=None):
    m = {"color": color, "roughness": rough, "metalness": metal, "alpha": alpha}
    if emissive:
        m["emissive"] = emissive
    return m


# Each material is a PBR bucket: baseColor RGBA + roughness/metalness, an
# optional emissive (for screens), and an alpha mode. Geometry is batched per
# material name, so adding instance colours would mean new buckets.
MATERIALS = {
    # --- shell ---
    "wall":       _mat([0.91, 0.90, 0.88, 1.0], rough=0.93),
    "slab":       _mat([0.62, 0.62, 0.64, 1.0], rough=0.95),
    "roof":       _mat([0.66, 0.26, 0.20, 1.0], rough=0.85),
    "glass":      _mat([0.60, 0.76, 0.90, 0.20], rough=0.05, alpha="BLEND"),
    "window":     _mat([0.60, 0.76, 0.90, 0.28], rough=0.05, alpha="BLEND"),
    "frame":      _mat([0.28, 0.29, 0.31, 1.0], rough=0.4, metal=0.7),
    "door":       _mat([0.56, 0.41, 0.27, 1.0], rough=0.55),
    # --- furniture / fixture families ---
    "wood":       _mat([0.55, 0.40, 0.27, 1.0], rough=0.6),
    "wood_light": _mat([0.80, 0.66, 0.47, 1.0], rough=0.55),
    "white":      _mat([0.92, 0.92, 0.93, 1.0], rough=0.5),
    "fabric":     _mat([0.42, 0.45, 0.52, 1.0], rough=0.96),
    "fabric_warm":_mat([0.66, 0.60, 0.52, 1.0], rough=0.96),
    "metal":      _mat([0.55, 0.56, 0.58, 1.0], rough=0.35, metal=0.9),
    "dark":       _mat([0.15, 0.16, 0.18, 1.0], rough=0.5),
    "screen":     _mat([0.04, 0.05, 0.07, 1.0], rough=0.2,
                       emissive=[0.05, 0.09, 0.15]),
    "stone":      _mat([0.82, 0.81, 0.78, 1.0], rough=0.65),  # worktops
    "concrete":   _mat([0.66, 0.66, 0.67, 1.0], rough=0.9),   # columns
    "plant":      _mat([0.33, 0.52, 0.29, 1.0], rough=0.85),
    "plant_dark": _mat([0.21, 0.39, 0.21, 1.0], rough=0.85),
    "pot":        _mat([0.42, 0.40, 0.38, 1.0], rough=0.8),
    "carpet":     _mat([0.48, 0.54, 0.60, 1.0], rough=1.0),
    "appliance":  _mat([0.80, 0.81, 0.83, 1.0], rough=0.3, metal=0.45),
    "sanitary":   _mat([0.95, 0.96, 0.97, 1.0], rough=0.25),
    "bed":        _mat([0.87, 0.85, 0.81, 1.0], rough=0.9),
    "stairs":     _mat([0.74, 0.74, 0.76, 1.0], rough=0.8),
}

# Per-type defaults: footprint [w, d, h] in metres plus the component
# "builder" that renders it (composed multi-part furniture, not a flat box).
# A few simple items pin a "material". Unknown types fall back to "generic".
FURNITURE_TYPES = {
    # beds
    "bed":             {"size": [1.6, 2.0, 0.5],  "builder": "bed"},
    "single_bed":      {"size": [0.9, 2.0, 0.5],  "builder": "bed"},
    "double_bed":      {"size": [1.6, 2.0, 0.5],  "builder": "bed"},
    "king_bed":        {"size": [1.8, 2.0, 0.5],  "builder": "bed"},
    # seating
    "sofa":            {"size": [2.2, 0.95, 0.8], "builder": "sofa"},
    "sofa_l":          {"size": [2.6, 2.0, 0.8],  "builder": "sofa"},
    "armchair":        {"size": [0.9, 0.9, 0.8],  "builder": "armchair"},
    "chair":           {"size": [0.5, 0.5, 0.9],  "builder": "chair"},
    "office_chair":    {"size": [0.62, 0.62, 1.1],"builder": "office_chair"},
    "stool":           {"size": [0.4, 0.4, 0.75], "builder": "stool"},
    # tables / desks
    "coffee_table":    {"size": [1.1, 0.6, 0.4],  "builder": "table"},
    "side_table":      {"size": [0.5, 0.5, 0.5],  "builder": "table"},
    "dining_table":    {"size": [1.6, 0.9, 0.74], "builder": "table_chairs"},
    "table":           {"size": [1.4, 0.8, 0.74], "builder": "table"},
    "conference_table":{"size": [3.6, 1.2, 0.74], "builder": "table_chairs"},
    "meeting_table":   {"size": [2.4, 1.1, 0.74], "builder": "table_chairs"},
    "round_table":     {"size": [1.2, 1.2, 0.74], "builder": "round_table"},
    "desk":            {"size": [1.4, 0.7, 0.74], "builder": "desk"},
    "office_desk":     {"size": [1.6, 0.8, 0.74], "builder": "office_desk"},
    "desk_bench":      {"size": [3.2, 1.6, 0.74], "builder": "desk_bench"},
    "workstation":     {"size": [3.2, 1.6, 0.74], "builder": "desk_bench"},
    "nightstand":      {"size": [0.45, 0.4, 0.5], "builder": "box", "material": "wood"},
    # storage / kitchen
    "counter":         {"size": [2.0, 0.6, 0.9],  "builder": "kitchen"},
    "kitchen_counter": {"size": [2.0, 0.6, 0.9],  "builder": "kitchen"},
    "kitchenette":     {"size": [2.4, 0.6, 0.9],  "builder": "kitchen"},
    "island":          {"size": [2.0, 1.0, 0.9],  "builder": "kitchen"},
    "cabinet":         {"size": [0.9, 0.45, 1.1], "builder": "cabinet"},
    "shelving":        {"size": [1.2, 0.4, 1.8],  "builder": "cabinet"},
    "bookshelf":       {"size": [1.0, 0.3, 1.8],  "builder": "cabinet"},
    "wardrobe":        {"size": [1.5, 0.6, 2.0],  "builder": "cabinet"},
    "closet":          {"size": [1.5, 0.6, 2.0],  "builder": "cabinet"},
    "lockers":         {"size": [1.2, 0.5, 1.9],  "builder": "cabinet"},
    "credenza":        {"size": [1.6, 0.45, 0.8], "builder": "cabinet"},
    # appliances / electronics
    "fridge":          {"size": [0.7, 0.7, 1.8],  "builder": "box", "material": "appliance"},
    "stove":           {"size": [0.6, 0.6, 0.9],  "builder": "box", "material": "appliance"},
    "oven":            {"size": [0.6, 0.6, 0.9],  "builder": "box", "material": "appliance"},
    "dishwasher":      {"size": [0.6, 0.6, 0.85], "builder": "box", "material": "appliance"},
    "washing_machine": {"size": [0.6, 0.6, 0.85], "builder": "box", "material": "appliance"},
    "tv":              {"size": [1.3, 0.08, 0.75],"builder": "tv"},
    "screen":          {"size": [1.6, 0.08, 0.9], "builder": "tv"},
    # sanitary
    "sink":            {"size": [0.6, 0.45, 0.85],"builder": "box", "material": "sanitary"},
    "kitchen_sink":    {"size": [0.8, 0.5, 0.9],  "builder": "box", "material": "sanitary"},
    "toilet":          {"size": [0.4, 0.65, 0.4], "builder": "box", "material": "sanitary"},
    "bathtub":         {"size": [1.7, 0.75, 0.55],"builder": "box", "material": "sanitary"},
    "shower":          {"size": [0.9, 0.9, 0.05], "builder": "box", "material": "sanitary"},
    "bidet":           {"size": [0.4, 0.55, 0.4], "builder": "box", "material": "sanitary"},
    # misc
    "plant":           {"size": [0.5, 0.5, 1.2],  "builder": "plant"},
    "plant_large":     {"size": [0.7, 0.7, 1.7],  "builder": "plant"},
    "column":          {"size": [0.4, 0.4, 2.7],  "builder": "column"},
    "partition":       {"size": [1.6, 0.06, 1.8], "builder": "box", "material": "glass"},
    "stairs":          {"size": [1.0, 3.0, 2.7],  "builder": "stairs"},
    "rug":             {"size": [2.0, 1.5, 0.02], "builder": "box", "material": "carpet"},
    "carpet":          {"size": [2.0, 1.5, 0.02], "builder": "box", "material": "carpet"},
    "generic":         {"size": [1.0, 1.0, 0.8],  "builder": "box", "material": "wood"},
}

EPS = 1e-6


# ---------------------------------------------------------------------------
# Geometry accumulation. Coordinates are emitted in glTF axes:
#   X = plan x, Y = height (up), Z = plan y
# We build vertices directly as (px, height, py).
# ---------------------------------------------------------------------------
class MeshBuilder:
    def __init__(self):
        # material name -> {"positions": [...], "normals": [...], "indices": [...]}
        self.groups = {name: {"positions": [], "normals": [], "indices": []}
                       for name in MATERIALS}

    def _add_face(self, material, verts):
        """verts: list of 3 or 4 (x,y,z) points, planar, defining a polygon."""
        g = self.groups[material]
        base = len(g["positions"])
        n = _normal(verts[0], verts[1], verts[2])
        for v in verts:
            g["positions"].append([float(v[0]), float(v[1]), float(v[2])])
            g["normals"].append(n)
        if len(verts) == 3:
            g["indices"] += [base, base + 1, base + 2]
        else:  # quad -> 2 triangles
            g["indices"] += [base, base + 1, base + 2,
                             base, base + 2, base + 3]

    def add_quad(self, material, p0, p1, p2, p3):
        self._add_face(material, [p0, p1, p2, p3])

    def add_tri(self, material, p0, p1, p2):
        self._add_face(material, [p0, p1, p2])

    def add_box8(self, material, c):
        """c: 8 corners. 0-3 bottom ring, 4-7 top ring (4 above 0, etc.)."""
        a0, a1, a2, a3, b0, b1, b2, b3 = c
        self.add_quad(material, a0, a3, a2, a1)   # bottom
        self.add_quad(material, b0, b1, b2, b3)   # top
        self.add_quad(material, a0, a1, b1, b0)
        self.add_quad(material, a1, a2, b2, b1)
        self.add_quad(material, a2, a3, b3, b2)
        self.add_quad(material, a3, a0, b0, b3)


def _normal(p0, p1, p2):
    ux, uy, uz = p1[0] - p0[0], p1[1] - p0[1], p1[2] - p0[2]
    vx, vy, vz = p2[0] - p0[0], p2[1] - p0[1], p2[2] - p0[2]
    nx, ny, nz = uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx
    ln = math.sqrt(nx * nx + ny * ny + nz * nz)
    if ln < EPS:
        return [0.0, 1.0, 0.0]
    return [nx / ln, ny / ln, nz / ln]


def oriented_box(mesh, material, start2d, end2d, thickness, z0, z1):
    """Extrude a wall segment (2D start->end) with thickness between z0 and z1."""
    sx, sy = start2d
    ex, ey = end2d
    dx, dy = ex - sx, ey - sy
    ln = math.hypot(dx, dy)
    if ln < EPS or (z1 - z0) < EPS or thickness < EPS:
        return
    ux, uy = dx / ln, dy / ln
    # perpendicular in plan, scaled to half thickness
    px, py = uy * (thickness / 2.0), -ux * (thickness / 2.0)
    # plan corners
    A = (sx + px, sy + py)
    B = (ex + px, ey + py)
    C = (ex - px, ey - py)
    D = (sx - px, sy - py)
    # glTF coords (x, height, y)
    corners = [
        (A[0], z0, A[1]), (B[0], z0, B[1]), (C[0], z0, C[1]), (D[0], z0, D[1]),
        (A[0], z1, A[1]), (B[0], z1, B[1]), (C[0], z1, C[1]), (D[0], z1, D[1]),
    ]
    mesh.add_box8(material, corners)


def aabb_box(mesh, material, minx, miny, maxx, maxy, z0, z1):
    corners = [
        (minx, z0, miny), (maxx, z0, miny), (maxx, z0, maxy), (minx, z0, maxy),
        (minx, z1, miny), (maxx, z1, miny), (maxx, z1, maxy), (minx, z1, maxy),
    ]
    mesh.add_box8(material, corners)


# ===========================================================================
# Furniture component system
# ---------------------------------------------------------------------------
# Every item has a local frame centred on its footprint, with +X along its
# width and +Y along its depth, rotated by `rotation` degrees in plan. Builders
# emit boxes / cylinders in that local frame; helpers transform to world.
# Convention: an item's "front" (where a person sits / the open face) is -Y,
# its "back" (headboard, monitor, backrest, wall side) is +Y.
# ===========================================================================
class Frame:
    def __init__(self, cx, cy, rot_deg):
        self.cx, self.cy = cx, cy
        self.rot = rot_deg
        a = math.radians(rot_deg)
        self.ca, self.sa = math.cos(a), math.sin(a)

    def xy(self, lx, ly):
        return (self.cx + lx * self.ca - ly * self.sa,
                self.cy + lx * self.sa + ly * self.ca)


def child_frame(fr, lx, ly, drot):
    """A new frame centred at local (lx, ly) of `fr`, rotated by `drot` more."""
    wc = fr.xy(lx, ly)
    return Frame(wc[0], wc[1], fr.rot + drot)


def box_local(mesh, mat, fr, x0, y0, x1, y1, z0, z1):
    """A box spanning local rectangle (x0,y0)-(x1,y1), z0..z1, in frame `fr`."""
    if x1 < x0:
        x0, x1 = x1, x0
    if y1 < y0:
        y0, y1 = y1, y0
    if (x1 - x0) < EPS or (y1 - y0) < EPS or (z1 - z0) < EPS:
        return
    p = [fr.xy(x0, y0), fr.xy(x1, y0), fr.xy(x1, y1), fr.xy(x0, y1)]
    corners = [
        (p[0][0], z0, p[0][1]), (p[1][0], z0, p[1][1]),
        (p[2][0], z0, p[2][1]), (p[3][0], z0, p[3][1]),
        (p[0][0], z1, p[0][1]), (p[1][0], z1, p[1][1]),
        (p[2][0], z1, p[2][1]), (p[3][0], z1, p[3][1]),
    ]
    mesh.add_box8(mat, corners)


def cyl_local(mesh, mat, fr, lcx, lcy, r, z0, z1, segs=16):
    """An upright n-gon prism (cylinder) centred at local (lcx, lcy)."""
    if r < EPS or (z1 - z0) < EPS:
        return
    pts = []
    for i in range(segs):
        ang = 2.0 * math.pi * i / segs
        pts.append(fr.xy(lcx + r * math.cos(ang), lcy + r * math.sin(ang)))
    cb = fr.xy(lcx, lcy)
    for i in range(segs):
        j = (i + 1) % segs
        a, b = pts[i], pts[j]
        mesh.add_quad(mat, (a[0], z0, a[1]), (b[0], z0, b[1]),
                      (b[0], z1, b[1]), (a[0], z1, a[1]))
        mesh.add_tri(mat, (cb[0], z1, cb[1]), (a[0], z1, a[1]), (b[0], z1, b[1]))
        mesh.add_tri(mat, (cb[0], z0, cb[1]), (b[0], z0, b[1]), (a[0], z0, a[1]))


def build_stairs(mesh, material, center, width, run, rotation_deg, z0, total_rise, n_steps):
    """A flight of stairs climbing along local +Y; each tread is a solid block
    from the floor up to its own top, giving a clean ascending silhouette."""
    if n_steps < 1 or run < EPS or total_rise < EPS:
        return
    fr = Frame(center[0], center[1], rotation_deg)
    step_run = run / n_steps
    step_rise = total_rise / n_steps
    hw = width / 2.0
    half_run = run / 2.0
    for i in range(n_steps):
        y0 = -half_run + i * step_run
        y1 = y0 + step_run
        box_local(mesh, material, fr, -hw, y0, hw, y1, z0, z0 + (i + 1) * step_rise)


# --- individual component builders --------------------------------------
# signature: (mesh, fr, w, d, h, z0, item, mat)
def b_box(mesh, fr, w, d, h, z0, item, mat):
    box_local(mesh, mat, fr, -w / 2, -d / 2, w / 2, d / 2, z0, z0 + h)


def b_bed(mesh, fr, w, d, h, z0, item, mat):
    box_local(mesh, "wood", fr, -w / 2, -d / 2, w / 2, d / 2, z0, z0 + h * 0.35)
    m = 0.04
    box_local(mesh, "bed", fr, -w / 2 + m, -d / 2 + m, w / 2 - m, d / 2 - m,
              z0 + h * 0.35, z0 + h)
    pw = w * 0.40
    box_local(mesh, "white", fr, -pw - 0.03, d / 2 - 0.5, -0.03, d / 2 - 0.15,
              z0 + h, z0 + h + 0.08)
    box_local(mesh, "white", fr, 0.03, d / 2 - 0.5, pw + 0.03, d / 2 - 0.15,
              z0 + h, z0 + h + 0.08)


def b_table(mesh, fr, w, d, h, z0, item, mat):
    top_t = 0.04
    top_mat = item.get("top_material", "wood_light")
    box_local(mesh, top_mat, fr, -w / 2, -d / 2, w / 2, d / 2, z0 + h - top_t, z0 + h)
    lt, ins = 0.05, 0.06
    for sx in (-1, 1):
        for sy in (-1, 1):
            xc = sx * (w / 2 - ins - lt / 2)
            yc = sy * (d / 2 - ins - lt / 2)
            box_local(mesh, "metal", fr, xc - lt / 2, yc - lt / 2,
                      xc + lt / 2, yc + lt / 2, z0, z0 + h - top_t)


def b_desk(mesh, fr, w, d, h, z0, item, mat):
    it = dict(item); it["top_material"] = "white"
    b_table(mesh, fr, w, d, h, z0, it, mat)


def b_office_desk(mesh, fr, w, d, h, z0, item, mat):
    b_desk(mesh, fr, w, d, h, z0, item, mat)
    top = z0 + h
    by = d / 2 - 0.12
    stand_h, mon_w, mon_h = 0.12, 0.5, 0.32
    box_local(mesh, "dark", fr, -0.10, by - 0.06, 0.10, by - 0.02, top, top + 0.015)
    box_local(mesh, "dark", fr, -0.025, by - 0.02, 0.025, by + 0.02, top, top + stand_h)
    box_local(mesh, "screen", fr, -mon_w / 2, by, mon_w / 2, by + 0.03,
              top + stand_h, top + stand_h + mon_h)
    box_local(mesh, "dark", fr, -0.22, -0.04, 0.22, 0.18, top, top + 0.02)


def b_chair(mesh, fr, w, d, h, z0, item, mat):
    seat_h = 0.45
    smat = item.get("material") or "fabric"
    box_local(mesh, smat, fr, -w / 2, -d / 2, w / 2, d / 2, z0 + seat_h - 0.06, z0 + seat_h)
    box_local(mesh, smat, fr, -w / 2, d / 2 - 0.06, w / 2, d / 2, z0 + seat_h, z0 + h)
    lt = 0.035
    for sx in (-1, 1):
        for sy in (-1, 1):
            xc = sx * (w / 2 - 0.045); yc = sy * (d / 2 - 0.045)
            box_local(mesh, "metal", fr, xc - lt / 2, yc - lt / 2,
                      xc + lt / 2, yc + lt / 2, z0, z0 + seat_h - 0.06)


def b_office_chair(mesh, fr, w, d, h, z0, item, mat):
    seat_h = 0.48
    cyl_local(mesh, "dark", fr, 0, 0, 0.03, z0 + 0.05, z0 + seat_h - 0.06, segs=10)
    for ang in range(0, 360, 72):
        a = math.radians(ang)
        cyl_local(mesh, "dark", fr, 0.26 * math.cos(a), 0.26 * math.sin(a),
                  0.025, z0, z0 + 0.05, segs=8)
    box_local(mesh, "fabric", fr, -w / 2, -d / 2, w / 2, d / 2,
              z0 + seat_h - 0.07, z0 + seat_h)
    box_local(mesh, "fabric", fr, -w / 2 + 0.05, d / 2 - 0.06, w / 2 - 0.05, d / 2,
              z0 + seat_h, z0 + h)


def b_stool(mesh, fr, w, d, h, z0, item, mat):
    cyl_local(mesh, "wood", fr, 0, 0, min(w, d) / 2, z0 + h - 0.05, z0 + h, segs=14)
    for ang in (45, 135, 225, 315):
        a = math.radians(ang)
        cyl_local(mesh, "metal", fr, (w / 2 - 0.05) * math.cos(a),
                  (d / 2 - 0.05) * math.sin(a), 0.02, z0, z0 + h - 0.05, segs=6)


def b_sofa(mesh, fr, w, d, h, z0, item, mat):
    smat = item.get("material") or "fabric"
    arm, back, seat_h = 0.18, 0.18, 0.42
    box_local(mesh, smat, fr, -w / 2, -d / 2, w / 2, d / 2, z0, z0 + seat_h * 0.6)
    box_local(mesh, smat, fr, -w / 2, d / 2 - back, w / 2, d / 2, z0, z0 + h)
    box_local(mesh, smat, fr, -w / 2, -d / 2, -w / 2 + arm, d / 2, z0, z0 + h * 0.7)
    box_local(mesh, smat, fr, w / 2 - arm, -d / 2, w / 2, d / 2, z0, z0 + h * 0.7)
    n = max(1, int(round((w - 2 * arm) / 0.7)))
    cw = (w - 2 * arm) / n
    for i in range(n):
        x0 = -w / 2 + arm + i * cw + 0.03
        x1 = -w / 2 + arm + (i + 1) * cw - 0.03
        box_local(mesh, smat, fr, x0, -d / 2 + 0.05, x1, d / 2 - back - 0.03,
                  z0 + seat_h * 0.6, z0 + seat_h)
        box_local(mesh, smat, fr, x0, d / 2 - back - 0.05, x1, d / 2 - back + 0.02,
                  z0 + seat_h, z0 + h - 0.05)


def b_armchair(mesh, fr, w, d, h, z0, item, mat):
    b_sofa(mesh, fr, w, d, h, z0, item, mat)


def b_table_chairs(mesh, fr, w, d, h, z0, item, mat):
    b_table(mesh, fr, w, d, h, z0, item, mat)
    n = max(1, int(w // 0.75))
    cw = w / n
    for i in range(n):
        cx = -w / 2 + (i + 0.5) * cw
        b_chair(mesh, child_frame(fr, cx, -(d / 2 + 0.30), 180), 0.5, 0.5, 0.9, z0, {}, None)
        b_chair(mesh, child_frame(fr, cx, (d / 2 + 0.30), 0), 0.5, 0.5, 0.9, z0, {}, None)


def b_round_table(mesh, fr, w, d, h, z0, item, mat):
    r = min(w, d) / 2
    top_t = 0.04
    cyl_local(mesh, "wood_light", fr, 0, 0, r, z0 + h - top_t, z0 + h, segs=24)
    cyl_local(mesh, "metal", fr, 0, 0, 0.06, z0, z0 + h - top_t, segs=12)
    cyl_local(mesh, "metal", fr, 0, 0, r * 0.5, z0, z0 + 0.03, segs=18)
    n = min(8, max(3, int(round(2 * math.pi * r / 0.65))))
    for i in range(n):
        ang = 2 * math.pi * i / n
        cx = (r + 0.32) * math.cos(ang); cy = (r + 0.32) * math.sin(ang)
        drot = math.degrees(ang) - 90.0
        b_chair(mesh, child_frame(fr, cx, cy, drot), 0.5, 0.5, 0.9, z0, {}, None)


def b_desk_bench(mesh, fr, w, d, h, z0, item, mat):
    seats = int(item.get("seats", max(2, int(w // 1.5))))
    seat_w = w / max(1, seats)
    for sign in (-1, 1):
        desk_rot = 0 if sign < 0 else 180
        chair_rot = 180 if sign < 0 else 0
        for i in range(seats):
            cx = -w / 2 + (i + 0.5) * seat_w
            cy = sign * (d * 0.25)
            b_office_desk(mesh, child_frame(fr, cx, cy, desk_rot),
                          seat_w * 0.92, d * 0.42, h, z0, {}, None)
            b_office_chair(mesh, child_frame(fr, cx, sign * (d * 0.5 + 0.28), chair_rot),
                           0.55, 0.55, 1.05, z0, {}, None)


def b_kitchen(mesh, fr, w, d, h, z0, item, mat):
    box_local(mesh, "dark", fr, -w / 2, -d / 2, w / 2, d / 2, z0, z0 + 0.1)
    box_local(mesh, "wood_light", fr, -w / 2, -d / 2, w / 2, d / 2, z0 + 0.1, z0 + h - 0.04)
    box_local(mesh, "stone", fr, -w / 2 - 0.02, -d / 2 - 0.02, w / 2 + 0.02, d / 2 + 0.02,
              z0 + h - 0.04, z0 + h)
    if not item.get("island"):
        box_local(mesh, "wood_light", fr, -w / 2, d / 2 - 0.35, w / 2, d / 2,
                  z0 + 1.45, z0 + 2.15)
    n = max(1, int(round(w / 0.6)))
    for i in range(1, n):
        x = -w / 2 + i * w / n
        box_local(mesh, "dark", fr, x - 0.008, -d / 2, x + 0.008, -d / 2 + 0.02,
                  z0 + 0.1, z0 + h - 0.04)


def b_cabinet(mesh, fr, w, d, h, z0, item, mat):
    body = item.get("material") or "wood"
    box_local(mesh, body, fr, -w / 2, -d / 2, w / 2, d / 2, z0, z0 + h)
    if item.get("type") in ("shelving", "bookshelf"):
        ns = max(2, int(h // 0.35))
        for i in range(1, ns):
            zz = z0 + i * h / ns
            box_local(mesh, "dark", fr, -w / 2 + 0.02, -d / 2, w / 2 - 0.02, -d / 2 + 0.05,
                      zz - 0.01, zz + 0.01)
    else:
        box_local(mesh, "dark", fr, -0.008, -d / 2, 0.008, -d / 2 + 0.02,
                  z0 + 0.05, z0 + h - 0.05)


def b_tv(mesh, fr, w, d, h, z0, item, mat):
    base = z0 if z0 > EPS else 1.05
    box_local(mesh, "screen", fr, -w / 2, -d / 2, w / 2, d / 2, base, base + h)


def b_plant(mesh, fr, w, d, h, z0, item, mat):
    pot_h = min(0.3, h * 0.3)
    pr = min(w, d) / 2 * 0.6
    cyl_local(mesh, "pot", fr, 0, 0, pr, z0, z0 + pot_h, segs=14)
    cyl_local(mesh, "wood", fr, 0, 0, 0.03, z0 + pot_h, z0 + h * 0.5, segs=8)
    fr_r = min(w, d) / 2
    box_local(mesh, "plant", fr, -fr_r, -fr_r, fr_r, fr_r, z0 + h * 0.45, z0 + h * 0.85)
    box_local(mesh, "plant_dark", fr, -fr_r * 0.7, -fr_r * 0.7, fr_r * 0.7, fr_r * 0.7,
              z0 + h * 0.78, z0 + h)


def b_column(mesh, fr, w, d, h, z0, item, mat):
    if item.get("round"):
        cyl_local(mesh, "concrete", fr, 0, 0, min(w, d) / 2, z0, z0 + h, segs=20)
    else:
        box_local(mesh, "concrete", fr, -w / 2, -d / 2, w / 2, d / 2, z0, z0 + h)


def b_stairs(mesh, fr, w, d, h, z0, item, mat):
    n = int(item.get("steps", max(2, round(h / 0.17))))
    build_stairs(mesh, "stairs", (fr.cx, fr.cy), w, d, fr.rot, z0, h, n)


BUILDERS = {
    "box": b_box, "bed": b_bed, "table": b_table, "desk": b_desk,
    "office_desk": b_office_desk, "desk_bench": b_desk_bench,
    "chair": b_chair, "office_chair": b_office_chair, "stool": b_stool,
    "sofa": b_sofa, "armchair": b_armchair, "table_chairs": b_table_chairs,
    "round_table": b_round_table, "kitchen": b_kitchen, "cabinet": b_cabinet,
    "tv": b_tv, "plant": b_plant, "column": b_column, "stairs": b_stairs,
}


def build_furniture(mesh, items):
    """Render every furniture / fixture / stairs item from spec['furniture']."""
    if not items:
        return
    for item in items:
        t = str(item.get("type", "generic"))
        defaults = FURNITURE_TYPES.get(t, FURNITURE_TYPES["generic"])
        size = item.get("size")
        if size and len(size) >= 2:
            w, d = float(size[0]), float(size[1])
        else:
            w, d = float(defaults["size"][0]), float(defaults["size"][1])
        h = float(item.get("height", defaults["size"][2]))
        at = item.get("at", [0.0, 0.0])
        cx, cy = float(at[0]), float(at[1])
        rot = float(item.get("rotation", 0.0))
        z0 = float(item.get("z", 0.0))
        builder = item.get("builder") or defaults.get("builder", "box")
        mat = item.get("material") or defaults.get("material", "wood")
        fn = BUILDERS.get(builder, b_box)
        fn(mesh, Frame(cx, cy, rot), w, d, h, z0, item, mat)


# ---------------------------------------------------------------------------
# Build the model from the spec
# ---------------------------------------------------------------------------
def build_model(spec):
    meta = spec.get("meta", {})
    default_h = float(meta.get("wall_height", 2.7))
    default_t = float(meta.get("wall_thickness", 0.2))

    walls = spec.get("walls", [])
    openings_by_wall = {}
    for op in spec.get("openings", []):
        openings_by_wall.setdefault(int(op["wall"]), []).append(op)

    mesh = MeshBuilder()

    for i, w in enumerate(walls):
        start = (float(w["start"][0]), float(w["start"][1]))
        end = (float(w["end"][0]), float(w["end"][1]))
        thickness = float(w.get("thickness", default_t))
        height = float(w.get("height", default_h))
        ln = math.hypot(end[0] - start[0], end[1] - start[1])
        if ln < EPS:
            continue
        ux, uy = (end[0] - start[0]) / ln, (end[1] - start[1]) / ln

        def at(d):
            return (start[0] + ux * d, start[1] + uy * d)

        # Glass partition: a transparent pane between thin top/bottom frame rails.
        if w.get("type") == "glass" or w.get("glass"):
            gt = min(thickness, 0.05)
            oriented_box(mesh, "frame", start, end, thickness, 0.0, 0.06)
            oriented_box(mesh, "glass", start, end, gt, 0.06, height - 0.06)
            oriented_box(mesh, "frame", start, end, thickness, height - 0.06, height)
            continue

        ops = []
        for op in openings_by_wall.get(i, []):
            width = float(op.get("width", 0.9))
            if "distance" in op:
                c = float(op["distance"])
            else:
                c = float(op.get("position", 0.5)) * ln
            a = max(0.0, c - width / 2.0)
            b = min(ln, c + width / 2.0)
            if b - a < EPS:
                continue
            sill = float(op.get("sill", 0.0 if op.get("kind") == "door" else 0.9))
            oh = float(op.get("height", 2.0 if op.get("kind") == "door" else 1.2))
            top = min(height, sill + oh)
            ops.append({"a": a, "b": b, "sill": sill, "top": top,
                        "kind": op.get("kind", "window")})
        ops.sort(key=lambda o: o["a"])

        if not ops:
            oriented_box(mesh, "wall", start, end, thickness, 0.0, height)
            continue

        prev = 0.0
        for o in ops:
            if o["a"] - prev > EPS:
                oriented_box(mesh, "wall", at(prev), at(o["a"]), thickness, 0.0, height)
            # sill (below opening)
            if o["sill"] > EPS:
                oriented_box(mesh, "wall", at(o["a"]), at(o["b"]), thickness, 0.0, o["sill"])
            # lintel (above opening)
            if height - o["top"] > EPS:
                oriented_box(mesh, "wall", at(o["a"]), at(o["b"]), thickness, o["top"], height)
            # pane / door panel inside the hole
            mat = "window" if o["kind"] == "window" else "door"
            pane_t = 0.05
            oriented_box(mesh, mat, at(o["a"]), at(o["b"]), pane_t, o["sill"], o["top"])
            prev = max(prev, o["b"])
        if ln - prev > EPS:
            oriented_box(mesh, "wall", at(prev), end, thickness, 0.0, height)

    # footprint bbox (used by slab / roof)
    bbox = _footprint_bbox(walls)

    slab = spec.get("slab")
    if slab and slab.get("enabled", True) and bbox:
        margin = float(slab.get("margin", 0.0))
        t = float(slab.get("thickness", 0.15))
        minx, miny, maxx, maxy = bbox
        aabb_box(mesh, "slab", minx - margin, miny - margin,
                 maxx + margin, maxy + margin, -t, 0.0)

    roof = spec.get("roof")
    if roof and roof.get("type", "none") != "none" and bbox:
        _build_roof(mesh, roof, bbox, default_h)

    build_furniture(mesh, spec.get("furniture", []))

    rooms = []
    for r in spec.get("rooms", []):
        rooms.append({
            "name": str(r.get("name", "")),
            "x": float(r["at"][0]),
            "y": float(default_h * 0.55),
            "z": float(r["at"][1]),
        })

    return mesh, rooms


def _footprint_bbox(walls):
    xs, ys = [], []
    for w in walls:
        xs += [float(w["start"][0]), float(w["end"][0])]
        ys += [float(w["start"][1]), float(w["end"][1])]
    if not xs:
        return None
    return (min(xs), min(ys), max(xs), max(ys))


def _build_roof(mesh, roof, bbox, wall_h):
    minx, miny, maxx, maxy = bbox
    oh = float(roof.get("overhang", 0.3))
    rtype = roof.get("type", "flat")
    eave = wall_h
    if rtype == "flat":
        t = float(roof.get("thickness", 0.2))
        aabb_box(mesh, "roof", minx - oh, miny - oh, maxx + oh, maxy + oh,
                 eave, eave + t)
        return
    # gable
    rise = float(roof.get("height", 1.5))
    ridge = eave + rise
    xL, xR = minx - oh, maxx + oh
    yN, yF = miny - oh, maxy + oh
    if roof.get("ridge_axis", "x") == "x":
        yC = (miny + maxy) / 2.0
        # two slopes
        mesh.add_quad("roof", (xL, eave, yN), (xR, eave, yN),
                      (xR, ridge, yC), (xL, ridge, yC))
        mesh.add_quad("roof", (xR, eave, yF), (xL, eave, yF),
                      (xL, ridge, yC), (xR, ridge, yC))
        # gable end triangles
        mesh.add_tri("wall", (xL, eave, yN), (xL, ridge, yC), (xL, eave, yF))
        mesh.add_tri("wall", (xR, eave, yN), (xR, eave, yF), (xR, ridge, yC))
    else:
        xC = (minx + maxx) / 2.0
        mesh.add_quad("roof", (xL, eave, yN), (xL, eave, yF),
                      (xC, ridge, yF), (xC, ridge, yN))
        mesh.add_quad("roof", (xR, eave, yF), (xR, eave, yN),
                      (xC, ridge, yN), (xC, ridge, yF))
        mesh.add_tri("wall", (xL, eave, yN), (xC, ridge, yN), (xR, eave, yN))
        mesh.add_tri("wall", (xL, eave, yF), (xR, eave, yF), (xC, ridge, yF))


# ---------------------------------------------------------------------------
# GLB (binary glTF 2.0) writer  -- standard library only
# ---------------------------------------------------------------------------
def write_glb(mesh):
    bin_blob = bytearray()
    buffer_views = []
    accessors = []
    materials_json = []
    mat_index = {}
    primitives = []

    def add_view(data_bytes, target):
        # 4-byte align
        while len(bin_blob) % 4 != 0:
            bin_blob.append(0)
        offset = len(bin_blob)
        bin_blob.extend(data_bytes)
        buffer_views.append({
            "buffer": 0,
            "byteOffset": offset,
            "byteLength": len(data_bytes),
            "target": target,
        })
        return len(buffer_views) - 1

    for name, spec in MATERIALS.items():
        g = mesh.groups[name]
        if not g["indices"]:
            continue
        positions = g["positions"]
        normals = g["normals"]
        indices = g["indices"]

        # indices (uint32)
        idx_bytes = struct.pack("<%dI" % len(indices), *indices)
        idx_view = add_view(idx_bytes, 34963)  # ELEMENT_ARRAY_BUFFER
        accessors.append({
            "bufferView": idx_view, "componentType": 5125,
            "count": len(indices), "type": "SCALAR",
        })
        idx_acc = len(accessors) - 1

        # positions (float32 vec3)
        flat_pos = [c for v in positions for c in v]
        pos_bytes = struct.pack("<%df" % len(flat_pos), *flat_pos)
        pos_view = add_view(pos_bytes, 34962)  # ARRAY_BUFFER
        xs = [v[0] for v in positions]
        ys = [v[1] for v in positions]
        zs = [v[2] for v in positions]
        accessors.append({
            "bufferView": pos_view, "componentType": 5126,
            "count": len(positions), "type": "VEC3",
            "min": [min(xs), min(ys), min(zs)],
            "max": [max(xs), max(ys), max(zs)],
        })
        pos_acc = len(accessors) - 1

        # normals (float32 vec3)
        flat_nrm = [c for v in normals for c in v]
        nrm_bytes = struct.pack("<%df" % len(flat_nrm), *flat_nrm)
        nrm_view = add_view(nrm_bytes, 34962)
        accessors.append({
            "bufferView": nrm_view, "componentType": 5126,
            "count": len(normals), "type": "VEC3",
        })
        nrm_acc = len(accessors) - 1

        # material
        m = {
            "pbrMetallicRoughness": {
                "baseColorFactor": spec["color"],
                "metallicFactor": float(spec.get("metalness", 0.0)),
                "roughnessFactor": float(spec.get("roughness", 0.85)),
            },
            "doubleSided": True,
            "name": name,
        }
        if spec.get("emissive"):
            m["emissiveFactor"] = spec["emissive"]
        if spec.get("alpha") == "BLEND":
            m["alphaMode"] = "BLEND"
        materials_json.append(m)
        mat_index[name] = len(materials_json) - 1

        primitives.append({
            "attributes": {"POSITION": pos_acc, "NORMAL": nrm_acc},
            "indices": idx_acc,
            "material": mat_index[name],
            "mode": 4,
        })

    if not primitives:
        raise ValueError("Empty model: no geometry was produced from the spec.")

    gltf = {
        "asset": {"version": "2.0", "generator": "plan-to-3d"},
        "scene": 0,
        "scenes": [{"nodes": [0]}],
        "nodes": [{"mesh": 0, "name": "building"}],
        "meshes": [{"primitives": primitives}],
        "materials": materials_json,
        "accessors": accessors,
        "bufferViews": buffer_views,
        "buffers": [{"byteLength": len(bin_blob)}],
    }

    json_bytes = json.dumps(gltf, separators=(",", ":")).encode("utf-8")
    while len(json_bytes) % 4 != 0:
        json_bytes += b" "
    bin_padded = bytes(bin_blob)
    while len(bin_padded) % 4 != 0:
        bin_padded += b"\x00"

    total = 12 + 8 + len(json_bytes) + 8 + len(bin_padded)
    out = bytearray()
    out += struct.pack("<III", 0x46546C67, 2, total)         # header
    out += struct.pack("<II", len(json_bytes), 0x4E4F534A)   # JSON chunk
    out += json_bytes
    out += struct.pack("<II", len(bin_padded), 0x004E4942)   # BIN chunk
    out += bin_padded
    return bytes(out)


# ---------------------------------------------------------------------------
# Self-contained HTML viewer (Three.js via CDN import map, GLB embedded)
# ---------------------------------------------------------------------------
VIEWER_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>__TITLE__ — plan-to-3d</title>
<style>
  html,body{margin:0;height:100%;background:#e9edf2;overflow:hidden;
    font-family:system-ui,Segoe UI,Roboto,sans-serif;color:#2a2f36}
  #app{position:fixed;inset:0}
  .label{padding:2px 8px;background:rgba(255,255,255,.86);border:1px solid #c4ccd6;
    border-radius:6px;font-size:12px;color:#2a2f36;white-space:nowrap;
    pointer-events:none;transform:translateY(-50%);
    box-shadow:0 1px 3px rgba(0,0,0,.12)}
  #hud{position:fixed;left:12px;bottom:12px;font-size:12px;line-height:1.5;
    color:#3a4250;background:rgba(255,255,255,.78);padding:8px 11px;
    border-radius:7px;box-shadow:0 1px 4px rgba(0,0,0,.1)}
  #hud b{color:#11151b}
  #toolbar{position:fixed;right:12px;top:12px;display:flex;gap:6px}
  #toolbar button{font:inherit;font-size:12px;padding:6px 10px;cursor:pointer;
    border:1px solid #c4ccd6;border-radius:7px;background:rgba(255,255,255,.85);
    color:#2a2f36;box-shadow:0 1px 3px rgba(0,0,0,.1)}
  #toolbar button:hover{background:#fff}
  #toolbar button.active{background:#3a72d0;color:#fff;border-color:#2f5fb0}
</style>
</head>
<body>
<div id="app"></div>
<div id="toolbar">
  <button id="btn-roof">Hide roof</button>
  <button id="btn-top">Top view</button>
  <button id="btn-iso">Iso view</button>
</div>
<div id="hud"><b>__TITLE__</b><br>drag = rotate · scroll = zoom · right-drag = pan</div>
<script type="importmap">
{ "imports": {
  "three": "https://unpkg.com/three@0.160.0/build/three.module.js",
  "three/addons/": "https://unpkg.com/three@0.160.0/examples/jsm/"
}}
</script>
<script type="module">
import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';
import { RoomEnvironment } from 'three/addons/environments/RoomEnvironment.js';
import { CSS2DRenderer, CSS2DObject } from 'three/addons/renderers/CSS2DRenderer.js';

const GLB_B64 = "__GLB_B64__";
const LABELS = __LABELS__;
const SKY = 0xe9edf2;

const app = document.getElementById('app');
const scene = new THREE.Scene();
scene.background = new THREE.Color(SKY);

const camera = new THREE.PerspectiveCamera(48, innerWidth/innerHeight, 0.05, 5000);
const renderer = new THREE.WebGLRenderer({antialias:true});
renderer.setSize(innerWidth, innerHeight);
renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
renderer.shadowMap.enabled = true;
renderer.shadowMap.type = THREE.PCFSoftShadowMap;
renderer.toneMapping = THREE.ACESFilmicToneMapping;
renderer.toneMappingExposure = 1.05;
renderer.outputColorSpace = THREE.SRGBColorSpace;
app.appendChild(renderer.domElement);

// Image-based lighting from a procedurally-generated room (no network needed),
// so PBR materials pick up soft, realistic ambient reflections.
const pmrem = new THREE.PMREMGenerator(renderer);
scene.environment = pmrem.fromScene(new RoomEnvironment(), 0.04).texture;

const labelRenderer = new CSS2DRenderer();
labelRenderer.setSize(innerWidth, innerHeight);
labelRenderer.domElement.style.position = 'absolute';
labelRenderer.domElement.style.top = '0';
labelRenderer.domElement.style.pointerEvents = 'none';
app.appendChild(labelRenderer.domElement);

const controls = new OrbitControls(camera, renderer.domElement);
controls.enableDamping = true;
controls.maxPolarAngle = Math.PI * 0.495;  // stay above the floor

scene.add(new THREE.HemisphereLight(0xffffff, 0x9aa3b0, 0.55));
const sun = new THREE.DirectionalLight(0xfff4e6, 2.4);
sun.castShadow = true;
sun.shadow.mapSize.set(2048, 2048);
sun.shadow.bias = -0.0004;
scene.add(sun);
const fill = new THREE.DirectionalLight(0xdfe7ff, 0.5);
fill.position.set(-1.2, 1.0, -0.8);
scene.add(fill);

function b64ToArrayBuffer(b64){
  const bin = atob(b64); const len = bin.length;
  const bytes = new Uint8Array(len);
  for(let i=0;i<len;i++) bytes[i] = bin.charCodeAt(i);
  return bytes.buffer;
}

let MODEL=null, ROOF=null, CENTER=new THREE.Vector3(), RADIUS=5;

function isoView(){
  camera.position.set(CENTER.x + RADIUS*1.25, CENTER.y + RADIUS*0.95, CENTER.z + RADIUS*1.25);
  controls.target.copy(CENTER); controls.update();
}
function topView(){
  camera.position.set(CENTER.x, CENTER.y + RADIUS*1.7, CENTER.z + 0.001);
  controls.target.copy(CENTER); controls.update();
}

const loader = new GLTFLoader();
loader.parse(b64ToArrayBuffer(GLB_B64), '', (gltf) => {
  const model = gltf.scene; MODEL = model;
  scene.add(model);
  model.traverse(o => {
    if(o.isMesh){
      o.castShadow = true; o.receiveShadow = true;
      if(o.material && o.material.name === 'roof') ROOF = o;
      // glass should not cast heavy shadows
      if(o.material && (o.material.name === 'glass' || o.material.name === 'window'))
        o.castShadow = false;
    }
  });

  const box = new THREE.Box3().setFromObject(model);
  const size = box.getSize(new THREE.Vector3());
  const center = box.getCenter(new THREE.Vector3());
  const radius = Math.max(size.x, size.z) || 5;
  CENTER.copy(center); RADIUS = radius;
  camera.near = radius/200; camera.far = radius*200; camera.updateProjectionMatrix();

  // sun + shadow frustum sized to the model
  sun.position.set(center.x + radius*0.8, box.max.y + radius*1.6, center.z + radius*0.5);
  sun.target.position.copy(center); scene.add(sun.target);
  const sc = sun.shadow.camera;
  sc.left=-radius; sc.right=radius; sc.top=radius; sc.bottom=-radius;
  sc.near = 0.1; sc.far = radius*6; sc.updateProjectionMatrix();

  // ground plane that only shows the contact shadow
  const ground = new THREE.Mesh(
    new THREE.PlaneGeometry(radius*30, radius*30),
    new THREE.ShadowMaterial({opacity:0.22}));
  ground.rotation.x = -Math.PI/2;
  ground.position.y = box.min.y - 0.005;
  ground.receiveShadow = true;
  scene.add(ground);

  scene.fog = new THREE.Fog(SKY, radius*5, radius*18);

  for(const l of LABELS){
    const div = document.createElement('div');
    div.className = 'label'; div.textContent = l.name;
    const obj = new CSS2DObject(div);
    obj.position.set(l.x, l.y, l.z);
    scene.add(obj);
  }

  isoView();

  // Debug / scripting handle.
  window.__viewer = {THREE, scene, camera, controls, model, center, radius,
    setMaterialVisible(name, visible){
      model.traverse(o => { if(o.isMesh && o.material && o.material.name === name) o.visible = visible; });
    },
    topDown: topView, iso: isoView};
}, (err) => { console.error('GLB parse error', err); });

// toolbar
const btnRoof = document.getElementById('btn-roof');
btnRoof.addEventListener('click', () => {
  if(!ROOF) return;
  ROOF.visible = !ROOF.visible;
  btnRoof.textContent = ROOF.visible ? 'Hide roof' : 'Show roof';
  btnRoof.classList.toggle('active', !ROOF.visible);
});
document.getElementById('btn-top').addEventListener('click', topView);
document.getElementById('btn-iso').addEventListener('click', isoView);

addEventListener('resize', () => {
  camera.aspect = innerWidth/innerHeight; camera.updateProjectionMatrix();
  renderer.setSize(innerWidth, innerHeight);
  labelRenderer.setSize(innerWidth, innerHeight);
});

(function animate(){
  requestAnimationFrame(animate);
  controls.update();
  renderer.render(scene, camera);
  labelRenderer.render(scene, camera);
})();
</script>
</body>
</html>
"""


def write_viewer(glb_bytes, rooms, title):
    b64 = base64.b64encode(glb_bytes).decode("ascii")
    labels = json.dumps(rooms, ensure_ascii=False)
    html = (VIEWER_TEMPLATE
            .replace("__TITLE__", title)
            .replace("__GLB_B64__", b64)
            .replace("__LABELS__", labels))
    return html


# ---------------------------------------------------------------------------
# Optional isometric preview (matplotlib)
# ---------------------------------------------------------------------------
def write_preview(mesh, path):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from mpl_toolkits.mplot3d.art3d import Poly3DCollection
    except Exception as e:
        print("preview skipped (matplotlib unavailable): %s" % e)
        return False

    light = (0.4, 0.75, 0.5)
    llen = math.sqrt(sum(c * c for c in light))
    light = tuple(c / llen for c in light)

    fig = plt.figure(figsize=(10, 8))
    fig.patch.set_facecolor("#1b1f24")
    ax = fig.add_subplot(111, projection="3d")
    ax.set_facecolor("#1b1f24")
    allx, ally, allz = [], [], []

    # Accumulate every triangle into ONE collection so matplotlib depth-sorts
    # them together (it has no real z-buffer; per-collection sorting is wrong).
    polys, facecolors = [], []
    for name, spec in MATERIALS.items():
        g = mesh.groups[name]
        if not g["indices"]:
            continue
        base_rgb = spec["color"][:3]
        # roof is drawn semi-transparent so the interior layout stays visible
        alpha = 0.30 if name == "roof" else spec["color"][3]
        idx = g["indices"]
        pos = g["positions"]
        nrm = g["normals"]
        for t in range(0, len(idx), 3):
            tri = [pos[idx[t]], pos[idx[t + 1]], pos[idx[t + 2]]]
            n = nrm[idx[t]]
            shade = 0.45 + 0.55 * abs(sum(n[k] * light[k] for k in range(3)))
            facecolors.append((base_rgb[0] * shade, base_rgb[1] * shade,
                               base_rgb[2] * shade, alpha))
            # map glTF (x, height, z) -> matplotlib (x, z, height) so Z is up
            polys.append([(p[0], p[2], p[1]) for p in tri])
            for p in tri:
                allx.append(p[0]); ally.append(p[2]); allz.append(p[1])

    if polys:
        coll = Poly3DCollection(polys, facecolors=facecolors,
                                edgecolors=(0, 0, 0, 0.15), linewidths=0.2)
        ax.add_collection3d(coll)

    if allx:
        rng = max(max(allx) - min(allx), max(ally) - min(ally),
                  max(allz) - min(allz)) or 1.0
        cx = (max(allx) + min(allx)) / 2
        cy = (max(ally) + min(ally)) / 2
        cz = (max(allz) + min(allz)) / 2
        ax.set_xlim(cx - rng / 2, cx + rng / 2)
        ax.set_ylim(cy - rng / 2, cy + rng / 2)
        ax.set_zlim(min(allz), min(allz) + rng)
    ax.set_box_aspect((1, 1, 0.6))
    ax.view_init(elev=25, azim=-50)
    ax.set_axis_off()
    fig.tight_layout()
    fig.savefig(path, dpi=130, facecolor="#1b1f24")
    plt.close(fig)
    return True


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Generate a 3D model from a building spec.")
    ap.add_argument("spec", help="building spec JSON file")
    ap.add_argument("--out", default="output", help="output directory")
    ap.add_argument("--preview", action="store_true", help="also render preview.png")
    ap.add_argument("--name", default=None, help="model title (default: meta.name)")
    args = ap.parse_args()

    with open(args.spec, "r", encoding="utf-8") as f:
        spec = json.load(f)

    title = args.name or spec.get("meta", {}).get("name", "Building")
    os.makedirs(args.out, exist_ok=True)

    mesh, rooms = build_model(spec)
    glb = write_glb(mesh)

    glb_path = os.path.join(args.out, "model.glb")
    with open(glb_path, "wb") as f:
        f.write(glb)

    viewer_path = os.path.join(args.out, "viewer.html")
    with open(viewer_path, "w", encoding="utf-8") as f:
        f.write(write_viewer(glb, rooms, title))

    tri = sum(len(mesh.groups[m]["indices"]) // 3 for m in MATERIALS)
    print("model:   %s (%d bytes)" % (glb_path, len(glb)))
    print("viewer:  %s" % viewer_path)
    print("title:   %s" % title)
    print("walls:   %d | openings: %d | rooms: %d | furniture: %d | triangles: %d" % (
        len(spec.get("walls", [])), len(spec.get("openings", [])),
        len(rooms), len(spec.get("furniture", [])), tri))

    if args.preview:
        ppath = os.path.join(args.out, "preview.png")
        if write_preview(mesh, ppath):
            print("preview: %s" % ppath)


if __name__ == "__main__":
    main()

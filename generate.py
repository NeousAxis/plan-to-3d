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
MATERIALS = {
    "wall":   {"color": [0.86, 0.85, 0.82, 1.0], "alpha": "OPAQUE"},
    "slab":   {"color": [0.55, 0.55, 0.57, 1.0], "alpha": "OPAQUE"},
    "roof":   {"color": [0.66, 0.26, 0.20, 1.0], "alpha": "OPAQUE"},
    "window": {"color": [0.52, 0.72, 0.90, 0.45], "alpha": "BLEND"},
    "door":   {"color": [0.45, 0.30, 0.18, 1.0], "alpha": "OPAQUE"},
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
                "metallicFactor": 0.0,
                "roughnessFactor": 0.85,
            },
            "doubleSided": True,
            "name": name,
        }
        if spec["alpha"] == "BLEND":
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
  html,body{margin:0;height:100%;background:#1b1f24;overflow:hidden;
    font-family:system-ui,Segoe UI,Roboto,sans-serif;color:#e8eaed}
  #app{position:fixed;inset:0}
  .label{padding:2px 7px;background:rgba(20,24,28,.78);border:1px solid #3a4350;
    border-radius:5px;font-size:12px;color:#dfe5ee;white-space:nowrap;
    pointer-events:none;transform:translateY(-50%)}
  #hud{position:fixed;left:12px;bottom:12px;font-size:12px;line-height:1.5;
    color:#aeb6c2;background:rgba(20,24,28,.6);padding:8px 11px;border-radius:7px}
  #hud b{color:#fff}
</style>
</head>
<body>
<div id="app"></div>
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
import { CSS2DRenderer, CSS2DObject } from 'three/addons/renderers/CSS2DRenderer.js';

const GLB_B64 = "__GLB_B64__";
const LABELS = __LABELS__;

const app = document.getElementById('app');
const scene = new THREE.Scene();
scene.background = new THREE.Color(0x1b1f24);

const camera = new THREE.PerspectiveCamera(50, innerWidth/innerHeight, 0.05, 5000);
const renderer = new THREE.WebGLRenderer({antialias:true});
renderer.setSize(innerWidth, innerHeight);
renderer.setPixelRatio(devicePixelRatio);
app.appendChild(renderer.domElement);

const labelRenderer = new CSS2DRenderer();
labelRenderer.setSize(innerWidth, innerHeight);
labelRenderer.domElement.style.position = 'absolute';
labelRenderer.domElement.style.top = '0';
labelRenderer.domElement.style.pointerEvents = 'none';
app.appendChild(labelRenderer.domElement);

const controls = new OrbitControls(camera, renderer.domElement);
controls.enableDamping = true;

scene.add(new THREE.HemisphereLight(0xffffff, 0x444a55, 1.05));
const sun = new THREE.DirectionalLight(0xffffff, 1.4);
sun.position.set(1, 2, 1.3);
scene.add(sun);
const sun2 = new THREE.DirectionalLight(0xffffff, 0.5);
sun2.position.set(-1.2, 1, -0.8);
scene.add(sun2);
scene.add(new THREE.GridHelper(200, 200, 0x2a313b, 0x232a33));

function b64ToArrayBuffer(b64){
  const bin = atob(b64); const len = bin.length;
  const bytes = new Uint8Array(len);
  for(let i=0;i<len;i++) bytes[i] = bin.charCodeAt(i);
  return bytes.buffer;
}

const loader = new GLTFLoader();
loader.parse(b64ToArrayBuffer(GLB_B64), '', (gltf) => {
  const model = gltf.scene;
  scene.add(model);
  const box = new THREE.Box3().setFromObject(model);
  const size = box.getSize(new THREE.Vector3());
  const center = box.getCenter(new THREE.Vector3());
  const radius = Math.max(size.x, size.y, size.z) || 5;
  controls.target.copy(center);
  camera.position.set(center.x + radius*1.3, center.y + radius*1.0, center.z + radius*1.3);
  camera.near = radius/100; camera.far = radius*100; camera.updateProjectionMatrix();

  for(const l of LABELS){
    const div = document.createElement('div');
    div.className = 'label'; div.textContent = l.name;
    const obj = new CSS2DObject(div);
    obj.position.set(l.x, l.y, l.z);
    scene.add(obj);
  }
}, (err) => { console.error('GLB parse error', err); });

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
    print("walls:   %d | openings: %d | rooms: %d | triangles: %d" % (
        len(spec.get("walls", [])), len(spec.get("openings", [])),
        len(rooms), tri))

    if args.preview:
        ppath = os.path.join(args.out, "preview.png")
        if write_preview(mesh, ppath):
            print("preview: %s" % ppath)


if __name__ == "__main__":
    main()

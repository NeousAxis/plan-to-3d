"""Run inside Blender headless. Loads the GLB produced by generate.py, sets
up an arch-viz lighting rig from the building spec, and renders a single
photoreal still with Cycles (GI, soft shadows, indirect bounces).

Invocation (via run_bake.py / Makefile):
    blender -b -P bake_blender.py -- \
        --glb work/lobby/model.glb \
        --spec examples/lobby.json \
        --out work/lobby/render.png \
        --cam "eye,height,target" \
        --samples 256

The command-line camera is optional; without it the script auto-frames a
walkthrough view inside the building envelope.
"""

import argparse
import json
import math
import os
import sys

import bpy
import mathutils


# ---------------------------------------------------------------------------
# CLI (Blender ignores anything before the '--' separator)
# ---------------------------------------------------------------------------
def parse_argv():
    argv = sys.argv
    if "--" in argv:
        argv = argv[argv.index("--") + 1:]
    else:
        argv = []
    p = argparse.ArgumentParser()
    p.add_argument("--glb", required=True)
    p.add_argument("--spec", required=True, help="building.json (lights, …)")
    p.add_argument("--out", required=True)
    p.add_argument("--samples", type=int, default=128)
    p.add_argument("--res", default="1920x1080")
    p.add_argument("--cam", default=None,
                   help="camera pose 'ex,ey,ez,tx,ty,tz' (world metres)")
    p.add_argument("--exposure", type=float, default=0.4)
    return p.parse_args(argv)


# ---------------------------------------------------------------------------
# Scene reset + render config
# ---------------------------------------------------------------------------
def reset_scene():
    bpy.ops.wm.read_factory_settings(use_empty=True)


def configure_render(samples, w, h, exposure):
    scn = bpy.context.scene
    scn.render.engine = 'CYCLES'
    scn.render.resolution_x = w
    scn.render.resolution_y = h
    scn.render.resolution_percentage = 100
    scn.render.image_settings.file_format = 'PNG'
    scn.render.film_transparent = False
    scn.cycles.samples = samples
    scn.cycles.use_denoising = True
    scn.cycles.max_bounces = 8
    scn.cycles.diffuse_bounces = 4
    scn.cycles.glossy_bounces = 4
    scn.cycles.transmission_bounces = 4
    scn.view_settings.view_transform = 'AgX'
    scn.view_settings.look = 'AgX - Medium High Contrast'
    scn.view_settings.exposure = exposure
    # Prefer GPU Metal when available
    prefs = bpy.context.preferences.addons['cycles'].preferences
    try:
        prefs.compute_device_type = 'METAL'
        prefs.get_devices()
        for d in prefs.devices:
            d.use = (d.type == 'METAL')
        scn.cycles.device = 'GPU'
        print(f"[bake] GPU devices: "
              f"{[d.name for d in prefs.devices if d.use]}")
    except Exception as e:  # noqa: BLE001
        print(f"[bake] GPU init failed, falling back to CPU: {e}")
        scn.cycles.device = 'CPU'


def load_glb(path):
    bpy.ops.import_scene.gltf(filepath=path)
    meshes = [o for o in bpy.context.scene.objects if o.type == 'MESH']
    upgrade_materials()
    return meshes


def upgrade_materials():
    """Replace the flat glTF PBR shaders with richer Cycles networks:
       - glass / window : real transmissive Principled BSDF (IOR 1.45, low
         roughness, transmissive) so light filters through.
       - lamp           : strong emission so fixtures cast visible light.
       - art            : raise emission so the canvas pops in dim halls.
       - ceiling_perf   : the alpha channel of the colour map cuts real
         holes (Cycles 'Mix Shader' with Transparent BSDF).
       - slab / floors  : a soft bump from the colour map so the terrazzo
         catches highlights instead of looking flat.
    """
    for m in bpy.data.materials:
        nm = (m.name or "").lower()
        if not m.use_nodes:
            m.use_nodes = True
        nt = m.node_tree
        bsdf = next((n for n in nt.nodes if n.type == 'BSDF_PRINCIPLED'), None)
        out  = next((n for n in nt.nodes if n.type == 'OUTPUT_MATERIAL'), None)
        if not bsdf or not out:
            continue
        if 'glass' in nm or 'window' in nm:
            bsdf.inputs['Transmission Weight'].default_value = 1.0
            bsdf.inputs['Roughness'].default_value = 0.0
            bsdf.inputs['IOR'].default_value = 1.45
            bsdf.inputs['Alpha'].default_value = 1.0
            bsdf.inputs['Base Color'].default_value = (0.92, 0.96, 0.99, 1.0)
            m.use_backface_culling = False
        elif nm == 'lamp':
            bsdf.inputs['Emission Strength'].default_value = 18.0
            bsdf.inputs['Emission Color'].default_value = (1.0, 0.92, 0.74, 1.0)
        elif nm == 'art':
            bsdf.inputs['Emission Strength'].default_value = 1.2
            # use the texture for emission too (linked to its Base Color tex)
            tex = next((l.from_node for l in bsdf.inputs['Base Color'].links
                        if l.from_node.type == 'TEX_IMAGE'), None)
            if tex:
                nt.links.new(tex.outputs['Color'],
                             bsdf.inputs['Emission Color'])
        elif 'ceiling_perf' in nm:
            tex = next((l.from_node for l in bsdf.inputs['Base Color'].links
                        if l.from_node.type == 'TEX_IMAGE'), None)
            if tex:
                # alpha cutout via mix with transparent BSDF
                tr = nt.nodes.new('ShaderNodeBsdfTransparent')
                mix = nt.nodes.new('ShaderNodeMixShader')
                mix.location = (out.location[0] - 200, out.location[1])
                tr.location = (out.location[0] - 400, out.location[1] - 200)
                nt.links.new(tex.outputs['Alpha'], mix.inputs['Fac'])
                nt.links.new(tr.outputs['BSDF'], mix.inputs[1])
                nt.links.new(bsdf.outputs['BSDF'], mix.inputs[2])
                nt.links.new(mix.outputs['Shader'], out.inputs['Surface'])
        elif nm in ('slab', 'wood', 'wood_light', 'concrete'):
            tex = next((l.from_node for l in bsdf.inputs['Base Color'].links
                        if l.from_node.type == 'TEX_IMAGE'), None)
            if tex:
                bump = nt.nodes.new('ShaderNodeBump')
                bump.inputs['Strength'].default_value = 0.12 if nm == 'slab' else 0.06
                bump.location = (bsdf.location[0] - 250, bsdf.location[1] - 320)
                nt.links.new(tex.outputs['Color'], bump.inputs['Height'])
                nt.links.new(bump.outputs['Normal'], bsdf.inputs['Normal'])


# ---------------------------------------------------------------------------
# Lighting — re-create the viewer's rig (sun + spots/points from fixtures)
# ---------------------------------------------------------------------------
def add_sun(angle_deg=40):
    bpy.ops.object.light_add(type='SUN')
    sun = bpy.context.object
    sun.data.energy = 3.5
    sun.data.angle = math.radians(2.5)
    sun.rotation_euler = (math.radians(angle_deg), 0, math.radians(35))
    return sun


def add_environment(strength=1.6):
    """Bright diffuse sky so interior bounces are clear (matches the
    well-lit reception in the user's reference photo, not a moody night
    interior)."""
    world = bpy.context.scene.world
    if world is None:
        world = bpy.data.worlds.new("World")
        bpy.context.scene.world = world
    world.use_nodes = True
    nt = world.node_tree
    nt.nodes.clear()
    bg = nt.nodes.new('ShaderNodeBackground')
    sky = nt.nodes.new('ShaderNodeTexSky')
    sky.sky_type = 'HOSEK_WILKIE'
    sky.sun_direction = mathutils.Vector((0.6, 0.4, 0.8)).normalized()
    sky.turbidity = 2.0                        # clearer sky -> more light
    out = nt.nodes.new('ShaderNodeOutputWorld')
    nt.links.new(sky.outputs[0], bg.inputs[0])
    bg.inputs[1].default_value = strength
    nt.links.new(bg.outputs[0], out.inputs[0])


def add_fill_areas(meshes):
    """Adds two large Area lights inside the building envelope as soft fill,
    so the interior is gallery-bright instead of cave-dark. Energies tuned
    against the brighter sun + sky above."""
    if not meshes:
        return
    minp = mathutils.Vector(( 1e9,  1e9,  1e9))
    maxp = mathutils.Vector((-1e9, -1e9, -1e9))
    for o in meshes:
        for v in o.bound_box:
            wv = o.matrix_world @ mathutils.Vector(v)
            minp = mathutils.Vector(map(min, minp, wv))
            maxp = mathutils.Vector(map(max, maxp, wv))
    cx, cy = (minp.x+maxp.x)/2, (minp.y+maxp.y)/2
    cz = (minp.z+maxp.z)/2
    sx, sy = (maxp.x-minp.x), (maxp.y-minp.y)
    # Big "skylight" hovering just above the ceiling, pointing down
    bpy.ops.object.light_add(type='AREA')
    L = bpy.context.object
    L.data.energy = 350
    L.data.shape = 'RECTANGLE'
    L.data.size = sx * 0.9
    L.data.size_y = sy * 0.9
    L.data.color = (1.0, 0.98, 0.95)
    L.location = (cx, cy, maxp.z + 0.5)
    L.rotation_euler = (0, 0, 0)


def add_fixtures(spec, floor_y):
    """Translate the spec's furniture light fixtures into Cycles lights."""
    types = {
        "downlight":   {"kind": "SPOT", "energy": 250, "size": 0.4,
                        "spot_angle": math.radians(55), "spot_blend": 0.4},
        "pendant":     {"kind": "POINT", "energy": 220, "size": 0.25},
        "sconce":      {"kind": "POINT", "energy": 80,  "size": 0.18},
        "floor_lamp":  {"kind": "POINT", "energy": 110, "size": 0.20},
    }
    count = 0
    for f in spec.get("furniture", []):
        cfg = types.get(f.get("type"))
        if not cfg:
            continue
        at = f.get("at", [0, 0])
        z = float(f.get("z", 2.85 if f.get("type") == "downlight" else 1.8))
        bpy.ops.object.light_add(type=cfg["kind"])
        L = bpy.context.object
        L.data.energy = cfg["energy"]
        L.data.color = (1.0, 0.88, 0.72)
        if hasattr(L.data, "shadow_soft_size"):
            L.data.shadow_soft_size = cfg["size"]
        if cfg["kind"] == "SPOT":
            L.data.spot_size = cfg["spot_angle"]
            L.data.spot_blend = cfg["spot_blend"]
            # glTF axes: X=plan x, Y=height, Z=plan y -> match Blender (Z up)
            L.location = (float(at[0]), float(at[1]), z - 0.05)
            L.rotation_euler = (math.radians(180), 0, 0)  # point down
        else:
            L.location = (float(at[0]), float(at[1]), z)
        count += 1
    print(f"[bake] fixtures lit: {count}")


# ---------------------------------------------------------------------------
# Camera — eye-level walkthrough by default
# ---------------------------------------------------------------------------
def autoframe_camera(meshes):
    minp = mathutils.Vector(( 1e9,  1e9,  1e9))
    maxp = mathutils.Vector((-1e9, -1e9, -1e9))
    for o in meshes:
        for v in o.bound_box:
            wv = o.matrix_world @ mathutils.Vector(v)
            minp = mathutils.Vector(map(min, minp, wv))
            maxp = mathutils.Vector(map(max, maxp, wv))
    centre = (minp + maxp) * 0.5
    size = maxp - minp
    floor_z = minp.z
    eye_z = floor_z + 1.65
    # stand near one edge, looking across the long axis
    eye = mathutils.Vector((maxp.x - 0.6, maxp.y * 0.4 + minp.y * 0.6, eye_z))
    target = mathutils.Vector((minp.x + 0.6, centre.y, eye_z - 0.1))
    return eye, target


def add_camera(eye, target, fov_deg=55):
    bpy.ops.object.camera_add()
    cam = bpy.context.object
    cam.data.lens_unit = 'FOV'
    cam.data.angle = math.radians(fov_deg)
    cam.location = eye
    # Look-at: build a rotation that puts -Z towards target
    fwd = (target - eye).normalized()
    up = mathutils.Vector((0, 0, 1))
    if abs(fwd.dot(up)) > 0.99:
        up = mathutils.Vector((0, 1, 0))
    right = fwd.cross(up).normalized()
    up2 = right.cross(fwd).normalized()
    m = mathutils.Matrix((
        ( right.x,  right.y,  right.z,  0),
        ( up2.x,    up2.y,    up2.z,    0),
        (-fwd.x,   -fwd.y,   -fwd.z,    0),
        ( 0,        0,        0,        1),
    )).transposed()
    cam.matrix_world = mathutils.Matrix.Translation(eye) @ m.to_4x4()
    bpy.context.scene.camera = cam
    return cam


def main():
    args = parse_argv()
    w, h = (int(x) for x in args.res.lower().split('x'))
    spec = json.load(open(args.spec, 'r', encoding='utf-8'))

    reset_scene()
    configure_render(args.samples, w, h, args.exposure)
    meshes = load_glb(args.glb)
    print(f"[bake] imported {len(meshes)} meshes from {args.glb}")

    add_environment(strength=1.6)
    add_sun()
    add_fill_areas(meshes)
    floor_y = min((o.matrix_world @ mathutils.Vector(o.bound_box[0])).z
                  for o in meshes) if meshes else 0.0
    add_fixtures(spec, floor_y)

    if args.cam:
        nums = [float(x) for x in args.cam.split(',')]
        eye = mathutils.Vector(nums[:3])
        target = mathutils.Vector(nums[3:6])
    else:
        eye, target = autoframe_camera(meshes)
    add_camera(eye, target)

    bpy.context.scene.render.filepath = os.path.abspath(args.out)
    print(f"[bake] rendering {w}x{h} @ {args.samples} samples -> {args.out}")
    bpy.ops.render.render(write_still=True)
    print(f"[bake] done: {args.out}")


if __name__ == "__main__":
    main()

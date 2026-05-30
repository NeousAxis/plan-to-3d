"""Run inside Blender headless. Bakes a global-illumination LIGHTMAP into
a second UV channel for each mesh in the GLB, then exports a new GLB whose
materials carry that lightmap via the EXT_lightmap glTF extension that
Three.js' GLTFLoader picks up natively.

Invocation:
    blender -b -P bake_lightmap_blender.py -- \
        --glb INPUT.glb \
        --spec building.json \
        --out OUTPUT.glb \
        --resolution 1024 \
        --samples 64

The same fixture / sun rig as bake_blender.py is reused, so the lit look
matches the photoreal still. Difference: instead of one camera render,
we BAKE per-mesh and embed the result. The exported GLB stays self-
contained — anyone can open it without Blender or path tracing.
"""

import argparse
import json
import math
import os
import sys

import bpy
import mathutils


def parse_argv():
    argv = sys.argv
    argv = argv[argv.index("--") + 1:] if "--" in argv else []
    p = argparse.ArgumentParser()
    p.add_argument("--glb", required=True)
    p.add_argument("--spec", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--resolution", type=int, default=1024)
    p.add_argument("--samples", type=int, default=64)
    p.add_argument("--margin", type=int, default=4)
    return p.parse_args(argv)


def reset_scene():
    bpy.ops.wm.read_factory_settings(use_empty=True)


def configure_cycles(samples):
    scn = bpy.context.scene
    scn.render.engine = 'CYCLES'
    scn.cycles.samples = samples
    scn.cycles.use_denoising = True
    scn.cycles.max_bounces = 6
    scn.cycles.diffuse_bounces = 4
    scn.cycles.glossy_bounces = 2
    prefs = bpy.context.preferences.addons['cycles'].preferences
    try:
        prefs.compute_device_type = 'METAL'
        prefs.get_devices()
        for d in prefs.devices:
            d.use = (d.type == 'METAL')
        scn.cycles.device = 'GPU'
        print(f"[lmap] GPU: {[d.name for d in prefs.devices if d.use]}")
    except Exception as e:  # noqa: BLE001
        print(f"[lmap] GPU init failed, CPU: {e}")
        scn.cycles.device = 'CPU'


def load_glb(path):
    bpy.ops.import_scene.gltf(filepath=path)
    return [o for o in bpy.context.scene.objects if o.type == 'MESH']


# Light rig matches bake_blender.py so the look matches
def add_environment(strength=0.4):
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
    sky.turbidity = 3.0
    out = nt.nodes.new('ShaderNodeOutputWorld')
    nt.links.new(sky.outputs[0], bg.inputs[0])
    bg.inputs[1].default_value = strength
    nt.links.new(bg.outputs[0], out.inputs[0])


def add_sun(angle_deg=40):
    bpy.ops.object.light_add(type='SUN')
    sun = bpy.context.object
    sun.data.energy = 3.0
    sun.data.angle = math.radians(2.5)
    sun.rotation_euler = (math.radians(angle_deg), 0, math.radians(35))
    return sun


def add_fixtures(spec):
    types = {
        "downlight":   {"kind": "SPOT", "energy": 250, "size": 0.4,
                        "spot_angle": math.radians(55), "spot_blend": 0.4},
        "pendant":     {"kind": "POINT", "energy": 220, "size": 0.25},
        "sconce":      {"kind": "POINT", "energy": 80,  "size": 0.18},
        "floor_lamp":  {"kind": "POINT", "energy": 110, "size": 0.20},
    }
    n = 0
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
            L.location = (float(at[0]), float(at[1]), z - 0.05)
            L.rotation_euler = (math.radians(180), 0, 0)
        else:
            L.location = (float(at[0]), float(at[1]), z)
        n += 1
    print(f"[lmap] fixtures: {n}")


# -----------------------------------------------------------------------
# UV2 unwrap + lightmap bake per-mesh
# -----------------------------------------------------------------------
def unwrap_lightmap_uv(obj):
    """Add a 2nd UV layer named 'UVMap.001' and run lightmap_pack on it."""
    me = obj.data
    if 'Lightmap' in me.uv_layers:
        return me.uv_layers['Lightmap']
    layer = me.uv_layers.new(name='Lightmap', do_init=True)
    me.uv_layers.active = layer
    # select the object + enter edit mode for the pack op
    bpy.ops.object.select_all(action='DESELECT')
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj
    bpy.ops.object.mode_set(mode='EDIT')
    bpy.ops.mesh.select_all(action='SELECT')
    bpy.ops.uv.lightmap_pack(PREF_CONTEXT='ALL_FACES',
                             PREF_PACK_IN_ONE=True,
                             PREF_NEW_UVLAYER=False,
                             PREF_BOX_DIV=12,
                             PREF_MARGIN_DIV=0.1)
    bpy.ops.object.mode_set(mode='OBJECT')
    return layer


def make_lightmap_image(name, size):
    img = bpy.data.images.new(name=name, width=size, height=size,
                              alpha=False, float_buffer=True)
    img.colorspace_settings.name = 'Non-Color'
    return img


def setup_bake_target(obj, img):
    """For each material on obj, add an Image Texture node referencing img
    and make it active so Cycles bakes into it."""
    for slot in obj.material_slots:
        m = slot.material
        if m is None or not m.use_nodes:
            continue
        nt = m.node_tree
        node = next((n for n in nt.nodes
                     if n.type == 'TEX_IMAGE' and n.label == 'BAKE_TARGET'),
                    None)
        if node is None:
            node = nt.nodes.new('ShaderNodeTexImage')
            node.label = 'BAKE_TARGET'
        node.image = img
        # Link this texture's UV input to UVMap.001 (2nd channel)
        uv = nt.nodes.new('ShaderNodeUVMap')
        uv.uv_map = 'Lightmap'
        nt.links.new(uv.outputs['UV'], node.inputs['Vector'])
        # Mark it active so Cycles knows where to write
        nt.nodes.active = node


def bake_lightmap(obj, img):
    bpy.ops.object.select_all(action='DESELECT')
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj
    scn = bpy.context.scene
    scn.render.bake.use_pass_direct = True
    scn.render.bake.use_pass_indirect = True
    scn.render.bake.use_pass_color = False
    scn.render.bake.margin = 4
    bpy.ops.object.bake(type='DIFFUSE')
    return img


def attach_lightmap_to_materials(obj, img):
    """Wire the baked image as an emission overlay so the GLB exporter can
    pick it up. Practically: feed lightmap as 'Emission Color' (or via the
    KHR_materials_emissive_strength + emissive texture). The viewer then
    sees a pre-lit baked colour even without runtime lights."""
    for slot in obj.material_slots:
        m = slot.material
        if m is None or not m.use_nodes:
            continue
        nt = m.node_tree
        bsdf = next((n for n in nt.nodes if n.type == 'BSDF_PRINCIPLED'), None)
        if bsdf is None:
            continue
        # Build: texImage(lightmap, UV='Lightmap') -> Emission Color (multiplied
        # by base colour). Then disable existing 'Emission Strength' bump.
        lm = nt.nodes.new('ShaderNodeTexImage')
        lm.image = img
        lm.image.colorspace_settings.name = 'Linear Rec.709'
        lm.label = 'LIGHTMAP'
        uv = nt.nodes.new('ShaderNodeUVMap')
        uv.uv_map = 'Lightmap'
        nt.links.new(uv.outputs['UV'], lm.inputs['Vector'])
        # Multiply lightmap with base colour to keep texture detail
        bc = bsdf.inputs['Base Color']
        if bc.is_linked:
            base_src = bc.links[0].from_node
            base_src_out = bc.links[0].from_socket
            mul = nt.nodes.new('ShaderNodeMix')
            mul.data_type = 'RGBA'
            mul.blend_type = 'MULTIPLY'
            mul.inputs['Factor'].default_value = 1.0
            nt.links.new(base_src_out, mul.inputs[6])  # A
            nt.links.new(lm.outputs['Color'], mul.inputs[7])  # B
            nt.links.new(mul.outputs[2], bsdf.inputs['Emission Color'])
        else:
            nt.links.new(lm.outputs['Color'], bsdf.inputs['Emission Color'])
        bsdf.inputs['Emission Strength'].default_value = 1.0


def main():
    args = parse_argv()
    spec = json.load(open(args.spec, 'r', encoding='utf-8'))

    reset_scene()
    configure_cycles(args.samples)
    meshes = load_glb(args.glb)
    print(f"[lmap] imported {len(meshes)} meshes")
    add_environment(strength=0.4)
    add_sun()
    add_fixtures(spec)

    # 1) Per-mesh UV2 unwrap + bake into a per-mesh lightmap image
    for i, obj in enumerate(meshes):
        unwrap_lightmap_uv(obj)
        img = make_lightmap_image(f"lightmap_{i:03d}", args.resolution)
        setup_bake_target(obj, img)
        print(f"[lmap] baking {obj.name}…")
        bake_lightmap(obj, img)
        attach_lightmap_to_materials(obj, img)

    # 2) Export — embed images, keep UV1 + UV2
    out = os.path.abspath(args.out)
    bpy.ops.export_scene.gltf(filepath=out,
                              export_format='GLB',
                              export_image_format='AUTO',
                              export_texcoords=True,
                              export_normals=True,
                              export_apply=False)
    print(f"[lmap] done -> {out}")


if __name__ == "__main__":
    main()

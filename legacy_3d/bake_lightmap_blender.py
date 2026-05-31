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
    sun.data.energy = 6.0
    sun.data.angle = math.radians(2.5)
    sun.rotation_euler = (math.radians(angle_deg), 0, math.radians(35))
    return sun


def add_fill_areas(meshes):
    """A big skylight just above the ceiling so every face of every mesh
    receives at least some indirect light, otherwise back walls and plant
    interiors bake to near-black in the lightmap."""
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
    sx, sy = (maxp.x-minp.x), (maxp.y-minp.y)
    bpy.ops.object.light_add(type='AREA')
    L = bpy.context.object
    L.data.energy = 800
    L.data.shape = 'RECTANGLE'
    L.data.size = sx * 0.9
    L.data.size_y = sy * 0.9
    L.data.color = (1.0, 0.98, 0.95)
    L.location = (cx, cy, maxp.z + 0.5)
    L.rotation_euler = (0, 0, 0)


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
    """1st pass: bake pure DIFFUSE light (direct + indirect, no colour).
    The result is a per-mesh "shadow + GI" image; multiplied by base colour
    in the 2nd EMIT pass to produce the final pre-lit texture."""
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


def bake_emit_flatten(obj, src_img, dst_img):
    """2nd pass: bake type='EMIT' after wiring `base_colour × src_img` into
    the BSDF emission. The result is a SINGLE image holding the fully pre-lit
    (base × light) colour, with NO node-graph multiplications glTF would
    have trouble exporting. The shader is restored to its original state
    after the bake so realtime fixture lights can still act on the mesh."""
    # Snapshot the original material state per slot
    saved = []
    for slot in obj.material_slots:
        m = slot.material
        if m is None or not m.use_nodes:
            continue
        nt = m.node_tree
        bsdf = next((n for n in nt.nodes if n.type == 'BSDF_PRINCIPLED'), None)
        if bsdf is None:
            continue
        bc = bsdf.inputs['Base Color']
        em_color = bsdf.inputs['Emission Color']
        em_str = bsdf.inputs['Emission Strength']
        saved.append({
            'm': m,
            'em_color_links': [(l.from_socket, l.to_socket) for l in em_color.links],
            'em_color_val': tuple(em_color.default_value),
            'em_str_val': em_str.default_value,
        })
        # Build base_colour × lightmap into Emission Color
        uv = nt.nodes.new('ShaderNodeUVMap')
        uv.uv_map = 'Lightmap'
        lm = nt.nodes.new('ShaderNodeTexImage')
        lm.image = src_img
        lm.image.colorspace_settings.name = 'Linear Rec.709'
        nt.links.new(uv.outputs['UV'], lm.inputs['Vector'])
        mul = nt.nodes.new('ShaderNodeMix')
        mul.data_type = 'RGBA'
        mul.blend_type = 'MULTIPLY'
        mul.inputs['Factor'].default_value = 1.0
        if bc.is_linked:
            nt.links.new(bc.links[0].from_socket, mul.inputs[6])
        else:
            rgb = nt.nodes.new('ShaderNodeRGB')
            rgb.outputs[0].default_value = tuple(bc.default_value)
            nt.links.new(rgb.outputs[0], mul.inputs[6])
        nt.links.new(lm.outputs['Color'], mul.inputs[7])
        # Clear existing emission links, wire the multiply
        for l in list(em_color.links):
            nt.links.remove(l)
        nt.links.new(mul.outputs[2], em_color)
        em_str.default_value = 1.0
        # Make the destination image the bake target
        dst_node = next((n for n in nt.nodes
                         if n.type == 'TEX_IMAGE' and n.label == 'BAKE_TARGET'),
                        None)
        if dst_node is None:
            dst_node = nt.nodes.new('ShaderNodeTexImage')
            dst_node.label = 'BAKE_TARGET'
        dst_node.image = dst_img
        uv2 = nt.nodes.new('ShaderNodeUVMap')
        uv2.uv_map = 'Lightmap'
        nt.links.new(uv2.outputs['UV'], dst_node.inputs['Vector'])
        nt.nodes.active = dst_node

    # Run the EMIT bake
    bpy.ops.object.select_all(action='DESELECT')
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj
    bpy.context.scene.render.bake.margin = 4
    bpy.ops.object.bake(type='EMIT')

    # Restore each material's emission to the original state so realtime
    # lights still behave correctly when the mesh is later viewed.
    for s in saved:
        m = s['m']
        nt = m.node_tree
        bsdf = next((n for n in nt.nodes if n.type == 'BSDF_PRINCIPLED'), None)
        if bsdf is None:
            continue
        em_color = bsdf.inputs['Emission Color']
        em_str = bsdf.inputs['Emission Strength']
        for l in list(em_color.links):
            nt.links.remove(l)
        em_color.default_value = s['em_color_val']
        em_str.default_value = s['em_str_val']
    return dst_img


SHELL_MATS = {
    'wall', 'slab', 'roof', 'frame',
    'ceiling', 'ceiling_perf', 'ceiling_dark',
    'wood', 'wood_light', 'stone', 'concrete', 'door',
}


def attach_flattened_to_materials(obj, flat_img):
    """Wire the flattened bake into the BSDF Emission Color (UV='Lightmap'),
    ONLY for shell materials. Lamps / art / glass / fabric / plants keep
    their original emission untouched, so they still glow / be transparent /
    pick up realtime fixture lights in the viewer."""
    for slot in obj.material_slots:
        m = slot.material
        if m is None or not m.use_nodes:
            continue
        if m.name not in SHELL_MATS:
            continue
        nt = m.node_tree
        bsdf = next((n for n in nt.nodes if n.type == 'BSDF_PRINCIPLED'), None)
        if bsdf is None:
            continue
        em = bsdf.inputs['Emission Color']
        for l in list(em.links):
            nt.links.remove(l)
        uv = nt.nodes.new('ShaderNodeUVMap')
        uv.uv_map = 'Lightmap'
        tex = nt.nodes.new('ShaderNodeTexImage')
        tex.image = flat_img
        tex.label = 'FLAT_BAKE'
        nt.links.new(uv.outputs['UV'], tex.inputs['Vector'])
        nt.links.new(tex.outputs['Color'], em)
        bsdf.inputs['Emission Strength'].default_value = 1.0


def main():
    args = parse_argv()
    spec = json.load(open(args.spec, 'r', encoding='utf-8'))

    reset_scene()
    configure_cycles(args.samples)
    meshes = load_glb(args.glb)
    print(f"[lmap] imported {len(meshes)} meshes")
    add_environment(strength=1.6)
    add_sun()
    add_fill_areas(meshes)
    add_fixtures(spec)

    # 1) Per-mesh UV2 unwrap + 2-pass bake (DIFFUSE light, then EMIT flatten)
    # The bake runs on every mesh, but only SHELL_MATS receive the result —
    # see attach_flattened_to_materials — so lamps/art/glass/etc keep their
    # original emission, transparency and runtime light response.
    for i, obj in enumerate(meshes):
        unwrap_lightmap_uv(obj)
        light_img = make_lightmap_image(f"lightmap_{i:03d}", args.resolution)
        flat_img  = make_lightmap_image(f"baked_{i:03d}",    args.resolution)
        setup_bake_target(obj, light_img)
        print(f"[lmap] pass 1/2 (DIFFUSE light) {obj.name}…")
        bake_lightmap(obj, light_img)
        print(f"[lmap] pass 2/2 (EMIT flatten) {obj.name}…")
        bake_emit_flatten(obj, light_img, flat_img)
        attach_flattened_to_materials(obj, flat_img)

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

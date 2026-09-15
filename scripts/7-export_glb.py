import os
import sys
import bpy
from bpy_extras import anim_utils


def get_arg(name, default=None):
    """Recupere la valeur d'un argument --name dans sys.argv (apres --)."""
    args = sys.argv
    if "--" in args:
        args = args[args.index("--") + 1:]
    for i, arg in enumerate(args):
        if arg == f"--{name}" and i + 1 < len(args):
            return args[i + 1]
    return default


# Load the .blend file saved by the previous script
blend_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "morrowind.blend")
for i, arg in enumerate(sys.argv):
    if arg == "--blend" and i + 1 < len(sys.argv):
        blend_path = sys.argv[i + 1]
        break

if os.path.exists(blend_path):
    bpy.ops.wm.open_mainfile(filepath=blend_path)
    print(f"[+] Loaded: {blend_path}")

# Parse --output from sys.argv
output_path = get_arg("output", "morrowind.glb")

# Séquences à exporter : liste de préfixes (insensible à la casse) ou "all"
ANIMS_ARG = get_arg(
    "anims",
    "all",
)
KEEP_PREFIXES = (
    None
    if ANIMS_ARG.strip().lower() == "all"
    else [p.strip().lower() for p in ANIMS_ARG.split(",") if p.strip()]
)

print(f"[+] Exporting GLB to: {output_path}")


# ==========================================
# ANIMATIONS
# ==========================================

def split_marker_actions():
    """Découpe les actions Morrowind (pose markers '<Seq>: Start|Stop')
    en pistes NLA nommées -> chaque piste devient une animation dans le GLB."""
    for ob in bpy.data.objects:
        if ob.type != 'ARMATURE':
            continue
        ad = ob.animation_data
        if not ad or not ad.action:
            continue
        src = ad.action
        if not src.pose_markers:
            continue

        # Regrouper les marqueurs par clip : "Idle2: Start" -> Idle2,
        # "SpellCast: Equip Start" -> SpellCast_Equip, etc.
        clips = {}
        for m in src.pose_markers:
            if ":" not in m.name:
                continue
            seq, phase = (p.strip() for p in m.name.split(":", 1))
            if phase.endswith("Start"):
                base = phase[: -len("Start")].strip()
                name = f"{seq}_{base}" if base else seq
                entry = clips.setdefault(name, [m.frame, None])
                entry[0] = m.frame if entry[0] is None else min(entry[0], m.frame)
            elif phase.endswith("Stop"):
                base = phase[: -len("Stop")].strip()
                name = f"{seq}_{base}" if base else seq
                entry = clips.setdefault(name, [None, m.frame])
                entry[1] = m.frame if entry[1] is None else min(entry[1], m.frame)

        # Piste bonus : l'action complète telle quelle
        src_slot = next(iter(src.slots))
        full_track = ad.nla_tracks.new()
        full_track.name = f"{src.name}_FULL"
        full_strip = full_track.strips.new(name=full_track.name, start=0, action=src)
        full_strip.action_slot = src_slot

        ad.action = None

        created = []
        for clip_name, (start, stop) in sorted(clips.items()):
            if start is None or stop is None or stop <= start:
                continue
            if KEEP_PREFIXES and not any(clip_name.lower().startswith(p) for p in KEEP_PREFIXES):
                continue

            new_act = bpy.data.actions.new(clip_name)
            slot = new_act.slots.new(id_type='OBJECT', name=ob.name)
            cbag = anim_utils.action_ensure_channelbag_for_slot(new_act, slot)
            offset = -start
            copied = 0

            for layer in src.layers:
                for strip in layer.strips:
                    for scbag in strip.channelbags:
                        for fc in scbag.fcurves:
                            keys = [kp for kp in fc.keyframe_points if start <= kp.co.x <= stop]
                            if not keys:
                                continue
                            group = fc.group.name if fc.group else ""
                            nfc = cbag.fcurves.ensure(fc.data_path, index=fc.array_index, group_name=group)
                            nfc.keyframe_points.add(len(keys))
                            for dst_kp, src_kp in zip(nfc.keyframe_points, keys):
                                dst_kp.co = (src_kp.co.x + offset, src_kp.co.y)
                                dst_kp.handle_left = (src_kp.handle_left.x + offset, src_kp.handle_left.y)
                                dst_kp.handle_right = (src_kp.handle_right.x + offset, src_kp.handle_right.y)
                                dst_kp.interpolation = src_kp.interpolation
                                dst_kp.easing = src_kp.easing
                                dst_kp.handle_left_type = src_kp.handle_left_type
                                dst_kp.handle_right_type = src_kp.handle_right_type
                            copied += len(keys)

            if not copied:
                bpy.data.actions.remove(new_act)
                continue

            track = ad.nla_tracks.new()
            track.name = clip_name
            strip = track.strips.new(name=clip_name, start=0, action=new_act)
            strip.action_slot = slot
            created.append(clip_name)

        print(f"[+] {ob.name}: {len(created)} animations découpées: {created}")


def clear_material_animations():
    """Supprime les actions parasites des node trees de matériaux."""
    removed = 0
    for mat in bpy.data.materials:
        nt = getattr(mat, "node_tree", None)
        if nt and nt.animation_data:
            nt.animation_data_clear()
            removed += 1
    if removed:
        print(f"[+] Cleaned {removed} material node tree actions")


split_marker_actions()
clear_material_animations()
bpy.data.orphans_purge()

# Export only visible objects
bpy.ops.export_scene.gltf(
    filepath=output_path,
    export_format='GLB',
    use_visible=True,
    export_apply=True,
    export_animations=True,
    export_animation_mode='NLA_TRACKS',
)

print(f"[+] GLB exported successfully: {output_path}")

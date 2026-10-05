import os
import sys
import time
import re
import bpy
from bpy_extras import anim_utils

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mwlog import vprint, eprint, Progress


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
    vprint(f"[+] Loaded: {blend_path}")

# Parse --output from sys.argv (.gltf : the .bin is written next to it)
output_path = os.path.abspath(get_arg("output", "morrowind.gltf"))

# Textures written by the exporter: shared folder, passed with --textures-dir.
# Default: "textures_gltf" next to the .gltf.
TEX_DIR = os.path.abspath(
    get_arg("textures-dir") or os.path.join(os.path.dirname(output_path), "textures_gltf")
)
# export_texture_dir is relative to the .gltf file
TEX_REL = os.path.relpath(TEX_DIR, os.path.dirname(output_path))
os.makedirs(TEX_DIR, exist_ok=True)

# Séquences à exporter : liste de préfixes (insensible à la casse) ou "all"
ANIMS_ARG = get_arg(
    "anims",
    "all",
)
# --anims none : no animation at all (fastest export, useful to find out what is slow)
EXPORT_ANIMATIONS = ANIMS_ARG.strip().lower() != "none"
KEEP_PREFIXES = (
    None
    if ANIMS_ARG.strip().lower() == "all"
    else [p.strip().lower() for p in ANIMS_ARG.split(",") if p.strip()]
)

vprint(f"[+] Exporting glTF to: {output_path}")
vprint(f"[+] Textures folder: {TEX_DIR} (relative: {TEX_REL})")


# ==========================================
# ANIMATIONS
# ==========================================

def split_marker_actions():
    """Découpe les actions Morrowind (pose markers '<Seq>: Start|Stop')
    en pistes NLA nommées -> chaque piste devient une animation dans le glTF."""
    _seen_skeletons = set()

    for ob in bpy.data.objects:
        if ob.type != 'ARMATURE':
            continue
        ad = ob.animation_data
        if not ad or not ad.action:
            continue
        src = ad.action
        if not src.pose_markers:
            continue

        sig = tuple(sorted(b.name for b in ob.data.bones))
        if sig in _seen_skeletons:
            ad.action = None          # doublon: pas d'animations
            continue
        _seen_skeletons.add(sig)

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

        # Piste bonus : l'action complète telle quelle. Seulement sans filtre --anims :
        # avec un filtre (ex. idle), elle réintroduirait les animations écartées.
        if KEEP_PREFIXES is None:
            src_slot = next(iter(src.slots))
            full_track = ad.nla_tracks.new()
            full_track.name = f"{src.name}_FULL"
            full_strip = full_track.strips.new(name=full_track.name, start=0, action=src)
            full_strip.action_slot = src_slot

        ad.action = None

        created = []
        _clips = sorted(clips.items())
        _cbar = Progress(len(_clips), f"Animations {ob.name}", disable=not _clips)
        _cbar.__enter__()
        for clip_name, (start, stop) in _clips:
            _cbar.update()
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

        _cbar.close()
        vprint(f"[+] {ob.name}: {len(created)} animations split: {created}")


# ==========================================
# TEXTURES
# ==========================================

def merge_and_shrink_images(max_px=2_000_000, min_side=256):
    """Fusionne les images de même nom de base et même taille (x.dds / x / x.001),
    puis divise par 2 les textures trop grosses."""
    by_key = {}
    for img in list(bpy.data.images):
        if img.type != 'IMAGE' or img.users == 0:
            continue
        stem = re.sub(r'(\.(dds|tga|png))?(\.\d{3})?$', '', img.name.lower())
        key = (stem, tuple(img.size))
        keep = by_key.setdefault(key, img)
        if keep is not img:
            img.user_remap(keep)

    shrunk = 0
    for img in bpy.data.images:
        if img.type != 'IMAGE' or img.users == 0:
            continue
        w, h = img.size
        nw, nh = w, h
        while nw * nh > max_px and min(nw, nh) > min_side:
            nw, nh = max(nw // 2, 1), max(nh // 2, 1)
        if (nw, nh) != (w, h):
            img.scale(nw, nh)
            shrunk += 1
    vprint(f"[+] {len(by_key)} unique image(s), {shrunk} downscaled")


def clear_material_animations():
    """Supprime les actions parasites des node trees de matériaux."""
    removed = 0
    for mat in bpy.data.materials:
        nt = getattr(mat, "node_tree", None)
        if nt and nt.animation_data:
            nt.animation_data_clear()
            removed += 1
    if removed:
        vprint(f"[+] Cleaned {removed} material node tree actions")


_steps = Progress(4, "Export glTF", eta=False)   # the export itself dominates: an ETA would be meaningless
_steps.__enter__()
_steps.update()           # .blend charge
if EXPORT_ANIMATIONS:
    split_marker_actions()
_steps.update()           # animations decoupees
clear_material_animations()
merge_and_shrink_images()
bpy.data.orphans_purge()
_steps.update()           # nettoyage

# The glTF export is ONE long blocking call (minutes on big exterior scenes: thousands of
# objects, ~2000 materials). The bar can't advance inside it, so a heartbeat keeps the
# elapsed time ticking: if it counts, the export is alive.
_steps.set_label("Writing glTF")
_steps.start_heartbeat()
_t_export = time.time()

# Export only visible objects. GLTF_SEPARATE: .gltf + .bin, textures written once in the
# shared folder and referenced by relative URI.
bpy.ops.export_scene.gltf(
    filepath=output_path,
    export_format='GLTF_SEPARATE',
    export_texture_dir=TEX_REL,
    use_visible=True,
    export_apply=True,
    export_animations=EXPORT_ANIMATIONS,
    export_animation_mode='NLA_TRACKS',
    export_optimize_animation_size=True,
    export_optimize_animation_keep_anim_armature=False,
    export_force_sampling=False,
)

_steps.update()           # export ecrit
_steps.close()
vprint(f"[+] glTF exported successfully in {time.time() - _t_export:.0f}s: {output_path}")
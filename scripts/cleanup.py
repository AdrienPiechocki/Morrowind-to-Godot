import os
import sys
import bpy

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mwlog import vprint, eprint, Progress
from mwcompat import has_nodes

# Load the .blend file saved by the previous script
blend_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "morrowind.blend")
for i, arg in enumerate(sys.argv):
    if arg == "--blend" and i + 1 < len(sys.argv):
        blend_path = sys.argv[i + 1]
        break

if os.path.exists(blend_path):
    bpy.ops.wm.open_mainfile(filepath=blend_path)
    vprint(f"[+] Loaded: {blend_path}")

# Hide non-renderable objects and all their descendants
def hide_recursive(obj):
    obj.hide_viewport = True
    obj.hide_render = True
    for child in obj.children:
        hide_recursive(child)

_objs = list(bpy.data.objects)
_bar = Progress(len(_objs), "Hiding", disable=not _objs)
_bar.__enter__()
for obj in _objs:
    _bar.update()
    if "shadow" in obj.name.lower() or "marker_north" in obj.name.lower() or "editormarker" in obj.name.lower() or "marker_prison" in obj.name.lower():
        hide_recursive(obj)

_bar.close()

# Remove Blender default objects
for obj_name in ["Cube", "Light", "Camera"]:
    obj = bpy.data.objects.get(obj_name)
    if obj:
        bpy.data.objects.remove(obj, do_unlink=True)


REMOVE_TEXTURE_PATTERNS = ["door_icon", "dm_decal"]

removed = 0
_meshes = list(bpy.data.objects)
_bar = Progress(len(_meshes), "Unwanted textures", disable=not _meshes)
_bar.__enter__()
for obj in _meshes:
    _bar.update()
    if obj.type != 'MESH':
        continue
    found = False
    for slot in obj.material_slots:
        mat = slot.material
        if not has_nodes(mat):
            continue
        for node in mat.node_tree.nodes:
            img = None
            if node.type == 'TEX_IMAGE' and node.image:
                img = node.image
            elif node.type == 'GROUP' and node.node_tree:
                for sub in node.node_tree.nodes:
                    if sub.type == 'TEX_IMAGE' and sub.image:
                        img = sub.image
                        break
            if img:
                name = img.name.lower()
                fpath = img.filepath.lower()
                for pattern in REMOVE_TEXTURE_PATTERNS:
                    if pattern in name or pattern in fpath:
                        vprint(f"[-] Removing '{obj.name}' (texture: {img.name})")
                        bpy.data.objects.remove(obj, do_unlink=True)
                        removed += 1
                        found = True
                        break
            if found:
                break
        if found:
            break

_bar.close()

if removed:
    vprint(f"[+] {removed} blackbox object(s) removed.")


bpy.ops.wm.save_as_mainfile(filepath=blend_path, compress=True)
vprint(f"[+] Saved: {blend_path}")
import os
import sys
import bpy

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mwlog import vprint, eprint, Progress

# Load the .blend file saved by the previous script
blend_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "morrowind.blend")
for i, arg in enumerate(sys.argv):
    if arg == "--blend" and i + 1 < len(sys.argv):
        blend_path = sys.argv[i + 1]
        break

if os.path.exists(blend_path):
    bpy.ops.wm.open_mainfile(filepath=blend_path)
    vprint(f"[+] Loaded: {blend_path}")

# Godot suffix: '-colonly' (Trimesh, visible mesh removed) or '-convcolonly' (Convex, visible
# mesh removed). The '-col' / '-convcol' variants keep the mesh visible, so they are not used.
SUFFIXE = "-colonly"
# Suffixes written by earlier runs: renamed to SUFFIXE (idempotent migration)
OLD_SUFFIXES = ("-convcol", "-col")

count = 0
_objs = list(bpy.data.objects)
_bar = Progress(len(_objs), "Collisions", disable=not _objs)
_bar.__enter__()
for obj in _objs:
    _bar.update()
    for child in obj.children:
        if "collision" in child.name.lower():
            for _child in child.children:
                if _child.type != 'MESH':
                    continue
                if _child.name.endswith(SUFFIXE):
                    continue

                base = _child.name
                for old in OLD_SUFFIXES:
                    if base.endswith(old):
                        base = base[: -len(old)]
                        break

                _child.name = base + SUFFIXE
                count += 1

_bar.close()
vprint(f"[+] {count} object(s) renamed with suffix {SUFFIXE}")

bpy.ops.wm.save_as_mainfile(filepath=blend_path, compress=True)
vprint(f"[+] Saved: {blend_path}")
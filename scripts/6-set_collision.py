import os
import sys
import bpy

# Load the .blend file saved by the previous script
blend_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "morrowind.blend")
for i, arg in enumerate(sys.argv):
    if arg == "--blend" and i + 1 < len(sys.argv):
        blend_path = sys.argv[i + 1]
        break

if os.path.exists(blend_path):
    bpy.ops.wm.open_mainfile(filepath=blend_path)
    print(f"[+] Loaded: {blend_path}")

# Godot suffix: '-col' for Trimesh (precise) or '-convcol' for Convex (optimized)
SUFFIXE = "-col"

for obj in bpy.data.objects:
    # Target only 3D meshes
    if obj.type == 'MESH':
        # Avoid duplicates if script is rerun
        if not (obj.name.endswith("-col") or obj.name.endswith("-convcol")):
            obj.name += SUFFIXE

bpy.ops.wm.save_as_mainfile(filepath=blend_path)
print(f"[+] Saved: {blend_path}")

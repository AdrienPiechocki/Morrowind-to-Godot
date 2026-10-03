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
THRESHOLD = 1.0

import math

count = 0
for obj in bpy.data.objects:
    if obj.type != 'MESH':
        continue
    if obj.name.endswith("-col") or obj.name.endswith("-convcol"):
        continue

    dims = obj.dimensions
    diameter = math.sqrt(dims.x**2 + dims.y**2 + dims.z**2)
    if diameter >= THRESHOLD:
        obj.name += SUFFIXE
        count += 1

print(f"[+] {count} objets > {THRESHOLD} unites renommes avec {SUFFIXE}")

bpy.ops.wm.save_as_mainfile(filepath=blend_path, compress=True)
print(f"[+] Saved: {blend_path}")

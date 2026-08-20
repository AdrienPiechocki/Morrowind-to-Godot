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

# Parse --output from sys.argv
output_path = "morrowind.glb"

for i, arg in enumerate(sys.argv):
    if arg == "--output" and i + 1 < len(sys.argv):
        output_path = sys.argv[i + 1]
        break

print(f"[+] Exporting GLB to: {output_path}")

# Export only visible objects
bpy.ops.export_scene.gltf(
    filepath=output_path,
    export_format='GLB',
    use_visible=True,
    export_apply=True,
)

print(f"[+] GLB exported successfully: {output_path}")

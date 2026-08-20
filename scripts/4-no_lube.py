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

for mat in bpy.data.materials:
    if mat.use_nodes and mat.node_tree:
        for node in mat.node_tree.nodes:
            if node.type == 'BSDF_PRINCIPLED':
                # Increase roughness (0.0 = mirror/wet, 1.0 = fully matte)
                node.inputs['Roughness'].default_value = 1.0
                
                # Reduce specular (compatible with Blender 3.x and 4.x+)
                if 'Specular' in node.inputs:
                    node.inputs['Specular'].default_value = 0.0
                elif 'Specular IOR Level' in node.inputs:
                    node.inputs['Specular IOR Level'].default_value = 0.0

bpy.ops.wm.save_as_mainfile(filepath=blend_path)
print(f"[+] Saved: {blend_path}")

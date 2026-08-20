import os
import sys
import bpy

blend_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "morrowind.blend")
for i, arg in enumerate(sys.argv):
    if arg == "--blend" and i + 1 < len(sys.argv):
        blend_path = sys.argv[i + 1]
        break

if os.path.exists(blend_path):
    bpy.ops.wm.open_mainfile(filepath=blend_path)
    print(f"[+] Charge: {blend_path}")

print(f"[+] {len(bpy.data.materials)} materials avant nettoyage")

def get_material_images(mat):
    paths = set()
    if not mat or not mat.use_nodes:
        return paths
    for node in mat.node_tree.nodes:
        if node.type == 'TEX_IMAGE' and node.image:
            fp = node.image.filepath.lower()
            if fp:
                paths.add(fp)
    return paths

path_map = {}
deduped = 0

for mat in list(bpy.data.materials):
    images = get_material_images(mat)
    if not images:
        continue
    key = frozenset(images)
    if key in path_map:
        existing = path_map[key]
        for obj in bpy.data.objects:
            for slot in obj.material_slots:
                if slot.material == mat:
                    slot.material = existing
        bpy.data.materials.remove(mat)
        deduped += 1
    else:
        path_map[key] = mat

print(f"[+] {deduped} materials dupliques supprimes")
print(f"[+] {len(path_map)} materials uniques restants")

bpy.ops.wm.save_as_mainfile(filepath=blend_path)
print(f"[+] Sauvegarde: {blend_path}")

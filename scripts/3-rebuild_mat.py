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


def rebuild_clean_gltf_materials():
    rebuilt_count = 0

    for mat in bpy.data.materials:
        if not mat:
            continue

        mat.use_nodes = True
        nodes = mat.node_tree.nodes
        links = mat.node_tree.links

        # 1. Find the image linked to this material
        img = None
        for node in list(nodes):
            if node.type == "TEX_IMAGE" and node.image:
                img = node.image
                break
            elif node.type == "GROUP" and node.node_tree:
                for sub in node.node_tree.nodes:
                    if sub.type == "TEX_IMAGE" and sub.image:
                        img = sub.image
                        break
                if img:
                    break

        # If image not found via nodes, try matching by image name
        if not img:
            mat_clean = mat.name.split(".")[0].lower()
            for image in bpy.data.images:
                if mat_clean in image.name.lower():
                    img = image
                    break

        # 2. CLEANUP: Remove stray nodes from the NIF addon
        nodes.clear()

        # 3. REBUILD: Standard 100% glTF-compatible structure
        out_node = nodes.new("ShaderNodeOutputMaterial")
        out_node.location = (300, 0)

        bsdf_node = nodes.new("ShaderNodeBsdfPrincipled")
        bsdf_node.location = (0, 0)

        links.new(bsdf_node.outputs["BSDF"], out_node.inputs["Surface"])

        if img:
            # Physically pack the texture into Blender data
            if not img.packed_file:
                try:
                    img.pack()
                except Exception as e:
                    print(
                        f"Failed to pack image {img.name}: {e}"
                    )

            tex_node = nodes.new("ShaderNodeTexImage")
            tex_node.image = img
            tex_node.location = (-350, 0)

            # Direct link from color to Base Color channel
            links.new(tex_node.outputs["Color"], bsdf_node.inputs["Base Color"])
            rebuilt_count += 1
            print(f"[REBUILT] Material '{mat.name}' -> Image '{img.name}'")
        else:
            print(f"[NO TEXTURE] Material '{mat.name}' reset.")

    print(f"[+] {rebuilt_count} material(s) cleaned and linked successfully.")


rebuild_clean_gltf_materials()

bpy.ops.wm.save_as_mainfile(filepath=blend_path)
print(f"[+] Saved: {blend_path}")
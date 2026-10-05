import os
import sys
import bpy

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mwlog import vprint, eprint, Progress
from mwcompat import ensure_nodes


# Load the .blend file saved by the previous script
blend_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "morrowind.blend")
for i, arg in enumerate(sys.argv):
    if arg == "--blend" and i + 1 < len(sys.argv):
        blend_path = sys.argv[i + 1]
        break

if os.path.exists(blend_path):
    bpy.ops.wm.open_mainfile(filepath=blend_path)
    vprint(f"[+] Loaded: {blend_path}")


def tree_uses_alpha(nt, seen=None):
    """True if the original (NIF addon) node tree actually used transparency:
    an 'Alpha' output wired somewhere, or a Principled alpha that is linked / < 1.
    Looks inside node groups too."""
    if nt is None:
        return False
    seen = seen if seen is not None else set()
    if nt.as_pointer() in seen:
        return False
    seen.add(nt.as_pointer())

    for link in nt.links:
        if link.from_socket.name == "Alpha":
            return True
    for node in nt.nodes:
        if node.type == "BSDF_PRINCIPLED":
            a = node.inputs.get("Alpha")
            if a is not None and (a.is_linked or a.default_value < 1.0):
                return True
        elif node.type == "GROUP" and tree_uses_alpha(node.node_tree, seen):
            return True
    return False


def rebuild_clean_gltf_materials():
    rebuilt_count = 0

    mats = list(bpy.data.materials)
    bar = Progress(len(mats), "Materials", disable=not mats)
    bar.__enter__()

    for mat in mats:
        bar.update()
        if not mat:
            continue

        if ensure_nodes(mat) is None:
            continue    # no node tree (e.g. grease pencil material)
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

        # 2. Remember whether the original material used transparency BEFORE wiping
        #    it. Without the Alpha link the glTF exporter drops the alpha channel.
        uses_alpha = tree_uses_alpha(mat.node_tree) or getattr(mat, "blend_method", "OPAQUE") in ("BLEND", "CLIP", "HASHED")

        # 3. CLEANUP: Remove stray nodes from the NIF addon
        nodes.clear()

        # 4. REBUILD: Standard 100% glTF-compatible structure
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
                    vprint(
                        f"Failed to pack image {img.name}: {e}"
                    )

            tex_node = nodes.new("ShaderNodeTexImage")
            tex_node.image = img
            tex_node.location = (-350, 0)

            # Direct link from color to Base Color channel
            links.new(tex_node.outputs["Color"], bsdf_node.inputs["Base Color"])

            # Keep transparency: texture alpha -> Principled Alpha
            if uses_alpha:
                links.new(tex_node.outputs["Alpha"], bsdf_node.inputs["Alpha"])
                # glTF export maps blend_method to alphaMode; never leave it OPAQUE here
                if getattr(mat, "blend_method", None) == "OPAQUE":
                    try:
                        mat.blend_method = "BLEND"
                    except Exception:
                        pass

            rebuilt_count += 1
            vprint(f"[REBUILT] Material '{mat.name}' -> Image '{img.name}'" + (" (alpha)" if uses_alpha else ""))
        else:
            vprint(f"[NO TEXTURE] Material '{mat.name}' reset.")

    bar.close()
    vprint(f"[+] {rebuilt_count} material(s) cleaned and linked successfully.")


rebuild_clean_gltf_materials()

bpy.ops.wm.save_as_mainfile(filepath=blend_path)
vprint(f"[+] Saved: {blend_path}")
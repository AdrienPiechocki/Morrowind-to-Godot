import sys
import json
import os
import addon_utils
import bpy
from mathutils import Euler, Vector, Matrix


# ==========================================
# Parse CLI arguments (after Blender's --)
# ==========================================

def get_arg(name, default=None):
    """Recupere la valeur d'un argument --name dans sys.argv."""
    args = sys.argv
    if "--" in args:
        args = args[sys.argv.index("--") + 1:]
    for i, arg in enumerate(args):
        if arg == f"--{name}" and i + 1 < len(args):
            return args[i + 1]
    return default


# ==========================================
# Ensure NIF plugin is enabled
# ==========================================

addon_name = "io_scene_mw"
loaded_default, loaded_state = addon_utils.check(addon_name)

if not loaded_state:
    addon_utils.enable(addon_name, default_set=True)


# ==========================================
# CONFIGURATION
# ==========================================

JSON_FILE_PATH = get_arg("json")

_meshes = get_arg("meshes", r"../data/meshes")
MESHES_DIRS = [_meshes]

_textures = get_arg("textures", r"../data/textures")
TEXTURES_DIRS = [_textures]

# Morrowind -> Blender scale factor
SCALE_FACTOR = 0.01

# ==========================================
# REGLAGES DE ROTATION (CORRECTION)
# ==========================================
# Morrowind applique Rx * Ry * Rz. Dans Blender, cela correspond à 'ZYX'.
# Si 'ZYX' ne donne pas un résultat parfait, testez 'ZXY'.
EULER_ORDER = 'ZYX'

# Facteurs de signe pour les axes (1.0 ou -1.0 si un axe est inversé)
ROT_SIGN_X = -1.0
ROT_SIGN_Y = -1.0
ROT_SIGN_Z = -1.0

# Cell filtering (Set to None to rebuild everything)
_cell = get_arg("cell", "Balmora, Temple")
TARGET_CELL_NAME = None if _cell == "all" else _cell

# Record types that should not be imported
EXCLUDED_TYPES = {
    "Npc",
    "Creature",
    "BodyPart",
}

# Cache of imported meshes
imported_mesh_cache = {}


# ==========================================
# JSON & FILE SYSTEM
# ==========================================

def load_tes3_json(filepath):
    with open(filepath, "r", encoding="utf-8") as f:
        return json.load(f)


def build_record_map(data):
    record_map = {}
    for record in data:
        rec_id = record.get("id")
        if rec_id:
            record_map[rec_id.lower()] = {
                "mesh": record.get("mesh"),
                "type": record.get("type"),
            }
    return record_map


def resolve_case_insensitive(path):
    if os.path.exists(path):
        return path

    parts = os.path.normpath(path).split(os.sep)
    current = "/" if path.startswith("/") else ""

    for part in parts:
        if not part:
            continue
        exact_match = os.path.join(current, part)
        if os.path.exists(exact_match):
            current = exact_match
            continue

        found = False
        try:
            entries = os.listdir(current or ".")
            for entry in entries:
                if entry.lower() == part.lower():
                    current = os.path.join(current, entry)
                    found = True
                    break
        except Exception:
            pass

        if not found:
            return None

    return current


def find_nif_file(mesh_relative_path):
    clean_path = mesh_relative_path.replace("\\", "/")
    for base_dir in MESHES_DIRS:
        full_path = os.path.join(base_dir, clean_path)
        resolved_path = resolve_case_insensitive(full_path)
        if resolved_path and os.path.isfile(resolved_path):
            return resolved_path
    return None


# ==========================================
# MESH IMPORT
# ==========================================

def get_or_import_mesh(mesh_relative_path):
    clean_key = mesh_relative_path.replace("\\", "/").lower()

    if clean_key in imported_mesh_cache:
        return imported_mesh_cache[clean_key]

    resolved_nif_path = find_nif_file(mesh_relative_path)

    if resolved_nif_path:
        bpy.ops.object.select_all(action="DESELECT")
        objs_before = set(bpy.data.objects)

        try:
            bpy.ops.import_scene.mw(filepath=resolved_nif_path)
            imported_objs = [o for o in bpy.data.objects if o not in objs_before]

            if imported_objs:
                for obj in imported_objs:
                    # Nettoyer les clés d'animation du NIF
                    if obj.animation_data:
                        obj.animation_data_clear()

                    # Délier des collections de la scène pour ne pas encombrer le point (0,0,0)
                    for col in list(obj.users_collection):
                        col.objects.unlink(obj)

                imported_mesh_cache[clean_key] = imported_objs
                return imported_objs

        except Exception as e:
            print(f"[-] Import error for '{resolved_nif_path}': {e}")
    else:
        print(f"[-] Mesh file not found on disk: {mesh_relative_path}")

    return None


# ==========================================
# CELL RECONSTRUCTION
# ==========================================

def rebuild_cells_in_blender():
    data = load_tes3_json(JSON_FILE_PATH)
    record_map = build_record_map(data)

    cell_count = 0
    object_count = 0

    # Dedupliquer les cellules par nom : en cas de doublon (vanilla + mod),
    # on garde la derniere occurrence (les mods sont apres le base game)
    seen_cells = {}
    for record in data:
        if record.get("type") != "Cell":
            continue
        cell_key = record.get("name") or f"Cell_{record.get('data', {}).get('grid')}"
        seen_cells[cell_key] = record

    # Filtrer par cellule cible si specifiee
    if TARGET_CELL_NAME is not None:
        cells_to_process = [seen_cells[TARGET_CELL_NAME]] if TARGET_CELL_NAME in seen_cells else []
    else:
        cells_to_process = list(seen_cells.values())

    for cell in cells_to_process:
        cell_label = cell.get("name") or f"Cell_{cell.get('data', {}).get('grid')}"
        collection_name = f"MW_{cell_label}"

        print()
        print("=" * 60)
        print(f"[+] Processing cell: {cell_label}")
        print("=" * 60)

        if collection_name in bpy.data.collections:
            cell_collection = bpy.data.collections[collection_name]
        else:
            cell_collection = bpy.data.collections.new(collection_name)
            bpy.context.scene.collection.children.link(cell_collection)

        cell_count += 1
        references = cell.get("references", [])

        # Trouver la position de la premiere reference pour centrer la cellule
        origin_offset = Vector((0.0, 0.0, 0.0))
        for ref in references:
            if ref.get("deleted", False):
                continue
            ref_id = ref.get("id", "Unknown_Ref")
            rec_info = record_map.get(ref_id.lower(), {})
            if rec_info.get("type") in EXCLUDED_TYPES:
                continue
            if not rec_info.get("mesh"):
                continue
            origin_offset = Vector(ref.get("translation", [0.0, 0.0, 0.0])) * SCALE_FACTOR
            break

        print(f"[+] References in cell: {len(references)}")
        print(f"[+] Origin offset: {origin_offset[:]}" if origin_offset.length > 0 else "[+] No offset (already centered)")

        for ref in references:
            if ref.get("deleted", False):
                continue

            ref_id = ref.get("id", "Unknown_Ref")
            rec_info = record_map.get(ref_id.lower(), {})

            if rec_info.get("type") in EXCLUDED_TYPES:
                continue

            mesh_path = rec_info.get("mesh")
            if not mesh_path:
                continue

            translation = ref.get("translation", [0.0, 0.0, 0.0])
            rotation = ref.get("rotation", [0.0, 0.0, 0.0])
            scale = ref.get("scale", 1.0)

            # Position ajustée à l'échelle Blender, centrée sur la premiere reference
            pos = Vector(translation) * SCALE_FACTOR - origin_offset

            # --- CORRECTION DE LA ROTATION ---
            rx, ry, rz = rotation
            rot_euler = Euler(
                (rx * ROT_SIGN_X, ry * ROT_SIGN_Y, rz * ROT_SIGN_Z),
                EULER_ORDER
            )

            template_objs = get_or_import_mesh(mesh_path)

            if template_objs:
                copied_map = {}

                # 1. Duplication des objets de la hiérarchie NIF
                for t_obj in template_objs:
                    new_obj = t_obj.copy()

                    if new_obj.animation_data:
                        new_obj.animation_data_clear()

                    new_obj.hide_set(False)
                    new_obj.hide_viewport = False
                    new_obj.hide_render = False

                    cell_collection.objects.link(new_obj)
                    copied_map[t_obj] = new_obj

                # 2. Restauration des relations parent-enfant
                roots = []
                for t_obj in template_objs:
                    new_obj = copied_map[t_obj]
                    if t_obj.parent in copied_map:
                        new_obj.parent = copied_map[t_obj.parent]
                        new_obj.matrix_local = t_obj.matrix_local.copy()
                    else:
                        roots.append((t_obj, new_obj))

                # 3. Application de la matrice de transformation de référence
                mat_trans = Matrix.Translation(pos)
                mat_rot = rot_euler.to_matrix().to_4x4()
                mat_scale = Matrix.Diagonal((scale, scale, scale, 1.0))

                # Matrice globale de la référence dans la cellule
                mat_ref = mat_trans @ mat_rot @ mat_scale

                for t_root, new_root in roots:
                    # On combine la transformation de la cellule avec la matrice locale originale du NIF
                    # Appliquer les signes d'axes aussi sur la rotation du NIF
                    nif_euler = t_root.matrix_local.to_euler(EULER_ORDER)
                    nif_euler_signed = Euler(
                        (nif_euler.x * ROT_SIGN_X, nif_euler.y * ROT_SIGN_Y, nif_euler.z * ROT_SIGN_Z),
                        EULER_ORDER
                    )
                    nif_mat = nif_euler_signed.to_matrix().to_4x4()
                    nif_mat.translation = t_root.matrix_local.translation
                    nif_mat = nif_mat @ Matrix.Diagonal((*t_root.matrix_local.to_scale(), 1.0))
                    new_root.matrix_world = mat_ref @ nif_mat

                object_count += len(copied_map)

            else:
                # Placeholder if the NIF file is not found
                obj = bpy.data.objects.new(f"{ref_id}_placeholder", None)
                cell_collection.objects.link(obj)
                obj.location = pos
                obj.rotation_euler = rot_euler
                obj.scale = (scale, scale, scale)
                object_count += 1

        print(f"[+] Finished cell: {cell_label}")

    # Force le rafraîchissement complet de la scène Blender
    bpy.context.view_layer.update()

    print()
    print("=" * 60)
    print("[+] RECONSTRUCTION COMPLETE")
    print("=" * 60)
    print(f"[+] Cells:   {cell_count}")
    print(f"[+] Objects: {object_count}")
    print(f"[+] Cached meshes: {len(imported_mesh_cache)}")
    print("=" * 60)


# ==========================================
# TEXTURES
# ==========================================

def fix_missing_textures():
    rebound_count = 0

    # Construire un index recursif de toutes les textures disponibles
    tex_index = {}
    for tex_dir in TEXTURES_DIRS:
        resolved_dir = resolve_case_insensitive(tex_dir)
        if not resolved_dir or not os.path.isdir(resolved_dir):
            continue
        for root, _dirs, files in os.walk(resolved_dir):
            for f in files:
                key = f.lower()
                if key not in tex_index:
                    tex_index[key] = os.path.join(root, f)

    for img in bpy.data.images:
        raw_path = img.filepath.replace("\\", "/")

        if not raw_path or os.path.exists(bpy.path.abspath(img.filepath)):
            continue

        filename = os.path.basename(raw_path)
        base_name, _ = os.path.splitext(filename)

        # 1. Essayer avec le chemin relatif original (resolve_case_insensitive)
        found_path = None
        for tex_dir in TEXTURES_DIRS:
            full_path = os.path.join(tex_dir, raw_path)
            resolved = resolve_case_insensitive(full_path)
            if resolved and os.path.isfile(resolved):
                found_path = resolved
                break

        # 2. Chercher recursivement par nom de fichier dans l'index
        if not found_path:
            for ext in ["", ".dds", ".tga", ".png"]:
                key = (base_name + ext).lower()
                if key in tex_index:
                    found_path = tex_index[key]
                    break

        if found_path:
            img.filepath = found_path
            try:
                img.reload()
                if not img.packed_file:
                    img.pack()
            except Exception as e:
                print(f"[-] Failed to reload texture '{found_path}': {e}")
                continue
            rebound_count += 1
        else:
            print(f"[-] Missing texture file: {filename}")

    print(f"[+] Rebound {rebound_count} missing texture paths.")


# ==========================================
# EXECUTION PIPELINE
# ==========================================

print()
print("=" * 60)
print("MORROWIND WORLD RECONSTRUCTION")
print("=" * 60)
print()

rebuild_cells_in_blender()

print()
print("[+] Fixing missing textures...")
fix_missing_textures()

print()
print("[+] Saving Blender file...")

bpy.ops.wm.save_as_mainfile(filepath="morrowind.blend")

print()
print("=" * 60)
print("[+] DONE")
print("=" * 60)

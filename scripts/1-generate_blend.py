import sys
import json
import os
import re
import addon_utils
import bpy
import bmesh
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

# Mode npcs : recherche d'un NPC/créature par nom ou id dans toutes les cellules
TARGET_NPC_NAME = get_arg("npc", None)

# Mode d'import : "cell" (décor statique) ou "npc" (créatures/NPCs animés)
IMPORT_MODE = get_arg("mode", "cell")

# Record types that should not be imported (mode cells)
EXCLUDED_TYPES = {
    "Npc",
    "Creature",
    "BodyPart",
}

# Record types à importer (mode npcs)
ONLY_TYPES = {"Npc", "Creature"}

def should_import(rec_type):
    if IMPORT_MODE == "npc":
        return rec_type in ONLY_TYPES
    return rec_type not in EXCLUDED_TYPES

def get_ref_mesh_paths(rec_info):
    if IMPORT_MODE == "npc":
        return build_npc_mesh_paths(rec_info)
    mesh_path = rec_info.get("mesh")
    return [mesh_path] if mesh_path else []

# Maps pour l'assemblage des NPCs (bodyparts + vêtements/armures)
bodypart_map = {}
item_map = {}

# Cache of imported meshes
imported_mesh_cache = {}
# Instantanés des matrices locales PRISTINES de chaque fichier importé.
# Indispensable : l'importeur peut réécrire les matrix_local des imports
# suivants (contamination), on fige donc les valeurs juste après l'import.
imported_mesh_snapshots = {}

# Carte globale template -> copie : l'importeur partage le squelette Bip01
# entre les NIFs (les meshes d'un NIF pointent vers l'armature d'un autre),
# il faut donc résoudre les cibles au-delà du seul import courant.
GLOBAL_TEMPLATE_COPY_MAP = {}


# ==========================================
# JSON & FILE SYSTEM
# ==========================================

def load_tes3_json(filepath):
    with open(filepath, "r", encoding="utf-8") as f:
        return json.load(f)


# Types pouvant être référencés dans une cellule (évite que Dialogue/GameSetting/...
# qui partagent des ids avec les NPCs n'écrasent leur entrée)
REFERENCEABLE_TYPES = {
    "Activator", "Alchemy", "Apparatus", "Armor", "Book", "Clothing",
    "Container", "Creature", "Door", "Ingredient", "LeveledCreature",
    "LeveledItem", "Light", "Lockpick", "MiscItem", "Npc", "Probe",
    "RepairItem", "Static", "Weapon",
}


def build_record_map(data):
    record_map = {}
    for record in data:
        rec_id = record.get("id")
        if rec_id and record.get("type") in REFERENCEABLE_TYPES:
            record_map[rec_id.lower()] = record
    return record_map


def build_npc_lookup_maps(data):
    for record in data:
        rtype = record.get("type")
        rid = (record.get("id") or "").lower()
        if rtype == "Bodypart":
            bodypart_map[rid] = record
        elif rtype in ("Clothing", "Armor"):
            item_map[rid] = record


def get_default_skeleton(race, is_female):
    """Squelette d'animation par défaut selon la race (comme le jeu quand mesh est vide)."""
    kna = (race or "").strip().lower() in ("khajiit", "argonian")
    if kna:
        return "base_animkna.nif"
    return "base_anim_female.nif" if is_female else "base_anim.nif"


def build_npc_mesh_paths(rec):
    """Construit la liste [(chemin_nif, indice_slot)] composant un NPC.

    L'indice_slot ('Groin', 'LeftUpperLeg', 'Head'...) correspond au type
    d'attache du moteur : les pièces rigides sont modelées dans l'espace
    local de leur os et doivent être parentées à celui-ci.
    """
    entries = []
    is_female = "FEMALE" in (rec.get("npc_flags") or "")

    if rec.get("mesh"):
        entries.append((rec["mesh"], None))
    else:
        # Certains NPCs n'ont pas de modèle : squelette par défaut de la race
        entries.append((get_default_skeleton(rec.get("race"), is_female), None))

    # Tête et cheveux : parties de peau de la race (un casque les masque)
    for key, hint in (("head", "Head"), ("hair", "Hair")):
        bp = bodypart_map.get((rec.get(key) or "").lower())
        if bp and bp.get("mesh"):
            entries.append((bp["mesh"], hint, True))

    # Vêtements / armures portés
    for _count, item_id in rec.get("inventory", []):
        item = item_map.get(item_id.lower())
        if not item:
            continue  # armes & co : jamais importées (item_map = Clothing/Armor)
        for biped in item.get("biped_objects", []):
            slot_type = (biped.get("biped_object_type") or "")
            if re.sub(r"[\s_]+", "", slot_type).lower() in ("shield", "weapon"):
                continue  # boucliers/armes tenues : pas de pièce de corps
            bp_id = biped.get("male_bodypart")
            if is_female and biped.get("female_bodypart"):
                bp_id = biped.get("female_bodypart")
            bp = bodypart_map.get((bp_id or "").lower())
            if bp and bp.get("mesh"):
                entries.append((bp["mesh"], slot_type))

    # Peaux de la race (corps nu, mains, pieds...) : remplace les placeholders
    # génériques 'Tri *' du squelette base_anim.
    if rec.get("type") == "Npc":
        race = (rec.get("race") or "").strip().lower()
        gender = "f" if is_female else "m"
        prefix = f"b_n_{race}_{gender}"
        seen = {e[0] for e in entries}
        for bp_id, bp in bodypart_map.items():
            data = bp.get("data") or {}
            part = (data.get("part") or "")
            if not bp_id.startswith(prefix):
                continue
            if data.get("bodypart_type") != "Skin":
                continue
            if part.lower() in ("head", "hair"):
                continue  # déjà gérés via npc.head / npc.hair
            if ".1st" in bp_id:
                continue  # vue première personne
            mesh = bp.get("mesh")
            if mesh and mesh not in seen:
                entries.append((mesh, part, True))
                seen.add(mesh)

    # Déduplication :
    # - (chemin, slot) : un même fichier de pièce peut être référencé par
    #   PLUSIEURS slots explicites côté gauche/droite (pantalon -> Left et
    #   Right Upper Leg) : chaque slot doit produire son attache, miroir
    #   inclus. Seuls les doublons EXACTS sont fusionnés.
    # - Les fichiers contenant un squelette (base_anim, Skins.NIF référencé
    #   par toutes ses parties) ne doivent être traités qu'UNE fois : c'est
    #   géré au moment de l'import (garde sur les chemins déjà mergés).
    dedup = {}
    for e in entries:
        path = e[0]
        hint = e[1] if len(e) > 1 else None
        key = (path.replace("\\", "/").lower(),
               re.sub(r"[\s_]+", "", (hint or "")).lower())
        dedup.setdefault(key, e)
    return list(dedup.values())


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


def ensure_kf_sibling(nif_path):
    """TEMP: les créatures spéciales stockent leurs anims dans x<name>.kf ;
    l'importeur exige <name>.kf -> symlink."""
    kf_path = os.path.splitext(nif_path)[0] + ".kf"
    if os.path.exists(kf_path):
        return
    xkf = os.path.join(os.path.dirname(kf_path), "x" + os.path.basename(kf_path))
    if os.path.exists(xkf):
        try:
            os.symlink(os.path.basename(xkf), kf_path)
            print(f"[+] Linked KF: {kf_path} -> {os.path.basename(xkf)}")
        except OSError:
            pass


# ==========================================
# MESH IMPORT
# ==========================================

def get_or_import_mesh(mesh_relative_path):
    clean_key = mesh_relative_path.replace("\\", "/").lower()

    if clean_key in imported_mesh_cache:
        return imported_mesh_cache[clean_key], imported_mesh_snapshots[clean_key]

    resolved_nif_path = find_nif_file(mesh_relative_path)

    if resolved_nif_path:
        bpy.ops.object.select_all(action="DESELECT")
        objs_before = set(bpy.data.objects)

        # En mode npcs : attacher les animations du .kf s'il existe
        # (créé au besoin depuis x<name>.kf). Sans .kf, pas d'attache :
        # l'importeur créerait sinon une action vide sur chaque squelette,
        # ce qui casserait la détection du rig animé.
        with_animations = False
        if IMPORT_MODE == "npc":
            ensure_kf_sibling(resolved_nif_path)
            kf_path = os.path.splitext(resolved_nif_path)[0] + ".kf"
            with_animations = os.path.exists(kf_path)

        try:
            bpy.ops.import_scene.mw(
                filepath=resolved_nif_path,
                attach_keyframe_data=with_animations,
            )
            imported_objs = [o for o in bpy.data.objects if o not in objs_before]

            # Les squelettes base_anim embarquent des pièces de corps génériques
            # sans UVs ('Tri ...') que le jeu remplace par les peaux de la race.
            base_name = os.path.basename(resolved_nif_path).lower()
            if IMPORT_MODE == "npc" and base_name.startswith("base_anim"):
                placeholders = [o for o in imported_objs
                                if o.type == "MESH" and o.name.lower().startswith("tri ")]
                for obj in placeholders:
                    bpy.data.objects.remove(obj, do_unlink=True)
                imported_objs = [o for o in imported_objs if o not in placeholders]
                if placeholders:
                    print(f"[-] {len(placeholders)} placeholder(s) 'Tri *' supprimés de {base_name}")

            if imported_objs:
                for obj in imported_objs:
                    # En mode cells, les clés d'animation du NIF ne servent à rien
                    if not with_animations and obj.animation_data:
                        obj.animation_data_clear()

                # Figer immédiatement la hiérarchie native du fichier :
                # parents + matrices locales d'origine, avant que des imports
                # ultérieurs ne puissent contaminer ces valeurs.
                snapshot = {}
                for obj in imported_objs:
                    snapshot[obj] = {
                        'parent': obj.parent,
                        'matrix_local': obj.matrix_local.copy(),
                    }

                for obj in imported_objs:
                    # Délier des collections de la scène pour ne pas encombrer le point (0,0,0)
                    for col in list(obj.users_collection):
                        col.objects.unlink(obj)

                imported_mesh_cache[clean_key] = imported_objs
                imported_mesh_snapshots[clean_key] = snapshot
                return imported_objs, snapshot

        except Exception as e:
            print(f"[-] Import error for '{resolved_nif_path}': {e}")
    else:
        print(f"[-] Mesh file not found on disk: {mesh_relative_path}")

    return None


# ==========================================
# CELL RECONSTRUCTION
# ==========================================
# Attaches façon moteur (cf. NpcAnimation::sPartList d'OpenMW) : chaque pièce
# rigide est ajoutée comme enfant statique du nœud nommé correspondant du
# squelette ('Groin', 'Left Upper Leg'...). Ces nœuds existent dans base_anim
# et l'importeur les parente déjà à l'os animé adéquat.
ATTACH_NODES_SINGLE = {
    "head": "Head",
    "hair": "Head",  # OpenMW : os "Head" pour les cheveux aussi
    "neck": "Neck",
    "groin": "Groin",
    "chest": "Chest",
    "skirt": "Groin",
    "tail": "Tail",
}
ATTACH_NODES_PAIRED = {
    "upperleg": "Upper Leg",
    "knee": "Knee",
    "ankle": "Ankle",
    "foot": "Foot",
    "hand": "Hand",
    "wrist": "Wrist",
    "forearm": "Forearm",
    "upperarm": "Upper Arm",
    # Les épaulettes s'attachent aux clavicules (comme OpenMW)
    "pauldron": "Clavicle",
    "clavicle": "Clavicle",
}


def attach_targets_for_slot(slot_hint):
    """Retourne [(nom_noeud_attache, miroir_x)] pour un indice de slot.

    - 'LeftXxx'/'RightXxx' (entrées biped) -> ce côté uniquement ; miroir si
      gauche (les géométries sont authorées côté droit, cf. SceneUtil::attach).
    - Partie paire sans côté (peaux de race) -> les DEUX côtés à partir du
      même fichier : droite telle quelle + gauche en miroir.
    """
    key = re.sub(r"[\s_]+", "", slot_hint).lower()
    side = ""
    for prefix in ("left", "right"):
        if key.startswith(prefix):
            side = prefix
            key = key[len(prefix):]
            break
    if key in ATTACH_NODES_PAIRED:
        word = ATTACH_NODES_PAIRED[key]
        if side == "left":
            return [(f"Left {word}", True)]
        if side == "right":
            return [(f"Right {word}", False)]
        return [(f"Right {word}", False), (f"Left {word}", True)]
    if not side and key in ATTACH_NODES_SINGLE:
        return [(ATTACH_NODES_SINGLE[key], False)]
    return []


def mirrored_copy(src, cell_collection):
    """Duplique un mesh avec géométrie miroir en X (winding inversé)."""
    dup = src.copy()
    dup.data = src.data.copy()
    dup.name = f"{src.name}.M"
    bm = bmesh.new()
    bm.from_mesh(dup.data)
    bmesh.ops.reverse_faces(bm, faces=bm.faces[:])
    for v in bm.verts:
        v.co.x *= -1
    bm.to_mesh(dup.data)
    bm.free()
    dup.data.validate(verbose=False)
    dup.data.update()
    cell_collection.objects.link(dup)
    return dup


def place_reference(rec_info, ref, origin_offset, cell_collection):
    """Importe et place les meshes d'une référence. Retourne le nombre d'objets créés."""
    ref_id = ref.get("id", "Unknown_Ref")

    # En mode npcs : [(chemin, indice_slot)] ; en mode cells : chemins simples
    mesh_entries = get_ref_mesh_paths(rec_info)
    if not mesh_entries:
        return 0

    translation = ref.get("translation", [0.0, 0.0, 0.0])
    rotation = ref.get("rotation", [0.0, 0.0, 0.0])
    scale = ref.get("scale", 1.0)

    # Position ajustée à l'échelle Blender, centrée sur l'origine fournie
    pos = Vector(translation) * SCALE_FACTOR - origin_offset

    # --- CORRECTION DE LA ROTATION ---
    rx, ry, rz = rotation
    rot_euler = Euler(
        (rx * ROT_SIGN_X, ry * ROT_SIGN_Y, rz * ROT_SIGN_Z),
        EULER_ORDER
    )

    any_imported = False
    created = 0
    canonical = None  # armature animée de référence pour tout le NPC
    attach_nodes = {}  # nœuds d'attache du squelette : 'groin' -> objet Blender
    # Fichiers à squelette déjà traités (base_anim, Skins.NIF...) : leurs
    # entrées suivantes (autres slots pointant le même fichier) sont
    # ignorées, sinon les pièces skinnées seraient dupliquées.
    armature_paths = set()
    # Slots occupés par des pièces (vêtements, armures, parties de race) ->
    # pour masquer les parties de peau couvertes.
    occupied_slots = set()
    skin_part_objs = []

    for entry in mesh_entries:
        # npcs : (chemin, slot[, is_skin]) ; cells : chemin simple
        if isinstance(entry, tuple):
            mesh_path = entry[0]
            slot_hint = entry[1] if len(entry) > 1 else None
            is_skin = bool(entry[2]) if len(entry) > 2 else False
        else:
            mesh_path, slot_hint, is_skin = entry, None, False
        template_objs, snapshot = get_or_import_mesh(mesh_path)

        if not template_objs:
            continue

        norm_path = mesh_path.replace("\\", "/").lower()
        has_armature = any(o.type == 'ARMATURE' for o in template_objs)
        if has_armature and norm_path in armature_paths:
            print(f"[~] Entrée ignorée (squelette déjà traité): "
                  f"{mesh_path} [{slot_hint}]")
            continue

        any_imported = True
        copied_map = {}

        # 1. Duplication des objets de la hiérarchie NIF
        for t_obj in template_objs:
            new_obj = t_obj.copy()

            # Mode cells : les clés d'animation ne servent à rien
            if IMPORT_MODE != "npc" and new_obj.animation_data:
                new_obj.animation_data_clear()

            new_obj.hide_set(False)
            new_obj.hide_viewport = False
            new_obj.hide_render = False

            cell_collection.objects.link(new_obj)
            copied_map[t_obj] = new_obj

        GLOBAL_TEMPLATE_COPY_MAP.update(copied_map)

        if has_armature:
            armature_paths.add(norm_path)
        if is_skin:
            skin_part_objs.extend(o for o in copied_map.values()
                                  if o.type == 'MESH')
        elif slot_hint:
            occupied_slots.add(
                re.sub(r"[\s_]+", "", slot_hint).lower())

        # 2. Restauration des relations parent-enfant
        # On utilise l'instantané PRISTINE du fichier (les matrix_local des
        # templates peuvent avoir été contaminés par des imports ultérieurs).
        roots = []
        for t_obj in template_objs:
            new_obj = copied_map[t_obj]
            snap_par = snapshot[t_obj]['parent']
            if snap_par in copied_map:
                new_obj.parent = copied_map[snap_par]
                new_obj.matrix_local = snapshot[t_obj]['matrix_local'].copy()
            else:
                roots.append((t_obj, new_obj))

        # Retarget des modificateurs ARMATURE : on résout chaque cible via la
        # carte globale (le squelette est partagé entre imports), puis on
        # unifie vers l'armature animée canonique du NPC.
        local_armatures = [copied_map[t] for t in template_objs if t.type == 'ARMATURE']
        if canonical is None:
            canonical = next(
                (a for a in local_armatures
                 if a.animation_data and a.animation_data.action),
                local_armatures[0] if local_armatures else None,
            )
        # Pièces skinnées dont le bind était exprimé dans le repère d'un
        # autre squelette que le canonique : leur matrice monde devra être
        # corrigée du delta entre les deux repères avant suppression.
        skinned_retargeted = []
        for new_obj in copied_map.values():
            if new_obj.type != 'MESH':
                continue
            nl = new_obj.name.lower()
            if 'left hand' in nl or 'right hand' in nl:
                # Les mains sont traitées à part (voir bloc squelettes
                # redondants) : déformation bakée au repos puis attachée
                # au nœud 'Left/Right Hand' comme une pièce rigide — elles
                # ne s'articulent pas dans Morrowind et leur bind diverge
                # par-os du rig canonique (~90° sur les doigts).
                continue
            for mod in new_obj.modifiers:
                if mod.type != 'ARMATURE' or not mod.object:
                    continue
                resolved = GLOBAL_TEMPLATE_COPY_MAP.get(mod.object, mod.object)
                if (resolved.type == 'ARMATURE' and resolved is not canonical
                        and not (resolved.animation_data and resolved.animation_data.action)):
                    mod.object = canonical
                    skinned_retargeted.append(new_obj)
                elif resolved is not None:
                    mod.object = resolved

        # 3. Application de la matrice de transformation de référence
        mat_trans = Matrix.Translation(pos)
        mat_rot = rot_euler.to_matrix().to_4x4()
        mat_scale = Matrix.Diagonal((scale, scale, scale, 1.0))

        # Matrice globale de la référence dans la cellule
        mat_ref = mat_trans @ mat_rot @ mat_scale

        for t_root, new_root in roots:
            # On combine la transformation de la cellule avec la matrice locale
            # originale du NIF (instantané pristine, pas la valeur potentiellement
            # contaminée des templates). Signes d'axes appliqués aussi sur la rotation.
            t_root_local = snapshot[t_root]['matrix_local']
            nif_euler = t_root_local.to_euler(EULER_ORDER)
            nif_euler_signed = Euler(
                (nif_euler.x * ROT_SIGN_X, nif_euler.y * ROT_SIGN_Y, nif_euler.z * ROT_SIGN_Z),
                EULER_ORDER
            )
            nif_mat = nif_euler_signed.to_matrix().to_4x4()
            nif_mat.translation = t_root_local.translation
            nif_mat = nif_mat @ Matrix.Diagonal((*t_root_local.to_scale(), 1.0))
            new_root.matrix_world = mat_ref @ nif_mat

        # Suppression des squelettes redondants sans animation (ex: celui
        # embarqué dans Skins.NIF) au profit du rig canonique. Se fait APRÈS
        # les transforms : ces armatures peuvent être des racines du NIF.
        if canonical:
            for arm in local_armatures:
                if arm is canonical or (arm.animation_data and arm.animation_data.action):
                    continue
                # Le bind des pièces skinnées était exprimé dans le repère du
                # squelette supprimé : on repositionne leur monde avec le
                # delta entre les deux repères (W' = W_canon @ inv(W_suppr) @ W),
                # suffisant car les repos ne diffèrent que d'un transform
                # global pour les os du torse.
                bpy.context.view_layer.update()
                delta = canonical.matrix_world @ arm.matrix_world.inverted()
                # L'action importée peut poser les os : le bake et la
                # lecture des repères doivent se faire au REPOS.
                arm.data.pose_position = 'REST'
                canonical.data.pose_position = 'REST'
                bpy.context.view_layer.update()
                to_fix = set(skinned_retargeted)
                for child in arm.children:
                    to_fix.add(child)
                for obj in to_fix:
                    nl = obj.name.lower()
                    if ('left hand' in nl or 'right hand' in nl) \
                            and any(m.type == 'ARMATURE' for m in obj.modifiers):
                        # Mains : bakons la déformation AU REPOS évaluée par
                        # CE squelette (encore vivant), puis re-skinnons la
                        # pièce à 100 % sur l'os Hand.L/R canonique : le
                        # skinning standard la fait suivre l'animation,
                        # sans dépendre d'un parenting par os.
                        bpy.context.view_layer.update()
                        dg = bpy.context.evaluated_depsgraph_get()
                        ev = obj.evaluated_get(dg)
                        me = ev.to_mesh()
                        for sv, dv in zip(me.vertices, obj.data.vertices):
                            dv.co = sv.co
                        ev.to_mesh_clear()
                        bname = ('Bip01 Hand.L' if 'left' in nl
                                 else 'Bip01 Hand.R')
                        obj.vertex_groups.clear()
                        vg = obj.vertex_groups.new(name=bname)
                        vg.add(list(range(len(obj.data.vertices))),
                               1.0, 'REPLACE')
                        for m in list(obj.modifiers):
                            if m.type == 'ARMATURE':
                                m.object = canonical
                        # La déformation bakée vivait liée au repère de l'os
                        # Hand du squelette supprimé. On mappe ce repère sur
                        # celui de l'os canonique : un simple delta global ne
                        # suffit pas, les os des deux squelettes diffèrent
                        # aussi en ROTATION (~90° sur les membres).
                        sb = arm.data.bones.get(bname)
                        cb = canonical.data.bones.get(bname)
                        if sb and cb:
                            m_s = arm.matrix_world @ sb.matrix_local
                            m_c = canonical.matrix_world @ cb.matrix_local
                            obj.matrix_world = \
                                (m_c @ m_s.inverted()) @ obj.matrix_world
                        else:
                            obj.matrix_world = delta @ obj.matrix_world
                    elif any(m.type == 'ARMATURE' for m in obj.modifiers):
                        obj.matrix_world = delta @ obj.matrix_world
                # Flush : les matrix_world viennent d'être réécrits, sans
                # cela les lectures ci-dessous renvoient des valeurs périmées.
                bpy.context.view_layer.update()
                for child in list(to_fix):
                    world = child.matrix_world.copy()
                    child.parent_bone = ''
                    child.parent_type = 'OBJECT'
                    child.parent = canonical
                    child.matrix_world = world
                bpy.data.objects.remove(arm, do_unlink=True)
                for mapping in (copied_map, GLOBAL_TEMPLATE_COPY_MAP):
                    for k in [k for k, v in mapping.items() if v is arm]:
                        del mapping[k]

        # Enregistrement des nœuds d'attache du squelette ('Groin',
        # 'Left Ankle', ...) : l'importeur crée des empties déjà parentés
        # aux os animés correspondants (parent_type='BONE').
        for new_obj in copied_map.values():
            if new_obj.type == 'EMPTY' and new_obj.parent_bone:
                base = re.sub(r"\.\d+$", "", new_obj.name).lower()
                attach_nodes[base] = new_obj

        # Les mains sont désormais skinnées à 100 % sur les os 'Bip01
        # Hand.L/R' canoniques (voir plus haut) : elles suivent l'animation
        # par skinning standard, sans nœud d'attache.

        # Les pièces rigides (têtes, cheveux, vêtements segmentés, parties de
        # race sans skin : le moteur les ajoute comme enfants statiques d'un
        # nœud nommé du squelette à l'exécution) sont parentées à ces nœuds.
        # Leurs sommets sont modelés dans l'espace local du nœud ; pour les
        # attaches gauches, la géométrie (authorée à droite) est mise en
        # miroir X, comme SceneUtil::attach d'OpenMW (scale -1).
        if canonical and slot_hint:
            # Les mains bakées sont déjà attachées à leurs nœuds Hand :
            # on les exclut ici (Skins.NIF peut arriver avec un slot
            # vêtement, ex 'Chest', qui sinon les capturerait).
            rigid = [o for o in copied_map.values()
                     if o.type == 'MESH'
                     and not any(m.type == 'ARMATURE' for m in o.modifiers)
                     and 'left hand' not in o.name.lower()
                     and 'right hand' not in o.name.lower()]
            targets = attach_targets_for_slot(slot_hint)

            # Chaîne pristine pour chaque objet, en NEUTRALISANT le transform
            # de la racine CONTENEUR du fichier (empty sans géométrie) : le
            # moteur ignore ce transform à l'attache. Les racines non identité
            # ('A_Orcish_Boots_F' (-0.06,-0.03,+0.03), cuirass 'Bip01'
            # (0,+0.01,+0.76)...) décalent sinon la pièce de quelques cm.
            # Les pièces qui SONT leur propre racine (parties de race : nuque,
            # groin...) gardent leur matrice authorée : c'est LEUR transform.
            def pristine_chain(t_obj):
                entries = []
                cur = t_obj
                while cur is not None:
                    info = snapshot.get(cur)
                    if info is None:
                        break
                    entries.append((cur, info['matrix_local']))
                    cur = info['parent']
                m = Matrix.Identity(4)
                for i, (obj, ml) in enumerate(entries):
                    if i == len(entries) - 1 and obj.type != 'MESH' \
                            and t_obj is not obj:
                        continue  # racine conteneur : ignorée
                    m = ml @ m
                return m

            # Uniquement pour les pièces qui seront attachées (rigides) ;
            # les entrées des armatures supprimées n'existent plus dans
            # copied_map à ce stade (purge lors du retarget).
            pristine_locals = {}
            for t, c in copied_map.items():
                if c.type != 'MESH':
                    continue
                pristine_locals[c] = pristine_chain(t)

            if rigid and targets:
                # Les originaux ne servent que pour une cible NON miroir ;
                # s'il n'y en a pas (ex: entrée biped 'LeftXxx' seule), ils
                # sont remplacés par les duplicatas inversés et supprimés,
                # sinon ils resteraient orphelins à l'origine.
                consomme = any(not mirror for _, mirror in targets)

                # Construction du plan : les duplicatas miroirs sont créés
                # AVANT le premier reparentement (chaîne parentale intacte).
                plan = []
                for node_name, mirror in targets:
                    if not mirror:
                        plan.append((node_name, list(rigid)))
                        continue
                    dups = []
                    for src in rigid:
                        dup = src.copy()
                        dup.data = src.data.copy()
                        dup.name = f"{src.name}.M"
                        cell_collection.objects.link(dup)
                        bm = bmesh.new()
                        bm.from_mesh(dup.data)
                        bmesh.ops.reverse_faces(bm, faces=bm.faces[:])
                        for v in bm.verts:
                            v.co.x *= -1
                        bm.to_mesh(dup.data)
                        bm.free()
                        dup.data.validate(verbose=False)
                        dup.data.update()
                        # La géométrie est mise en miroir dans le mesh même ;
                        # pour reproduire exactement le moteur (qui insère le
                        # scale -1 ENTRE le nœud et la sous-arborescence :
                        # N @ Sx @ C @ v), la chaîne du duplicata doit être
                        # conjuguée par Sx : C' = Sx @ C @ Sx.
                        flip = Matrix.Diagonal((-1.0, 1.0, 1.0, 1.0))
                        pristine_locals[dup] = (flip @ pristine_locals[src]
                                                @ flip)
                        dups.append(dup)
                    plan.append((node_name, dups))
                    created += len(dups)

                if not consomme:
                    for ob in rigid:
                        bpy.data.objects.remove(ob, do_unlink=True)
                        for mapping in (copied_map, GLOBAL_TEMPLATE_COPY_MAP):
                            for k in [k for k, v in mapping.items() if v is ob]:
                                del mapping[k]

                for node_name, objs in plan:
                    node = attach_nodes.get(node_name.lower())
                    if node is None:
                        print(f"[-] Nœud d'attache '{node_name}' introuvable "
                              f"(slot {slot_hint})")
                        continue
                    # Sans ce flush, matrix_local renvoie des valeurs périmées
                    # (pré-restauration) et l'attache hérite d'orientations fausses.
                    bpy.context.view_layer.update()
                    for ob in objs:
                        local = pristine_locals[ob]
                        ob.parent = node
                        ob.parent_type = 'OBJECT'
                        ob.parent_bone = ''
                        ob.matrix_parent_inverse = Matrix.Identity(4)
                        ob.matrix_basis = local
                    print(f"[+] Attache {len(objs)} pièce(s) -> "
                          f"'{node_name}' ({slot_hint})")

            # Nettoyage des empties devenus inutiles (racines/coquilles des
            # fichiers de pièces désormais accrochées aux os)
            for obj in [o for o in copied_map.values() if o.type == 'EMPTY']:
                if not obj.children:
                    bpy.data.objects.remove(obj, do_unlink=True)
                    for mapping in (copied_map, GLOBAL_TEMPLATE_COPY_MAP):
                        for k in [k for k, v in mapping.items() if v is obj]:
                            del mapping[k]

        created += len(copied_map)

    # Remplacement des parties de peau : un vêtement/armure occupant un slot
    # masque la partie de peau correspondante (comme le moteur, qui n'ajoute
    # que la pièce du vêtement pour les slots couverts).
    SLOT_COVERS = {
        "chest": ("chest",),
        "groin": ("groin",),
        "skirt": ("groin",),
        # Un casque occupe le slot 'Head' et masque la CHEVELURE
        # (la tête reste visible) ; les noms de pièces cheveux
        # contiennent toujours 'hair'.
        "head": ("hair",),
        "upperleg": ("upper leg",),
        "knee": ("knee",),
        "ankle": ("ankle",),
        "foot": ("foot",),
        "hand": ("hand",),
        "wrist": ("wrist",),
        "forearm": ("forearm",),
        "upperarm": ("upper arm",),
        "pauldron": ("upper arm",),
        "tail": ("tail",),
    }
    covered = set()
    for slot in occupied_slots:
        key = slot
        for prefix in ("left", "right"):
            if key.startswith(prefix):
                key = key[len(prefix):]
                break
        for frag in SLOT_COVERS.get(key, ()):
            covered.add(frag)
    if skin_part_objs and covered:
        # Stems des pièces de peau ('B_N_Dark Elf_M_Groin', 'Tri Chest'...)
        # : permet de cibler aussi les duplicatas miroirs '.M' et les
        # variantes '.001' créés pendant l'assemblage.
        def _stem(nm):
            nm = re.sub(r"\.M$", "", nm, flags=re.IGNORECASE)
            nm = re.sub(r"\.\d+$", "", nm)
            return nm.lower()
        stems = {_stem(o.name) for o in skin_part_objs}
        removed = 0
        for ob in list(cell_collection.objects):
            if ob.type != 'MESH':
                continue
            nm = ob.name.lower()
            if _stem(ob.name) in stems and \
                    any(frag in nm for frag in covered):
                bpy.data.objects.remove(ob, do_unlink=True)
                removed += 1
        if removed:
            print(f"[+] Peau remplacée par vêtement : {removed} partie(s) "
                  f"supprimée(s) pour {sorted(covered)}")

    if not any_imported:
        # Placeholder if the NIF file is not found
        obj = bpy.data.objects.new(f"{ref_id}_placeholder", None)
        cell_collection.objects.link(obj)
        obj.location = pos
        obj.rotation_euler = rot_euler
        obj.scale = (scale, scale, scale)
        created += 1

    return created


def find_npc_references(record_map, seen_cells):
    """Cherche les références de NPCs/créatures dont le nom ou l'id contient TARGET_NPC_NAME.
    Retourne une liste de tuples (ref, rec_info, cell_label)."""
    query = TARGET_NPC_NAME.lower()
    hits = []
    for cell in seen_cells.values():
        cell_label = cell.get("name") or f"Cell_{cell.get('data', {}).get('grid')}"
        for ref in cell.get("references", []):
            if ref.get("deleted", False):
                continue
            rec_info = record_map.get((ref.get("id") or "").lower())
            if not rec_info or not should_import(rec_info.get("type")):
                continue
            name = (rec_info.get("name") or "").lower()
            rid = (rec_info.get("id") or "").lower()
            if query in name or query in rid:
                hits.append((ref, rec_info, cell_label))
    return hits


def rebuild_npc_by_name(record_map, seen_cells):
    """Mode npcs + --npc : importe toutes les occurrences du NPC cherché,
    centrées sur la première trouvaille."""
    print()
    print("=" * 60)
    print(f"[+] Searching NPC/Creature: '{TARGET_NPC_NAME}'")
    print("=" * 60)

    hits = find_npc_references(record_map, seen_cells)
    if not hits:
        print(f"[-] No NPC/Creature found matching '{TARGET_NPC_NAME}'")
        return 0, 0

    # Plusieurs occurrences (cells différentes) : laisser l'utilisateur
    # choisir laquelle importer. Entrée vide = la première.
    if len(hits) > 1:
        print()
        print(f"[?] {len(hits)} occurrences trouvées :")
        for i, (ref, rec_info, cell_label) in enumerate(hits, 1):
            label = rec_info.get("name") or rec_info.get("id")
            tr = [round(v, 1) for v in ref.get("translation", [0, 0, 0])]
            print(f"    {i}) '{label}' in cell: {cell_label}  pos={tr}")
        try:
            choice = input(
                "Numéro de l'occurrence à importer "
                "(entrée vide = 1) : ").strip()
        except EOFError:
            choice = "1"
        try:
            idx = int(choice) if choice else 1
            if 1 <= idx <= len(hits):
                hits = [hits[idx - 1]]
            else:
                print(f"[!] Hors range 1-{len(hits)}, "
                      f"première occurrence retenue.")
                hits = hits[:1]
        except ValueError:
            print(f"[!] Entrée invalide ('{choice}'), "
                  f"première occurrence retenue.")
            hits = hits[:1]

    collection_name = f"MW_{TARGET_NPC_NAME}"
    if collection_name in bpy.data.collections:
        npc_collection = bpy.data.collections[collection_name]
    else:
        npc_collection = bpy.data.collections.new(collection_name)
        bpy.context.scene.collection.children.link(npc_collection)

    origin_offset = Vector(hits[0][0].get("translation", [0.0, 0.0, 0.0])) * SCALE_FACTOR
    object_count = 0

    for ref, rec_info, cell_label in hits:
        label = rec_info.get("name") or rec_info.get("id")
        print(f"[+] Found '{label}' in cell: {cell_label}")
        object_count += place_reference(rec_info, ref, origin_offset, npc_collection)

    bpy.context.view_layer.update()

    print()
    print("=" * 60)
    print("[+] RECONSTRUCTION COMPLETE")
    print("=" * 60)
    print(f"[+] Hits:    {len(hits)}")
    print(f"[+] Objects: {object_count}")
    print(f"[+] Cached meshes: {len(imported_mesh_cache)}")
    print("=" * 60)

    return len(hits), object_count


def rebuild_cells_in_blender():
    data = load_tes3_json(JSON_FILE_PATH)
    record_map = build_record_map(data)
    build_npc_lookup_maps(data)

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

    # Mode npcs + --npc : recherche par nom dans toutes les cellules
    if IMPORT_MODE == "npc" and TARGET_NPC_NAME:
        rebuild_npc_by_name(record_map, seen_cells)
        return

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
            if not should_import(rec_info.get("type")):
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

            rec_info = record_map.get((ref.get("id") or "").lower(), {})

            if not should_import(rec_info.get("type")):
                continue

            object_count += place_reference(rec_info, ref, origin_offset, cell_collection)

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

# Vider la scène par défaut (cube, caméra, lumière)
for obj in list(bpy.data.objects):
    bpy.data.objects.remove(obj)

rebuild_cells_in_blender()

print()
print("[+] Fixing missing textures...")
fix_missing_textures()

print()
print("[+] Saving Blender file...")

# Le mode REST n'était nécessaire que pour les calculs de bake : le
# restaurer, sinon l'armature reste évaluée au repos (T-Pose figée).
for _arm in bpy.data.armatures:
    if _arm.pose_position != 'POSE':
        _arm.pose_position = 'POSE'

bpy.ops.wm.save_as_mainfile(filepath="morrowind.blend")

print()
print("=" * 60)
print("[+] DONE")
print("=" * 60)

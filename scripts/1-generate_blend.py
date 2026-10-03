import sys
import json
import os
import re
import addon_utils
import bpy
import math
import bmesh
from mathutils import Euler, Vector, Matrix
import base64
import shutil
import struct
import subprocess
import numpy as np
import time

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
# Plusieurs dossiers possibles (séparés par os.pathsep), par priorité décroissante :
# fichiers loose des mods (openmw.cfg) d'abord, cache des BSA en dernier.
MESHES_DIRS = [p for p in _meshes.split(os.pathsep) if p]

_textures = get_arg("textures", r"../data/textures")
TEXTURES_DIRS = [p for p in _textures.split(os.pathsep) if p]

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

# Exterior cells : --grid "x,y" ou --grid "x1,y1:x2,y2" (rectangle inclusif).
# Prioritaire sur --cell. Ex. : Seyda Neen = "-2,-9".
CELL_SIZE = 8192.0  # côté d'une exterior cell, en unités Morrowind


def parse_grid_arg(value):
    if not value:
        return None
    nums = [int(n) for n in re.findall(r"-?\d+", value)]
    if len(nums) == 2:
        nums = nums + nums
    if len(nums) != 4:
        print(f"[ERROR] --grid invalide : '{value}' (attendu 'x,y' ou 'x1,y1:x2,y2')")
        sys.exit(1)
    x1, y1, x2, y2 = nums
    return (min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2))


TARGET_GRID = parse_grid_arg(get_arg("grid", None))

# Mode npcs : recherche d'un NPC/créature par nom ou id dans toutes les cellules
TARGET_NPC_NAME = get_arg("npc", None)

# Mode d'import : "cell" (décor statique) ou "npc" (créatures/NPCs animés)
_mode_arg = get_arg("mode", "cell")
FULL_MODE = (_mode_arg == "full")
IMPORT_MODE = "cell" if FULL_MODE else _mode_arg
if FULL_MODE and TARGET_GRID is None:
    print("[ERROR] --mode full necessite --grid")
    sys.exit(1)

# Animations a conserver pour les NPCs/creatures : liste de prefixes (insensible a la
# casse, separes par des virgules) ou "all". Par defaut : idle uniquement en mode full.
# Les autres cles d'animation sont supprimees des actions des l'import (fichiers plus
# legers) ; 7-export_glb.py recoit le meme --anims pour filtrer l'export.
_anims_arg = get_arg("anims", "idle" if FULL_MODE else "all")
ANIM_KEEP_PREFIXES = (None if _anims_arg.strip().lower() == "all"
                      else [a.strip().lower() for a in _anims_arg.split(",") if a.strip()])

# Mode full : dossier de sortie (un .blend par cell)
FULL_OUTDIR = os.path.abspath(get_arg("outdir", "export"))

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
race_map = {}

# Cache of imported meshes
imported_mesh_cache = {}
# Meshes impossibles à importer (introuvables, ou l'import a levé une erreur) :
# clé -> raison. Évite de retenter (et de re-logguer) à chaque référence.
failed_meshes = {}
# Instantanés des matrices locales PRISTINES de chaque fichier importé.
# Indispensable : l'importeur peut réécrire les matrix_local des imports
# suivants (contamination), on fige donc les valeurs juste après l'import.
imported_mesh_snapshots = {}

# Carte globale template -> copie : l'importeur partage le squelette Bip01
# entre les NIFs (les meshes d'un NIF pointent vers l'armature d'un autre),
# il faut donc résoudre les cibles au-delà du seul import courant.
GLOBAL_TEMPLATE_COPY_MAP = {}

# True quand le dernier NPC assemblé n'a trouvé AUCUNE peau de race
# (préfixe b_n_<race>_<genre> absent des bodyparts) : on garde alors les
# placeholders 'Tri *' du squelette plutôt qu'un NPC sans torse.
NPC_RACE_SKINS_MISSING = False


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


def cell_is_interior(cell):
    flags = (cell.get("data") or {}).get("flags")
    if isinstance(flags, int):
        return bool(flags & 1)
    return "INTERIOR" in str(flags).upper()


def cell_grid(cell):
    grid = (cell.get("data") or {}).get("grid") or [0, 0]
    return int(grid[0]), int(grid[1])


def cell_key(cell):
    """Interieur : son nom. Exterieur : sa grille (le nom d'une exterior n'est pas unique)."""
    if cell_is_interior(cell):
        return cell.get("name") or f"Cell_{cell_grid(cell)}"
    gx, gy = cell_grid(cell)
    return f"Ext_{gx}_{gy}"


def cell_display_label(cell):
    if cell_is_interior(cell):
        return cell.get("name") or f"Cell_{cell_grid(cell)}"
    gx, gy = cell_grid(cell)
    return f"Ext_{gx}_{gy}"


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
        elif rtype == "Race":
            race_map[rid] = record


def _race_is_beast(race):
    """True si la race est une race-bête (squelette base_animkna).

    Se fie au flag Beast du record Race — indispensable pour les races de
    mods qui ne s'appellent pas khajiit/argonian. Le format exact des flags
    dépend du convertisseur : on accepte chaîne, liste, dict ou entier
    (bit 0x02 = Beast côté OpenMW). Sans record Race connu, retombe sur la
    liste historique.
    """
    rid = (race or "").strip().lower()
    rec = race_map.get(rid)
    if rec is None:
        return rid in ("khajiit", "argonian")
    # tes3conv met les flags du record RACE dans data.flags
    # ("PLAYABLE | BEAST_RACE"), le champ racine restant vide.
    candidates = (
        rec.get("flags"),
        rec.get("race_flags"),
        (rec.get("data") or {}).get("flags"),
    )
    for flags in candidates:
        if not flags:
            continue
        if isinstance(flags, str):
            if "beast" in flags.lower():
                return True
            try:
                if int(flags, 0) & 0x02:
                    return True
            except ValueError:
                pass
        elif isinstance(flags, (list, tuple)):
            if any(isinstance(f, str) and "beast" in f.lower()
                   for f in flags):
                return True
        elif isinstance(flags, dict):
            if any("beast" in str(k).lower() and v for k, v in flags.items()):
                return True
        elif isinstance(flags, (int, float)):
            if int(flags) & 0x02:
                return True
    return False


def get_default_skeleton(race, is_female):
    """Squelette d'animation par défaut selon la race (comme le jeu quand mesh est vide)."""
    if _race_is_beast(race):
        return "base_animkna.nif"
    return "base_anim_female.nif" if is_female else "base_anim.nif"


def _is_robe(item):
    d = item.get("data") or {}
    ct = str(d.get("clothing_type") or item.get("clothing_type") or "")
    return "robe" in ct.lower() or "robe" in (item.get("id") or "").lower()


def _layer_zone(slot_type):
    """Slot de biped -> zone du corps (gauche/droite fusionnés, pauldron = haut du bras)."""
    z = re.sub(r"[\s_]+", "", slot_type).lower()
    for prefix in ("left", "right"):
        if z.startswith(prefix):
            z = z[len(prefix):]
            break
    return {"pauldron": "upperarm"}.get(z, z)


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

    # Vêtements / armures portés. Superposition par zone du corps :
    # robe > armure > vêtement (un vêtement sous une armure est masqué ; une robe
    # recouvre l'armure). Les pièces sont filtrées individuellement : une chemise
    # sous un plastron perd son torse mais garde ses manches.
    layers = []  # (zone, rang, entree, id_objet)
    for _count, item_id in rec.get("inventory", []):
        item = item_map.get(item_id.lower())
        if not item:
            continue  # armes & co : jamais importées (item_map = Clothing/Armor)
        if item.get("type") == "Armor":
            rank = 2
        elif _is_robe(item):
            rank = 3
        else:
            rank = 1
        for biped in item.get("biped_objects", []):
            slot_type = (biped.get("biped_object_type") or "")
            if re.sub(r"[\s_]+", "", slot_type).lower() in ("shield", "weapon"):
                continue  # boucliers/armes tenues : pas de pièce de corps
            bp_id = biped.get("male_bodypart")
            if is_female and biped.get("female_bodypart"):
                bp_id = biped.get("female_bodypart")
            bp = bodypart_map.get((bp_id or "").lower())
            if bp and bp.get("mesh"):
                layers.append((_layer_zone(slot_type), rank,
                               (bp["mesh"], slot_type), item_id))

    top = {}
    for zone, rank, _e, _i in layers:
        top[zone] = max(top.get(zone, 0), rank)
    for zone, rank, entry, item_id in layers:
        if rank < top[zone]:
            print(f"[-] '{item_id}' [{entry[1]}] masqué (zone '{zone}' couverte "
                  f"par {'une robe' if top[zone] == 3 else 'une armure'})")
            continue
        entries.append(entry)

    # Peaux de la race (corps nu, mains, pieds...) : remplace les placeholders
    # génériques 'Tri *' du squelette base_anim.
    # Résolution EXACTE du moteur (NpcAnimation::getBodyParts) : chaque
    # Bodypart porte un champ 'race' comparé à la race du NPC, et un flag
    # 'FEMALE' pour le genre — AUCUNE convention de nom d'id n'est supposée
    # (les races de mods type Tamriel Data utilisent des ids arbitraires).
    # Fallback mâle -> femelle par partie manquante, comme OpenMW.
    global NPC_RACE_SKINS_MISSING
    NPC_RACE_SKINS_MISSING = False
    if rec.get("type") == "Npc":
        race = (rec.get("race") or "").strip().lower()
        seen = {e[0] for e in entries}

        def _race_skins(want_female):
            out = []
            for bp_id, bp in bodypart_map.items():
                data = bp.get("data") or {}
                part = (data.get("part") or "")
                if data.get("bodypart_type") != "Skin":
                    continue
                if (bp.get("race") or "").strip().lower() != race:
                    continue
                if ("FEMALE" in (data.get("flags") or "")) != want_female:
                    continue
                if part.lower() in ("head", "hair"):
                    continue  # déjà gérés via npc.head / npc.hair
                if ".1st" in bp_id:
                    continue  # vue première personne
                mesh = bp.get("mesh")
                if mesh and mesh not in seen:
                    out.append((mesh, part))
            return out

        found_skins = 0
        added_parts = set()
        for mesh, part in _race_skins(is_female):
            entries.append((mesh, part, True))
            seen.add(mesh)
            added_parts.add(part.lower())
            found_skins += 1
        if is_female:
            # Parties sans variante féminine : fallback sur le mâle
            for mesh, part in _race_skins(False):
                if part.lower() in added_parts:
                    continue
                entries.append((mesh, part, True))
                seen.add(mesh)
                added_parts.add(part.lower())
                found_skins += 1

        # mesh peut contenir un VRAI modèle custom OU un squelette nu
        # ('base_animKnA.nif') qui embarque lui-même des placeholders :
        # dans le second cas l'absence de peaux rendrait le NPC invisible.
        rec_mesh = (rec.get("mesh") or "")
        uses_placeholder_skeleton = (
            not rec_mesh
            or os.path.basename(rec_mesh).lower().startswith("base_anim")
        )
        if found_skins == 0 and uses_placeholder_skeleton:
            NPC_RACE_SKINS_MISSING = True
            print(f"[!] Aucune peau de race pour "
                  f"'{rec.get('name') or rec.get('id')}' (race '{race}') : "
                  "placeholders génériques conservés")

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


# Squelette + animations des NPCs/creatures : doivent venir d'UNE SEULE source coherente.
# Un base_anim.nif/.kf de mod (Animated Morrowind...) a un repos different du vanilla
# -> corps etires / decales. Par defaut les mods gardent la priorite ;
# --vanilla-skeleton force la version du cache BSA (dernier dossier de MESHES_DIRS).
MOD_SKELETON = "--vanilla-skeleton" not in sys.argv
# --root-signs : ancien comportement (signes d'axes appliques a la racine de chaque NIF)
ROOT_SIGNS = "--root-signs" in sys.argv
# --debug-mesh <fragment> (ou variable d'env MW_DEBUG_MESH, utile avec run.sh) : affiche la hierarchie/rotations des NIF dont le chemin contient <fragment>
DEBUG_MESH = (get_arg("debug-mesh", "") or os.environ.get("MW_DEBUG_MESH", "")).lower()
_SKELETON_RE = re.compile(r"(^|/)(x?base_anim[^/]*|skins)\.(nif|kf)$", re.IGNORECASE)


def find_nif_file(mesh_relative_path, exclude=()):
    """Premier fichier trouve par priorite de dossier, en ignorant ceux de `exclude`
    (deja essayes et en echec) -> permet de retomber sur la version vanilla d'un mod."""
    clean_path = mesh_relative_path.replace("\\", "/")
    dirs = MESHES_DIRS
    if (IMPORT_MODE == "npc" and not MOD_SKELETON and len(MESHES_DIRS) > 1
            and _SKELETON_RE.search(clean_path)):
        dirs = [MESHES_DIRS[-1]] + MESHES_DIRS[:-1]   # cache BSA vanilla d'abord
    for base_dir in dirs:
        full_path = os.path.join(base_dir, clean_path)
        resolved_path = resolve_case_insensitive(full_path)
        if resolved_path and os.path.isfile(resolved_path) and resolved_path not in exclude:
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

def _clip_name(seq, phase, suffix):
    base = phase[: -len(suffix)].strip()
    return f"{seq}_{base}" if base else seq


def action_clips(action):
    """{nom_de_clip: [start, stop]} depuis les pose markers 'Seq: ... Start|Stop'.

    Meme decoupage que split_marker_actions() de 7-export_glb.py : les plages
    gardees ici sont exactement celles que l'export reconnaitrait."""
    clips = {}
    for m in action.pose_markers:
        if ":" not in m.name:
            continue
        seq, phase = (p.strip() for p in m.name.split(":", 1))
        if phase.endswith("Start"):
            name = _clip_name(seq, phase, "Start")
            entry = clips.setdefault(name, [m.frame, None])
            entry[0] = m.frame if entry[0] is None else min(entry[0], m.frame)
        elif phase.endswith("Stop"):
            name = _clip_name(seq, phase, "Stop")
            entry = clips.setdefault(name, [None, m.frame])
            entry[1] = m.frame if entry[1] is None else min(entry[1], m.frame)
    return clips


def prune_action_to_prefixes(action, prefixes):
    """Ne garde dans `action` que les clips dont le nom commence par un des prefixes.

    Supprime les keyframes hors des plages gardees et les pose markers des autres
    clips. Ne fait rien (et le signale) si aucun clip ne correspond, pour ne jamais
    detruire une animation par erreur. Retourne (gardes, supprimes) en keyframes."""
    if not action.pose_markers:
        return None
    ranges = []
    for name, (start, stop) in action_clips(action).items():
        if start is None or stop is None or stop <= start:
            continue
        if any(name.lower().startswith(p) for p in prefixes):
            ranges.append((start, stop))
    if not ranges:
        print(f"[~] Action '{action.name}': aucun clip {prefixes} trouve, animations laissees telles quelles")
        return None

    kept = removed = 0
    for layer in action.layers:
        for strip in layer.strips:
            for cbag in strip.channelbags:
                empty = []
                for fc in cbag.fcurves:
                    pts = fc.keyframe_points
                    for i in range(len(pts) - 1, -1, -1):
                        f = pts[i].co.x
                        if any(a <= f <= b for a, b in ranges):
                            kept += 1
                        else:
                            pts.remove(pts[i], fast=True)
                            removed += 1
                    if len(pts) == 0:
                        empty.append(fc)
                    else:
                        fc.update()
                for fc in empty:
                    cbag.fcurves.remove(fc)

    # Marqueurs des clips ecartes
    for m in list(action.pose_markers):
        if ":" not in m.name:
            action.pose_markers.remove(m)
            continue
        seq, phase = (p.strip() for p in m.name.split(":", 1))
        suffix = "Start" if phase.endswith("Start") else ("Stop" if phase.endswith("Stop") else None)
        if suffix is None or not any(_clip_name(seq, phase, suffix).lower().startswith(p) for p in prefixes):
            action.pose_markers.remove(m)
    return kept, removed


_PRUNED_ACTIONS = set()


def prune_imported_animations(objs):
    """Applique ANIM_KEEP_PREFIXES aux actions des objets qu'on vient d'importer."""
    if not ANIM_KEEP_PREFIXES:
        return
    for obj in objs:
        ad = obj.animation_data
        act = ad.action if ad else None
        if act is None or act.name in _PRUNED_ACTIONS:
            continue
        _PRUNED_ACTIONS.add(act.name)
        try:
            res = prune_action_to_prefixes(act, ANIM_KEEP_PREFIXES)
        except Exception as e:
            print(f"[-] Prune animations '{act.name}': {type(e).__name__}: {e}")
            continue
        if res:
            print(f"[+] Animations {ANIM_KEEP_PREFIXES}: {res[0]} keyframes gardees, {res[1]} supprimees ({obj.name})")


def get_or_import_mesh(mesh_relative_path, _tried=()):
    clean_key = mesh_relative_path.replace("\\", "/").lower()

    if clean_key in imported_mesh_cache:
        return imported_mesh_cache[clean_key], imported_mesh_snapshots[clean_key]
    if clean_key in failed_meshes:
        return None

    resolved_nif_path = find_nif_file(mesh_relative_path, _tried)

    if resolved_nif_path:
        if IMPORT_MODE == "npc" and _SKELETON_RE.search(clean_key):
            print(f"[~] Squelette NPC: {resolved_nif_path}")
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
            # Si aucune peau de race n'a été trouvée pour ce NPC, on les garde
            # (NPC visible en mannequin plutôt qu'invisible).
            base_name = os.path.basename(resolved_nif_path).lower()
            if IMPORT_MODE == "npc" and base_name.startswith("base_anim") \
                    and not NPC_RACE_SKINS_MISSING:
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
                if with_animations:
                    prune_imported_animations(imported_objs)

                # Figer immédiatement la hiérarchie native du fichier :
                # parents + matrices locales d'origine, avant que des imports
                # ultérieurs ne puissent contaminer ces valeurs.
                bpy.context.view_layer.update()
                snapshot = {}
                for obj in imported_objs:
                    snapshot[obj] = {
                        'parent': obj.parent,
                        'matrix_local': obj.matrix_local.copy(),
                        # Monde tel que l'importeur l'a produit (référence pour les
                        # objets parentés à un os, dont matrix_local ignore l'os).
                        'matrix_world': obj.matrix_world.copy(),
                    }

                for obj in imported_objs:
                    # Délier des collections de la scène pour ne pas encombrer le point (0,0,0)
                    for col in list(obj.users_collection):
                        col.objects.unlink(obj)

                imported_mesh_cache[clean_key] = imported_objs
                imported_mesh_snapshots[clean_key] = snapshot
                return imported_objs, snapshot

            reason = "import returned no object"

        except Exception as e:
            # Les messages de l'importeur embarquent toute la traceback : on ne
            # garde que la dernière ligne utile.
            lines = [ln.strip() for ln in str(e).splitlines() if ln.strip()]
            reason = lines[-1] if lines else type(e).__name__
            print(f"[-] Import error for '{resolved_nif_path}': {reason}")
            # Supprimer les objets créés à moitié avant l'erreur
            for obj in [o for o in bpy.data.objects if o not in objs_before]:
                bpy.data.objects.remove(obj, do_unlink=True)

        # Echec : essayer le meme mesh dans un dossier de priorité inférieure
        # (ex. version vanilla d'un mesh de mod qui ne s'importe pas).
        tried = _tried + (resolved_nif_path,)
        alt = find_nif_file(mesh_relative_path, tried)
        if alt:
            print(f"[~] Repli sur '{alt}'")
            return get_or_import_mesh(mesh_relative_path, tried)
        failed_meshes[clean_key] = f"{reason} ({resolved_nif_path})"
    else:
        print(f"[-] Mesh file not found on disk: {mesh_relative_path}")
        failed_meshes[clean_key] = "file not found"

    return None


def report_failed_meshes():
    if not failed_meshes:
        return
    print()
    print("=" * 60)
    print(f"[!] {len(failed_meshes)} mesh(es) skipped (references ignored):")
    for key, reason in sorted(failed_meshes.items()):
        print(f"    - {key}: {reason}")
    print("=" * 60)


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


def _rest_remap(obj, src_arm, dst_arm):
    """Re-bind au repos d'une piece skinnee : chaque sommet passe de la pose de repos
    des os de `src_arm` a celle des MEMES os de `dst_arm` (skinning lineaire par poids).

    Un simple delta global ne suffit pas quand les squelettes different en proportions
    (body replacer, squelette de mod) : le corps serait etire. Retourne False si aucun
    os commun (l'appelant retombe sur le delta global)."""
    Ms, Md = src_arm.matrix_world, dst_arm.matrix_world
    T = {}
    for vg in obj.vertex_groups:
        sb = src_arm.data.bones.get(vg.name)
        db = dst_arm.data.bones.get(vg.name)
        if sb and db:
            T[vg.index] = (Md @ db.matrix_local) @ (Ms @ sb.matrix_local).inverted()
    if not T:
        return False
    # Le mesh est partage avec le template (Object.copy() ne duplique pas la donnee) :
    # on le rend unique avant de deplacer les sommets.
    if obj.data.users > 1:
        obj.data = obj.data.copy()
    M = obj.matrix_world.copy()
    Mi = M.inverted()
    worst = 0.0
    for v in obj.data.vertices:
        w = M @ v.co
        acc, tot = Vector((0.0, 0.0, 0.0)), 0.0
        for g in v.groups:
            t = T.get(g.group)
            if t is not None and g.weight > 0.0:
                acc += (t @ w) * g.weight
                tot += g.weight
        if tot > 1e-6:
            new = acc / tot
            worst = max(worst, (new - w).length)
            v.co = Mi @ new
    obj.data.update()
    print(f"[~] Rebind au repos '{obj.name}' {src_arm.name}->{dst_arm.name}: "
          f"{len(T)} os, deplacement max {worst:.3f} m")
    return True


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
        imported = get_or_import_mesh(mesh_path)
        if not imported:
            continue  # mesh introuvable ou import en erreur : déjà signalé
        template_objs, snapshot = imported

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
            if not ROOT_SIGNS:
                # Tous modes : la racine du NIF est deja dans le repere Blender
                # (l'importeur l'a convertie). Inverser les signes de son euler la
                # retourne (ex. silt strider nez dans le sol) ; seule la reference
                # (rotation de la cell) utilise la convention de signes.
                if t_root_local.to_3x3().to_euler().to_quaternion().angle > 1e-3:
                    e = t_root_local.to_euler()
                    print(f"[~] Racine NIF '{t_root.name}' tournee "
                          f"({math.degrees(e.x):.0f}, {math.degrees(e.y):.0f}, "
                          f"{math.degrees(e.z):.0f})° : conservee telle quelle")
                new_root.matrix_world = mat_ref @ t_root_local
                continue
            nif_euler = t_root_local.to_euler(EULER_ORDER)
            nif_euler_signed = Euler(
                (nif_euler.x * ROT_SIGN_X, nif_euler.y * ROT_SIGN_Y, nif_euler.z * ROT_SIGN_Z),
                EULER_ORDER
            )
            nif_mat = nif_euler_signed.to_matrix().to_4x4()
            nif_mat.translation = t_root_local.translation
            nif_mat = nif_mat @ Matrix.Diagonal((*t_root_local.to_scale(), 1.0))
            new_root.matrix_world = mat_ref @ nif_mat

        # Objets parentés à un OS (parent_type='BONE') : matrix_local ignore la
        # transformation de l'os, donc la restaurer ci-dessus les décale (ex. silt
        # strider couché a 90°). On recale leur monde sur celui de l'importeur,
        # exprimé relativement à la racine du NIF. Hors mode npc (les NPCs ont leur
        # propre gestion des nœuds d'attache).
        if IMPORT_MODE != "npc":
            def _root_of(t):
                while True:
                    par = snapshot[t]['parent']
                    if par is None or par not in copied_map:
                        return t
                    t = par
            copied_vals = set(copied_map.values())
            for t_obj in template_objs:
                new_obj = copied_map[t_obj]
                if new_obj.parent_type != 'BONE' or new_obj.parent not in copied_vals:
                    continue
                t_root = _root_of(t_obj)
                if 'matrix_world' not in snapshot[t_obj] or t_root not in copied_map:
                    continue
                bpy.context.view_layer.update()
                new_obj.matrix_world = (copied_map[t_root].matrix_world
                                        @ snapshot[t_root]['matrix_world'].inverted()
                                        @ snapshot[t_obj]['matrix_world'])
                print(f"[~] Objet parenté à un os recalé : '{new_obj.name}' "
                      f"(os '{new_obj.parent_bone}')")

        if DEBUG_MESH and DEBUG_MESH in mesh_path.lower():
            bpy.context.view_layer.update()
            print(f"[DBG] {mesh_path} (ref rot MW = {tuple(round(math.degrees(r), 1) for r in rotation)})")
            for o in copied_map.values():
                e = o.matrix_world.to_euler()
                extra = f" pose={o.data.pose_position}" if o.type == 'ARMATURE' else ""
                extra += f" ptype={o.parent_type}" + (f"/{o.parent_bone}" if o.parent_bone else "")
                print(f"[DBG]   {o.type:8s} '{o.name}' parent={o.parent.name if o.parent else None} "
                      f"rot=({math.degrees(e.x):.0f},{math.degrees(e.y):.0f},{math.degrees(e.z):.0f}) "
                      f"dims={tuple(round(d, 2) for d in o.dimensions)}{extra}")

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
                        if not _rest_remap(obj, arm, canonical):
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


# ==========================================
# TERRAIN & EAU (exterior cells)
# ==========================================
LAND_N = 65                      # sommets par côté
LAND_STEP = CELL_SIZE / 64.0     # 128 unités MW
LAND_HEIGHT_UNIT = 8.0           # les hauteurs MW sont en multiples de 8

landscape_map = {}               # (gx, gy) -> record Landscape
ltex_map = {}                    # index LTEX -> file_name
terrain_mat_cache = {}
water_mat = None


def build_landscape_maps(data):
    # data est dans l'ordre de chargement : le dernier gagne
    for rec in data:
        t = rec.get("type")
        if t == "Landscape" and rec.get("grid"):
            g = rec["grid"]
            landscape_map[(int(g[0]), int(g[1]))] = rec
        elif t == "LandscapeTexture":
            ltex_map[int(rec.get("index", 0))] = rec.get("file_name", "")
    print(f"[+] Landscape: {len(landscape_map)} | LTEX: {len(ltex_map)}")


LAND_TEX_CHUNKED = True


def _unwrap(v):
    if isinstance(v, dict) and "data" in v:
        return v["data"]
    return v


def _zstd_decompress(raw):
    try:
        from compression import zstd            # Python 3.14+
        return zstd.decompress(raw)
    except ImportError:
        pass
    try:
        import zstandard
        return zstandard.ZstdDecompressor().decompressobj().decompress(raw)
    except ImportError:
        pass
    exe = shutil.which("zstd")
    if not exe:
        raise RuntimeError("zstd introuvable : `sudo pacman -S zstd` ou `pip install zstandard`")
    return subprocess.run([exe, "-d", "-c", "-q"], input=raw,
                          stdout=subprocess.PIPE, check=True).stdout


def _numbers(v, fmt):
    """Liste plate de nombres depuis un champ tes3conv.
    fmt : 'b' int8, 'B' uint8, 'H' uint16 (little endian).
    Accepte un blob base64(+zstd) ou un tableau JSON (éventuellement imbriqué)."""
    v = _unwrap(v)
    if v is None:
        return None
    if isinstance(v, str):
        raw = base64.b64decode(v)
        if raw[:4] == b"\x28\xb5\x2f\xfd":
            raw = _zstd_decompress(raw)
        size = struct.calcsize(fmt)
        n = len(raw) // size
        return list(struct.unpack(f"<{n}{fmt}", raw[:n * size]))
    out = []
    def rec(x):
        if isinstance(x, (list, tuple)):
            for i in x:
                rec(i)
        else:
            out.append(x)
    rec(v)
    return out


def decode_heights(land):
    N = LAND_N
    flat = [[0.0] * N for _ in range(N)]
    vh = land.get("vertex_heights")
    if not vh:
        return flat
    offset = float(vh.get("offset", 0.0)) if isinstance(vh, dict) else 0.0
    nums = _numbers(vh, "b")
    if not nums or len(nums) < N * N:
        print(f"[!] vertex_heights inattendu ({0 if not nums else len(nums)} valeurs), terrain plat")
        return flat
    row_start = offset
    for y in range(N):
        row = nums[y * N:(y + 1) * N]
        row_start += row[0]              # delta par rapport au début de la rangée précédente
        v = row_start
        flat[y][0] = v * LAND_HEIGHT_UNIT
        for x in range(1, N):
            v += row[x]                  # delta par rapport au sommet précédent
            flat[y][x] = v * LAND_HEIGHT_UNIT
    return flat


def load_land_image(file_name):
    if not file_name:
        return None
    clean = file_name.replace("\\", "/")
    base = os.path.splitext(clean)[0]
    candidates = [clean] + [base + e for e in (".dds", ".tga", ".png", ".bmp")]
    for d in TEXTURES_DIRS:
        for c in candidates:
            p = resolve_case_insensitive(os.path.join(d, c))
            if p and os.path.isfile(p):
                img = bpy.data.images.load(p, check_existing=True)
                try:
                    if not img.packed_file:
                        img.pack()
                except Exception:
                    pass
                return img
    print(f"[-] Land texture not found: {file_name}")
    return None


def get_terrain_material(vtex):
    """vtex : valeur de texture_indices (0 = défaut, sinon index LTEX + 1)."""
    if vtex in terrain_mat_cache:
        return terrain_mat_cache[vtex]
    file_name = "_land_default.dds" if vtex == 0 else ltex_map.get(vtex - 1, "")
    stem = os.path.splitext(os.path.basename(file_name))[0] or f"idx{vtex}"
    mat = bpy.data.materials.new(f"LAND_{stem}")
    mat.use_nodes = True
    nt = mat.node_tree
    bsdf = nt.nodes.get("Principled BSDF")
    bsdf.inputs["Roughness"].default_value = 1.0
    bsdf.inputs["IOR"].default_value = 1.0
    img = load_land_image(file_name)
    if img:
        tex = nt.nodes.new("ShaderNodeTexImage")
        tex.image = img
        attr = nt.nodes.new("ShaderNodeAttribute")
        attr.attribute_name = "Col"
        mix = nt.nodes.new("ShaderNodeMixRGB")
        mix.blend_type = "MULTIPLY"
        mix.inputs[0].default_value = 1.0
        nt.links.new(tex.outputs["Color"], mix.inputs[1])
        nt.links.new(attr.outputs["Color"], mix.inputs[2])
        nt.links.new(mix.outputs["Color"], bsdf.inputs["Base Color"])
    else:
        bsdf.inputs["Base Color"].default_value = (0.25, 0.3, 0.15, 1.0)
    terrain_mat_cache[vtex] = mat
    return mat

LAND_TILE_PX = 128      # pixels par tuile (16x16 tuiles) -> image de 2048x2048 par cell
                        # 64 pour tester vite, 256 pour plus de netteté (lourd)
land_layer_cache = {}   # vtex -> (T,T,4) float32 ou None


def decode_texture_grid(land):
    tn = _numbers(land.get("texture_indices"), "H")
    if not tn or len(tn) < 256:
        return None
    if LAND_TEX_CHUNKED:
        ti = [[0] * 16 for _ in range(16)]
        k = 0
        for y1 in range(4):
            for x1 in range(4):
                for y2 in range(4):
                    for x2 in range(4):
                        ti[y1 * 4 + y2][x1 * 4 + x2] = tn[k]
                        k += 1
        return ti
    return [tn[y * 16:(y + 1) * 16] for y in range(16)]


def _layer_pixels(vtex):
    """Pixels (T,T,4) float32 de la texture de terrain vtex, ou None."""
    if vtex in land_layer_cache:
        return land_layer_cache[vtex]
    T = LAND_TILE_PX
    file_name = "_land_default.dds" if vtex == 0 else ltex_map.get(vtex - 1, "")
    img = load_land_image(file_name)
    px = None
    if img:
        tmp = None
        try:
            tmp = img.copy()
            tmp.scale(T, T)
            buf = np.empty(T * T * 4, dtype=np.float32)
            tmp.pixels.foreach_get(buf)
            px = buf.reshape(T, T, 4)
        except Exception as e:
            print(f"[-] Land texture unreadable '{file_name}': {e}")
        finally:
            if tmp:
                bpy.data.images.remove(tmp)
        if img.users == 0:               # image chargée uniquement pour le bake
            bpy.data.images.remove(img)
    land_layer_cache[vtex] = px
    return px


def _bilinear(A, T):
    """A: (17,17) valeurs aux coins de tuiles -> (16T,16T) interpolé."""
    S = 16 * T
    c = (np.arange(S, dtype=np.float32) + 0.5) / T
    i0 = np.minimum(c.astype(np.int32), 15)
    f = (c - i0).astype(np.float32)
    y0, x0 = i0[:, None], i0[None, :]
    fy, fx = f[:, None], f[None, :]
    a00, a01 = A[y0, x0], A[y0, x0 + 1]
    a10, a11 = A[y0 + 1, x0], A[y0 + 1, x0 + 1]
    return (a00 * (1 - fx) + a01 * fx) * (1 - fy) + (a10 * (1 - fx) + a11 * fx) * fy


def bake_terrain_image(ti, gx, gy):
    T = LAND_TILE_PX
    S = 16 * T
    # Ordre de superposition : la couche la plus etendue sert de base, les plus petites
    # sont peintes PAR-DESSUS. Sinon (tri par index), une grande zone peinte apres une
    # petite recouvre les coins partages et la ronge de l'exterieur vers l'interieur.
    counts = {}
    for row in ti:
        for v in row:
            counts[int(v)] = counts.get(int(v), 0) + 1
    layers = sorted(counts, key=lambda vt: (-counts[vt], vt))
    out = None
    for vt in layers:
        tex = _layer_pixels(vt)
        if tex is None:
            tex = np.empty((T, T, 4), dtype=np.float32)
            tex[:] = (0.25, 0.3, 0.15, 1.0)
        tiled = np.tile(tex, (16, 16, 1))            # une répétition par tuile
        if out is None:
            out = tiled                              # couche de base
            continue
        A = np.zeros((17, 17), dtype=np.float32)
        for ty in range(16):
            for tx in range(16):
                if ti[ty][tx] == vt:
                    A[ty:ty + 2, tx:tx + 2] = 1.0    # les 4 coins de la tuile
        alpha = _bilinear(A, T)[..., None]
        out += (tiled - out) * alpha
    out[..., 3] = 1.0

    img = bpy.data.images.new(f"LandBake_{gx}_{gy}", S, S, alpha=False)
    img.pixels.foreach_set(out.ravel())
    img.update()
    img.pack()
    return img


def make_baked_material(img, gx, gy):
    mat = bpy.data.materials.new(f"LAND_{gx}_{gy}")
    mat.use_nodes = True
    nt = mat.node_tree
    bsdf = nt.nodes.get("Principled BSDF")
    bsdf.inputs["Roughness"].default_value = 1.0
    bsdf.inputs["IOR"].default_value = 1.0
    tex = nt.nodes.new("ShaderNodeTexImage")
    tex.image = img
    attr = nt.nodes.new("ShaderNodeAttribute")
    attr.attribute_name = "Col"
    mix = nt.nodes.new("ShaderNodeMixRGB")
    mix.blend_type = "MULTIPLY"
    mix.inputs[0].default_value = 1.0
    nt.links.new(tex.outputs["Color"], mix.inputs[1])
    nt.links.new(attr.outputs["Color"], mix.inputs[2])
    nt.links.new(mix.outputs["Color"], bsdf.inputs["Base Color"])
    return mat

def build_terrain(land, origin_offset, collection):
    gx, gy = int(land["grid"][0]), int(land["grid"][1])
    heights = decode_heights(land)
    N = LAND_N

    verts = []
    for y in range(N):
        for x in range(N):
            w = Vector((gx * CELL_SIZE + x * LAND_STEP,
                        gy * CELL_SIZE + y * LAND_STEP,
                        heights[y][x]))
            verts.append(w * SCALE_FACTOR - origin_offset)

    faces = []
    for y in range(N - 1):
        for x in range(N - 1):
            i = y * N + x
            faces.append((i, i + 1, i + N + 1, i + N))

    mesh = bpy.data.meshes.new(f"Terrain_{gx}_{gy}")
    mesh.from_pydata([tuple(v) for v in verts], [], faces)
    mesh.update()

    # Texture bakée (blend façon moteur) ; fallback sur la texture par défaut
    ti = decode_texture_grid(land)
    if ti:
        mesh.materials.append(make_baked_material(bake_terrain_image(ti, gx, gy), gx, gy))
    else:
        mesh.materials.append(get_terrain_material(0))

    # UV : une seule image pour toute la cell
    uv = mesh.uv_layers.new(name="UVMap")
    for poly in mesh.polygons:
        for li, vi in zip(poly.loop_indices, poly.vertices):
            uv.data[li].uv = ((vi % N) / 64.0, (vi // N) / 64.0)

    cn = _numbers(land.get("vertex_colors"), "B")
    if cn and len(cn) >= N * N * 3:
        ca = mesh.color_attributes.new("Col", "FLOAT_COLOR", "POINT")
        for i in range(N * N):
            ca.data[i].color = (cn[i * 3] / 255.0, cn[i * 3 + 1] / 255.0,
                                cn[i * 3 + 2] / 255.0, 1.0)

    try:
        mesh.shade_smooth()
    except AttributeError:
        for p in mesh.polygons:
            p.use_smooth = True

    obj = bpy.data.objects.new(f"Terrain_{gx}_{gy}", mesh)
    collection.objects.link(obj)
    return obj


def get_water_material():
    global water_mat
    if water_mat:
        return water_mat
    m = bpy.data.materials.new("Water")
    m.use_nodes = True
    b = m.node_tree.nodes.get("Principled BSDF")
    b.inputs["Base Color"].default_value = (0.025, 0.095, 0.130, 1.0)
    b.inputs["Alpha"].default_value = 0.15
    b.inputs["Roughness"].default_value = 0.05
    if hasattr(m, "blend_method"):
        try:
            m.blend_method = "BLEND"
        except Exception:
            pass
    water_mat = m
    return m


def build_water(gx, gy, level, origin_offset, collection):
    z = level * SCALE_FACTOR
    x0, y0 = gx * CELL_SIZE * SCALE_FACTOR, gy * CELL_SIZE * SCALE_FACTOR
    x1, y1 = x0 + CELL_SIZE * SCALE_FACTOR, y0 + CELL_SIZE * SCALE_FACTOR
    vs = [Vector(p) - origin_offset for p in
          ((x0, y0, z), (x1, y0, z), (x1, y1, z), (x0, y1, z))]
    mesh = bpy.data.meshes.new(f"Water_{gx}_{gy}")
    mesh.from_pydata([tuple(v) for v in vs], [], [(0, 1, 2, 3)])
    mesh.update()
    mesh.materials.append(get_water_material())
    obj = bpy.data.objects.new(f"Water_{gx}_{gy}", mesh)
    collection.objects.link(obj)
    return obj


def build_exterior_ground(cell, origin_offset, collection):
    gx, gy = cell_grid(cell)
    land = landscape_map.get((gx, gy))
    if land:
        build_terrain(land, origin_offset, collection)
    else:
        print(f"[~] No LAND record for cell ({gx},{gy})")
    wh = cell.get("water_height")
    build_water(gx, gy, float(wh) if wh is not None else 0.0, origin_offset, collection)


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
    build_landscape_maps(data)
    
    cell_count = 0
    object_count = 0

    # Dedupliquer les cellules par nom : en cas de doublon (vanilla + mod),
    # on garde la derniere occurrence (les mods sont apres le base game)
    seen_cells = {}
    for record in data:
        if record.get("type") != "Cell":
            continue
        seen_cells[cell_key(record)] = record

    # Filtrer par cellule cible si specifiee
    grid_origin = Vector((0.0, 0.0, 0.0))
    if TARGET_GRID is not None:
        gx1, gy1, gx2, gy2 = TARGET_GRID
        cells_to_process = sorted(
            (c for c in seen_cells.values()
             if not cell_is_interior(c)
             and gx1 <= cell_grid(c)[0] <= gx2
             and gy1 <= cell_grid(c)[1] <= gy2),
            key=lambda c: (cell_grid(c)[1], cell_grid(c)[0]))
        expected = (gx2 - gx1 + 1) * (gy2 - gy1 + 1)
        print(f"[+] Exterior grid {TARGET_GRID}: {len(cells_to_process)}/{expected} cell(s) found")
        # Origine commune a toutes les cells : centre du rectangle demande
        # (le niveau de la mer reste a z=0, les cells restent alignees)
        grid_origin = Vector((
            (gx1 + gx2 + 1) / 2.0 * CELL_SIZE,
            (gy1 + gy2 + 1) / 2.0 * CELL_SIZE,
            0.0)) * SCALE_FACTOR
    elif TARGET_CELL_NAME is not None:
        if TARGET_CELL_NAME in seen_cells:
            cells_to_process = [seen_cells[TARGET_CELL_NAME]]
        else:
            # Exterior nommee ("Seyda Neen"...) : correspondance par nom
            cells_to_process = [c for c in seen_cells.values()
                                if not cell_is_interior(c)
                                and c.get("name") == TARGET_CELL_NAME]
    else:
        cells_to_process = list(seen_cells.values())

    # Mode npcs + --npc : recherche par nom dans toutes les cellules
    if IMPORT_MODE == "npc" and TARGET_NPC_NAME:
        rebuild_npc_by_name(record_map, seen_cells)
        return

    if FULL_MODE:
        rebuild_full(cells_to_process, grid_origin, record_map, seen_cells)
        return

    for cell in cells_to_process:
        cell_label = cell_display_label(cell)
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

        # Interieur : centrer sur la premiere reference.
        # Exterieur (--grid) : origine commune a toutes les cells.
        origin_offset = Vector((0.0, 0.0, 0.0))
        if TARGET_GRID is not None:
            origin_offset = grid_origin
        else:
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

        if IMPORT_MODE != "npc" and not cell_is_interior(cell):
            build_exterior_ground(cell, origin_offset, cell_collection)

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
# MODE FULL : exterieur + interieurs (via les portes) + NPCs
# ==========================================
# Colle ce bloc juste avant la section "# TEXTURES" de 1-generate_blend.py.
# (necessite `import time` en haut du fichier)

INTERIOR_MAX_DEPTH = int(get_arg("interior_depth", 3))  # portes interieur -> interieur suivies
FULL_WITH_NPCS = get_arg("npcs", "1") != "0"             # --npcs 0 : ignorer les NPCs


def _set_import_mode(mode):
    """Bascule entre 'cell' (decor statique) et 'npc' (NPCs/creatures)."""
    global IMPORT_MODE
    IMPORT_MODE = mode


_SCRATCH = None


def _scratch_collection():
    global _SCRATCH
    if _SCRATCH is None or _SCRATCH.name not in bpy.data.collections:
        _SCRATCH = bpy.data.collections.new("MW_scratch")
        bpy.context.scene.collection.children.link(_SCRATCH)
    return _SCRATCH


class _IsolatedView:
    """Exclut du view layer toutes les collections sauf `keep`.

    Chaque ajout/suppression d'objet force Blender a reconstruire le depsgraph de
    TOUTE la scene ; un NPC en fait des dizaines, d'ou des minutes par NPC quand
    l'exterieur contient des centaines d'objets. Isole, le depsgraph ne contient
    plus que le NPC en cours."""

    def __init__(self, keep):
        self.keep = keep
        self.saved = []
        self.prev_active = None

    def __enter__(self):
        vl = bpy.context.view_layer
        self.prev_active = vl.active_layer_collection
        for lc in vl.layer_collection.children:
            if lc.collection is self.keep:
                vl.active_layer_collection = lc   # l'importeur NIF cree ses objets ici
                continue
            self.saved.append((lc, lc.exclude))
            lc.exclude = True
        return self

    def __exit__(self, *exc):
        vl = bpy.context.view_layer
        for lc, was_excluded in self.saved:
            lc.exclude = was_excluded
        if self.prev_active is not None:
            try:
                vl.active_layer_collection = self.prev_active
            except Exception:
                pass
        vl.update()


def import_cell_refs(cell, record_map, origin_offset, collection, with_npcs=True):
    """Decor statique puis NPCs/creatures d'une cell. Retourne le nombre d'objets."""
    refs = [r for r in (cell.get("references") or []) if not r.get("deleted", False)]
    count = 0

    _set_import_mode("cell")
    for ref in refs:
        rec_info = record_map.get((ref.get("id") or "").lower(), {})
        if not should_import(rec_info.get("type")):
            continue
        count += place_reference(rec_info, ref, origin_offset, collection)

    if not with_npcs:
        return count

    _set_import_mode("npc")
    try:
        npc_refs = []
        for ref in refs:
            rec_info = record_map.get((ref.get("id") or "").lower(), {})
            if should_import(rec_info.get("type")):
                npc_refs.append((ref, rec_info))
        if not npc_refs:
            return count

        # Un NPC a la fois dans une collection vide et isolee :
        #  - depsgraph minuscule (rapide) ;
        #  - le remplacement "peau -> vetement" de place_reference parcourt toute la
        #    collection : sur une collection partagee il supprimerait les parties de
        #    peau des NPCs precedents de la meme race.
        scratch = _scratch_collection()
        t_all = time.time()
        with _IsolatedView(scratch):
            for i, (ref, rec_info) in enumerate(npc_refs, 1):
                t0 = time.time()
                count += place_reference(rec_info, ref, origin_offset, scratch)
                for obj in list(scratch.objects):
                    collection.objects.link(obj)
                    scratch.objects.unlink(obj)
                label = rec_info.get("name") or rec_info.get("id")
                print(f"    [NPC {i}/{len(npc_refs)}] {label}: {time.time() - t0:.1f}s", flush=True)
        print(f"    [NPC] {len(npc_refs)} importe(s) en {time.time() - t_all:.1f}s", flush=True)
    finally:
        _set_import_mode("cell")
    return count


def find_interior_doors(cell, record_map, interiors_by_name):
    """[(ref, nom_interieur, destination)] des portes qui menent a un interieur.

    `destination` est le dict 'destination' de la reference (translation + rotation
    du point d'arrivee dans l'interieur)."""
    out = []
    for ref in cell.get("references") or []:
        if ref.get("deleted", False):
            continue
        dest = ref.get("destination")
        if not isinstance(dest, dict):
            continue
        name = (dest.get("cell") or "").strip()
        if not name or name.lower() not in interiors_by_name:
            continue  # porte vers l'exterieur (ou cell inconnue)
        rec_info = record_map.get((ref.get("id") or "").lower(), {})
        if rec_info.get("type") != "Door":
            continue
        out.append((ref, name, dest))
    return out


def reset_scene():
    """Repart d'une scene vide entre deux fichiers : objets, collections, donnees
    orphelines (meshes, materiaux, images, armatures, actions) et caches lies a Blender."""
    global _SCRATCH, water_mat, NPC_RACE_SKINS_MISSING
    for obj in list(bpy.data.objects):          # y compris les templates du cache d'import
        bpy.data.objects.remove(obj, do_unlink=True)
    for coll in list(bpy.data.collections):
        bpy.data.collections.remove(coll)
    imported_mesh_cache.clear()
    imported_mesh_snapshots.clear()
    GLOBAL_TEMPLATE_COPY_MAP.clear()
    terrain_mat_cache.clear()
    _PRUNED_ACTIONS.clear()
    water_mat = None
    _SCRATCH = None
    NPC_RACE_SKINS_MISSING = False
    try:
        bpy.data.orphans_purge(do_recursive=True)
    except TypeError:
        bpy.data.orphans_purge()
    bpy.context.view_layer.update()


def finalize_and_save(path):
    """Corrige les textures puis ecrit la scene courante dans `path` (.blend compresse)."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    print(f"[+] Fixing textures + saving {path}")
    fix_missing_textures()
    # Le mode REST n'etait necessaire que pour les calculs de bake : le restaurer,
    # sinon l'armature reste evaluee au repos (T-Pose figee).
    for arm in bpy.data.armatures:
        if arm.pose_position != 'POSE':
            arm.pose_position = 'POSE'
    bpy.ops.wm.save_as_mainfile(filepath=path, compress=True, copy=True)


def _safe_stem(name, used):
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("_.")[:80] or "interior"
    stem, n = base, 2
    while stem.lower() in used:
        stem = f"{base}_{n}"
        n += 1
    used.add(stem.lower())
    return stem


def _r(v, nd=4):
    return [round(float(x), nd) for x in v]


def rebuild_full(cells_to_process, grid_origin, record_map, seen_cells):
    """Mode full : un .blend pour l'exterieur + un .blend par interieur atteignable.

    export/exterior.blend            exterieur (terrain, eau, decor, NPCs)
    export/interiors/<nom>.blend     un interieur, origine = point d'arrivee de la 1re porte
    export/manifest.json             portes : exterieur/interieur <-> interieur, positions
    export/files.txt                 liste des fichiers (sans extension) pour run.sh
    """
    print()
    print("=" * 60)
    print("[+] FULL MODE : exterior + interiors + NPCs (un .blend par cell)")
    print("=" * 60)

    # Sorties precedentes de ce dossier (gere par ce script) : on repart de zero
    shutil.rmtree(os.path.join(FULL_OUTDIR, "interiors"), ignore_errors=True)
    for old in ("exterior.blend", "exterior.glb", "manifest.json", "files.txt"):
        try:
            os.remove(os.path.join(FULL_OUTDIR, old))
        except OSError:
            pass
    os.makedirs(os.path.join(FULL_OUTDIR, "interiors"), exist_ok=True)

    interiors_by_name = {}
    for c in seen_cells.values():
        if cell_is_interior(c) and c.get("name"):
            interiors_by_name[c["name"].lower()] = c

    produced = []                      # chemins sans extension, relatifs a FULL_OUTDIR
    object_count = 0
    t_start = time.time()

    # ---------- 1. Exterieur ----------
    queue = []   # (nom, porte, destination, 'exterior'|nom parent, position porte, profondeur)
    seen_doors = 0
    for cell in cells_to_process:
        label = cell_display_label(cell)
        coll = bpy.data.collections.new(f"MW_{label}"[:60])
        bpy.context.scene.collection.children.link(coll)
        print(f"[+] Exterior cell {label} ({len(cell.get('references') or [])} refs)")
        build_exterior_ground(cell, grid_origin, coll)
        object_count += import_cell_refs(cell, record_map, grid_origin, coll, FULL_WITH_NPCS)

        for ref, name, dest in find_interior_doors(cell, record_map, interiors_by_name):
            seen_doors += 1
            p = Vector(ref.get("translation", [0.0, 0.0, 0.0])) * SCALE_FACTOR - grid_origin
            queue.append((name, ref, dest, "exterior", p, 1))

    finalize_and_save(os.path.join(FULL_OUTDIR, "exterior.blend"))
    produced.append("exterior")
    reset_scene()

    print(f"[+] {seen_doors} porte(s) vers un interieur dans l'exterieur")
    if seen_doors == 0:
        print("[!] Aucune porte trouvee : verifie le champ 'destination' des references "
              "(jq '.[]|select(.type==\"Cell\")|.references[]|select(.destination)' output.json | head)")

    # ---------- 2. Interieurs (BFS) : un fichier chacun ----------
    manifest_interiors = {}            # nom en minuscules -> entree du manifest
    origins = {}                       # nom en minuscules -> origin_offset (Vector)
    used_stems = set()
    while queue:
        name, door_ref, dest, parent, door_pos, depth = queue.pop(0)
        key = name.lower()
        arrival = Vector(dest.get("translation") or [0.0, 0.0, 0.0]) * SCALE_FACTOR

        if key in manifest_interiors:
            # Interieur deja exporte : on note juste cette porte supplementaire
            manifest_interiors[key]["doors"].append({
                "from": parent,
                "door_id": door_ref.get("id"),
                "door_position": _r(door_pos),
                "arrival_position": _r(arrival - origins[key]),
                "arrival_rotation": _r(dest.get("rotation") or [0.0, 0.0, 0.0]),
            })
            continue

        cell = interiors_by_name[key]
        stem = _safe_stem(name, used_stems)
        origin_offset = arrival.copy()   # le point d'arrivee de la 1re porte = origine du fichier
        origins[key] = origin_offset

        t0 = time.time()
        coll = bpy.data.collections.new(f"MW_{name}"[:60])
        bpy.context.scene.collection.children.link(coll)
        print(f"[+] Interior '{name}' (depth {depth}, {len(cell.get('references') or [])} refs) -> interiors/{stem}.blend")
        object_count += import_cell_refs(cell, record_map, origin_offset, coll, FULL_WITH_NPCS)

        finalize_and_save(os.path.join(FULL_OUTDIR, "interiors", stem + ".blend"))
        produced.append(f"interiors/{stem}")
        reset_scene()
        print(f"    [+] '{name}' termine en {time.time() - t0:.1f}s ({len(produced) - 1} interieur(s) faits)", flush=True)

        manifest_interiors[key] = {
            "name": name,
            "file": f"interiors/{stem}",
            "depth": depth,
            "doors": [{
                "from": parent,
                "door_id": door_ref.get("id"),
                "door_position": _r(door_pos),
                "arrival_position": [0.0, 0.0, 0.0],
                "arrival_rotation": _r(dest.get("rotation") or [0.0, 0.0, 0.0]),
            }],
        }

        # Portes interieur -> interieur : position de la porte dans CE fichier
        if depth < INTERIOR_MAX_DEPTH:
            for ref, nxt, nxt_dest in find_interior_doors(cell, record_map, interiors_by_name):
                p = Vector(ref.get("translation", [0.0, 0.0, 0.0])) * SCALE_FACTOR - origin_offset
                queue.append((nxt, ref, nxt_dest, name, p, depth + 1))

    # ---------- 3. Manifest + liste de fichiers ----------
    manifest = {
        "scale": SCALE_FACTOR,
        "notes": "Positions en metres, repere Blender (Z haut), dans le fichier indique. "
                 "Chaque interieur a son origine sur le point d'arrivee de sa premiere porte ; "
                 "'arrival_position' donne le point d'arrivee de chaque porte dans le fichier de l'interieur.",
        "exterior": {
            "file": "exterior",
            "grid": list(TARGET_GRID),
            "origin_mw": _r(grid_origin / SCALE_FACTOR, 2),
        },
        "interiors": list(manifest_interiors.values()),
    }
    with open(os.path.join(FULL_OUTDIR, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    with open(os.path.join(FULL_OUTDIR, "files.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(produced) + "\n")

    print()
    print("=" * 60)
    print("[+] FULL RECONSTRUCTION COMPLETE")
    print("=" * 60)
    print(f"[+] Exterior cells: {len(cells_to_process)}")
    print(f"[+] Interiors:      {len(manifest_interiors)}")
    print(f"[+] Objects:        {object_count}")
    print(f"[+] Files:          {len(produced)} .blend dans {FULL_OUTDIR}")
    print(f"[+] Time:           {time.time() - t_start:.0f}s")
    print("=" * 60)


# ==========================================
# TEXTURES
# ==========================================

_TEX_INDEX = None


def fix_missing_textures():
    rebound_count = 0

    # Index recursif de toutes les textures disponibles (construit une seule fois :
    # en mode full cette fonction est appelee une fois par .blend)
    global _TEX_INDEX
    if _TEX_INDEX is None:
        _TEX_INDEX = {}
        for tex_dir in TEXTURES_DIRS:
            resolved_dir = resolve_case_insensitive(tex_dir)
            if not resolved_dir or not os.path.isdir(resolved_dir):
                continue
            for root, _dirs, files in os.walk(resolved_dir):
                for f in files:
                    key = f.lower()
                    if key not in _TEX_INDEX:
                        _TEX_INDEX[key] = os.path.join(root, f)
    tex_index = _TEX_INDEX

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
report_failed_meshes()

if not FULL_MODE:   # en mode full, chaque .blend est ecrit (textures corrigees) par rebuild_full
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
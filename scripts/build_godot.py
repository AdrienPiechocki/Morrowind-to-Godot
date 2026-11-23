#!/usr/bin/env python3
"""Assemble un dossier Godot complet a partir des exports (glTF + manifests).

Python pur (pas de bpy). Sorties, dans <out>/ :

  project.godot                 (pratique pour ouvrir le dossier directement)
  <name>/                       <- le dossier a copier dans votre projet Godot
    world.tscn                  scene principale : exterieur + farlands + portes + soleil + camera
    farlands.tscn               tous les chunks LOD
    scenes/interiors/*.tscn     un interieur par scene (marqueurs d'arrivee / portes)
    models/world/               glTF + textures de l'export "full" (ou morrowind.gltf)
    models/lod/                 glTF + textures des farlands
    scripts/fly_cam.gd          camera libre (clic droit + ZQSD/WASD, Shift = rapide)
    manifest.json

Les chemins des scenes sont en res://<name>/... : si vous renommez le dossier, relancez avec
--name, ou editez-les.

Sources (toutes optionnelles, au moins une requise) :
  --export DIR        export "full" (exterior.* + interiors/ + manifest.json + textures/)
  --single FILE       gltf unique (modes interior / exterior / npc) ; --single-textures DIR
  --lod DIR           export_lod (lod_*.gltf + manifest.json + textures/)
"""
import argparse
import json
import math
import re
import shutil
import sys
from pathlib import Path

ASSET_EXT = {".gltf", ".glb", ".bin", ".png", ".jpg", ".jpeg", ".webp"}


# ------------------------------------------------------------------
# Utilitaires
# ------------------------------------------------------------------

def to_godot(p):
    """Repere Blender (Z haut) -> Godot (Y haut) : (x, y, z) -> (x, z, -y)."""
    return (p[0], p[2], -p[1])


def num(v, nd=4):
    """Nombre court, sans '-0'."""
    t = f"{v:.{nd}f}".rstrip("0").rstrip(".")
    return "0" if t in ("", "-0") else t


def vec3(p):
    return "Vector3(%s)" % ", ".join(num(v) for v in p)


def transform(pos=(0, 0, 0), yaw=0.0):
    """Transform3D : rotation autour de Y (radians) + position (deja en repere Godot)."""
    c, s = math.cos(yaw), math.sin(yaw)
    basis = (c, 0, s, 0, 1, 0, -s, 0, c)      # Ry, ligne par ligne
    vals = list(basis) + list(pos)
    return "Transform3D(%s)" % ", ".join(num(v, 5) for v in vals)


def node_name(text, used):
    """Nom de noeud Godot valide et unique."""
    base = re.sub(r'[.:@/"%\\\s]+', "_", str(text)).strip("_") or "Node"
    name, n = base, 2
    while name in used:
        name, n = f"{base}_{n}", n + 1
    used.add(name)
    return name


def gltf_file(base):
    """base (sans extension) -> .gltf ou .glb existant, ou None."""
    for ext in (".gltf", ".glb"):
        p = Path(str(base) + ext)
        if p.is_file():
            return p
    return None


def copy_assets(src, dst):
    """Copie glTF/bin/images en conservant l'arborescence (pas de .blend, logs...)."""
    n = 0
    for f in Path(src).rglob("*"):
        if f.is_file() and f.suffix.lower() in ASSET_EXT:
            target = Path(dst) / f.relative_to(src)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, target)
            n += 1
    return n


class Scene:
    """Ecrivain minimal de .tscn (format 3, Godot 4)."""

    def __init__(self):
        self.ext = []        # (type, path)
        self.subs = []       # texte de sous-ressources
        self.nodes = []      # texte des noeuds
        self._ext_ids = {}

    def ext_resource(self, rtype, path):
        key = (rtype, path)
        if key not in self._ext_ids:
            self.ext.append(key)
            self._ext_ids[key] = len(self.ext)
        return self._ext_ids[key]

    def sub(self, rtype, props):
        sid = f"sub{len(self.subs) + 1}"
        body = "".join(f"{k} = {v}\n" for k, v in props.items())
        self.subs.append(f'[sub_resource type="{rtype}" id="{sid}"]\n{body}')
        return sid

    def node(self, name, ntype=None, parent=None, instance=None, props=None, meta=None):
        head = f'[node name="{name}"'
        if ntype:
            head += f' type="{ntype}"'
        if parent is not None:
            head += f' parent="{parent}"'
        if instance is not None:
            head += f' instance=ExtResource("{instance}")'
        head += "]\n"
        body = "".join(f"{k} = {v}\n" for k, v in (props or {}).items())
        body += "".join(f"metadata/{k} = {v}\n" for k, v in (meta or {}).items())
        self.nodes.append(head + body)

    def write(self, path):
        steps = len(self.ext) + len(self.subs) + 1
        out = [f"[gd_scene load_steps={steps} format=3]\n"]
        for i, (t, p) in enumerate(self.ext, 1):
            out.append(f'[ext_resource type="{t}" path="{p}" id="{i}"]\n')
        out.extend(self.subs)
        out.extend(self.nodes)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text("\n".join(out), encoding="utf-8")


def string(s):
    return json.dumps(str(s), ensure_ascii=False)


# ------------------------------------------------------------------
# Environnement commun
# ------------------------------------------------------------------

def add_environment(sc, sun=True, ambient=0.5):
    sky_mat = sc.sub("ProceduralSkyMaterial", {})
    sky = sc.sub("Sky", {"sky_material": f'SubResource("{sky_mat}")'})
    env = sc.sub("Environment", {
        "background_mode": "2",
        "sky": f'SubResource("{sky}")',
        "ambient_light_source": "3" if sun else "2",
        "ambient_light_color": "Color(0.75, 0.78, 0.85, 1)",
        "ambient_light_energy": f"{ambient}",
        "tonemap_mode": "2",
        "fog_enabled": "true" if sun else "false",
        "fog_light_color": "Color(0.62, 0.68, 0.76, 1)",
        "fog_density": "0.0003",
    })
    sc.node("WorldEnvironment", "WorldEnvironment", ".", props={"environment": f'SubResource("{env}")'})
    if sun:
        # inclinaison du soleil : rotation X de -50 deg puis yaw
        ax = math.radians(-50)
        cx_, sx_ = math.cos(ax), math.sin(ax)
        cy_, sy_ = math.cos(math.radians(35)), math.sin(math.radians(35))
        # basis = Ry * Rx
        b = (cy_, sy_ * sx_, sy_ * cx_,
             0, cx_, -sx_,
             -sy_, cy_ * sx_, cy_ * cx_)
        rot = "Transform3D(%s, 0, 0, 0)" % ", ".join(f"{v:.5f}" for v in b)
        sc.node("Sun", "DirectionalLight3D", ".", props={
            "transform": rot, "shadow_enabled": "true",
            "directional_shadow_max_distance": "400.0"})


def add_camera(sc, cam_script_id, pos, far=20000.0):
    sc.node("FlyCamera", "Camera3D", ".", props={
        "transform": transform(pos), "far": f"{far}", "current": "true",
        "script": f'ExtResource("{cam_script_id}")'})


FLY_CAM = '''extends Camera3D
# Camera libre pour parcourir la scene : clic droit maintenu + souris, ZQSD/WASD, A/E ou Q/E
# pour monter/descendre, Shift = rapide, molette = vitesse.

@export var speed := 30.0
@export var sensitivity := 0.003

var _yaw := 0.0
var _pitch := 0.0

func _ready() -> void:
	_yaw = rotation.y
	_pitch = rotation.x

func _unhandled_input(event: InputEvent) -> void:
	if event is InputEventMouseMotion and Input.is_mouse_button_pressed(MOUSE_BUTTON_RIGHT):
		_yaw -= event.relative.x * sensitivity
		_pitch = clamp(_pitch - event.relative.y * sensitivity, -1.5, 1.5)
		rotation = Vector3(_pitch, _yaw, 0.0)
	elif event is InputEventMouseButton and event.pressed:
		if event.button_index == MOUSE_BUTTON_WHEEL_UP:
			speed *= 1.2
		elif event.button_index == MOUSE_BUTTON_WHEEL_DOWN:
			speed /= 1.2

func _process(delta: float) -> void:
	var dir := Vector3.ZERO
	if Input.is_key_pressed(KEY_W) or Input.is_key_pressed(KEY_UP):
		dir -= global_transform.basis.z
	if Input.is_key_pressed(KEY_S) or Input.is_key_pressed(KEY_DOWN):
		dir += global_transform.basis.z
	if Input.is_key_pressed(KEY_A) or Input.is_key_pressed(KEY_LEFT):
		dir -= global_transform.basis.x
	if Input.is_key_pressed(KEY_D) or Input.is_key_pressed(KEY_RIGHT):
		dir += global_transform.basis.x
	if Input.is_key_pressed(KEY_E) or Input.is_key_pressed(KEY_SPACE):
		dir += Vector3.UP
	if Input.is_key_pressed(KEY_Q) or Input.is_key_pressed(KEY_CTRL):
		dir -= Vector3.UP
	if dir != Vector3.ZERO:
		var mult := 4.0 if Input.is_key_pressed(KEY_SHIFT) else 1.0
		global_position += dir.normalized() * speed * mult * delta
'''


# ------------------------------------------------------------------
# Scenes
# ------------------------------------------------------------------

def build_interior_scene(sc_path, res, stem, model_res, entry, doors_out, cam_id_path):
    """Scene d'un interieur : modele + marqueurs d'arrivee + portes vers d'autres interieurs."""
    sc = Scene()
    model_id = sc.ext_resource("PackedScene", model_res)
    cam_id = sc.ext_resource("Script", cam_id_path)
    sc.node(node_name(entry["name"], set()), "Node3D")
    add_environment(sc, sun=False)
    sc.node("Model", None, ".", instance=model_id)
    used = set()
    sc.node("Arrivals", "Node3D", ".")
    first = None
    for d in entry.get("doors", []):
        pos = to_godot(d.get("arrival_position", [0, 0, 0]))
        yaw = -(d.get("arrival_rotation") or [0, 0, 0])[2]    # ROT_SIGN_Z = -1 cote Blender
        first = first or pos
        sc.node(node_name(f"From_{d.get('from')}_{d.get('door_id')}", used), "Marker3D", "Arrivals",
                props={"transform": transform(pos, yaw)},
                meta={"from": string(d.get("from")), "door_id": string(d.get("door_id")),
                      "arrival_yaw": num(yaw)})
    if doors_out:
        sc.node("Doors", "Node3D", ".")
        for d in doors_out:
            sc.node(node_name(d["name"], used), "Marker3D", "Doors",
                    props={"transform": transform(to_godot(d["pos"]))}, meta=d["meta"])
    add_camera(sc, cam_id, (first[0], first[1] + 1.7, first[2]) if first else (0, 2, 0), far=2000.0)
    sc.write(sc_path)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--export", help="dossier de l'export full (export/)")
    ap.add_argument("--single", help="gltf unique (morrowind.gltf)")
    ap.add_argument("--single-textures", help="dossier de textures de --single")
    ap.add_argument("--single-grid", help="grille (x1,y1:x2,y2) d'un export exterior : recale le modele sur le monde")
    ap.add_argument("--lod", help="dossier export_lod/")
    ap.add_argument("--out", default="godot_export")
    ap.add_argument("--name", default="morrowind", help="nom du dossier dans le projet Godot")
    args = ap.parse_args()

    if not (args.export or args.single or args.lod):
        print("[ERROR] nothing to build (--export / --single / --lod)", file=sys.stderr)
        return 1

    out_root = Path(args.out)
    base = out_root / args.name
    res = f"res://{args.name}"
    if base.exists():
        shutil.rmtree(base)          # dossier genere : on repart de zero
    (base / "scripts").mkdir(parents=True)
    (base / "scripts" / "fly_cam.gd").write_text(FLY_CAM, encoding="utf-8")
    cam_script = f"{res}/scripts/fly_cam.gd"

    world = Scene()
    cam_id = world.ext_resource("Script", cam_script)
    world.node("World", "Node3D")
    add_environment(world, sun=True)
    spawn = (0.0, 50.0, 0.0)
    manifest_out = {}

    # ---------- Exterieur / modele du monde ----------
    ext_gltf = None
    interiors_info = []
    if args.export:
        src = Path(args.export)
        copy_assets(src, base / "models" / "world")
        man_path = src / "manifest.json"
        manifest = json.loads(man_path.read_text(encoding="utf-8")) if man_path.is_file() else {}
        manifest_out["world"] = manifest
        ext_gltf = gltf_file(base / "models" / "world" / "exterior")
        scale = manifest.get("scale", 0.01)
        org = (manifest.get("exterior") or {}).get("origin_mw") or [0, 0, 0]
        # le fichier exterior est centre sur cette origine : on la rend au monde
        ext_pos = to_godot([org[0] * scale, org[1] * scale, org[2] * scale])
        if ext_gltf is None:
            print("[!] exterior.gltf not found in --export")
        else:
            rel = ext_gltf.relative_to(base).as_posix()
            mid = world.ext_resource("PackedScene", f"{res}/{rel}")
            world.node("ExteriorRoot", "Node3D", ".", props={"transform": transform(ext_pos)})
            world.node("Exterior", None, "ExteriorRoot", instance=mid)
            grid = (manifest.get("exterior") or {}).get("grid")
            if grid:
                spawn = (ext_pos[0] , 60.0, ext_pos[2])
        interiors_info = manifest.get("interiors", [])
    elif args.single:
        f = Path(args.single)
        dst = base / "models" / "world"
        dst.mkdir(parents=True, exist_ok=True)
        for part in (f, f.with_suffix(".bin")):
            if part.is_file():
                shutil.copy2(part, dst / part.name)
        tdir = Path(args.single_textures) if args.single_textures else f.parent / "textures_gltf"
        if tdir.is_dir():
            copy_assets(tdir, dst / tdir.name)
        mid = world.ext_resource("PackedScene", f"{res}/models/world/{f.name}")
        pos = (0.0, 0.0, 0.0)
        if args.single_grid:
            nums = [int(n) for n in re.findall(r"-?\d+", args.single_grid)]
            if len(nums) == 2:
                nums *= 2
            if len(nums) == 4:
                x1, y1, x2, y2 = min(nums[0], nums[2]), min(nums[1], nums[3]), max(nums[0], nums[2]), max(nums[1], nums[3])
                cs = 8192 * 0.01     # cell exterieure, en metres
                pos = to_godot(((x1 + x2 + 1) / 2 * cs, (y1 + y2 + 1) / 2 * cs, 0.0))
                spawn = (pos[0], 60.0, pos[2])
        world.node("Model", None, ".", instance=mid, props={"transform": transform(pos)})

    # ---------- Interieurs ----------
    used_doors = set()
    interior_scene_res = {}
    if interiors_info:
        for e in interiors_info:
            stem = Path(e["file"]).name
            g = gltf_file(base / "models" / "world" / e["file"])
            if g is None:
                print(f"[!] interior model not found: {e['file']}")
                continue
            interior_scene_res[e["file"]] = f"{res}/scenes/interiors/{stem}.tscn"

        # portes : exterieur -> world.tscn ; interieur -> scene de l'interieur parent
        per_parent = {}
        world_doors = []
        for e in interiors_info:
            target = interior_scene_res.get(e["file"])
            if not target:
                continue
            for d in e.get("doors", []):
                meta = {"target_scene": string(target), "target_name": string(e["name"]),
                        "door_id": string(d.get("door_id")),
                        "arrival_position": vec3(to_godot(d.get("arrival_position", [0, 0, 0]))),
                        "arrival_yaw": num(-(d.get('arrival_rotation') or [0, 0, 0])[2])}
                item = {"name": f"Door_{d.get('door_id')}_to_{e['name']}",
                        "pos": d.get("door_position", [0, 0, 0]), "meta": meta}
                if d.get("from") == "exterior":
                    world_doors.append(item)
                else:
                    per_parent.setdefault(d.get("from"), []).append(item)

        for e in interiors_info:
            target = interior_scene_res.get(e["file"])
            if not target:
                continue
            g = gltf_file(base / "models" / "world" / e["file"])
            build_interior_scene(base / "scenes" / "interiors" / (Path(e["file"]).name + ".tscn"),
                                 res, Path(e["file"]).name,
                                 f"{res}/{g.relative_to(base).as_posix()}", e,
                                 per_parent.get(e["name"], []), cam_script)

        if world_doors:
            parent = "ExteriorRoot" if ext_gltf else "."
            world.node("Doors", "Node3D", parent)
            dparent = "ExteriorRoot/Doors" if ext_gltf else "Doors"
            for d in world_doors:
                world.node(node_name(d["name"], used_doors), "Marker3D", dparent,
                           props={"transform": transform(to_godot(d["pos"]))}, meta=d["meta"])

    # interieur seul (pas d'exterieur) : il devient le contenu de world.tscn
    if args.export and ext_gltf is None and interior_scene_res and interiors_info:
        first = interiors_info[0]
        g = gltf_file(base / "models" / "world" / first["file"])
        if g is not None:
            mid = world.ext_resource("PackedScene", f"{res}/{g.relative_to(base).as_posix()}")
            world.node("Interior", None, ".", instance=mid)
            d0 = (first.get("doors") or [{}])[0]
            p0 = to_godot(d0.get("arrival_position", [0, 0, 0]))
            spawn = (p0[0], p0[1] + 1.7, p0[2])

    # ---------- Farlands ----------
    lod_chunks = 0
    if args.lod:
        lsrc = Path(args.lod)
        copy_assets(lsrc, base / "models" / "lod")
        lman_path = lsrc / "manifest.json"
        lman = json.loads(lman_path.read_text(encoding="utf-8")) if lman_path.is_file() else {}
        manifest_out["lod"] = lman
        far = Scene()
        far.node("Farlands", "Node3D")
        used = set()
        for c in lman.get("chunks", []):
            g = gltf_file(base / "models" / "lod" / Path(c["file"]).with_suffix(""))
            if g is None:
                continue
            fid = far.ext_resource("PackedScene", f"{res}/{g.relative_to(base).as_posix()}")
            far.node(node_name(c["name"], used), None, ".", instance=fid)
            lod_chunks += 1
        far.write(base / "farlands.tscn")
        if lod_chunks:
            fid = world.ext_resource("PackedScene", f"{res}/farlands.tscn")
            world.node("Farlands", None, ".", instance=fid)
            if not args.export and not args.single and lman.get("chunks"):
                c0 = lman["chunks"][len(lman["chunks"]) // 2]
                cs = lman.get("cell_size_m", 81.92)
                x1, y1, x2, y2 = c0["cells"]
                spawn = ((x1 + x2 + 1) / 2 * cs, 400.0, -(y1 + y2 + 1) / 2 * cs)

    add_camera(world, cam_id, spawn)
    world.write(base / "world.tscn")

    (base / "manifest.json").write_text(json.dumps(manifest_out, ensure_ascii=False, indent=2),
                                        encoding="utf-8")
    (out_root / "project.godot").write_text(
        'config_version=5\n\n[application]\n\nconfig/name="Morrowind to Godot"\n'
        f'run/main_scene="{res}/world.tscn"\nconfig/features=PackedStringArray("4.3")\n\n'
        '[rendering]\n\nenvironment/defaults/default_clear_color=Color(0.5, 0.6, 0.75, 1)\n',
        encoding="utf-8")

    print(f"[OK] Godot folder: {base}")
    print(f"     world.tscn | {len(interior_scene_res)} interior scene(s) | {lod_chunks} farland chunk(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

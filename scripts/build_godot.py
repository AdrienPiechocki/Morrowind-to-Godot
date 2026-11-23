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
import os
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


def fix_alpha_materials(models):
    """Harmonise le mode alpha d'une meme texture entre les meshes.

    Selon le NIF, une meme texture a canal alpha (tapisseries, bordures de tapis, paille, feuillages...) est
    exportee en BLEND/MASK sur certains meshes et en OPAQUE sur d'autres (propriete alpha absente ou non
    heritee) : sur ceux-la, les zones transparentes ressortent en aplats opaques (« tapis mal textures »).
    Si une texture a >2 % de pixels transparents et sert deja en BLEND/MASK ailleurs, ses materiaux OPAQUE
    passent dans ce mode (BLEND s'il existe, sinon MASK), en double face."""
    import collections
    import numpy as np
    from PIL import Image
    docs, modes = {}, collections.defaultdict(set)
    for f in Path(models).rglob("*.gltf"):
        g = json.loads(f.read_text(encoding="utf-8"))
        docs[f] = g
        for m in g.get("materials", []):
            bt = m.get("pbrMetallicRoughness", {}).get("baseColorTexture")
            if bt:
                uri = g["images"][g["textures"][bt["index"]]["source"]]["uri"]
                modes[os.path.normpath(os.path.join(f.parent, uri))].add(m.get("alphaMode", "OPAQUE"))
    upgrade = {}
    for tex, ms in modes.items():
        if "OPAQUE" in ms and ms & {"BLEND", "MASK"}:
            try:
                alpha = np.asarray(Image.open(tex).convert("RGBA"))[..., 3]
            except (OSError, ValueError):
                continue
            if float((alpha < 250).mean()) > 0.02:
                upgrade[tex] = "BLEND" if "BLEND" in ms else "MASK"
    if not upgrade:
        return 0
    fixed = 0
    for f, g in docs.items():
        changed = False
        for m in g.get("materials", []):
            bt = m.get("pbrMetallicRoughness", {}).get("baseColorTexture")
            if not bt or m.get("alphaMode", "OPAQUE") != "OPAQUE":
                continue
            uri = g["images"][g["textures"][bt["index"]]["source"]]["uri"]
            mode = upgrade.get(os.path.normpath(os.path.join(f.parent, uri)))
            if mode:
                m["alphaMode"] = mode
                m["doubleSided"] = True
                if mode == "MASK":
                    m["alphaCutoff"] = 0.28
                changed = True
                fixed += 1
        if changed:
            f.write_text(json.dumps(g, separators=(",", ":")), encoding="utf-8")
    return fixed


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
        if f.is_file() and (f.suffix.lower() in ASSET_EXT or f.name.endswith(".inst.json")):
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

    def node(self, name, ntype=None, parent=None, instance=None, props=None, meta=None, groups=None):
        head = f'[node name="{name}"'
        if ntype:
            head += f' type="{ntype}"'
        if parent is not None:
            head += f' parent="{parent}"'
        if instance is not None:
            head += f' instance=ExtResource("{instance}")'
        if groups:
            head += " groups=[" + ", ".join(f'"{g}"' for g in groups) + "]"
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

FX_SRC = Path(__file__).resolve().parent / "godot_fx"
FX = {"on": False, "res": "", "tex": {}}      # effets (equivalents de Rafael's Shader Pack)


def fx_env_props(sun):
    """Reglages d'Environment : tonemap filmique + bloom (tonemap.omwfx), SSAO (HBAO), brouillard
    volumetrique (godrays), brouillard atmospherique. Valeurs de depart tirees des defauts du pack."""
    p = {
        "tonemap_mode": "2",                       # filmique
        "tonemap_exposure": "1.0",
        "glow_enabled": "true",                    # bloom
        "glow_intensity": "0.45",
        "glow_bloom": "0.04",
        "glow_hdr_threshold": "0.85",
        "adjustment_enabled": "true",              # "special contrast / saturation"
        "adjustment_contrast": "1.07",
        "adjustment_saturation": "1.10",
        "ssao_enabled": "true",                    # HBAO / GTAO
        "ssao_radius": "2.2",
        "ssao_intensity": "1.4",
        "ssao_power": "1.6",
        "ssao_detail": "0.6",
        "ssao_light_affect": "0.15",
    }
    if sun:
        p.update({
            "volumetric_fog_enabled": "true",      # rayons de soleil (godrays)
            "volumetric_fog_density": "0.0006",
            "volumetric_fog_albedo": "Color(1, 0.85, 0.65, 1)",
            "volumetric_fog_anisotropy": "0.75",
            "volumetric_fog_length": "220.0",
            "volumetric_fog_sky_affect": "0.0",
            "fog_aerial_perspective": "0.25",
            "fog_sky_affect": "0.35",
        })
    return p


def add_environment(sc, sun=True, ambient=0.5):
    if FX["on"] and sun:
        res = FX["res"]
        sh = sc.ext_resource("Shader", f"{res}/shaders/sky.gdshader")
        params = {"shader": f'ExtResource("{sh}")'}
        for pname, key in (("cloud_noise", "perlin2d"), ("star_map", "starmap"),
                           ("masser_tex", "masser"), ("secunda_tex", "secunda")):
            if key in FX["tex"]:
                t = sc.ext_resource("Texture2D", f"{res}/fx/{FX['tex'][key]}")
                params[f"shader_parameter/{pname}"] = f'ExtResource("{t}")'
        sky_mat = sc.sub("ShaderMaterial", params)
        sky = sc.sub("Sky", {"sky_material": f'SubResource("{sky_mat}")', "process_mode": "3",
                             "radiance_size": "3"})
    else:
        sky_mat = sc.sub("ProceduralSkyMaterial", {})
        sky = sc.sub("Sky", {"sky_material": f'SubResource("{sky_mat}")'})
    props = {
        "background_mode": "2",
        "sky": f'SubResource("{sky}")',
        "ambient_light_source": "3" if sun else "2",
        "ambient_light_color": "Color(0.75, 0.78, 0.85, 1)",
        "ambient_light_energy": f"{ambient}",
        "tonemap_mode": "2",
        "fog_enabled": "true" if sun else "false",
        "fog_light_color": "Color(0.62, 0.68, 0.76, 1)",
        "fog_density": "0.0003",
    }
    if FX["on"]:
        props.update(fx_env_props(sun))
    env = sc.sub("Environment", props)
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


def fog_distance(manifest_out, override):
    """Distance de visibilite (m) : la brume cache les bords de la carte exportee.
    Auto : 90 % de la distance du centre de la grille detaillee au bord le plus proche de la zone totale."""
    if override > 0:
        return override
    ext = (manifest_out.get("world") or {}).get("exterior") or {}
    lod = manifest_out.get("lod") or {}
    cell = lod.get("cell_size_m") or 81.92
    boxes = []
    if ext.get("grid"):
        boxes.append(ext["grid"])
    boxes += [c["cells"] for c in lod.get("chunks", []) if c.get("cells")]
    if not boxes:
        return 0.0
    x1 = min(b[0] for b in boxes)
    y1 = min(b[1] for b in boxes)
    x2 = max(b[2] for b in boxes) + 1
    y2 = max(b[3] for b in boxes) + 1
    g = ext.get("grid") or [(x1 + x2 - 1) / 2, (y1 + y2 - 1) / 2] * 2
    cx, cy = (g[0] + g[2] + 1) / 2, (g[1] + g[3] + 1) / 2
    edge = min(cx - x1, x2 - cx, cy - y1, y2 - cy) * cell
    return round(max(edge * 0.9, 150.0), 1)


def add_fx_nodes(sc, outdoor):
    """Noeuds de scripts d'effets : post-traitement et sous-marin partout ; cycle jour/nuit et meteo en exterieur."""
    if not FX["on"]:
        return
    res = FX["res"]

    def script(name):
        return f'ExtResource("{sc.ext_resource("Script", f"{res}/scripts/{name}.gd")}")'
    sc.node("PostFX", "Node", ".", props={"script": script("postfx")})
    # Shader et bruit fournis par la scene (exports) : un load() a chemin construit n'est pas transmis aux
    # autres joueurs en multijoueur (CyberRealm), un export l'est.
    uw_sh = sc.ext_resource("Shader", f"{res}/shaders/underwater.gdshader")
    uw_props = {"script": script("underwater"), "shader": f'ExtResource("{uw_sh}")'}
    noise = FX["tex"].get("perlin2d")
    if noise:
        uw_props["noise_tex"] = f'ExtResource("{sc.ext_resource("Texture2D", f"{res}/fx/{noise}")}")'
    sc.node("Underwater", "Node", ".", props=uw_props)
    if outdoor:
        sc.node("DayNight", "Node", ".", props={
            "script": script("daynight"), "sun": 'NodePath("../Sun")',
            "world_environment": 'NodePath("../WorldEnvironment")',
            "view_distance": "@@VIEWDIST@@"})
        sc.node("Weather", "Node3D", ".", props={"script": script("weather")})
        sc.node("SunGlare", "Node", ".", props={"script": script("sun_glare")})


def find_shader_pack(arg):
    """Dossier de Rafael's Shader Pack : --shader-pack, config.json ("shader_pack"), puis emplacements usuels."""
    cands = []
    if arg:
        cands.append(arg)
    cfg = Path(__file__).resolve().parent.parent / "config.json"
    try:
        v = json.loads(cfg.read_text(encoding="utf-8")).get("shader_pack")
        if v:
            cands.append(v)
    except (OSError, ValueError):
        pass
    for d in ("~/Téléchargements", "~/Downloads"):
        cands.append(f"{d}/RafaelsShaderPack")
    for c in cands:
        p = Path(c).expanduser()
        if (p / "Textures").is_dir():
            return p
    return None


RM = {"on": False}
RM_CELL = (2, 8)            # cell du cratere du Mont Ecarlate (x=20848, y=69177)


def find_red_mountain(arg):
    """Dossier du mod "Red Mountain" : --red-mountain, config.json ("red_mountain"), emplacements usuels."""
    cands = []
    if arg:
        cands.append(arg)
    cfg = Path(__file__).resolve().parent.parent / "config.json"
    try:
        v = json.loads(cfg.read_text(encoding="utf-8")).get("red_mountain")
        if v:
            cands.append(v)
    except (OSError, ValueError):
        pass
    for d in ("~/Téléchargements", "~/Downloads"):
        cands.append(f"{d}/Red Mountain")
    for c in cands:
        p = Path(c).expanduser()
        if (p / "textures" / "red-mountain").is_dir():
            return p
    return None


def install_red_mountain(base, src):
    """Panache + impostor du Mont Ecarlate : scripts/shaders originaux + textures du mod (copiees ici)."""
    (base / "scripts").mkdir(parents=True, exist_ok=True)
    (base / "shaders").mkdir(parents=True, exist_ok=True)
    shutil.copy2(FX_SRC / "redmountain.gd", base / "scripts" / "redmountain.gd")
    for f in ("redmountain_plume.gdshader", "redmountain_mountain.gdshader"):
        shutil.copy2(FX_SRC / f, base / "shaders" / f)
    dst = base / "fx" / "red-mountain"
    dst.mkdir(parents=True, exist_ok=True)
    for f in (src / "textures" / "red-mountain").glob("*.png"):
        shutil.copy2(f, dst / f.name)
    bn = src / "textures" / "LDR_RGBA_0.png"
    if bn.is_file():
        shutil.copy2(bn, base / "fx" / "redmountain_bluenoise.png")
    for lic in src.glob("LICEN*"):
        shutil.copy2(lic, base / "fx" / f"LICENSE-RedMountain-{lic.name}")
    RM["on"] = True


def make_cloud_noise(size=256, seed=7):
    """Bruit nuageux sans raccord (repli si le pack n'est pas installe)."""
    import numpy as np
    rng = np.random.default_rng(seed)
    f = np.fft.fft2(rng.standard_normal((size, size)))
    fy = np.fft.fftfreq(size)[:, None]
    fx = np.fft.fftfreq(size)[None, :]
    r = np.sqrt(fx * fx + fy * fy)
    f *= 1.0 / (r + 0.01) ** 1.6
    h = np.real(np.fft.ifft2(f))
    h = (h - h.min()) / (h.max() - h.min())
    return (h * 255).astype(np.uint8)


def install_fx(base, res, pack):
    """Copie scripts/shaders d'effets et textures du pack (si present) dans le dossier genere."""
    from PIL import Image
    (base / "scripts").mkdir(parents=True, exist_ok=True)
    (base / "shaders").mkdir(parents=True, exist_ok=True)
    (base / "fx").mkdir(parents=True, exist_ok=True)
    for f in ("daynight.gd", "underwater.gd", "weather.gd", "postfx.gd", "sun_glare.gd"):
        shutil.copy2(FX_SRC / f, base / "scripts" / f)
    for f in ("sky.gdshader", "underwater.gdshader"):
        shutil.copy2(FX_SRC / f, base / "shaders" / f)
    tex = {}
    if pack is not None:
        T = pack / "Textures"

        def put(src, key, dst=None, convert=False):
            p = T / src
            if not p.is_file():
                return
            dst = dst or src
            if convert:
                Image.open(p).convert("RGBA").save(base / "fx" / dst)
            else:
                shutil.copy2(p, base / "fx" / dst)
            tex[key] = dst
        put("perlin2d.png", "perlin2d")
        put("vaiostarmap.png", "starmap")
        put("vaiomoonmasser.jpg", "masser")
        put("vaiomoonsecunda.jpg", "secunda")
        put("ripples.dds", "ripples", "ripples.png", convert=True)
        lic = pack / "LICENSE"
        if lic.is_file():
            shutil.copy2(lic, base / "fx" / "LICENSE-RafaelsShaderPack.txt")
        (base / "fx" / "README.txt").write_text(
            "Textures copied from Rafael's Shader Pack (GPL-3.0, see LICENSE-RafaelsShaderPack.txt).\n"
            "The shaders/scripts of this folder are original Godot equivalents of its effects.\n",
            encoding="utf-8")
    if "perlin2d" not in tex:
        Image.fromarray(make_cloud_noise(), "L").save(base / "fx" / "perlin2d.png")
        tex["perlin2d"] = "perlin2d.png"
    FX.update(on=True, res=res, tex=tex)
    return tex


WATER_SHADER = """shader_type spatial;
// Eau facon OpenMW : teinte verte/bleue selon la profondeur, ondulations animees (deux normal maps),
// reflets du ciel / du soleil (PBR), refraction du fond, bord de rive adouci.
render_mode depth_draw_always, cull_disabled, specular_schlick_ggx;

uniform vec3 shallow_color : source_color = vec3(0.24, 0.44, 0.42);
uniform vec3 deep_color : source_color = vec3(0.03, 0.14, 0.17);
uniform float clarity = 6.0;              // metres avant la teinte "profonde"
uniform float refraction_strength = 0.03;
uniform float ripple_scale = 0.05;        // frequence des ondulations (par metre)
uniform float flow_speed = 1.0;
uniform sampler2D normal_a : hint_normal, repeat_enable, filter_linear_mipmap;
uniform sampler2D normal_b : hint_normal, repeat_enable, filter_linear_mipmap;
uniform sampler2D screen_tex : hint_screen_texture, repeat_disable, filter_linear_mipmap;
uniform sampler2D depth_tex : hint_depth_texture, repeat_disable, filter_nearest;
uniform sampler2D ripple_tex : repeat_enable, filter_linear_mipmap, hint_default_black;
uniform float rain_intensity : hint_range(0.0, 1.0) = 0.0;   // pilote par weather.gd

varying vec3 world_pos;

void vertex() {
	world_pos = (MODEL_MATRIX * vec4(VERTEX, 1.0)).xyz;
}

void fragment() {
	float t_ = TIME * flow_speed;
	vec2 uv_a = world_pos.xz * ripple_scale + vec2(t_ * 0.015, t_ * 0.010);
	vec2 uv_b = world_pos.xz * ripple_scale * 2.3 + vec2(-t_ * 0.020, t_ * 0.012);
	vec3 na = texture(normal_a, uv_a).xyz * 2.0 - 1.0;
	vec3 nb = texture(normal_b, uv_b).xyz * 2.0 - 1.0;
	vec2 slope = na.xy + nb.xy * 0.6;
	if (rain_intensity > 0.01) {
		// gouttes : normales de "bulles" animees (texture de ripples), deux couches dephasees
		for (int i = 0; i < 2; i++) {
			float ph = fract(t_ * (0.9 + 0.3 * float(i)) + 0.5 * float(i));
			vec4 r = texture(ripple_tex, world_pos.xz * (0.45 + 0.17 * float(i)) + vec2(float(i) * 0.37, float(i) * 0.61));
			slope += (r.rg * 2.0 - 1.0) * r.a * sin(ph * 3.14159) * rain_intensity * 1.4;
		}
	}
	vec3 n = normalize(vec3(slope, 1.0));
	NORMAL_MAP = n * 0.5 + 0.5;
	float far_fade = smoothstep(60.0, 500.0, length(VERTEX));    // ondulations atténuées au loin : pas de scintillement
	NORMAL_MAP_DEPTH = mix(1.0, 0.12, far_fade);

	// profondeur d'eau sous la surface
	float d = texture(depth_tex, SCREEN_UV).x;
	vec3 ndc = vec3(SCREEN_UV * 2.0 - 1.0, d);
	vec4 view = INV_PROJECTION_MATRIX * vec4(ndc, 1.0);
	view.xyz /= view.w;
	float depth_diff = max(-view.z + VERTEX.z, 0.0);
	float t = 1.0 - exp(-depth_diff / clarity);

	// fond refracte (attenue pres du bord pour eviter les artefacts)
	vec2 offset = n.xy * refraction_strength * clamp(depth_diff, 0.0, 1.0);
	vec3 refracted = textureLod(screen_tex, SCREEN_UV + offset, 0.0).rgb;

	float tint = clamp(0.30 + 0.70 * t, 0.0, 1.0);
	ALBEDO = mix(shallow_color, deep_color, t) * tint;
	EMISSION = refracted * (1.0 - tint);
	ROUGHNESS = mix(0.06, 0.18, far_fade);
	METALLIC = 0.0;
	SPECULAR = 0.6;
	ALPHA = smoothstep(0.0, 0.2, depth_diff);
}
"""


def make_normal_map(seed, size=256, strength=3.0, cutoff=0.18):
    """Normal map ondulee et sans raccord (bruit filtre dans l'espace de Fourier)."""
    import numpy as np
    rng = np.random.default_rng(seed)
    f = np.fft.fft2(rng.standard_normal((size, size)))
    fy = np.fft.fftfreq(size)[:, None]
    fx = np.fft.fftfreq(size)[None, :]
    r = np.sqrt(fx * fx + fy * fy)
    f *= np.exp(-(r / cutoff) ** 2) / (r + 0.02)           # passe-bas, plus d'energie aux grandes ondes
    h = np.real(np.fft.ifft2(f))
    h = (h - h.mean()) / (h.std() + 1e-9)
    dx = (np.roll(h, -1, axis=1) - np.roll(h, 1, axis=1)) * 0.5
    dy = (np.roll(h, -1, axis=0) - np.roll(h, 1, axis=0)) * 0.5
    n = np.stack([-dx * strength, -dy * strength, np.ones_like(h)], axis=-1)
    n /= np.linalg.norm(n, axis=-1, keepdims=True)
    return ((n * 0.5 + 0.5) * 255 + 0.5).astype(np.uint8)


def write_water_assets(base):
    """shaders/water.gdshader + textures/water_nm_{a,b}.png dans le dossier genere."""
    from PIL import Image
    (base / "shaders").mkdir(parents=True, exist_ok=True)
    (base / "shaders" / "water.gdshader").write_text(WATER_SHADER, encoding="utf-8")
    (base / "textures").mkdir(parents=True, exist_ok=True)
    Image.fromarray(make_normal_map(1, strength=1.5, cutoff=0.09), "RGB").save(base / "textures" / "water_nm_a.png")
    Image.fromarray(make_normal_map(2, size=256, strength=1.8, cutoff=0.13), "RGB").save(
        base / "textures" / "water_nm_b.png")


def water_material(sc, res):
    """Sous-ressource ShaderMaterial de l'eau (les scenes partagent les memes fichiers)."""
    # Shader embarque dans la scene (pas de dependance externe) : necessaire pour le transfert multijoueur.
    sh = sc.sub("Shader", {"code": string(WATER_SHADER)})
    na = sc.ext_resource("Texture2D", f"{res}/textures/water_nm_a.png")
    nb = sc.ext_resource("Texture2D", f"{res}/textures/water_nm_b.png")
    params = {
        "shader": f'SubResource("{sh}")',
        "shader_parameter/normal_a": f'ExtResource("{na}")',
        "shader_parameter/normal_b": f'ExtResource("{nb}")'}
    if FX["on"] and "ripples" in FX["tex"]:
        rt = sc.ext_resource("Texture2D", f"{res}/fx/{FX['tex']['ripples']}")
        params["shader_parameter/ripple_tex"] = f'ExtResource("{rt}")'
    return sc.sub("ShaderMaterial", params)


def add_water_plane(sc, mat_id, name, parent, center, size):
    """Plan d'eau : center = (x, y, z) Godot, size = (largeur x, profondeur z) en metres."""
    plane = sc.sub("PlaneMesh", {"size": f"Vector2({num(size[0], 3)}, {num(size[1], 3)})",
                                 "material": f'SubResource("{mat_id}")'})
    sc.node(name, "MeshInstance3D", parent, props={
        "transform": transform(center), "mesh": f'SubResource("{plane}")', "cast_shadow": "0"},
        groups=["water"])


def add_cell_water(sc, mat_id, parent, cells, cell_size):
    """cells : [[gx, gy, niveau_m]] ; un seul PlaneMesh partage par toutes les cells."""
    plane = sc.sub("PlaneMesh", {"size": f"Vector2({num(cell_size, 3)}, {num(cell_size, 3)})",
                                 "material": f'SubResource("{mat_id}")'})
    for gx, gy, level in cells:
        sc.node(f"W_{gx}_{gy}".replace("-", "m"), "MeshInstance3D", parent, props={
            "transform": transform(((gx + 0.5) * cell_size, level, -(gy + 0.5) * cell_size)),
            "mesh": f'SubResource("{plane}")', "cast_shadow": "0"}, groups=["water"])


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
    add_fx_nodes(sc, False)
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
                    props={"transform": transform(to_godot(d["pos"]), d.get("yaw", 0.0))}, meta=d["meta"])
    water = entry.get("water")
    if water:
        wm = water_material(sc, res)
        sc.node("Water", "Node3D", ".")
        x0, y0, x1, y1 = water["bounds"]
        add_water_plane(sc, wm, "WaterSurface", "Water",
                        ((x0 + x1) / 2, water["level"], -(y0 + y1) / 2), (x1 - x0, y1 - y0))
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
    ap.add_argument("--shader-pack", help="dossier de Rafael's Shader Pack (defaut : config.json 'shader_pack', "
                                          "puis ~/Telechargements ou ~/Downloads)")
    ap.add_argument("--red-mountain", help="dossier du mod Red Mountain (defaut : config.json 'red_mountain', "
                                           "puis ~/Telechargements ou ~/Downloads)")
    ap.add_argument("--no-red-mountain", action="store_true", help="pas de panache / impostor du Mont Ecarlate")
    ap.add_argument("--fog-distance", type=float, default=0.0,
                    help="distance (m) ou tout disparait dans la brume ; 0 = auto (selon l'etendue exportee)")
    ap.add_argument("--no-fx", action="store_true", help="pas de ciel/meteo/post-traitement (eau et scenes seules)")
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
    write_water_assets(base)
    FX["on"] = False
    if not args.no_fx:
        pack = find_shader_pack(args.shader_pack)
        tex = install_fx(base, res, pack)
        RM["on"] = False
        if not args.no_red_mountain:
            rm = find_red_mountain(args.red_mountain)
            if rm is not None:
                install_red_mountain(base, rm)
                print(f"[+] Red Mountain: {rm}")
        print(f"[+] Shader effects: {'Rafael' + chr(39) + 's Shader Pack textures from ' + str(pack) if pack else 'pack not found, generic textures'}"
              f" ({', '.join(sorted(tex))})")

    world = Scene()
    cam_id = world.ext_resource("Script", cam_script)
    world.node("World", "Node3D")
    add_environment(world, sun=True)
    add_fx_nodes(world, True)
    spawn = (0.0, 50.0, 0.0)
    manifest_out = {}

    # ---------- Exterieur / modele du monde ----------
    world_water = []          # [[gx, gy, niveau_m]] : exterieur + farlands
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
            world_water.extend((manifest.get("exterior") or {}).get("water") or [])
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
                # Marker3D = point de la porte de retour (devant la porte, cote parent), comme le DoorMarker de
                # Morrowind ; repli : origine du mesh de la porte. door_position garde l'ancienne position.
                mpos = d.get("marker_position") or d.get("door_position", [0, 0, 0])
                meta["door_position"] = vec3(to_godot(d.get("door_position", [0, 0, 0])))
                if d.get("marker_rotation"):
                    meta["marker_yaw"] = num(-d["marker_rotation"][2])
                item = {"name": f"Door_{d.get('door_id')}_to_{e['name']}",
                        "pos": mpos, "meta": meta}
                if d.get("marker_rotation"):
                    item["yaw"] = -d["marker_rotation"][2]
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
                           props={"transform": transform(to_godot(d["pos"]), d.get("yaw", 0.0))}, meta=d["meta"])

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
        shutil.copy2(FX_SRC / "farlands.gd", base / "scripts" / "farlands.gd")
        far_script = far.ext_resource("Script", f"{res}/scripts/farlands.gd")
        far.node("Farlands", "Node3D", props={"script": f'ExtResource("{far_script}")'})
        used = set()
        for c in lman.get("chunks", []):
            g = gltf_file(base / "models" / "lod" / Path(c["file"]).with_suffix(""))
            if g is None:
                continue
            fid = far.ext_resource("PackedScene", f"{res}/{g.relative_to(base).as_posix()}")
            far.node(node_name(c["name"], used), None, ".", instance=fid)
            lod_chunks += 1
            world_water.extend(c.get("water") or [])
        far.write(base / "farlands.tscn")
        if lod_chunks:
            fid = world.ext_resource("PackedScene", f"{res}/farlands.tscn")
            world.node("Farlands", None, ".", instance=fid)
            if not args.export and not args.single and lman.get("chunks"):
                c0 = lman["chunks"][len(lman["chunks"]) // 2]
                cs = lman.get("cell_size_m", 81.92)
                x1, y1, x2, y2 = c0["cells"]
                spawn = ((x1 + x2 + 1) / 2 * cs, 400.0, -(y1 + y2 + 1) / 2 * cs)

    if world_water:
        wm = water_material(world, res)
        world.node("Water", "Node3D", ".")
        cell_m = (manifest_out.get("lod") or {}).get("cell_size_m") or 81.92
        add_cell_water(world, wm, "Water", world_water, cell_m)

    if FX["on"] and RM["on"]:
        # le vrai volcan est-il dans la scene (grille detaillee ou farlands) ? sinon : impostor
        gx, gy = RM_CELL

        def covers(b):
            return b and b[0] <= gx <= b[2] and b[1] <= gy <= b[3]
        real = covers((manifest_out.get("world") or {}).get("exterior", {}).get("grid")) or any(
            covers(c.get("cells")) for c in (manifest_out.get("lod") or {}).get("chunks", []))
        rs = world.ext_resource("Script", f"{res}/scripts/redmountain.gd")
        rm_props = {
            "script": f'ExtResource("{rs}")',
            "show_impostor": "false" if real else "true",
            "daynight": 'NodePath("../DayNight")'}

        def rm_res(rtype, rel, prop):
            if (base / rel).is_file():
                rm_props[prop] = f'ExtResource("{world.ext_resource(rtype, f"{res}/{rel}")}")'
        # ressources fournies par la scene (exports) : transmises aux autres joueurs en multijoueur
        rm_res("Shader", "shaders/redmountain_plume.gdshader", "plume_shader")
        rm_res("Shader", "shaders/redmountain_mountain.gdshader", "mountain_shader")
        rm_res("Texture2D", "fx/redmountain_bluenoise.png", "blue_noise")
        for i, nm in enumerate(("0-15", "16-31", "32-47", "48-63")):
            rm_res("Texture2D", f"fx/red-mountain/{nm}.png", f"atlas{i}")
        world.node("RedMountain", "Node3D", ".", props=rm_props)
        print(f"[+] Red Mountain at cell {RM_CELL}: {'real terrain in scene, no impostor' if real else 'impostor'}")

    # brume epaisse : cache les bords de la carte exportee (valeur connue seulement maintenant)
    vd = fog_distance(manifest_out, args.fog_distance)
    world.nodes = [n.replace("@@VIEWDIST@@", f"{vd}") for n in world.nodes]
    if FX["on"]:
        print(f"[+] Fog: everything fades out at {vd:.0f} m" if vd else "[+] Fog: default (no extent known)")

    n_alpha = fix_alpha_materials(base / "models")
    if n_alpha:
        print(f"[+] Alpha: {n_alpha} opaque material(s) switched to the alpha mode their texture uses elsewhere")
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

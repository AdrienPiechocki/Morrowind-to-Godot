#!/usr/bin/env python3
"""Adapte le dossier Godot genere (godot_export/<nom>) a CyberRealm : niveau jouable avec portes, joueur,
options de niveau dans le menu pause et multijoueur LAN.

    python3 scripts/cyberrealm_export.py [--src godot_export/morrowind] [--dest <CyberRealm>/Game/source/user]

Produit dans `--dest` (le dossier `user/` du projet CyberRealm) :
  level.tscn                    niveau (monde + joueur + 80 interieurs ranges sur une grille haute + options)
  Levels/<Nom>/                 assets (models, textures, fx, shaders, farlands.tscn), scripts d'effets
                                et de jeu (scripts/cyberrealm/ -> interiors.gd, door.gd, level_options.gd...),
                                scenes d'interieur nettoyees (interiors/*.tscn)

Les chemins `res://morrowind/...` deviennent `res://user/Levels/<Nom>/...`. Les scripts de jeu s'appuient sur le
menu pause et le LevelBaker de CyberRealm (voir Game/source/user/README.md de CyberRealm). Apres l'export,
relancer l'import Godot : `godot --headless --import --path Game/source`.
"""
import argparse
import glob
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GAME_SCRIPTS = Path(__file__).resolve().parent / "cyberrealm"


def rd(p):
    return Path(p).read_text(encoding="utf-8")


def wr(p, t):
    Path(p).write_text(t, encoding="utf-8")


def copy_tree(src, dst):
    """Copie recursive sans les fichiers .import (Godot les regenere a l'import)."""
    if shutil.which("rsync"):
        subprocess.check_call(["rsync", "-a", "--exclude", "*.import", f"{src}/", f"{dst}/"])
    else:
        shutil.copytree(src, dst, ignore=shutil.ignore_patterns("*.import"), dirs_exist_ok=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--src", default=str(ROOT / "godot_export" / "morrowind"),
                    help="dossier genere par build_godot.py (defaut : godot_export/morrowind)")
    ap.add_argument("--dest", default=os.path.expanduser("~/Projets/CyberRealm/Game/source/user"),
                    help="dossier user/ du projet CyberRealm")
    ap.add_argument("--name", default="Morrowind", help="nom du dossier sous user/Levels/")
    args = ap.parse_args()

    SRC, USER = Path(args.src), Path(args.dest)
    if not (SRC / "world.tscn").is_file():
        print(f"[ERROR] {SRC}/world.tscn introuvable : lancer build_godot.py / run.sh d'abord", file=sys.stderr)
        return 1
    HERE = USER / "Levels" / args.name
    OLD = f"res://{SRC.name}/"
    P = f"res://user/Levels/{args.name}/"
    HERE.mkdir(parents=True, exist_ok=True)

    # ── 1. Assets ─────────────────────────────────────────────────────
    for d in ("models", "textures", "fx", "shaders", "interiors", "scripts"):
        shutil.rmtree(HERE / d, ignore_errors=True)
    for d in ("models", "textures", "fx", "shaders", "scripts"):
        if (SRC / d).is_dir():
            copy_tree(SRC / d, HERE / d)
    if (SRC / "farlands.tscn").is_file():
        shutil.copy(SRC / "farlands.tscn", HERE / "farlands.tscn")
        wr(HERE / "farlands.tscn", rd(HERE / "farlands.tscn").replace(OLD, P))
    for f in glob.glob(f"{HERE}/shaders/*.gdshader") + glob.glob(f"{HERE}/scripts/*.gd"):
        wr(f, rd(f).replace(OLD, P))
    if (HERE / "scripts" / "fly_cam.gd").exists():
        (HERE / "scripts" / "fly_cam.gd").unlink()
    for f in GAME_SCRIPTS.glob("*.gd"):                 # scripts de jeu (portes, options, environnement)
        shutil.copy(f, HERE / "scripts" / f.name)

    # ── 2. Interieurs nettoyes ────────────────────────────────────────
    # Plus de camera libre / post-traitement / sous-marin (le niveau a les siens) ; le WorldEnvironment devient
    # un noeud porteur d'Environment (interior_env.gd) ; le shader d'eau est embarque (transfert LAN).
    water_code = rd(HERE / "shaders" / "water.gdshader")
    water_sub = '[sub_resource type="Shader" id="water_shader"]\ncode = "%s"\n\n' % \
        water_code.replace("\\", "\\\\").replace('"', '\\"')

    def inline_water(t):
        m = re.search(r'\[ext_resource type="Shader" path="[^"]*/water\.gdshader" id="([^"]+)"\]\n\n?', t)
        if not m:
            return t
        t = t.replace(m.group(0), "", 1).replace('ExtResource("%s")' % m.group(1), 'SubResource("water_shader")')
        k = t.index("[sub_resource")
        return t[:k] + water_sub + t[k:]

    def drop_unused_ext(t):
        for m in list(re.finditer(r'\[ext_resource [^\n]*id="([^"]+)"\]\n\n?', t)):
            if 'ExtResource("%s")' % m.group(1) not in t.replace(m.group(0), ""):
                t = t.replace(m.group(0), "", 1)
        return t

    (HERE / "interiors").mkdir(exist_ok=True)
    names = []
    for f in sorted(glob.glob(f"{SRC}/scenes/interiors/*.tscn")):
        t = rd(f).replace(OLD, P)
        t = re.sub(r"\[gd_scene load_steps=\d+ ", "[gd_scene ", t)
        out = []
        for b in re.split(r"(?m)^(?=\[(?:node|ext_resource|sub_resource|connection)\b)", t):
            if re.match(r'\[node name="(FlyCamera|PostFX|Underwater)"', b):
                continue
            if b.startswith('[node name="WorldEnvironment"'):
                b = b.replace('type="WorldEnvironment"', 'type="Node"', 1)
                b = re.sub(r"(\[node [^\]]*\]\n)", r'\1script = ExtResource("envscript")\n', b, count=1)
            out.append(b)
        t = "".join(out)
        t = t.replace("\n[sub_resource", '\n[ext_resource type="Script" path="%sscripts/interior_env.gd" id="envscript"]\n\n[sub_resource' % P, 1)
        assert t.count('id="envscript"') == 1, f
        t = drop_unused_ext(inline_water(t))
        wr(HERE / "interiors" / os.path.basename(f), t)
        names.append(os.path.basename(f)[:-5])

    # ── 3. level.tscn ─────────────────────────────────────────────────
    s = rd(SRC / "world.tscn").replace(OLD, P)
    s = re.sub(r"\[gd_scene load_steps=\d+ ", "[gd_scene ", s)
    s = re.sub(r'\[ext_resource type="Script" path="[^"]*fly_cam\.gd" id="[^"]*"\]\n\n?', "", s)
    k = s.index('[node name="FlyCamera"')
    m = re.search(r"transform = Transform3D\([^)]*?,\s*([-\d.e]+),\s*([-\d.e]+),\s*([-\d.e]+)\)", s[k:])
    spawn = ", ".join(m.groups()) if m else "0, 60, 0"       # position de l'ancienne camera libre
    s = s[:k].rstrip() + "\n"
    s = s.replace('[node name="World" type="Node3D"]', '[node name="Level" type="Node3D"]')
    s = inline_water(s)

    ext = ['[ext_resource type="PackedScene" path="res://scenes/player.tscn" id="player"]\n']

    def e(rtype, path, i):
        ext.append('[ext_resource type="%s" path="%s%s" id="%s"]\n' % (rtype, P, path, i))
    e("Script", "scripts/interiors.gd", "interiors_gd")
    e("Script", "scripts/level_options.gd", "lopt_gd")
    e("Script", "scripts/level_environment.gd", "lenv_gd")
    for i, n in enumerate(names):
        e("PackedScene", f"interiors/{n}.tscn", f"int{i}")
    last = [mm for mm in re.finditer(r"\[ext_resource[^\n]*\]\n", s)][-1].end()
    s = s[:last] + "\n" + "\n".join(ext) + s[last:]

    s = s.rstrip() + ('\n\n[node name="Player" parent="." instance=ExtResource("player")]\n'
                      'transform = Transform3D(1, 0, 0, 0, 1, 0, 0, 0, 1, %s)\nMaxDepth = 1\n\n'
                      '[node name="Camera3D" parent="Player" index="0"]\nfar = 20000.0\n') % spawn
    # Reglages d'environnement du niveau, copies de son Environment (voir level_environment.gd).
    m = re.search(r'\[sub_resource type="Environment" id="[^"]*"\]\n(.*?)(?=\n\[|\Z)', s, re.S)
    env_props = {}
    for line in (m.group(1) if m else "").splitlines():
        key, _, v = line.partition(" = ")
        if key in ("ssr_enabled", "ssao_enabled", "ssil_enabled", "sdfgi_enabled", "glow_enabled", "fog_enabled",
                   "volumetric_fog_enabled", "adjustment_enabled", "adjustment_brightness", "adjustment_contrast",
                   "adjustment_saturation"):
            env_props[key] = v
    s += '\n[node name="LevelEnvironment" type="Node" parent="."]\nscript = ExtResource("lenv_gd")\n'
    s += "".join("%s = %s\n" % kv for kv in env_props.items())
    s += '\n[node name="LevelOptions" type="Node" parent="."]\nscript = ExtResource("lopt_gd")\n'
    s += '\n[node name="Interiors" type="Node3D" parent="."]\nscript = ExtResource("interiors_gd")\n'
    for i, n in enumerate(names):
        x, z = (i % 10) * 800.0, (i // 10) * 800.0
        s += ('\n[node name="%s" parent="Interiors" instance=ExtResource("int%d")]\n'
              'transform = Transform3D(1, 0, 0, 0, 1, 0, 0, 0, 1, %s, 6000, %s)\n') % (n.replace(".", "_"), i, x, z)
    wr(USER / "level.tscn", s)
    print(f"[OK] CyberRealm : {len(names)} interieurs -> {USER / 'level.tscn'} ; assets -> {HERE}")
    print("     Relancer l'import : godot --headless --import --path <CyberRealm>/Game/source")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Prépare les données de Morrowind à partir de openmw.cfg.

Python pur (pas de bpy) : lancé par run.sh avant Blender.

Sous-commandes :
  find      affiche le chemin du openmw.cfg détecté (code 1 si introuvable)
  prepare   - lit openmw.cfg (data=, data-local=, content=, fallback-archive=)
            - extrait les BSA dans le cache (dans l'ordre de priorité)
            - convertit chaque plugin de content= avec tes3conv
            - fusionne les plugins DANS L'ORDRE DE CHARGEMENT, cell par cell et
              référence par référence (un plugin n'écrase plus toute la cell)
            - écrit output.json et la liste des dossiers meshes/textures
"""
import argparse
import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

TES3_EXT = (".esm", ".esp")


def log(msg):
    print(msg, flush=True)


# ==========================================
# openmw.cfg
# ==========================================

def candidate_cfg_paths():
    home = Path.home()
    xdg = Path(os.environ.get("XDG_CONFIG_HOME", home / ".config"))
    return [
        xdg / "openmw" / "openmw.cfg",
        home / ".var/app/org.openmw.OpenMW/config/openmw/openmw.cfg",  # Flatpak
        home / "Documents/My Games/OpenMW/openmw.cfg",                  # Windows
        home / "Library/Preferences/openmw/openmw.cfg",                 # macOS
    ]


def find_cfg():
    for p in candidate_cfg_paths():
        if p.is_file():
            return p
    return None


def userdata_dir(cfg_path):
    if "org.openmw.OpenMW" in str(cfg_path):                            # Flatpak
        return Path.home() / ".var/app/org.openmw.OpenMW/data/openmw"
    if sys.platform == "win32":
        return Path.home() / "Documents/My Games/OpenMW"
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/openmw"
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "openmw"


@dataclass
class OpenMWConfig:
    data: list = field(default_factory=list)      # priorité CROISSANTE (le dernier gagne)
    content: list = field(default_factory=list)   # ordre de chargement
    archives: list = field(default_factory=list)  # priorité croissante


def _unquote(raw):
    """Valeur d'un cfg OpenMW : "..." avec '&' comme caractère d'échappement."""
    raw = raw.strip()
    if not raw.startswith('"'):
        return raw
    out, i = [], 1
    while i < len(raw) and raw[i] != '"':
        if raw[i] == "&" and i + 1 < len(raw):
            i += 1
        out.append(raw[i])
        i += 1
    return "".join(out)


def parse_cfg(cfg_path, _seen=None):
    cfg_path = Path(cfg_path).expanduser().resolve()
    seen = _seen if _seen is not None else set()
    cfg = OpenMWConfig()
    if cfg_path in seen:
        return cfg
    seen.add(cfg_path)

    cfg_dir = cfg_path.parent
    userdata = userdata_dir(cfg_path)
    local = []  # data-local= : prioritaire sur tous les data=

    for line in cfg_path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, raw = line.split("=", 1)
        key = key.strip()
        val = _unquote(raw).replace("?userdata?", str(userdata)).replace("?userconfig?", str(cfg_dir))

        if key in ("data", "data-local"):
            if "?" in val:  # ?global? / ?local? : dépend de l'installation d'OpenMW
                log(f"[!] Token non résolu, dossier ignoré : {val}")
                continue
            p = Path(val)
            if not p.is_absolute():
                p = cfg_dir / p
            (local if key == "data-local" else cfg.data).append(p)
        elif key == "content":
            cfg.content.append(val)
        elif key == "fallback-archive":
            cfg.archives.append(val)
        elif key == "replace":
            # replace=content|data|data-local|fallback-archive : vide ce qui précède
            if val == "content":
                cfg.content.clear()
            elif val == "data":
                cfg.data.clear()
            elif val == "data-local":
                local.clear()
            elif val == "fallback-archive":
                cfg.archives.clear()
        elif key == "config":
            sub = parse_cfg(Path(val) / "openmw.cfg", seen)
            cfg.data += sub.data
            cfg.content += sub.content
            cfg.archives += sub.archives

    cfg.data += local
    return cfg


# ==========================================
# Fichiers (insensible à la casse)
# ==========================================

def ci_child(directory, name):
    """directory/name en ignorant la casse (Morrowind est insensible, Linux non)."""
    directory = Path(directory)
    p = directory / name
    if p.exists():
        return p
    try:
        low = name.lower()
        for entry in os.listdir(directory):
            if entry.lower() == low:
                return directory / entry
    except OSError:
        pass
    return None


_ROOT_INDEX = {}


def _root_index(cfg):
    """{nom en minuscules: chemin} des fichiers à la racine des dossiers data.

    Construit une seule fois (1 listdir par dossier) ; le dernier dossier gagne,
    comme dans OpenMW. Évite des centaines de listdir par plugin quand openmw.cfg
    contient des centaines de dossiers data=.
    """
    key = id(cfg)
    if key not in _ROOT_INDEX:
        index = {}
        for d in cfg.data:
            try:
                for entry in os.scandir(d):
                    if entry.is_file():
                        index[entry.name.lower()] = Path(entry.path)
            except OSError:
                pass
        _ROOT_INDEX[key] = index
    return _ROOT_INDEX[key]


def find_in_data(cfg, name):
    """Cherche un fichier à la racine des dossiers data (le dernier a la priorité)."""
    return _root_index(cfg).get(name.lower())


def asset_dirs(cfg, cache, sub):
    """Dossiers <sub> (meshes, textures...) par priorité DÉCROISSANTE, cache BSA en dernier."""
    out = []
    for d in list(reversed(cfg.data)) + [Path(cache)]:
        p = ci_child(d, sub)
        if p and p.is_dir() and str(p) not in out:
            out.append(str(p))
    return out


# ==========================================
# BSA -> cache
# ==========================================

def extract_archives(cfg, cache, bsatool):
    cache = Path(cache)
    stamp_file = cache / ".cache" / "bsa_stamp.json"
    archives = []
    for name in cfg.archives:
        p = find_in_data(cfg, name)
        if p:
            archives.append(p)
        else:
            log(f"[!] Archive introuvable dans les dossiers data : {name}")

    stamp = [[str(p), p.stat().st_size, int(p.stat().st_mtime)] for p in archives]
    try:
        if json.loads(stamp_file.read_text()) == stamp:
            log("[OK] Cache BSA à jour, rien à extraire.")
            return
    except (OSError, ValueError):
        pass

    cache.mkdir(parents=True, exist_ok=True)
    # Ordre de priorité croissante : un archive plus tardif écrase les fichiers du précédent
    for p in archives:
        log(f"[..] Extraction de {p.name} vers {cache}/ ...")
        subprocess.run([bsatool, "extractall", str(p), str(cache)],
                       check=True, stdout=subprocess.DEVNULL)
    stamp_file.parent.mkdir(parents=True, exist_ok=True)
    stamp_file.write_text(json.dumps(stamp))


# ==========================================
# Plugins -> JSON
# ==========================================

def convert_plugin(plugin, cache, tes3conv):
    st = plugin.stat()
    out = Path(cache) / ".cache" / "json" / f"{plugin.name}.{st.st_size}.json"
    if out.exists() and out.stat().st_mtime >= st.st_mtime:
        return out
    out.parent.mkdir(parents=True, exist_ok=True)
    log(f"[..] tes3conv {plugin.name} ...")
    res = subprocess.run([tes3conv, str(plugin), str(out)],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if res.returncode != 0:
        # Typiquement un plugin OpenMW-only (records LUAL/LUAS...) que tes3conv ne
        # sait pas lire : sans effet sur la géométrie, on l'ignore.
        reason = (res.stderr or res.stdout or "").strip().splitlines()
        log(f"[!] Plugin ignoré (tes3conv a échoué) : {plugin.name}"
            + (f" — {reason[-1]}" if reason else ""))
        try:
            out.unlink()
        except OSError:
            pass
        return None
    return out


# ==========================================
# Fusion des plugins
# ==========================================

def _master_names(header):
    names = []
    for m in header.get("masters") or []:
        if isinstance(m, (list, tuple)):
            names.append(str(m[0]))
        elif isinstance(m, dict):
            names.append(str(m.get("name") or m.get("filename") or ""))
        else:
            names.append(str(m))
    return names


def cell_is_interior(cell):
    flags = (cell.get("data") or {}).get("flags")
    if isinstance(flags, int):
        return bool(flags & 1)
    return "INTERIOR" in str(flags).upper()


def cell_grid(cell):
    grid = (cell.get("data") or {}).get("grid") or [0, 0]
    return int(grid[0]), int(grid[1])


def cell_key(cell):
    if cell_is_interior(cell):
        return ("int", (cell.get("name") or "").lower())
    return ("ext",) + cell_grid(cell)


def merge_plugins(plugins):
    """plugins : [(nom_fichier, [records tes3conv])] dans l'ordre de chargement.

    Retourne une liste plate de records (comme l'ancien output.json) où chaque
    cell apparaît UNE fois, avec les références de tous les plugins fusionnées :
      - identité d'une référence = (plugin qui l'a créée, refr_index)
        (mast_index 0 = ce plugin, k >= 1 = k-ième master du plugin)
      - une référence redéfinie par un plugin ultérieur remplace l'ancienne
      - `moved_cell` déplace la référence vers une autre exterior cell
      - `deleted` est conservé (les consommateurs l'ignorent)
    """
    other = []        # records hors Cell/Header, dans l'ordre (le dernier gagne par id)
    cells = {}        # cell_key -> record fusionné (sans references)
    refs = {}         # ident -> [cell_key, ref]
    anon = 0
    stats = dict(overridden=0, moved=0, deleted=0, no_translation=0,
                 high_total=0, high_matched=0)

    for name, records in plugins:
        header = next((r for r in records if r.get("type") == "Header"), {})
        masters = _master_names(header)
        me = name.lower()

        for rec in records:
            rtype = rec.get("type")
            if rtype == "Header":
                continue
            if rtype != "Cell":
                other.append(rec)
                continue

            key = cell_key(rec)
            base = cells.setdefault(key, {})
            base.update({k: v for k, v in rec.items() if k != "references"})

            for ref in rec.get("references") or []:
                mi, ri = ref.get("mast_index"), ref.get("refr_index")
                if mi is None or ri is None:
                    anon += 1
                    ident = ("anon", anon)
                else:
                    if mi == 0:
                        owner = me
                    elif mi - 1 < len(masters):
                        owner = masters[mi - 1].lower()
                    else:
                        owner = f"?master{mi}"
                    ident = (owner, ri)

                target = key
                mc = ref.get("moved_cell")
                if isinstance(mc, (list, tuple)) and len(mc) == 2:
                    target = ("ext", int(mc[0]), int(mc[1]))
                    stats["moved"] += 1

                if mi:
                    stats["high_total"] += 1
                    if ident in refs:
                        stats["high_matched"] += 1

                if ident in refs:
                    stats["overridden"] += 1
                    merged = dict(refs[ident][1])
                    merged.update(ref)
                    ref = merged
                if ref.get("deleted"):
                    stats["deleted"] += 1
                refs[ident] = [target, ref]

    by_cell = {}
    for target, ref in refs.values():
        if "translation" not in ref:
            stats["no_translation"] += 1
            continue
        by_cell.setdefault(target, []).append(ref)

    out = list(other)
    for key, base in cells.items():
        rec = dict(base)
        rec["references"] = by_cell.get(key, [])
        out.append(rec)

    active = sum(1 for v in by_cell.values() for r in v if not r.get("deleted"))
    log(f"[+] Cells fusionnées : {len(cells)} | références actives : {active}")
    log(f"[+] Références redéfinies par un plugin : {stats['overridden']} | "
        f"déplacées : {stats['moved']} | supprimées : {stats['deleted']}")
    if stats["no_translation"]:
        log(f"[!] {stats['no_translation']} référence(s) sans 'translation' ignorée(s)")
    if stats["high_total"] >= 50:
        ratio = stats["high_matched"] / stats["high_total"]
        if ratio < 0.9:
            log(f"[!] Seulement {ratio:.0%} des références pointant vers un master ont été retrouvées. "
                "La sémantique de mast_index diffère peut-être de celle supposée "
                "(0 = ce plugin, k = k-ième master) : vérifie avec jq.")
    return out


# ==========================================
# Commandes
# ==========================================

def cmd_find(_args):
    p = find_cfg()
    if not p:
        return 1
    print(p)
    return 0


def cmd_prepare(args):
    cfg_path = Path(args.cfg).expanduser()
    if not cfg_path.is_file():
        log(f"[ERROR] openmw.cfg introuvable : {cfg_path}")
        return 1
    cfg = parse_cfg(cfg_path)
    cache = Path(args.cache).resolve()
    tes3conv = str(Path(args.tes3conv).resolve())
    bsatool = str(Path(args.bsatool).resolve())

    log(f"[+] openmw.cfg : {cfg_path}")
    log(f"[+] {len(cfg.data)} dossier(s) data, {len(cfg.content)} plugin(s), {len(cfg.archives)} archive(s)")
    if not cfg.data or not cfg.content:
        log("[ERROR] Aucun data= ou content= dans ce openmw.cfg (config= non résolu ?)")
        return 1

    extract_archives(cfg, cache, bsatool)

    plugins = []
    skipped = []
    for name in cfg.content:
        if not name.lower().endswith(TES3_EXT):
            log(f"[~] Ignoré (format non géré par tes3conv) : {name}")
            continue
        p = find_in_data(cfg, name)
        if not p:
            log(f"[!] Plugin introuvable dans les dossiers data : {name}")
            continue
        json_path = convert_plugin(p, cache, tes3conv)
        if json_path is None:
            skipped.append(name)
            continue
        with open(json_path, "r", encoding="utf-8") as f:
            plugins.append((p.name, json.load(f)))

    if not any(n.lower() == "morrowind.esm" for n, _ in plugins):
        log("[ERROR] Morrowind.esm n'est pas dans content= (ou introuvable).")
        return 1

    if skipped:
        log(f"[!] {len(skipped)} plugin(s) non convertible(s), ignoré(s) : {', '.join(skipped)}")
    log(f"[..] Fusion de {len(plugins)} plugin(s) dans l'ordre de chargement ...")
    merged = merge_plugins(plugins)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(merged, f, ensure_ascii=False, separators=(",", ":"))

    paths = {
        "meshes": asset_dirs(cfg, cache, "meshes"),
        "textures": asset_dirs(cfg, cache, "textures"),
    }
    with open(args.paths, "w", encoding="utf-8") as f:
        json.dump(paths, f, indent=2)
    log(f"[OK] {args.out} et {args.paths} générés "
        f"({len(paths['meshes'])} dossier(s) meshes, {len(paths['textures'])} textures).")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("find").set_defaults(func=cmd_find)

    p = sub.add_parser("prepare")
    p.add_argument("--cfg", required=True)
    p.add_argument("--tes3conv", required=True)
    p.add_argument("--bsatool", required=True)
    p.add_argument("--cache", default="data")
    p.add_argument("--out", default="output.json")
    p.add_argument("--paths", default="output_paths.json")
    p.set_defaults(func=cmd_prepare)

    args = ap.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()

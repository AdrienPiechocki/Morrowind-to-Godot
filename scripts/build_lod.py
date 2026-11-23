#!/usr/bin/env python3
"""Farlands / LODs directement depuis les donnees Morrowind : Python pur, sans Blender.

Pour chaque chunk de N x N cells exterieures, ecrit export_lod/lod_<cx>_<cy>.gltf :
  - le terrain (un mesh, sommets echantillonnes tous les --terrain-step, une texture atlas) ;
  - les gros assets (Static/Activator dont la plus grande dimension x scale >= --min-size), a leur
    resolution d'origine : les modeles de Morrowind sont deja low-poly, et Godot genere ses propres
    niveaux de detail a l'import (pas de decimation ici, donc pas de modeles casses).
Pas d'eau. Tous les chunks partagent l'origine Morrowind (0,0). Les chunks sont calcules en parallele.

  --json output.json --meshes DIRS --textures DIRS      (DIRS separes par ':' comme openmw_cfg.py)
  --outdir export_lod  --grid x1,y1:x2,y2  --exclude-grid x1,y1:x2,y2
  --chunk 8  --terrain-step 4  --tex-px 128  --min-size 400  --tex-max-side 256  --workers N
"""
import argparse
import json
import math
import multiprocessing
import os
import re
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mwassets
import mwgltf
import mwnif
import mwterrain
from mwlog import Progress, eprint, vprint

LOD_TYPES = {"Static", "Activator"}
MESH_SKIP = re.compile(r"marker|editor", re.I)

# ---- etat partage avec les workers (fork : copie sans serialisation) ----
G = {}


def parse_grid(value):
    if not value:
        return None
    n = [int(v) for v in re.findall(r"-?\d+", value)]
    if len(n) == 2:
        n *= 2
    if len(n) != 4:
        eprint(f"[ERROR] invalid grid '{value}' (expected 'x,y' or 'x1,y1:x2,y2')")
        sys.exit(1)
    return (min(n[0], n[2]), min(n[1], n[3]), max(n[0], n[2]), max(n[1], n[3]))


def inside(grid, gx, gy):
    return grid is None or (grid[0] <= gx <= grid[2] and grid[1] <= gy <= grid[3])


def cell_is_interior(cell):
    flags = (cell.get("data") or {}).get("flags")
    if isinstance(flags, int):
        return bool(flags & 1)
    return "INTERIOR" in str(flags).upper()


def cell_has_water(cell):
    """Cell exterieure sans record (terrain seul) : eau par defaut, comme dans le jeu."""
    if cell is None:
        return True
    return "HAS_WATER" in str((cell.get("data") or {}).get("flags")).upper()


def cell_water_level(cell, meters=True):
    wh = (cell or {}).get("water_height")
    wh = float(wh) if wh is not None else 0.0
    return round(wh * 0.01, 4) if meters else wh


def script_disabled_ids(data):
    pat = re.compile(r'(?:"([^"]+)"|([\w]+))\s*->\s*(?:Disable|SetDelete)\b', re.I)
    out = set()
    for r in data:
        if r.get("type") == "Script":
            for q, b in pat.findall(r.get("text", "")):
                out.add((q or b).strip().lower())
    return out


# ==========================================
# Un chunk (execute dans un worker)
# ==========================================

def get_model(mesh_rel):
    cache = G["models"]
    key = mesh_rel.replace("\\", "/").lower()
    if key not in cache:
        path = G["mesh_index"].find("meshes/" + key) or G["mesh_index"].find(key)
        cache[key] = mwnif.load_model(path, G.get("collision", False)) if path else None
    return cache[key]


def texture_uri(builder, nif_name, needed):
    """URI relative du PNG d'une texture NIF (et enregistrement de la conversion a faire)."""
    if not nif_name:
        return None
    src = G["tex_index"].find_texture(nif_name)
    if not src:
        G.setdefault("missing", set()).add(nif_name)
        return None
    out = mwassets.texture_out_name(src, G["tex_side"])
    needed[out] = src
    return G.get("tex_prefix", "textures/") + out


def add_model_mesh(builder, key, model, needed):
    prims = []
    for p in model.prims:
        m = p.material
        uri = texture_uri(builder, m.texture, needed)
        mat_id = builder.material(m.key(), os.path.splitext(os.path.basename(m.texture))[0] or "mat",
                                  uri, m.alpha, m.cutoff, m.double_sided, m.color)
        prims.append(builder.primitive(p.pos, p.nrm, p.uv if uri else None, p.col, p.idx, mat_id))
    return builder.add_mesh(key, prims)


def build_chunk(task):
    cx, cy = task
    t0 = time.time()
    G["missing"] = set()
    chunk = G["chunk"]
    step, T = G["terrain_step"], G["tile_px"]
    cell_px = T * 16
    needed = {}
    builder = mwgltf.GltfBuilder()
    cells = G["chunks"][(cx, cy)]

    # ---- terrain : un seul mesh + un atlas ----
    land_cells = [(gx, gy, G["landscape"][(gx, gy)]) for gx, gy, _ in cells if (gx, gy) in G["landscape"]]
    if land_cells:
        atlas = np.empty((chunk * cell_px, chunk * cell_px, 4), dtype=np.float32)
        atlas[:] = (0.1, 0.12, 0.08, 1.0)
        pos_l, nrm_l, col_l, uv_l, idx_l = [], [], [], [], []
        base = 0
        for gx, gy, land in land_cells:
            lx, ly = gx - cx * chunk, gy - cy * chunk
            ti = mwterrain.decode_texture_grid(land)
            px = G["land_tex"].bake(ti) if ti is not None else None
            if px is None:
                px = np.empty((cell_px, cell_px, 4), dtype=np.float32)
                px[:] = mwterrain.DEFAULT_COLOR
            atlas[ly * cell_px:(ly + 1) * cell_px, lx * cell_px:(lx + 1) * cell_px] = px

            pos, nrm, col, uv, tris, M = mwterrain.cell_geometry(land, step)
            pos_l.append(pos)
            nrm_l.append(nrm)
            col_l.append(col if col is not None else np.ones((len(pos), 4), np.float32))
            # uv de la cell -> uv de l'atlas (centres de pixels : pas de saignement entre cells)
            u = (lx * cell_px + 0.5 + uv[:, 0] * (cell_px - 1)) / (chunk * cell_px)
            v = (ly * cell_px + 0.5 + uv[:, 1] * (cell_px - 1)) / (chunk * cell_px)
            uv_l.append(np.stack([u, 1.0 - v], axis=-1))     # glTF : v vers le bas
            idx_l.append(tris + base)
            base += len(pos)

        from PIL import Image
        atlas_name = f"landatlas_{cx}_{cy}.png"
        os.makedirs(os.path.join(G["outdir"], "textures"), exist_ok=True)
        img = (np.clip(np.flipud(atlas[..., :3]), 0, 1) * 255 + 0.5).astype(np.uint8)
        Image.fromarray(img, "RGB").save(os.path.join(G["outdir"], "textures", atlas_name))
        mat = builder.material(("terrain", cx, cy), f"Terrain_{cx}_{cy}", "textures/" + atlas_name,
                               "OPAQUE", 0.5, False, (1, 1, 1, 1))
        prim = builder.primitive(np.concatenate(pos_l), np.concatenate(nrm_l), np.concatenate(uv_l),
                                 np.concatenate(col_l), np.concatenate(idx_l), mat)
        builder.add_node(builder.add_mesh(f"Terrain_{cx}_{cy}", [prim]), f"Terrain_{cx}_{cy}")

    # ---- assets ----
    mesh_ids = {}
    placed = 0
    for gx, gy, refs in cells:
        for rid, tr, rot, scale in refs:
            rec = G["records"].get(rid)
            if rec is None:
                continue
            key = rec.replace("\\", "/").lower()
            model = get_model(rec)
            if model is None or model.size * scale < G["min_size"]:
                continue
            if key not in mesh_ids:
                mesh_ids[key] = add_model_mesh(builder, key, model, needed)
            t, q, s = mwgltf.ref_node(tr, rot, scale)
            builder.add_node(mesh_ids[key], None, t, q, s)
            placed += 1

    stem = f"lod_{cx}_{cy}"
    builder.write(os.path.join(G["outdir"], stem))
    gxs = [gx for gx, _, _ in cells]
    gys = [gy for _, gy, _ in cells]
    return {"name": stem, "file": stem + ".gltf", "chunk": [cx, cy],
            "cells": [min(gxs), min(gys), max(gxs), max(gys)], "assets": placed,
            "water": [[gx, gy, cell_water_level(G["cells"].get((gx, gy)))] for gx, gy, _ in cells
                      if cell_has_water(G["cells"].get((gx, gy)))],
            "terrain_cells": len(land_cells), "seconds": round(time.time() - t0, 1),
            "missing_textures": sorted(G["missing"])}, needed


# ==========================================
# Pipeline
# ==========================================

def report_textures(results, errors, outdir):
    """Textures introuvables / non convertibles : resume + liste dans <outdir>/missing_textures.txt."""
    missing = sorted({m for r in results for m in r.get("missing_textures", [])}, key=str.lower)
    lines = [f"not found: {m}" for m in missing] + [f"not converted: {e}" for e in errors]
    path = os.path.join(outdir, "missing_textures.txt")
    if lines:
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        eprint(f"[!] {len(missing)} texture(s) not found, {len(errors)} not converted "
               f"(meshes kept untextured): see {path}")
    elif os.path.exists(path):
        os.remove(path)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--json", required=True)
    ap.add_argument("--meshes", required=True)
    ap.add_argument("--textures", required=True)
    ap.add_argument("--outdir", default="export_lod")
    ap.add_argument("--grid")
    ap.add_argument("--exclude-grid")
    ap.add_argument("--chunk", type=int, default=8)
    ap.add_argument("--terrain-step", type=int, default=4)
    ap.add_argument("--tex-px", type=int, default=128)
    ap.add_argument("--min-size", type=float, default=400.0)
    ap.add_argument("--tex-max-side", type=int, default=256)
    ap.add_argument("--workers", type=int, default=os.cpu_count() or 1)
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()

    if 64 % a.terrain_step:
        eprint(f"[ERROR] --terrain-step must divide 64 (got {a.terrain_step})")
        return 1
    target, excluded = parse_grid(a.grid), parse_grid(a.exclude_grid)
    outdir = os.path.abspath(a.outdir)
    os.makedirs(outdir, exist_ok=True)
    for old in os.listdir(outdir):                       # sorties precedentes de ce dossier
        if re.fullmatch(r"lod_-?\d+_-?\d+\.(gltf|bin)", old):
            os.remove(os.path.join(outdir, old))

    t0 = time.time()
    print("[..] Reading plugin data...", file=sys.stderr, flush=True)
    with open(a.json, "r", encoding="utf-8") as f:
        data = json.load(f)
    disabled = script_disabled_ids(data)

    records, ltex, landscape, cells = {}, {}, {}, {}
    for r in data:
        t = r.get("type")
        if t in LOD_TYPES and r.get("id") and r.get("mesh") and not MESH_SKIP.search(r["mesh"]):
            records[r["id"].lower()] = r["mesh"]
        elif t == "Landscape" and r.get("grid"):
            landscape[(int(r["grid"][0]), int(r["grid"][1]))] = r
        elif t == "LandscapeTexture":
            ltex[int(r.get("index", 0))] = r.get("file_name", "")
        elif t == "Cell" and not cell_is_interior(r):
            g = (r.get("data") or {}).get("grid") or [0, 0]
            cells[(int(g[0]), int(g[1]))] = r             # le dernier plugin gagne
    del data
    cells_by_grid = cells

    chunks = {}
    keys = set(cells) | set(landscape)
    for (gx, gy) in sorted(keys):
        if not (inside(target, gx, gy) and not (excluded and inside(excluded, gx, gy))):
            continue
        refs = []
        for ref in (cells.get((gx, gy)) or {}).get("references") or []:
            rid = (ref.get("id") or "").lower()
            if ref.get("deleted") or ref.get("disabled") is True or rid in disabled or rid not in records:
                continue
            refs.append((rid, ref.get("translation") or [0, 0, 0], ref.get("rotation") or [0, 0, 0],
                         float(ref.get("scale", 1.0) or 1.0)))
        chunks.setdefault((gx // a.chunk, gy // a.chunk), []).append((gx, gy, refs))
    if not chunks:
        eprint("[ERROR] No exterior cell found (check --grid and the plugins).")
        return 1
    ncells = sum(len(v) for v in chunks.values())
    print(f"[+] {ncells} exterior cell(s) -> {len(chunks)} chunk(s) of {a.chunk}x{a.chunk}",
          file=sys.stderr, flush=True)

    print("[..] Indexing meshes and textures...", file=sys.stderr, flush=True)
    mesh_index = mwassets.AssetIndex([d for d in a.meshes.split(os.pathsep) if d])
    tex_index = mwassets.AssetIndex([d for d in a.textures.split(os.pathsep) if d])

    G.update(chunk=a.chunk, terrain_step=a.terrain_step, tile_px=max(a.tex_px // 16, 1),
             min_size=a.min_size, tex_side=a.tex_max_side, outdir=outdir, chunks=chunks,
             landscape=landscape, records=records, cells=cells_by_grid, mesh_index=mesh_index, tex_index=tex_index,
             land_tex=mwterrain.LandTextures(ltex, tex_index, max(a.tex_px // 16, 1)), models={})

    order = sorted(chunks, key=lambda k: (k[1], k[0]))
    results, needed = [], {}
    workers = max(1, min(a.workers, len(order)))
    with Progress(len(order), "Chunks") as bar:
        if workers == 1:
            outs = map(build_chunk, order)
            for info, need in outs:
                results.append(info)
                needed.update(need)
                bar.update()
        else:
            ctx = multiprocessing.get_context("fork")
            with ctx.Pool(workers) as pool:
                for info, need in pool.imap_unordered(build_chunk, order):
                    results.append(info)
                    needed.update(need)
                    bar.update()
    results.sort(key=lambda r: (r["chunk"][1], r["chunk"][0]))

    print(f"[..] Converting {len(needed)} texture(s)...", file=sys.stderr, flush=True)
    texdir = os.path.join(outdir, "textures")
    os.makedirs(texdir, exist_ok=True)
    errors = mwassets.convert_textures(
        [(src, os.path.join(texdir, dst), a.tex_max_side) for dst, src in needed.items()], a.workers)
    report_textures(results, errors, outdir)

    manifest = {
        "scale": mwgltf.SCALE,
        "cell_size_m": mwterrain.CELL_SIZE * mwgltf.SCALE,
        "chunk_cells": a.chunk,
        "exclude_grid": list(excluded) if excluded else None,
        "origin": "Morrowind (0,0,0) = glTF (0,0,0); glTF/Godot: x = x, y = z (up), z = -y",
        "chunks": results,
    }
    with open(os.path.join(outdir, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)
    print(f"[OK] {len(results)} chunk(s), {sum(r['assets'] for r in results)} asset(s) in "
          f"{time.time() - t0:.0f}s -> {outdir}", file=sys.stderr, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Exterieur + interieurs detailles directement depuis les donnees Morrowind (Python pur, sans Blender).

Meme sortie que le mode "full" du pipeline Blender (lue telle quelle par build_godot.py) :

  <outdir>/exterior.gltf            terrain (une texture par cell) + decor de la grille
  <outdir>/interiors/<nom>.gltf     un interieur par fichier (origine = point d'arrivee de la 1re porte)
  <outdir>/textures/                PNG
  <outdir>/manifest.json            portes : d'ou l'on vient, ou l'on arrive

Les collisions des NIF sont exportees en noeuds "...-colonly" (Godot en fait des StaticBody3D).
Pas de NPC / creatures / animations : ce mode Blender-only (run.sh --npcs 1) reste le pipeline Blender.

  --mode full | exterior | interior   --grid x1,y1:x2,y2   --cell "Balmora, Temple"
  --depth 3        portes interieur -> interieur suivies
  --tex-px 1024    pixels par cell pour le terrain    --tex-max-side 1024   textures des modeles
"""
import argparse
import json
import multiprocessing
import os
import re
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import build_lod as L
import mwassets
import mwgltf
import mwterrain
from mwlog import Progress, eprint, vprint

SKIP_TYPES = {"Npc", "Creature", "BodyPart", "Bodypart", "LeveledCreature", "LeveledItem"}
G = L.G


def safe_stem(name, used):
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("_.")[:80] or "interior"
    stem, n = base, 2
    while stem.lower() in used:
        stem, n = f"{base}_{n}", n + 1
    used.add(stem.lower())
    return stem


def rnd(v, nd=4):
    return [round(float(x), nd) for x in v]


def place_refs(builder, refs, origin, needed, mesh_ids):
    """Un noeud (+ un noeud de collision) par reference. `origin` : decalage MW soustrait."""
    placed = 0
    for rid, tr, rot, scale in refs:
        rec = G["records"].get(rid)
        if rec is None:
            continue
        key = rec.replace("\\", "/").lower()
        model = L.get_model(rec)
        if model is None:
            continue
        if key not in mesh_ids:
            vis = L.add_model_mesh(builder, key, model, needed) if model.prims else None
            col = None
            if model.collision is not None:
                pos, idx = model.collision
                col = builder.add_mesh(key + "-colonly",
                                       [builder.primitive(pos, None, None, None, idx, None)])
            mesh_ids[key] = (vis, col)
        vis, col = mesh_ids[key]
        t, q, s = mwgltf.ref_node(np.asarray(tr, dtype=np.float64) - origin, rot, scale)
        if vis is not None:
            builder.add_node(vis, rid, t, q, s)
        if col is not None:
            builder.add_node(col, rid + "-colonly", t, q, s)
        placed += 1
    return placed


def build_exterior(task):
    """Exterieur de la grille : terrain (un mesh + une texture par cell) + decor."""
    origin, cells = task
    G["missing"] = set()
    outdir = G["outdir"]
    T = G["tile_px"]
    needed = {}
    builder = mwgltf.GltfBuilder("textures/")
    G["tex_prefix"] = "textures/"
    from PIL import Image
    os.makedirs(os.path.join(outdir, "textures"), exist_ok=True)
    mesh_ids = {}
    placed = 0
    for gx, gy, refs in cells:
        land = G["landscape"].get((gx, gy))
        if land is not None:
            pos, nrm, col, uv, tris, _ = mwterrain.cell_geometry(land, 1)
            ti = mwterrain.decode_texture_grid(land)
            px = G["land_tex"].bake(ti, land.get("_plugin")) if ti is not None else None
            if px is None:
                px = np.empty((16 * T, 16 * T, 4), dtype=np.float32)
                px[:] = mwterrain.DEFAULT_COLOR
            name = f"land_{gx}_{gy}.png"
            Image.fromarray((np.clip(np.flipud(px[..., :3]), 0, 1) * 255 + 0.5).astype(np.uint8), "RGB") \
                .save(os.path.join(outdir, "textures", name))
            mat = builder.material(("land", gx, gy), f"Land_{gx}_{gy}", "textures/" + name,
                                   "OPAQUE", 0.5, False, (1, 1, 1, 1))
            uvg = np.stack([uv[:, 0], 1.0 - uv[:, 1]], axis=-1).astype(np.float32)
            prim = builder.primitive(pos - origin, nrm, uvg, col, tris, mat)
            tmesh = builder.add_mesh(f"Terrain_{gx}_{gy}", [prim])
            builder.add_node(tmesh, f"Terrain_{gx}_{gy}")
            # meme mesh en collision : Godot en fait un StaticBody3D (suffixe -colonly, comme les decors)
            builder.add_node(tmesh, f"Terrain_{gx}_{gy}-colonly")
        placed += place_refs(builder, refs, origin, needed, mesh_ids)
    builder.write(os.path.join(outdir, "exterior"))
    return "exterior", placed, needed, sorted(G["missing"])


def build_interior(task):
    stem, origin, refs = task
    G["missing"] = set()
    outdir = G["outdir"]
    needed = {}
    G["tex_prefix"] = "../textures/"
    builder = mwgltf.GltfBuilder("../textures/")
    placed = place_refs(builder, refs, origin, needed, {})
    os.makedirs(os.path.join(outdir, "interiors"), exist_ok=True)
    builder.write(os.path.join(outdir, "interiors", stem))
    return stem, placed, needed, sorted(G["missing"])


def _run(fn, tasks, workers, label):
    results = []
    with Progress(len(tasks), label, disable=not tasks) as bar:
        if workers <= 1 or len(tasks) == 1:
            for t in tasks:
                results.append(fn(t))
                bar.update()
        else:
            ctx = multiprocessing.get_context("fork")
            with ctx.Pool(min(workers, len(tasks))) as pool:
                for r in pool.imap_unordered(fn, tasks):
                    results.append(r)
                    bar.update()
    return results


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--json", required=True)
    ap.add_argument("--meshes", required=True)
    ap.add_argument("--textures", required=True)
    ap.add_argument("--outdir", default="export")
    ap.add_argument("--mode", choices=("full", "exterior", "interior"), default="full")
    ap.add_argument("--grid")
    ap.add_argument("--cell", default="")
    ap.add_argument("--depth", type=int, default=3)
    ap.add_argument("--tex-px", type=int, default=1024)
    ap.add_argument("--tex-max-side", type=int, default=1024)
    ap.add_argument("--workers", type=int, default=os.cpu_count() or 1)
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()

    grid = L.parse_grid(a.grid)
    if a.mode in ("full", "exterior") and grid is None:
        eprint(f"[ERROR] --mode {a.mode} requires --grid")
        return 1
    if a.mode == "interior" and not a.cell:
        eprint("[ERROR] --mode interior requires --cell")
        return 1
    outdir = os.path.abspath(a.outdir)
    t0 = time.time()

    # sorties precedentes de ce dossier : on repart de zero
    import shutil
    shutil.rmtree(os.path.join(outdir, "interiors"), ignore_errors=True)
    for old in ("exterior.gltf", "exterior.bin", "manifest.json"):
        try:
            os.remove(os.path.join(outdir, old))
        except OSError:
            pass
    os.makedirs(outdir, exist_ok=True)

    print("[..] Reading plugin data...", file=sys.stderr, flush=True)
    with open(a.json, "r", encoding="utf-8") as f:
        data = json.load(f)
    disabled = L.script_disabled_ids(data)
    records, rtypes, ltex, landscape = {}, {}, {}, {}
    ext_cells, int_cells = {}, {}
    for r in data:
        t = r.get("type")
        rid = (r.get("id") or "").lower()
        if rid and r.get("mesh") and t not in SKIP_TYPES and not L.MESH_SKIP.search(r["mesh"]):
            records[rid] = r["mesh"]
        if rid and t:
            rtypes[rid] = t
        if t == "Landscape" and r.get("grid"):
            landscape[(int(r["grid"][0]), int(r["grid"][1]))] = r
        elif t == "LandscapeTexture":
            mwterrain.register_ltex(ltex, r)
        elif t == "Cell":
            if L.cell_is_interior(r):
                if r.get("name"):
                    int_cells[r["name"].lower()] = r
            else:
                g = (r.get("data") or {}).get("grid") or [0, 0]
                ext_cells[(int(g[0]), int(g[1]))] = r
    del data

    def cell_refs(cell):
        out = []
        for ref in (cell or {}).get("references") or []:
            rid = (ref.get("id") or "").lower()
            if ref.get("deleted") or ref.get("disabled") is True or rid in disabled or rid not in records:
                continue
            out.append((rid, ref.get("translation") or [0, 0, 0], ref.get("rotation") or [0, 0, 0],
                        float(ref.get("scale", 1.0) or 1.0)))
        return out

    def interior_doors(cell):
        """[(ref, nom, destination)] des portes menant a un interieur connu."""
        out = []
        for ref in (cell or {}).get("references") or []:
            dest = ref.get("destination")
            if ref.get("deleted") or not isinstance(dest, dict):
                continue
            name = (dest.get("cell") or "").strip()
            if name and name.lower() in int_cells and rtypes.get((ref.get("id") or "").lower()) == "Door":
                out.append((ref, name, dest))
        return out

    print("[..] Indexing meshes and textures...", file=sys.stderr, flush=True)
    mesh_index = mwassets.AssetIndex([d for d in a.meshes.split(os.pathsep) if d])
    tex_index = mwassets.AssetIndex([d for d in a.textures.split(os.pathsep) if d])
    T = max(a.tex_px // 16, 1)
    G.update(outdir=outdir, tile_px=T, tex_side=a.tex_max_side, landscape=landscape, records=records,
             mesh_index=mesh_index, tex_index=tex_index, models={}, collision=True, tex_prefix="textures/",
             land_tex=mwterrain.LandTextures(ltex, tex_index, T))

    needed, missing = {}, set()
    manifest = {
        "scale": mwgltf.SCALE,
        "notes": "Positions in meters, Blender frame (Z up), in the indicated file. "
                 "Each interior has its origin at the arrival point of its first door; "
                 "'arrival_position' gives the arrival point of each door in the interior's file.",
        "interiors": [],
    }
    cs = mwterrain.CELL_SIZE
    interior_tasks = []          # (stem, origin_mw, refs)
    queue = []                   # (nom, ref, dest, parent, door_pos, depth)

    # ---------- exterieur ----------
    if a.mode in ("full", "exterior"):
        x1, y1, x2, y2 = grid
        origin = np.array([(x1 + x2 + 1) / 2.0 * cs, (y1 + y2 + 1) / 2.0 * cs, 0.0])
        cells = []
        for gy in range(y1, y2 + 1):
            for gx in range(x1, x2 + 1):
                if (gx, gy) in ext_cells or (gx, gy) in landscape:
                    cells.append((gx, gy, cell_refs(ext_cells.get((gx, gy)))))
        print(f"[+] Exterior grid {grid}: {len(cells)} cell(s)", file=sys.stderr, flush=True)
        manifest["exterior"] = {"file": "exterior", "grid": list(grid), "origin_mw": rnd(origin, 2),
                                "water": [[gx, gy, L.cell_water_level(ext_cells.get((gx, gy)))]
                                          for gx, gy, _ in cells if L.cell_has_water(ext_cells.get((gx, gy)))]}
        # le decor est decoupe en lots pour paralleliser quand la grille est grande ; ici un seul fichier
        _, n_ext, need, miss = _run(build_exterior, [(origin, cells)], 1, "Exterior")[0]
        needed.update(need)
        missing.update(miss)
        print(f"[+] Exterior: {n_ext} object(s)", file=sys.stderr, flush=True)
        if a.mode == "full":
            for gx, gy, _ in cells:
                for ref, name, dest in interior_doors(ext_cells.get((gx, gy))):
                    p = np.asarray(ref.get("translation") or [0, 0, 0]) * mwgltf.SCALE - origin * mwgltf.SCALE
                    queue.append((name, ref, dest, "exterior", p, 1))
    else:
        cell = int_cells.get(a.cell.strip().lower())
        if cell is None:
            eprint(f"[ERROR] interior cell not found: {a.cell}")
            return 1
        queue.append((cell["name"], {"id": None}, {"translation": [0, 0, 0], "rotation": [0, 0, 0]},
                      "start", np.zeros(3), 1))

    # ---------- interieurs (BFS) ----------
    used, origins, entries = set(), {}, {}
    while queue:
        name, door_ref, dest, parent, door_pos, depth = queue.pop(0)
        key = name.lower()
        arrival = np.asarray(dest.get("translation") or [0, 0, 0], dtype=np.float64)    # unites MW
        if key in entries:
            entries[key]["doors"].append({
                "from": parent, "door_id": door_ref.get("id"), "door_position": rnd(door_pos),
                "arrival_position": rnd((arrival - origins[key]) * mwgltf.SCALE),
                "arrival_rotation": rnd(dest.get("rotation") or [0, 0, 0])})
            continue
        stem = safe_stem(name, used)
        origins[key] = arrival
        cell = int_cells[key]
        irefs = cell_refs(cell)
        interior_tasks.append((stem, arrival, irefs))
        water = None
        if L.cell_has_water(cell) and irefs:
            pts = np.array([r[1][:2] for r in irefs], dtype=np.float64) - arrival[:2]
            lo, hi = pts.min(axis=0) * mwgltf.SCALE - 20.0, pts.max(axis=0) * mwgltf.SCALE + 20.0
            water = {"level": round((L.cell_water_level(cell, meters=False) - arrival[2]) * mwgltf.SCALE, 4),
                     "bounds": rnd([lo[0], lo[1], hi[0], hi[1]], 2)}
        entries[key] = {"name": name, "file": f"interiors/{stem}", "depth": depth, "water": water, "doors": [{
            "from": parent, "door_id": door_ref.get("id"), "door_position": rnd(door_pos),
            "arrival_position": [0.0, 0.0, 0.0],
            "arrival_rotation": rnd(dest.get("rotation") or [0, 0, 0])}]}
        if depth < a.depth:
            for ref, nxt, nxt_dest in interior_doors(cell):
                p = (np.asarray(ref.get("translation") or [0, 0, 0], dtype=np.float64) - arrival) * mwgltf.SCALE
                queue.append((nxt, ref, nxt_dest, name, p, depth + 1))
    manifest["interiors"] = list(entries.values())

    if interior_tasks:
        print(f"[+] {len(interior_tasks)} interior(s)", file=sys.stderr, flush=True)
        for _, n, need, miss in _run(build_interior, interior_tasks, a.workers, "Interiors"):
            needed.update(need)
            missing.update(miss)

    # ---------- textures + manifest ----------
    print(f"[..] Converting {len(needed)} texture(s)...", file=sys.stderr, flush=True)
    texdir = os.path.join(outdir, "textures")
    os.makedirs(texdir, exist_ok=True)
    errors = mwassets.convert_textures(
        [(src, os.path.join(texdir, dst), a.tex_max_side) for dst, src in needed.items()], a.workers)
    L.report_textures([{"missing_textures": sorted(missing)}], errors, outdir)
    with open(os.path.join(outdir, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    print(f"[OK] exterior + {len(interior_tasks)} interior(s) in {time.time() - t0:.0f}s -> {outdir}",
          file=sys.stderr, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

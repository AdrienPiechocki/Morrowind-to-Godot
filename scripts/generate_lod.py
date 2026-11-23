"""Genere des farlands / LODs : terrain + gros assets en basse qualite, par morceaux.

Lance par run_lod.sh (blender --background --python scripts/generate_lod.py -- ...).
Reutilise generate_blend.py (import des NIF, decodage du terrain, bake des textures).

Le monde (cells exterieures de output.json) est decoupe en chunks de N x N cells ;
un .blend par chunk, avec :
  - Terrain : UN mesh decime (1 sommet tous les --terrain-step, 64 = cell entiere) et UNE
    texture atlas (--tex-px pixels par cell) -> un seul materiau par chunk ;
  - Eau : un plan de mer au niveau 0 + les cells dont l'eau n'est pas a 0 (lacs) ;
  - Gros assets : seules les references Static/Activator dont la plus grande dimension
    (modele x scale) depasse --min-size, decimees a ~--lod-tris triangles par modele ;
    collisions / ombres / marqueurs retires.
Tous les chunks partagent la meme origine (MW 0,0) : ils se posent cote a cote sans decalage.

Arguments (apres "--") :
  --json output.json  --meshes DIRS  --textures DIRS     (comme generate_blend.py)
  --outdir export_lod        dossier de sortie
  --grid x1,y1:x2,y2         restreint a ce rectangle de cells (defaut : tout)
  --chunk 8                  cells par cote d'un chunk
  --terrain-step 4           diviseur de 64 (1 = pleine resolution, 4 = 17x17 sommets/cell)
  --tex-px 128               pixels par cell de la texture du terrain (multiple de 16)
  --min-size 400             taille minimale (unites MW, ~1 m = 70) d'un asset conserve
  --lod-tris 400             triangles cibles par modele
  --sea 1|0                  plan de mer par chunk
"""
import json
import os
import re
import sys
import time

import bpy
import numpy as np
from mathutils import Vector

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import generate_blend as gb
from generate_blend import get_arg
from mwlog import vprint, eprint, Progress

CELL_SIZE = gb.CELL_SIZE
SCALE = gb.SCALE_FACTOR
LAND_STEP = gb.LAND_STEP

OUTDIR = os.path.abspath(get_arg("outdir", "export_lod"))
CHUNK = max(int(get_arg("chunk", "8")), 1)
TERRAIN_STEP = int(get_arg("terrain-step", "4"))
TEX_PX = int(get_arg("tex-px", "128"))
MIN_SIZE = float(get_arg("min-size", "400"))
LOD_TRIS = max(int(get_arg("lod-tris", "400")), 12)
WITH_SEA = get_arg("sea", "1") != "0"

if 64 % TERRAIN_STEP != 0:
    eprint(f"[ERROR] --terrain-step must divide 64 (got {TERRAIN_STEP})")
    sys.exit(1)
TILE_PX = max(TEX_PX // 16, 1)
CELL_PX = TILE_PX * 16
gb.LAND_TILE_PX = TILE_PX

# Types dont on garde le decor lointain ; nom d'objet NIF a jeter
LOD_TYPES = {"Static", "Activator"}
JUNK_RE = re.compile(r"collision|shadow|marker|bounding|bound box", re.I)
MESH_SKIP_RE = re.compile(r"marker|editor", re.I)

ORIGIN = Vector((0.0, 0.0, 0.0))     # origine commune a tous les chunks
size_cache = {}                      # mesh (cle normalisee) -> plus grande dimension, unites MW
_processed = set()                   # modeles deja nettoyes/decimes dans la scene courante
_stats = {"decimated": 0, "tris_before": 0, "tris_after": 0}


# ==========================================
# Decimation des modeles
# ==========================================

_tmp_scene = None


def _decimate(obj, ratio):
    """Remplace le mesh de `obj` par sa version decimee (modificateur Decimate)."""
    global _tmp_scene
    if _tmp_scene is None:
        _tmp_scene = bpy.data.scenes.new("lod_tmp")
    layer = _tmp_scene.view_layers[0]
    _tmp_scene.collection.objects.link(obj)
    mod = obj.modifiers.new("lod", "DECIMATE")
    mod.decimate_type = "COLLAPSE"
    mod.ratio = ratio
    try:
        layer.update()
        evaluated = obj.evaluated_get(layer.depsgraph)
        new_mesh = bpy.data.meshes.new_from_object(evaluated)
        old = obj.data
        obj.modifiers.clear()
        obj.data = new_mesh
        if old.users == 0:
            bpy.data.meshes.remove(old)
    finally:
        obj.modifiers.clear()
        _tmp_scene.collection.objects.unlink(obj)


def _descendants(objs):
    out = set(objs)
    stack = list(objs)
    while stack:
        o = stack.pop()
        for c in o.children:
            if c not in out:
                out.add(c)
                stack.append(c)
    return out


def _model_size(template_objs, snapshot):
    """Plus grande dimension du modele (unites MW), dans son repere monde d'import."""
    lo = Vector((1e18, 1e18, 1e18))
    hi = Vector((-1e18, -1e18, -1e18))
    found = False
    for o in template_objs:
        if o.type != "MESH" or not len(o.data.vertices):
            continue
        mw = snapshot[o]["matrix_world"]
        for c in o.bound_box:
            p = mw @ Vector(c)
            for k in range(3):
                lo[k] = min(lo[k], p[k])
                hi[k] = max(hi[k], p[k])
            found = True
    if not found:
        return 0.0
    return max(hi[k] - lo[k] for k in range(3)) / SCALE


def lod_get_or_import_mesh(mesh_path, _tried=(), is_model=False):
    """get_or_import_mesh + nettoyage et decimation du modele (une fois par modele)."""
    res = _orig_get_or_import_mesh(mesh_path, _tried, is_model)
    if not res:
        return res
    key = mesh_path.replace("\\", "/").lower()
    if key in _processed:
        return res
    _processed.add(key)
    template_objs, snapshot = res

    # 1. collisions, ombres, marqueurs (et leurs descendants)
    junk = _descendants([o for o in template_objs if JUNK_RE.search(o.name)])
    for o in [o for o in template_objs if o in junk]:
        template_objs.remove(o)
        snapshot.pop(o, None)
    for o in junk:
        bpy.data.objects.remove(o, do_unlink=True)

    size_cache[key] = _model_size(template_objs, snapshot)

    # 2. decimation proportionnelle du modele entier
    meshes = [o for o in template_objs if o.type == "MESH"]
    total = sum(len(o.data.polygons) for o in meshes)
    _stats["tris_before"] += total
    ratio = LOD_TRIS / total if total else 1.0
    if ratio < 1.0:
        for o in meshes:
            if len(o.data.polygons) < 12:
                continue
            try:
                _decimate(o, max(ratio, 0.02))
            except Exception as e:
                vprint(f"[-] Decimation failed for '{o.name}': {e}")
        _stats["decimated"] += 1
    _stats["tris_after"] += sum(len(o.data.polygons) for o in meshes)
    return res


_orig_get_or_import_mesh = gb.get_or_import_mesh
gb.get_or_import_mesh = lod_get_or_import_mesh


# ==========================================
# Terrain d'un chunk
# ==========================================

def _read_pixels(img):
    buf = np.empty(img.size[0] * img.size[1] * 4, dtype=np.float32)
    img.pixels.foreach_get(buf)
    return buf.reshape(img.size[1], img.size[0], 4)


def build_chunk_terrain(cx, cy, cells, coll):
    """Un mesh decime + un atlas pour tous les cells (avec LAND) du chunk."""
    step = TERRAIN_STEP
    M = 64 // step                     # intervalles par cell
    lands = []
    for cell in cells:
        gx, gy = gb.cell_grid(cell)
        land = gb.landscape_map.get((gx, gy))
        if land:
            lands.append((gx, gy, land))
    if not lands:
        return None

    gx0, gy0 = cx * CHUNK, cy * CHUNK
    atlas = np.empty((CHUNK * CELL_PX, CHUNK * CELL_PX, 4), dtype=np.float32)
    atlas[:] = (0.1, 0.12, 0.08, 1.0)

    vert_index = {}                    # (X, Y) du reseau du chunk -> indice de sommet
    verts, colors = [], []
    faces, face_uv = [], []
    idx = [i * step for i in range(M + 1)]

    for gx, gy, land in lands:
        lx, ly = gx - gx0, gy - gy0
        heights = gb.decode_heights(land)
        cn = gb._numbers(land.get("vertex_colors"), "B")
        has_col = bool(cn and len(cn) >= gb.LAND_N * gb.LAND_N * 3)

        ti = gb.decode_texture_grid(land)
        if ti:
            img = gb.bake_terrain_image(ti, gx, gy)
            atlas[ly * CELL_PX:(ly + 1) * CELL_PX, lx * CELL_PX:(lx + 1) * CELL_PX] = _read_pixels(img)
            bpy.data.images.remove(img)
        else:
            atlas[ly * CELL_PX:(ly + 1) * CELL_PX, lx * CELL_PX:(lx + 1) * CELL_PX] = (0.25, 0.3, 0.15, 1.0)

        def vid(a, b):
            key = (lx * M + a, ly * M + b)
            v = vert_index.get(key)
            if v is None:
                v = len(verts)
                vert_index[key] = v
                verts.append(None)
                colors.append((1.0, 1.0, 1.0, 1.0))
            i, j = idx[a], idx[b]       # i : colonne (x), j : rangee (y)
            verts[v] = ((gx * CELL_SIZE + i * LAND_STEP) * SCALE,
                        (gy * CELL_SIZE + j * LAND_STEP) * SCALE,
                        heights[j][i] * SCALE)
            if has_col:
                k = (j * gb.LAND_N + i) * 3
                colors[v] = (cn[k] / 255.0, cn[k + 1] / 255.0, cn[k + 2] / 255.0, 1.0)
            return v

        for b in range(M):
            for a in range(M):
                quad = (vid(a, b), vid(a + 1, b), vid(a + 1, b + 1), vid(a, b + 1))
                faces.append(quad)
                for da, db in ((0, 0), (1, 0), (1, 1), (0, 1)):
                    # centre des pixels : evite le saignement entre cells de l'atlas
                    px = lx * CELL_PX + 0.5 + (a + da) / M * (CELL_PX - 1)
                    py = ly * CELL_PX + 0.5 + (b + db) / M * (CELL_PX - 1)
                    face_uv.append((px / (CHUNK * CELL_PX), py / (CHUNK * CELL_PX)))

    mesh = bpy.data.meshes.new(f"Terrain_{cx}_{cy}")
    mesh.from_pydata(verts, [], faces)
    mesh.update()

    atlas_img = bpy.data.images.new(f"LandAtlas_{cx}_{cy}", CHUNK * CELL_PX, CHUNK * CELL_PX, alpha=False)
    atlas[..., 3] = 1.0
    atlas_img.pixels.foreach_set(atlas.ravel())
    atlas_img.update()
    atlas_img.pack()
    mesh.materials.append(gb.make_baked_material(atlas_img, cx, cy))

    uv = mesh.uv_layers.new(name="UVMap")
    uv.data.foreach_set("uv", np.asarray(face_uv, dtype=np.float32).ravel())
    ca = mesh.color_attributes.new("Col", "FLOAT_COLOR", "POINT")
    ca.data.foreach_set("color", np.asarray(colors, dtype=np.float32).ravel())
    try:
        mesh.shade_smooth()
    except AttributeError:
        for p in mesh.polygons:
            p.use_smooth = True

    obj = bpy.data.objects.new(f"Terrain_{cx}_{cy}", mesh)
    coll.objects.link(obj)
    return obj


def build_chunk_water(cx, cy, cells, coll):
    if WITH_SEA:
        x0, y0 = cx * CHUNK * CELL_SIZE * SCALE, cy * CHUNK * CELL_SIZE * SCALE
        s = CHUNK * CELL_SIZE * SCALE
        vs = [(x0, y0, 0.0), (x0 + s, y0, 0.0), (x0 + s, y0 + s, 0.0), (x0, y0 + s, 0.0)]
        mesh = bpy.data.meshes.new(f"Sea_{cx}_{cy}")
        mesh.from_pydata(vs, [], [(0, 1, 2, 3)])
        mesh.update()
        mesh.materials.append(gb.get_water_material())
        coll.objects.link(bpy.data.objects.new(f"Sea_{cx}_{cy}", mesh))
    for cell in cells:     # lacs : eau a un autre niveau que la mer
        wh = cell.get("water_height")
        if wh is not None and abs(float(wh)) > 1e-6:
            gx, gy = gb.cell_grid(cell)
            gb.build_water(gx, gy, float(wh), ORIGIN, coll)


# ==========================================
# Assets
# ==========================================

def place_chunk_assets(cells, record_map, coll):
    gb._set_import_mode("cell")
    todo = []
    for cell in cells:
        for ref in cell.get("references") or []:
            if gb.is_ref_skipped(ref):
                continue
            rec = record_map.get((ref.get("id") or "").lower())
            if not rec or rec.get("type") not in LOD_TYPES:
                continue
            mesh = rec.get("mesh")
            if not mesh or MESH_SKIP_RE.search(mesh):
                continue
            key = mesh.replace("\\", "/").lower()
            known = size_cache.get(key)
            if known is not None and known * float(ref.get("scale", 1.0) or 1.0) < MIN_SIZE:
                continue            # deja mesure : trop petit, inutile de le reimporter
            todo.append((ref, rec, key))

    placed = 0
    with Progress(len(todo), "Assets", disable=not todo) as bar:
        for ref, rec, key in todo:
            bar.update()
            if key not in size_cache:
                if not gb.get_or_import_mesh(rec["mesh"], is_model=True):
                    size_cache[key] = 0.0
                    continue
            if size_cache.get(key, 0.0) * float(ref.get("scale", 1.0) or 1.0) < MIN_SIZE:
                continue
            placed += gb.place_reference(rec, ref, ORIGIN, coll)
    return placed


# ==========================================
# Pipeline
# ==========================================

def main():
    data = gb.load_tes3_json(gb.JSON_FILE_PATH)
    record_map = gb.build_record_map(data)
    gb.build_landscape_maps(data)

    seen = {}
    for rec in data:
        if rec.get("type") == "Cell" and not gb.cell_is_interior(rec):
            seen[gb.cell_key(rec)] = rec           # le dernier plugin gagne
    del data

    cells = list(seen.values())
    if gb.TARGET_GRID is not None:
        x1, y1, x2, y2 = gb.TARGET_GRID
        cells = [c for c in cells if x1 <= gb.cell_grid(c)[0] <= x2 and y1 <= gb.cell_grid(c)[1] <= y2]
    # une cell sans reference mais avec un LAND compte aussi (terrain seul)
    have = {gb.cell_grid(c) for c in cells}
    for (gx, gy) in gb.landscape_map:
        inside = gb.TARGET_GRID is None or (
            gb.TARGET_GRID[0] <= gx <= gb.TARGET_GRID[2] and gb.TARGET_GRID[1] <= gy <= gb.TARGET_GRID[3])
        if inside and (gx, gy) not in have:
            cells.append({"data": {"grid": [gx, gy], "flags": 0}, "references": []})

    chunks = {}
    for c in cells:
        gx, gy = gb.cell_grid(c)
        chunks.setdefault((gx // CHUNK, gy // CHUNK), []).append(c)
    if not chunks:
        eprint("[ERROR] No exterior cell found (check --grid and the plugins).")
        sys.exit(1)

    vprint(f"[+] {len(cells)} exterior cell(s) -> {len(chunks)} chunk(s) of {CHUNK}x{CHUNK}")
    os.makedirs(OUTDIR, exist_ok=True)
    for old in os.listdir(OUTDIR):
        if re.fullmatch(r"lod_-?\d+_-?\d+\.blend1?", old):
            os.remove(os.path.join(OUTDIR, old))

    for o in list(bpy.data.objects):
        bpy.data.objects.remove(o)

    manifest = {
        "scale": SCALE,
        "cell_size_m": CELL_SIZE * SCALE,
        "chunk_cells": CHUNK,
        "origin": "Blender (0,0,0) = Morrowind (0,0); glTF/Godot: x = x, y = z (up), z = -y",
        "chunks": [],
    }
    stems = []
    t0 = time.time()
    with Progress(len(chunks), "Chunks") as bar:
        for (cx, cy), ccells in sorted(chunks.items(), key=lambda kv: (kv[0][1], kv[0][0])):
            ccells.sort(key=lambda c: (gb.cell_grid(c)[1], gb.cell_grid(c)[0]))
            stem = f"lod_{cx}_{cy}"
            gb.reset_scene()
            _processed.clear()
            coll = bpy.data.collections.new(f"LOD_{cx}_{cy}")
            bpy.context.scene.collection.children.link(coll)

            build_chunk_terrain(cx, cy, ccells, coll)
            build_chunk_water(cx, cy, ccells, coll)
            n = place_chunk_assets(ccells, record_map, coll)
            vprint(f"[+] {stem}: {len(ccells)} cell(s), {n} asset(s)")

            gb.finalize_and_save(os.path.join(OUTDIR, stem + ".blend"))
            stems.append(stem)
            gxs = [gb.cell_grid(c)[0] for c in ccells]
            gys = [gb.cell_grid(c)[1] for c in ccells]
            manifest["chunks"].append({
                "name": stem, "file": stem + ".gltf", "chunk": [cx, cy],
                "cells": [min(gxs), min(gys), max(gxs), max(gys)],
                "assets": n,
            })
            bar.update()

    with open(os.path.join(OUTDIR, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)
    with open(os.path.join(OUTDIR, "files.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(stems) + "\n")
    gb.report_failed_meshes()
    vprint(f"[+] Decimated {_stats['decimated']} model(s): "
           f"{_stats['tris_before']} -> {_stats['tris_after']} polygons")
    vprint(f"[+] DONE in {time.time() - t0:.0f}s: {len(stems)} chunk(s) in {OUTDIR}")


main()

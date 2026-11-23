"""Terrain Morrowind (records Landscape de tes3conv) -> geometrie + texture, sans Blender."""
import base64
import os
import shutil
import struct
import subprocess

import numpy as np

CELL_SIZE = 8192.0           # cote d'une cell exterieure (unites MW)
LAND_N = 65                  # sommets par cote
LAND_STEP = CELL_SIZE / 64.0
LAND_HEIGHT_UNIT = 8.0
DEFAULT_COLOR = (0.25, 0.3, 0.15, 1.0)


# ---------- decodage des champs tes3conv ----------

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
        raise RuntimeError("zstd not found: `sudo pacman -S zstd` or `pip install zstandard`")
    return subprocess.run([exe, "-d", "-c", "-q"], input=raw, stdout=subprocess.PIPE, check=True).stdout


def numbers(v, fmt):
    """Liste plate de nombres depuis un champ tes3conv (blob base64[+zstd] ou tableau JSON).
    fmt : 'b' int8, 'B' uint8, 'H' uint16 (little endian)."""
    if isinstance(v, dict) and "data" in v:
        v = v["data"]
    if v is None:
        return None
    if isinstance(v, str):
        raw = base64.b64decode(v)
        if raw[:4] == b"\x28\xb5\x2f\xfd":
            raw = _zstd_decompress(raw)
        size = struct.calcsize(fmt)
        n = len(raw) // size
        return np.array(struct.unpack(f"<{n}{fmt}", raw[:n * size]))
    out = []

    def rec(x):
        if isinstance(x, (list, tuple)):
            for i in x:
                rec(i)
        else:
            out.append(x)
    rec(v)
    return np.array(out)


def decode_heights(land):
    """(65, 65) hauteurs [y][x] en unites MW."""
    N = LAND_N
    vh = land.get("vertex_heights")
    flat = np.zeros((N, N), dtype=np.float64)
    if not vh:
        return flat
    offset = float(vh.get("offset", 0.0)) if isinstance(vh, dict) else 0.0
    nums = numbers(vh, "b")
    if nums is None or len(nums) < N * N:
        return flat
    d = nums[:N * N].reshape(N, N).astype(np.float64)
    rows = offset + np.cumsum(d[:, 0])           # debut de chaque rangee : deltas verticaux
    flat[:, 0] = rows
    flat[:, 1:] = rows[:, None] + np.cumsum(d[:, 1:], axis=1)
    return flat * LAND_HEIGHT_UNIT


def decode_colors(land):
    cn = numbers(land.get("vertex_colors"), "B")
    if cn is None or len(cn) < LAND_N * LAND_N * 3:
        return None
    return cn[:LAND_N * LAND_N * 3].reshape(LAND_N, LAND_N, 3).astype(np.float32) / 255.0


def decode_texture_grid(land):
    """Grille 16x16 des indices de texture (ordre par blocs 4x4 du format Morrowind)."""
    tn = numbers(land.get("texture_indices"), "H")
    if tn is None or len(tn) < 256:
        return None
    t = tn[:256].reshape(4, 4, 4, 4)             # [y1][x1][y2][x2]
    return t.transpose(0, 2, 1, 3).reshape(16, 16)   # -> [y1*4+y2][x1*4+x2]


# ---------- geometrie ----------

def cell_geometry(land, step):
    """Sommets d'une cell echantillonnee tous les `step` : (pos MW (n,3), normales, couleurs|None, uv local
    [0..1] vers le haut, triangles).  n = (64/step + 1)^2, ordre [j][i]."""
    h = decode_heights(land)
    gx, gy = int(land["grid"][0]), int(land["grid"][1])
    gyy, gxx = np.gradient(h, LAND_STEP)          # d/dy, d/dx
    nrm_full = np.stack([-gxx, -gyy, np.ones_like(h)], axis=-1)
    nrm_full /= np.linalg.norm(nrm_full, axis=-1, keepdims=True)

    idx = np.arange(0, 65, step)
    M = len(idx) - 1
    J, I = np.meshgrid(idx, idx, indexing="ij")
    pos = np.stack([gx * CELL_SIZE + I * LAND_STEP, gy * CELL_SIZE + J * LAND_STEP, h[J, I]], axis=-1)
    nrm = nrm_full[J, I]
    cols = decode_colors(land)
    col = None
    if cols is not None:
        col = np.concatenate([cols[J, I], np.ones((M + 1, M + 1, 1), np.float32)], axis=-1).reshape(-1, 4)
    uv = np.stack([I / 64.0, J / 64.0], axis=-1).reshape(-1, 2)

    a = (np.arange(M)[:, None] * (M + 1) + np.arange(M)[None, :]).ravel()    # coin (i, j)
    b, c, d = a + 1, a + M + 2, a + M + 1
    tris = np.stack([a, b, c, a, c, d], axis=-1).reshape(-1)
    return pos.reshape(-1, 3), nrm.reshape(-1, 3).astype(np.float32), col, uv, tris.astype(np.uint32), M


# ---------- texture ----------

def register_ltex(ltex_map, rec):
    """Ajoute un record LandscapeTexture : cle (plugin, index), plus (None, index) = dernier plugin (repli)."""
    idx = int(rec.get("index", 0))
    name = rec.get("file_name", "")
    ltex_map[(rec.get("_plugin"), idx)] = name
    ltex_map[(None, idx)] = name


class LandTextures:
    """Bake de la texture d'une cell (melange facon moteur des couches de terrain).
    ltex_map : {(plugin, index): fichier} (voir register_ltex). Les indices d'un LAND
    se resolvent dans les LTEX du plugin qui contient ce LAND, comme dans le moteur."""

    def __init__(self, ltex_map, texture_index, tile_px):
        self.ltex_map = ltex_map
        self.index = texture_index
        self.T = tile_px
        self.layers = {}

    def _layer(self, vtex, plugin=None):
        key = (plugin, vtex)
        if key in self.layers:
            return self.layers[key]
        T = self.T
        if vtex == 0:
            name = "_land_default.dds"
        else:
            name = self.ltex_map.get((plugin, vtex - 1)) or self.ltex_map.get((None, vtex - 1), "")
        px = None
        path = self.index.find_texture(name) if name else None
        if path:
            try:
                from PIL import Image
                img = Image.open(path)
                img.load()
                img = img.convert("RGBA").resize((T, T), Image.BILINEAR)
                px = np.flipud(np.asarray(img, dtype=np.float32) / 255.0)     # lignes de bas en haut
            except Exception:
                px = None
        self.layers[key] = px
        return px

    @staticmethod
    def _bilinear(A, T):
        S = 16 * T
        c = (np.arange(S, dtype=np.float32) + 0.5) / T
        i0 = np.minimum(c.astype(np.int32), 15)
        f = (c - i0).astype(np.float32)
        y0, x0 = i0[:, None], i0[None, :]
        fy, fx = f[:, None], f[None, :]
        a00, a01 = A[y0, x0], A[y0, x0 + 1]
        a10, a11 = A[y0 + 1, x0], A[y0 + 1, x0 + 1]
        return (a00 * (1 - fx) + a01 * fx) * (1 - fy) + (a10 * (1 - fx) + a11 * fx) * fy

    def bake(self, ti, plugin=None):
        """ti : grille 16x16 -> (16T, 16T, 4) float32, lignes de bas en haut."""
        T = self.T
        vals, counts = np.unique(ti, return_counts=True)
        # la couche la plus etendue sert de base, les plus petites sont peintes par-dessus
        order = sorted(zip(vals.tolist(), counts.tolist()), key=lambda vc: (-vc[1], vc[0]))
        out = None
        for vt, _ in order:
            tex = self._layer(vt, plugin)
            if tex is None:
                tex = np.empty((T, T, 4), dtype=np.float32)
                tex[:] = DEFAULT_COLOR
            tiled = np.tile(tex, (16, 16, 1))
            if out is None:
                out = tiled
                continue
            A = np.zeros((17, 17), dtype=np.float32)
            ys, xs = np.nonzero(ti == vt)
            for ty, tx in zip(ys, xs):
                A[ty:ty + 2, tx:tx + 2] = 1.0
            alpha = self._bilinear(A, T)[..., None]
            out = out + (tiled - out) * alpha
        out[..., 3] = 1.0
        return out

"""Recherche des fichiers de Morrowind (meshes, textures) et conversion des textures en PNG.

Python pur (numpy + Pillow), sans Blender. Les dossiers sont donnes par priorite decroissante
(mods d'abord, cache BSA en dernier) : le premier fichier trouve gagne, sans tenir compte de la casse.
"""
import os
import zlib
from concurrent.futures import ProcessPoolExecutor

TEX_EXTS = (".dds", ".tga", ".png", ".bmp", ".jpg")


def _norm(rel):
    return rel.replace("\\", "/").lstrip("/").lower()


class AssetIndex:
    """Index insensible a la casse d'une liste de dossiers racines."""

    def __init__(self, dirs):
        self.exact = {}       # 'architecture/x.nif' -> chemin reel
        self.stem = {}        # 'architecture/x' -> chemin reel (extension quelconque)
        for root in dirs:
            if not os.path.isdir(root):
                continue
            root = os.path.abspath(root)
            for base, _dirs, files in os.walk(root):
                rel_dir = os.path.relpath(base, root)
                rel_dir = "" if rel_dir == "." else rel_dir.replace(os.sep, "/").lower() + "/"
                for f in files:
                    key = rel_dir + f.lower()
                    full = os.path.join(base, f)
                    self.exact.setdefault(key, full)
                    self.stem.setdefault(os.path.splitext(key)[0], full)

    def find(self, rel):
        """Chemin reel de `rel` (extension exacte d'abord, puis n'importe quelle extension)."""
        key = _norm(rel)
        return self.exact.get(key) or self.stem.get(os.path.splitext(key)[0])

    def find_texture(self, rel):
        """Texture d'un NIF/LTEX. Certains mods (Tamriel Rebuilt...) ecrivent 'textures\\tr\\x.dds' :
        le prefixe 'textures/' est retire, comme le fait le moteur."""
        key = _norm(rel)
        for k in (key, key[len("textures/"):] if key.startswith("textures/") else None):
            if not k:
                continue
            hit = self.exact.get(k) or self.stem.get(os.path.splitext(k)[0])
            if hit:
                return hit
        return None


def texture_out_name(src_path, side):
    """Nom de PNG stable et sans collision pour un fichier source."""
    stem = os.path.splitext(os.path.basename(src_path))[0].lower()
    # deux textures de meme nom dans des dossiers differents : on suffixe avec un hash du chemin
    h = format(zlib.crc32(os.path.dirname(src_path).lower().encode()) & 0xFFFFF, "05x")
    return f"{stem}_{h}_{side}.png"


def _convert_one(args):
    src, dst, max_side = args
    try:
        if os.path.exists(dst) and os.path.getmtime(dst) >= os.path.getmtime(src):
            return None
        from PIL import Image
        img = Image.open(src)
        img.load()
        img = img.convert("RGBA")
        w, h = img.size
        while max(w, h) > max_side and min(w, h) > 1:
            w, h = max(w // 2, 1), max(h // 2, 1)
        if (w, h) != img.size:
            img = img.resize((w, h), Image.BILINEAR)
        # PNG sans canal alpha quand il est inutile : fichiers plus petits
        if img.getchannel("A").getextrema() == (255, 255):
            img = img.convert("RGB")
        img.save(dst, optimize=False)
        return None
    except Exception as e:           # texture corrompue / format inconnu : le mesh reste sans texture
        return f"{src}: {e}"


def convert_textures(jobs, workers):
    """jobs : [(src, dst, max_side)] -> liste d'erreurs. Parallele."""
    jobs = list(jobs)
    errors = []
    if not jobs:
        return errors
    if workers <= 1 or len(jobs) < 8:
        errors = [e for e in map(_convert_one, jobs) if e]
    else:
        with ProcessPoolExecutor(max_workers=workers) as ex:
            errors = [e for e in ex.map(_convert_one, jobs, chunksize=16) if e]
    return errors

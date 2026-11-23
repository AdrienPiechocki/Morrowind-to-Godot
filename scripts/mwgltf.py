"""Ecriture de glTF (.gltf + .bin) en Python pur.

Repere : Morrowind (X, Y, Z haut, unites) -> glTF (X, Z, -Y, metres) via C = [[1,0,0],[0,0,1],[0,-1,0]].
Le maillage est converti une fois (sommets dans le repere glTF) ; chaque noeud porte la transformation
de placement, exprimee dans le meme repere.
"""
import json
import os

import numpy as np

SCALE = 0.01
C = np.array([[1, 0, 0], [0, 0, 1], [0, -1, 0]], dtype=np.float64)


def euler_to_matrix(rx, ry, rz):
    """Rotation d'une reference Morrowind : Rx(-rx) Ry(-ry) Rz(-rz) (signes du pipeline Blender)."""
    rx, ry, rz = -rx, -ry, -rz
    cx, sx, cy, sy, cz, sz = np.cos(rx), np.sin(rx), np.cos(ry), np.sin(ry), np.cos(rz), np.sin(rz)
    Rx = np.array([[1, 0, 0], [0, cx, -sx], [0, sx, cx]])
    Ry = np.array([[cy, 0, sy], [0, 1, 0], [-sy, 0, cy]])
    Rz = np.array([[cz, -sz, 0], [sz, cz, 0], [0, 0, 1]])
    return Rx @ Ry @ Rz


def matrix_to_quat(m):
    """Quaternion (x, y, z, w) d'une rotation 3x3."""
    t = m[0, 0] + m[1, 1] + m[2, 2]
    if t > 0:
        s = np.sqrt(t + 1.0) * 2
        q = ((m[2, 1] - m[1, 2]) / s, (m[0, 2] - m[2, 0]) / s, (m[1, 0] - m[0, 1]) / s, 0.25 * s)
    elif m[0, 0] > m[1, 1] and m[0, 0] > m[2, 2]:
        s = np.sqrt(1.0 + m[0, 0] - m[1, 1] - m[2, 2]) * 2
        q = (0.25 * s, (m[0, 1] + m[1, 0]) / s, (m[0, 2] + m[2, 0]) / s, (m[2, 1] - m[1, 2]) / s)
    elif m[1, 1] > m[2, 2]:
        s = np.sqrt(1.0 + m[1, 1] - m[0, 0] - m[2, 2]) * 2
        q = ((m[0, 1] + m[1, 0]) / s, 0.25 * s, (m[1, 2] + m[2, 1]) / s, (m[0, 2] - m[2, 0]) / s)
    else:
        s = np.sqrt(1.0 + m[2, 2] - m[0, 0] - m[1, 1]) * 2
        q = ((m[0, 2] + m[2, 0]) / s, (m[1, 2] + m[2, 1]) / s, 0.25 * s, (m[1, 0] - m[0, 1]) / s)
    q = np.array(q, dtype=np.float64)
    return (q / np.linalg.norm(q)).tolist()


def ref_node(translation, rotation, scale):
    """(translation, rotation quat, scale) glTF d'une reference Morrowind (position MW en unites)."""
    R = euler_to_matrix(*rotation)
    t = C @ (np.asarray(translation, dtype=np.float64) * SCALE)
    Rg = C @ R @ C.T
    return t.tolist(), matrix_to_quat(Rg), float(scale)


class GltfBuilder:
    def __init__(self, tex_prefix="textures/"):
        self.tex_prefix = tex_prefix        # chemin des textures relatif au .gltf
        self.bin = bytearray()
        self.views, self.accessors = [], []
        self.meshes, self.nodes = [], []
        self.materials, self._mat_ids = [], {}
        self.textures, self.images, self._tex_ids = [], [], {}
        self.texture_sources = {}        # uri -> nom NIF d'origine (pour la conversion)

    # ---- tampon ----
    def _view(self, arr, target=None):
        pad = (-len(self.bin)) % 4
        self.bin += b"\0" * pad
        view = {"buffer": 0, "byteOffset": len(self.bin), "byteLength": arr.nbytes}
        if target:
            view["target"] = target
        self.bin += arr.tobytes()
        self.views.append(view)
        return len(self.views) - 1

    def _accessor(self, arr, ctype, atype, target, minmax=False, normalized=False):
        a = {"bufferView": self._view(arr, target), "componentType": ctype,
             "count": int(len(arr)), "type": atype}
        if minmax:
            a["min"] = arr.min(axis=0).astype(float).tolist()
            a["max"] = arr.max(axis=0).astype(float).tolist()
        if normalized:
            a["normalized"] = True
        self.accessors.append(a)
        return len(self.accessors) - 1

    # ---- materiaux ----
    def texture(self, uri):
        if uri not in self._tex_ids:
            self.images.append({"uri": uri})
            self.textures.append({"source": len(self.images) - 1})
            self._tex_ids[uri] = len(self.textures) - 1
        return self._tex_ids[uri]

    def material(self, key, name, texture_uri, alpha, cutoff, double_sided, color):
        if key in self._mat_ids:
            return self._mat_ids[key]
        pbr = {"baseColorFactor": [float(c) for c in color], "metallicFactor": 0.0, "roughnessFactor": 1.0}
        if texture_uri:
            pbr["baseColorTexture"] = {"index": self.texture(texture_uri)}
        m = {"name": name, "pbrMetallicRoughness": pbr, "doubleSided": bool(double_sided)}
        if alpha != "OPAQUE":
            m["alphaMode"] = alpha
            if alpha == "MASK":
                m["alphaCutoff"] = float(cutoff)
        self.materials.append(m)
        self._mat_ids[key] = len(self.materials) - 1
        return self._mat_ids[key]

    # ---- geometrie ----
    def primitive(self, pos_mw, nrm_mw, uv, col, idx, material_id):
        """Sommets en repere Morrowind (unites) -> primitive glTF (repere glTF, metres)."""
        pos = (pos_mw.astype(np.float64) @ C.T * SCALE).astype(np.float32)
        attrs = {"POSITION": self._accessor(pos, 5126, "VEC3", 34962, minmax=True)}
        if nrm_mw is not None:
            attrs["NORMAL"] = self._accessor((nrm_mw.astype(np.float64) @ C.T).astype(np.float32),
                                             5126, "VEC3", 34962)
        if uv is not None:
            attrs["TEXCOORD_0"] = self._accessor(np.ascontiguousarray(uv, dtype=np.float32), 5126, "VEC2", 34962)
        if col is not None:
            attrs["COLOR_0"] = self._accessor(
                (np.clip(col, 0, 1) * 255.0 + 0.5).astype(np.uint8), 5121, "VEC4", 34962, normalized=True)
        if len(pos) < 65536:
            ia = self._accessor(idx.astype(np.uint16), 5123, "SCALAR", 34963)
        else:
            ia = self._accessor(idx.astype(np.uint32), 5125, "SCALAR", 34963)
        prim = {"attributes": attrs, "indices": ia, "mode": 4}
        if material_id is not None:
            prim["material"] = material_id
        return prim

    def add_mesh(self, name, primitives):
        self.meshes.append({"name": name, "primitives": primitives})
        return len(self.meshes) - 1

    def add_node(self, mesh, name=None, translation=None, rotation=None, scale=1.0):
        n = {"mesh": mesh}
        if name:
            n["name"] = name
        if translation is not None:
            n["translation"] = translation
        if rotation is not None and rotation != [0.0, 0.0, 0.0, 1.0]:
            n["rotation"] = rotation
        if abs(scale - 1.0) > 1e-6:
            n["scale"] = [scale, scale, scale]
        self.nodes.append(n)

    # ---- sortie ----
    def write(self, path_without_ext):
        base = os.path.basename(path_without_ext)
        doc = {
            "asset": {"version": "2.0", "generator": "Morrowind-to-Godot (direct)"},
            "scene": 0,
            "scenes": [{"nodes": list(range(len(self.nodes)))}],
            "nodes": self.nodes,
            "meshes": self.meshes,
            "materials": self.materials,
            "accessors": self.accessors,
            "bufferViews": self.views,
            "buffers": [{"uri": base + ".bin", "byteLength": len(self.bin)}],
        }
        if self.textures:
            doc["textures"] = self.textures
            doc["images"] = self.images
        with open(path_without_ext + ".bin", "wb") as f:
            f.write(self.bin)
        with open(path_without_ext + ".gltf", "w", encoding="utf-8") as f:
            json.dump(doc, f, separators=(",", ":"))

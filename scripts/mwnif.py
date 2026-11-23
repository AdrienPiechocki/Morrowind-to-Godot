"""NIF -> geometrie, sans Blender (bibliotheque es3 de l'addon io_scene_mw).

load_model(path) retourne un Model : primitives (positions/normales/uv/couleurs/indices, deja
transformees dans le repere de la racine du NIF, en unites Morrowind) regroupees par materiau.
Les collisions, ombres, marqueurs et noeuds caches sont ignores.
"""
import os
import sys

import numpy as np

_LIB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "io_scene_mw", "lib")
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)
from es3 import nif  # noqa: E402

SKIP_NAME_PARTS = ("collision", "shadow", "marker", "bounding box", "bound box")


class Material:
    """Cle de materiau glTF (hashable)."""
    __slots__ = ("texture", "alpha", "cutoff", "double_sided", "color", "vertex_colors")

    def __init__(self, texture, alpha, cutoff, double_sided, color, vertex_colors):
        self.texture = texture                # nom de fichier NIF ('' = aucune)
        self.alpha = alpha                    # 'OPAQUE' | 'MASK' | 'BLEND'
        self.cutoff = cutoff
        self.double_sided = double_sided
        self.color = color                    # (r, g, b, a)
        self.vertex_colors = vertex_colors

    def key(self):
        return (self.texture.lower(), self.alpha, round(self.cutoff, 3), self.double_sided,
                tuple(round(c, 3) for c in self.color), self.vertex_colors)


class Primitive:
    __slots__ = ("pos", "nrm", "uv", "col", "idx", "material")


class Model:
    __slots__ = ("prims", "size", "tris", "lo", "hi", "collision")

    def __init__(self, prims, collision=None):
        self.prims = prims
        self.collision = collision          # (positions (n,3) float32, indices uint32) ou None
        self.tris = sum(len(p.idx) // 3 for p in prims)
        if prims:
            self.lo = np.min([p.pos.min(axis=0) for p in prims], axis=0)
            self.hi = np.max([p.pos.max(axis=0) for p in prims], axis=0)
            self.size = float((self.hi - self.lo).max())
        else:
            self.lo = self.hi = np.zeros(3)
            self.size = 0.0


def _material(shape, has_vcol):
    props = shape.properties
    get = lambda cls: next((p for p in props if isinstance(p, cls)), None)
    tex, mat, alpha, stencil = (get(nif.NiTexturingProperty), get(nif.NiMaterialProperty),
                                get(nif.NiAlphaProperty), get(nif.NiStencilProperty))
    name, uv_set = "", 0
    if tex is not None and tex.base_texture is not None and tex.base_texture.source is not None:
        name = tex.base_texture.source.filename or ""
        uv_set = int(tex.base_texture.uv_set)
    mode, cutoff = "OPAQUE", 0.5
    if alpha is not None:
        if alpha.alpha_testing:
            mode, cutoff = "MASK", float(alpha.test_ref) / 255.0
        elif alpha.alpha_blending:
            mode = "BLEND"
    color = (1.0, 1.0, 1.0, 1.0)
    if mat is not None:
        d = mat.diffuse_color
        color = (float(d[0]), float(d[1]), float(d[2]), float(mat.alpha))
        if mode == "OPAQUE" and mat.alpha < 0.999:
            mode = "BLEND"
    return Material(name, mode, cutoff, stencil is not None, color, has_vcol), uv_set


def _fix_polar_fan(p):
    """Tapis ronds : le centre est un eventail (un sommet central a uv = (0.5, 1), l'anneau interne a v = 0) dont
    CHAQUE secteur plaque la texture entiere. Le centre se pince en une etoile de stries, et le filtrage de la
    texture (mipmaps) y change avec la position. On le remplace par un mappage plan de tout le disque central ;
    les sommets sont dupliques, car l'anneau est partage avec la couronne (qui garde son mappage radial)."""
    uv = p.uv
    cands = np.where((np.abs(uv[:, 0] - 0.5) < 1e-3) & (np.abs(uv[:, 1] - 1.0) < 1e-3))[0]
    if not len(cands):
        return
    idx = p.idx.reshape(-1, 3).copy()
    pos, nrm, col = p.pos, p.nrm, p.col
    for c in cands:
        fan = np.where((idx == c).any(axis=1))[0]
        if len(fan) < 6:
            continue
        ring = np.unique(idx[fan])
        ring = ring[ring != c]
        if not np.all(np.abs(uv[ring, 1]) < 1e-3):
            continue
        cx, cy = float(pos[c, 0]), float(pos[c, 1])
        R = float(np.hypot(pos[ring, 0] - cx, pos[ring, 1] - cy).max())
        if R < 1e-3:
            continue
        ids = np.concatenate([[c], ring])
        base = len(pos)
        remap = {int(v): base + k for k, v in enumerate(ids)}
        newp = pos[ids]
        new_uv = np.stack([(newp[:, 0] - cx) / (2 * R) + 0.5, (newp[:, 1] - cy) / (2 * R) + 0.5], axis=1).astype(np.float32)
        for t_i in fan:
            idx[t_i] = [remap[int(v)] for v in idx[t_i]]
        pos = np.concatenate([pos, newp])
        nrm = np.concatenate([nrm, p.nrm[ids]])
        uv = np.concatenate([uv, new_uv])
        if col is not None:
            col = np.concatenate([col, p.col[ids]])
        p.pos, p.nrm, p.uv, p.col = pos, nrm, uv, col
    p.idx = idx.ravel().astype(np.uint32)


def _is_junk(node):
    if node.app_culled or isinstance(node, (nif.RootCollisionNode, nif.AvoidNode)):
        return True
    n = (node.name or "").lower()
    return any(part in n for part in SKIP_NAME_PARTS)


def _collect_collision(node, world, out):
    if isinstance(node, nif.NiTriShape):
        out.append((node, world))
    for c in node.children:
        if c is not None:
            _collect_collision(c, world @ c.matrix, out)


def _collect(node, world, out, col_out=None):
    if isinstance(node, nif.RootCollisionNode):
        if col_out is not None:
            _collect_collision(node, world, col_out)
        return
    if _is_junk(node):
        return
    if isinstance(node, nif.NiTriShape):
        out.append((node, world))
        return
    if isinstance(node, nif.NiSwitchNode):
        i = node.active_index
        kids = [c for c in node.children]
        children = [kids[i]] if 0 <= i < len(kids) and kids[i] is not None else []
    else:
        children = node.children
    for c in children:
        if c is not None:
            _collect(c, world @ c.matrix, out, col_out)


def load_model(path, collision=False):
    """Model depuis un fichier NIF, ou None s'il est illisible / sans geometrie.
    collision=True : garde aussi la geometrie de collision (RootCollisionNode)."""
    try:
        data = nif.NiStream()
        data.load(path)
        data.merge_properties()
        roots = list(data.roots_of_type(nif.NiAVObject))
    except Exception:
        return None
    shapes, col_shapes = [], ([] if collision else None)
    for root in roots:
        # comme l'import Blender : un seul noeud racine -> sa transformation est ignoree
        m = np.eye(4) if (len(roots) == 1 and isinstance(root, nif.NiNode)) else root.matrix
        _collect(root, m, shapes, col_shapes)

    groups = {}      # cle de materiau -> (Material, [Primitive partielles])
    for shape, world in shapes:
        d = shape.data
        if d is None or not len(d.vertices) or not len(getattr(d, "triangles", ())):
            continue
        if getattr(shape, "skin", None) is not None and shape.skin.data is not None:
            continue        # pieces skinnees (NPC) : hors statique
        v = d.vertices.astype(np.float64)
        pos = (v @ world[:3, :3].T + world[:3, 3]).astype(np.float32)
        nrm = None
        if len(d.normals) == len(v):
            r = world[:3, :3]
            n = d.normals.astype(np.float64) @ np.linalg.inv(r).T       # inverse-transposee
            ln = np.linalg.norm(n, axis=1, keepdims=True)
            nrm = (n / np.where(ln > 1e-9, ln, 1.0)).astype(np.float32)
        has_vcol = len(d.vertex_colors) == len(v)
        material, uv_set = _material(shape, has_vcol)
        uv = None
        if len(d.uv_sets):
            uv = d.uv_sets[min(uv_set, len(d.uv_sets) - 1)].astype(np.float32)
        col = d.vertex_colors.astype(np.float32) if has_vcol else None
        tri = d.triangles.astype(np.uint32)
        if np.linalg.det(world[:3, :3]) < 0:        # miroir : conserver l'orientation des faces
            tri = tri[:, ::-1]
        groups.setdefault(material.key(), (material, []))[1].append((pos, nrm, uv, col, tri.ravel()))

    prims = []
    for material, parts in groups.values():
        p = Primitive()
        offs, base = [], 0
        for part in parts:
            offs.append(base)
            base += len(part[0])
        p.pos = np.concatenate([x[0] for x in parts])
        n_all = [x[1] if x[1] is not None else np.tile(np.float32([0, 0, 1]), (len(x[0]), 1)) for x in parts]
        p.nrm = np.concatenate(n_all)
        p.uv = np.concatenate([x[2] if x[2] is not None else np.zeros((len(x[0]), 2), np.float32) for x in parts])
        p.col = (np.concatenate([x[3] for x in parts]) if material.vertex_colors else None)
        p.idx = np.concatenate([x[4] + o for x, o in zip(parts, offs)]).astype(np.uint32)
        p.material = material
        prims.append(p)
    for p in prims:
        _fix_polar_fan(p)
    col = None
    if col_shapes:
        cp, ci, base = [], [], 0
        for shape, world in col_shapes:
            d = shape.data
            if d is None or not len(d.vertices) or not len(getattr(d, "triangles", ())):
                continue
            cp.append((d.vertices.astype(np.float64) @ world[:3, :3].T + world[:3, 3]).astype(np.float32))
            ci.append(d.triangles.astype(np.uint32).ravel() + base)
            base += len(d.vertices)
        if cp:
            col = (np.concatenate(cp), np.concatenate(ci))
    model = Model(prims, col)
    return model if (prims or col) else None

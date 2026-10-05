"""Blender version compatibility helpers shared by the pipeline scripts (needs bpy).

Blender 5.0 deprecated Material.use_nodes (removal planned in 6.0): it is now a no-op
that always returns True and emits a DeprecationWarning on both read and write, and
every material already has a node tree. Before 5.0, a material created with
bpy.data.materials.new() has no node tree until use_nodes is set to True.

These helpers only touch use_nodes on versions where it is still needed.
"""
import bpy

NODES_ALWAYS = bpy.app.version >= (5, 0, 0)


def ensure_nodes(mat):
    """Make sure `mat` has a node tree and return it (None if impossible, e.g. a grease
    pencil material). Replaces `mat.use_nodes = True`."""
    if mat is None:
        return None
    if not NODES_ALWAYS and not mat.use_nodes:
        mat.use_nodes = True
    return mat.node_tree


def has_nodes(mat):
    """True if `mat` exists and has a usable node tree. Replaces
    `mat.use_nodes and mat.node_tree`."""
    if not mat or mat.node_tree is None:
        return False
    return NODES_ALWAYS or bool(mat.use_nodes)

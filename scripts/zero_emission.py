"""Passe finale : emission des materiaux a 0 dans un .blend deja genere.

Usage : blender --background --python scripts/6b-zero_emission.py -- [--blend morrowind.blend]
Lance apres 4-no_lube / 5-cleanup / 6-set_collision, au cas ou l'un d'eux recree ou
modifie des materiaux, et avant l'export GLB.
"""
import os
import sys
import bpy

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mwlog import vprint, eprint, Progress


def get_arg(name, default=None):
    args = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    for i, a in enumerate(args):
        if a == f"--{name}" and i + 1 < len(args):
            return args[i + 1]
    return default


def zero_tree(nt, seen):
    if nt is None or nt.as_pointer() in seen:
        return 0
    seen.add(nt.as_pointer())
    count = 0
    for node in nt.nodes:
        if node.type == 'GROUP':
            count += zero_tree(node.node_tree, seen)
            continue
        if node.type == 'BSDF_PRINCIPLED':
            names, strength = ("Emission Color", "Emission"), "Emission Strength"
        elif node.type == 'EMISSION':
            names, strength = ("Color",), "Strength"
        else:
            continue
        for nm in names:
            sock = node.inputs.get(nm)
            if sock is None:
                continue
            if sock.is_linked:
                if sock.links[0].from_node.type == 'TEX_IMAGE':
                    st = node.inputs.get(strength)
                    if st is not None and not st.is_linked:
                        st.default_value = 0.0
                    count += 1
                    continue
                for lk in list(sock.links):
                    nt.links.remove(lk)
            sock.default_value = (0.0, 0.0, 0.0, 1.0)
            count += 1
    return count


path = os.path.abspath(get_arg("blend", "morrowind.blend"))
bpy.ops.wm.open_mainfile(filepath=path)
vprint(f"[+] Loaded: {path}")

seen = set()
total = 0
_mats = list(bpy.data.materials)
with Progress(len(_mats), "Emission", disable=not _mats) as _bar:
    for m in _mats:
        total += zero_tree(m.node_tree, seen)
        _bar.update()
vprint(f"[+] Emission zeroed: {total} entry/entries across {len(bpy.data.materials)} material(s)")

bpy.ops.wm.save_as_mainfile(filepath=path, compress=True)
vprint(f"[+] Saved: {path}")

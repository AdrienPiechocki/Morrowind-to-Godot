#!/usr/bin/env python3
"""Densify sparse accessors in a GLB so strict importers (Godot) accept it."""
import json, struct, sys, shutil

COMP_SIZE = {5120:1, 5121:1, 5122:2, 5123:2, 5125:4, 5126:4}
NUM_COMP = {"SCALAR":1, "VEC2":2, "VEC3":3, "VEC4":4,
            "MAT2":4, "MAT3":9, "MAT4":16}

def read_accessor_raw(gltf, bin_buf, acc):
    """Return list of raw little-endian element tuples (ints or floats as packed bytes kept simple via struct)."""
    comp = acc["componentType"]
    ncomp = NUM_COMP[acc["type"]]
    csz = COMP_SIZE[comp]
    fmt_char = {5120:"b",5121:"B",5122:"h",5123:"H",5125:"I",5126:"f"}[comp]
    count = acc["count"]
    if "bufferView" not in acc:
        return [(0,) * ncomp for _ in range(count)]
    bv = gltf["bufferViews"][acc["bufferView"]]
    base = bv.get("byteOffset", 0) + acc.get("byteOffset", 0)
    stride = bv.get("byteStride") or csz * ncomp
    out = []
    for i in range(count):
        off = base + i * stride
        vals = struct.unpack_from("<" + fmt_char * ncomp, bin_buf, off)
        out.append(vals)
    return out

def main(path):
    with open(path, "rb") as f:
        data = f.read()
    magic, ver, length = struct.unpack_from("<III", data, 0)
    assert magic == 0x46546C67
    json_len, json_type = struct.unpack_from("<II", data, 12)
    gltf = json.loads(data[20:20+json_len])
    bin_off = 20 + json_len
    bin_len, bin_type = struct.unpack_from("<II", data, bin_off)
    bin_buf = bytearray(data[bin_off+8 : bin_off+8+bin_len])

    buffers = gltf["buffers"]
    assert len(buffers) == 1, "script handles single-buffer GLBs"
    views = gltf["bufferViews"]
    fixed = 0

    for ai, acc in enumerate(gltf.get("accessors", [])):
        sp = acc.get("sparse")
        if not sp:
            continue
        base_vals = read_accessor_raw(gltf, bin_buf, acc)
        idx_acc = {"bufferView": sp["indices"]["bufferView"],
                   "byteOffset": sp["indices"].get("byteOffset", 0),
                   "componentType": sp["indices"]["componentType"],
                   "count": sp["count"], "type": "SCALAR"}
        indices = [v[0] for v in read_accessor_raw(gltf, bin_buf, idx_acc)]
        val_acc = {"bufferView": sp["values"]["bufferView"],
                   "byteOffset": sp["values"].get("byteOffset", 0),
                   "componentType": acc["componentType"],
                   "count": sp["count"], "type": acc["type"]}
        values = read_accessor_raw(gltf, bin_buf, val_acc)
        for i, v in zip(indices, values):
            base_vals[i] = v

        # Append dense data as a new bufferView
        while len(bin_buf) % 4:
            bin_buf.append(0)
        offset = len(bin_buf)
        payload = b"".join(struct.pack("<" + "{fmt}"*len(v), *v)
                           for v in base_vals) if False else None
        fmt_char = {5120:"b",5121:"B",5122:"h",5123:"H",5125:"I",5126:"f"}[acc["componentType"]]
        flat = []
        for v in base_vals:
            flat.extend(v)
        payload = struct.pack("<" + fmt_char * len(flat), *flat)
        bin_buf += payload
        views.append({"buffer": 0, "byteOffset": offset,
                      "byteLength": len(payload)})
        del acc["sparse"]
        acc["bufferView"] = len(views) - 1
        acc.pop("byteOffset", None)
        fixed += 1
        print(f"fixed accessor {ai}: {sp['count']} sparse entries densified "
              f"({acc['count']} x {acc['type']})")

    buffers[0]["byteLength"] = len(bin_buf)

    json_bytes = json.dumps(gltf, separators=(",", ":")).encode()
    while len(json_bytes) % 4:
        json_bytes += b" "
    while len(bin_buf) % 4:
        bin_buf.append(0)
    total = 20 + len(json_bytes) + 8 + len(bin_buf)
    with open(path, "wb") as f:
        f.write(struct.pack("<III", magic, ver, total))
        f.write(struct.pack("<II", len(json_bytes), 0x4E4F534A))
        f.write(json_bytes)
        f.write(struct.pack("<II", len(bin_buf), 0x004E4942))
        f.write(bin_buf)
    print(f"OK: {fixed} sparse accessor(s) rewritten -> {path}")

if __name__ == "__main__":
    main(sys.argv[1])

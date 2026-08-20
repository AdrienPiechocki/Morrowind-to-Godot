# Morrowind to Godot

Convert Morrowind game assets (meshes, textures, cells) into Godot Engine-ready 3D scenes via Blender.

## How It Works

The pipeline extracts Morrowind data files, reconstructs selected cells in Blender using the [io_scene_mw](https://github.com/Greatness7/io_scene_mw) addon, and exports a `.glb` file importable by Godot.

```
Morrowind Data Files (.bsa, .esm, .esp)
    │
    ├── bsatool ──────► Extracted meshes, textures, sounds
    ├── tes3conv ─────► JSON (cell/object data)
    │
    ▼
Blender (headless, via io_scene_mw)
    │
    ├── Import NIF meshes
    ├── Reconstruct cell (position, rotation, scale)
    ├── Convert TGA → PNG
    ├── Rebuild materials (Principled BSDF)
    ├── Remove specularity
    ├── Hide collision meshes
    ├── Rename meshes with -col suffix
    │
    ▼
morrowind.glb ──► Godot Engine
```

## Prerequisites

- **Blender** 3.6+ (with `bpy` module available in CLI)
- **Python** 3.x
- **curl**, **unzip**, **tar** (for installation)
- **The Elder Scrolls III: Morrowind** installed (for data files)

## Installation

```bash
./install.sh
```

This script will:
1. Download **tes3conv** v0.4.1 (ESM/ESP to JSON converter)
2. Download **OpenMW bsatool** 0.51.0 (BSA archive extractor)
3. Clone the **io_scene_mw** Blender addon
4. Symlink the addon into your Blender addons directory

## Configuration

Edit `config.json` to point to your Morrowind `Data Files` directory:

```json
{
  "morrowind_data": "/path/to/Morrowind/Data Files/"
}
```

On first run, if `config.json` is missing, you will be prompted to enter the path interactively.

## Usage

```bash
./run.sh
```

The script guides you through the conversion interactively:

1. **Extract BSA archives** — Select which `.bsa` files to unpack
2. **Extract mod assets** — Automatically processes `.zip`/`.7z`/`.rar` mods from `mods/`
3. **Convert plugins** — Converts `Morrowind.esm` to JSON, optionally merges extra plugins
4. **Select a cell** — Enter a Morrowind cell name (e.g. `Balmora, Temple`)
5. **Blender processing** — Automatically runs 7 processing scripts
6. **Export** — Outputs `morrowind.glb`

### Adding Mods

Place mod archives (`.zip`, `.7z`, `.rar`) in the `mods/` directory. The pipeline will automatically extract meshes, textures, and plugin files into `data/`.

## Blender Processing Pipeline

The following scripts run sequentially in Blender background mode:

| Script | Purpose |
|--------|---------|
| `1-generate_blend.py` | Parse JSON, import NIF meshes, reconstruct cell |
| `2-tga_to_png.py` | Convert TGA textures to PNG for glTF compatibility |
| `3-rebuild_mat.py` | Replace NIF shader nodes with Principled BSDF |
| `4-no_lube.py` | Set roughness=1.0, specular=0.0 (flat Morrowind look) |
| `5-cleanup.py` | Hide collision/shadow objects, remove junk |
| `6-set_collision.py` | Append `-col` suffix for Godot trimesh collision |
| `7-export_glb.py` | Export scene to GLB format |

## Project Structure

```
.
├── install.sh              # Downloads tools and sets up addon
├── run.sh                  # Main conversion pipeline
├── config.json             # Morrowind data path config
├── tes3conv                # ESM/ESP to JSON converter
├── openmw-tools/           # bsatool + shared libraries
├── data/                   # Extracted game assets
│   ├── meshes/
│   ├── textures/
│   └── *.esm, *.ESP
├── mods/                   # Mod archives (.zip, .7z, .rar)
├── scripts/                # Blender Python processing scripts
├── io_scene_mw/            # Blender NIF import/export addon
└── morrowind.glb           # Final output (Godot-ready)
```

## Godot Integration

The exported `morrowind.glb` can be directly imported into Godot 4.x. Mesh names are suffixed with `-col` so Godot automatically generates trimesh collision shapes on import.

## Credits

- [io_scene_mw](https://github.com/Greatness7/io_scene_mw) by Greatness7 — Blender addon for Morrowind NIF files
- [tes3conv](https://github.com/Greatness7/tes3conv) by Greatness7 — Morrowind ESM/ESP to JSON converter
- [OpenMW bsatool](https://openmw.org/) — BSA archive extraction tool

## License

The `io_scene_mw` addon is licensed under GPL-3.0. Other project scripts have no explicit license.

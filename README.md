# Morrowind-to-Godot

Converts cells from **The Elder Scrolls III: Morrowind** (interiors, exteriors, animated NPCs/creatures) into **`.blend`** and **`.glb`** files, ready to import into **Godot**.

The pipeline reads your game data (through `openmw.cfg` or a *Data Files* folder), merges plugins in load order, rebuilds the scenes in Blender (headless, `--background`), cleans them up, adds Godot collision suffixes and exports to GLB.

## Features

- **4 import modes**: `interior`, `npc`, `exterior`, `full`
- Reads **`openmw.cfg`** directly: `data=`, `data-local=`, `content=`, BSA archives, mod load order
- **Plugin merging** cell by cell and reference by reference (an `.esp` no longer overwrites the whole cell)
- **`full` mode**: exterior grid + every interior reachable through its doors + NPCs (`idle` animation), one `.glb` per cell and a `manifest.json` linking the doors
- Morrowind animations (`Seq: Start/Stop` markers) split into **NLA tracks**, so each clip becomes its own animation in the GLB
- Godot collisions (`-col`), matte materials, emission removed
- **`-v` / `--verbose`** option and a **progress bar** on every Python script

## Requirements

| Tool | Purpose |
|---|---|
| Linux / macOS / Windows (bash) | `run.sh` is a bash script |
| **Blender 4.4+** in your `PATH` | scene generation and export (script 7 uses the slotted actions API) |
| **Python 3** | `openmw_cfg.py`, `mwlog.py` |
| [`tes3conv`](https://github.com/Greatness7/tes3conv) | converts `.esm`/`.esp` to JSON |
| `bsatool` (OpenMW tools) | extracts `.bsa` archives |
| `io_scene_mw` | imports Morrowind `.nif` files into Blender |
| `unzip`, `7z`, `unrar` | only for mod extraction in legacy mode |
| A legitimate copy of Morrowind | game data is not included |

`run.sh` expects `./tes3conv`, `./openmw-tools/bsatool` and `./io_scene_mw/` at the project root (see `install.sh`).

## Project layout

```
.
├── run.sh                  # entry point (orchestrates everything)
├── install.sh              # installs tes3conv, bsatool, io_scene_mw
├── config.json             # created automatically (path to openmw.cfg)
├── data/                   # cache: extracted BSAs, plugins, meshes, textures
├── mods/                   # (legacy) mod archives to extract
├── export/                 # full mode output
└── scripts/
    ├── mwlog.py            # shared vprint / eprint / progress bar
    ├── openmw_cfg.py       # reads openmw.cfg, extracts BSAs, merges plugins
    ├── 1-generate_blend.py # builds the Blender scene
    ├── 4-no_lube.py        # materials: roughness 1, specular 0
    ├── 5-cleanup.py        # hides shadows/markers, removes decals and door icons
    ├── 6-set_collision.py  # adds the -col suffix to collision meshes
    ├── 6b-zero_emission.py # final pass: emission set to 0
    └── 7-export_glb.py     # splits animations into NLA tracks and exports the GLB
```

## Usage

```bash
./run.sh                    # progress bar only
./run.sh -v                 # all messages (Python scripts and Blender)
./run.sh --npcs 1           # include NPCs/creatures without prompting
./run.sh --anims idle       # animations to keep (comma-separated prefixes, or "all")
```

| Option | Effect |
|---|---|
| `-v`, `--verbose` | shows every progress message. Without it, only the progress bar and errors are visible |
| `--npcs 0\|1` | include or skip NPCs/creatures in `interior` / `exterior` / `full` scenes |
| `--anims <prefixes\|all>` | animations kept at export (default: `idle` whenever NPCs are present) |
| `MW2G_LEGACY=1` (env var) | forces the old flow: `config.json` + manual BSA/mod extraction |

### Workflow

1. **Data source**: `openmw.cfg` is auto-detected (`~/.config/openmw`, Flatpak, Windows, macOS) and remembered in `config.json`.
2. **Preparation** (`openmw_cfg.py prepare`): extracts BSAs into `data/`, converts plugins with `tes3conv`, merges them in load order and writes `output.json`.
3. **Mode selection** (interactive prompts, see below).
4. **Blender**: `1-generate_blend.py`, then `4`, `5`, `6`, `6b`.
5. **Export**: `7-export_glb.py` produces the `.glb`.
6. **Cleanup**: temporary `output*` files are deleted.

### Import modes

| Mode | Prompt | Result |
|---|---|---|
| `interior` (default) | exact cell name, e.g. `Balmora, Temple` | static decor of an interior |
| `npc` | name or id (partial match ok), empty = every NPC in the cell | NPCs/creatures with body parts and animations |
| `exterior` | grid `x,y` or rectangle `x1,y1:x2,y2`, e.g. `-2,-9` (Seyda Neen) or `-3,-10:-1,-8` | one or several exterior cells |
| `full` | exterior grid | exterior + every interior linked by its doors + NPCs |

If several NPCs match a search, a menu asks which one to import (empty input = the first).

## Output

**`interior`, `npc`, `exterior` modes**

```
morrowind.blend
morrowind.glb
```

**`full` mode**

```
export/
├── exterior.blend / exterior.glb
├── interiors/*.blend / *.glb
├── manifest.json        # doors: where each interior connects
├── files.txt            # list of produced files
└── post.log             # Blender log of the post-processing
```

Import the `.glb` files into Godot. `manifest.json` lets you link exteriors and interiors.

## Godot integration

- Collision meshes get the **`-col`** suffix (Trimesh, precise). For convex shapes (lighter), set `SUFFIXE = "-convcol"` in `scripts/6-set_collision.py`.
- Shadow objects, editor markers, door icons and decals are hidden or removed before export.
- Materials are made matte (roughness 1, specular 0, emission 0) to match Godot's lighting.
- Animations show up as separate clips in the `AnimationPlayer` (one NLA track per sequence, e.g. `Idle2`, `SpellCast_Equip`).

## Verbose and progress

- `vprint` (from `mwlog`) replaces `print` and only prints with `-v`.
- `eprint` always prints (errors, the NPC choice menu).
- Progress bars go through **stderr**, so they stay visible even when `run.sh` silences Blender's stdout outside verbose mode. In a log file (`full` mode, `export/post.log`), they shrink to one line every 10%.

```
[██████████░░░░░░░░░░░░░░░░░░] Interieurs 3/12 > Décor 45/300  15% 0:12 ETA 0:40
```

## Known limitations

- The **legacy** flow (without `openmw.cfg`) merges plugins without per-reference merging, so exterior cells modified by a plugin may be incomplete.
- NPC import is slow, especially in `full` mode.
- The cleanup step deletes every `output*` file in the project root: don't store anything important under that name.

## License

This project contains no Morrowind data. Assets remain the property of Bethesda Softworks; you need to own the game to use this pipeline.

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
| **Blender 4.4+** in your `PATH` | only for NPCs/creatures (`--blender` pipeline); the default conversion does not need it |
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
├── fix_textures.sh         # re-runs textures/materials/GLB export on existing .blend files
├── install.sh              # installs tes3conv, bsatool, io_scene_mw
├── config.json             # created automatically (path to openmw.cfg)
├── data/                   # cache: extracted BSAs, plugins, meshes, textures
├── mods/                   # (legacy) mod archives to extract
├── textures/               # PNGs converted from TGA (export/textures in full mode)
├── export/                 # full mode output
└── scripts/
    ├── mwlog.py            # shared vprint / eprint / progress bar
    ├── mwcompat.py         # Blender version compatibility helpers (Material.use_nodes)
    ├── openmw_cfg.py       # reads openmw.cfg, extracts BSAs, merges plugins
    ├── generate_blend.py   # builds the Blender scene
    ├── tga_to_png.py       # TGA textures -> PNG (written to textures/, not to the mod folders)
    ├── rebuild_mat.py      # rebuilds clean glTF-friendly materials, alpha kept
    ├── no_lube.py          # materials: roughness 1, specular 0
    ├── cleanup.py          # hides shadows/markers, removes decals and door icons
    ├── set_collision.py    # adds the -col suffix to collision meshes
    ├── zero_emission.py    # final pass: emission set to 0
    └── export_glb.py       # splits animations into NLA tracks and exports the GLB
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
4. **Blender**: `generate_blend.py`, then `tga_to_png`, `rebuild_mat`, `no_lube`, `cleanup`, `set_collision`, `zero_emission`. The order matters: `rebuild_mat` resets every material, so `no_lube` and `zero_emission` must run after it.
5. **Export**: `export_glb.py` produces the `.glb`.
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

- Collision meshes get the **`-col`** suffix (Trimesh, precise). For convex shapes (lighter), set `SUFFIXE = "-convcol"` in `scripts/set_collision.py`.
- Shadow objects, editor markers, door icons and decals are hidden or removed before export.
- Materials are rebuilt as plain glTF-friendly Principled shaders (texture alpha kept), then made matte (roughness 1, specular 0, emission 0) to match Godot's lighting.
- TGA textures are converted to PNG in `textures/` (`export/textures/` in full mode); the game and mod folders are never written to.
- Animations show up as separate clips in the `AnimationPlayer` (one NLA track per sequence, e.g. `Idle2`, `SpellCast_Equip`).

## Godot project folder

`./run.sh` converts Morrowind data **directly** (pure Python, no Blender, seconds instead of minutes) for the
`interior`, `exterior` and `full` modes: terrain, every placed object, textures (PNG), collisions
(`-colonly` nodes) and the doors manifest. NPCs/creatures and their animations still need the Blender
pipeline: it is used automatically when you answer `y` to "Include NPCs/creatures?", or with
`./run.sh --blender`.

Every `./run.sh` ends by assembling a ready-to-use folder, `godot_export/`:

```
godot_export/
├── project.godot            # open it directly in Godot...
└── morrowind/               # ...or copy this folder into your own project
    ├── world.tscn           # main scene: exterior + farlands + sun + sky + free camera
    ├── farlands.tscn        # all LOD chunks (if generated)
    ├── scenes/interiors/    # one scene per interior (Arrival_* markers, door markers)
    ├── models/world, models/lod   # glTF + textures
    ├── scripts/fly_cam.gd   # right-click + WASD/ZQSD, Shift = fast
    └── manifest.json
```

- Scene paths are `res://morrowind/...`: use `./run.sh --godot-name NAME` if you want another folder name.
- Doors are `Marker3D` nodes with metadata (`target_scene`, `arrival_position`, `arrival_yaw`):
  no gameplay is generated, plug your own door logic on them.
- `--lod 1` (or answer `y` in `exterior` / `full` modes) also generates the farlands: the cells within
  `--lod-radius N` (default 16, asked interactively) around the detailed grid, with a hole over the
  detailed grid (no z-fighting). To tune the farlands, run `./run_lod.sh` separately, then
  `python3 scripts/build_godot.py --help`.
- Godot imports the glTF files the first time you open the project (can take a while).

## Farlands / LODs

`run_lod.sh` builds distant terrain for a whole world from one or more plugins, **without Blender**
(NIF files are read in pure Python, a whole region takes seconds):

```bash
./run_lod.sh                      # Morrowind.esm -> all of Vvardenfell
./run_lod.sh --esm Morrowind.esm,Tribunal.esm --chunk 8 --terrain-step 4
./run_lod.sh --grid "-3,-10:-1,-8" --min-size 600
```

The world is split into `chunk x chunk` cells; each chunk gives `export_lod/lod_<cx>_<cy>.gltf`
(one terrain mesh + one texture atlas + the big `Static`/`Activator` models, no water, no collisions).
Models keep their original geometry (Morrowind meshes are already low-poly; Godot builds its own
mesh LODs on import), only the size filter (`--min-size`) and the texture size (`--tex-max-side`)
make them "low quality". All chunks share the same origin: place them at (0,0,0).
`manifest.json` lists the chunks and the cells they cover. See `./run_lod.sh --help`.

## Re-processing existing exports

```bash
./fix_textures.sh                 # every .blend in export/ and subfolders
./fix_textures.sh --anims all     # keep every animation (default: idle)
./fix_textures.sh --anims none    # no animation at all (fastest export)
```

Runs `tga_to_png`, `rebuild_mat`, `no_lube`, `zero_emission` and `export_glb` on each `.blend` and writes the `.glb` next to it. Useful for exports made before these steps were part of `run.sh`. The GLB export is a single long call (minutes on big exterior scenes): the progress bar shows `Writing GLB` with a ticking elapsed time while it runs.

## Verbose and progress

- `vprint` (from `mwlog`) replaces `print` and only prints with `-v`.
- `eprint` always prints (errors, the NPC choice menu).
- Progress bars go through **stderr**, so they stay visible even when `run.sh` silences Blender's stdout outside verbose mode. In a log file (`full` mode, `export/post.log`), they shrink to one line every 10%.

```
[██████████░░░░░░░░░░░░░░░░░░] Interiors 3/12 > Decor 45/300  15% 0:12 ETA 0:40
```

## Known limitations

- The **legacy** flow (without `openmw.cfg`) merges plugins without per-reference merging, so exterior cells modified by a plugin may be incomplete.
- NPC import is slow, especially in `full` mode.
- The cleanup step deletes every `output*` file in the project root: don't store anything important under that name.

## License

This project contains no Morrowind data. Assets remain the property of Bethesda Softworks; you need to own the game to use this pipeline.

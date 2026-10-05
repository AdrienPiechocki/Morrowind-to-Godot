#!/usr/bin/env bash
# Post-processes every .blend found in a folder (default: ./export) and all its subfolders:
#   1. tga_to_png.py   TGA textures -> PNG
#   2. rebuild_mat.py  clean glTF-friendly materials (alpha kept)
#   3. no_lube.py + zero_emission.py  re-applied: rebuild_mat resets roughness,
#      specular and emission
#   4. export_glb.py exports <name>.glb next to each <name>.blend
#
# PNGs are written to <folder>/textures (shared by all the .blend files), never next to the
# original textures in your game / mod folders.
#
# Usage: ./fix_textures.sh [folder] [--anims <prefixes|all>] [-v|--verbose]
#   ./fix_textures.sh                    # ./export, progress bars only
#   ./fix_textures.sh -v                 # all messages
#   ./fix_textures.sh export/interiors   # another folder
#   ./fix_textures.sh --anims all        # keep every animation (default: idle)

set -uo pipefail
cd "$(dirname "$0")"

EXPORT_DIR="export"
VFLAG=""
ANIMS="idle"    # same default as run.sh in full mode
while [ $# -gt 0 ]; do
    case "$1" in
        -v|--verbose) VFLAG="--verbose"; shift ;;
        --anims)      [ $# -ge 2 ] || { echo "[ERROR] --anims needs a value" >&2; exit 1; }
                      ANIMS="$2"; shift 2 ;;
        --anims=*)    ANIMS="${1#--anims=}"; shift ;;
        -h|--help)    sed -n '2,18p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *)            EXPORT_DIR="$1"; shift ;;
    esac
done

command -v blender &>/dev/null || { echo "[ERROR] blender not found in PATH." >&2; exit 1; }
[ -d "$EXPORT_DIR" ] || { echo "[ERROR] Folder not found: $EXPORT_DIR" >&2; exit 1; }

# Outside verbose mode, Blender's stdout is silenced. The scripts' progress bars and
# errors go through stderr, so they stay visible.
bl() {
    if [ -n "$VFLAG" ]; then blender "$@"; else blender "$@" >/dev/null; fi
}

# Shared PNG output folder (absolute path, passed to tga_to_png.py)
TEX_DIR="$(cd "$EXPORT_DIR" && pwd)/textures"

# Every *.blend (not the .blend1 backups), recursively, sorted
mapfile -d '' FILES < <(find "$EXPORT_DIR" -type f -name '*.blend' -print0 | sort -z)
TOTAL=${#FILES[@]}
[ "$TOTAL" -gt 0 ] || { echo "[!] No .blend file found in $EXPORT_DIR" >&2; exit 0; }

echo "[+] $TOTAL .blend file(s) in $EXPORT_DIR" >&2
FAILED=()
i=0
for f in "${FILES[@]}"; do
    i=$((i + 1))
    echo "[$i/$TOTAL] $f" >&2
    abs="$(cd "$(dirname "$f")" && pwd)/$(basename "$f")"
    glb="${abs%.blend}.glb"
    rm -f "$glb"    # so a stale .glb can't hide a failed export

    for script in tga_to_png rebuild_mat no_lube zero_emission export_glb; do
        extra=()
        case "$script" in
            tga_to_png)    extra=(--textures-dir "$TEX_DIR") ;;
            7-export_glb)  extra=(--output "$glb" --anims "$ANIMS") ;;
        esac
        if ! bl --background --python-exit-code 1 --python "scripts/${script}.py" -- --blend "$abs" ${extra[@]+"${extra[@]}"} $VFLAG; then
            echo "[ERROR] ${script}.py failed on $f" >&2
            FAILED+=("$f (${script})")
            continue 2   # later steps depend on this one: next file
        fi
    done

    if [ ! -f "$glb" ]; then
        echo "[ERROR] $(basename "$glb") was not generated" >&2
        FAILED+=("$f (glb not generated)")
    fi
done

echo >&2
if [ "${#FAILED[@]}" -eq 0 ]; then
    echo "[OK] $TOTAL file(s) processed, $TOTAL .glb exported." >&2
else
    echo "[!] $((TOTAL - ${#FAILED[@]}))/$TOTAL processed, ${#FAILED[@]} failed:" >&2
    printf '    - %s\n' "${FAILED[@]}" >&2
    exit 1
fi
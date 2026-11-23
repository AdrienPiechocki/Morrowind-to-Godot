#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

usage() {
cat <<'EOF'
Usage: ./run_lod.sh [options]

Genere des farlands / LODs (terrain + gros assets en basse qualite) a partir d'un ou
plusieurs plugins. Sortie : export_lod/lod_<cx>_<cy>.gltf (un par chunk) + manifest.json.

  --esm LISTE         plugins de openmw.cfg, separes par des virgules (defaut : Morrowind.esm)
                      "all" = tout le load order
  --grid x1,y1:x2,y2  restreint a un rectangle de cells (defaut : toute la carte)
  --chunk N           cells par cote d'un chunk                       (defaut 8)
  --terrain-step N    diviseur de 64 : 1 = pleine resolution, 4 = 17x17 sommets/cell (defaut 4)
  --tex-px N          pixels par cell pour la texture du terrain      (defaut 128)
  --min-size N        taille mini d'un asset conserve, unites MW      (defaut 400)
  --lod-tris N        triangles cibles par modele                     (defaut 400)
  --tex-max-side N    cote max des textures des assets                (defaut 256)
  --no-sea            pas de plan de mer par chunk
  --outdir DIR        dossier de sortie                               (defaut export_lod)
  -v | --verbose      affiche tous les messages
EOF
}

ESM="Morrowind.esm"
GRID=""
CHUNK=8
TSTEP=4
TEXPX=128
MINSIZE=400
LODTRIS=400
TEXSIDE=256
SEA=1
OUTDIR="$SCRIPT_DIR/export_lod"
VFLAG=""
while [ $# -gt 0 ]; do
    case "$1" in
        -h|--help) usage; exit 0 ;;
        -v|--verbose) VFLAG="--verbose"; shift ;;
        --no-sea) SEA=0; shift ;;
        --esm|--grid|--chunk|--terrain-step|--tex-px|--min-size|--lod-tris|--tex-max-side|--outdir)
            [ $# -ge 2 ] || { echo "[ERROR] $1 attend une valeur"; exit 1; }
            case "$1" in
                --esm) ESM="$2" ;;
                --grid) GRID="$2" ;;
                --chunk) CHUNK="$2" ;;
                --terrain-step) TSTEP="$2" ;;
                --tex-px) TEXPX="$2" ;;
                --min-size) MINSIZE="$2" ;;
                --lod-tris) LODTRIS="$2" ;;
                --tex-max-side) TEXSIDE="$2" ;;
                --outdir) OUTDIR="$(realpath -m "$2")" ;;
            esac
            shift 2 ;;
        --esm=*|--grid=*|--chunk=*|--terrain-step=*|--tex-px=*|--min-size=*|--lod-tris=*|--tex-max-side=*|--outdir=*)
            set -- "${1%%=*}" "${1#*=}" "${@:2}" ;;
        *) echo "[ERROR] Option inconnue : $1"; usage; exit 1 ;;
    esac
done

BLUE='\033[0;34m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
step()  { echo -e "\n${BLUE}[$1/4]${NC} $2"; }
ok()    { echo -e "${GREEN}[OK]${NC} $1"; }
warn()  { echo -e "${YELLOW}[!]${NC} $1"; }
fail()  { echo -e "${RED}[ERROR]${NC} $1"; exit 1; }

bl() {
    if [ -n "$VFLAG" ]; then blender "$@"; else blender "$@" >/dev/null; fi
}

echo -e "${BLUE}========================================${NC}"
echo -e "${BLUE} Morrowind-to-Godot : Farlands / LODs${NC}"
echo -e "${BLUE}========================================${NC}"

[ -x "./tes3conv" ] || fail "tes3conv not found. Run install.sh first."
[ -x "./openmw-tools/bsatool" ] || fail "bsatool not found. Run install.sh first."
command -v blender &>/dev/null || fail "blender not found in PATH."

# ==========================================
# 1. openmw.cfg
# ==========================================
step 1 "Locate openmw.cfg"
CONFIG_FILE="$SCRIPT_DIR/config.json"
OPENMW_CFG=""
if [ -f "$CONFIG_FILE" ]; then
    OPENMW_CFG=$(python3 -c "
import json
print(json.load(open('$CONFIG_FILE')).get('openmw_cfg', ''))
" 2>/dev/null || true)
    OPENMW_CFG="${OPENMW_CFG/#\~/$HOME}"
fi
if [ -z "$OPENMW_CFG" ]; then
    OPENMW_CFG=$(python3 scripts/openmw_cfg.py find 2>/dev/null || true)
fi
[ -n "$OPENMW_CFG" ] || fail "openmw.cfg not found: set \"openmw_cfg\" in config.json (run ./run.sh once)."
[ -f "$OPENMW_CFG" ] || fail "openmw.cfg not found: $OPENMW_CFG (check config.json)"
ok "$OPENMW_CFG"

# ==========================================
# 2. Donnees
# ==========================================
step 2 "Prepare data (BSA cache, plugin(s): $ESM)"
PLUGINS_ARGS=()
[ "${ESM,,}" = "all" ] || PLUGINS_ARGS=(--plugins "$ESM")
python3 scripts/openmw_cfg.py prepare \
    --cfg "$OPENMW_CFG" \
    --tes3conv ./tes3conv \
    --bsatool ./openmw-tools/bsatool \
    --cache "$SCRIPT_DIR/data" \
    --out output.json \
    --paths output_paths.json \
    ${PLUGINS_ARGS[@]+"${PLUGINS_ARGS[@]}"} \
    $VFLAG \
    || fail "Data preparation failed."

MESHES_ARG=$(python3 -c "import json, os; print(os.pathsep.join(json.load(open('output_paths.json'))['meshes']))")
TEXTURES_ARG=$(python3 -c "import json, os; print(os.pathsep.join(json.load(open('output_paths.json'))['textures']))")
[ -n "$MESHES_ARG" ] || fail "No meshes directory found (check data= lines in openmw.cfg)."
ok "output.json generated."

# ==========================================
# 3. Generation (un .blend par chunk)
# ==========================================
step 3 "Generate LOD chunks"
ARGS=(--json output.json --meshes "$MESHES_ARG" --textures "$TEXTURES_ARG" --outdir "$OUTDIR"
      --chunk "$CHUNK" --terrain-step "$TSTEP" --tex-px "$TEXPX"
      --min-size "$MINSIZE" --lod-tris "$LODTRIS" --sea "$SEA")
[ -n "$GRID" ] && ARGS+=(--grid "$GRID")
[ -n "$VFLAG" ] && ARGS+=(--verbose)
bl --background --python scripts/generate_lod.py -- "${ARGS[@]}"
[ -f "$OUTDIR/files.txt" ] || fail "scripts/generate_lod.py produced no file."

# ==========================================
# 4. Post-traitement + export glTF
# ==========================================
step 4 "Post-process + export glTF (one per chunk)"
# Pas de set_collision : les collisions sont retirees des LODs.
POST_SCRIPTS=(tga_to_png rebuild_mat no_lube cleanup zero_emission)
TEXTURES_DIR="$OUTDIR/textures"
LOG="$OUTDIR/post.log"
: > "$LOG"

mapfile -t STEMS < "$OUTDIR/files.txt"
TOTAL=0
for stem in "${STEMS[@]}"; do [ -n "$stem" ] && TOTAL=$((TOTAL + 1)); done
N=0
for stem in "${STEMS[@]}"; do
    [ -n "$stem" ] || continue
    N=$((N + 1))
    echo "[..] [$N/$TOTAL] $stem"
    for script in "${POST_SCRIPTS[@]}"; do
        extra=()
        [ "$script" = "tga_to_png" ] && extra=(--textures-dir "$TEXTURES_DIR")
        blender --background --python "scripts/${script}.py" -- \
            --blend "$OUTDIR/$stem.blend" ${extra[@]+"${extra[@]}"} $VFLAG >> "$LOG" 2>&1 \
            || warn "$script failed on $stem (see export_lod/post.log)"
    done
    blender --background --python scripts/export_glb.py -- \
        --blend "$OUTDIR/$stem.blend" --output "$OUTDIR/$stem.gltf" \
        --textures-dir "$TEXTURES_DIR" --anims none --tex-max-side "$TEXSIDE" \
        $VFLAG >> "$LOG" 2>&1 || true
    [ -f "$OUTDIR/$stem.gltf" ] || warn "$stem.gltf not generated (see export_lod/post.log)"
done
ok "$TOTAL chunk(s) processed (Blender log: $OUTDIR/post.log)."

find . -maxdepth 1 -type f -regex "\./output.*" -exec rm -f {} \;

echo ""
echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN} LOD generation complete!${NC}"
echo -e "${GREEN}========================================${NC}"
echo ""
echo "  - $OUTDIR/lod_<cx>_<cy>.gltf   ($(find "$OUTDIR" -maxdepth 1 -name '*.gltf' 2>/dev/null | wc -l) chunk(s))"
echo "  - $OUTDIR/manifest.json        (grid of the chunks)"
echo ""
echo "All chunks share the same origin: put them at (0,0,0) in Godot, side by side."

#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

usage() {
cat <<'EOF'
Usage: ./run_lod.sh [options]

Genere des farlands / LODs (terrain + gros assets) a partir d'un ou plusieurs plugins, directement
depuis les donnees Morrowind (sans Blender). Sortie : export_lod/lod_<cx>_<cy>.gltf + manifest.json.

  --esm LISTE         plugins de openmw.cfg, separes par des virgules (defaut : Morrowind.esm)
                      "all" = tout le load order
  --grid x1,y1:x2,y2  restreint a un rectangle de cells (defaut : toute la carte)
  --chunk N           cells par cote d'un chunk                       (defaut 8)
  --terrain-step N    diviseur de 64 : 1 = pleine resolution, 4 = 17x17 sommets/cell (defaut 4)
  --tex-px N          pixels par cell pour la texture du terrain      (defaut 128)
  --min-size N        taille mini d'un asset conserve, unites MW      (defaut 400)
  --tex-max-side N    cote max des textures des assets                (defaut 256)
  --exclude-grid R    trou dans les farlands sur ce rectangle (zone deja exportee en detail)
  --reuse-data        reutilise output.json / output_paths.json deja prepares (appele par run.sh)
  --workers N         processus en parallele                          (defaut : tous les coeurs)
  --outdir DIR        dossier de sortie                               (defaut export_lod)
  -v | --verbose      affiche tous les messages
EOF
}

ESM="Morrowind.esm"
GRID=""
EXCLUDE=""
CHUNK=8
TSTEP=4
TEXPX=128
MINSIZE=400
TEXSIDE=256
WORKERS=""
REUSE=0
OUTDIR="$SCRIPT_DIR/export_lod"
VFLAG=""
while [ $# -gt 0 ]; do
    case "$1" in
        -h|--help) usage; exit 0 ;;
        --reuse-data) REUSE=1; shift ;;
        -v|--verbose) VFLAG="--verbose"; shift ;;
        --esm|--grid|--exclude-grid|--chunk|--terrain-step|--tex-px|--min-size|--tex-max-side|--workers|--outdir)
            [ $# -ge 2 ] || { echo "[ERROR] $1 attend une valeur"; exit 1; }
            case "$1" in
                --esm) ESM="$2" ;;
                --grid) GRID="$2" ;;
                --exclude-grid) EXCLUDE="$2" ;;
                --chunk) CHUNK="$2" ;;
                --terrain-step) TSTEP="$2" ;;
                --tex-px) TEXPX="$2" ;;
                --min-size) MINSIZE="$2" ;;
                --workers) WORKERS="$2" ;;
                --tex-max-side) TEXSIDE="$2" ;;
                --outdir) OUTDIR="$(realpath -m "$2")" ;;
            esac
            shift 2 ;;
        --esm=*|--grid=*|--exclude-grid=*|--chunk=*|--terrain-step=*|--tex-px=*|--min-size=*|--tex-max-side=*|--workers=*|--outdir=*)
            set -- "${1%%=*}" "${1#*=}" "${@:2}" ;;
        *) echo "[ERROR] Option inconnue : $1"; usage; exit 1 ;;
    esac
done

BLUE='\033[0;34m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
step()  { echo -e "\n${BLUE}[$1/3]${NC} $2"; }
ok()    { echo -e "${GREEN}[OK]${NC} $1"; }
warn()  { echo -e "${YELLOW}[!]${NC} $1"; }
fail()  { echo -e "${RED}[ERROR]${NC} $1"; exit 1; }


echo -e "${BLUE}========================================${NC}"
echo -e "${BLUE} Morrowind-to-Godot : Farlands / LODs${NC}"
echo -e "${BLUE}========================================${NC}"

[ -x "./tes3conv" ] || fail "tes3conv not found. Run install.sh first."
[ -x "./openmw-tools/bsatool" ] || fail "bsatool not found. Run install.sh first."

if [ "$REUSE" = "1" ] && [ -f output.json ] && [ -f output_paths.json ]; then
    step 1 "Reuse prepared data"
    ok "output.json (from the caller)"
    step 2 "Prepare data"
    ok "skipped"
else
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

fi

MESHES_ARG=$(python3 -c "import json, os; print(os.pathsep.join(json.load(open('output_paths.json'))['meshes']))")
TEXTURES_ARG=$(python3 -c "import json, os; print(os.pathsep.join(json.load(open('output_paths.json'))['textures']))")
[ -n "$MESHES_ARG" ] || fail "No meshes directory found (check data= lines in openmw.cfg)."
ok "output.json generated."

# ==========================================
# 3. Generation (Python pur, sans Blender)
# ==========================================
step 3 "Generate LOD chunks"
ARGS=(--json output.json --meshes "$MESHES_ARG" --textures "$TEXTURES_ARG" --outdir "$OUTDIR"
      --chunk "$CHUNK" --terrain-step "$TSTEP" --tex-px "$TEXPX"
      --min-size "$MINSIZE" --tex-max-side "$TEXSIDE")
[ -n "$GRID" ] && ARGS+=(--grid "$GRID")
[ -n "$EXCLUDE" ] && ARGS+=(--exclude-grid "$EXCLUDE")
[ -n "$WORKERS" ] && ARGS+=(--workers "$WORKERS")
[ -n "$VFLAG" ] && ARGS+=(--verbose)
python3 scripts/build_lod.py "${ARGS[@]}" || fail "scripts/build_lod.py failed."
[ -f "$OUTDIR/manifest.json" ] || fail "scripts/build_lod.py produced no file."

[ "$REUSE" = "1" ] || find . -maxdepth 1 -type f -regex "\./output.*" -exec rm -f {} \;

echo ""
echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN} LOD generation complete!${NC}"
echo -e "${GREEN}========================================${NC}"
echo ""
echo "  - $OUTDIR/lod_<cx>_<cy>.gltf   ($(find "$OUTDIR" -maxdepth 1 -name '*.gltf' 2>/dev/null | wc -l) chunk(s))"
echo "  - $OUTDIR/manifest.json        (grid of the chunks)"
echo ""
echo "All chunks share the same origin: put them at (0,0,0) in Godot, side by side."

#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# Options : --anims <prefixes|all>  (ex. --anims idle) ; --npcs 0|1  (NPCs dans les scenes interior/exterior/full)
#           -v | --verbose  (affiche tous les messages des scripts Python et de Blender)
USER_ANIMS=""
USER_NPCS=""
VFLAG=""          # "--verbose" transmis aux scripts Python (vide = barre de progression seule)
while [ $# -gt 0 ]; do
    case "$1" in
        --anims)   [ $# -ge 2 ] || { echo "[ERROR] --anims attend une valeur"; exit 1; }
                   USER_ANIMS="$2"; shift 2 ;;
        --anims=*) USER_ANIMS="${1#--anims=}"; shift ;;
        --npcs)    [ $# -ge 2 ] || { echo "[ERROR] --npcs attend 0 ou 1"; exit 1; }
                   USER_NPCS="$2"; shift 2 ;;
        --npcs=*)  USER_NPCS="${1#--npcs=}"; shift ;;
        -v|--verbose) VFLAG="--verbose"; shift ;;
        *)         echo "[ERROR] Option inconnue : $1"; exit 1 ;;
    esac
done

# Blender : hors verbose, son stdout est coupe (la barre de progression et les
# erreurs des scripts passent par stderr, donc restent visibles).
bl() {
    if [ -n "$VFLAG" ]; then blender "$@"; else blender "$@" >/dev/null; fi
}

BLUE='\033[0;34m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

step()  { echo -e "\n${BLUE}[$1/9]${NC} $2"; }
ok()    { echo -e "${GREEN}[OK]${NC} $1"; }
warn()  { echo -e "${YELLOW}[!]${NC} $1"; }
fail()  { echo -e "${RED}[ERROR]${NC} $1"; exit 1; }

# ==========================================
# 0. Verifier les dependances
# ==========================================
echo -e "${BLUE}========================================${NC}"
echo -e "${BLUE} Morrowind-to-Godot : Conversion${NC}"
echo -e "${BLUE}========================================${NC}"

[ -x "./tes3conv" ] || fail "tes3conv not found. Run install.sh first."
[ -x "./openmw-tools/bsatool" ]  || fail "bsatool not found. Run install.sh first."
[ -d "./io_scene_mw/" ] || fail "io_scene_mw not found. Run install.sh first."
command -v blender &>/dev/null || fail "blender not found in PATH."

BSATOOL="./openmw-tools/bsatool"

ok "Dependencies verified."

# ==========================================
# 1. Data source: openmw.cfg (preferred) or legacy config.json
# ==========================================
# Set MW2G_LEGACY=1 to force the old flow (config.json + manual BSA/mod extraction).
CONFIG_FILE="$SCRIPT_DIR/config.json"
OPENMW_CFG=""
MESHES_ARG="data/meshes"
TEXTURES_ARG="data/textures"

if [ "${MW2G_LEGACY:-0}" != "1" ]; then
    # a) path stored in config.json ("openmw_cfg")
    if [ -f "$CONFIG_FILE" ]; then
        OPENMW_CFG=$(python3 -c "
import json
print(json.load(open('$CONFIG_FILE')).get('openmw_cfg', ''))
" 2>/dev/null || true)
        OPENMW_CFG="${OPENMW_CFG/#\~/$HOME}"
    fi

    # b) autodetection (~/.config/openmw, Flatpak, Windows, macOS)
    if [ -z "$OPENMW_CFG" ]; then
        DETECTED=$(python3 scripts/openmw_cfg.py find 2>/dev/null || true)
        if [ -n "$DETECTED" ]; then
            read -rp "openmw.cfg detected: $DETECTED. Use it? [Y/n] " USE_CFG
            case "$USE_CFG" in
                n|N|no|NO) ;;
                *)
                    OPENMW_CFG="$DETECTED"
                    python3 - "$CONFIG_FILE" "$OPENMW_CFG" <<'PY'
import json, os, sys
path, cfg = sys.argv[1:3]
data = {}
if os.path.exists(path):
    try:
        data = json.load(open(path))
    except ValueError:
        pass
data["openmw_cfg"] = cfg
json.dump(data, open(path, "w"), indent=2)
PY
                    ;;
            esac
        fi
    fi

    if [ -n "$OPENMW_CFG" ] && [ ! -f "$OPENMW_CFG" ]; then
        fail "openmw.cfg not found: $OPENMW_CFG (check config.json)"
    fi
fi

if [ -n "$OPENMW_CFG" ]; then
    step 1 "Using openmw.cfg"
    ok "$OPENMW_CFG"

    step 2 "Prepare data (BSA cache, plugins in load order, merge)"
    python3 scripts/openmw_cfg.py prepare \
        --cfg "$OPENMW_CFG" \
        --tes3conv ./tes3conv \
        --bsatool "$BSATOOL" \
        --cache "$SCRIPT_DIR/data" \
        --out output.json \
        --paths output_paths.json \
        $VFLAG \
        || fail "Data preparation failed."

    MESHES_ARG=$(python3 -c "import json, os; print(os.pathsep.join(json.load(open('output_paths.json'))['meshes']))")
    TEXTURES_ARG=$(python3 -c "import json, os; print(os.pathsep.join(json.load(open('output_paths.json'))['textures']))")
    [ -n "$MESHES_ARG" ] || fail "No meshes directory found (check data= lines in openmw.cfg)."
    ok "output.json generated."
else
# ==========================================
# 1. Load configuration
# ==========================================
CONFIG_FILE="$SCRIPT_DIR/config.json"

if [ ! -f "$CONFIG_FILE" ]; then
    step 1 "Initial configuration"
    echo "No config.json found. Let's create one."
    echo ""
    read -rp "Path to Morrowind Data Files directory: " MORROWIND_DATA
    MORROWIND_DATA="${MORROWIND_DATA/#\~/$HOME}"
    [ -d "$MORROWIND_DATA" ] || fail "Directory not found: $MORROWIND_DATA"
    python3 -c "
import json
config = {\"morrowind_data\": \"$MORROWIND_DATA\"}
with open('$CONFIG_FILE', 'w') as f:
    json.dump(config, f, indent=2)
print('[+] config.json created.')
"
    ok "Directory: $MORROWIND_DATA"
else
    step 1 "Loading config.json"
    MORROWIND_DATA=$(python3 -c "
import json, sys
with open('$CONFIG_FILE') as f:
    data = json.load(f)
path = data.get('morrowind_data', '')
print(path)
")
    [ -n "$MORROWIND_DATA" ] || fail "morrowind_data missing in config.json"
    [ -d "$MORROWIND_DATA" ] || fail "Directory not found: $MORROWIND_DATA (check config.json)"
    ok "Directory: $MORROWIND_DATA"
fi

# ==========================================
# 2. Extraire des fichiers .bsa
# ==========================================
step 2 "Select .bsa files to extract"

mapfile -t BSA_FILES < <(find "$MORROWIND_DATA" -maxdepth 1 -iname "*.bsa" | sort)

if [ ${#BSA_FILES[@]} -eq 0 ]; then
    warn "No .bsa files found in $MORROWIND_DATA"
    SKIP_BSA=true
else
    echo "Found .bsa files:"
    echo ""
    for i in "${!BSA_FILES[@]}"; do
        basename="$(basename "${BSA_FILES[$i]}")"
        size="$(du -h "${BSA_FILES[$i]}" | cut -f1)"
        echo "  $((i+1))) $basename ($size)"
    done
    echo "  0) Skip (extract nothing)"
    echo ""
    read -rp "Choice (space-separated numbers): " BSA_CHOICES

    if [ -z "$BSA_CHOICES" ] || [ "$BSA_CHOICES" = "0" ]; then
        SKIP_BSA=true
        ok "BSA extraction skipped."
    else
        mkdir -p data
        for choice in $BSA_CHOICES; do
            choice=$((choice - 1))
            if [ "$choice" -ge 0 ] && [ "$choice" -lt ${#BSA_FILES[@]} ]; then
                SELECTED_BSA="${BSA_FILES[$choice]}"
                echo "[..] Extracting $(basename "$SELECTED_BSA") to data/..."
                "$BSATOOL" extractall "$SELECTED_BSA" "$SCRIPT_DIR/data/"
                ok "$(basename "$SELECTED_BSA") extracted."
            else
                warn "Invalid choice: $((choice+1)), skipping."
            fi
        done
    fi
fi

# ==========================================
# 3. Extract mod assets into data/
# ==========================================
step 3 "Extract mod assets"

mkdir -p mods
MODS_DIR="$SCRIPT_DIR/mods"
if [ -d "$MODS_DIR" ]; then
    ASSET_NAMES=("meshes" "textures" "icons" "bookart" "shaders" "sound" "music" "video" "fonts" "movements")
    printf -v ASSET_ICASE '%s|' "${ASSET_NAMES[@]}"
    ASSET_ICASE="(${ASSET_ICASE%|})"
    TMPDIR_MOD="$(mktemp -d)"
    trap "rm -rf '$TMPDIR_MOD'" EXIT

    # Trouver toutes les archives dans mods/
    ARCHIVES=()
    while IFS= read -r -d '' f; do
        ARCHIVES+=("$f")
    done < <(find "$MODS_DIR" -maxdepth 1 -type f \( -iname "*.zip" -o -iname "*.7z" -o -iname "*.rar" -o -iname "*.tar" -o -iname "*.tar.gz" -o -iname "*.tgz" \) -print0 2>/dev/null)

    # Aussi traiter les .esm/.esp a la racine de mods/
    while IFS= read -r -d '' esp; do
        basename_esp="$(basename "$esp")"
        if [ ! -f "$SCRIPT_DIR/data/$basename_esp" ]; then
            mkdir -p "$SCRIPT_DIR/data"
            cp -n "$esp" "$SCRIPT_DIR/data/" 2>/dev/null &&             echo "[+] $basename_esp copied to data/"
        fi
    done < <(find "$MODS_DIR" -maxdepth 1 -type f \( -iname "*.esm" -o -iname "*.esp" \) -print0 2>/dev/null)

    if [ ${#ARCHIVES[@]} -eq 0 ]; then
        warn "No archives (.zip/.7z/.rar) found in mods/"
    else
        echo "Found archives:"
        echo ""
        for i in "${!ARCHIVES[@]}"; do
            name="$(basename "${ARCHIVES[$i]}")"
            size="$(du -h "${ARCHIVES[$i]}" | cut -f1)"
            echo "  $((i+1))) $name ($size)"
        done
        echo "  0) Skip"
        echo ""
        read -rp "Choice (space-separated numbers): " ARCH_CHOICES

        if [ -n "$ARCH_CHOICES" ] && [ "$ARCH_CHOICES" != "0" ]; then
            mkdir -p "$SCRIPT_DIR/data"
            for choice in $ARCH_CHOICES; do
                choice=$((choice - 1))
                if [ "$choice" -ge 0 ] && [ "$choice" -lt ${#ARCHIVES[@]} ]; then
                    archive="${ARCHIVES[$choice]}"
                    name="$(basename "$archive")"
                    echo ""
                    echo "[..] Extracting $name..."

                    # Extraire dans un tmp propre pour cette archive
                    workdir="$TMPDIR_MOD/$choice"
                    mkdir -p "$workdir"

                    case "${archive,,}" in
                        *.zip)       unzip -q -o "$archive" -d "$workdir" ;;
                        *.7z)        7z x -y -o"$workdir" "$archive" >/dev/null ;;
                        *.rar)       unrar x -o+ "$archive" "$workdir" >/dev/null ;;
                        *.tar.gz|*.tgz) tar xzf "$archive" -C "$workdir" ;;
                        *.tar)       tar xf "$archive" -C "$workdir" ;;
                        *)           warn "Unsupported format: $name"; continue ;;
                    esac

                    # Check if fomod/ exists in the archive
                    fomod_config=""
                    for f in "$workdir/fomod/ModuleConfig.xml" "$workdir/Fomod/ModuleConfig.xml" "$workdir/fomod/moduleconfig.xml"; do
                        [ -f "$f" ] && fomod_config="$f" && break
                    done

                    if [ -n "$fomod_config" ]; then
                        echo "  Fomod structure detected"
                        # Extraire les chemins depuis le ModuleConfig.xml
                        python3 -c "
import xml.etree.ElementTree as ET, os, sys
try:
    tree = ET.parse('$fomod_config')
    for elem in tree.iter():
        for attr in ['file', 'source', 'path']:
            val = elem.get(attr, '')
            if val and not val.startswith('http'):
                print(val)
except: pass
" 2>/dev/null | while IFS= read -r filepath; do
                            basename_lower="$(basename "$filepath" | tr '[:upper:]' '[:lower:]')"
                            if echo "$basename_lower" | grep -qiE "^${ASSET_ICASE}$"; then
                                full_path="$(realpath -m "$workdir/$filepath" 2>/dev/null || echo "$workdir/$filepath")"
                                if [ -d "$full_path" ]; then
                                    mkdir -p "$SCRIPT_DIR/data/$basename_lower"
                                    echo "  [+] $basename_lower/ (via fomod)"
                                    cp -ru "$full_path/"* "$SCRIPT_DIR/data/$basename_lower/" 2>/dev/null
                                fi
                            elif echo "$basename_lower" | grep -qiE "\.(esm|esp)$"; then
                                src_file="$(realpath -m "$workdir/$filepath" 2>/dev/null || echo "$workdir/$filepath")"
                                if [ -f "$src_file" ]; then
                                    echo "  [+] $(basename "$src_file")"
                                    cp -n "$src_file" "$SCRIPT_DIR/data/" 2>/dev/null
                                fi
                            fi
                        done
                    fi

                    # Recursively find asset directories
                    while IFS= read -r -d '' asset_dir; do
                        asset_name="$(basename "$asset_dir")"
                        dest="$SCRIPT_DIR/data/$asset_name"
                        mkdir -p "$dest"
                        echo "  [+] $asset_name/ ($(find "$asset_dir" -type f | wc -l) files)"
                        cp -ru "$asset_dir/"* "$dest/" 2>/dev/null
                    done < <(find "$workdir" -mindepth 1 -maxdepth 4 -type d -regextype posix-extended -iregex ".*/${ASSET_ICASE}" -print0 2>/dev/null)

                    # Copy found .esm/.esp files
                    while IFS= read -r -d '' esp; do
                        dest="$SCRIPT_DIR/data/$(basename "$esp")"
                        if [ ! -f "$dest" ]; then
                            echo "  [+] $(basename "$esp")"
                            cp -n "$esp" "$dest" 2>/dev/null
                        fi
                    done < <(find "$workdir" -maxdepth 4 -type f \( -iname "*.esm" -o -iname "*.esp" \) -print0 2>/dev/null)

                    ok "$name extracted."
                else
                warn "Invalid choice: $((choice+1)), skipping."
                fi
            done
        else
            ok "Mod extraction skipped."
        fi
    fi
else
    warn "mods/ directory not found, skipping."
fi
step 4 "Convert Morrowind.esm to JSON"

ESM_FILE=""
for candidate in "$MORROWIND_DATA/Morrowind.esm"; do
    if [ -f "$candidate" ]; then
        ESM_FILE="$candidate"
        break
    fi
done
[ -n "$ESM_FILE" ] || fail "Morrowind.esm not found in $MORROWIND_DATA"

echo "[..] Converting $(basename "$ESM_FILE")..."
./tes3conv "$ESM_FILE" output.json
ok "output.json generated."

# ==========================================
# 5. Convertir un ou plusieurs .esp/esm
# ==========================================
step 5 "Select .esp/esm plugins (optional)"

mapfile -t ESP_FILES < <(find "$SCRIPT_DIR/data" -maxdepth 1 -type f \( -iname "*.esp" -o -iname "*.esm" \) | sort)

if [ ${#ESP_FILES[@]} -eq 0 ]; then
    warn "No .esp files found in data/"
else
    echo "Found plugins:"
    echo ""
    for i in "${!ESP_FILES[@]}"; do
        echo "  $((i+1))) $(basename "${ESP_FILES[$i]}")"
    done
    echo "  0) Skip (no plugins)"
    echo ""
    read -rp "Choice (space-separated numbers): " ESP_CHOICES

    if [ -z "$ESP_CHOICES" ] || [ "$ESP_CHOICES" = "0" ]; then
        ok "No plugins added."
    else
        for choice in $ESP_CHOICES; do
            choice=$((choice - 1))
            if [ "$choice" -ge 0 ] && [ "$choice" -lt ${#ESP_FILES[@]} ]; then
                ESP_FILE="${ESP_FILES[$choice]}"
                ESP_BASENAME="$(basename "$ESP_FILE" .esp)"
                ESP_BASENAME="$(basename "$ESP_BASENAME" .ESP)"
                echo "[..] Converting $(basename "$ESP_FILE")..."
                ./tes3conv "$ESP_FILE" "output_${ESP_BASENAME}.json"
                # Merge into output.json (plugins are appended at the end)
                python3 -c "
import json, sys
with open('output.json') as f: data = json.load(f)
with open('output_${ESP_BASENAME}.json') as f: data += json.load(f)
with open('output.json', 'w') as f: json.dump(data, f, indent=2)
print('[+] Merged output_${ESP_BASENAME}.json into output.json')
"
                rm -f "output_${ESP_BASENAME}.json"
                ok "$(basename "$ESP_FILE") merged."
            else
                warn "Invalid choice: $((choice+1)), skipping."
            fi
        done
    fi
fi

fi

# ==========================================
# 6. Import mode + Cell name
# ==========================================
step 6 "Import mode"
echo "What do you want to import?"
echo "  1) interior - static decor of an interior cell (default)"
echo "  2) npc      - NPCs/Creatures with body parts and animations"
echo "  3) exterior - one or several exterior cells (by grid coordinates)"
echo "  4) full     - exterior grid + every interior reachable through its doors + NPCs (idle only)"
echo "                one .blend/.glb for the exterior + one per interior, in export/"
echo ""
read -rp "Mode [interior]: " IMPORT_MODE
case "$IMPORT_MODE" in
    npc|NPC|2)                  IMPORT_MODE="npc" ;;
    exterior|EXTERIOR|ext|3)    IMPORT_MODE="exterior" ;;
    full|FULL|Full|4)           IMPORT_MODE="full" ;;
    *)                          IMPORT_MODE="interior" ;;
esac
ok "Mode: $IMPORT_MODE"

if { [ "$IMPORT_MODE" = "exterior" ] || [ "$IMPORT_MODE" = "full" ]; } && [ -z "$OPENMW_CFG" ]; then
    warn "Legacy flow: plugins are merged without per-reference merge, so exterior cells"
    warn "modified by a plugin may be incomplete. Use openmw.cfg for reliable results."
fi

NPC_NAME=""
if [ "$IMPORT_MODE" = "npc" ]; then
    echo "Search a specific NPC/creature by name or id (partial match ok)"
    echo "Leave empty to import every NPC of the cell instead."
    echo ""
    read -rp "NPC [Jiub]: " NPC_NAME
    NPC_NAME="${NPC_NAME:-Jiub}"
    NPC_NAME="${NPC_NAME%\"}"
    NPC_NAME="${NPC_NAME#\"}"
    NPC_NAME="${NPC_NAME%\'}"
    NPC_NAME="${NPC_NAME#\'}"
    [ -n "$NPC_NAME" ] && ok "NPC search: $NPC_NAME" || ok "NPC search: none (all NPCs of the cell)"
fi

CELL_NAME=""
if [ "$IMPORT_MODE" = "interior" ]; then
    echo "Enter the cell name (exactly as in Morrowind)"
    echo "E.g.: \"Balmora, Temple\" / \"Balmora, Guild of Mages\""
    echo ""
    read -rp "Cell [Balmora, Temple]: " CELL_NAME
    CELL_NAME="${CELL_NAME:-Balmora, Temple}"
    # Remove surrounding quotes if present
    CELL_NAME="${CELL_NAME#\"}"
    CELL_NAME="${CELL_NAME%\"}"
    CELL_NAME="${CELL_NAME#\'}"
    CELL_NAME="${CELL_NAME%\'}"
    ok "Cell: $CELL_NAME"
fi

GRID=""
if [ "$IMPORT_MODE" = "exterior" ] || [ "$IMPORT_MODE" = "full" ]; then
    echo "Enter the exterior cell grid: 'x,y' for one cell, 'x1,y1:x2,y2' for a rectangle"
    echo "E.g.: -2,-9 (Seyda Neen) / -3,-10:-1,-8 (3x3 cells around it)"
    echo ""
    read -rp "Grid [-2,-9]: " GRID
    if [ -z "$GRID" ]; then
        GRID="-2,-9"
    fi
    GRID="${GRID#\"}"
    GRID="${GRID%\"}"
    ok "Grid: $GRID"
fi

# NPCs/creatures places dans la scene (modes interior, exterior, full).
# Defaut : oui en full, non en interior/exterior. --npcs 0|1 evite la question.
WITH_NPCS=0
case "$IMPORT_MODE" in
    full|interior|exterior)
        if [ -n "$USER_NPCS" ]; then
            WITH_NPCS="$USER_NPCS"
        elif [ "$IMPORT_MODE" = "full" ]; then
            WITH_NPCS=1
            read -rp "Include NPCs/creatures? (slow) [Y/n] " USE_NPCS
            case "$USE_NPCS" in n|N|no|NO) WITH_NPCS=0 ;; esac
        else
            read -rp "Include NPCs/creatures? (slow) [y/N] " USE_NPCS
            case "$USE_NPCS" in y|Y|yes|YES) WITH_NPCS=1 ;; esac
        fi
        ;;
esac
[ "$WITH_NPCS" = "1" ] || WITH_NPCS=0

# Animations des NPCs conservees (prefixes separes par des virgules, ou "all") :
# idle par defaut des qu'il y a des NPCs dans la scene.
FULL_ANIMS="${USER_ANIMS:-idle}"
SCENE_ANIMS=""     # --anims transmis aux scripts 1 et 7 hors mode full
if [ "$IMPORT_MODE" != "full" ] && { [ "$WITH_NPCS" = "1" ] || [ -n "$USER_ANIMS" ]; }; then
    SCENE_ANIMS="$FULL_ANIMS"
fi

# ==========================================
# 7. Conversion Blender (scripts 1-6)
# ==========================================
step 7 "Blender conversion"

echo "[..] Launching Blender (script 1: generation)..."
BLENDER_MODE="$IMPORT_MODE"
[ "$IMPORT_MODE" = "exterior" ] && BLENDER_MODE="interior"
BLENDER_ARGS=(--json output.json --meshes "$MESHES_ARG" --textures "$TEXTURES_ARG" --mode "$BLENDER_MODE")
[ -n "$NPC_NAME" ] && BLENDER_ARGS+=(--npc "$NPC_NAME")
[ -n "$CELL_NAME" ] && BLENDER_ARGS+=(--cell "$CELL_NAME")
[ -n "$GRID" ] && BLENDER_ARGS+=(--grid "$GRID")
[ "$IMPORT_MODE" = "full" ] && BLENDER_ARGS+=(--npcs "$WITH_NPCS" --anims "$FULL_ANIMS" --outdir "$SCRIPT_DIR/export")
if [ "$IMPORT_MODE" = "interior" ] || [ "$IMPORT_MODE" = "exterior" ]; then
    BLENDER_ARGS+=(--npcs "$WITH_NPCS")
fi
[ -n "$SCENE_ANIMS" ] && BLENDER_ARGS+=(--anims "$SCENE_ANIMS")
[ -n "$VFLAG" ] && BLENDER_ARGS+=(--verbose)

# --python-exit-code 1 : si le script plante, on s'arrête (sinon Blender sort en
# code 0 et la suite du pipeline traite un morrowind.blend périmé).
bl --background --python-exit-code 1 --python scripts/generate_blend.py -- "${BLENDER_ARGS[@]}"

# Post-processing of one .blend (scripts), then GLB export (script 7).
#   post_process <blend> <glb> [options for export_glb.py]
# When POST_LOG is set (full mode, many files) Blender's output goes to that log.
run_blender() {
    if [ -n "${POST_LOG:-}" ]; then
        blender --background --python "$@" >> "$POST_LOG" 2>&1
    else
        bl --background --python "$@"
    fi
}

post_process() {
    local blend="$1" glb="$2"
    shift 2
    local script
    for script in no_lube cleanup set_collision zero_emission; do
        [ -z "${POST_LOG:-}" ] && echo "[..] Launching Blender (script ${script})..."
        run_blender "scripts/${script}.py" -- --blend "$blend" $VFLAG
    done
    [ -z "${POST_LOG:-}" ] && echo "[..] Exporting $(basename "$glb")..."
    run_blender scripts/export_glb.py -- --blend "$blend" --output "$glb" $VFLAG "$@"
}

if [ "$IMPORT_MODE" = "full" ]; then
    EXPORT_DIR="$SCRIPT_DIR/export"
    [ -f "$EXPORT_DIR/files.txt" ] || fail "export/files.txt not found: script 1 produced no file."
    mapfile -t STEMS < "$EXPORT_DIR/files.txt"

    step 8 "Post-process + export GLB (one per cell)"
    POST_LOG="$EXPORT_DIR/post.log"
    : > "$POST_LOG"
    TOTAL=0
    for stem in "${STEMS[@]}"; do [ -n "$stem" ] && TOTAL=$((TOTAL + 1)); done
    N=0
    for stem in "${STEMS[@]}"; do
        [ -n "$stem" ] || continue
        N=$((N + 1))
        echo "[..] [$N/$TOTAL] $stem"
        post_process "$EXPORT_DIR/$stem.blend" "$EXPORT_DIR/$stem.glb" --anims "$FULL_ANIMS"
        [ -f "$EXPORT_DIR/$stem.glb" ] || warn "$stem.glb not generated (see export/post.log)"
    done
    unset POST_LOG
    ok "$TOTAL file(s) processed (Blender log: export/post.log)."
else
    for script in no_lube cleanup set_collision zero_emission; do
        echo "[..] Launching Blender (script ${script})..."
        bl --background --python "scripts/${script}.py" -- $VFLAG
    done

    ok "Blender scripts executed."

    # ==========================================
    # 8. Export GLB
    # ==========================================
    step 8 "Export GLB"
    echo "[..] Exporting..."
    EXPORT_ARGS=()
    [ -n "$SCENE_ANIMS" ] && EXPORT_ARGS+=(--anims "$SCENE_ANIMS")
    [ -n "$VFLAG" ] && EXPORT_ARGS+=(--verbose)
    bl --background --python scripts/export_glb.py -- ${EXPORT_ARGS[@]+"${EXPORT_ARGS[@]}"}
    ok "morrowind.glb generated."
fi

# ==========================================
# 9. Cleanup and summary
# ==========================================
step 9 "Cleanup"
find . -type f -regex "\./output.*" -exec rm -f {} \;

echo ""
echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN} Conversion complete!${NC}"
echo -e "${GREEN}========================================${NC}"
echo ""
echo "Generated files:"
if [ "$IMPORT_MODE" = "full" ]; then
    echo "  - export/exterior.blend / exterior.glb"
    echo "  - export/interiors/*.blend / *.glb  ($(find "$SCRIPT_DIR/export/interiors" -name '*.glb' 2>/dev/null | wc -l) interior(s))"
    echo "  - export/manifest.json  (doors: where each interior connects)"
    echo ""
    echo "Import the .glb files into Godot; manifest.json links exteriors and interiors."
else
    echo "  - morrowind.blend"
    echo "  - morrowind.glb"
    echo ""
    echo "Import morrowind.glb into Godot."
fi
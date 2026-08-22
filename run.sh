#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

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

# ==========================================
# 6. Import mode + Cell name
# ==========================================
step 6 "Import mode"
echo "What do you want to import?"
echo "  1) cell - static decor of the cell (default)"
echo "  2) npc  - NPCs/Creatures with body parts and animations"
echo ""
read -rp "Mode [cells]: " IMPORT_MODE
case "$IMPORT_MODE" in
   npc|NPC|2) IMPORT_MODE="npc" ;;
    *)           IMPORT_MODE="cell" ;;
esac
ok "Mode: $IMPORT_MODE"

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
if [ "$IMPORT_MODE" = "cell" ]; then
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

# ==========================================
# 7. Conversion Blender (scripts 1-6)
# ==========================================
step 7 "Blender conversion (scripts 1-6)"

echo "[..] Launching Blender (script 1/6: generation)..."
BLENDER_ARGS=(--json output.json --meshes data/meshes --textures data/textures --mode "$IMPORT_MODE")
[ -n "$NPC_NAME" ] && BLENDER_ARGS+=(--npc "$NPC_NAME")
[ -n "$CELL_NAME" ] && BLENDER_ARGS+=(--cell "$CELL_NAME")

blender --background --python scripts/1-generate_blend.py -- "${BLENDER_ARGS[@]}"

for script in 2-tga_to_png 3-rebuild_mat 3.5-dedup_materials 4-no_lube 5-cleanup 6-set_collision; do
    echo "[..] Launching Blender (script ${script})..."
    blender --background --python "scripts/${script}.py"
done

ok "Blender scripts executed."

# ==========================================
# 8. Export GLB
# ==========================================
step 8 "Export GLB"
echo "[..] Exporting..."
blender --background --python scripts/7-export_glb.py
ok "morrowind.glb generated."

npx @gltf-transform/cli optimize morrowind.glb morrowind.glb \
  --compress false \
  --texture-compress webp \
  --instance false

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
echo "  - morrowind.blend"
echo "  - morrowind.glb"
echo ""
echo "Import morrowind.glb into Godot."

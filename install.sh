#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

TES3CONV_VERSION="v0.4.1"
OPENMW_VERSION="0.51.0"

echo "========================================"
echo " Morrowind-to-Godot: Installation"
echo "========================================"
echo ""

# ------------------------------------------
# tes3conv
# ------------------------------------------
if [ -f "./tes3conv" ] && [ -x "./tes3conv" ]; then
    echo "[OK] tes3conv already installed."
else
    echo "[..] Downloading tes3conv ${TES3CONV_VERSION}..."
    TES3CONV_URL="https://github.com/Greatness7/tes3conv/releases/download/${TES3CONV_VERSION}/ubuntu-latest.zip"
    curl -fSL "$TES3CONV_URL" -o /tmp/tes3conv.zip
    unzip -o /tmp/tes3conv.zip -d /tmp/tes3conv_extract
    mv /tmp/tes3conv_extract/tes3conv ./tes3conv
    chmod +x ./tes3conv
    rm -rf /tmp/tes3conv.zip /tmp/tes3conv_extract
    echo "[OK] tes3conv installed."
fi

# ------------------------------------------
# bsatool (OpenMW)
# ------------------------------------------
BSATOOL_DIR="$SCRIPT_DIR/openmw-tools"

if [ -f "$BSATOOL_DIR/bsatool" ] && [ -x "$BSATOOL_DIR/bsatool" ]; then
    echo "[OK] bsatool already installed."
else
    echo "[..] Downloading bsatool (OpenMW ${OPENMW_VERSION})..."
    echo "     (~94 MB, one-time download)"
    OPENMW_URL="https://github.com/OpenMW/openmw/releases/download/openmw-${OPENMW_VERSION}/openmw-${OPENMW_VERSION}-Linux-64Bit.tar.gz"
    curl -fSL "$OPENMW_URL" -o /tmp/openmw.tar.gz
    echo "[..] Extracting bsatool + libraries..."
    mkdir -p "$BSATOOL_DIR"
    tar -xzf /tmp/openmw.tar.gz -C "$BSATOOL_DIR" --strip-components=1 \
        "openmw-${OPENMW_VERSION}-Linux-64Bit/bsatool" \
        "openmw-${OPENMW_VERSION}-Linux-64Bit/bsatool.x86_64" \
        "openmw-${OPENMW_VERSION}-Linux-64Bit/lib/"
    chmod +x "$BSATOOL_DIR/bsatool" "$BSATOOL_DIR/bsatool.x86_64"
    rm -rf /tmp/openmw.tar.gz
    echo "[OK] bsatool installed in openmw-tools/."
fi

# ------------------------------------------
# io_scene_mw (Blender addon)
# ------------------------------------------
IO_SCENEMW_DIR="$SCRIPT_DIR/io_scene_mw"

if [ -d "$IO_SCENEMW_DIR" ]; then
    echo "[OK] io_scene_mw already cloned."
else
    echo "[..] Cloning io_scene_mw (Blender addon)..."
    git clone https://github.com/Greatness7/io_scene_mw.git "$IO_SCENEMW_DIR"
    echo "[OK] io_scene_mw cloned into io_scene_mw/."
fi

# Installer l'addon dans tous les addons Blender trouves
ADDON_SRC="$IO_SCENEMW_DIR/io_scene_mw"

if [ ! -d "$ADDON_SRC" ]; then
    echo "[-]  Addon directory not found: $ADDON_SRC"
else
    INSTALLED=0
    # Chercher les repertoires d'addons Blender existants
    while IFS= read -r BLENDER_ADDONS_DIR; do
        DEST="$BLENDER_ADDONS_DIR/io_scene_mw"
        if [ -d "$DEST" ]; then
            echo "[OK] io_scene_mw already installed in $BLENDER_ADDONS_DIR"
        else
            ln -s "$ADDON_SRC" "$DEST"
            echo "[OK] io_scene_mw linked in $BLENDER_ADDONS_DIR"
            INSTALLED=1
        fi
    done < <(find "$HOME/.config/blender" -path "*/scripts/addons" -type d 2>/dev/null || true)

    if [ "$INSTALLED" -eq 0 ] && [ -z "$(find "$HOME/.config/blender" -path "*/scripts/addons" -type d 2>/dev/null)" ]; then
        echo "[..] No Blender addons directory found (~/.config/blender/*/scripts/addons)"
        echo "     Launch Blender at least once, then rerun install.sh to link the addon."
    fi
fi

# ------------------------------------------
# Verification
# ------------------------------------------
echo ""
echo "========================================"
echo " Verification"
echo "========================================"
if [ -x "./tes3conv" ]; then echo "tes3conv: OK"; else echo "tes3conv: ERROR"; fi
if [ -x "$BSATOOL_DIR/bsatool" ]; then echo "bsatool:  OK (openmw-tools/)"; else echo "bsatool:  ERROR"; fi
if [ -d "$IO_SCENEMW_DIR" ]; then echo "io_scene_mw: OK (addon clone)"; else echo "io_scene_mw: ERROR"; fi
if command -v blender &>/dev/null; then echo "blender:  OK ($(blender --version 2>&1 | head -1))"; else echo "blender:  ERROR (not found in PATH)"; fi
echo ""
echo "Installation complete."

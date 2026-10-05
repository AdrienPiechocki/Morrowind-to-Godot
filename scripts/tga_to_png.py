import os
import sys
import bpy

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mwlog import vprint, eprint, Progress


def show_popup(message, title="Conversion result", icon="INFO"):
    # Errors always visible, other messages only with -v
    (eprint if icon == "ERROR" else vprint)(f"[{title}] {message}")


def get_cli_arg(name, default=None):
    """Value of --name in the arguments after '--' (Blender passes them through)."""
    args = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    for i, a in enumerate(args):
        if a == f"--{name}" and i + 1 < len(args):
            return args[i + 1]
    return default


# Load the .blend file saved by the previous script
blend_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "morrowind.blend")
for i, arg in enumerate(sys.argv):
    if arg == "--blend" and i + 1 < len(sys.argv):
        blend_path = sys.argv[i + 1]
        break

if os.path.exists(blend_path):
    bpy.ops.wm.open_mainfile(filepath=blend_path)
    vprint(f"[+] Loaded: {blend_path}")

# PNGs are written HERE, never next to the original textures (which live in the game /
# mod folders: possibly read-only, and we don't want to pollute them).
# Default: a "textures" folder next to the .blend. Shared between files with --textures-dir.
TEXTURES_DIR = os.path.abspath(
    get_cli_arg("textures-dir")
    or os.path.join(os.path.dirname(os.path.abspath(blend_path)), "textures")
)
vprint(f"[+] PNG output folder: {TEXTURES_DIR}")


def convert_all_tga_to_png():
    count = 0       # PNG written
    reused = 0      # PNG already present and up to date
    missing = 0
    failed = []     # (image name, error)

    # Check: the .blend file must be saved
    if not bpy.data.is_saved:
        show_popup(
            "Please save your .blend file first (Ctrl + S).",
            title="Unsaved file",
            icon="ERROR",
        )
        return

    os.makedirs(TEXTURES_DIR, exist_ok=True)

    images = list(bpy.data.images)
    bar = Progress(len(images), "Textures", disable=not images)
    bar.__enter__()

    for img in images:
        bar.update()
        filepath = bpy.path.abspath(img.filepath)
        is_tga = filepath.lower().endswith(".tga") or img.name.lower().endswith(
            ".tga"
        )
        if not is_tga:
            continue

        src_exists = os.path.exists(filepath)
        if not src_exists and not img.has_data:
            missing += 1
            continue

        # Case 1: the .tga exists on disk -> keep its file name.
        # Case 2: the image is only packed in Blender -> use the image name.
        stem = os.path.splitext(os.path.basename(filepath) if src_exists else img.name)[0]
        png_filepath = os.path.join(TEXTURES_DIR, stem + ".png")

        # Keep the original state to restore it if anything goes wrong
        old_path, old_format = img.filepath_raw, img.file_format
        try:
            img.file_format = "PNG"
            img.filepath_raw = png_filepath
            if src_exists and os.path.exists(png_filepath) and \
                    os.path.getmtime(png_filepath) >= os.path.getmtime(filepath):
                img.reload()          # PNG already converted by a previous file: reuse it
                reused += 1
            else:
                img.save()
                count += 1
        except Exception as e:
            # Don't abort the whole run: put the image back as it was and report it
            img.filepath_raw = old_path
            img.file_format = old_format
            failed.append((img.name, str(e).strip().splitlines()[0] if str(e).strip() else type(e).__name__))

    bar.close()

    if failed:
        eprint(f"[!] {len(failed)} texture(s) could not be converted (left unchanged):")
        for name, err in failed[:10]:
            eprint(f"    - {name}: {err}")
        if len(failed) > 10:
            eprint(f"    ... and {len(failed) - 10} more")

    # Confirmation popup
    done = count + reused
    if done > 0:
        msg = f"{done} TGA texture(s) converted to PNG successfully ({reused} reused)!"
        show_popup(msg, title="Success", icon="CHECKMARK")
    elif missing > 0:
        msg = (
            f"0 conversions performed.\n"
            f"{missing} TGA file(s) referenced but not found."
        )
        show_popup(msg, title="Path error", icon="ERROR")
    elif not failed:
        msg = "No .tga texture found in the scene."
        show_popup(msg, title="Information", icon="INFO")


# Execute script
convert_all_tga_to_png()

bpy.ops.wm.save_as_mainfile(filepath=blend_path)
vprint(f"[+] Saved: {blend_path}")
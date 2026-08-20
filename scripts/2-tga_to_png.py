import os
import sys
import bpy


def show_popup(message, title="Conversion result", icon="INFO"):
    print(f"[{title}] {message}")


# Load the .blend file saved by the previous script
blend_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "morrowind.blend")
for i, arg in enumerate(sys.argv):
    if arg == "--blend" and i + 1 < len(sys.argv):
        blend_path = sys.argv[i + 1]
        break

if os.path.exists(blend_path):
    bpy.ops.wm.open_mainfile(filepath=blend_path)
    print(f"[+] Loaded: {blend_path}")


def convert_all_tga_to_png():
    count = 0
    missing = 0

    # Check: the .blend file must be saved
    if not bpy.data.is_saved:
        show_popup(
            "Please save your .blend file first (Ctrl + S).",
            title="Unsaved file",
            icon="ERROR",
        )
        return

    for img in bpy.data.images:
        filepath = bpy.path.abspath(img.filepath)
        is_tga = filepath.lower().endswith(".tga") or img.name.lower().endswith(
            ".tga"
        )

        if is_tga:
            if os.path.exists(filepath):
                # Case 1: The .tga file exists on disk
                png_filepath = os.path.splitext(filepath)[0] + ".png"
                img.file_format = "PNG"
                img.filepath_raw = png_filepath
                img.save()
                count += 1
            elif img.has_data:
                # Case 2: The image is packed in Blender
                base_dir = bpy.path.abspath("//")
                clean_name = os.path.splitext(img.name)[0] + ".png"
                png_filepath = os.path.join(base_dir, clean_name)

                img.file_format = "PNG"
                img.filepath_raw = png_filepath
                img.save()
                count += 1
            else:
                missing += 1

    # Confirmation popup
    if count > 0:
        msg = f"{count} TGA texture(s) converted to PNG successfully!"
        show_popup(msg, title="Success", icon="CHECKMARK")
    elif missing > 0:
        msg = (
            f"0 conversions performed.\n"
            f"{missing} TGA file(s) referenced but not found."
        )
        show_popup(msg, title="Path error", icon="ERROR")
    else:
        msg = "No .tga texture found in the scene."
        show_popup(msg, title="Information", icon="INFO")


# Execute script
convert_all_tga_to_png()

bpy.ops.wm.save_as_mainfile(filepath=blend_path)
print(f"[+] Saved: {blend_path}")
"""Build the MA3_SAVA_Patch_Plugin release archive.

Bundles the importable plugin with its own README so the GitHub release can
offer one small download next to the installer and the portable build -- a
user should not have to unpack a 165 MB portable zip to get a 17 KB plugin.

Contents (inside a single top-level folder, so extracting never scatters
files into the user's Downloads directory):

    MA3_SAVA_Patch_Plugin/
        SAVA_patch.xml
        README.md

Run `build_xml.py` first if the .lua changed -- this script only packages
whatever `SAVA_patch.xml` currently holds.

    python build_archive.py
"""

import sys
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT_DIR = HERE / "dist"
ARCHIVE_NAME = "MA3_SAVA_Patch_Plugin"

# (source file, name inside the archive folder)
CONTENTS = (
    (HERE / "SAVA_patch.xml", "SAVA_patch.xml"),
    (HERE / "archive" / "README.md", "README.md"),
)


def main():
    missing = [str(src) for src, _ in CONTENTS if not src.exists()]
    if missing:
        sys.exit("missing input(s):\n  " + "\n  ".join(missing))

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    zip_path = OUT_DIR / f"{ARCHIVE_NAME}.zip"

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for source, arcname in CONTENTS:
            archive.write(source, f"{ARCHIVE_NAME}/{arcname}")

    print(f"wrote {zip_path} ({zip_path.stat().st_size} bytes)")
    with zipfile.ZipFile(zip_path) as archive:
        for info in archive.infolist():
            print(f"  {info.filename}  {info.file_size} bytes")


if __name__ == "__main__":
    main()

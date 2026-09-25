#!/usr/bin/env python3
"""Build the Dispatcharr plugin release zip.

Windows' Compress-Archive writes backslash path separators inside the zip
(e.g. "channel_visibility_manager\\plugin.py"). That's not valid per the ZIP
spec, and Dispatcharr's importer (running on Linux) doesn't treat a
backslash as a directory separator, so it never finds plugin.py inside the
mangled single-entry "folder". Always build the release zip with this
script (or any tool that writes POSIX-style forward slashes) instead.

Usage: python scripts/build_release_zip.py
"""
import json
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLUGIN_DIR = ROOT / "channel_visibility_manager"


def main():
    manifest = json.loads((PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))
    version = manifest["version"]
    out_path = ROOT / f"channel-visibility-manager-{version}.zip"

    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(PLUGIN_DIR.rglob("*")):
            if path.is_dir() or "__pycache__" in path.parts:
                continue
            arcname = path.relative_to(ROOT).as_posix()
            zf.write(path, arcname)

    print(f"Wrote {out_path}")
    with zipfile.ZipFile(out_path) as zf:
        for info in zf.infolist():
            print(f"  {info.filename}")


if __name__ == "__main__":
    main()

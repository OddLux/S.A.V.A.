"""Wrap SAVA_patch.lua into an importable grandMA3 plugin XML.

SAVA_patch.lua is the single source of truth. Re-run this after editing it.

Two output formats, both verified against real MA3 2.4.2 files on disk:

  embedded (default)
      Lua is base64-encoded into <FileContent><Block/></FileContent>, in
      1024-byte raw chunks. This is the format MA3 itself writes when you
      export a plugin, and it produces ONE self-contained .xml.

  external (--external)
      <ComponentLua FileName="SAVA_patch.lua"/> referencing the .lua beside
      it. This is the format MA3's own bundled examples use. Requires both
      files to be copied together.

NOT supported by MA3: inline Lua in a CDATA block. MA3 imports such a file
without error but the plugin ends up 0 B with no code -- silent failure.

Usage:
    python build_xml.py [--data-version 2.4.2.2] [--external]
"""

import argparse
import base64
import os
from pathlib import Path

HERE = Path(__file__).resolve().parent
LUA_PATH = HERE / "SAVA_patch.lua"
XML_PATH = HERE / "SAVA_patch.xml"

PLUGIN_NAME = "SAVA"
COMPONENT_NAME = "SAVA_patch"
BLOCK_SIZE = 1024  # raw bytes per <Block>, matching MA3's own exports


def _guid():
    """A 16-byte GUID in MA3's space-separated uppercase-hex form.

    If MA3 dislikes it on import it mints its own, which is harmless.
    """
    return " ".join(f"{byte:02X}" for byte in os.urandom(16))


def _blocks(data):
    """Base64 each 1024-byte chunk separately, as MA3 does."""
    out = []
    for pos in range(0, len(data), BLOCK_SIZE):
        chunk = data[pos : pos + BLOCK_SIZE]
        out.append(base64.b64encode(chunk).decode("ascii"))
    return out


def build_embedded(lua_text, data_version):
    data = lua_text.encode("utf-8")
    blocks = _blocks(data)
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<GMA3 DataVersion="{data_version}">',
        f'    <UserPlugin Name="{PLUGIN_NAME}" Guid="{_guid()}" Version="0.0.0.0">',
        f'        <ComponentLua Name="{COMPONENT_NAME}" Guid="{_guid()}">',
        f'            <FileContent Size="{len(blocks)}">',
    ]
    for block in blocks:
        lines.append(f'                <Block Base64="{block}" />')
    lines += [
        "            </FileContent>",
        "        </ComponentLua>",
        "    </UserPlugin>",
        "</GMA3>",
        "",
    ]
    return "\n".join(lines)


def build_external(data_version):
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<GMA3 DataVersion="{data_version}">\n'
        f'    <UserPlugin Name="{PLUGIN_NAME}" Version="0.0.0.0">\n'
        f'        <ComponentLua Name="{COMPONENT_NAME}" '
        f'FileName="{LUA_PATH.name}" />\n'
        "    </UserPlugin>\n"
        "</GMA3>\n"
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--data-version",
        default="2.4.2.2",
        help="MA3 DataVersion to stamp (match your console build)",
    )
    parser.add_argument(
        "--external",
        action="store_true",
        help="reference SAVA_patch.lua by filename instead of embedding it",
    )
    args = parser.parse_args()

    lua_text = LUA_PATH.read_text(encoding="utf-8")

    if args.external:
        xml = build_external(args.data_version)
        note = f"external reference to {LUA_PATH.name} (copy both files)"
    else:
        xml = build_embedded(lua_text, args.data_version)
        note = f"self-contained, {len(lua_text.encode('utf-8'))} B of Lua embedded"

    XML_PATH.write_text(xml, encoding="utf-8")
    print(f"wrote {XML_PATH}")
    print(f"  DataVersion {args.data_version}, {note}")


if __name__ == "__main__":
    main()

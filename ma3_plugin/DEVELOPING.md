# Developing the SAVA grandMA3 plugin

Build details and hard-won API caveats. For installing and running the plugin,
see [`README.md`](README.md).

Edit `SAVA_patch.lua` -- it is the single source of truth -- then regenerate the
XML.

## Rebuilding the XML

```sh
python build_xml.py                        # DataVersion 2.4.2.2 (default)
python build_xml.py --data-version 2.2.5.0 # match a different console build
python build_xml.py --external             # reference the .lua instead
```

### The XML format matters

MA3 does **not** support inline Lua in a CDATA block. An XML built that way
imports with no error, then shows **0 B and no code** in the plugin editor -- a
silent failure. Two formats actually work, both verified against real MA3 2.4.2
files:

- **embedded** (default) -- Lua base64-encoded into `<FileContent>` as
  1024-byte `<Block>` chunks. This is what MA3 writes when *it* exports a
  plugin. One self-contained file.
- **external** (`--external`) -- `<ComponentLua FileName="SAVA_patch.lua"/>`
  referencing the .lua beside it, the format MA3's bundled examples use. Both
  files must be copied together.

## Notes / caveats

- **Group "Additive"** is `Enums.GroupMasterMode.Additive`. The plugin sets it
  via the object property, falling back to
  `Set Group N Property "Mode" "Additive"`. If both fail it logs a warning per
  group and keeps going -- it never aborts the patch. Check the command-line
  feedback after a run.
- If you remap channels in SAVA, edit `SAVA_CHANNELS` in the .lua, then rerun
  `build_xml.py`.
- **It does not use `AddFixtures()`.** That function returned nil for every
  call here even with a valid mode handle and the correct destination. The
  plugin instead follows the command-line sequence the console itself uses in
  `shared/resource/lib_plugins/systemtests/help/system_test_helping_functions_patch.lua`
  (`Store` -> `Assign FixtureType` -> `Set property FID/Name/Mode/Patch`).
- **Entering the patch is mandatory.** Outside it, `Patch()` resolves to
  `LivePatch` and writes go nowhere, so the plugin does
  `cd Root` -> `cd 'ShowData'.'Patch'` first and `cd Root` at the end.
- The fixture position index in those commands is the slot in the stage's
  Fixtures list, **not** the Fixture ID. New fixtures append after whatever is
  already patched.
- `Cmd()` returns **`"OK"` in uppercase** on success. Comparing against `"Ok"`
  silently discards valid results.
- Numeric `ChangeDestination` chains (`13.9...`, `14.10...`) are version
  specific and are not used -- the plugin resolves the address with `ToAddr()`
  instead.
- `STAGE_INDEX` at the top of the .lua selects which stage to patch into
  (default 1). `PREFIX` prepends a string to every fixture and group name.

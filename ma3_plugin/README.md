# SAVA grandMA3 patch plugin

Patches a grandMA3 show for SAVA ArtNet control in one shot: 18 single-channel
dimmers, one per SAVA function, each with its own Group.

Unofficial. Not affiliated with, endorsed by, or supported by MA Lighting.

## What it does

You are prompted for **Universe**, **Start Fixture ID** and **Start Group
number**. The plugin then:

1. Patches 18 single-channel dimmers at `<universe>.<SAVA channel>` -- channels
   1-18, matching SAVA's `config/artnet_config.ini`.
2. Names each fixture after the function it drives (`Play`, `Stop`,
   `Cue 1` ... `Cue 8`).
3. Stores one Group per fixture, numbered sequentially from your start value,
   with Mode set to **Additive**.

Fixture IDs run sequentially from the start value. DMX addresses do **not** --
they are fixed by SAVA's channel map:

| Order | Name | DMX ch |
|---|---|---|
| 1 | Play | 1 |
| 2 | Pause | 2 |
| 3 | Stop | 3 |
| 4 | Next track | 4 |
| 5 | Prev track | 5 |
| 6 | Master volume | 6 |
| 7 | Seek | 7 |
| 8 | Track sel enable | 8 |
| 9 | Track select | 9 |
| 10 | Loop AB | 10 |
| 11-18 | Cue 1 .. Cue 8 | 11-18 |

## Install

`SAVA_patch.xml` is self-contained -- the `.lua` does not need to travel with
it.

**Copy the file into the console's plugin library:**

```
gma3_library/datapools/plugins/SAVA_patch.xml
```

- **grandMA3 onPC (Windows):**
  `C:\ProgramData\MALightingTechnology\gma3_library\datapools\plugins\`
- **Console:** the same `gma3_library/datapools/plugins/` path on the internal
  drive, or on a USB stick -- the console reads a `gma3_library` folder at the
  root of the stick.

Then in the show: open the **Plugins** pool, tap an empty slot, and import
`SAVA_patch`. Run it by tapping the plugin, or with:

```
Call Plugin "SAVA"
```

Progress and any failures are printed to the command line feedback, each line
prefixed `[SAVA]`.

## Requirements

- A single-channel dimmer fixture type must exist in the show. The plugin looks
  for one named `Dimmer`, `Generic Dimmer` or `Dim`, and tries to import
  `generic@dimmer` from the GrandMA2 library if none is present. If that fails
  it aborts with a message -- import a dimmer via
  **Patch -> Fixture Types -> Import** and rerun.
- Re-running appends another set of fixtures; it does not clean up a previous
  run. Delete the old fixtures and groups first if you are re-patching.
- The Fixture IDs and DMX addresses you choose must be free. There is no
  collision pre-flight: if one is already taken, that single fixture fails, its
  Group is skipped, and the run continues with the rest -- leaving a partial
  patch. Each failure is logged to the command line, so check the feedback.

## Files

| File | Role |
|---|---|
| `SAVA_patch.xml` | The importable plugin. This is the file you copy to the console. |
| `SAVA_patch.lua` | Plugin source. Single source of truth -- edit this. |
| `build_xml.py` | Regenerates the `.xml` from the `.lua`. |
| `DEVELOPING.md` | XML format details, rebuild instructions, API caveats. |

## Status

Verified against the grandMA3 API documentation and MA3's on-disk file formats,
not on a live console. Test on a spare show file before using it on a
production show.

## License

Part of SAVA and covered by the same license: **GNU General Public License
v3.0** -- see [`../LICENSE`](../LICENSE). The plugin's Lua source is original
work; no MA Lighting code is reproduced or redistributed here.

grandMA3 and MA are trademarks of MA Lighting Technology GmbH, used here only
to describe what this plugin is for.

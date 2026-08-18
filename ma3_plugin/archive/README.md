# SAVA grandMA3 patch plugin

Patches a grandMA3 show for SAVA ArtNet control in one shot: 18 single-channel
dimmers, one per SAVA function, each with its own Group.

Unofficial. Not affiliated with, endorsed by, or supported by MA Lighting.

## What it does

You are prompted for **Universe**, **Start Fixture ID** and **Start Group
number**. The plugin then:
1. Patches 18 single-channel dimmers at `<universe>.<SAVA channel>` -- channels
   1-18, matching SAVA's default  `config/artnet_config.ini`.
2. Names each fixture after the function it drives (`Play`, `Stop`,
   `Cue 1` ... `Cue 8`).
3. Stores one Group per fixture, numbered sequentially from your start value,
   with Mode set to **Additive**.

## Install
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
## License

Part of SAVA and covered by the same license: **GNU General Public License
v3.0** -- see [`../LICENSE`](../LICENSE). The plugin's Lua source is original
work; no MA Lighting code is reproduced or redistributed here.

grandMA3 and MA are trademarks of MA Lighting Technology GmbH, used here only
to describe what this plugin is for.

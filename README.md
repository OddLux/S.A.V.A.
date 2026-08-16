# SAVA — Synchronizing Audio Via Art-net

SAVA is a Windows desktop audio player with a Winamp-style interface that can
be driven remotely over the network by **ArtNet / DMX**. A lighting console
sends ArtNet packets to SAVA, and channel values map to playback actions —
play, pause, track select, cue jumps, master volume, A-B loop, and more — so a
show's audio can be cued from the same desk that runs the lights.

Built with Python and PyQt6.

![Version](https://img.shields.io/badge/version-3.0.1-blue)
![License](https://img.shields.io/badge/license-GPL--3.0-green)
![Platform](https://img.shields.io/badge/platform-Windows-lightgrey)

## Features

- **Dual-deck streaming playback** with gapless track transitions and
  configurable bezier-curve **crossfades**.
- **ArtNet / DMX remote control** — map console channels to playback
  functions, with a per-row "Learn" mode in the mapping dialog.
- **8 cue points**, **A-B loop**, and per-track **Start / Fin markers**.
- **Waveform display** with click/drag scrubbing, plus a preview waveform of
  the queued-next track.
- Per-track **volume**, **color labels**, and **end actions** (next / loop /
  stop).
- Playlist with shuffle and repeat (none / one / all); **M3U** and **PLS**
  import/export; **`.savashow`** export/import that carries cues and markers.
- Selectable **color themes** and audio **output-device** selection.

## Supported audio formats

MP3, WAV, FLAC, AAC, M4A, OGG, Opus, WMA, AIFF/AIF — decoded via the bundled
FFmpeg.

## Running from source

Requires Python 3.11+ on Windows. Dependencies: `PyQt6`, `sounddevice`,
`numpy`, `mutagen`. `ffmpeg.exe` / `ffprobe.exe` are bundled in `assets/`.

```sh
python -m venv venv
venv\Scripts\pip install PyQt6 sounddevice numpy mutagen
venv\Scripts\python main.py
```

## ArtNet / DMX setup

SAVA listens on UDP port **6454** (the ArtNet standard). Configure your
subnet, universe, and per-function channel mapping via **ArtNet → Configure
DMX mapping…** in the app, or by editing `config/artnet_config.ini`. SAVA is
**receive-only** — it never transmits DMX.

## License

SAVA is free and open-source software, released under the **GNU General Public
License v3.0** — see [`LICENSE`](LICENSE).

This license is required because SAVA uses PyQt6 under its GPL option and
bundles a GPL build of FFmpeg. In plain terms: you may use, study, modify, and
redistribute SAVA freely, but any distributed derivative must also be
GPL-3.0 and ship its source. See [`THIRD-PARTY-NOTICES.md`](THIRD-PARTY-NOTICES.md)
for the full dependency license breakdown and the FFmpeg source offer.

## Support

SAVA is free. If it's useful to you and you'd like to support development,
donations are welcome — see the repository's sponsor/donation link.

---

Developed by Massimo — Sava Kisiov, for OddLux.

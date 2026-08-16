# Third-Party Notices

SAVA is distributed under the GNU General Public License v3.0 (see `LICENSE`).
It bundles and/or depends on the following third-party components. Their
licenses are listed here for compliance and attribution.

## Bundled binaries

### FFmpeg (`assets/ffmpeg.exe`, `assets/ffprobe.exe`)

- **License:** GNU General Public License v3 (this is a `--enable-gpl
  --enable-version3` build).
- **Version:** 8.1 "essentials" build from https://www.gyan.dev/ffmpeg/builds/
- **Upstream source:** https://ffmpeg.org/download.html
- **Source offer:** FFmpeg's complete corresponding source is available from
  https://ffmpeg.org/releases/ and https://www.gyan.dev/ffmpeg/builds/. The
  bundled binaries are unmodified redistributions.

Because the bundled FFmpeg is a GPL build, and because PyQt6 is used under its
GPL option (see below), SAVA as a whole is licensed under GPL-3.0.

## Python dependencies

| Package | License | Notes |
|---------|---------|-------|
| PyQt6 | GPL v3 **or** Riverbank Commercial | Used here under the **GPL v3** option. This is the primary reason SAVA must be GPL-3.0. |
| mutagen | GPL v2.0-or-later | Compatible with GPL-3.0 (upgraded to v3 under the "or later" clause). |
| numpy | BSD 3-Clause | Permissive; compatible. |
| sounddevice | MIT | Permissive; compatible. Wraps PortAudio (MIT-style license). |

`pygame` and `pyartnet` appear in the development virtual environment but are
**not imported** by the shipped application (phantom dependencies — see
`CLAUDE.md`), so they impose no obligation on the release. `pydub` is imported
only as a never-executed lazy fallback in `core/analyzer.py`; it is
MIT-licensed and compatible regardless.

## What GPL-3.0 requires of anyone redistributing SAVA

1. Keep this notice and the `LICENSE` file with any copy you distribute.
2. Make the complete corresponding source code available to recipients
   (the public repository satisfies this).
3. Preserve the FFmpeg source offer above.
4. Any derivative you distribute must also be licensed GPL-3.0.

# Changelog

All notable changes to SAVA are recorded here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versions follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html) and
are sourced from the plain-text `VERSION` file at the repo root — the single
source both `core/version.py` and `installer.iss` read.

**How this file is maintained.** Entries are collected **at release time**, not
per commit. Each ticket in `.tickets/` carries its own `## Changelog update`
section written while the work was fresh; when `VERSION` is bumped, the notes
from every ticket closed since the previous release are gathered here. The
tickets stay the working record — this file is the published summary for anyone
holding an installed build rather than the repo.

**History before 2.0.0** is not reconstructed here. SAVA V2 was developed
ticket-first with no changelog, and inventing per-version entries after the fact
would produce a record that looks authoritative but was never verified. For
anything predating this file, read `.tickets/closed/` — that is the accurate
account.

---

## [3.0.1]

### Changed

- Title bar no longer says "v2". It read `SAVA v2 - …` regardless of the
  actual version; the running version is in Help -> About, which reads the
  real VERSION file. (dp-254)
- **Track Start now sits above Track End** in the transport. A track's start
  point precedes its end point, so the group reads top-to-bottom in the order
  the markers actually occur. (dp-254)

---

## [3.0.0]

### Added

- **Show files.** `File -> Export Show…` writes a `.savashow` next to your
  audio, carrying everything M3U and PLS throw away: cue points, Start and Fin
  markers, per-track volumes, colour labels, end actions, and the crossfade
  layout. `Import Show…` restores all of it. Track paths are stored relative to
  the show file, so a show folder copied to another machine or drive letter
  just works. Audio is never copied — you keep one folder per show with the
  tracks and the `.savashow` in it. If a track is missing, the rest of the show
  still loads and the transition that spanned it falls back to no crossfade;
  both export and import warn you by name about anything that will not resolve,
  and neither ever blocks. (dp-246)

### Fixed

- The Learn button in the DMX mapping dialog can be switched off again. Arming
  a row disabled its own button, so once you had armed Learn the only ways out
  were to send a DMX change — assigning a channel you might not have wanted —
  or to close the dialog. Click Learn to arm, click Cancel to stop. (dp-249)
- ArtNet no longer silently drops DMX packets on a busy network. The listener
  read at most 50 packets per 50 ms tick — a hard ceiling of 1000 packets/sec,
  which a rig broadcasting roughly 23 or more universes exceeds. Past that the
  backlog filled the kernel buffer and packets were discarded, and a discarded
  packet on a trigger channel is a **missed cue**, not merely a late one. The
  listener now drains until the socket is empty and asks for a 1 MiB receive
  buffer. (dp-247)
- The "Listener enabled" checkbox in the DMX dialog actually works. It wrote a
  setting nothing read, while a separate hidden setting decided whether the
  listener ran — so the config file could say the listener was off while SAVA
  listened anyway. The config file is now the single source of truth, and
  editing it in a text editor starts or stops the listener. (dp-247)
- Two latent races that could kill the audio stream outright and silence
  playback until restart. Starting a fade (play-with-fade-in, fade-out-to-stop)
  published its "fade in progress" flag before the fade length it depends on,
  so an audio callback landing between the two raised on the realtime thread;
  and switching output device could null the crossfade curve while a callback
  was mid-crossfade. Both are now published/snapshotted in a consistent order.
  (dp-245)
- Removing or reordering a playlist row no longer mis-tracks which track is
  current. Removing any row *above* the playing one, or dragging any other row
  across it, left the internal index pointing at a different track — so the
  highlight, the "Track N of M" counter, the end-of-track action and the
  gapless preload prediction all referred to the wrong row. (dp-245)
- Seeking past the point a track has decoded to no longer freezes the window
  for up to five seconds. Scrubbing the waveform, jumping to a cue, or driving
  the ArtNet seek fader could each block the UI on the decoder; the wait is now
  bounded at 250 ms and playback simply catches up. (dp-245)
- Saving the DMX mapping now writes the config file atomically, so a crash
  mid-save can no longer truncate the whole mapping, and SAVA's own
  config-file watcher can no longer reload a half-written file. (dp-245)

### Changed

- Rewrote the in-app instructions (**Help -> Instructions**). They now cover
  show files, audio-device selection, the Start marker, the preview waveform,
  timecode modes and the crossfade scrub slider, and no longer describe three
  things that do not exist: a playlist "Set Cue 1-4" menu removed in an earlier
  release, a crossfade Preview transport, and a "Presets tab" that never
  shipped. Trimmed throughout. Headings now follow the selected theme instead
  of being hardcoded green. (dp-250)
- About now reads "Developed by Massimo - Sava Kisiov, for OddLux" and credits
  the libraries SAVA actually runs on. (dp-250)
- Dragging the waveform no longer scrubs the audio while you drag. The needle
  follows the cursor and playback jumps once, when you release. Previously
  every mouse-move triggered a real seek, so one drag meant hundreds of seeks
  and dozens of throwaway decoder threads. (dp-247)
- The log file is rotated at startup once it passes 5 MB (one previous run is
  kept as `sava.log.1`). It previously only ever grew. (dp-245)
- SAVA reports its real version to the OS instead of a hardcoded "1.0.0" that
  had drifted from the `VERSION` file. (dp-245)
- Groundwork for non-Windows builds: the bundled decoder is located by a
  platform-aware name rather than a hardcoded `.exe`, the audio host-API
  preference list includes Core Audio/ALSA, opening the DMX config file falls
  back to a portable handler, and `pydub` is no longer a hard import that could
  take the whole app down at startup. Windows behaviour is unchanged. (dp-245)
- Waveform display is dramatically cheaper to render. The envelope is now
  rasterized once into a cached pixmap and reused, instead of being redrawn
  column-by-column in Python on every 10 Hz position tick. Measured ~15.7x
  faster per paint (0.30 ms vs 4.73 ms); the played/unplayed colouring is
  pixel-for-pixel identical to before. (dp-225)
- Dragging a track in the crossfade timeline no longer rebuilds the entire
  scene on every mouse-move. Renders are coalesced to roughly 60 Hz, and the
  expensive overlap-waveform pass is skipped mid-drag and restored at full
  fidelity on release. (dp-222)
- Removed a dead ArtNet feedback block in `_load_and_play` that called two
  functions which never existed (`send_feedback`, `get_dmx`) and silently
  swallowed the resulting error on every track load. SAVA remains receive-only
  over ArtNet. (dp-210)

### Fixed

- Elapsed and remaining timers in the combined ("both") timecode display now
  update on the same tick. They were previously derived from two independently
  truncated values, so they drifted apart by an amount determined by the
  track's duration — invisible on some tracks, obvious on others. (dp-233)
- A right-click on a crossfade timeline track no longer begins a drag that a
  later mouse-move acts on. (dp-222)
- A crossfade timeline drag whose mouse-release is never delivered (released
  outside the viewport, or the grab broken by a modal) no longer leaves overlap
  waveforms permanently hidden. (dp-222)

### Changed

- **v3.0.0 default DMX mapping.** New installs now assign every ArtNet
  function a unique channel, 1 through 18, with no gaps and no duplicates --
  the previous defaults had Seek and Track Select Enable both defaulting to
  channel 7. Master Volume, Seek, and Track Select Enable now default to
  **disabled**; every other function still defaults to enabled. Existing
  installs are unaffected -- this only changes what a fresh `artnet_config.ini`
  is seeded with. (dp-251)

---

## [2.0.0]

Current baseline as of the v3.0.0 release notes above. `VERSION` was established as the single source of truth for
both the About dialog and the Inno Setup installer, replacing hardcoded strings
and fixing an installer that had been packaging V1's build output. (dp-227)

See `.tickets/closed/` for the full record of work that went into 2.0.0.

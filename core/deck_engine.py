"""dp-216 Phase 1/2a: sounddevice + numpy streaming playback engine.

Replaces the single pygame.mixer.music stream with a real audio callback
mixer. Phase 1 scope was a single active deck reaching feature parity with
core/engine.py.AudioEngine's public API. Phase 2a (this revision) adds a
second deck and ping-pongs A/B for sample-accurate gapless auto-advance,
still fully in-memory (D6's resident branch); the hybrid ring-buffer
streaming branch for 60-min+ tracks is Phase 2b.

Decode: bundled ffmpeg -> raw s16le stereo PCM at the OUTPUT DEVICE's native
sample rate (D2/R7 -- PortAudio does not resample, so a hardcoded 44100
would silently fail or play pitched on a 48000 Hz WASAPI device).

Audio callback discipline (R2): core.deck_engine._callback and Deck.read_block
/ Deck.fill_into / Deck.advance / Deck.fade_envelope never allocate beyond the
block-sized arrays sounddevice already asks for, never take a lock, and never
decode -- all of that happens on background threads. The callback only reads
plain attributes (int/float assignment is atomic under the GIL) and numpy
views.

Phase 2a correctness rules (see the saved plan, "Confirmed decisions" +
"Architecture"):
1. The audio callback (`_callback`, via `_drain_commands`) is the SOLE
   mutator of `read_idx` and the `_active`/`_idle` deck references.
   Control-thread methods (`seek`, `swap_to_preloaded`, `invalidate_preload`)
   push a command onto `self._command_queue` (a `collections.deque`,
   thread-safe append/popleft under the GIL) instead of mutating directly.
2. Auto-advance stitches on `active.just_ended` (a TRUE end: end-marker or
   decode-complete frontier), NEVER on `produced < frames` -- a mid-track
   frontier underrun outputs silence and does not skip tracks.
3. `_idle_armed` is the sole authority for "idle deck holds a ready decode"
   (manual `swap_to_preloaded` is keyed on this alone). Set True only when
   `preload()` finishes filling the idle deck; cleared the instant the
   callback consumes idle (either swap route) and on `invalidate_preload()`.
   A spent (just-finished) deck landing in the idle slot is never
   re-stitched -- the poll thread unloads it. dp-254: whether a READY idle
   deck is also allowed to auto-stitch at natural end is a SEPARATE flag,
   `_auto_advance_armed` (defaults True; `preload()` re-arms it; only
   `ui/main_window.py`'s `_rearm_preload` opts out for a `stop`/`loop`
   end-action). Both the gapless swap and the crossfade trigger gate on
   `_idle_armed and _auto_advance_armed` together.
4. `preload(fp)` no-ops ONLY when `_idle_armed and _idle.filepath == fp`;
   every other case (including a SPENT idle deck sharing that filepath --
   the A/B/A/B short-playlist case) tears down and rebuilds from scratch.
   The swap activates the incoming deck with no fade-in (gapless);
   `fill_into` applies deck-local gain only, the engine post-multiplies
   master volume + clips once over the whole mixed block.
"""

import collections
import os
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import numpy as np
import sounddevice as sd
from mutagen import File as MutagenFile

from config.settings import settings
from core.subproc import popen_kwargs
from core.track_identity import ROW_KEY_MAP


def _row_or_file_key(track_id, filepath, file_key):
    """dp-237: pick which settings map/key a per-track read or write should
    use. A deck carrying a track_id (i.e. loaded via the playlist) uses the
    row-keyed map so duplicate rows of the same file diverge; a deck loaded
    without one (direct engine.load()/preload() calls, e.g. existing tests)
    falls back to the legacy file-keyed map -- unchanged behaviour."""
    if track_id:
        return ROW_KEY_MAP[file_key], track_id
    return file_key, filepath

# dp-244: the bundled binary's filename is platform-specific. On Windows this
# resolves to the same assets/ffmpeg.exe it always did; elsewhere it looks for
# an extensionless assets/ffmpeg, and falls back to whatever is on PATH either
# way. Naming it here rather than hardcoding ".exe" is the one change that has
# to happen in EVERY module that shells out, so it is centralised.
_EXE_SUFFIX = ".exe" if os.name == "nt" else ""
_ASSETS_DIR = Path(__file__).resolve().parent.parent / "assets"


def _bundled_tool(name: str) -> str:
    """Absolute path to a bundled ffmpeg-family binary, or the bare name so
    the OS resolves it from PATH when nothing is bundled."""
    candidate = _ASSETS_DIR / f"{name}{_EXE_SUFFIX}"
    return str(candidate) if candidate.exists() else name


_FFMPEG_BIN = _bundled_tool("ffmpeg")
_FFPROBE_BIN = _bundled_tool("ffprobe")

STATE_STOPPED = "stopped"
STATE_PLAYING = "playing"
STATE_PAUSED = "paused"

_BLOCK_SIZE = 1024
_POLL_INTERVAL = 0.1
# Prebuffer `play()` waits for before the deck starts sounding. ffmpeg decodes
# far faster than realtime, so 0.3s is reached in a few ms on a healthy track;
# the point of the wait is only to stop the first block being silence. It runs
# on the Qt main thread (main_window calls play() directly), so this doubles as
# the worst-case UI hitch -- bounded further by _PREBUFFER_TIMEOUT.
_PREBUFFER_SECONDS = 0.3
# Hard cap on that wait. A track whose decode never produces anything must
# fail fast rather than freeze the window (dp-216 Phase 6 live-pass finding).
_PREBUFFER_TIMEOUT = 1.0
# Hard cap on `seek()`'s wait for the decode frontier. seek() runs on the Qt
# main thread (waveform drag-scrub emits one per mouse-move, ArtNet emits one
# per DMX frame), so this is a direct UI-freeze budget -- see seek()'s comment
# for why giving up early is correct rather than merely tolerable.
_SEEK_WAIT_TIMEOUT = 0.25
_DECODE_CHUNK_FRAMES = 65536
_PRELOAD_ARM_TIMEOUT = 60.0
_SWAP_COMMAND_TIMEOUT = 0.25
# dp-221: how long the incoming deck takes to reach full gain when a crossfade
# is TRUNCATED (the outgoing track ended before the ramp finished). Short
# enough to be inaudible as a fade, long enough to not be a step -- a one-block
# jump from a partial gain to 1.0 is a click.
_TRUNCATED_FADE_MS = 30

# Phase 2b (D6 hybrid memory): tracks over this length stream from a
# temp-file-backed np.memmap instead of a full RAM np.zeros buffer.
_RESIDENT_CAP_SECONDS = 15 * 60
# W4: arm the idle deck once it has this much prebuffer, not only on full
# decode_complete -- an over-cap idle deck may take minutes to fully decode,
# far longer than a short active track's remaining runtime (B10).
_ARM_PREBUFFER_SECONDS = 3.0
# W2/B4: pre-size margin added to the probed-duration frame count to absorb
# ffprobe estimate error, so overflow-truncation is a rare safety net, not
# the normal path.
_MMAP_MARGIN_SECONDS = 10.0


def _ffprobe_duration(filepath: str):
    """Authoritative duration via the bundled ffprobe (W1) -- used for both
    the resident-cap mode decision and the mmap pre-size. mutagen alone can
    under-report (or return 0 for untagged files); a too-small pre-size
    overflows the mmap and a mis-classified long track balloons RAM. Returns
    None (caller falls back to mutagen, then a default) on any failure."""
    try:
        proc = subprocess.run(
            [
                _FFPROBE_BIN, "-v", "error", "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1", filepath,
            ],
            **popen_kwargs(
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, timeout=10,
            ),
        )
        out = proc.stdout.strip()
        if out:
            value = float(out)
            if value > 0:
                return value
    except Exception:
        pass
    return None


# Host APIs to try, best first (dp-216 D2/R7). PortAudio's own "default output
# device" is whatever its FIRST host API reports -- on Windows that is MME,
# whose default is frequently an HDMI display's audio endpoint rather than the
# user's Windows default playback device. Selecting the WASAPI host API's
# default instead is what makes SAVA follow the Windows default device, at
# shared-mode latency, on an arbitrary machine (Phase 6 live-pass finding).
# DirectSound and MME remain as fallbacks for hosts without WASAPI.
# dp-244: the non-Windows entries are additive and inert on Windows -- no
# PortAudio build reports a "Core Audio" or "ALSA" host API there, so the
# lookup simply never matches and the Windows ordering above is untouched.
# They exist so a macOS/Linux run picks a real host API instead of falling
# through to PortAudio's own default (the MME-equivalent trap this tuple was
# written to avoid in the first place).
_HOST_API_PREFERENCE = (
    "Windows WASAPI", "Windows DirectSound", "MME",
    "Core Audio", "ALSA", "JACK Audio Connection Kit",
)


FOLLOW_SYSTEM_DEFAULT = "__system_default__"
_DEVICE_SETTING_KEY = "output_device_name"


def list_output_devices():
    """dp-223: [(index, name, hostapi_name)] for every device that can
    actually play stereo, best host API first.

    Keyed for the UI by NAME, not index: PortAudio device indices are
    positional in its own enumeration and shift whenever a device is added,
    removed, or re-enumerated (dock plugged in, monitor woken, driver
    restarted). Persisting an index would silently point at a different
    device later; the name survives.
    """
    devices = []
    seen_names = set()
    try:
        hostapis = sd.query_hostapis()
        all_devices = sd.query_devices()
    except Exception:
        return devices

    api_rank = {}
    for rank, preferred in enumerate(_HOST_API_PREFERENCE):
        for api_index, api in enumerate(hostapis):
            if api.get("name") == preferred:
                api_rank[api_index] = rank

    for index, info in enumerate(all_devices):
        if info.get("max_output_channels", 0) < 2:
            continue
        api_index = info.get("hostapi")
        if api_index not in api_rank:
            continue
        name = info.get("name", "").strip()
        if not name:
            continue
        key = (name, api_index)
        if key in seen_names:
            continue
        seen_names.add(key)
        api_name = hostapis[api_index].get("name", "")
        devices.append((index, name, api_name, api_rank[api_index]))

    devices.sort(key=lambda row: (row[3], row[1].lower()))
    return [(index, name, api) for index, name, api, _rank in devices]


def _resolve_device_by_name(name):
    """Index of the first stereo-capable output device whose name matches,
    or None. Returning None is the normal 'that device is gone' path (the
    monitor was unplugged, the dock was removed) -- callers fall back to
    following the system default rather than failing to start."""
    if not name or name == FOLLOW_SYSTEM_DEFAULT:
        return None
    for index, dev_name, _api in list_output_devices():
        if dev_name == name:
            return index
    return None


def _output_device_candidates(preferred_name=None):
    """Ordered (device_index, samplerate) candidates for the output stream.

    dp-223: when `preferred_name` names a device that currently exists, it is
    tried FIRST; everything below stays as the fallback chain, so an
    unplugged saved device degrades to following the system default instead
    of leaving SAVA silent.

    Ordered by `_HOST_API_PREFERENCE`, then PortAudio's own default as a last
    resort, then a bare (None, 44100) so a machine with no usable output at
    all (CI/headless) still constructs an engine instead of raising at import
    time. Each candidate carries the DEVICE's native rate: PortAudio does not
    resample, so the stream rate and every deck's decode rate must match the
    device actually opened, not a global assumption.
    """
    candidates = []
    seen = set()

    def _add(index):
        if index is None or index < 0 or index in seen:
            return
        try:
            info = sd.query_devices(index)
        except Exception:
            return
        if info.get("max_output_channels", 0) < 2:
            return
        rate = int(round(info.get("default_samplerate") or 0))
        seen.add(index)
        candidates.append((index, rate if rate > 0 else 44100))

    _add(_resolve_device_by_name(preferred_name))

    try:
        hostapis = sd.query_hostapis()
    except Exception:
        hostapis = ()
    for name in _HOST_API_PREFERENCE:
        for api in hostapis:
            if api.get("name") == name:
                _add(api.get("default_output_device"))

    try:
        info = sd.query_devices(kind="output")
        _add(info.get("index"))
    except Exception:
        pass

    candidates.append((None, 44100))
    return candidates


class Deck:
    """One playback slot. Holds a background-decoded int16 stereo buffer and
    all per-track state (loop points, cues, end marker, track volume) so a
    fully-loaded deck is self-contained -- the design Phase 2a's A/B rotation
    depends on. The `_frontier` index gates every callback read (D3): the
    callback can never play audio decode hasn't produced yet."""

    def __init__(self, sample_rate: int):
        self.sample_rate = sample_rate
        self.filepath = None
        # dp-237: the playlist row id this deck is currently loaded for, set
        # by DeckEngine.load()/preload() (never inside _callback -- rule #1).
        # None when the caller didn't pass one (direct engine.load() calls,
        # e.g. existing tests) -- per-track settings then fall back to the
        # legacy file-keyed maps, unchanged behaviour.
        self.track_id = None
        self.duration = 0.0
        self.active = False

        self.gain = 1.0          # crossfade ramp multiplier (Phase 3 hook)
        self.track_volume = 1.0

        self.loop_a = None
        self.loop_b = None
        self.loop_active = False
        self.cue_points = {}
        self.end_marker = None
        self.start_marker = None

        self.read_idx = 0
        self.just_ended = False    # natural end-of-track, cleared by poller
        self.pending_stop = False  # fade-out-to-stop completed, not a "natural end"

        self._buf = np.zeros((0, 2), dtype=np.int16)
        self._frontier = 0
        self.decode_complete = False  # set True by the worker on clean ffmpeg EOF

        # Phase 2b (D6): True when self._buf is a temp-file-backed np.memmap
        # (over-cap track) rather than a resident np.ndarray. _temp_path is
        # the backing file, retired via DeckEngine's deferred-close list
        # (W3) -- never closed synchronously while a reader might still hold
        # this deck as engine._active / engine._idle (B13).
        self._streamed = False
        self._temp_path = None

        self._decode_thread = None
        self._decode_proc = None
        self._decode_stop = threading.Event()

        # Fade envelope, expressed purely in frame indices so the callback
        # needs no wall-clock math. `_fade_start is None` means no fade is in
        # progress; the deck then holds at `_fade_level` -- the gain the last
        # fade settled into (1.0 by default, 0.0 after a completed fade-out).
        # Holding at _fade_level, not snapping back to unity, is what stops a
        # completed fade-out from blasting full volume for the ~100 ms before
        # the poll thread stops the deck.
        self._fade_start = None
        self._fade_len = None
        self._fade_from = None
        self._fade_to = None
        self._fade_stop_after = False
        self._fade_level = 1.0

    # -- decode (background thread only) -----------------------------------

    def load(self, filepath: str, duration: float, temp_dir: str = None):
        """Allocate this deck for `filepath`. Caller is expected to have
        already retired any live prior buffer via DeckEngine._retire (W3) --
        stop_decode()+detach_buffer() here are a defensive, idempotent
        self-cleanup, not the primary teardown path (a Deck constructed
        fresh, or already retired, has nothing to close). Buffer type is
        chosen from the authoritative `duration` (W1, ffprobe): under the
        resident cap -> RAM np.zeros (2a path, unchanged); over the cap ->
        a pre-sized np.memmap backed by a temp file under `temp_dir` (W2).
        `temp_dir` is None only in defensive/test contexts -- treated as
        "always RAM"."""
        self.stop_decode()
        self.detach_buffer()
        self.filepath = filepath
        self.track_id = None  # engine sets this right after calling load()
        self.duration = duration
        self.read_idx = 0
        self.just_ended = False
        self.pending_stop = False
        self.loop_a = None
        self.loop_b = None
        self.loop_active = False
        self.cue_points = {}
        self.end_marker = None
        self.start_marker = None
        self._fade_start = None
        self._fade_level = 1.0
        self.gain = 1.0  # A11: a deck reloaded after finishing as the ramped-
        # out side of a crossfade (gain ~0.0) must not start silent.

        use_mmap = temp_dir is not None and duration > _RESIDENT_CAP_SECONDS
        if use_mmap:
            try:
                pre_frames = int(duration * self.sample_rate) + int(
                    _MMAP_MARGIN_SECONDS * self.sample_rate
                )
                pre_frames = max(pre_frames, self.sample_rate)
                fd, temp_path = tempfile.mkstemp(
                    prefix="deck_", suffix=".raw", dir=temp_dir
                )
                os.close(fd)
                self._buf = np.memmap(
                    temp_path, dtype=np.int16, mode="w+", shape=(pre_frames, 2)
                )
                self._streamed = True
                self._temp_path = temp_path
            except Exception as e:
                # W6: disk-full / mmap-failure fallback -- play correctly
                # from RAM rather than crash. Heavy for a 60-min track, but
                # correct.
                print(
                    f"[DeckEngine] mmap allocation failed for {filepath}, "
                    f"falling back to RAM: {e}"
                )
                use_mmap = False

        if not use_mmap:
            n_frames = max(
                self.sample_rate, int(duration * self.sample_rate) + self.sample_rate
            )
            self._buf = np.zeros((n_frames, 2), dtype=np.int16)
            self._streamed = False
            self._temp_path = None

        self._frontier = 0
        self.decode_complete = False
        self._decode_stop.clear()
        self._decode_thread = threading.Thread(
            target=self._decode_worker, args=(filepath,), daemon=True
        )
        self._decode_thread.start()

    def stop_decode(self):
        """Stop and join the decode worker -- no writer is left touching
        `_buf` after this returns. Does NOT touch `_buf` itself (see
        `detach_buffer`); split from teardown so the caller can defer the
        buffer close until the audio callback can no longer reach it (W3,
        B13). Idempotent."""
        self._decode_stop.set()
        proc = self._decode_proc
        if proc is not None:
            try:
                proc.kill()
            except Exception:
                pass
        thread = self._decode_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=1.0)
        self._decode_proc = None
        self._decode_thread = None
        self.active = False
        self.filepath = None
        self.track_id = None

    def detach_buffer(self):
        """Hand off the current buffer for a DEFERRED close and install a
        fresh empty one. Returns (mmap, temp_path) for a streamed deck --
        the caller (DeckEngine._retire) must not close/delete it until the
        audio callback can no longer reach this deck as `_active`/`_idle`
        (W3, B13 -- closing/removing a live mmap under a reading callback is
        an unmapped-memory segfault, not a catchable exception). Returns
        (None, None) for a RAM deck: a still-referenced np.ndarray is never
        freed under a reader, so there is nothing to defer."""
        mm = None
        path = None
        if self._streamed and isinstance(self._buf, np.memmap):
            mm = self._buf
            path = self._temp_path
        self._buf = np.zeros((0, 2), dtype=np.int16)
        self._frontier = 0
        self._streamed = False
        self._temp_path = None
        return mm, path

    def _decode_worker(self, filepath: str):
        # `-nostdin` pairs with subproc's stdin=DEVNULL: in a windowed frozen
        # build ffmpeg would otherwise inherit an invalid stdin handle, block
        # polling it, and emit zero PCM -- silent playback with a frozen
        # position marker (dp-216 Phase 6 live-pass root cause).
        cmd = [
            _FFMPEG_BIN, "-nostdin", "-v", "error", "-i", filepath,
            "-ar", str(self.sample_rate), "-ac", "2",
            "-f", "s16le", "-",
        ]
        proc = None
        # stderr to a real temp file, not PIPE: nothing reads it until after
        # the stdout loop finishes, and a full PIPE buffer would deadlock the
        # decode. DEVNULL was the old behavior and threw the diagnosis away.
        err = tempfile.TemporaryFile()
        try:
            proc = subprocess.Popen(
                cmd, **popen_kwargs(stdout=subprocess.PIPE, stderr=err)
            )
            self._decode_proc = proc
            leftover = b""
            while not self._decode_stop.is_set():
                chunk = proc.stdout.read(_DECODE_CHUNK_FRAMES * 4)
                if not chunk:
                    break
                data = leftover + chunk
                usable = len(data) - (len(data) % 4)  # R12: buffer split stereo frames
                leftover = data[usable:]
                if usable == 0:
                    continue
                frames = np.frombuffer(data[:usable], dtype=np.int16).reshape(-1, 2)
                self._append(frames)
            # Clean EOF (not a teardown via _decode_stop) means the whole
            # track is now resident: the true end of audio is the frontier,
            # which `advance`/`fill_into` use for a duration-independent
            # natural end. Decide this from the READ LOOP's outcome, before
            # reaping -- a slow/failed reap must not cost us a correct
            # decode_complete, which is what used to leave a fully decoded
            # deck with no effective end frame.
            reached_eof = not self._decode_stop.is_set()
            self._reap(proc)
            if reached_eof:
                self.decode_complete = True
                if self._frontier == 0:
                    self._report_decode_failure(filepath, err)
        except Exception as e:
            print(f"[DeckEngine] Decode error for {filepath}: {e}")
            self._reap(proc)
            # Fail FAST, do not hang: with decode_complete set,
            # `_effective_end_frame` resolves (to 0 on a total failure) so the
            # deck ends and the orchestrator moves on, instead of sitting
            # active-but-silent forever with no end frame.
            self.decode_complete = True
        finally:
            self._decode_proc = None
            try:
                err.close()
            except Exception:
                pass

    @staticmethod
    def _reap(proc):
        """Close ffmpeg's stdout and make certain the process is gone. The
        old code let a `wait()` timeout propagate as an exception, which both
        skipped `decode_complete` and left an ORPHANED ffmpeg running -- one
        per load, accumulating for the life of the app."""
        if proc is None:
            return
        try:
            proc.stdout.close()
        except Exception:
            pass
        try:
            proc.wait(timeout=2.0)
            return
        except Exception:
            pass
        try:
            proc.kill()
            proc.wait(timeout=1.0)
        except Exception:
            pass

    @staticmethod
    def _report_decode_failure(filepath, err):
        """Surface ffmpeg's own stderr when a decode produced no audio at all.
        Silent zero-length decodes are the hardest failure to diagnose in a
        frozen build, where this log is the only diagnostic channel."""
        detail = ""
        try:
            err.seek(0)
            detail = err.read().decode("utf-8", "replace").strip()
        except Exception:
            pass
        print(
            f"[DeckEngine] Decode produced no audio for {filepath}"
            + (f": {detail}" if detail else " (no ffmpeg output)")
        )

    def _append(self, frames: np.ndarray):
        end = self._frontier + len(frames)
        if end > self._buf.shape[0]:
            if self._streamed:
                # B4: an mmap is pre-sized and fixed -- remapping under a
                # live reader is fragile, so never grow it. Write only the
                # partial slice that still fits, mark the deck done, and
                # warn. With ffprobe (W1) + the W2 margin this should be
                # rare; it exists as a correctness net, not the normal path.
                capacity = self._buf.shape[0]
                remaining = capacity - self._frontier
                if remaining > 0:
                    self._buf[self._frontier:capacity] = frames[:remaining]
                self._frontier = capacity
                self.decode_complete = True
                self._decode_stop.set()
                proc = self._decode_proc
                if proc is not None:
                    try:
                        proc.kill()
                    except Exception:
                        pass
                print(
                    f"[DeckEngine] mmap overflow for {self.filepath}: track "
                    "exceeded its pre-sized buffer, truncating"
                )
                return
            grown = np.zeros((end + self.sample_rate, 2), dtype=np.int16)
            grown[: self._buf.shape[0]] = self._buf
            self._buf = grown
        self._buf[self._frontier:end] = frames
        self._frontier = end  # publish last: readers may see stale-but-safe data

    def frontier_seconds(self) -> float:
        return self._frontier / float(self.sample_rate)

    def wait_until_decoded(self, target_frame: int, timeout: float = 5.0):
        """Block the CALLING thread (never the audio callback, R2) until the
        decode frontier reaches target_frame or the whole track is decoded.
        Used for the cold-manual-select pre-buffer (D3) and for a seek ahead
        of the frontier."""
        deadline = time.time() + timeout
        cap = self._buf.shape[0]
        while self._frontier < min(target_frame, cap) and time.time() < deadline:
            if self._decode_thread is None or not self._decode_thread.is_alive():
                break
            time.sleep(0.01)

    # -- callback-side reads (no locks, no allocation beyond the block) ----
    #
    # SNAPSHOT DISCIPLINE (read this before editing read_block/advance/
    # fill_into). These run on the realtime audio thread with no lock, while
    # a control thread can concurrently swap `_buf` out from under them --
    # `Deck.load()` and `detach_buffer()` (via DeckEngine._retire) both
    # replace `_buf` with a DIFFERENT array, and `detach_buffer` installs an
    # EMPTY (0, 2) one. Every method here must therefore bind `_buf` and
    # `_frontier` to LOCALS ONCE and use only those locals for the rest of
    # the call. Reading `self._buf` twice (e.g. once to size the block, again
    # to slice it) can straddle that swap: the size comes from the old large
    # buffer and the slice lands on the new empty one, so the assignment
    # raises a shape-mismatch ValueError ON THE AUDIO THREAD, which kills the
    # PortAudio stream outright. A stale-but-whole snapshot is always safe --
    # the retired array stays alive as long as the local holds a reference,
    # and mmaps are close-deferred by DeckEngine._drain_pending_close (W3/B13)
    # precisely so a snapshot like this can never point at unmapped memory.

    def read_block(self, frames: int) -> np.ndarray:
        buf = self._buf              # snapshot -- see SNAPSHOT DISCIPLINE
        frontier = self._frontier
        start = self.read_idx
        avail = max(0, min(frontier, buf.shape[0]) - start)
        take = max(0, min(frames, avail))
        out = np.zeros((frames, 2), dtype=np.int16)
        if take > 0:
            out[:take] = buf[start:start + take]
        return out

    def fade_envelope(self, frames: int) -> np.ndarray:
        """Per-sample gain multiplier for the upcoming block. When no fade is
        active it holds flat at `_fade_level` (the gain the last fade settled
        into); otherwise it's a linear ramp so the fade is click-free within
        the block, not just between blocks. On completion it latches
        `_fade_level = _fade_to` so a finished fade-out stays silent instead
        of snapping back to unity for the ~100 ms until the deck stops."""
        if self._fade_start is None:
            return np.full(frames, self._fade_level, dtype=np.float32)
        positions = self.read_idx + np.arange(frames)
        t = (positions - self._fade_start) / float(self._fade_len)
        t = np.clip(t, 0.0, 1.0)
        env = self._fade_from + (self._fade_to - self._fade_from) * t
        if positions[-1] >= self._fade_start + self._fade_len:
            self._fade_level = self._fade_to
            self._fade_start = None
            if self._fade_stop_after:
                self.pending_stop = True
        return env.astype(np.float32)

    def start_fade(self, from_gain: float, to_gain: float, duration_ms: int, stop_after: bool):
        # PUBLICATION ORDER MATTERS. `_fade_start is None` is the flag
        # `fade_envelope` (audio thread) tests to decide whether a fade is in
        # progress, and it dereferences `_fade_len`/`_fade_from`/`_fade_to`
        # immediately after. This method runs on a CONTROL thread (play(),
        # fade_out()) with no lock against the callback, so `_fade_start` must
        # be published LAST: every field it gates has to already hold a usable
        # value by the time the callback observes the flag flip. Assigning it
        # before `_fade_len` left a window where a callback read `_fade_len is
        # None` (its initial value) and raised TypeError in
        # `float(self._fade_len)` ON THE AUDIO THREAD, which kills the
        # PortAudio stream outright.
        self._fade_from = from_gain
        self._fade_to = to_gain
        self._fade_len = max(1, int(duration_ms / 1000.0 * self.sample_rate))
        self._fade_stop_after = stop_after
        self._fade_start = self.read_idx

    def is_buffering(self) -> bool:
        """D7: True when the reader has caught up to (or is ahead of) the
        decode frontier on a deck that isn't fully decoded yet -- the only
        case is a far-forward seek on a streamed deck before decode catches
        up (with mmap there is no reseek/backward buffering; decode is
        already racing linearly, the flag just reflects the wait)."""
        return self.active and self.read_idx >= self._frontier and not self.decode_complete

    def _effective_end_frame(self, frontier=None):
        """The frame at which this deck's playback should stop, or None if
        not yet determinable. Priority: the dp-199 "Fin" end marker if set;
        otherwise the decode frontier once decode is complete -- the
        sample-accurate true end (beats mutagen's reported `duration`, which
        can over/under-report on VBR sources -- see `advance`'s history).
        Shared by `advance` and `fill_into` so end-of-track logic lives in
        exactly one place.

        dp-221 -- KNOWN LIMITATION, deliberately not fixed. Returning None
        until `decode_complete` means an over-cap (mmap-streamed, 60-min+)
        track whose decode is still racing has no known end frame, so the
        crossfade trigger in `DeckEngine._callback`
        (`read_idx >= end - _crossfade_len`) can never fire for it: the
        transition silently degrades to the plain gapless path at the true
        end. A Fin end marker on that track restores the crossfade, since the
        marker short-circuits this method. Fixing it properly would mean
        trusting ffprobe's duration as a provisional end frame, which is
        exactly what W1/`_effective_end_frame` avoids on purpose -- a VBR
        source that over-reports would cut the track early, a worse failure
        than losing a crossfade.

        `frontier` lets a caller that already snapshotted `_frontier` pass it
        in, so the end frame is derived from the SAME snapshot that sized the
        block rather than a second, possibly-newer read (see SNAPSHOT
        DISCIPLINE above). Defaults to reading `self._frontier` for callers
        that don't hold a snapshot."""
        if self.end_marker is not None:
            return int(self.end_marker * self.sample_rate)
        if self.decode_complete:
            return self._frontier if frontier is None else frontier
        return None

    def advance(self, requested: int) -> int:
        """Advance read_idx, gated by the decode frontier (D3): playback
        stalls (silence) rather than racing ahead of decode. Applies loop
        wrap or end-of-track detection using pure index math afterward."""
        frontier = self._frontier    # snapshot -- see SNAPSHOT DISCIPLINE
        available = max(0, frontier - self.read_idx)
        step = min(requested, available)
        self.read_idx += step

        if self.loop_active and self.loop_b is not None:
            loop_b_idx = int(self.loop_b * self.sample_rate)
            if self.read_idx >= loop_b_idx:
                self.read_idx = int((self.loop_a or 0.0) * self.sample_rate)
            return step

        end_frame = self._effective_end_frame(frontier)
        if end_frame is not None and self.read_idx >= end_frame:
            self.just_ended = True
            self.active = False
        return step

    def fill_into(self, out_slice, n: int) -> int:
        """Write up to `n` frames of deck-local gain/fade-applied float32
        audio into `out_slice[:take]` -- track_volume * gain * fade envelope
        ONLY, NOT master volume (the engine post-multiplies master + clips
        once over the whole mixed block so both decks share it, see
        `DeckEngine._callback`). Does not zero `out_slice[take:]` -- that is
        the caller's responsibility (silence tail on underrun, or a
        stitched-in idle deck on a gapless swap).

        `take` is capped by BOTH the decode frontier (D3 silence net) and
        `_effective_end_frame`, so a mid-track frontier underrun
        (`take < n`, `just_ended` stays False) can never be confused with a
        true end (`just_ended` set) by the caller -- Phase 2a correctness
        rule #2. Loop wrap is block-granular (same precision as `advance`,
        not sample-accurate) -- a looping deck never reaches
        `_effective_end_frame` so it never triggers a gapless stitch. This
        applies to the gapless path only: `DeckEngine._callback`'s crossfade
        trigger is a separate comparison outside this method and needs its
        own loop guard (dp-259) -- it does not inherit this one.
        """
        buf = self._buf              # snapshot -- see SNAPSHOT DISCIPLINE
        frontier = self._frontier
        idx = self.read_idx
        frontier_avail = max(0, min(frontier, buf.shape[0]) - idx)
        end = self._effective_end_frame(frontier)
        if end is not None:
            take = min(n, frontier_avail, max(0, end - idx))
        else:
            take = min(n, frontier_avail)
        take = max(0, take)

        if take > 0:
            block = buf[idx:idx + take].astype(np.float32) * (1.0 / 32768.0)
            env = self.fade_envelope(take)
            gain = self.gain * self.track_volume
            out_slice[:take] = block * (env[:, None] * gain)

        self.read_idx += take

        if self.loop_active and self.loop_b is not None:
            loop_b_idx = int(self.loop_b * self.sample_rate)
            if self.read_idx >= loop_b_idx:
                self.read_idx = int((self.loop_a or 0.0) * self.sample_rate)
            return take

        if end is not None and self.read_idx >= end:
            self.just_ended = True
            self.active = False
        return take


class DeckEngine:
    """Public API mirrors core.engine.AudioEngine so ui/main_window.py and the
    ArtNet dispatch table need minimal change (see dp-216 "Public API to
    preserve"). Phase 2a runs two decks -- `_active` (sounding) and `_idle`
    (pre-loaded next track, or empty) -- and ping-pongs them for gapless
    auto-advance. The engine is playlist-agnostic: it never imports
    `core.playlist`; the orchestrator (Phase 5) decides *what* plays next and
    calls `preload`/`swap_to_preloaded`/`invalidate_preload` accordingly."""

    def __init__(self):
        # Device selection and stream construction come FIRST: the chosen
        # device's native rate is `self.sample_rate`, and every Deck decodes
        # to it. Constructing an OutputStream does not invoke the callback --
        # only `.start()` does, at the end of __init__ -- so it is safe to
        # build the stream before the decks it reads.
        self._stream = None
        self._open_output_stream()

        self._lock = threading.Lock()
        # dp-220: a SEPARATE lock covering only the slow deck-teardown /
        # decode-allocation work (`_retire`, `_read_duration`, `Deck.load`).
        # It serializes background workers against each other so two of them
        # can never tear down and rebuild the same deck concurrently -- but it
        # is NEVER taken by the control path (play/pause/seek/stop/volume/cue/
        # end-marker/invalidate_preload), all of which run on the Qt main
        # thread. Holding `self._lock` across a 1s decode-thread join and a
        # ~150MB buffer allocation is what still froze the UI under rapid-Next
        # stress after dp-218 moved that work off the main thread but left it
        # under the engine-wide lock.
        #
        # LOCK ORDER, if both are ever held: `_retire_lock` OUTERMOST, then
        # `self._lock`. Never acquire `_retire_lock` while holding
        # `self._lock`.
        self._retire_lock = threading.Lock()
        self._state = STATE_STOPPED
        self._master_volume = settings.get("master_volume", 80) / 100.0
        self._fade_in_ms = settings.get("fade_in_ms", 0)
        self._fade_out_ms = settings.get("fade_out_ms", 0)

        self.on_track_end = None
        self.on_position = None
        self.on_track_changed = None  # fired (new filepath) on either swap route
        self.on_buffering = None  # D7: fired (bool) on active-deck buffering change
        self._last_buffering = False
        self.on_crossfade_progress = None  # dp-219: fired (running, t, len_frames)
        self._last_crossfade_running = False

        self._active = Deck(self.sample_rate)
        self._idle = Deck(self.sample_rate)
        self._idle_armed = False  # sole authority for "idle deck holds a ready decode"
        # dp-254: split off "is auto-advance permitted" from `_idle_armed`.
        # `_idle_armed` alone used to serve BOTH "idle deck is ready to play"
        # and "auto-advance at natural end is permitted" -- which meant a
        # stop/loop end-action could never be preloaded without also making
        # it auto-advance. Defaults True so every existing preload() caller
        # keeps today's gapless/crossfade behavior; only `_rearm_preload`
        # (ui/main_window.py) opts out for `stop`/`loop`, via
        # `set_auto_advance(False)`. Manual `swap_to_preloaded` intentionally
        # stays keyed on `_idle_armed` ONLY -- see the "swap" branch in
        # `_drain_commands` -- so a manual Next after a stop/loop halt is
        # still zero-latency.
        self._auto_advance_armed = True

        # Phase 3 (dp-216): live dual-deck crossfade gain ramp. Dormant until
        # arm_crossfade() is called (Phase 5 wires that into the
        # orchestrator) -- see the module docstring's Phase 3 section.
        self._crossfade_len = 0
        self._crossfade_overlap = None
        self._crossfade_running = False
        self._crossfade_elapsed = 0
        # A12/R2: pre-allocated mix scratch for the crossfade branch's
        # second-deck blend, so no per-block allocation happens on the
        # audio thread.
        self._mix_scratch = np.zeros((_BLOCK_SIZE, 2), dtype=np.float32)

        # Phase 2b (D6): engine owns the temp dir for over-cap decks' mmap
        # backing files, and the deferred-close list (W3/B13) -- an mmap
        # retired from _active/_idle is closed+deleted only after at least
        # one full poll tick has elapsed (>> one ~21ms callback block), so
        # the audio callback can never be mid-read of it.
        self._temp_dir = tempfile.mkdtemp(prefix="sava_deck_")
        self._pending_close = []  # list of (mmap, temp_path, retire_tick)
        self._pending_close_lock = threading.Lock()
        self._poll_tick = 0

        # Command queue (R2/correctness rule #1): control-thread methods push
        # small tuples here instead of mutating read_idx / deck refs
        # directly. The audio callback is the only consumer.
        self._command_queue = collections.deque()
        # `_swap_pending` + `_pending_active_fp` are the POLL THREAD's
        # housekeeping signal (unload the spent deck, fire on_track_changed);
        # written by BOTH swap routes in the callback, consumed only by the
        # poll thread. `_swap_ack` is the SEPARATE result channel for a manual
        # swap_to_preloaded() (None=pending, True=accepted, False=rejected),
        # written only by the drain's "swap" branch and read only by
        # swap_to_preloaded. Keeping them separate stops a manual swap and the
        # poll thread from clobbering each other's flag.
        self._swap_pending = False
        self._swap_ack = None
        self._pending_active_fp = None

        # Monotonic preload generation (R10): every preload / invalidate /
        # load bumps it. A background _arm_when_ready thread captures the
        # generation at spawn and refuses to arm if it has since changed, so a
        # still-decoding idle deck can never be armed after the preload that
        # started it was superseded or invalidated (a lone invalidate with no
        # follow-up preload would otherwise let the arm-thread re-arm a stale
        # deck, and the next natural end would stitch a removed track).
        self._preload_gen = 0

        if self._stream is not None:
            self._stream.start()

        self._poll_running = True
        self._poll_thread = threading.Thread(target=self._poll_position, daemon=True)
        self._poll_thread.start()

    def _open_output_stream(self):
        """Construct (not start) the output stream on the best device that
        actually opens, and set `self.sample_rate` to that device's rate.

        Tries `_output_device_candidates()` in order. A candidate can fail for
        reasons that are only discoverable by opening it -- an exclusive-mode
        holder, a stale endpoint, a device that reports a rate it will not
        accept -- so "does it open" is the real test, not "does it exist"
        (Phase 6: SAVA has to come up on arbitrary venue hardware). If every
        candidate fails the engine still constructs, with `_stream = None`:
        the rest of the API stays usable and the failure is logged rather than
        killing the app at import time.

        dp-223: honours the persisted `output_device_name` setting, tried
        first; `FOLLOW_SYSTEM_DEFAULT` (or a device that no longer exists)
        keeps the original host-API-preference chain.
        """
        preferred = settings.get(_DEVICE_SETTING_KEY, FOLLOW_SYSTEM_DEFAULT)
        self.output_device_name = None
        for device, rate in _output_device_candidates(preferred):
            try:
                stream = sd.OutputStream(
                    samplerate=rate,
                    device=device,
                    channels=2,
                    dtype="float32",
                    blocksize=_BLOCK_SIZE,
                    callback=self._callback,
                )
            except Exception as e:
                print(f"[DeckEngine] Output device {device} @ {rate}Hz unusable: {e}")
                continue
            self._stream = stream
            self.sample_rate = rate
            try:
                info = sd.query_devices(stream.device)
                api = sd.query_hostapis(info["hostapi"])["name"]
                self.output_device_name = info["name"]
                print(f"[DeckEngine] Output: {info['name']} [{api}] @ {rate}Hz")
            except Exception:
                print(f"[DeckEngine] Output: device {device} @ {rate}Hz")
            return
        self.sample_rate = 44100
        print("[DeckEngine] No usable audio output device -- playback disabled")

    # -- output device selection (dp-223) ------------------------------------

    def set_output_device(self, name):
        """Switch the output device live. `name` is a device name from
        `list_output_devices()`, or `FOLLOW_SYSTEM_DEFAULT`.

        Playback STOPS -- by design (dp-223, confirmed with the user). Every
        deck decodes at the OLD stream's sample rate, and PortAudio does not
        resample, so decoded buffers are worthless the moment the rate
        changes. Rather than guess whether the new device happens to share a
        rate, the decks are unconditionally retired and the user presses play
        again; `load()` will re-decode at the new rate.

        Returns the device name actually in use afterwards, which may differ
        from `name` if the requested device would not open.
        """
        with self._lock:
            self._preload_gen += 1  # fence any in-flight preload/arm thread
            self._idle_armed = False
            self._state = STATE_STOPPED
            self._active.active = False
            self._idle.active = False
            self._crossfade_running = False
            self._crossfade_len = 0
            self._crossfade_overlap = None
            self._crossfade_elapsed = 0

        settings.set(_DEVICE_SETTING_KEY, name)

        # Stop and close the OLD stream before touching any deck buffer: once
        # this returns the callback can never fire again, so the retires below
        # cannot race a reader (same ordering `shutdown` relies on, W3/B13).
        old = self._stream
        self._stream = None
        if old is not None:
            try:
                old.stop()
                old.close()
            except Exception as e:
                print(f"[DeckEngine] Error closing previous output stream: {e}")

        for deck in (self._active, self._idle):
            self._retire(deck)

        self._open_output_stream()
        # Decks were constructed against the old rate; rebuild them at the new
        # one so a later load()/preload() decodes to a matching rate.
        self._active = Deck(self.sample_rate)
        self._idle = Deck(self.sample_rate)
        if self._stream is not None:
            self._stream.start()
        return self.output_device_name

    # -- deferred mmap teardown (W3, B13 -- read the module docstring's
    # Phase 2b notes before touching this) ----------------------------------

    def _retire(self, deck: "Deck"):
        """Stop a deck's decode and hand off its buffer for a deferred
        close (W3). Safe to call on a deck that could still be reachable as
        `self._active`/`self._idle` a moment ago -- it never closes an mmap
        synchronously, only queues it. Call this everywhere 2a called
        `deck.unload()` on a deck that the live callback could still read or
        that a later reuse could clobber."""
        deck.stop_decode()
        mm, path = deck.detach_buffer()
        if mm is not None:
            with self._pending_close_lock:
                self._pending_close.append((mm, path, self._poll_tick))

    def _drain_pending_close(self, force: bool = False):
        """Close+delete retired mmaps that are old enough to be certain no
        audio callback still holds a reference. Require >= 2 elapsed poll
        ticks, NOT 1: `_retire` captures the current tick and the poll thread
        increments-then-drains in the same iteration, so a `>= 1` test could
        close a buffer retired just before a poll wake almost immediately
        (~0ms). `>= 2` guarantees a FULL poll interval (~100ms, far more than
        one ~21ms callback block) elapses regardless of where the retirement
        landed in the poll cycle -- provable safety, not just probable (W3,
        B13). `force=True` (shutdown only, after the stream is stopped/closed)
        drains everything regardless of age. Never called from the callback."""
        with self._pending_close_lock:
            if not self._pending_close:
                return
            ready = []
            remaining = []
            for entry in self._pending_close:
                _, _, retire_tick = entry
                if force or (self._poll_tick - retire_tick) >= 2:
                    ready.append(entry)
                else:
                    remaining.append(entry)
            self._pending_close = remaining
        for mm, path, _ in ready:
            self._close_mmap(mm, path)

    @staticmethod
    def _close_mmap(mm, path):
        """Close the np.memmap's underlying mmap and delete its backing
        file. NO flush() first -- the data is being discarded, and flushing
        a large streamed track's dirty pages would just stall teardown
        (B11). Windows (B5/B6) will not let a file be removed while its
        memmap is open -- close + drop the reference BEFORE removing, with a
        short retry loop to absorb the handle being released lazily."""
        try:
            underlying = getattr(mm, "_mmap", None)
            if underlying is not None:
                underlying.close()
        except Exception:
            pass
        del mm
        if not path:
            return
        for _attempt in range(5):
            try:
                os.remove(path)
                return
            except FileNotFoundError:
                return
            except Exception:
                time.sleep(0.05)
        print(f"[DeckEngine] Failed to remove temp file {path} after retries")

    # -- realtime callback (R2: no locks, no allocation beyond the block,
    # no decode, no Python-level callback invocation) ----------------------

    def _drain_commands(self):
        """Applies queued seek/swap/invalidate commands. Called ONLY at the
        top of `_callback` -- the sole mutator of `read_idx` and the
        `_active`/`_idle` references (correctness rule #1)."""
        while self._command_queue:
            cmd = self._command_queue.popleft()
            kind = cmd[0]
            if kind == "seek":
                self._active.read_idx = cmd[1]
                self._active.just_ended = False
            elif kind == "swap":
                fp = cmd[1]
                if self._idle_armed and self._idle.filepath == fp:
                    self._idle.active = True
                    self._pending_active_fp = self._idle.filepath
                    self._active, self._idle = self._idle, self._active
                    self._idle_armed = False
                    self._swap_pending = True   # poll-thread housekeeping
                    self._swap_ack = True       # manual swap_to_preloaded result
                else:
                    self._swap_ack = False      # rejected; no poll housekeeping
            elif kind == "invalidate":
                self._idle_armed = False
            elif kind == "cancel_crossfade":
                self._crossfade_running = False
                self._crossfade_len = 0
                self._crossfade_overlap = None
                self._crossfade_elapsed = 0
                self._active.gain = 1.0
                self._idle.gain = 1.0
                # The idle deck was marked active by the trigger; a cancel
                # stops it sounding. Inert given the _idle_armed gate on every
                # idle read, but leaving a non-playing deck flagged active is
                # a state smell -- clear it so idle state stays coherent.
                self._idle.active = False
            elif kind == "set_auto_advance":
                # dp-254: control-thread-set flag, drained here per rule #1
                # (only _drain_commands mutates callback-visible state).
                self._auto_advance_armed = cmd[1]
            elif kind == "set_crossfade":
                overlap = cmd[1]
                if overlap is not None and overlap.duration > 0:
                    self._crossfade_overlap = overlap
                    self._crossfade_len = max(1, int(overlap.duration * self.sample_rate))
                else:
                    self._crossfade_overlap = None
                    self._crossfade_len = 0
            elif kind == "seek_crossfade_gain":
                # A9-style guard, re-checked here (not just in
                # seek_crossfade_gain()): _crossfade_running can flip between
                # the command being queued and drained.
                if self._crossfade_running:
                    frames = cmd[1]
                    self._crossfade_elapsed = max(0, min(frames, self._crossfade_len))

    def _callback(self, outdata, frames, time_info, status):
        self._drain_commands()
        active = self._active
        if not active.active:
            outdata.fill(0)
            return

        # Phase 3 (dp-216) crossfade trigger check: armed (overlap > 0),
        # not yet running, AND the idle deck ready (_idle_armed) for the
        # correct next track. A4: when _crossfade_len <= 0 this whole branch
        # is skipped and the Phase-2 gapless path below runs unmodified.
        #
        # The _idle_armed gate mirrors the gapless path's authority: without
        # it, a crossfade armed while the idle deck is still decoding (or was
        # invalidated) would ramp into an empty/stale deck -> silence or the
        # wrong track during the fade. By the time playback is within
        # _crossfade_len of the end, the idle deck has had the whole active
        # track to decode, so this never spuriously blocks a real transition;
        # if the idle deck genuinely isn't ready, the crossfade correctly
        # does not fire and playback falls through to the gapless path at the
        # true end (itself _idle_armed-gated).
        # dp-254: `_auto_advance_armed` belt-and-braces gate -- a stop/loop
        # track's idle deck is preloaded but must never start a crossfade
        # ramp into it, even if `_crossfade_len` is stale from a prior track.
        # dp-259: an active A-B loop (loop_active AND loop_b set -- matching
        # the exact guard Deck.advance()/fill_into() use before their own end
        # check) must suppress the trigger too. Without this, a loop whose B
        # point sits inside the last _crossfade_len frames satisfies the end
        # comparison on every pass and starts a ramp mid-loop.
        if (
            self._crossfade_len > 0
            and not self._crossfade_running
            and self._idle_armed
            and self._auto_advance_armed
            and not (active.loop_active and active.loop_b is not None)
        ):
            end = active._effective_end_frame()
            if end is not None and active.read_idx >= end - self._crossfade_len:
                self._crossfade_running = True
                self._crossfade_elapsed = 0
                self._idle.active = True
                self._idle_armed = False  # A5: idle is now being consumed by the ramp

        # SNAPSHOT DISCIPLINE (same rule as Deck.read_block/fill_into, applied
        # to the crossfade scalars). `_crossfade_overlap` and `_crossfade_len`
        # are read here and dereferenced several lines below, while a CONTROL
        # thread can null them out in between: `set_output_device` clears
        # `_crossfade_running`, `_crossfade_overlap` and `_crossfade_len`
        # together under `self._lock`, which the callback never takes. Reading
        # `self._crossfade_overlap` twice could therefore see True for the
        # running flag and None for the overlap one line later, raising
        # AttributeError ON THE AUDIO THREAD and killing the PortAudio stream.
        # Binding both to locals once makes the block internally consistent;
        # the `overlap is not None and length > 0` test then degrades a
        # concurrently-cancelled crossfade to the plain gapless path below
        # instead of crashing.
        overlap = self._crossfade_overlap
        length = self._crossfade_len
        if self._crossfade_running and overlap is not None and length > 0:
            outdata.fill(0)
            t = min(1.0, self._crossfade_elapsed / length)
            active.gain = overlap.evaluate_out(t)
            self._idle.gain = overlap.evaluate_in(t)
            active.fill_into(outdata, frames)
            scratch = self._mix_scratch[:frames]
            scratch.fill(0)
            self._idle.fill_into(scratch, frames)
            outdata += scratch
            self._crossfade_elapsed += frames
            if self._crossfade_elapsed >= length or active.just_ended:
                # A6: finalize -- swap refs, reset both gains, hand off to the
                # existing poll-thread housekeeping (same as a gapless swap).
                #
                # dp-221: on the TRUNCATED path (`active.just_ended` fires
                # before the ramp completed -- the outgoing track hit its real
                # end mid-fade) the incoming deck is still at a partial gain,
                # and setting gain = 1.0 below steps it straight to full
                # volume in one block: an audible click, not a fade. Hand the
                # remaining distance to the deck's own per-sample fade
                # envelope so it ramps across the next few ms instead.
                # start_fade only assigns plain fields, so it is safe on the
                # audio thread (R2).
                incoming_gain = self._idle.gain
                if incoming_gain < 1.0:
                    self._idle.start_fade(
                        incoming_gain, 1.0, _TRUNCATED_FADE_MS, stop_after=False
                    )
                active.gain = 1.0
                self._idle.gain = 1.0
                self._pending_active_fp = self._idle.filepath
                self._active, self._idle = self._idle, self._active
                self._crossfade_running = False
                self._crossfade_len = 0
                self._crossfade_overlap = None
                self._swap_pending = True
            outdata *= self._master_volume
            np.clip(outdata, -1.0, 1.0, out=outdata)
            return

        # ---- unchanged Phase-2 gapless path below (A4) ----
        produced = active.fill_into(outdata, frames)
        if active.just_ended and self._idle_armed and self._auto_advance_armed:
            # TRUE end (never a mid-track underrun, rule #2) + idle armed
            # (rule #3) + auto-advance permitted (dp-254: `_idle_armed` alone
            # no longer implies auto-advance is wanted -- a stop/loop track
            # can be preloaded without auto-stitching at natural end) ->
            # gapless stitch: finish the boundary block from the incoming
            # deck, then flip refs. No fade-in (gapless).
            self._idle.active = True
            self._idle.fill_into(outdata[produced:], frames - produced)
            self._pending_active_fp = self._idle.filepath
            self._active, self._idle = self._idle, self._active
            self._idle_armed = False
            self._swap_pending = True
        elif produced < frames:
            outdata[produced:].fill(0)  # underrun OR unarmed real end -> silence tail
        outdata *= self._master_volume  # master spans both decks, applied once
        np.clip(outdata, -1.0, 1.0, out=outdata)

    # -- crossfade (Phase 3, dp-216) -----------------------------------------

    def set_auto_advance(self, armed: bool):
        """dp-254: control-thread setter for `_auto_advance_armed`, split off
        `_idle_armed` so a `stop`/`loop` end-action can preload its successor
        (zero-latency manual Next) without also permitting the natural-end
        auto-stitch. Routed through `_command_queue` like every other
        callback-visible mutation (rule #1) -- never assigned directly here."""
        self._command_queue.append(("set_auto_advance", bool(armed)))

    def arm_crossfade(self, overlap):
        """Arm the upcoming active->idle transition to blend via `overlap`
        (duck-typed: .duration, .evaluate_in(t), .evaluate_out(t)) instead of
        a hard gapless swap. None / duration<=0 re-arms plain gapless.
        Single-shot: consumed when the crossfade triggers, or superseded by a
        later arm_crossfade()/load() call. Wired into main_window's
        _rearm_preload() (Phase 5).

        No-ops while a crossfade is running (same A9-style guard as
        preload()/invalidate_preload()): the callback reads
        `_crossfade_len`/`_crossfade_overlap` every block to compute the
        in-flight ramp's progress, so overwriting them mid-ramp (e.g. a
        playlist reorder firing _rearm_preload during an active crossfade)
        would corrupt that math instead of just affecting the *next*
        transition."""
        if self._crossfade_running:
            return
        self._command_queue.append(("set_crossfade", overlap))

    def seek_crossfade_gain(self, frames: int):
        """dp-219: manually retime the live crossfade's gain schedule by
        rewriting only `_crossfade_elapsed` (the frame counter driving
        `Overlap.evaluate_in`/`evaluate_out`) -- never either deck's
        `read_idx`. Each deck's own audio content keeps advancing at normal
        real-time speed regardless of where this moves the gain schedule
        (see dp-219's chosen scrub model: gain-schedule warp only, no deck
        seeking). Queued through the same command-queue pattern as
        `set_crossfade`/`cancel_crossfade`, consumed at the top of the
        audio callback (correctness rule #1: only `_drain_commands` mutates
        crossfade state). No-op if no crossfade is running -- enforced again
        at drain time (A9-style guard) since `_crossfade_running` can flip
        between this call and the command's consumption; a stray command
        outside an active crossfade must not arm or corrupt state. Clamped
        to `[0, _crossfade_len]` at drain time -- reaching the upper bound
        is already the callback's existing finalize condition (deck swap,
        gains reset to 1.0), so no new finalize logic is needed here."""
        if not self._crossfade_running:
            return
        self._command_queue.append(("seek_crossfade_gain", frames))

    @property
    def crossfade_progress(self):
        """dp-219: read-only `(running, t, crossfade_len_frames)` snapshot
        for the poll thread to report via `on_crossfade_progress`. `t` is
        `_crossfade_elapsed` normalized to `[0.0, 1.0]` (0.0 when no
        crossfade is running/armed, i.e. `crossfade_len_frames == 0`).

        Also read from the Qt thread on every scrub drag (main_window's
        `_on_crossfade_gain_seek_requested`), which is why it deliberately
        does NOT take `self._lock`: all three fields are plain ints/bools
        mutated only by the audio callback, which never takes the lock, so
        the lock would buy no atomicity whatsoever -- while exposing a
        per-drag-event UI call to blocking behind a `_preload_worker` that
        holds the lock across a decode teardown."""
        length = self._crossfade_len
        elapsed = self._crossfade_elapsed
        t = (elapsed / length) if length > 0 else 0.0
        return self._crossfade_running, t, length

    # -- load / transport ---------------------------------------------------

    def load(self, filepath: str, track_id: str = None):
        """dp-220: the IDLE deck's retire is pushed off-thread; the ACTIVE
        deck's stays synchronous. `load()` runs on the Qt main thread (via
        `_load_and_play`, which is also `_advance_to`'s fallback when a swap
        is rejected -- squarely on the rapid-Next path), and each `_retire`
        joins a decode thread for up to 1s. The idle deck is simply being
        discarded here, so retiring it asynchronously behind the generation
        bump is safe. The active deck's is NOT made async: `self._active.load`
        two lines below re-enters `stop_decode()` on that same deck, and
        letting a background `_retire` tear it down concurrently would race
        two teardowns on one deck. Halving the worst case is the safe fix;
        removing the remaining join needs `Deck.load`'s own allocation moved
        off-thread, which is a larger change than this ticket."""
        doomed_idle = None
        with self._lock:
            # A new "current" invalidates any armed next (the old current's
            # prediction is no longer verifiable). The orchestrator re-preloads
            # after. Bumping the generation also bails any in-flight arm-thread.
            self._preload_gen += 1
            self._idle_armed = False
            self._command_queue.append(("cancel_crossfade",))
            self._retire(self._active)
            doomed_idle = self._idle
            self._stop_internal()
            duration = self._read_duration(filepath)
            self._active.load(filepath, duration, self._temp_dir)
            self._active.track_id = track_id
            vol_map, vol_key = _row_or_file_key(track_id, filepath, "track_volumes")
            tv = settings.get(vol_map, {}).get(vol_key, 100)
            self._active.track_volume = tv / 100.0
            cue_map, cue_key = _row_or_file_key(track_id, filepath, "cue_points")
            raw_cues = settings.get(cue_map, {}).get(cue_key, [])
            self._active.cue_points = {i: v for i, v in enumerate(raw_cues)}
            end_map, end_key = _row_or_file_key(track_id, filepath, "track_end_markers")
            self._active.end_marker = settings.get(end_map, {}).get(end_key, None)
            start_map, start_key = _row_or_file_key(track_id, filepath, "track_start_markers")
            start = settings.get(start_map, {}).get(start_key, None)
            self._active.start_marker = start
            if start is not None:
                self._active.read_idx = max(
                    0, min(int(start * self.sample_rate), self._active._buf.shape[0])
                )
        self._retire_async(doomed_idle)  # dp-220: outside the lock, off-thread

    def _read_duration(self, filepath: str) -> float:
        """Authoritative duration (W1): ffprobe first (accurate, drives the
        mmap cap decision + pre-size), mutagen as a fallback, 0.0 if both
        fail (deck.load then falls to the RAM path -- see B7)."""
        dur = _ffprobe_duration(filepath)
        if dur is not None:
            return dur
        try:
            mf = MutagenFile(filepath)
            if mf is not None and mf.info and hasattr(mf.info, "length"):
                length = float(mf.info.length)
                if length > 0:
                    return length
        except Exception as e:
            print(f"[DeckEngine] Duration read error: {e}")
        return 0.0

    def prefetch(self, filepath: str):
        # dp-178 OS-cache warm was a pygame.mixer.music.load()-latency
        # mitigation; the streaming decode-ahead worker (D3) makes it moot.
        pass

    def play(self, from_position: float = None):
        with self._lock:
            deck = self._active
            if deck.filepath is None:
                return
            if from_position is not None:
                deck.read_idx = max(0, min(int(from_position * self.sample_rate), deck._buf.shape[0]))
            target = deck.read_idx + int(_PREBUFFER_SECONDS * self.sample_rate)
        # Pre-buffer OUTSIDE the lock: wait_until_decoded only reads the
        # decode frontier, and blocking here under self._lock would stall the
        # poll thread and every concurrent control call for the wait's
        # duration (R2/R8 -- keep control-path locks short). This still runs
        # on the CALLER's thread, which is the Qt main thread, so the timeout
        # is the UI's worst-case freeze -- keep it short (Phase 6).
        deck.wait_until_decoded(target, timeout=_PREBUFFER_TIMEOUT)
        with self._lock:
            if deck.read_idx == 0 and self._fade_in_ms > 0:
                deck.start_fade(0.0, 1.0, self._fade_in_ms, stop_after=False)
            deck.active = True
            self._state = STATE_PLAYING

    def pause(self):
        with self._lock:
            if self._state == STATE_PLAYING:
                self._active.active = False
                self._state = STATE_PAUSED

    def resume(self):
        with self._lock:
            if self._state == STATE_PAUSED:
                self._active.active = True
                self._state = STATE_PLAYING

    def stop(self):
        with self._lock:
            self._stop_internal()

    def _stop_internal(self):
        """Caller must hold self._lock. Cancels any running/armed crossfade
        as well (Phase 5): a STOPPED engine must never carry live crossfade
        state. Leaving `_crossfade_running` set past a stop is not
        self-healing -- the A9 guards make preload()/arm_crossfade()/
        invalidate_preload() ALL no-op while it is True, so nothing
        downstream can clear it, and the next play() would re-enter the
        crossfade branch against stale decks. Doing it in this shared
        primitive rather than at each call site covers every stop path (Stop
        button, Clear playlist, ArtNet stop, fade-out-to-stop).

        `_crossfade_running` is ALSO cleared synchronously here, not only via
        the queued command: the command is drained by the audio callback one
        block later (~21ms), and a caller like main_window's
        _on_clear_playlist runs stop() -> playlist.clear() -> _rearm_preload()
        synchronously INSIDE that window, so the A9 guards would still see a
        stale True and skip retiring the idle deck. Clearing the flag after
        `active.active = False` (below) is safe and cannot re-trigger: the
        callback's crossfade trigger requires an ACTIVE deck, which no longer
        holds. The other crossfade scalars (`_crossfade_overlap`,
        `_crossfade_len`) are deliberately NOT touched here -- a callback
        already inside the crossfade branch dereferences
        `_crossfade_overlap.evaluate_out(t)` every block, so nulling it from
        a control thread would raise on the realtime thread. The queued
        command clears those safely, on the audio thread."""
        self._active.active = False
        self._active.read_idx = 0
        self._active._fade_start = None
        self._state = STATE_STOPPED
        self._command_queue.append(("cancel_crossfade",))
        self._crossfade_running = False

    def seek(self, position_sec: float):
        with self._lock:
            active = self._active
            if active.filepath is None:
                return
            position_sec = max(0.0, min(position_sec, active.duration))
            target = int(position_sec * self.sample_rate)
        # Blocking wait OUTSIDE the lock (same reasoning as play()); the
        # actual read_idx write is deferred to the command queue (rule #1)
        # so it can never race the callback's own advance of read_idx.
        #
        # The wait is HARD-BOUNDED at _SEEK_WAIT_TIMEOUT rather than using
        # wait_until_decoded's 5s default. seek() runs on the CALLER's thread,
        # which for every real caller is the Qt main thread -- the waveform
        # scrub emits one seek per mouse-move event, cue jumps route through
        # here, and an ArtNet seek fader emits one per DMX frame. Seeking
        # ahead of the decode frontier on a long track therefore froze the
        # whole window for up to five seconds PER EVENT. Giving up early is
        # safe because the frontier gate in `Deck.fill_into` already handles
        # a read_idx ahead of decode correctly: it outputs silence (never a
        # spurious end-of-track, correctness rule #2) until decode catches
        # up, which is a brief stall in the audio rather than a frozen UI.
        active.wait_until_decoded(target + _BLOCK_SIZE, timeout=_SEEK_WAIT_TIMEOUT)
        # A8: cancel any running/armed crossfade before the seek lands --
        # moving _active.read_idx mid-ramp would strand the fade against a
        # stale end-distance. Cue jumps route through seek(), so covered too.
        self._command_queue.append(("cancel_crossfade",))
        self._command_queue.append(("seek", target))

    def seek_percent(self, percent: float):
        self.seek(percent * self._active.duration)

    # -- gapless rotation (Phase 2a) -----------------------------------------

    def preload(self, filepath: str, track_id: str = None):
        """Background-decode `filepath` into the idle deck for a gapless
        auto-advance. Idempotent ONLY when the idle deck is already armed
        and holds this exact filepath (correctness rule #4); every other
        case -- including a SPENT idle deck whose filepath happens to equal
        `filepath` (the A/B/A/B short-playlist case) -- tears down the
        current idle decode and rebuilds from scratch. Never applies a
        fade-in; the swap must be gapless. Reads track_volume/cues/end_marker
        from settings for that file, same as `load`.

        No-ops while a crossfade is running (mirrors invalidate_preload's
        A9 guard, Phase 5): during an active ramp `self._idle` IS the deck
        being faded in, consumed directly by the audio callback outside the
        lock. Retiring/reloading it here (as this method otherwise would)
        would pull the audio out from under an in-flight, audible
        transition. The orchestrator re-calls preload() from
        on_track_changed once the ramp finalizes and swaps decks, so this
        is a transient skip, not a lost prediction.

        Everything past the idempotency/reuse check runs on a background
        thread (`_preload_worker`), not the caller's thread: retiring the
        previous idle deck (`_retire` -> `stop_decode` -> `thread.join`,
        up to 1s if that deck's decode was still in flight) used to run
        synchronously here, and `preload()` is called from
        `_rearm_preload()` on the Qt main thread on every single track
        advance -- so that join froze the UI for up to a second on any
        transition where the deck being replaced hadn't finished decoding
        yet (bugfix: UI freeze between tracks)."""
        if self._crossfade_running:
            return
        # dp-254: every preload() call re-arms auto-advance by default, even
        # on the idempotent no-op path below -- keeps existing callers'
        # gapless/crossfade behavior unchanged. `_rearm_preload` opts back
        # out with an explicit set_auto_advance(False) call right after.
        self.set_auto_advance(True)
        if self._idle_armed and self._idle.filepath == filepath:
            return
        with self._lock:
            self._preload_gen += 1
            gen = self._preload_gen
            idle_deck = self._idle
            # An in-flight decode for this exact file is NOT the "spent idle
            # deck" of correctness rule #4 -- a spent deck has been retired
            # (filepath None) or has just_ended set, and neither has a live
            # decode worker. Rebuilding it anyway would stop_decode(), which
            # JOINS the worker (up to 1s) on the caller's thread, and restart
            # ffmpeg from zero. Playlist drag-reorder calls _rearm_preload on
            # every move, so without this a reorder mid-preload stalls the UI
            # once per move. The generation bump above still happens, and a
            # fresh _arm_when_ready thread is still spawned below, so a
            # preceding invalidate_preload() is correctly undone.
            reuse = (
                idle_deck.filepath == filepath
                and not idle_deck.just_ended
                and idle_deck._decode_thread is not None
                and idle_deck._decode_thread.is_alive()
            )
            if not reuse:
                self._idle_armed = False
            elif idle_deck.track_id != track_id:
                # dp-237: reusing an in-flight decode for the same filepath,
                # but a different row asked for it (e.g. two duplicate rows
                # of the same file adjacent in the playlist). The settings
                # read already happened under the old track_id; retarget the
                # deck's identity so any future marker/cue write lands on the
                # right row instead of the wrong one.
                idle_deck.track_id = track_id
        if reuse:
            threading.Thread(
                target=self._arm_when_ready, args=(idle_deck, filepath, gen), daemon=True
            ).start()
            return
        threading.Thread(
            target=self._preload_worker, args=(idle_deck, filepath, gen, track_id), daemon=True
        ).start()

    def _preload_worker(self, idle_deck: "Deck", filepath: str, gen: int, track_id: str = None):
        """Off-main-thread body of preload()'s non-reuse path: retire the
        previous idle decode, ffprobe the new file, load it, then hand off
        to `_arm_when_ready` -- same generation-fencing pattern (`gen`) that
        method already uses, so a newer preload/invalidate racing in while
        this runs is correctly abandoned rather than undone.

        dp-220: the slow half runs under `_retire_lock`, NOT `self._lock`.
        dp-218 moved this body off the Qt main thread but left it holding the
        engine-wide lock across `_retire` (which joins a decode thread, up to
        1s) and `Deck.load` (which allocates and zeroes a buffer -- ~150MB for
        a 15-minute track). The Qt thread takes that same lock in play/seek/
        pause/stop/volume/cue/end-marker/invalidate_preload, so the join was
        off the main thread but the BLOCKING was not. `_retire_lock` serializes
        this teardown/allocation work against other background workers only;
        the generation fence (`gen`, R10) is what actually provides the
        correctness the lock was standing in for, and it is re-checked at
        every stage below. `self._lock` is now taken only for the short field
        writes."""
        with self._retire_lock:
            if self._preload_gen != gen:
                return  # superseded before we even started
            self._retire(idle_deck)  # W3: defer-close any prior mmap (R9)
            duration = self._read_duration(filepath)
            if self._preload_gen != gen:
                return  # superseded while ffprobe ran
            idle_deck.load(filepath, duration, self._temp_dir)
        with self._lock:
            if self._preload_gen != gen:
                return  # superseded while loading
            idle_deck.track_id = track_id
            vol_map, vol_key = _row_or_file_key(track_id, filepath, "track_volumes")
            tv = settings.get(vol_map, {}).get(vol_key, 100)
            idle_deck.track_volume = tv / 100.0
            cue_map, cue_key = _row_or_file_key(track_id, filepath, "cue_points")
            raw_cues = settings.get(cue_map, {}).get(cue_key, [])
            idle_deck.cue_points = {i: v for i, v in enumerate(raw_cues)}
            end_map, end_key = _row_or_file_key(track_id, filepath, "track_end_markers")
            idle_deck.end_marker = settings.get(end_map, {}).get(end_key, None)
            start_map, start_key = _row_or_file_key(track_id, filepath, "track_start_markers")
            start = settings.get(start_map, {}).get(start_key, None)
            idle_deck.start_marker = start
            if start is not None:
                idle_deck.read_idx = max(
                    0, min(int(start * self.sample_rate), idle_deck._buf.shape[0])
                )
        self._arm_when_ready(idle_deck, filepath, gen)

    def _arm_when_ready(self, idle_deck: "Deck", filepath: str, gen: int,
                        timeout: float = _PRELOAD_ARM_TIMEOUT):
        """Runs on a background thread (never the audio callback). Waits for
        `idle_deck` to be armable and, if nothing superseded this preload in
        the meantime, arms it. `gen` is the preload generation captured at
        spawn (R10): if `_preload_gen` has moved on (a newer preload, an
        invalidate, or a load), this arm is stale and must NOT arm --
        otherwise a lone invalidate could be undone here.

        W4: armable means a healthy PREBUFFER, not only full
        decode_complete. An over-cap idle deck can take minutes to fully
        decode; waiting for that would miss a short active track's end and
        lose the gapless swap (D6's short->long Must, B10). `_frontier > 0`
        guards against a FAILED decode (ffmpeg errored -> decode_complete
        True, frontier 0) arming an empty deck that would instant-end on
        swap."""
        prebuffer_frames = int(_ARM_PREBUFFER_SECONDS * self.sample_rate)

        def _armable():
            return idle_deck._frontier > 0 and (
                idle_deck.decode_complete or idle_deck._frontier >= prebuffer_frames
            )

        deadline = time.time() + timeout
        while not _armable() and time.time() < deadline:
            if (self._preload_gen != gen
                    or self._idle is not idle_deck
                    or idle_deck.filepath != filepath):
                return  # superseded by a newer preload/invalidate/load/swap
            time.sleep(0.01)
        with self._lock:
            if (self._preload_gen == gen
                    and self._idle is idle_deck
                    and idle_deck.filepath == filepath
                    and _armable()):
                self._idle_armed = True

    def swap_to_preloaded(self, filepath: str, timeout: float = _SWAP_COMMAND_TIMEOUT) -> bool:
        """Manual "Next" mid-track: enqueue a swap command and wait briefly
        (bounded) for the callback to report the outcome. Never mutates
        decks on the control thread (rule #1). Returns False (caller falls
        back to a normal `load()`) if the idle deck isn't armed for exactly
        this filepath. Uses the dedicated `_swap_ack` channel -- never touches
        `_swap_pending`, so a racing natural auto-advance's poll-thread signal
        is not clobbered."""
        self._swap_ack = None
        self._command_queue.append(("swap", filepath))
        deadline = time.time() + timeout
        while time.time() < deadline:
            ack = self._swap_ack
            if ack is not None:
                return ack
            time.sleep(0.001)
        return False

    def invalidate_preload(self):
        """Unarm AND retire the idle deck (e.g. on playlist mutation, R10).
        Safe anytime -- the idle deck is never read by the audio callback
        except during a swap (which this call preempts via the generation
        bump), so retiring it here synchronously is safe, not just deferred
        housekeeping for later. Without this, a LONE invalidate (playlist
        cleared / last track, no follow-up preload) would leave an over-cap
        idle deck's background decode running and its ~690MB temp file
        resident until the next preload or shutdown happened to reuse/close
        it -- a real resource leak on an otherwise-idle engine, not just an
        R10 staleness risk. `_retire` itself is what defers the actual mmap
        CLOSE (W3/B13); calling it here just starts that clock immediately
        instead of leaving it pending indefinitely. Bumping the generation
        (R10) guarantees any in-flight arm-thread bails instead of re-arming
        the now-stale idle deck.

        A9: no-ops while a crossfade is running -- once a crossfade holds
        `_idle.active = True` for seconds, the old safety claim ("idle never
        read by the callback except during a swap") no longer holds, and
        retiring the idle deck out from under an in-flight, audible,
        committed transition would be wrong.

        dp-220: the UNARM half is synchronous (it is all the caller actually
        needs, and it is three field writes); the RETIRE half is pushed to a
        background thread via `_retire_async`. `_retire` joins the idle deck's
        decode thread for up to 1s, and this method is called from
        `MainWindow._rearm_preload()` on the Qt main thread on three separate
        branches (no current file, an end-action of loop/stop, end of
        playlist) -- so under a rapid-Next burst it froze the UI even though
        dp-218 had already moved `preload()`'s equivalent work off-thread.
        The deck being retired is fenced by the generation bump below, so no
        later preload can adopt it mid-teardown."""
        if self._crossfade_running:
            return
        with self._lock:
            self._preload_gen += 1
            self._idle_armed = False
            doomed = self._idle
        self._retire_async(doomed)
        self._command_queue.append(("invalidate",))

    def _retire_async(self, deck: "Deck"):
        """dp-220: run `_retire(deck)` on a background thread under
        `_retire_lock`, so no caller ever blocks on its decode-thread join.

        Used by every retire site that previously ran on a latency-sensitive
        thread: `invalidate_preload` (Qt main thread) and `_poll_position`'s
        post-swap cleanup (the 10 Hz thread that drives `on_position`, where a
        1s join visibly froze the playhead right after each track change).
        `_retire` itself only STOPS the decode and QUEUES the mmap close --
        the actual close stays deferred through `_drain_pending_close`'s
        two-tick rule (W3/B13), which this does not weaken: a later close is
        still strictly safer than an earlier one.

        GENERATION-FENCED, and it must be. Deferring the retire opens a race
        the synchronous version did not have: if a new `preload()` lands
        before this thread gets scheduled, `_preload_worker` takes
        `_retire_lock` first, retires the deck AND reloads it -- and then this
        thread would wake up and retire a freshly-loaded, still-wanted deck,
        silently destroying the preload. Capturing `_preload_gen` at spawn and
        re-checking it under the lock closes that: if anything superseded us,
        whoever did so has already taken ownership of this deck object and
        performed its own teardown (`_preload_worker` calls `_retire`
        explicitly, and `Deck.load` self-cleans besides), so skipping here
        leaks nothing."""
        threading.Thread(
            target=self._retire_locked,
            args=(deck, self._preload_gen),
            daemon=True,
        ).start()

    def _retire_locked(self, deck: "Deck", gen: int):
        with self._retire_lock:
            if self._preload_gen != gen:
                return  # superseded -- the new owner already tore this down
            self._retire(deck)

    @property
    def preloaded_file(self):
        """The idle deck's filepath if -- and only if -- it is armed for a
        gapless auto-advance (armed-aware, mirrors dp-192's `is_ready`)."""
        return self._idle.filepath if self._idle_armed else None

    @property
    def preloaded_track_id(self):
        """dp-253: track_id counterpart to `preloaded_file` -- lets a caller
        tell two duplicate rows of the same file apart when deciding whether
        the queued row is the one they mean (same reasoning as
        `current_track_id` vs `current_file`)."""
        return self._idle.track_id if self._idle_armed else None

    # -- volume ---------------------------------------------------------

    def set_master_volume(self, volume: int):
        # dp-245 D1: deliberately NOT persisted immediately. This is driven
        # by an ArtNet fader at up to one call per DMX frame (~40/s) as well
        # as the transport slider's live drag signal -- a settings.save()
        # here would fsync the whole settings file dozens of times a second
        # on the Qt main thread. It still survives a clean exit via
        # MainWindow.closeEvent -> settings.save(). Do not "fix" this
        # inconsistency by adding a save call.
        with self._lock:
            self._master_volume = max(0, min(volume, 100)) / 100.0
            settings.set("master_volume", volume)

    def set_track_volume(self, volume: int, filepath: str = None, track_id: str = None):
        """Set (and persist) the per-track volume for `filepath`/`track_id`,
        defaulting to the active deck's track.

        The live gain is applied ONLY to decks actually holding that
        row/file. This used to persist under `fp` but unconditionally assign
        `self._active.track_volume`, so adjusting a NON-playing track's
        volume instantly changed the volume of whatever was currently
        playing. The correct value was still written to settings, so the
        symptom was purely audible and never showed as a wrong number on
        screen. The idle deck is updated too -- it may already hold the
        preloaded next track, and must not swap in at a stale gain.

        dp-237: when `track_id` is given, both the live-deck match and the
        settings write are row-keyed (a specific duplicate row's volume),
        not file-keyed (every row of that file). Passing neither defaults to
        the active deck's own identity, whatever that is."""
        with self._lock:
            if filepath is None and track_id is None:
                filepath = self._active.filepath
                track_id = self._active.track_id
            if filepath is None:
                return
            gain = max(0, min(volume, 100)) / 100.0
            for deck in (self._active, self._idle):
                if track_id is not None:
                    if deck.track_id == track_id:
                        deck.track_volume = gain
                elif deck.filepath == filepath:
                    deck.track_volume = gain
            vol_map, vol_key = _row_or_file_key(track_id, filepath, "track_volumes")
            track_vols = settings.get(vol_map, {})
            track_vols[vol_key] = volume
            settings.set(vol_map, track_vols)

    # -- fades ------------------------------------------------------------

    def fade_out(self, duration_ms: int = None):
        ms = duration_ms if duration_ms is not None else self._fade_out_ms
        with self._lock:
            if ms > 0:
                self._active.start_fade(1.0, 0.0, ms, stop_after=True)
            else:
                self._stop_internal()

    def set_fade_in_ms(self, ms: int):
        self._fade_in_ms = ms
        settings.set("fade_in_ms", ms)

    def set_fade_out_ms(self, ms: int):
        self._fade_out_ms = ms
        settings.set("fade_out_ms", ms)

    # -- loop / cue / end marker --------------------------------------------

    def set_loop_a(self, pos: float = None):
        with self._lock:
            self._active.loop_a = pos if pos is not None else self.position

    def set_loop_b(self, pos: float = None):
        with self._lock:
            self._active.loop_b = pos if pos is not None else self.position

    def toggle_loop_ab(self):
        with self._lock:
            deck = self._active
            if deck.loop_a is not None and deck.loop_b is not None:
                deck.loop_active = not deck.loop_active

    def clear_loop(self):
        with self._lock:
            self._active.loop_a = None
            self._active.loop_b = None
            self._active.loop_active = False

    def set_cue(self, index: int, position: float = None):
        with self._lock:
            pos = position if position is not None else self.position
            self._active.cue_points[index] = pos
            fp = self._active.filepath
            if fp:
                cue_map, cue_key = _row_or_file_key(
                    self._active.track_id, fp, "cue_points"
                )
                cue_dict = settings.get(cue_map, {})
                slots = cue_dict.get(cue_key, [None] * 8)
                while len(slots) <= index:
                    slots.append(None)
                slots[index] = pos
                cue_dict[cue_key] = slots
                settings.set(cue_map, cue_dict)
        # dp-245 D1: explicit user-authoring action -- persist immediately so
        # a crash/taskkill/power-cut doesn't lose it (previously survived
        # only via MainWindow.closeEvent). Rare relative to master volume
        # (a user press, not a fader stream), so the fsync cost is fine here.
        settings.save()

    def jump_to_cue(self, index: int):
        pos = self._active.cue_points.get(index)
        if pos is not None:
            self.seek(pos)

    def set_end_marker(self, position: float = None):
        with self._lock:
            deck = self._active
            pos = position if position is not None else self.position
            if not (0 < pos < deck.duration):
                return
            # dp-232: the marker pair may never be inverted. Setting the end
            # at or before an existing start marker is silently ignored --
            # the same rule as set_start_marker applied from the other
            # direction, so `start < end` holds however the user gets there.
            if deck.start_marker is not None and pos <= deck.start_marker:
                return
            deck.end_marker = pos
            fp = deck.filepath
            if fp:
                m, key = _row_or_file_key(deck.track_id, fp, "track_end_markers")
                d = settings.get(m, {})
                d[key] = pos
                settings.set(m, d)
        # dp-245 D1: persist immediately -- explicit user-authoring action,
        # not a per-frame/per-drag stream (see set_master_volume).
        settings.save()

    def clear_end_marker(self):
        with self._lock:
            deck = self._active
            deck.end_marker = None
            fp = deck.filepath
            if fp:
                m, key = _row_or_file_key(deck.track_id, fp, "track_end_markers")
                d = settings.get(m, {})
                d.pop(key, None)
                settings.set(m, d)
        settings.save()

    def set_start_marker(self, position: float = None):
        with self._lock:
            deck = self._active
            pos = position if position is not None else self.position
            if not (0 <= pos < deck.duration):
                return
            # dp-232: a start marker at or after the end (Fin) marker would
            # mean a track that starts after it is supposed to stop. Silently
            # ignore it -- the user's stated behaviour is "nothing happens",
            # not an error dialog. Mirrored in set_end_marker.
            if deck.end_marker is not None and pos >= deck.end_marker:
                return
            deck.start_marker = pos
            fp = deck.filepath
            if fp:
                m, key = _row_or_file_key(deck.track_id, fp, "track_start_markers")
                d = settings.get(m, {})
                d[key] = pos
                settings.set(m, d)
        settings.save()

    def clear_start_marker(self):
        with self._lock:
            deck = self._active
            deck.start_marker = None
            fp = deck.filepath
            if fp:
                m, key = _row_or_file_key(deck.track_id, fp, "track_start_markers")
                d = settings.get(m, {})
                d.pop(key, None)
                settings.set(m, d)
        settings.save()

    def clear_all_cues(self):
        """Clear every cue point on the active deck (dp-216 Phase 4 parity for
        main_window's _on_cue_clear_all, which used to write engine._cue_points
        directly -- a no-op against the deck model; Phase 5 wired this in).
        Settings deletion stays in the caller (main_window), same as today."""
        with self._lock:
            self._active.cue_points = {}

    # -- properties ---------------------------------------------------------

    @property
    def state(self):
        return self._state

    @property
    def position(self):
        return self._active.read_idx / float(self.sample_rate)

    @property
    def duration(self):
        return self._active.duration

    @property
    def current_file(self):
        return self._active.filepath

    @property
    def current_track_id(self):
        """dp-237: the playlist row id the active deck is loaded for, or
        None if it was loaded without one (direct engine.load() call)."""
        return self._active.track_id

    @property
    def master_volume(self):
        return int(self._master_volume * 100)

    @property
    def track_volume(self):
        return int(self._active.track_volume * 100)

    @property
    def loop_points(self):
        return self._active.loop_a, self._active.loop_b, self._active.loop_active

    @property
    def cue_points(self):
        return dict(self._active.cue_points)

    @property
    def end_marker(self):
        return self._active.end_marker

    @property
    def start_marker(self):
        return self._active.start_marker

    # -- position/end-of-track poller (R8: never emit on_position or
    # invoke on_track_end/on_track_changed from the realtime audio
    # callback) -------------------------------------------------------------

    def _poll_position(self):
        while self._poll_running:
            time.sleep(_POLL_INTERVAL)
            self._poll_tick += 1
            self._drain_pending_close()  # W3: retire mmaps aged >= 1 tick
            with self._lock:
                active = self._active
                ended = active.just_ended
                stopped_by_fade = active.pending_stop
                swapped = self._swap_pending
                new_fp = self._pending_active_fp
                if swapped:
                    self._swap_pending = False
                    self._pending_active_fp = None
                if ended and not swapped:
                    active.just_ended = False
                    active.read_idx = 0  # match old engine: position -> 0 on end
                    self._state = STATE_STOPPED
                if swapped and self._state != STATE_PLAYING:
                    # dp-261: a manual Next (swap) after a stop/loop natural
                    # end lands the successor on the active deck and it plays,
                    # but _state was left STOPPED by that natural end (above,
                    # or a prior tick). Restore PLAYING so on_position resumes
                    # (it is gated on state != STOPPED below) and pause() works
                    # again (it no-ops unless state == PLAYING). Before dp-254
                    # a stop track wasn't preloaded, so Next fell through to
                    # load()+play() which set PLAYING; dp-254's preload made
                    # Next take the swap path instead, which never did.
                    self._state = STATE_PLAYING
                if stopped_by_fade:
                    active.pending_stop = False
                    self._stop_internal()
                pos_cb = self.on_position
                cur_pos = self.position
                state = self._state
                buffering_now = active.is_buffering()
                buffering_changed = buffering_now != self._last_buffering
                if buffering_changed:
                    self._last_buffering = buffering_now
                cf_running = self._crossfade_running
                cf_len = self._crossfade_len
                cf_elapsed = self._crossfade_elapsed

            if swapped:
                # The spent deck now sits in the idle slot -- retire it off
                # the audio thread (R9, W3) so a later preload() rebuilds it
                # from scratch, then notify (R5): NOT on_track_end, this was
                # a handoff, not a stop.
                # dp-220: retire ASYNCHRONOUSLY. This runs on the poll thread,
                # which is what drives on_position at 10 Hz -- a synchronous
                # _retire here joins the spent deck's decode thread for up to
                # 1s, visibly freezing the playhead right after every track
                # change.
                self._retire_async(self._idle)
                cb = self.on_track_changed
                if cb:
                    try:
                        cb(new_fp)
                    except Exception:
                        pass
            elif ended:
                cb = self.on_track_end
                if cb:
                    try:
                        cb()
                    except Exception:
                        pass
            if buffering_changed:
                # D7: fired only from the poll thread, only on change --
                # never from the realtime callback (R8).
                cb = self.on_buffering
                if cb:
                    try:
                        cb(buffering_now)
                    except Exception:
                        pass
            if pos_cb and state != STATE_STOPPED:
                try:
                    pos_cb(cur_pos)
                except Exception:
                    pass
            # dp-219: fire while a crossfade is live, PLUS exactly one final
            # emit on the running->stopped edge so the slider can return to
            # its inert state. Deliberately NOT an unconditional 10 Hz emit:
            # every other engine->UI callback here is change-gated
            # (`buffering_changed`) or state-gated (`state != STOPPED`), and
            # an always-on emit would push a Qt signal and a slider repaint
            # ten times a second for the entire life of the app, whether or
            # not a crossfade had ever been configured.
            cf_cb = self.on_crossfade_progress
            if cf_cb and (cf_running or self._last_crossfade_running):
                cf_t = (cf_elapsed / cf_len) if cf_len > 0 else 0.0
                try:
                    cf_cb(cf_running, cf_t, cf_len)
                except Exception:
                    pass
            self._last_crossfade_running = cf_running

    def shutdown(self):
        self._poll_running = False
        self._poll_thread.join(timeout=1.0)
        # W3: stop+close the stream BEFORE closing any buffer -- after this
        # the callback can never fire again, so it is finally safe to close
        # decks' buffers directly instead of deferring (B13). 2a's order
        # (unload decks, then stop the stream) segfaults with a live mmap
        # deck.
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
        for deck in (self._active, self._idle):
            self._retire(deck)
        self._drain_pending_close(force=True)
        if self._temp_dir:
            shutil.rmtree(self._temp_dir, ignore_errors=True)

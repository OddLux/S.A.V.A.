"""dp-216 Phase 1: headless tests for the DeckEngine callback/index math.

These exercise Deck's pure index/mixing logic directly -- no ffmpeg decode,
no real sounddevice stream -- so they run in CI/headless environments with
no output device (R3). DeckEngine._callback is invoked against a minimal
duck-typed stand-in (it only touches self._deck / self._master_volume) so
we get real callback-path coverage without opening a live OutputStream.

Plain unittest, no pytest dependency:
    ./venv/Scripts/python.exe -m unittest discover tests
"""

import collections
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import core.deck_engine as deck_engine_module
import core.subproc as subproc_module
from core.deck_engine import (
    Deck,
    DeckEngine,
    _ARM_PREBUFFER_SECONDS,
    _BLOCK_SIZE,
    _RESIDENT_CAP_SECONDS,
)

_ROOT = Path(__file__).resolve().parent.parent
_FFMPEG_BIN = str(_ROOT / "assets" / "ffmpeg.exe")


def _cleanup_deck_buffer(deck):
    """Test-only helper: detach + immediately close a deck's buffer (bypasses
    the deferred-close queue -- fine at teardown, once no reader can touch it)."""
    mm, path = deck.detach_buffer()
    DeckEngine._close_mmap(mm, path)


class _FakeEngine:
    """Duck-typed stand-in for DeckEngine, exposing only what _callback /
    _drain_commands read, so the callback and command-queue logic can be
    tested without opening a real audio stream (R3). `_drain_commands` is
    the REAL bound DeckEngine method -- not reimplemented here -- so these
    tests exercise the actual command-drain logic."""

    _drain_commands = DeckEngine._drain_commands

    def __init__(self, active, idle=None, master_volume=1.0):
        self._active = active
        self._idle = idle if idle is not None else Deck(active.sample_rate)
        self._idle_armed = False
        self._auto_advance_armed = True  # dp-254: default matches DeckEngine.__init__
        self._command_queue = collections.deque()
        self._swap_pending = False
        self._swap_ack = None
        self._pending_active_fp = None
        self._master_volume = master_volume
        self.sample_rate = active.sample_rate
        # Phase 3 (dp-216) crossfade state, mirroring DeckEngine.__init__.
        self._crossfade_len = 0
        self._crossfade_overlap = None
        self._crossfade_running = False
        self._crossfade_elapsed = 0
        self._mix_scratch = np.zeros((_BLOCK_SIZE, 2), dtype=np.float32)


def _make_tone_file(directory, seconds=0.5, freq=440, name="tone.wav"):
    """Render a short real audio file via ffmpeg's lavfi sine source -- used
    by tests that need actual decoded PCM (nonzero frontier), not just a
    fast ffmpeg failure on a missing path."""
    path = os.path.join(directory, name)
    subprocess.run(
        [
            _FFMPEG_BIN, "-y", "-v", "error", "-f", "lavfi",
            "-i", f"sine=frequency={freq}:duration={seconds}",
            path,
        ],
        check=True, timeout=20,
    )
    return path


def _wait_for(predicate, timeout=3.0, interval=0.01):
    """Poll `predicate` (a zero-arg callable) until it returns truthy or the
    timeout elapses. Returns the last value seen, or False on timeout."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(interval)
    return False


# Every fresh tempdir _make_bare_engine() mints (when the caller doesn't
# supply one) is tracked here and swept in tearDownModule() -- most callers
# are pre-2b tests that never touch _temp_dir at all (RAM path only) and
# have no reason to clean it up themselves.
_BARE_ENGINE_TEMP_DIRS = []


def _make_bare_engine(sample_rate=48000, temp_dir=None):
    """A DeckEngine that never opens a real sounddevice stream or spawns the
    poll thread (R3: no live audio device needed in headless tests) --
    `__init__` is bypassed via `__new__`, attributes set manually to exactly
    what the Phase 2a/2b control-path methods (preload/swap_to_preloaded/
    invalidate_preload/_drain_commands/_retire/_drain_pending_close)
    actually touch. `temp_dir` defaults to a fresh tempdir tracked for
    module-teardown cleanup (mirrors DeckEngine.__init__'s own dir); pass an
    explicit `temp_dir` when the test manages its own lifecycle."""
    engine = DeckEngine.__new__(DeckEngine)
    engine.sample_rate = sample_rate
    engine._lock = threading.Lock()
    engine._retire_lock = threading.Lock()  # dp-220: separate teardown lock
    engine._state = "stopped"
    engine._master_volume = 1.0
    engine._fade_in_ms = 0
    engine._fade_out_ms = 0
    engine.on_track_end = None
    engine.on_position = None
    engine.on_track_changed = None
    engine.on_buffering = None
    engine._last_buffering = False
    engine.on_crossfade_progress = None
    engine._last_crossfade_running = False
    engine._active = _make_deck(sample_rate)
    engine._idle = Deck(sample_rate)
    engine._idle_armed = False
    engine._auto_advance_armed = True  # dp-254: default matches DeckEngine.__init__
    engine._command_queue = collections.deque()
    engine._swap_pending = False
    engine._swap_ack = None
    engine._pending_active_fp = None
    engine._preload_gen = 0
    engine._crossfade_len = 0
    engine._crossfade_overlap = None
    engine._crossfade_running = False
    engine._crossfade_elapsed = 0
    engine._mix_scratch = np.zeros((_BLOCK_SIZE, 2), dtype=np.float32)
    if temp_dir is None:
        temp_dir = tempfile.mkdtemp(prefix="sava_deck_test_")
        _BARE_ENGINE_TEMP_DIRS.append(temp_dir)
    engine._temp_dir = temp_dir
    engine._pending_close = []
    engine._pending_close_lock = threading.Lock()
    engine._poll_tick = 0
    return engine


def tearDownModule():
    """unittest hook: sweep every tempdir _make_bare_engine() minted for a
    caller that didn't manage its own (most pre-2b tests never touch
    _temp_dir at all, since they stay on the RAM path)."""
    for d in _BARE_ENGINE_TEMP_DIRS:
        shutil.rmtree(d, ignore_errors=True)


def _make_deck(sample_rate=48000, seconds=1.0, amplitude=16000):
    deck = Deck(sample_rate)
    n = int(seconds * sample_rate)
    deck._buf = np.full((n, 2), amplitude, dtype=np.int16)
    deck._frontier = n
    deck.decode_complete = True  # simulate a fully-decoded resident deck
    deck.duration = seconds
    deck.filepath = "fake.wav"
    deck.active = True
    return deck


class _BufSwapOnNthRead:
    """Descriptor standing in for `Deck._buf` that swaps the buffer out from
    under the reader on the Nth attribute read, deterministically reproducing
    the control-thread-vs-audio-thread race (Deck.load()/detach_buffer()
    replacing `_buf` while the callback reads it). `reads` counts accesses so
    a test can assert the method under test only touched `_buf` ONCE."""

    def __init__(self, initial, replacement, swap_on_read):
        self.current = initial
        self.replacement = replacement
        self.swap_on_read = swap_on_read
        self.reads = 0

    def __get__(self, obj, objtype=None):
        if obj is None:
            return self
        self.reads += 1
        if self.reads == self.swap_on_read:
            self.current = self.replacement
        return self.current

    def __set__(self, obj, value):
        self.current = value


class TestBufferSwapRace(unittest.TestCase):
    """dp-216 Phase 5: the callback-side readers must snapshot `_buf` /
    `_frontier` into locals ONCE (see SNAPSHOT DISCIPLINE in deck_engine.py).
    Reading `self._buf` twice -- once to size the block, again to slice it --
    can straddle a concurrent `Deck.load()` / `detach_buffer()` that installs
    a DIFFERENT (or empty) array, so the size comes from the old buffer and
    the slice from the new one, raising a shape-mismatch ValueError on the
    REALTIME AUDIO THREAD (which kills the PortAudio stream).

    Phase 5 made this newly reachable: a manual Next during a live crossfade
    clears `_idle_armed` (A5), so `_advance_to` falls through to
    `_load_and_play` -> `engine.load()`, which retires the idle deck while the
    callback may still be blending it."""

    def _swapping_deck(self, swap_on_read):
        """A fully-decoded 1s deck whose `_buf` flips to the EMPTY (0, 2)
        array `detach_buffer()` installs, on the Nth `_buf` read."""
        deck = _make_deck(sample_rate=48000, seconds=1.0)
        full = deck._buf
        empty = np.zeros((0, 2), dtype=np.int16)
        descriptor = _BufSwapOnNthRead(full, empty, swap_on_read)
        # Descriptors only fire on the CLASS, so give this deck its own
        # throwaway subclass rather than mutating the shared Deck class.
        deck.__class__ = type("_RacingDeck", (Deck,), {"_buf": descriptor})
        return deck, descriptor

    def test_fill_into_survives_buffer_swap_mid_call(self):
        # Swap on the very first read: with a single snapshot the whole call
        # sees the empty buffer and cleanly produces 0 frames. Pre-fix, the
        # size came from the full buffer and the slice from the empty one.
        deck, descriptor = self._swapping_deck(swap_on_read=1)
        out = np.zeros((256, 2), dtype=np.float32)

        take = deck.fill_into(out, 256)  # must not raise

        self.assertEqual(take, 0)
        self.assertEqual(descriptor.reads, 1, "fill_into must read _buf exactly once")

    def test_fill_into_uses_consistent_snapshot_when_swap_follows_read(self):
        # Swap on the SECOND read: a single-snapshot implementation never
        # makes that read, so the whole block is served from the full buffer.
        deck, descriptor = self._swapping_deck(swap_on_read=2)
        out = np.zeros((256, 2), dtype=np.float32)

        take = deck.fill_into(out, 256)  # must not raise

        self.assertEqual(take, 256)
        self.assertEqual(descriptor.reads, 1, "fill_into must read _buf exactly once")
        np.testing.assert_allclose(out, 16000 / 32768.0, atol=1e-6)

    def test_read_block_survives_buffer_swap_mid_call(self):
        deck, descriptor = self._swapping_deck(swap_on_read=1)

        out = deck.read_block(256)  # must not raise

        self.assertEqual(out.shape, (256, 2))
        np.testing.assert_array_equal(out, np.zeros((256, 2), dtype=np.int16))
        self.assertEqual(descriptor.reads, 1, "read_block must read _buf exactly once")

    def test_callback_survives_idle_buffer_swap_mid_crossfade(self):
        # The Phase-5-reachable path end to end: a crossfade is blending the
        # idle deck when engine.load() retires it. The realtime callback must
        # not raise.
        active = _make_deck(sample_rate=48000, seconds=5.0)
        idle, _ = self._swapping_deck(swap_on_read=1)
        idle.active = True

        fake = _FakeEngine(active, idle=idle)
        fake._crossfade_running = True
        fake._crossfade_len = 48000
        fake._crossfade_elapsed = 0
        fake._crossfade_overlap = _LinearOverlap(1.0)

        outdata = np.zeros((256, 2), dtype=np.float32)
        DeckEngine._callback(fake, outdata, 256, None, None)  # must not raise

        self.assertTrue(np.all(np.isfinite(outdata)))


class TestCallbackMixing(unittest.TestCase):

    def test_int16_to_float32_gain_sum(self):
        deck = _make_deck(amplitude=16384)  # exactly half-scale
        deck.track_volume = 0.5
        fake = _FakeEngine(deck, master_volume=0.5)

        outdata = np.zeros((256, 2), dtype=np.float32)
        DeckEngine._callback(fake, outdata, 256, None, None)

        expected = (16384 / 32768.0) * 0.5 * 0.5  # deck.gain(1.0) * track * master
        np.testing.assert_allclose(outdata, expected, atol=1e-6)

    def test_inactive_deck_outputs_silence(self):
        deck = _make_deck()
        deck.active = False
        fake = _FakeEngine(deck)

        outdata = np.ones((128, 2), dtype=np.float32)  # pre-fill with garbage
        DeckEngine._callback(fake, outdata, 128, None, None)

        np.testing.assert_array_equal(outdata, np.zeros((128, 2), dtype=np.float32))

    def test_advance_follows_callback(self):
        deck = _make_deck()
        fake = _FakeEngine(deck)
        outdata = np.zeros((256, 2), dtype=np.float32)

        self.assertEqual(deck.read_idx, 0)
        DeckEngine._callback(fake, outdata, 256, None, None)
        self.assertEqual(deck.read_idx, 256)


class TestFrontierGuard(unittest.TestCase):

    def test_read_block_never_reads_past_frontier(self):
        deck = Deck(48000)
        deck._buf = np.full((1000, 2), 12345, dtype=np.int16)
        deck._frontier = 100  # only 100 frames actually decoded so far
        deck.read_idx = 0

        block = deck.read_block(256)  # requests more than is decoded
        self.assertTrue(np.all(block[:100] == 12345))
        self.assertTrue(np.all(block[100:] == 0))  # silence net, not garbage

    def test_advance_stalls_at_frontier_instead_of_racing_ahead(self):
        deck = Deck(48000)
        deck._buf = np.zeros((1000, 2), dtype=np.int16)
        deck._frontier = 50
        deck.duration = 1000 / 48000.0
        deck.read_idx = 0

        step = deck.advance(256)

        self.assertEqual(step, 50)
        self.assertEqual(deck.read_idx, 50)  # did not jump to 256


class TestSeekIndexMath(unittest.TestCase):

    def test_seek_sets_sample_accurate_index_both_directions(self):
        deck = _make_deck(sample_rate=48000, seconds=5.0)

        deck.read_idx = int(3.0 * 48000)
        target_back = int(1.0 * 48000)
        deck.read_idx = target_back  # backward seek
        self.assertEqual(deck.read_idx, 48000)

        target_fwd = int(4.5 * 48000)
        deck.read_idx = target_fwd  # forward seek
        self.assertEqual(deck.read_idx, 216000)


class TestLoopWrap(unittest.TestCase):

    def test_loop_wraps_at_b_back_to_a(self):
        deck = _make_deck(sample_rate=48000, seconds=10.0)
        deck.loop_a = 2.0
        deck.loop_b = 3.0
        deck.loop_active = True
        deck.read_idx = int(2.9 * 48000)

        deck.advance(48000 // 10)  # advance ~0.1s, crossing loop_b at 3.0s

        self.assertEqual(deck.read_idx, int(2.0 * 48000))

    def test_no_wrap_when_loop_inactive(self):
        deck = _make_deck(sample_rate=48000, seconds=10.0)
        deck.loop_a = 2.0
        deck.loop_b = 3.0
        deck.loop_active = False
        deck.read_idx = int(2.9 * 48000)

        for _ in range(5):
            deck.advance(1024)

        self.assertGreater(deck.read_idx, int(3.0 * 48000))


class TestEndTrigger(unittest.TestCase):

    def test_natural_end_at_duration(self):
        deck = _make_deck(sample_rate=48000, seconds=1.0)
        deck.read_idx = int(0.99 * 48000)

        deck.advance(48000)  # frontier caps this at what's decoded

        self.assertTrue(deck.just_ended)
        self.assertFalse(deck.active)

    def test_end_marker_overrides_duration(self):
        deck = _make_deck(sample_rate=48000, seconds=10.0)
        deck.end_marker = 5.0
        deck.read_idx = int(4.99 * 48000)

        for _ in range(20):
            if deck.just_ended:
                break
            deck.advance(1024)

        self.assertTrue(deck.just_ended)
        self.assertGreaterEqual(deck.read_idx, int(5.0 * 48000))
        self.assertLess(deck.read_idx, int(5.0 * 48000) + 1024)

    def test_loop_active_suppresses_end_trigger(self):
        deck = _make_deck(sample_rate=48000, seconds=1.0)
        deck.loop_a = 0.0
        deck.loop_b = 0.5
        deck.loop_active = True
        deck.read_idx = int(0.49 * 48000)

        deck.advance(48000)

        self.assertFalse(deck.just_ended)

    def test_end_uses_frontier_not_overreported_duration(self):
        # Regression: mutagen over-reports duration (e.g. VBR MP3). The real
        # audio is 1.0s (frontier), but duration claims 2.0s. Ending must
        # trigger at the frontier once decode is complete, not hang waiting
        # for read_idx to reach 2.0s (which it never can).
        deck = _make_deck(sample_rate=48000, seconds=1.0)
        deck.duration = 2.0  # over-reported; real decoded audio is 1.0s
        deck.read_idx = int(0.99 * 48000)

        for _ in range(20):
            if deck.just_ended:
                break
            deck.advance(1024)

        self.assertTrue(deck.just_ended)
        self.assertEqual(deck.read_idx, deck._frontier)

    def test_no_end_before_decode_complete_at_frontier(self):
        # A deck stalled at the frontier mid-decode (not yet complete) must
        # NOT end -- it's waiting for more audio, not at the true end.
        deck = _make_deck(sample_rate=48000, seconds=1.0)
        deck.decode_complete = False
        deck.read_idx = deck._frontier

        deck.advance(1024)

        self.assertFalse(deck.just_ended)


class TestFadeEnvelope(unittest.TestCase):

    def test_fade_in_ramps_monotonically_from_zero(self):
        deck = _make_deck(sample_rate=48000, seconds=1.0)
        deck.read_idx = 0
        deck.start_fade(0.0, 1.0, duration_ms=100, stop_after=False)

        env = deck.fade_envelope(4800)  # full 100ms fade in one block

        self.assertAlmostEqual(env[0], 0.0, places=3)
        self.assertAlmostEqual(env[-1], 1.0, places=2)
        self.assertTrue(np.all(np.diff(env) >= 0))

    def test_fade_out_sets_pending_stop_when_complete(self):
        deck = _make_deck(sample_rate=48000, seconds=1.0)
        deck.read_idx = 0
        deck.start_fade(1.0, 0.0, duration_ms=10, stop_after=True)

        deck.fade_envelope(4800)  # far past the 10ms fade window

        self.assertTrue(deck.pending_stop)

    def test_completed_fade_out_holds_silence_not_unity(self):
        # Regression: after a fade-out completes, the envelope must stay at
        # 0.0, not snap back to unity for the ~100ms before the poll thread
        # stops the deck (which would blast full volume at the end of a fade).
        deck = _make_deck(sample_rate=48000, seconds=1.0)
        deck.read_idx = 0
        deck.start_fade(1.0, 0.0, duration_ms=10, stop_after=True)

        deck.fade_envelope(4800)          # completes the fade
        deck.read_idx = 4800
        held = deck.fade_envelope(1024)   # next block, fade already done

        np.testing.assert_array_equal(held, np.zeros(1024, dtype=np.float32))

    def test_no_fade_returns_unity_gain(self):
        deck = _make_deck(sample_rate=48000, seconds=1.0)
        env = deck.fade_envelope(64)
        np.testing.assert_array_equal(env, np.ones(64, dtype=np.float32))


class TestGaplessRotation(unittest.TestCase):
    """Phase 2a: the audio-callback-side rotation logic (correctness rules
    #1-#4 in deck_engine.py's module docstring), driven through
    DeckEngine._callback / _drain_commands against `_FakeEngine`."""

    def test_gapless_boundary_block_is_continuous(self):
        tail = 50
        active = _make_deck(sample_rate=48000, seconds=1.0, amplitude=16000)
        active._frontier = tail  # active is 50 frames from its true end

        idle = _make_deck(sample_rate=48000, seconds=1.0, amplitude=8000)
        idle.filepath = "idleTrack.wav"

        fake = _FakeEngine(active, idle=idle)
        fake._idle_armed = True

        frames = 256
        outdata = np.zeros((frames, 2), dtype=np.float32)
        DeckEngine._callback(fake, outdata, frames, None, None)

        # No zero-run at the seam: active's tail, then idle's head, both
        # nonzero and numerically distinct (different source amplitudes).
        self.assertTrue(np.all(outdata[:tail] != 0))
        self.assertTrue(np.all(outdata[tail:] != 0))
        self.assertFalse(np.allclose(outdata[0], outdata[tail]))

        self.assertIs(fake._active, idle)   # refs flipped
        self.assertIs(fake._idle, active)
        self.assertFalse(fake._idle_armed)  # consumed
        self.assertTrue(fake._swap_pending)
        self.assertEqual(fake._pending_active_fp, "idleTrack.wav")

    def test_underrun_does_not_autoadvance(self):
        # Guards the #1 correctness bug: a mid-track frontier underrun
        # (decode still in progress, NOT a true end) must output silence,
        # not skip to the armed idle deck.
        active = _make_deck(sample_rate=48000, seconds=1.0)
        active.decode_complete = False
        active._frontier = 40  # short: still decoding
        active.read_idx = 0

        idle = _make_deck(sample_rate=48000, seconds=1.0, amplitude=5000)
        idle.filepath = "nextTrack.wav"

        fake = _FakeEngine(active, idle=idle)
        fake._idle_armed = True

        frames = 256
        outdata = np.ones((frames, 2), dtype=np.float32)  # pre-fill, must be zeroed
        DeckEngine._callback(fake, outdata, frames, None, None)

        self.assertFalse(active.just_ended)
        self.assertTrue(np.all(outdata[40:] == 0))  # silence tail, not idle content
        self.assertIs(fake._active, active)   # no flip
        self.assertIs(fake._idle, idle)
        self.assertTrue(fake._idle_armed)     # still armed, untouched
        self.assertFalse(fake._swap_pending)

    def test_idle_armed_manual_swap_gate_ignores_auto_advance_armed(self):
        # dp-254: manual swap_to_preloaded (the "swap" command) stays keyed
        # on _idle_armed ONLY -- _auto_advance_armed must have zero effect on
        # it, even when False (the stop/loop case). This is the whole point
        # of the split: a stop/loop track's successor is still zero-latency
        # on a manual Next.
        active = _make_deck(sample_rate=48000, seconds=5.0)
        idle = _make_deck(sample_rate=48000, seconds=5.0, amplitude=4000)
        idle.filepath = "swapTarget.wav"

        fake = _FakeEngine(active, idle=idle)
        fake._idle_armed = True
        fake._auto_advance_armed = False  # stop/loop successor: no auto-advance
        fake._command_queue.append(("swap", "swapTarget.wav"))

        frames = 64
        outdata = np.zeros((frames, 2), dtype=np.float32)
        DeckEngine._callback(fake, outdata, frames, None, None)

        self.assertIs(fake._active, idle)  # manual swap still succeeded
        self.assertTrue(fake._swap_ack)

    def test_auto_advance_disarmed_true_end_does_not_stitch(self):
        # dp-254: the idle deck is READY (_idle_armed) but auto-advance is
        # explicitly disarmed (a stop/loop successor preload). A true end
        # must NOT auto-stitch into it -- playback halts, on_track_end still
        # fires (via the poll thread, not exercised here), same as the
        # historical unarmed case.
        active = _make_deck(sample_rate=48000, seconds=1.0)
        active._frontier = 50
        active.read_idx = 0

        idle = _make_deck(sample_rate=48000, seconds=1.0, amplitude=8000)
        idle.filepath = "nextTrack.wav"

        fake = _FakeEngine(active, idle=idle)
        fake._idle_armed = True
        fake._auto_advance_armed = False

        frames = 256
        outdata = np.zeros((frames, 2), dtype=np.float32)
        DeckEngine._callback(fake, outdata, frames, None, None)

        self.assertTrue(active.just_ended)     # TRUE end fired
        self.assertIs(fake._active, active)    # no flip -- no auto-stitch
        self.assertIs(fake._idle, idle)
        self.assertTrue(fake._idle_armed)      # untouched: still ready for a manual Next
        self.assertFalse(fake._swap_pending)
        self.assertTrue(np.all(outdata[50:] == 0))  # silence tail, not idle content

    def test_unarmed_true_end_fires_track_end_not_swap(self):
        active = _make_deck(sample_rate=48000, seconds=1.0)
        active._frontier = 50
        active.read_idx = 0

        fake = _FakeEngine(active)
        fake._idle_armed = False

        frames = 256
        outdata = np.zeros((frames, 2), dtype=np.float32)
        DeckEngine._callback(fake, outdata, frames, None, None)

        self.assertTrue(active.just_ended)   # TRUE end fired
        self.assertIs(fake._active, active)  # no flip -- poll would fire on_track_end
        self.assertFalse(fake._swap_pending)
        self.assertTrue(np.all(outdata[50:] == 0))

    def test_swap_command_match_flips_and_reports_pending(self):
        active = _make_deck(sample_rate=48000, seconds=5.0)
        idle = _make_deck(sample_rate=48000, seconds=5.0, amplitude=4000)
        idle.filepath = "swapTarget.wav"

        fake = _FakeEngine(active, idle=idle)
        fake._idle_armed = True
        fake._command_queue.append(("swap", "swapTarget.wav"))

        frames = 128
        outdata = np.zeros((frames, 2), dtype=np.float32)
        DeckEngine._callback(fake, outdata, frames, None, None)

        self.assertIs(fake._active, idle)    # flipped
        self.assertIs(fake._idle, active)
        self.assertTrue(fake._swap_pending)
        self.assertIs(fake._swap_ack, True)
        self.assertFalse(fake._idle_armed)
        self.assertTrue(np.all(outdata != 0))  # whole block from the new active

    def test_swap_command_mismatch_sets_rejected(self):
        active = _make_deck(sample_rate=48000, seconds=5.0)
        idle = _make_deck(sample_rate=48000, seconds=5.0)
        idle.filepath = "armedTrack.wav"

        fake = _FakeEngine(active, idle=idle)
        fake._idle_armed = True
        fake._command_queue.append(("swap", "otherTrack.wav"))

        frames = 64
        outdata = np.zeros((frames, 2), dtype=np.float32)
        DeckEngine._callback(fake, outdata, frames, None, None)

        self.assertIs(fake._swap_ack, False)
        self.assertFalse(fake._swap_pending)
        self.assertIs(fake._active, active)  # no flip
        self.assertIs(fake._idle, idle)
        self.assertTrue(fake._idle_armed)    # untouched by a rejected mismatch

    def test_rejected_manual_swap_preserves_pending_natural_swap(self):
        # Decoupling regression: a natural auto-advance set _swap_pending
        # (the poll thread's spent-deck-unload + on_track_changed signal). A
        # racing manual swap that gets REJECTED must report only via
        # _swap_ack and must NOT touch _swap_pending -- otherwise the poll
        # thread drops the natural swap's housekeeping (leaked spent deck +
        # missing on_track_changed).
        active = _make_deck(sample_rate=48000, seconds=5.0)
        idle = _make_deck(sample_rate=48000, seconds=5.0)
        idle.filepath = "armed.wav"
        fake = _FakeEngine(active, idle=idle)
        fake._idle_armed = False              # -> manual swap will be rejected
        fake._swap_pending = True             # a natural swap is pending for the poll
        fake._pending_active_fp = "natural.wav"
        fake._command_queue.append(("swap", "armed.wav"))

        fake._drain_commands()

        self.assertIs(fake._swap_ack, False)          # manual rejected
        self.assertTrue(fake._swap_pending)           # natural signal preserved
        self.assertEqual(fake._pending_active_fp, "natural.wav")

    def test_seek_command_applied_in_callback_not_lost_under_advance(self):
        # Proves the command queue fixes the Phase-1 lost-update race: the
        # control thread's seek target must land exactly, not get
        # overwritten by the callback's own read_idx advance in the same
        # tick as a stale/racy direct write would.
        active = _make_deck(sample_rate=48000, seconds=5.0)
        active.read_idx = int(1.0 * 48000)
        active.active = False  # paused: isolate the drain from further fill/advance

        fake = _FakeEngine(active)
        target = int(3.5 * 48000)
        fake._command_queue.append(("seek", target))

        outdata = np.zeros((256, 2), dtype=np.float32)
        DeckEngine._callback(fake, outdata, 256, None, None)

        self.assertEqual(active.read_idx, target)  # exactly target, not target + 256
        self.assertFalse(active.just_ended)


class TestSwapRestoresPlayingState(unittest.TestCase):
    """dp-261: a manual Next (swap) after a stop/loop natural end must move
    _state back to PLAYING, or on_position stays suppressed and pause() no-ops.
    Drives the REAL poll loop briefly (not a hand-rolled tick) so the exact
    production critical section is what runs."""

    def _run_poll_briefly(self, engine, ticks=3):
        # retirement is not under test here -- stub it so the poll body cannot
        # block on joining a bare Deck's (nonexistent) decode thread.
        engine._retire_async = lambda deck: None
        engine.on_track_changed = None
        engine._poll_running = True
        thread = threading.Thread(target=engine._poll_position, daemon=True)
        thread.start()
        time.sleep(deck_engine_module._POLL_INTERVAL * ticks + 0.05)
        engine._poll_running = False
        thread.join(timeout=1.0)

    def test_swap_after_stop_end_restores_playing_and_resumes_position(self):
        engine = _make_bare_engine()
        engine._active = _make_deck(sample_rate=48000, seconds=5.0)
        engine._active.read_idx = 4800  # mid-track: position emits a nonzero value
        engine._state = deck_engine_module.STATE_STOPPED  # left by the natural end
        engine._swap_pending = True                        # the manual swap landed
        engine._pending_active_fp = "successor.wav"

        positions = []
        engine.on_position = positions.append

        self._run_poll_briefly(engine)

        self.assertEqual(engine._state, deck_engine_module.STATE_PLAYING)
        self.assertTrue(positions, "on_position must resume after the swap")

    def test_normal_swap_while_playing_stays_playing(self):
        # Regression guard: an ordinary mid-track Next (state already PLAYING)
        # must be a no-op for the new transition -- not perturbed by dp-261.
        engine = _make_bare_engine()
        engine._active = _make_deck(sample_rate=48000, seconds=5.0)
        engine._state = deck_engine_module.STATE_PLAYING
        engine._swap_pending = True
        engine._pending_active_fp = "successor.wav"
        engine.on_position = lambda pos: None

        self._run_poll_briefly(engine)

        self.assertEqual(engine._state, deck_engine_module.STATE_PLAYING)


class TestPreloadIdempotencyAndTeardown(unittest.TestCase):
    """Phase 2a control-path methods (preload/invalidate_preload) against a
    bare DeckEngine (`_make_bare_engine`: __init__ bypassed, no real
    sounddevice stream or poll thread, R3). Most cases use nonexistent
    filepaths -- the bundled ffmpeg fails fast on a missing input file (no
    such file), so `decode_complete` flips True almost immediately without
    needing a real playable asset. Tests that assert real arming use a tiny
    rendered tone instead (W4's `_frontier > 0` guard means a fast-failed
    decode, which never produces frames, correctly never arms)."""

    def test_preload_rebuilds_spent_deck_same_filepath(self):
        # Guards the A/B/A/B short-playlist bug: a SPENT deck sitting in the
        # idle slot (armed False, read_idx parked at its own end) whose
        # filepath happens to equal the new preload target must be REBUILT,
        # not treated as already-idempotent-ready. Uses a REAL tiny tone
        # (not a nonexistent path) so decode actually produces frames --
        # W4's `_frontier > 0` arm guard means a fast ffmpeg failure (0
        # frames) would never arm, which would defeat this test's purpose.
        with tempfile.TemporaryDirectory(prefix="dp216_test_") as tmpdir:
            fp = _make_tone_file(tmpdir, seconds=0.3, name="rebuild.wav")
            engine = _make_bare_engine()

            spent = engine._idle
            spent.filepath = fp
            spent.decode_complete = True
            spent._frontier = 500
            spent.read_idx = 500
            engine._idle_armed = False

            engine.preload(fp)

            # dp-fix: retire+load now happens on a background thread (was
            # synchronous on the caller's thread, which froze the UI on
            # every track advance) -- wait for the rebuild to land instead
            # of asserting read_idx immediately.
            rebuilt = _wait_for(lambda: engine._idle.read_idx == 0, timeout=5.0)
            self.assertTrue(rebuilt, "idle deck never rebuilt (read_idx never reset)")
            armed = _wait_for(lambda: engine._idle_armed, timeout=5.0)
            self.assertTrue(armed, "idle deck never armed after preload rebuild")
            self.assertEqual(engine._idle.filepath, fp)

    def test_preload_noop_when_already_armed_for_same_file(self):
        engine = _make_bare_engine()
        fp = "dp216_nonexistent_armed.wav"
        engine._idle.filepath = fp
        engine._idle.decode_complete = True
        engine._idle.read_idx = 12345
        engine._idle_armed = True

        engine.preload(fp)  # must no-op: already armed for this exact file

        self.assertEqual(engine._idle.read_idx, 12345)  # untouched
        self.assertTrue(engine._idle_armed)

    def test_set_auto_advance_travels_through_command_queue(self):
        # dp-254: set_auto_advance() must NEVER assign _auto_advance_armed
        # directly (correctness rule #1) -- it queues a command that only
        # _drain_commands applies.
        engine = _make_bare_engine()
        engine._auto_advance_armed = True

        engine.set_auto_advance(False)

        self.assertTrue(engine._auto_advance_armed)  # not yet applied
        self.assertEqual(list(engine._command_queue), [("set_auto_advance", False)])

        engine._drain_commands()

        self.assertFalse(engine._auto_advance_armed)  # applied by the drain

    def test_preload_rearms_auto_advance_via_command_queue(self):
        # dp-254: every preload() call re-arms auto-advance by default (via
        # the command queue, not a direct assignment) -- so a caller that
        # previously disarmed it (stop/loop) and later preloads a "next"
        # track gets auto-advance back without an explicit set_auto_advance
        # call of its own.
        engine = _make_bare_engine()
        engine._auto_advance_armed = False
        fp = "dp216_nonexistent_rearm.wav"
        engine._idle.filepath = fp
        engine._idle.decode_complete = True
        engine._idle_armed = True  # idempotent no-op path

        engine.preload(fp)

        self.assertEqual(list(engine._command_queue), [("set_auto_advance", True)])
        engine._drain_commands()
        self.assertTrue(engine._auto_advance_armed)

    def test_rearm_preload_command_order_leaves_auto_advance_disarmed(self):
        # dp-254 linchpin: `_rearm_preload` calls engine.preload() FIRST
        # (which enqueues set_auto_advance(True)) and engine.set_auto_advance
        # (False) SECOND for a stop/loop track. Both land in the same queue,
        # so the FIFO drain order is what decides whether a stop track
        # auto-advances. If anyone ever reorders those two calls, or the
        # queue stops being FIFO, stop/loop silently starts auto-advancing
        # again -- and neither the UI test (which mocks the engine and only
        # asserts the calls happen) nor the isolated set_auto_advance test
        # would catch it. This pins the COMBINATION.
        engine = _make_bare_engine()
        fp = "dp254_nonexistent_order.wav"
        engine._idle.filepath = fp
        engine._idle.decode_complete = True
        engine._idle_armed = True  # idempotent no-op path, still re-arms

        engine.preload(fp)              # enqueues ("set_auto_advance", True)
        engine.set_auto_advance(False)  # what _rearm_preload does next

        self.assertEqual(
            list(engine._command_queue),
            [("set_auto_advance", True), ("set_auto_advance", False)],
        )
        engine._drain_commands()
        self.assertFalse(
            engine._auto_advance_armed,
            "set_auto_advance(False) must win -- a stop/loop track would "
            "otherwise auto-advance at natural end",
        )
        # The idle deck stays ready, so a manual Next is still zero-latency.
        self.assertTrue(engine._idle_armed)

    def test_preload_noop_while_crossfade_running(self):
        # dp-216 Phase 5 (A9-style guard): during an active ramp, _idle IS
        # the deck being faded in and read directly by the audio callback
        # outside the lock -- preload() must not retire/reload it under a
        # live transition (e.g. main_window's _rearm_preload firing off a
        # playlist reorder mid-crossfade).
        engine = _make_bare_engine()
        engine._idle.filepath = "dp216_ramping.wav"
        engine._idle.read_idx = 777
        engine._idle_armed = False  # cleared at trigger (A5), same as real flight
        engine._crossfade_running = True

        engine.preload("dp216_other_track.wav")

        self.assertEqual(engine._idle.filepath, "dp216_ramping.wav")  # untouched
        self.assertEqual(engine._idle.read_idx, 777)

    def test_invalidate_preload_unarms(self):
        engine = _make_bare_engine()
        engine._idle.filepath = "dp216_nonexistent_B.wav"
        engine._idle.decode_complete = True
        engine._idle._frontier = 48000
        engine._idle_armed = True

        engine.invalidate_preload()

        self.assertIsNone(engine.preloaded_file)
        self.assertFalse(engine._idle_armed)

        # A subsequent true end must NOT stitch -- only _idle_armed gates a
        # swap, even though the idle deck's buffer/filepath still look
        # "ready".
        engine._active._frontier = 50
        engine._active.read_idx = 0
        prev_active = engine._active

        outdata = np.zeros((256, 2), dtype=np.float32)
        engine._callback(outdata, 256, None, None)

        self.assertIs(engine._active, prev_active)  # no flip
        self.assertFalse(engine._swap_pending)
        self.assertTrue(engine._active.just_ended)

    def test_lone_invalidate_retires_streamed_idle_deck(self):
        # Regression: a LONE invalidate_preload() (playlist cleared / last
        # track, no follow-up preload) on an OVER-CAP idle deck must retire
        # its mmap, not just unarm it -- otherwise the background decode
        # keeps running and the temp file stays resident until some later
        # preload/shutdown happens to reuse or close it. Force mmap via a
        # tiny monkeypatched cap on a real short tone.
        orig_cap = deck_engine_module._RESIDENT_CAP_SECONDS
        deck_engine_module._RESIDENT_CAP_SECONDS = 0.1
        try:
            with tempfile.TemporaryDirectory(prefix="dp216_test_") as asset_dir:
                fp = _make_tone_file(asset_dir, seconds=1.0, name="lone_invalidate.wav")
                engine = _make_bare_engine()
                try:
                    engine.preload(fp)
                    self.assertTrue(
                        _wait_for(lambda: engine._idle._streamed, timeout=5.0),
                        "test setup: preload never reached the mmap branch",
                    )
                    temp_path = engine._idle._temp_path
                    self.assertTrue(temp_path and os.path.exists(temp_path))

                    engine.invalidate_preload()

                    # dp-220: the retire is now ASYNCHRONOUS. invalidate_preload
                    # does the cheap unarm synchronously and pushes _retire to a
                    # background thread, because _retire joins a decode thread
                    # for up to 1s and this runs on the Qt main thread. So
                    # detach_buffer() no longer completes before invalidate
                    # returns -- wait for it instead of asserting it already
                    # happened. The invariant that matters is unchanged and
                    # still asserted below: the mmap is retired and the temp
                    # file goes away. Only the timing moved.
                    self.assertTrue(
                        _wait_for(lambda: not engine._idle._streamed, timeout=5.0),
                        "lone invalidate must still retire the streamed idle deck",
                    )
                    self.assertIsNone(engine._idle._temp_path)
                    # The CLOSE is still deferred (W3/B13) even once the
                    # retire has run -- _drain_pending_close's two-tick rule,
                    # not the retire itself, is what releases the mmap.
                    self.assertTrue(
                        os.path.exists(temp_path),
                        "must not close synchronously -- deferred via _retire",
                    )

                    engine._poll_tick += 2
                    engine._drain_pending_close()
                    self.assertFalse(
                        os.path.exists(temp_path),
                        "lone invalidate must retire the streamed idle deck's temp file",
                    )
                finally:
                    engine._active.stop_decode()
                    _cleanup_deck_buffer(engine._active)
                    engine._idle.stop_decode()
                    _cleanup_deck_buffer(engine._idle)
                    engine._drain_pending_close(force=True)
                    shutil.rmtree(engine._temp_dir, ignore_errors=True)
        finally:
            deck_engine_module._RESIDENT_CAP_SECONDS = orig_cap

    def test_arm_when_ready_bails_on_stale_generation(self):
        # R10 regression: a background arm-thread whose preload generation is
        # stale (a lone invalidate_preload, or a newer preload/load, bumped
        # _preload_gen after it was spawned) must NOT arm the idle deck --
        # otherwise the next natural end would stitch a removed/stale track.
        engine = _make_bare_engine()
        fp = "dp216_nonexistent_gen.wav"
        engine._idle.filepath = fp
        engine._idle.decode_complete = True  # decode already finished
        engine._idle._frontier = 1000  # W4: a real completed decode has frames

        engine._preload_gen = 5
        # Arm-thread carrying an OLD generation (as if invalidate bumped it):
        engine._arm_when_ready(engine._idle, fp, gen=4, timeout=0.5)
        self.assertFalse(engine._idle_armed, "stale-generation arm must not arm")

        # Positive control: the CURRENT generation arms normally.
        engine._arm_when_ready(engine._idle, fp, gen=5, timeout=0.5)
        self.assertTrue(engine._idle_armed)

    def test_invalidate_bumps_generation(self):
        # invalidate_preload must advance _preload_gen so a pending arm bails.
        engine = _make_bare_engine()
        before = engine._preload_gen
        engine.invalidate_preload()
        self.assertGreater(engine._preload_gen, before)
        self.assertFalse(engine._idle_armed)

    def test_no_zombie_after_rapid_preload_churn(self):
        engine = _make_bare_engine()
        fp_a = "dp216_nonexistent_churn_a.wav"
        fp_b = "dp216_nonexistent_churn_b.wav"

        for i in range(20):
            engine.preload(fp_a if i % 2 == 0 else fp_b)

        _wait_for(lambda: engine._idle.decode_complete, timeout=5.0)
        deadline = time.time() + 2.0
        while (
            engine._idle._decode_thread is not None
            and engine._idle._decode_thread.is_alive()
            and time.time() < deadline
        ):
            time.sleep(0.01)

        self.assertIsNone(engine._idle._decode_proc)
        alive = (
            engine._idle._decode_thread is not None
            and engine._idle._decode_thread.is_alive()
        )
        self.assertFalse(alive, "decode thread still alive after churn")


class TestMmapModeDecision(unittest.TestCase):
    """Phase 2b W2: buffer type (RAM vs memmap) is chosen from the probed
    duration vs the resident cap. `_RESIDENT_CAP_SECONDS` is monkeypatched
    small so these stay cheap/fast -- the classification logic is the same
    regardless of the cap's actual value."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="dp216_mmap_mode_")
        self._orig_cap = deck_engine_module._RESIDENT_CAP_SECONDS
        deck_engine_module._RESIDENT_CAP_SECONDS = 0.5

    def tearDown(self):
        deck_engine_module._RESIDENT_CAP_SECONDS = self._orig_cap
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_mode_decision_uses_cap(self):
        under = Deck(48000)
        under.load("nonexistent_under.wav", duration=0.2, temp_dir=self.tmpdir)
        try:
            self.assertFalse(under._streamed)
            self.assertNotIsInstance(under._buf, np.memmap)
        finally:
            under.stop_decode()
            _cleanup_deck_buffer(under)

        over = Deck(48000)
        over.load("nonexistent_over.wav", duration=2.0, temp_dir=self.tmpdir)
        try:
            self.assertTrue(over._streamed)
            self.assertIsInstance(over._buf, np.memmap)
        finally:
            over.stop_decode()
            _cleanup_deck_buffer(over)

    def test_mmap_buffer_is_not_resident_ndarray(self):
        deck = Deck(48000)
        deck.load("nonexistent.wav", duration=2.0, temp_dir=self.tmpdir)
        try:
            self.assertIsInstance(deck._buf, np.memmap)
        finally:
            deck.stop_decode()
            _cleanup_deck_buffer(deck)

    def test_two_adjacent_over_cap_decks_both_memmap(self):
        active = Deck(48000)
        idle = Deck(48000)
        active.load("nonexistent_a.wav", duration=2.0, temp_dir=self.tmpdir)
        idle.load("nonexistent_b.wav", duration=2.0, temp_dir=self.tmpdir)
        try:
            self.assertIsInstance(active._buf, np.memmap)
            self.assertIsInstance(idle._buf, np.memmap)
            self.assertIsNot(active._buf, idle._buf)  # two distinct backing files
        finally:
            for d in (active, idle):
                d.stop_decode()
                _cleanup_deck_buffer(d)

    def test_effective_end_is_frontier_not_buffer_size(self):
        # B9: the pre-sized mmap is LARGER than the real track (W2's margin).
        # _effective_end_frame must stay frontier-based, or every over-cap
        # track plays a trailing-silence tail.
        deck = Deck(48000)
        deck.load("nonexistent_margin.wav", duration=2.0, temp_dir=self.tmpdir)
        try:
            deck.stop_decode()  # no live decode racing the manual state below
            deck._frontier = int(2.0 * 48000)
            deck.decode_complete = True
            self.assertGreater(deck._buf.shape[0], deck._frontier)  # margin present
            self.assertEqual(deck._effective_end_frame(), deck._frontier)
        finally:
            _cleanup_deck_buffer(deck)

    def test_disk_or_mmap_failure_falls_back_to_ram(self):
        # W6: a disk-full / mmap-creation failure must fall back to RAM and
        # keep playing, not crash.
        with mock.patch("numpy.memmap", side_effect=OSError("disk full (simulated)")):
            deck = Deck(48000)
            deck.load("nonexistent_fallback.wav", duration=2.0, temp_dir=self.tmpdir)
        try:
            self.assertFalse(deck._streamed)
            self.assertFalse(isinstance(deck._buf, np.memmap))
            self.assertGreater(deck._buf.shape[0], 0)
        finally:
            deck.stop_decode()
            _cleanup_deck_buffer(deck)


class TestBufferingFlag(unittest.TestCase):
    """Phase 2b W5 (D7): Deck.is_buffering() is a pure state read -- no
    ffmpeg/temp files needed."""

    def test_is_buffering_true_when_read_past_frontier(self):
        deck = Deck(48000)
        deck.active = True
        deck._frontier = 1000
        deck.read_idx = 1000
        deck.decode_complete = False

        self.assertTrue(deck.is_buffering())

        deck._frontier = 2000  # decode catches up past read_idx
        self.assertFalse(deck.is_buffering())

    def test_is_buffering_false_once_decode_complete(self):
        # A deck parked exactly at its true end (decode_complete) is not
        # "buffering" -- it is simply finished.
        deck = Deck(48000)
        deck.active = True
        deck._frontier = 1000
        deck.read_idx = 1000
        deck.decode_complete = True

        self.assertFalse(deck.is_buffering())


class TestArmOnPrebuffer(unittest.TestCase):
    """Phase 2b W4/B10: the idle deck arms once it has a healthy prebuffer,
    not only once fully decode_complete -- an over-cap idle deck can take
    minutes to fully decode."""

    def test_arm_on_prebuffer_before_decode_complete(self):
        engine = _make_bare_engine()
        fp = "dp216_prebuffer_test.wav"
        engine._idle.filepath = fp
        engine._idle.decode_complete = False
        engine._idle._frontier = int(_ARM_PREBUFFER_SECONDS * engine.sample_rate) + 100

        engine._arm_when_ready(engine._idle, fp, gen=engine._preload_gen, timeout=0.5)

        self.assertTrue(engine._idle_armed, "idle deck should arm on prebuffer alone")

    def test_does_not_arm_below_prebuffer_and_not_decode_complete(self):
        engine = _make_bare_engine()
        fp = "dp216_underbuffer_test.wav"
        engine._idle.filepath = fp
        engine._idle.decode_complete = False
        engine._idle._frontier = int(_ARM_PREBUFFER_SECONDS * engine.sample_rate) - 100

        engine._arm_when_ready(engine._idle, fp, gen=engine._preload_gen, timeout=0.1)

        self.assertFalse(engine._idle_armed)

    def test_zero_frontier_never_arms_even_if_decode_complete(self):
        # B10 guard: a FAILED decode (ffmpeg errored -> decode_complete True
        # but zero frames) must not arm an empty deck that would
        # instant-end on swap.
        engine = _make_bare_engine()
        fp = "dp216_failed_decode_test.wav"
        engine._idle.filepath = fp
        engine._idle.decode_complete = True
        engine._idle._frontier = 0

        engine._arm_when_ready(engine._idle, fp, gen=engine._preload_gen, timeout=0.1)

        self.assertFalse(engine._idle_armed)


class TestMmapLifecycle(unittest.TestCase):
    """Phase 2b W3 (B5/B6/B13): deferred close of a retired mmap deck's
    temp file. Real ffmpeg, forced-mmap via a small monkeypatched cap."""

    def setUp(self):
        self.asset_dir = tempfile.mkdtemp(prefix="dp216_lifecycle_asset_")
        self._orig_cap = deck_engine_module._RESIDENT_CAP_SECONDS
        deck_engine_module._RESIDENT_CAP_SECONDS = 0.5

    def tearDown(self):
        deck_engine_module._RESIDENT_CAP_SECONDS = self._orig_cap
        shutil.rmtree(self.asset_dir, ignore_errors=True)

    def test_temp_file_created_and_retired(self):
        fp = _make_tone_file(self.asset_dir, seconds=1.0, name="short.wav")
        engine = _make_bare_engine()
        try:
            engine.preload(fp)
            # dp-fix: preload's retire+load now runs on a background thread.
            self.assertTrue(
                _wait_for(lambda: engine._idle._streamed, timeout=5.0),
                "idle deck never streamed after preload",
            )
            temp_path = engine._idle._temp_path
            self.assertTrue(temp_path and os.path.exists(temp_path))

            engine._retire(engine._idle)
            self.assertTrue(
                os.path.exists(temp_path),
                "must not close synchronously -- W3/B13 deferred close",
            )

            engine._poll_tick += 2  # >= 2 ticks elapsed -> eligible for close
            engine._drain_pending_close()
            self.assertFalse(os.path.exists(temp_path), "must be gone after the drain")
        finally:
            engine._active.stop_decode()
            _cleanup_deck_buffer(engine._active)
            engine._idle.stop_decode()
            _cleanup_deck_buffer(engine._idle)
            engine._drain_pending_close(force=True)
            shutil.rmtree(engine._temp_dir, ignore_errors=True)

    def test_no_temp_file_leak_after_churn(self):
        fp_a = _make_tone_file(self.asset_dir, seconds=1.0, name="churn_a.wav")
        fp_b = _make_tone_file(self.asset_dir, seconds=1.0, name="churn_b.wav")
        engine = _make_bare_engine()
        try:
            for i in range(20):
                engine.preload(fp_a if i % 2 == 0 else fp_b)
                engine._poll_tick += 1
                engine._drain_pending_close()

            # dp-fix: preload's retire+load runs on a background thread now
            # -- let the final one land before retiring/draining below, or
            # its (racing) load() call would create a temp file after the
            # force-drain and this test would see it as "leaked".
            last_fp = fp_b if (19 % 2) else fp_a
            _wait_for(
                lambda: engine._idle.filepath == last_fp and engine._idle_armed,
                timeout=5.0,
            )

            # Retire whatever is still live (active + the final idle) and
            # force-drain everything regardless of age.
            engine._retire(engine._active)
            engine._retire(engine._idle)
            engine._drain_pending_close(force=True)

            remaining = os.listdir(engine._temp_dir)
            self.assertEqual(remaining, [], f"leaked temp files: {remaining}")
        finally:
            shutil.rmtree(engine._temp_dir, ignore_errors=True)

    def test_load_replacing_active_mmap_deck_no_crash(self):
        # B13 -- the crash-safety gate: retiring a live active mmap deck
        # while a callback loop keeps reading it must NEVER close/unmap the
        # buffer synchronously (that is an unmapped-memory segfault on the
        # realtime thread, not a catchable exception -- if the deferred
        # design is wrong, this test process crashes outright rather than
        # failing an assertion).
        fp_old = _make_tone_file(self.asset_dir, seconds=1.5, name="old.wav")
        fp_new = _make_tone_file(self.asset_dir, seconds=1.0, name="new.wav")
        engine = _make_bare_engine()
        stop_flag = threading.Event()
        cb_thread = None
        try:
            engine._active = Deck(engine.sample_rate)
            engine._active.load(fp_old, engine._read_duration(fp_old), engine._temp_dir)
            engine._active.active = True
            _wait_for(lambda: engine._active._frontier > 1000, timeout=5.0)
            old_temp_path = engine._active._temp_path
            self.assertTrue(engine._active._streamed, "test setup: cap not forced")

            errors = []
            block = 256
            outdata = np.zeros((block, 2), dtype=np.float32)

            def _callback_loop():
                while not stop_flag.is_set():
                    try:
                        engine._callback(outdata, block, None, None)
                    except Exception as e:  # pragma: no cover -- gate, not expected
                        errors.append(e)
                        return
                    time.sleep(0.001)

            cb_thread = threading.Thread(target=_callback_loop, daemon=True)
            cb_thread.start()
            time.sleep(0.05)

            # Retires the old active deck's mmap while the callback loop is
            # live -- must defer the close, never crash.
            engine.load(fp_new)

            for _ in range(5):
                time.sleep(0.03)
                engine._poll_tick += 1
                engine._drain_pending_close()

            stop_flag.set()
            cb_thread.join(timeout=2.0)
            cb_thread = None

            self.assertEqual(errors, [], f"callback thread raised: {errors}")
            self.assertFalse(
                os.path.exists(old_temp_path), "old temp file should be retired by now"
            )
        finally:
            stop_flag.set()
            if cb_thread is not None:
                cb_thread.join(timeout=2.0)
            engine._active.stop_decode()
            _cleanup_deck_buffer(engine._active)
            engine._idle.stop_decode()
            _cleanup_deck_buffer(engine._idle)
            engine._drain_pending_close(force=True)
            shutil.rmtree(engine._temp_dir, ignore_errors=True)


class _RecordingStream:
    """Stub for DeckEngine._stream: records call order instead of touching
    real PortAudio (no live device needed, R3)."""

    def __init__(self, order):
        self._order = order

    def stop(self):
        self._order.append("stream_stop")

    def close(self):
        self._order.append("stream_close")


class TestShutdownOrdering(unittest.TestCase):
    """Phase 2b W3: shutdown() must stop+close the stream BEFORE closing any
    deck buffer -- 2a's order (unload decks, then stop the stream) segfaults
    with a live mmap deck (B13)."""

    def test_shutdown_stops_stream_before_closing_buffers(self):
        tmpdir = tempfile.mkdtemp(prefix="dp216_shutdown_test_")
        try:
            engine = _make_bare_engine(temp_dir=tmpdir)
            order = []
            engine._stream = _RecordingStream(order)
            engine._poll_running = False
            engine._poll_thread = threading.Thread(target=lambda: None, daemon=True)
            engine._poll_thread.start()

            # A real (tiny) memmap on the active deck -- no ffmpeg decode
            # needed, this test only cares about the close ORDER.
            temp_path = os.path.join(tmpdir, "shutdown_active.raw")
            engine._active._buf = np.memmap(
                temp_path, dtype=np.int16, mode="w+", shape=(100, 2)
            )
            engine._active._streamed = True
            engine._active._temp_path = temp_path
            engine._active._decode_thread = None
            engine._active._decode_proc = None

            original_close = DeckEngine._close_mmap

            def _recording_close(mm, path):
                order.append("buffer_close")
                return original_close(mm, path)

            DeckEngine._close_mmap = staticmethod(_recording_close)
            try:
                engine.shutdown()
            finally:
                DeckEngine._close_mmap = staticmethod(original_close)

            self.assertIn("stream_stop", order)
            self.assertIn("buffer_close", order)
            self.assertLess(order.index("stream_stop"), order.index("buffer_close"))
            self.assertLess(order.index("stream_close"), order.index("buffer_close"))
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)


class TestForcedMmapSoak(unittest.TestCase):
    """Phase 2b R13 automated gate: a short real tone forced onto the mmap
    path, played through a PACED (~1x) callback loop -- NOT a tight loop
    (the reader must not artificially outpace the decoder) and NOT a real
    audio device. Asserts the decode frontier never falls to/behind the
    read pointer mid-track. This is the one intentionally ~15s test."""

    def setUp(self):
        self.asset_dir = tempfile.mkdtemp(prefix="dp216_soak_asset_")
        self._orig_cap = deck_engine_module._RESIDENT_CAP_SECONDS
        deck_engine_module._RESIDENT_CAP_SECONDS = 1.0

    def tearDown(self):
        deck_engine_module._RESIDENT_CAP_SECONDS = self._orig_cap
        shutil.rmtree(self.asset_dir, ignore_errors=True)

    def test_forced_mmap_short_soak_frontier_stays_ahead(self):
        seconds = 15
        fp = _make_tone_file(self.asset_dir, seconds=seconds, name="soak.wav")
        engine = _make_bare_engine()
        try:
            engine._active = Deck(engine.sample_rate)
            duration = engine._read_duration(fp)
            engine._active.load(fp, duration, engine._temp_dir)
            self.assertTrue(engine._active._streamed, "test setup: cap not forced")
            deck = engine._active

            # Cold-select prebuffer (D3) before releasing to the "callback".
            deadline = time.time() + 5.0
            while deck._frontier < int(2.0 * engine.sample_rate) and time.time() < deadline:
                time.sleep(0.01)
            deck.active = True

            block = _BLOCK_SIZE
            block_seconds = block / float(engine.sample_rate)
            total_blocks = int(seconds / block_seconds) + 40
            outdata = np.zeros((block, 2), dtype=np.float32)

            reached_end = False
            for _ in range(total_blocks):
                DeckEngine._callback(engine, outdata, block, None, None)
                if deck.just_ended:
                    reached_end = True
                    break
                occupancy = deck._frontier - deck.read_idx
                self.assertGreater(
                    occupancy, 0,
                    "frontier did not stay ahead of read_idx -- silence net "
                    "would fire mid-track",
                )
                time.sleep(block_seconds)  # ~1x pacing (per plan: NOT a tight loop)

            self.assertTrue(reached_end, "soak did not reach the end of the tone")
        finally:
            engine._active.stop_decode()
            _cleanup_deck_buffer(engine._active)
            engine._idle.stop_decode()
            _cleanup_deck_buffer(engine._idle)
            shutil.rmtree(engine._temp_dir, ignore_errors=True)


class TestSyntheticHourScaleMath(unittest.TestCase):
    """Phase 2b: index ARITHMETIC at hour scale, on a tiny buffer (NOT a
    >1hr / ~690MB allocation) -- proves position/end-trigger math holds at
    magnitude, plain Python ints, no overflow."""

    def test_synthetic_hour_scale_index_math(self):
        sr = 48000
        hour_frames = 60 * 60 * sr  # 172,800,000 -- 1 hour at 48kHz

        deck = Deck(sr)
        deck._buf = np.zeros((10, 2), dtype=np.int16)  # tiny; frontier is what matters
        deck._frontier = hour_frames
        deck.decode_complete = True
        deck.duration = hour_frames / sr
        deck.read_idx = hour_frames - sr  # one second before the true end

        # position math mirrors DeckEngine.position (read_idx / sample_rate)
        self.assertAlmostEqual(deck.read_idx / float(sr), 3599.0, places=3)
        self.assertEqual(deck._effective_end_frame(), hour_frames)

        step = deck.advance(sr)  # advance the final second -- should trigger end
        self.assertTrue(deck.just_ended)
        self.assertEqual(deck.read_idx, hour_frames)
        self.assertEqual(step, sr)

        # End marker at hour scale.
        deck2 = Deck(sr)
        deck2._buf = np.zeros((10, 2), dtype=np.int16)
        deck2._frontier = hour_frames + sr
        deck2.decode_complete = True
        deck2.end_marker = float(hour_frames) / sr  # exactly the 1-hour mark
        deck2.read_idx = hour_frames - 1024

        deck2.advance(2048)

        self.assertTrue(deck2.just_ended)
        self.assertGreaterEqual(deck2.read_idx, hour_frames)


class _LinearOverlap:
    """Duck-typed crossfade curve stand-in for arm_crossfade/set_crossfade
    tests -- linear ramps make expected mid-fade values trivial to assert,
    without pulling in core.crossfade_model (kept untouched per the plan)."""

    def __init__(self, duration):
        self.duration = duration

    def evaluate_in(self, t):
        return t

    def evaluate_out(self, t):
        return 1.0 - t


class TestDeckGainReset(unittest.TestCase):
    """A11 regression: Deck.load() must reset gain to 1.0 -- a deck that
    finished as the ramped-out side of a crossfade (gain ~0.0) and is later
    reloaded must not start silent."""

    def test_load_resets_gain_to_unity(self):
        deck = Deck(48000)
        deck.gain = 0.0
        deck.load("dp216_gain_reset.wav", duration=1.0, temp_dir=None)
        try:
            self.assertEqual(deck.gain, 1.0)
        finally:
            deck.stop_decode()
            _cleanup_deck_buffer(deck)


class TestCrossfadeTrigger(unittest.TestCase):
    """Phase 3 (dp-216): the callback's crossfade trigger check."""

    def test_armed_overlap_fires_at_trigger_distance(self):
        overlap_len = 100
        active = _make_deck(sample_rate=48000, seconds=1.0, amplitude=16000)
        active.read_idx = active._frontier - overlap_len  # exactly at the trigger point

        idle = _make_deck(sample_rate=48000, seconds=1.0, amplitude=8000)
        idle.filepath = "idleTrack.wav"
        idle.active = False

        fake = _FakeEngine(active, idle=idle)
        fake._idle_armed = True  # idle deck ready -- crossfade trigger requires it
        fake._command_queue.append(("set_crossfade", _LinearOverlap(overlap_len / 48000.0)))

        outdata = np.zeros((32, 2), dtype=np.float32)
        DeckEngine._callback(fake, outdata, 32, None, None)

        self.assertTrue(fake._crossfade_running)
        self.assertTrue(idle.active)
        self.assertFalse(fake._idle_armed)  # A5: consumed by the ramp

    def test_unarmed_idle_does_not_trigger_crossfade(self):
        # The crossfade trigger requires _idle_armed (same authority the
        # gapless path uses). An armed overlap whose idle deck is NOT ready
        # (still decoding / invalidated) must NOT fire the ramp -- otherwise
        # it would blend in an empty/stale deck (silence or wrong track). It
        # stays un-triggered and falls through to the gapless path at the end.
        overlap_len = 100
        active = _make_deck(sample_rate=48000, seconds=1.0, amplitude=16000)
        active.read_idx = active._frontier - overlap_len  # past the trigger distance

        idle = _make_deck(sample_rate=48000, seconds=1.0, amplitude=8000)
        idle.filepath = "idleTrack.wav"
        idle.active = False

        fake = _FakeEngine(active, idle=idle)
        fake._idle_armed = False  # idle deck NOT ready
        fake._command_queue.append(("set_crossfade", _LinearOverlap(overlap_len / 48000.0)))

        outdata = np.zeros((32, 2), dtype=np.float32)
        DeckEngine._callback(fake, outdata, 32, None, None)

        self.assertFalse(fake._crossfade_running)  # did not fire
        self.assertFalse(idle.active)              # idle deck untouched

    def test_auto_advance_disarmed_never_triggers_crossfade(self):
        # dp-254: belt-and-braces gate on the crossfade trigger. An armed
        # overlap + a ready idle deck must still NOT start a ramp when
        # auto-advance is disarmed (stop/loop successor) -- guards against a
        # stale _crossfade_len from a prior track firing a ramp the stop/loop
        # branch never intended.
        overlap_len = 100
        active = _make_deck(sample_rate=48000, seconds=1.0, amplitude=16000)
        active.read_idx = active._frontier - overlap_len

        idle = _make_deck(sample_rate=48000, seconds=1.0, amplitude=8000)
        idle.filepath = "idleTrack.wav"
        idle.active = False

        fake = _FakeEngine(active, idle=idle)
        fake._idle_armed = True
        fake._auto_advance_armed = False
        fake._command_queue.append(("set_crossfade", _LinearOverlap(overlap_len / 48000.0)))

        outdata = np.zeros((32, 2), dtype=np.float32)
        DeckEngine._callback(fake, outdata, 32, None, None)

        self.assertFalse(fake._crossfade_running)  # did not fire
        self.assertFalse(idle.active)               # idle deck untouched
        self.assertTrue(fake._idle_armed)            # still ready for a manual Next

    def test_active_ab_loop_suppresses_crossfade_trigger(self):
        # dp-259: an A-B loop whose B point sits inside the last
        # _crossfade_len frames must not start a ramp -- Deck.advance() and
        # Deck.fill_into() both guard their own end check on
        # `loop_active and loop_b is not None`; the crossfade trigger is a
        # separate comparison in _callback and needs the identical guard.
        overlap_len = 100
        active = _make_deck(sample_rate=48000, seconds=1.0, amplitude=16000)
        end = active._frontier
        active.loop_active = True
        active.loop_a = (end - overlap_len - 200) / active.sample_rate
        active.loop_b = (end - 10) / active.sample_rate  # B inside the trigger window
        active.read_idx = end - overlap_len  # looping playback reaches the zone

        idle = _make_deck(sample_rate=48000, seconds=1.0, amplitude=8000)
        idle.filepath = "idleTrack.wav"
        idle.active = False

        fake = _FakeEngine(active, idle=idle)
        fake._idle_armed = True
        fake._auto_advance_armed = True
        fake._command_queue.append(("set_crossfade", _LinearOverlap(overlap_len / 48000.0)))

        outdata = np.zeros((32, 2), dtype=np.float32)
        DeckEngine._callback(fake, outdata, 32, None, None)

        self.assertFalse(fake._crossfade_running)  # did not fire mid-loop
        self.assertFalse(idle.active)               # idle deck untouched
        self.assertTrue(fake._idle_armed)            # still armed, ready when loop clears

    def test_clearing_loop_restores_crossfade_trigger(self):
        # dp-259 follow-through: once loop_active is cleared, the SAME
        # position that was suppressed above must fire normally.
        overlap_len = 100
        active = _make_deck(sample_rate=48000, seconds=1.0, amplitude=16000)
        end = active._frontier
        active.loop_active = False
        active.loop_a = None
        active.loop_b = None
        active.read_idx = end - overlap_len

        idle = _make_deck(sample_rate=48000, seconds=1.0, amplitude=8000)
        idle.filepath = "idleTrack.wav"
        idle.active = False

        fake = _FakeEngine(active, idle=idle)
        fake._idle_armed = True
        fake._auto_advance_armed = True
        fake._command_queue.append(("set_crossfade", _LinearOverlap(overlap_len / 48000.0)))

        outdata = np.zeros((32, 2), dtype=np.float32)
        DeckEngine._callback(fake, outdata, 32, None, None)

        self.assertTrue(fake._crossfade_running)
        self.assertTrue(idle.active)

    def test_undetermined_end_frame_never_fires_crossfade(self):
        # dp-260 gap 2: an over-cap mmap-streamed track whose decode is still
        # racing has no known end frame (_effective_end_frame() returns None
        # until decode_complete, dp-221 -- see deck_engine.py:708-719). The
        # trigger's `end is not None` guard must degrade this to a no-op,
        # not crash and not leave partial ramp state, even with read_idx
        # sitting right at the tail of what has decoded so far.
        overlap_len = 100
        active = _make_deck(sample_rate=48000, seconds=1.0, amplitude=16000)
        active.decode_complete = False  # still racing -- no known end frame
        active.end_marker = None
        active.read_idx = active._frontier - overlap_len

        idle = _make_deck(sample_rate=48000, seconds=1.0, amplitude=8000)
        idle.filepath = "idleTrack.wav"
        idle.active = False

        fake = _FakeEngine(active, idle=idle)
        fake._idle_armed = True
        fake._auto_advance_armed = True
        fake._command_queue.append(("set_crossfade", _LinearOverlap(overlap_len / 48000.0)))

        outdata = np.zeros((32, 2), dtype=np.float32)
        DeckEngine._callback(fake, outdata, 32, None, None)  # must not raise

        self.assertFalse(fake._crossfade_running)  # no-op, not a crash
        self.assertFalse(idle.active)
        self.assertTrue(fake._idle_armed)  # untouched -- gapless still owns the true end

    def test_overlap_zero_never_triggers_and_gapless_path_is_unchanged(self):
        # A4: arm_crossfade(None) / duration 0 must never set _crossfade_running,
        # and the existing gapless stitch (TestGaplessRotation) stays intact.
        tail = 50
        active = _make_deck(sample_rate=48000, seconds=1.0, amplitude=16000)
        active._frontier = tail

        idle = _make_deck(sample_rate=48000, seconds=1.0, amplitude=8000)
        idle.filepath = "idleTrack.wav"

        fake = _FakeEngine(active, idle=idle)
        fake._idle_armed = True
        fake._command_queue.append(("set_crossfade", None))

        frames = 256
        outdata = np.zeros((frames, 2), dtype=np.float32)
        DeckEngine._callback(fake, outdata, frames, None, None)

        self.assertFalse(fake._crossfade_running)
        self.assertIs(fake._active, idle)   # gapless swap still happened
        self.assertIs(fake._idle, active)
        self.assertFalse(fake._idle_armed)


class TestCrossfadeFinalize(unittest.TestCase):
    """Phase 3 (dp-216): finalize on elapsed >= len, or an early just_ended."""

    def test_finalize_on_elapsed_swaps_refs_and_resets_gain(self):
        overlap_len = 64
        active = _make_deck(sample_rate=48000, seconds=1.0, amplitude=16000)
        active.read_idx = active._frontier - overlap_len

        idle = _make_deck(sample_rate=48000, seconds=1.0, amplitude=8000)
        idle.filepath = "idleTrack.wav"
        idle.active = False

        fake = _FakeEngine(active, idle=idle)
        fake._idle_armed = True  # idle deck ready -- crossfade trigger requires it
        fake._command_queue.append(("set_crossfade", _LinearOverlap(overlap_len / 48000.0)))

        outdata = np.zeros((32, 2), dtype=np.float32)
        DeckEngine._callback(fake, outdata, 32, None, None)  # trigger
        self.assertTrue(fake._crossfade_running)

        DeckEngine._callback(fake, outdata, 32, None, None)  # elapsed reaches overlap_len

        self.assertFalse(fake._crossfade_running)
        self.assertEqual(fake._crossfade_len, 0)
        self.assertIsNone(fake._crossfade_overlap)
        self.assertIs(fake._active, idle)   # refs swapped
        self.assertIs(fake._idle, active)
        self.assertEqual(active.gain, 1.0)  # both gains reset
        self.assertEqual(idle.gain, 1.0)
        self.assertTrue(fake._swap_pending)
        self.assertEqual(fake._pending_active_fp, "idleTrack.wav")

    def test_early_just_ended_finalizes_cleanly(self):
        # A6: a track shorter than predicted (early just_ended) must not
        # leave a stuck fade -- finalize fires on just_ended even though
        # elapsed hasn't reached the full crossfade length yet.
        overlap_len = 10000  # much longer than the active deck actually has left
        active = _make_deck(sample_rate=48000, seconds=1.0, amplitude=16000)
        active.read_idx = active._frontier - 20  # only 20 frames of real audio left

        idle = _make_deck(sample_rate=48000, seconds=1.0, amplitude=8000)
        idle.filepath = "idleTrack.wav"
        idle.active = False

        fake = _FakeEngine(active, idle=idle)
        fake._idle_armed = True  # idle deck ready -- crossfade trigger requires it
        fake._command_queue.append(("set_crossfade", _LinearOverlap(overlap_len / 48000.0)))

        outdata = np.zeros((256, 2), dtype=np.float32)
        DeckEngine._callback(fake, outdata, 256, None, None)  # triggers + exhausts active

        self.assertTrue(active.just_ended)
        self.assertFalse(fake._crossfade_running)  # finalized despite elapsed < len
        self.assertIs(fake._active, idle)
        self.assertTrue(fake._swap_pending)


class TestCrossfadeBlendOutput(unittest.TestCase):
    """Phase 3 (dp-216): output is the gain-summed blend of both decks
    across the running crossfade, using the pre-allocated _mix_scratch."""

    def test_blend_ramps_from_outgoing_to_incoming(self):
        overlap_len = 1000
        active = _make_deck(sample_rate=48000, seconds=1.0, amplitude=16000)
        active.read_idx = active._frontier - overlap_len
        active.track_volume = 1.0

        idle = _make_deck(sample_rate=48000, seconds=1.0, amplitude=16000)
        idle.filepath = "idleTrack.wav"
        idle.active = False
        idle.track_volume = 1.0

        fake = _FakeEngine(active, idle=idle, master_volume=1.0)
        fake._idle_armed = True  # idle deck ready -- crossfade trigger requires it
        fake._command_queue.append(("set_crossfade", _LinearOverlap(overlap_len / 48000.0)))

        frames = 32
        outdata = np.zeros((frames, 2), dtype=np.float32)
        DeckEngine._callback(fake, outdata, frames, None, None)  # t ~= 0: outgoing only
        first_block = outdata.copy()

        expected_full = 16000 / 32768.0
        # t=0 block: outgoing near full scale, incoming near silent.
        np.testing.assert_allclose(first_block[0], expected_full, atol=0.05)

        # Drive elapsed close to overlap_len so t -> 1 (incoming dominates).
        fake._crossfade_elapsed = overlap_len - frames
        outdata2 = np.zeros((frames, 2), dtype=np.float32)
        DeckEngine._callback(fake, outdata2, frames, None, None)

        # Uses the pre-allocated scratch buffer, not a fresh allocation.
        self.assertEqual(fake._mix_scratch.shape, (_BLOCK_SIZE, 2))


class TestCrossfadeCancel(unittest.TestCase):
    """Phase 3 (dp-216): A7/A8/A9 -- load()/seek() cancel an in-flight
    crossfade; swap_to_preloaded() stays rejected while one runs."""

    def test_load_enqueues_cancel_crossfade_command(self):
        # A7: load() must enqueue ("cancel_crossfade",) so a manual "Next"
        # mid-crossfade hard-cuts instead of leaving a stuck ramp. load()
        # itself never drains the command queue (rule #1: only the callback
        # does) -- so the freshly-queued command is still there to inspect.
        engine = _make_bare_engine()

        engine.load("dp216_cancel_via_load.wav")

        self.assertIn(("cancel_crossfade",), list(engine._command_queue))

        # Prove the queued command actually cancels a running crossfade when
        # the callback later drains it, against a stand-in fake sharing the
        # engine's queue.
        fake = _FakeEngine(engine._active, idle=engine._idle)
        fake._command_queue = engine._command_queue
        fake._crossfade_running = True
        fake._crossfade_len = 500
        fake._crossfade_overlap = _LinearOverlap(500 / engine.sample_rate)
        fake._active.gain = 0.4
        fake._idle.gain = 0.6
        fake._drain_commands()

        self.assertFalse(fake._crossfade_running)
        self.assertEqual(fake._crossfade_len, 0)
        self.assertIsNone(fake._crossfade_overlap)
        self.assertEqual(fake._active.gain, 1.0)
        self.assertEqual(fake._idle.gain, 1.0)

        engine._active.stop_decode()
        _cleanup_deck_buffer(engine._active)
        engine._idle.stop_decode()
        _cleanup_deck_buffer(engine._idle)
        engine._drain_pending_close(force=True)
        shutil.rmtree(engine._temp_dir, ignore_errors=True)

    def test_seek_enqueues_cancel_before_seek(self):
        active = _make_deck(sample_rate=48000, seconds=5.0)
        active.read_idx = int(1.0 * 48000)
        active.active = False

        fake = _FakeEngine(active)
        fake._crossfade_running = True
        fake._crossfade_len = 500
        fake._crossfade_overlap = _LinearOverlap(500 / 48000.0)
        fake.sample_rate = 48000

        # Mirror seek()'s enqueue order (A8) directly against the fake's queue.
        fake._command_queue.append(("cancel_crossfade",))
        target = int(3.5 * 48000)
        fake._command_queue.append(("seek", target))

        fake._drain_commands()

        self.assertFalse(fake._crossfade_running)
        self.assertEqual(active.read_idx, target)  # seek still lands

    def test_swap_to_preloaded_rejected_while_crossfade_running(self):
        # A5/A7: _idle_armed is cleared the instant a crossfade triggers, so
        # a manual swap_to_preloaded() during a running crossfade hits the
        # same rejection path as any other armed/filepath mismatch -- no new
        # cancel path invented. Simulate the post-trigger state directly.
        active = _make_deck(sample_rate=48000, seconds=5.0)
        idle = _make_deck(sample_rate=48000, seconds=5.0)
        idle.filepath = "armed.wav"

        fake = _FakeEngine(active, idle=idle)
        fake._idle_armed = False  # cleared at trigger (A5)
        fake._crossfade_running = True
        fake._command_queue.append(("swap", "armed.wav"))

        fake._drain_commands()

        self.assertIs(fake._swap_ack, False)
        self.assertIs(fake._active, active)  # no flip

    def test_stop_cancels_crossfade_synchronously_and_by_command(self):
        # dp-216 Phase 5: a STOPPED engine must never carry live crossfade
        # state. The A9 guards make preload()/arm_crossfade()/
        # invalidate_preload() ALL no-op while _crossfade_running is True, so
        # nothing downstream can clear it -- it is not self-healing.
        #
        # The synchronous clear matters because the queued command is only
        # drained by the audio callback a block later (~21ms), while
        # main_window's _on_clear_playlist runs
        # stop() -> playlist.clear() -> _rearm_preload() synchronously INSIDE
        # that window; a queue-only fix would still let the guards see a
        # stale True and skip retiring the idle deck.
        engine = _make_bare_engine()
        engine._crossfade_running = True
        engine._crossfade_len = 500
        engine._crossfade_overlap = _LinearOverlap(500 / engine.sample_rate)
        engine._active.active = True
        engine._state = "playing"

        engine.stop()

        # Cleared immediately, not one audio block later.
        self.assertFalse(engine._crossfade_running)
        self.assertEqual(engine._state, "stopped")
        # ...and the queued command is there to clear the rest (overlap/len/
        # gains/idle.active) safely on the audio thread.
        self.assertIn(("cancel_crossfade",), list(engine._command_queue))
        # The overlap is deliberately NOT nulled synchronously: a callback
        # already inside the crossfade branch dereferences it every block.
        self.assertIsNotNone(engine._crossfade_overlap)

    def test_stopped_engine_lets_invalidate_preload_retire_idle(self):
        # The bug this guards, end to end: _on_clear_playlist does
        # engine.stop() then (synchronously) _rearm_preload() ->
        # invalidate_preload(). If stop() left _crossfade_running set, A9
        # would no-op the invalidate and strand the idle deck armed.
        engine = _make_bare_engine()
        engine._crossfade_running = True
        engine._idle.filepath = "stranded.wav"
        engine._idle_armed = True
        engine._active.active = True
        engine._state = "playing"

        engine.stop()
        engine.invalidate_preload()  # same turn, before any callback runs

        self.assertFalse(engine._idle_armed)
        self.assertIsNone(engine.preloaded_file)

    def test_arm_crossfade_noop_while_crossfade_running(self):
        # dp-216 Phase 5: the callback reads _crossfade_len/_crossfade_overlap
        # every block to compute the in-flight ramp's progress (t = elapsed /
        # _crossfade_len) -- overwriting them mid-ramp (e.g. main_window's
        # _rearm_preload firing off a playlist reorder during an active
        # crossfade) must not corrupt that math for the transition already
        # in flight.
        engine = _make_bare_engine()
        engine._crossfade_running = True
        engine._crossfade_len = 500
        original_overlap = _LinearOverlap(500 / engine.sample_rate)
        engine._crossfade_overlap = original_overlap

        engine.arm_crossfade(_LinearOverlap(1000 / engine.sample_rate))

        self.assertEqual(list(engine._command_queue), [])  # nothing enqueued
        self.assertEqual(engine._crossfade_len, 500)  # in-flight ramp untouched
        self.assertIs(engine._crossfade_overlap, original_overlap)


class _FakeSettings:
    """Minimal settings stand-in: get(key, default)/set(key, value) over a
    backing dict. Patched in via mock.patch.object(deck_engine_module,
    "settings", ...) so public-API persistence tests stay hermetic -- never
    touches config/sava_settings.json."""

    def __init__(self):
        self._data = {}

    def get(self, key, default=None):
        return self._data.get(key, default)

    def set(self, key, value):
        self._data[key] = value

    def save(self):
        self.save_calls = getattr(self, "save_calls", 0) + 1


class TestEnginePublicAPIParity(unittest.TestCase):
    """dp-216 Phase 4: proof that DeckEngine's PUBLIC methods (the ones
    ui/main_window.py and the ArtNet dispatch table call) match
    core.engine.AudioEngine's semantics. Loop/cue/end-marker/volume/seek
    math and Deck.advance-level end/loop triggers are already covered
    elsewhere (TestLoopWrap, TestEndTrigger, TestSeekIndexMath); this class
    is the thin public-surface wrapper proof only.

    Honest-coupling boundary: shuffle/repeat and ArtNet-driven actions are
    Phase-5 end-to-end behaviors that route through core.playlist and
    ui/main_window.py, not DeckEngine alone -- no dead end-to-end tests are
    added here for them. At the engine level, swap correctness
    (TestGaplessRotation) and these public methods are the unit-level
    proof."""

    def setUp(self):
        self.fake_settings = _FakeSettings()
        self._settings_patcher = mock.patch.object(
            deck_engine_module, "settings", self.fake_settings
        )
        self._settings_patcher.start()

    def tearDown(self):
        self._settings_patcher.stop()

    def test_loop_ab_default_toggle_and_clear(self):
        engine = _make_bare_engine()
        engine._active.read_idx = int(2.5 * engine.sample_rate)

        engine.set_loop_a()  # no arg -> defaults to current position
        self.assertAlmostEqual(engine._active.loop_a, 2.5)
        self.assertEqual(engine.loop_points, (2.5, None, False))

        engine.toggle_loop_ab()  # b still None -> no-op
        self.assertFalse(engine._active.loop_active)

        engine.set_loop_b(4.0)
        self.assertEqual(engine._active.loop_b, 4.0)

        engine.toggle_loop_ab()
        self.assertTrue(engine._active.loop_active)
        self.assertEqual(engine.loop_points, (2.5, 4.0, True))

        engine.toggle_loop_ab()
        self.assertFalse(engine._active.loop_active)

        engine.clear_loop()
        self.assertIsNone(engine._active.loop_a)
        self.assertIsNone(engine._active.loop_b)
        self.assertFalse(engine._active.loop_active)
        self.assertEqual(engine.loop_points, (None, None, False))

    def test_cue_points_set_persist_copy_and_jump(self):
        engine = _make_bare_engine()
        engine._active.filepath = "fake.wav"
        engine._active.read_idx = int(1.0 * engine.sample_rate)

        engine.set_cue(0)  # no position arg -> defaults to current position
        self.assertEqual(engine._active.cue_points[0], 1.0)
        slots = self.fake_settings.get("cue_points", {})["fake.wav"]
        self.assertEqual(slots[0], 1.0)

        engine.set_cue(7, 9.5)
        slots = self.fake_settings.get("cue_points", {})["fake.wav"]
        self.assertEqual(len(slots), 8)
        self.assertEqual(slots[7], 9.5)

        cues = engine.cue_points
        cues[99] = "mutated"
        self.assertNotIn(99, engine._active.cue_points)  # property returns a copy

        engine._command_queue.clear()
        engine.jump_to_cue(7)
        kinds = [cmd[0] for cmd in engine._command_queue]
        self.assertIn("seek", kinds)

        engine._command_queue.clear()
        engine.jump_to_cue(3)  # unset slot -> no-op
        self.assertEqual(len(engine._command_queue), 0)

    def test_end_marker_set_reject_and_clear(self):
        engine = _make_bare_engine()
        deck = engine._active
        deck.filepath = "fake.wav"
        deck.duration = 10.0

        engine.set_end_marker(0)  # rejected: not > 0
        self.assertIsNone(engine.end_marker)

        engine.set_end_marker(10.0)  # rejected: not < duration
        self.assertIsNone(engine.end_marker)

        engine.set_end_marker(5.0)
        self.assertEqual(engine.end_marker, 5.0)
        self.assertEqual(
            self.fake_settings.get("track_end_markers", {})["fake.wav"], 5.0
        )

        engine.clear_end_marker()
        self.assertIsNone(engine.end_marker)
        self.assertNotIn("fake.wav", self.fake_settings.get("track_end_markers", {}))

    def test_start_marker_cannot_be_set_at_or_after_end_marker(self):
        """dp-232: `start < end` is an invariant, enforced from both sides.

        A start marker at or after the Fin marker would mean a track that
        begins after it is supposed to stop. Rejection is silent -- the call
        is a no-op and leaves any previous value untouched."""
        engine = _make_bare_engine()
        deck = engine._active
        deck.filepath = "fake.wav"
        deck.duration = 10.0

        engine.set_end_marker(6.0)
        self.assertEqual(engine.end_marker, 6.0)

        engine.set_start_marker(7.0)   # after the end marker
        self.assertIsNone(engine.start_marker)
        engine.set_start_marker(6.0)   # exactly on it
        self.assertIsNone(engine.start_marker)
        self.assertNotIn(
            "fake.wav", self.fake_settings.get("track_start_markers", {})
        )

        engine.set_start_marker(2.0)   # before it -- accepted
        self.assertEqual(engine.start_marker, 2.0)

        engine.set_start_marker(8.0)   # rejected, must not disturb the valid one
        self.assertEqual(engine.start_marker, 2.0)

    def test_end_marker_cannot_be_set_at_or_before_start_marker(self):
        """dp-232: the same invariant enforced from the other direction."""
        engine = _make_bare_engine()
        deck = engine._active
        deck.filepath = "fake.wav"
        deck.duration = 10.0

        engine.set_start_marker(4.0)
        self.assertEqual(engine.start_marker, 4.0)

        engine.set_end_marker(3.0)   # before the start marker
        self.assertIsNone(engine.end_marker)
        engine.set_end_marker(4.0)   # exactly on it
        self.assertIsNone(engine.end_marker)

        engine.set_end_marker(8.0)   # after it -- accepted
        self.assertEqual(engine.end_marker, 8.0)

    def test_marker_order_rule_only_applies_when_the_other_marker_exists(self):
        """Discrimination check: the guard must key off the OTHER marker.

        With no Fin marker any start position is legal, and vice versa. If
        this fails, the guard is rejecting on something other than the pair."""
        engine = _make_bare_engine()
        deck = engine._active
        deck.filepath = "fake.wav"
        deck.duration = 10.0

        engine.set_start_marker(9.0)
        self.assertEqual(engine.start_marker, 9.0)

        engine.clear_start_marker()
        engine.set_end_marker(0.5)
        self.assertEqual(engine.end_marker, 0.5)

    def test_start_marker_set_persist_and_clear(self):
        """dp-232: mirrors test_end_marker_set_reject_and_clear structurally
        -- same reject/accept/persist/clear shape as the end marker."""
        engine = _make_bare_engine()
        deck = engine._active
        deck.filepath = "fake.wav"
        deck.duration = 10.0

        engine.set_start_marker(10.0)  # rejected: not < duration
        self.assertIsNone(engine.start_marker)

        engine.set_start_marker(-1.0)  # rejected: not >= 0
        self.assertIsNone(engine.start_marker)

        engine.set_start_marker(3.0)
        self.assertEqual(engine.start_marker, 3.0)
        self.assertEqual(
            self.fake_settings.get("track_start_markers", {})["fake.wav"], 3.0
        )

        engine.clear_start_marker()
        self.assertIsNone(engine.start_marker)
        self.assertNotIn(
            "fake.wav", self.fake_settings.get("track_start_markers", {})
        )

    def test_start_marker_persists_across_reload_and_clear_is_per_track(self):
        """dp-232 acceptance criterion: Clear affects ONLY the current
        track's start marker, not any other track's, and a re-`load()` of a
        filepath with a saved marker restores it (and seeds read_idx)."""
        engine = _make_bare_engine()
        engine._active.filepath = "track_a.wav"
        engine._active.duration = 10.0
        engine.set_start_marker(2.0)

        engine._active.filepath = "track_b.wav"
        engine._active.duration = 10.0
        engine.set_start_marker(4.0)

        self.assertEqual(
            self.fake_settings.get("track_start_markers", {})["track_a.wav"], 2.0
        )
        self.assertEqual(
            self.fake_settings.get("track_start_markers", {})["track_b.wav"], 4.0
        )

        # Clearing track_b's marker (the current active deck) must not touch
        # track_a's saved entry.
        engine.clear_start_marker()
        d = self.fake_settings.get("track_start_markers", {})
        self.assertNotIn("track_b.wav", d)
        self.assertEqual(d["track_a.wav"], 2.0)

    def test_load_seeds_read_idx_from_saved_start_marker(self):
        """dp-232: `load()` seeds the active deck's `read_idx` from a saved
        start marker right after `Deck.load()` resets it to 0 -- this is
        what makes manual play begin at the marker without any `_callback`
        change, since `play(from_position=None)` starts wherever `read_idx`
        already is."""
        engine = _make_bare_engine()
        self.fake_settings.set(
            "track_start_markers", {"dp232_seed.wav": 0.5}
        )
        engine.load("dp232_seed.wav")
        self.assertEqual(engine._active.start_marker, 0.5)
        self.assertEqual(
            engine._active.read_idx, int(0.5 * engine.sample_rate)
        )

    def test_master_and_track_volume_clamp_and_persist(self):
        engine = _make_bare_engine()
        engine._active.filepath = "fake.wav"

        engine.set_master_volume(150)
        self.assertEqual(engine.master_volume, 100)
        self.assertEqual(self.fake_settings.get("master_volume"), 150)

        engine.set_master_volume(-10)
        self.assertEqual(engine.master_volume, 0)

        engine.set_track_volume(120)  # active file, no fp arg
        self.assertEqual(engine.track_volume, 100)
        self.assertEqual(
            self.fake_settings.get("track_volumes", {})["fake.wav"], 120
        )

        engine.set_track_volume(50, "other.wav")
        self.assertEqual(
            self.fake_settings.get("track_volumes", {})["other.wav"], 50
        )
        # UI review 2026-08-01: this assertion used to read
        #   self.assertEqual(engine.track_volume, 50)
        # under the comment "set_track_volume always mutates the ACTIVE
        # deck's track_volume, regardless of which fp the persisted entry is
        # written under." That documented a DEFECT, not a requirement:
        # setting a queued (or arbitrary) track's volume silently changed the
        # volume of whatever was actually playing, while the playing track's
        # own persisted value stayed untouched -- so the change also
        # evaporated on reload. The gain now lands only on decks that
        # actually hold that file.
        self.assertEqual(engine.track_volume, 100)  # "fake.wav" deck, untouched

        engine2 = _make_bare_engine()
        engine2._active.filepath = None
        before = engine2._active.track_volume
        engine2.set_track_volume(77)  # no active file -> safe no-op
        self.assertEqual(engine2._active.track_volume, before)

    def test_seek_clamps_and_enqueues_cancel_then_seek(self):
        engine = _make_bare_engine()
        deck = engine._active
        deck.filepath = "fake.wav"
        deck.duration = 10.0

        engine.seek(-5.0)  # clamps to 0
        cmds = list(engine._command_queue)
        self.assertEqual(cmds[-2], ("cancel_crossfade",))
        self.assertEqual(cmds[-1], ("seek", 0))

        engine._command_queue.clear()
        engine.seek(999.0)  # clamps to duration
        cmds = list(engine._command_queue)
        self.assertEqual(cmds[-1], ("seek", int(10.0 * engine.sample_rate)))

        engine._command_queue.clear()
        engine.seek_percent(0.5)
        cmds = list(engine._command_queue)
        self.assertEqual(cmds[-1], ("seek", int(5.0 * engine.sample_rate)))

        engine._command_queue.clear()
        deck.filepath = None
        engine.seek(3.0)  # no active file -> no-op, queue stays empty
        self.assertEqual(len(engine._command_queue), 0)

    def test_state_transitions_pause_resume_stop(self):
        engine = _make_bare_engine()
        deck = engine._active
        deck.filepath = "fake.wav"
        deck.duration = 10.0
        deck.active = True
        engine._state = deck_engine_module.STATE_PLAYING

        engine.pause()
        self.assertEqual(engine.state, deck_engine_module.STATE_PAUSED)
        self.assertFalse(deck.active)

        engine.resume()
        self.assertEqual(engine.state, deck_engine_module.STATE_PLAYING)
        self.assertTrue(deck.active)

        deck.read_idx = int(3.0 * engine.sample_rate)
        engine.stop()
        self.assertEqual(engine.state, deck_engine_module.STATE_STOPPED)
        self.assertEqual(deck.read_idx, 0)

        self.assertEqual(engine.position, 0.0)
        self.assertEqual(engine.duration, 10.0)
        self.assertEqual(engine.current_file, "fake.wav")

    def test_prefetch_is_noop(self):
        engine = _make_bare_engine()
        state_before = engine._state
        active_before = engine._active
        idle_before = engine._idle
        qlen_before = len(engine._command_queue)

        engine.prefetch("anything.wav")  # dp-178 latency mitigation, moot now

        self.assertEqual(engine._state, state_before)
        self.assertIs(engine._active, active_before)
        self.assertIs(engine._idle, idle_before)
        self.assertEqual(len(engine._command_queue), qlen_before)

    def test_clear_all_cues(self):
        engine = _make_bare_engine()
        engine._active.cue_points = {0: 1.0, 1: 2.0, 7: 9.0}

        engine.clear_all_cues()

        self.assertEqual(engine.cue_points, {})

    # -- dp-245 D1: crash-survival persistence for authoring actions -------

    def test_set_cue_persists_immediately(self):
        """set_cue must call settings.save() so the value survives a crash,
        not just a clean MainWindow.closeEvent."""
        engine = _make_bare_engine()
        engine._active.filepath = "fake.wav"

        engine.set_cue(0, 4.0)

        self.assertEqual(getattr(self.fake_settings, "save_calls", 0), 1)

    def test_set_end_marker_persists_immediately(self):
        engine = _make_bare_engine()
        deck = engine._active
        deck.filepath = "fake.wav"
        deck.duration = 10.0

        engine.set_end_marker(5.0)

        self.assertEqual(getattr(self.fake_settings, "save_calls", 0), 1)

    def test_clear_end_marker_persists_immediately(self):
        engine = _make_bare_engine()
        deck = engine._active
        deck.filepath = "fake.wav"
        deck.duration = 10.0
        engine.set_end_marker(5.0)

        engine.clear_end_marker()

        self.assertEqual(getattr(self.fake_settings, "save_calls", 0), 2)

    def test_set_start_marker_persists_immediately(self):
        engine = _make_bare_engine()
        deck = engine._active
        deck.filepath = "fake.wav"
        deck.duration = 10.0

        engine.set_start_marker(2.0)

        self.assertEqual(getattr(self.fake_settings, "save_calls", 0), 1)

    def test_clear_start_marker_persists_immediately(self):
        engine = _make_bare_engine()
        deck = engine._active
        deck.filepath = "fake.wav"
        deck.duration = 10.0
        engine.set_start_marker(2.0)

        engine.clear_start_marker()

        self.assertEqual(getattr(self.fake_settings, "save_calls", 0), 2)

    def test_set_master_volume_does_not_persist_immediately(self):
        """Regression guard: master volume is ArtNet-fader-driven at up to
        one call per DMX frame -- must NEVER trigger settings.save() (that
        would fsync the whole settings file dozens of times a second). Only
        MainWindow.closeEvent (or an explicit save) may persist it."""
        engine = _make_bare_engine()

        engine.set_master_volume(42)

        self.assertEqual(getattr(self.fake_settings, "save_calls", 0), 0)
        self.assertEqual(self.fake_settings.get("master_volume"), 42)


class TestPollThreadDispatch(unittest.TestCase):
    """dp-216 Phase 4: DeckEngine._poll_position dispatch, exercised on the
    real poll thread against a bare engine (no live audio stream)."""

    def _run_poll_briefly(self, engine, until, timeout=3.0):
        engine._poll_running = True
        thread = threading.Thread(target=engine._poll_position, daemon=True)
        thread.start()
        try:
            result = _wait_for(until, timeout=timeout)
        finally:
            engine._poll_running = False
            thread.join(timeout=1.0)
        return result

    def test_poll_swap_dispatch_fires_track_changed_and_retires_idle(self):
        engine = _make_bare_engine()
        spent_idle = _make_deck()
        engine._idle = spent_idle
        engine._pending_active_fp = "new.wav"
        engine._swap_pending = True
        engine._active.filepath = "new.wav"
        engine._active.active = True
        engine._state = deck_engine_module.STATE_PLAYING

        changed = []
        ended = []
        engine.on_track_changed = lambda fp: changed.append(fp)
        engine.on_track_end = lambda: ended.append(True)

        ok = self._run_poll_briefly(engine, lambda: len(changed) > 0)

        self.assertTrue(ok)
        self.assertEqual(changed, ["new.wav"])
        self.assertEqual(ended, [])  # a swap is a handoff, never on_track_end
        self.assertEqual(spent_idle._buf.shape[0], 0)  # retired: buffer detached

    def test_poll_unarmed_end_fires_track_end_and_resets_position(self):
        engine = _make_bare_engine()
        engine._active.just_ended = True
        engine._active.read_idx = 12345
        engine._state = deck_engine_module.STATE_PLAYING
        engine._swap_pending = False

        ended = []
        engine.on_track_end = lambda: ended.append(True)
        engine.on_track_changed = lambda fp: self.fail("unexpected on_track_changed")

        ok = self._run_poll_briefly(engine, lambda: len(ended) > 0)

        self.assertTrue(ok)
        self.assertEqual(ended, [True])
        self.assertEqual(engine._active.read_idx, 0)
        self.assertEqual(engine._state, deck_engine_module.STATE_STOPPED)

    def test_poll_on_position_gated_by_stopped_state(self):
        engine = _make_bare_engine()
        engine._state = deck_engine_module.STATE_STOPPED
        engine._active.active = False

        positions = []
        engine.on_position = lambda p: positions.append(p)

        engine._poll_running = True
        thread = threading.Thread(target=engine._poll_position, daemon=True)
        thread.start()
        try:
            time.sleep(0.35)  # several poll ticks while STOPPED (dp-202)
        finally:
            engine._poll_running = False
            thread.join(timeout=1.0)
        self.assertEqual(positions, [])

    def test_poll_on_position_emitted_while_playing(self):
        engine = _make_bare_engine()
        engine._active.filepath = "fake.wav"
        engine._active.active = True
        engine._state = deck_engine_module.STATE_PLAYING

        positions = []
        engine.on_position = lambda p: positions.append(p)

        ok = self._run_poll_briefly(engine, lambda: len(positions) > 0)

        self.assertTrue(ok)
        self.assertGreater(len(positions), 0)

    def test_poll_pending_stop_finalizes_via_stop_internal(self):
        engine = _make_bare_engine()
        engine._active.pending_stop = True
        engine._active.active = True
        engine._active.read_idx = 5000
        engine._state = deck_engine_module.STATE_PLAYING

        ok = self._run_poll_briefly(
            engine, lambda: engine._state == deck_engine_module.STATE_STOPPED
        )

        self.assertTrue(ok)
        self.assertFalse(engine._active.pending_stop)
        self.assertFalse(engine._active.active)
        self.assertEqual(engine._active.read_idx, 0)


class TestFrozenBuildSubprocessFlags(unittest.TestCase):
    """dp-216 Phase 6 regression net for the live-pass root cause.

    Every ffmpeg/ffprobe launch must pass `stdin=DEVNULL` and
    CREATE_NO_WINDOW. Without the former, a windowed PyInstaller build hands
    ffmpeg an invalid inherited stdin handle, it blocks and emits zero PCM,
    and playback is silent with a frozen position marker. Without the latter,
    each launch flashes a console window. Neither reproduces from source,
    which is exactly why these need to be asserted rather than observed.
    """

    def test_decode_worker_passes_stdin_devnull_and_no_window(self):
        deck = Deck(48000)
        captured = {}

        class _FakeProc:
            stdout = mock.Mock(**{"read.return_value": b"", "close.return_value": None})

            def wait(self, timeout=None):
                return 0

        def _fake_popen(cmd, **kwargs):
            captured["cmd"] = cmd
            captured["kwargs"] = kwargs
            return _FakeProc()

        with mock.patch.object(deck_engine_module.subprocess, "Popen", _fake_popen):
            deck._decode_worker("nonexistent.wav")

        self.assertEqual(captured["kwargs"]["stdin"], subprocess.DEVNULL)
        self.assertEqual(
            captured["kwargs"]["creationflags"], subproc_module.CREATE_NO_WINDOW
        )
        self.assertIn("-nostdin", captured["cmd"])

    def test_failed_decode_marks_complete_so_deck_does_not_hang(self):
        """A decode that yields no audio must still resolve an effective end
        frame. Leaving decode_complete False left the deck active-but-silent
        with no end, so nothing ever advanced past it."""
        deck = Deck(48000)
        with mock.patch.object(
            deck_engine_module.subprocess, "Popen", side_effect=OSError("boom")
        ):
            deck._decode_worker("nonexistent.wav")
        self.assertTrue(deck.decode_complete)
        self.assertEqual(deck._effective_end_frame(), 0)

    def test_reap_kills_a_process_that_will_not_exit(self):
        """A `wait()` timeout used to propagate, skipping decode_complete AND
        orphaning one ffmpeg per load."""
        proc = mock.Mock()
        proc.wait.side_effect = [subprocess.TimeoutExpired("ffmpeg", 2.0), 0]
        Deck._reap(proc)
        proc.kill.assert_called_once()


class TestPreloadReusesInFlightDecode(unittest.TestCase):
    """dp-216 Phase 6: a repeated preload() for a file whose decode is still
    running must not tear that decode down. `_rearm_preload` fires on every
    playlist move, and the teardown path joins the decode worker (up to 1s)
    on the Qt main thread, then restarts ffmpeg -- which is what made
    drag-reordering the playlist stall."""

    def _engine_with_decoding_idle(self, filepath="track.wav"):
        engine = _make_bare_engine()
        idle = engine._idle
        idle.filepath = filepath
        idle.just_ended = False
        idle._decode_thread = mock.Mock(**{"is_alive.return_value": True})
        return engine, idle

    def test_repeat_preload_of_in_flight_file_does_not_reload(self):
        engine, idle = self._engine_with_decoding_idle()
        with mock.patch.object(Deck, "load") as load, \
                mock.patch.object(engine, "_retire") as retire, \
                mock.patch.object(engine, "_read_duration") as read_duration, \
                mock.patch.object(deck_engine_module.threading, "Thread"):
            engine.preload("track.wav")
        load.assert_not_called()
        retire.assert_not_called()
        read_duration.assert_not_called()  # no ffprobe subprocess either

    def test_repeat_preload_still_spawns_a_fresh_arm_thread(self):
        """The reuse path must NOT short-circuit arming: a preceding
        invalidate_preload() bumped the generation and killed the old arm
        thread, so returning early would leave a decoded-but-never-armed
        deck and silently lose the gapless swap."""
        engine, idle = self._engine_with_decoding_idle()
        with mock.patch.object(deck_engine_module.threading, "Thread") as thread:
            engine.preload("track.wav")
        thread.assert_called_once()
        self.assertIs(thread.call_args.kwargs["args"][0], idle)
        self.assertEqual(thread.call_args.kwargs["args"][2], engine._preload_gen)

    def test_spent_idle_deck_with_same_filepath_still_rebuilds(self):
        """Correctness rule #4 is unchanged: a deck that has ENDED is spent
        even if its filepath matches, and must be rebuilt from scratch."""
        engine, idle = self._engine_with_decoding_idle()
        idle.just_ended = True
        with mock.patch.object(Deck, "load") as load, \
                mock.patch.object(engine, "_read_duration", return_value=1.0), \
                mock.patch.object(engine, "_arm_when_ready") as arm, \
                mock.patch.object(deck_engine_module.threading, "Thread") as thread:
            engine.preload("track.wav")
            # dp-fix: the non-reuse path now runs retire+load in
            # `_preload_worker` on a background thread (was inline on the
            # caller's thread) -- invoke the spawned target directly rather
            # than letting a real thread run, mirroring the reuse-path
            # thread-args assertions above.
            thread.assert_called_once()
            target = thread.call_args.kwargs["target"]
            args = thread.call_args.kwargs["args"]
            target(*args)
        load.assert_called_once()
        arm.assert_called_once()

    def test_preload_of_a_different_file_still_rebuilds(self):
        engine, idle = self._engine_with_decoding_idle()
        with mock.patch.object(Deck, "load") as load, \
                mock.patch.object(engine, "_read_duration", return_value=1.0), \
                mock.patch.object(engine, "_arm_when_ready") as arm, \
                mock.patch.object(deck_engine_module.threading, "Thread") as thread:
            engine.preload("other.wav")
            thread.assert_called_once()
            target = thread.call_args.kwargs["target"]
            args = thread.call_args.kwargs["args"]
            target(*args)
        load.assert_called_once()
        arm.assert_called_once()


class TestOutputDeviceSelection(unittest.TestCase):
    """dp-216 D2/R7 was documented but never implemented: the engine took
    PortAudio's default, which on Windows is the MME host API's default --
    frequently an HDMI display rather than the Windows default playback
    device, at ~90ms latency. Selection must prefer WASAPI."""

    _HOSTAPIS = (
        {"name": "MME", "default_output_device": 4},
        {"name": "Windows DirectSound", "default_output_device": 9},
        {"name": "Windows WASAPI", "default_output_device": 13},
    )
    _DEVICES = {
        4: {"index": 4, "max_output_channels": 6, "default_samplerate": 44100.0},
        9: {"index": 9, "max_output_channels": 2, "default_samplerate": 44100.0},
        13: {"index": 13, "max_output_channels": 2, "default_samplerate": 48000.0},
    }

    def _candidates(self, hostapis=None, devices=None):
        devices = self._DEVICES if devices is None else devices

        def _query(arg=None, kind=None):
            if kind == "output":
                return devices[4]
            return devices[arg]

        with mock.patch.object(
            deck_engine_module.sd, "query_hostapis",
            return_value=self._HOSTAPIS if hostapis is None else hostapis,
        ), mock.patch.object(deck_engine_module.sd, "query_devices", _query):
            return deck_engine_module._output_device_candidates()

    def test_wasapi_is_preferred_with_its_own_rate(self):
        candidates = self._candidates()
        self.assertEqual(candidates[0], (13, 48000))

    def test_falls_back_through_directsound_then_mme(self):
        self.assertEqual(
            [device for device, _ in self._candidates()][:3], [13, 9, 4]
        )

    def test_always_ends_with_a_portaudio_default_fallback(self):
        """A machine with no host API at all must still yield a candidate, so
        the engine constructs instead of raising at import time."""
        self.assertEqual(self._candidates(hostapis=())[-1], (None, 44100))

    def test_mono_only_devices_are_skipped(self):
        devices = dict(self._DEVICES)
        devices[13] = dict(devices[13], max_output_channels=1)
        self.assertNotIn(13, [device for device, _ in self._candidates(devices=devices)])


if __name__ == "__main__":
    unittest.main()

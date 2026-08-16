"""dp-219: headless tests for the live-crossfade gain-schedule scrub command.

`seek_crossfade_gain()` / the `"seek_crossfade_gain"` queued command / the
`crossfade_progress` accessor -- exercised the same way as the rest of
Phase 3's crossfade control-path tests in `test_deck_engine.py`: real
`DeckEngine._drain_commands`/`_callback` against `_FakeEngine`, no real audio
stream or ffmpeg decode needed (R3).

Plain unittest, no pytest dependency:
    ./venv/Scripts/python.exe -m unittest discover tests
"""

import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from core.deck_engine import DeckEngine  # noqa: E402
from test_deck_engine import (  # noqa: E402
    _FakeEngine,
    _LinearOverlap,
    _make_bare_engine,
    _make_deck,
)


class TestSeekCrossfadeGainNoOp(unittest.TestCase):
    """A9-style guard: a stray seek command outside an active crossfade must
    not arm or corrupt state -- checked both at the call site
    (`seek_crossfade_gain`) and again at drain time (`_drain_commands`),
    since `_crossfade_running` can flip between the two."""

    def test_noop_when_not_running_does_not_queue(self):
        engine = _make_bare_engine()
        engine._crossfade_running = False

        engine.seek_crossfade_gain(500)

        self.assertEqual(len(engine._command_queue), 0)

    def test_drain_ignores_stray_command_when_not_running(self):
        # Simulates the race: queued while running, but running flips False
        # (e.g. cancel_crossfade lands first in the same drain) before the
        # seek command is consumed.
        active = _make_deck(sample_rate=48000, seconds=1.0)
        fake = _FakeEngine(active)
        fake._crossfade_running = False
        fake._crossfade_len = 1000
        fake._crossfade_elapsed = 200
        fake._command_queue.append(("seek_crossfade_gain", 900))

        DeckEngine._drain_commands(fake)

        self.assertEqual(fake._crossfade_elapsed, 200)  # untouched


class TestSeekCrossfadeGainClampingAndDirection(unittest.TestCase):
    """Bidirectional set + clamping to [0, _crossfade_len]."""

    def _running_fake(self, crossfade_len=1000, elapsed=400):
        active = _make_deck(sample_rate=48000, seconds=1.0)
        fake = _FakeEngine(active)
        fake._crossfade_running = True
        fake._crossfade_len = crossfade_len
        fake._crossfade_overlap = _LinearOverlap(crossfade_len / 48000.0)
        fake._crossfade_elapsed = elapsed
        return fake

    def test_seek_forward(self):
        fake = self._running_fake()
        fake._command_queue.append(("seek_crossfade_gain", 900))
        DeckEngine._drain_commands(fake)
        self.assertEqual(fake._crossfade_elapsed, 900)

    def test_seek_backward(self):
        fake = self._running_fake()
        fake._command_queue.append(("seek_crossfade_gain", 100))
        DeckEngine._drain_commands(fake)
        self.assertEqual(fake._crossfade_elapsed, 100)

    def test_clamped_below_zero(self):
        fake = self._running_fake()
        fake._command_queue.append(("seek_crossfade_gain", -500))
        DeckEngine._drain_commands(fake)
        self.assertEqual(fake._crossfade_elapsed, 0)

    def test_clamped_above_crossfade_len(self):
        fake = self._running_fake(crossfade_len=1000)
        fake._command_queue.append(("seek_crossfade_gain", 5000))
        DeckEngine._drain_commands(fake)
        self.assertEqual(fake._crossfade_elapsed, 1000)

    def test_engine_method_queues_command_while_running(self):
        engine = _make_bare_engine()
        engine._crossfade_running = True
        engine._crossfade_len = 1000

        engine.seek_crossfade_gain(750)

        self.assertEqual(
            list(engine._command_queue), [("seek_crossfade_gain", 750)]
        )


class TestSeekCrossfadeGainTriggersExistingFinalize(unittest.TestCase):
    """Setting elapsed to _crossfade_len must trigger the callback's
    existing finalize branch (deck swap, gains reset to 1.0) on the very
    next block -- no new finalize logic, per the ticket's chosen scrub
    model (gain-schedule warp only)."""

    def test_seek_to_end_finalizes_on_next_block(self):
        overlap_len = 1000
        active = _make_deck(sample_rate=48000, seconds=2.0, amplitude=16000)
        active.read_idx = 0  # plenty of real audio left -- not an early-end case

        idle = _make_deck(sample_rate=48000, seconds=2.0, amplitude=8000)
        idle.filepath = "idleTrack.wav"
        idle.active = True  # already marked active by the (simulated) trigger

        fake = _FakeEngine(active, idle=idle)
        fake._crossfade_running = True
        fake._crossfade_len = overlap_len
        fake._crossfade_overlap = _LinearOverlap(overlap_len / 48000.0)
        fake._crossfade_elapsed = 100  # nowhere near the end yet

        fake._command_queue.append(("seek_crossfade_gain", overlap_len))

        outdata = np.zeros((32, 2), dtype=np.float32)
        DeckEngine._callback(fake, outdata, 32, None, None)

        self.assertFalse(fake._crossfade_running)
        self.assertEqual(fake._crossfade_len, 0)
        self.assertIsNone(fake._crossfade_overlap)
        self.assertIs(fake._active, idle)   # refs swapped -- Track B took over
        self.assertIs(fake._idle, active)
        self.assertEqual(active.gain, 1.0)
        self.assertEqual(idle.gain, 1.0)
        self.assertTrue(fake._swap_pending)
        self.assertEqual(fake._pending_active_fp, "idleTrack.wav")


class TestCrossfadeProgressAccessor(unittest.TestCase):
    """Read-only (running, t, crossfade_len_frames) snapshot the poll thread
    reports via on_crossfade_progress."""

    def test_progress_while_running(self):
        engine = _make_bare_engine()
        engine._crossfade_running = True
        engine._crossfade_len = 1000
        engine._crossfade_elapsed = 250

        running, t, length = engine.crossfade_progress

        self.assertTrue(running)
        self.assertAlmostEqual(t, 0.25)
        self.assertEqual(length, 1000)

    def test_progress_when_idle_reports_zero_t_no_divide_by_zero(self):
        engine = _make_bare_engine()
        engine._crossfade_running = False
        engine._crossfade_len = 0
        engine._crossfade_elapsed = 0

        running, t, length = engine.crossfade_progress

        self.assertFalse(running)
        self.assertEqual(t, 0.0)
        self.assertEqual(length, 0)

    def test_accessor_does_not_take_the_engine_lock(self):
        """Review finding D6/D1: this is read from the Qt thread on every
        scrub drag event. Taking `self._lock` would buy no atomicity (the
        audio callback mutates these fields lock-free) while exposing a
        per-drag UI call to blocking behind a `_preload_worker` that holds
        the lock across a decode teardown. Acquiring the lock first here
        would deadlock the old implementation."""
        engine = _make_bare_engine()
        engine._crossfade_running = True
        engine._crossfade_len = 800
        engine._crossfade_elapsed = 400

        engine._lock.acquire()
        try:
            running, t, length = engine.crossfade_progress
        finally:
            engine._lock.release()

        self.assertTrue(running)
        self.assertAlmostEqual(t, 0.5)
        self.assertEqual(length, 800)


class TestCrossfadeProgressCallbackIsChangeGated(unittest.TestCase):
    """Review finding D3: the callback used to fire unconditionally on every
    10 Hz poll tick, pushing a Qt signal and a slider repaint ten times a
    second for the life of the app whether or not a crossfade had ever been
    configured. Every other engine->UI callback in `_poll_position` is
    change-gated (`buffering_changed`) or state-gated; this one must be too."""

    def _tick(self, engine, seen):
        # The exact emit shape `_poll_position` uses, extracted so the test
        # needs no real poll thread (which sleeps 0.1s per tick).
        cf_running = engine._crossfade_running
        cf_len = engine._crossfade_len
        cf_elapsed = engine._crossfade_elapsed
        cb = engine.on_crossfade_progress
        if cb and (cf_running or engine._last_crossfade_running):
            cb(cf_running, (cf_elapsed / cf_len) if cf_len > 0 else 0.0, cf_len)
        engine._last_crossfade_running = cf_running

    def test_silent_while_no_crossfade_ever_runs(self):
        engine = _make_bare_engine()
        seen = []
        engine.on_crossfade_progress = lambda *args: seen.append(args)
        engine._crossfade_running = False

        for _ in range(20):
            self._tick(engine, seen)

        self.assertEqual(seen, [])

    def test_fires_while_running_then_exactly_once_more_on_stop(self):
        engine = _make_bare_engine()
        seen = []
        engine.on_crossfade_progress = lambda *args: seen.append(args)

        engine._crossfade_running = True
        engine._crossfade_len = 100
        engine._crossfade_elapsed = 50
        for _ in range(3):
            self._tick(engine, seen)
        self.assertEqual(len(seen), 3)

        # Finalize: the slider still needs ONE more event to go inert.
        engine._crossfade_running = False
        engine._crossfade_len = 0
        engine._crossfade_elapsed = 0
        for _ in range(5):
            self._tick(engine, seen)

        self.assertEqual(len(seen), 4)
        self.assertEqual(seen[-1], (False, 0.0, 0))


if __name__ == "__main__":
    unittest.main()

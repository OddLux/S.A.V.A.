"""dp-220: no control-path call may block the caller on a decode teardown.

The dp-218 fix moved `preload()`'s retire+load off the Qt main thread but left
it holding the engine-wide `self._lock` across `_retire` (which joins a decode
thread, up to 1s) and `Deck.load` (which zeroes ~150MB for a long track). Every
control call the UI makes -- play, seek, pause, volume, cue, and
`invalidate_preload` -- contends on that same lock, so the join was off the
main thread but the BLOCKING was not. That is the residual rapid-Next freeze.

These tests assert the LATENCY CONTRACT, not the implementation: with a slow
decode teardown in flight, a control-path call must return promptly. They fail
against the pre-dp-220 code (verified: `invalidate_preload` took ~0.5s per call
against a deck with a slow decode thread, and `set_master_volume` blocked
behind `_preload_worker`).

    QT_QPA_PLATFORM=offscreen ./venv/Scripts/python.exe -m unittest discover tests
"""

import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_deck_engine import _make_bare_engine, _make_deck  # noqa: E402

# Generous vs. the ~1s join it is catching, tight enough that a real join
# cannot slip under it. Scheduler noise on a loaded Windows box is tens of ms,
# not hundreds.
STALL_BUDGET = 0.25


class _SlowDeck:
    """Stands in for a deck whose decode worker will not die promptly --
    exactly what `_retire` -> `stop_decode` -> `thread.join(timeout=1.0)`
    waits on. `stop_decode` sleeps to model that join."""

    def __init__(self, join_seconds=0.6):
        self.filepath = "slow.wav"
        self.just_ended = False
        self._decode_thread = None
        self._join_seconds = join_seconds
        self.stop_calls = 0

    def stop_decode(self):
        self.stop_calls += 1
        time.sleep(self._join_seconds)

    def detach_buffer(self):
        return None, None


class TestControlPathNeverBlocksOnDecodeTeardown(unittest.TestCase):

    def setUp(self):
        self.engine = _make_bare_engine()
        self.engine._crossfade_running = False

    def test_invalidate_preload_returns_immediately(self):
        """F2: `invalidate_preload` retired the idle deck synchronously under
        `self._lock`. MainWindow._rearm_preload() calls it on the Qt main
        thread on three separate branches (no current file, end-action
        loop/stop, end of playlist), so a rapid-Next burst froze the UI."""
        self.engine._idle = _SlowDeck()

        start = time.perf_counter()
        self.engine.invalidate_preload()
        elapsed = time.perf_counter() - start

        self.assertLess(
            elapsed,
            STALL_BUDGET,
            f"invalidate_preload blocked the caller for {elapsed:.3f}s",
        )
        self.assertFalse(self.engine._idle_armed)

    def test_rapid_invalidate_burst_never_stalls_a_single_call(self):
        """The actual stress shape: repeated manual Next in quick succession."""
        worst = 0.0
        for _ in range(8):
            self.engine._idle = _SlowDeck()
            start = time.perf_counter()
            self.engine.invalidate_preload()
            worst = max(worst, time.perf_counter() - start)

        self.assertLess(worst, STALL_BUDGET, f"worst call blocked {worst:.3f}s")

    def test_control_calls_do_not_queue_behind_a_preload_worker(self):
        """F1: `_preload_worker` held `self._lock` across `_retire` and
        `Deck.load`. Anything the UI does while that runs -- here
        `set_master_volume`, which is just two field writes -- had to wait it
        out. The teardown work now sits under `_retire_lock`, which the
        control path never touches.

        Drives the REAL `_preload_worker`, not a stand-in that grabs
        `_retire_lock` directly: a test that acquires the new lock itself
        would pass against the old code too (the old code simply never took
        that lock), proving nothing.
        """
        doomed = _SlowDeck(join_seconds=0.8)
        self.engine._idle = doomed
        self.engine._preload_gen = 7

        worker = threading.Thread(
            target=self.engine._preload_worker,
            args=(doomed, "slow.wav", 7),
            daemon=True,
        )
        worker.start()
        # Let the worker get into its teardown section.
        time.sleep(0.05)

        start = time.perf_counter()
        self.engine.set_master_volume(55)
        elapsed = time.perf_counter() - start

        self.assertLess(
            elapsed,
            STALL_BUDGET,
            f"set_master_volume blocked {elapsed:.3f}s behind _preload_worker",
        )
        self.assertEqual(self.engine.master_volume, 55)

    def test_retire_lock_is_held_by_the_preload_worker(self):
        """Guards the mechanism the latency tests depend on: the worker's slow
        section must genuinely run under `_retire_lock`. Without this, someone
        could satisfy the timing tests by removing locking altogether."""
        doomed = _SlowDeck(join_seconds=0.5)
        self.engine._idle = doomed
        self.engine._preload_gen = 3

        worker = threading.Thread(
            target=self.engine._preload_worker,
            args=(doomed, "slow.wav", 3),
            daemon=True,
        )
        worker.start()
        time.sleep(0.05)

        self.assertFalse(
            self.engine._retire_lock.acquire(blocking=False),
            "_preload_worker's teardown section did not hold _retire_lock",
        )

    def test_retire_lock_is_not_the_engine_lock(self):
        """The whole point: they must be distinct objects, and the control
        path must never take the teardown one. If someone later collapses
        them back into one lock, every test above silently stops testing
        anything."""
        self.assertIsNot(self.engine._retire_lock, self.engine._lock)

        acquired = self.engine._retire_lock.acquire(blocking=False)
        self.assertTrue(acquired)
        try:
            # Both must still be reachable by the control path.
            self.engine.set_master_volume(70)
            self.assertEqual(self.engine.master_volume, 70)
        finally:
            self.engine._retire_lock.release()


class TestAsyncRetireStillTearsDown(unittest.TestCase):
    """Latency must not be bought by silently skipping the teardown."""

    def test_retire_async_actually_stops_the_decode(self):
        engine = _make_bare_engine()
        doomed = _SlowDeck(join_seconds=0.05)

        engine._retire_async(doomed)

        deadline = time.time() + 2.0
        while doomed.stop_calls == 0 and time.time() < deadline:
            time.sleep(0.01)
        self.assertEqual(doomed.stop_calls, 1)

    def test_invalidate_preload_eventually_retires_the_idle_deck(self):
        engine = _make_bare_engine()
        engine._crossfade_running = False
        doomed = _SlowDeck(join_seconds=0.05)
        engine._idle = doomed

        engine.invalidate_preload()

        deadline = time.time() + 2.0
        while doomed.stop_calls == 0 and time.time() < deadline:
            time.sleep(0.01)
        self.assertEqual(doomed.stop_calls, 1)


class TestLoadKeepsActiveDeckTeardownOrdered(unittest.TestCase):

    def test_active_deck_retire_stays_synchronous(self):
        """Deliberate asymmetry (see `load`'s docstring): the ACTIVE deck's
        retire must stay synchronous, because `self._active.load()` re-enters
        `stop_decode()` on that same deck moments later and two concurrent
        teardowns on one deck would race. Only the idle deck's retire is
        backgrounded. This test exists so that asymmetry is not "tidied up"
        into a bug later."""
        engine = _make_bare_engine()
        active = _make_deck(sample_rate=48000, seconds=0.1)
        engine._active = active

        order = []
        original_stop = active.stop_decode

        def _tracking_stop():
            order.append("active_stop")
            original_stop()

        active.stop_decode = _tracking_stop
        engine._retire(active)

        self.assertEqual(order, ["active_stop"])


if __name__ == "__main__":
    unittest.main()

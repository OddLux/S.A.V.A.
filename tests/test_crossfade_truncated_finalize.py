"""dp-221: a TRUNCATED crossfade must not step the incoming deck to full gain.

`_callback`'s finalize branch fires on `_crossfade_elapsed >= _crossfade_len`
OR `active.just_ended`. On the second path the outgoing track hit its real end
mid-fade, so the incoming deck is still at a partial gain -- and the finalize
used to set `gain = 1.0` unconditionally, jumping it to full volume in a
single block. That is a click, not a fade.

    QT_QPA_PLATFORM=offscreen ./venv/Scripts/python.exe -m unittest discover tests
"""

import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_deck_engine import _LinearOverlap, _make_bare_engine, _make_deck  # noqa: E402

BLOCK = 1024
RATE = 48000


def _peak(block):
    return float(np.max(np.abs(block)))


class TestTruncatedCrossfadeRampsInsteadOfSnapping(unittest.TestCase):

    def _run_truncated_crossfade(self, blocks_to_render=12):
        """Outgoing deck ends WELL before the ramp completes: a 4s crossfade
        against an outgoing deck with only a few blocks of audio left."""
        engine = _make_bare_engine(sample_rate=RATE)
        engine._master_volume = 1.0

        # Distinct amplitudes ON PURPOSE. With both decks at the same
        # amplitude the incoming deck's gain step is exactly masked by the
        # outgoing deck vanishing, and the mixed peak barely moves -- a test
        # written that way passes with or without the fix. The outgoing deck
        # is made quiet so the incoming deck's own level dominates the mix.
        active = _make_deck(sample_rate=RATE, seconds=1.0, amplitude=800)
        active.read_idx = int(RATE * 1.0) - 3 * BLOCK  # 3 blocks from its end
        engine._active = active

        incoming = _make_deck(sample_rate=RATE, seconds=10.0, amplitude=30000)
        incoming.active = False
        engine._idle = incoming
        engine._idle_armed = True

        overlap = _LinearOverlap(4.0)  # far longer than what's left of active
        engine._crossfade_overlap = overlap
        engine._crossfade_len = int(overlap.duration * RATE)
        engine._crossfade_running = False
        engine._crossfade_elapsed = 0

        # Captured up front: finalize zeroes _crossfade_len, so it cannot be
        # read back afterwards to check the ramp was cut short.
        full_len = engine._crossfade_len

        blocks = []
        for _ in range(blocks_to_render):
            out = np.zeros((BLOCK, 2), dtype=np.float32)
            engine._callback(out, BLOCK, None, None)
            blocks.append(out.copy())
        return engine, np.concatenate(blocks), full_len

    def test_output_has_no_click_at_the_truncation_boundary(self):
        """A click IS a sample-to-sample discontinuity, so measure exactly
        that. Both decks carry a constant amplitude, so every adjacent-sample
        delta in the output is purely a gain change -- a hard step from the
        incoming deck's partial crossfade gain to 1.0 shows up as one large
        jump between two neighbouring samples, while a real ramp spreads the
        same distance over ~1440 samples.

        Block-level peak comparison was tried first and is useless here: a
        1024-frame block is 21ms, comparable to the fade itself, so a correct
        ramp and a hard step produce similar per-block peaks."""
        engine, signal, full_len = self._run_truncated_crossfade()

        # Guard the premise, or this proves nothing.
        self.assertLess(
            engine._crossfade_elapsed,
            full_len,
            "premise failed: the ramp completed normally, nothing was truncated",
        )
        self.assertTrue(engine._swap_pending, "premise failed: no swap happened")

        step = float(np.max(np.abs(np.diff(signal[:, 0]))))
        self.assertLess(
            step,
            0.05,
            f"discontinuity of {step:.3f} between adjacent samples -- the "
            "incoming deck's gain was stepped, not ramped",
        )

    def test_finalize_hands_the_remaining_gain_to_the_fade_envelope(self):
        """Pins the mechanism. Rendered only up to the finalize block: the
        ramp is 30ms (~1.4 blocks), so rendering further would let it complete
        and clear `_fade_start`, and the assertion would read post-fade state
        and fail against correct code."""
        engine, _signal, _len = self._run_truncated_crossfade(blocks_to_render=3)

        incoming = engine._active  # refs were swapped by finalize
        self.assertIsNotNone(
            incoming._fade_start,
            "no fade armed -- the incoming deck's gain was stepped to 1.0",
        )
        self.assertLess(
            incoming._fade_from, 0.5, "fade did not start from the partial gain"
        )
        self.assertEqual(incoming._fade_to, 1.0)

    def test_incoming_deck_reaches_full_gain_eventually(self):
        """The ramp must finish, not leave the new track quiet forever."""
        engine, _signal, _len = self._run_truncated_crossfade()

        out = np.zeros((BLOCK, 2), dtype=np.float32)
        for _ in range(20):
            engine._callback(out, BLOCK, None, None)

        self.assertEqual(engine._active.gain, 1.0)
        self.assertGreater(_peak(out), 0.4)

    def test_crossfade_state_is_fully_cleared_after_truncation(self):
        engine, _signal, _len = self._run_truncated_crossfade()

        self.assertFalse(engine._crossfade_running)
        self.assertEqual(engine._crossfade_len, 0)
        self.assertIsNone(engine._crossfade_overlap)
        self.assertTrue(engine._swap_pending)


if __name__ == "__main__":
    unittest.main()

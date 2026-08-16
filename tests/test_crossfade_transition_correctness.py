"""dp-221: the crossfade must belong to the transition that is actually
about to happen.

`_rearm_preload` predicts the incoming track with `playlist.peek_next()`
(shuffle-aware; returns the CURRENT track under repeat=one) but used to arm
the fade from `layout.overlaps[current_index]` -- the LINEAR neighbour. Under
shuffle that applied a curve authored for one pair of tracks to a different
pair; under repeat=one it crossfaded a track into a fresh copy of itself
using its neighbour's curve.

    QT_QPA_PLATFORM=offscreen ./venv/Scripts/python.exe -m unittest discover tests
"""

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.crossfade_markers import crossfade_marker_positions  # noqa: E402
from core.crossfade_model import CrossfadeLayout, LayoutTrack  # noqa: E402


def _make_layout(n=3, duration=100.0, overlap=10.0):
    layout = CrossfadeLayout(
        [LayoutTrack(filepath=f"t{i}.wav", duration=duration) for i in range(n)]
    )
    for i in range(n - 1):
        layout.set_overlap_duration(i, overlap)
    return layout


class _FakeWindow:
    """The MainWindow attributes the methods under test touch.

    `_next_is_layout_successor` is bound to the REAL implementation, not
    stubbed -- it is half the logic being tested here, and a stub would let
    `_overlap_for_transition` pass while the actual gate was broken.
    """

    def __init__(self, layout):
        from ui.main_window import MainWindow

        self._crossfade_layout = layout
        self._next_is_layout_successor = (
            lambda idx: MainWindow._next_is_layout_successor(self, idx)
        )


class TestOverlapMatchesTheActualNextTrack(unittest.TestCase):

    def _call(self, layout, idx, current_fp, next_fp):
        from ui.main_window import MainWindow

        window = _FakeWindow(layout)
        tracks = [{"filepath": t.filepath} for t in layout.tracks]
        with mock.patch("ui.main_window.playlist") as fake_playlist:
            fake_playlist.tracks = tracks
            fake_playlist.current = {"filepath": current_fp}
            fake_playlist.peek_next.return_value = (
                {"filepath": next_fp} if next_fp else None
            )
            return MainWindow._overlap_for_transition(window, idx)

    def test_linear_order_still_arms_its_authored_overlap(self):
        layout = _make_layout()
        overlap = self._call(layout, 0, "t0.wav", "t1.wav")
        self.assertIsNotNone(overlap)
        self.assertEqual(overlap.duration, 10.0)

    def test_shuffle_jumping_past_a_track_arms_nothing(self):
        """t0 -> t2 is not an authored pair; overlap[0] describes t0 -> t1."""
        layout = _make_layout()
        self.assertIsNone(self._call(layout, 0, "t0.wav", "t2.wav"))

    def test_repeat_one_never_arms_a_self_into_self_crossfade(self):
        """peek_next() returns the CURRENT track under repeat=one."""
        layout = _make_layout()
        self.assertIsNone(self._call(layout, 0, "t0.wav", "t0.wav"))

    def test_end_of_playlist_arms_nothing(self):
        layout = _make_layout()
        self.assertIsNone(self._call(layout, 0, "t0.wav", None))

    def test_zero_duration_overlap_still_arms_nothing(self):
        layout = _make_layout(overlap=0.0)
        self.assertIsNone(self._call(layout, 0, "t0.wav", "t1.wav"))


class TestFadeMarkersAgreeWithWhatWillBeArmed(unittest.TestCase):
    """The waveform must not draw a fade-out marker for a fade that will not
    fire -- otherwise the UI promises a transition the engine refuses."""

    def test_fade_out_marker_suppressed_when_next_is_not_the_successor(self):
        layout = _make_layout()
        _, fade_out = crossfade_marker_positions(
            layout, 0, "t0.wav", next_matches=False
        )
        self.assertIsNone(fade_out)

    def test_fade_out_marker_drawn_in_linear_order(self):
        layout = _make_layout()
        _, fade_out = crossfade_marker_positions(
            layout, 0, "t0.wav", next_matches=True
        )
        self.assertEqual(fade_out, 90.0)

    def test_fade_in_marker_survives_a_mismatched_next(self):
        """fade_in_end describes a fade that ALREADY happened on the way in,
        so an unpredictable next track has no bearing on it."""
        layout = _make_layout()
        fade_in, _ = crossfade_marker_positions(
            layout, 1, "t1.wav", next_matches=False
        )
        self.assertEqual(fade_in, 10.0)

    def test_default_keeps_the_old_behavior(self):
        layout = _make_layout()
        self.assertEqual(
            crossfade_marker_positions(layout, 0, "t0.wav"),
            crossfade_marker_positions(layout, 0, "t0.wav", next_matches=True),
        )


class TestOverlapClampUsesRemainingDuration(unittest.TestCase):
    """A track sits between two overlaps. Clamping each against the FULL
    track duration let both consume the whole track, so the track was eaten
    from both ends at once and DeckEngine's trigger fired from frame 0."""

    def test_two_overlaps_around_one_track_cannot_exceed_its_length(self):
        layout = _make_layout(n=3, duration=100.0, overlap=0.0)

        layout.set_overlap_duration(0, 80.0)   # eats t1's head
        layout.set_overlap_duration(1, 80.0)   # would eat t1's tail too

        total = layout.overlaps[0].duration + layout.overlaps[1].duration
        self.assertLessEqual(
            total,
            layout.tracks[1].duration,
            "both overlaps together consumed more than the middle track",
        )

    def test_second_overlap_is_clamped_to_what_the_first_left(self):
        layout = _make_layout(n=3, duration=100.0, overlap=0.0)
        layout.set_overlap_duration(0, 30.0)

        layout.set_overlap_duration(1, 999.0)

        self.assertEqual(layout.overlaps[1].duration, 70.0)

    def test_middle_track_never_starts_before_the_previous_one(self):
        layout = _make_layout(n=3, duration=100.0, overlap=0.0)
        layout.set_overlap_duration(0, 80.0)
        layout.set_overlap_duration(1, 80.0)

        positions = layout.track_positions()
        self.assertEqual(positions, sorted(positions))

    def test_unconstrained_edges_still_allow_a_full_length_overlap(self):
        """A 2-track layout has no second overlap to reserve room for, so the
        old maximum must still be reachable -- the fix must not over-clamp."""
        layout = CrossfadeLayout(
            [LayoutTrack(filepath="a.wav", duration=50.0),
             LayoutTrack(filepath="b.wav", duration=50.0)]
        )
        layout.set_overlap_duration(0, 999.0)
        self.assertEqual(layout.overlaps[0].duration, 50.0)


if __name__ == "__main__":
    unittest.main()

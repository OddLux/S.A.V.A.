"""dp-216 Phase 3 Part B: pure marker-position math, no Qt.

    ./venv/Scripts/python.exe -m unittest discover tests
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.crossfade_markers import crossfade_marker_positions
from core.crossfade_model import CrossfadeLayout, LayoutTrack


def _make_layout(durations, overlap_durations):
    tracks = [LayoutTrack(filepath=f"t{i}.wav", duration=d) for i, d in enumerate(durations)]
    layout = CrossfadeLayout(tracks)
    for i, ov_dur in enumerate(overlap_durations):
        layout.set_overlap_duration(i, ov_dur)
    return layout


class TestCrossfadeMarkerPositions(unittest.TestCase):

    def test_none_layout(self):
        self.assertEqual(crossfade_marker_positions(None, 0, "t0.wav"), (None, None))

    def test_empty_layout(self):
        layout = CrossfadeLayout([])
        self.assertEqual(crossfade_marker_positions(layout, 0, "t0.wav"), (None, None))

    def test_stale_filepath(self):
        layout = _make_layout([10.0, 10.0], [3.0])
        self.assertEqual(
            crossfade_marker_positions(layout, 0, "not_in_layout.wav"), (None, None)
        )

    def test_index_out_of_range(self):
        layout = _make_layout([10.0, 10.0], [3.0])
        self.assertEqual(crossfade_marker_positions(layout, 5, "t0.wav"), (None, None))
        self.assertEqual(crossfade_marker_positions(layout, -1, "t0.wav"), (None, None))

    def test_first_track_only_fade_out(self):
        layout = _make_layout([10.0, 10.0], [3.0])
        fade_in, fade_out = crossfade_marker_positions(layout, 0, "t0.wav")
        self.assertIsNone(fade_in)
        self.assertAlmostEqual(fade_out, 7.0)

    def test_last_track_only_fade_in(self):
        layout = _make_layout([10.0, 10.0], [3.0])
        fade_in, fade_out = crossfade_marker_positions(layout, 1, "t1.wav")
        self.assertAlmostEqual(fade_in, 3.0)
        self.assertIsNone(fade_out)

    def test_middle_track_both_markers(self):
        layout = _make_layout([10.0, 10.0, 10.0], [2.0, 4.0])
        fade_in, fade_out = crossfade_marker_positions(layout, 1, "t1.wav")
        self.assertAlmostEqual(fade_in, 2.0)
        self.assertAlmostEqual(fade_out, 6.0)

    def test_no_overlap_neighbors_gives_none(self):
        layout = _make_layout([10.0, 10.0, 10.0], [0.0, 0.0])
        fade_in, fade_out = crossfade_marker_positions(layout, 1, "t1.wav")
        self.assertIsNone(fade_in)
        self.assertIsNone(fade_out)

    def test_fade_out_uses_effective_fin_truncated_duration(self):
        # from_playlist_tracks bakes the Fin marker into LayoutTrack.duration
        # (see crossfade_model.py); this helper must use that field as-is,
        # not re-derive from a raw playlist duration.
        tracks = [
            LayoutTrack(filepath="a.wav", duration=8.0),  # Fin-truncated from 10.0
            LayoutTrack(filepath="b.wav", duration=10.0),
        ]
        layout = CrossfadeLayout(tracks)
        layout.set_overlap_duration(0, 3.0)
        fade_in, fade_out = crossfade_marker_positions(layout, 0, "a.wav")
        self.assertIsNone(fade_in)
        self.assertAlmostEqual(fade_out, 5.0)  # 8.0 - 3.0, not 10.0 - 3.0


if __name__ == "__main__":
    unittest.main()

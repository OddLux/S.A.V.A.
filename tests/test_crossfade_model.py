"""
Unit tests for core/crossfade_model.py (dp-157).

No pytest dependency in this project's venv — plain unittest, runnable via:
    ./venv/Scripts/python.exe -m unittest discover tests
"""

import copy
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.crossfade_model import CrossfadeLayout, Overlap
from config.settings import settings


def make_tracks():
    return [
        {"filepath": "a.mp3", "duration": 100.0, "id": "row-a"},
        {"filepath": "b.mp3", "duration": 80.0, "id": "row-b"},
        {"filepath": "c.mp3", "duration": 60.0, "id": "row-c"},
    ]


class TestCrossfadeLayout(unittest.TestCase):

    def test_default_layout_has_zero_overlap_and_flush_positions(self):
        layout = CrossfadeLayout.from_playlist_tracks(make_tracks())
        self.assertEqual(layout.track_positions(), [0.0, 100.0, 180.0])
        self.assertEqual(layout.total_duration(), 240.0)

    def test_move_track_left_shifts_rigid_chain_no_gap(self):
        layout = CrossfadeLayout.from_playlist_tracks(make_tracks())
        layout.set_overlap_duration(0, 10.0)  # track b moves 10s left
        positions = layout.track_positions()
        self.assertEqual(positions, [0.0, 90.0, 170.0])
        # No gap possible by construction: track[i+1] start == track[i]
        # start + duration - overlap, always contiguous.
        for i in range(len(positions) - 1):
            end_of_prev = positions[i] + layout.tracks[i].duration
            gap = positions[i + 1] - (end_of_prev - layout.overlaps[i].duration)
            self.assertAlmostEqual(gap, 0.0)

    def test_move_track_right_of_default_is_rejected(self):
        layout = CrossfadeLayout.from_playlist_tracks(make_tracks())
        layout.set_overlap_duration(0, -25.0)  # attempt to move right (gap)
        self.assertEqual(layout.overlaps[0].duration, 0.0)  # clamped to 0
        self.assertEqual(layout.track_positions(), [0.0, 100.0, 180.0])

    def test_overlap_clamped_to_shorter_neighbor_duration(self):
        layout = CrossfadeLayout.from_playlist_tracks(make_tracks())
        layout.set_overlap_duration(1, 999.0)  # b(80s) <-> c(60s): max is 60s
        self.assertEqual(layout.overlaps[1].duration, 60.0)

    def test_from_playlist_tracks_honors_fin_end_marker_as_effective_end(self):
        # dp-212/dp-238: a track with a set "Fin" end marker uses the
        # marker as its extent, not the file's full duration. Keyed on the
        # row's track_id, not filepath (dp-238).
        markers = settings.get("row_end_markers", {})
        original = copy.deepcopy(markers)
        try:
            markers["row-a"] = 40.0
            settings.set("row_end_markers", markers)
            layout = CrossfadeLayout.from_playlist_tracks(make_tracks())
            self.assertEqual(layout.tracks[0].duration, 40.0)
            # Unmarked tracks unaffected.
            self.assertEqual(layout.tracks[1].duration, 80.0)
            self.assertEqual(layout.track_positions(), [0.0, 40.0, 120.0])
        finally:
            settings.set("row_end_markers", original)

    def test_from_playlist_tracks_no_marker_uses_full_duration(self):
        # Regression: with no marker set, effective end == file duration,
        # unchanged from pre-dp-212 behavior.
        markers = settings.get("row_end_markers", {})
        original = copy.deepcopy(markers)
        try:
            markers.pop("row-a", None)
            settings.set("row_end_markers", markers)
            layout = CrossfadeLayout.from_playlist_tracks(make_tracks())
            self.assertEqual(layout.tracks[0].duration, 100.0)
            self.assertEqual(layout.track_positions(), [0.0, 100.0, 180.0])
        finally:
            settings.set("row_end_markers", original)

    def test_default_curve_is_monotonic_and_bounded(self):
        ov = Overlap()
        prev = -1.0
        for i in range(11):
            t = i / 10.0
            y = ov.evaluate_in(t)
            self.assertGreaterEqual(y, prev)
            self.assertGreaterEqual(y, 0.0)
            self.assertLessEqual(y, 1.0)
            prev = y
        self.assertAlmostEqual(ov.evaluate_in(0.0), 0.0, places=2)
        self.assertAlmostEqual(ov.evaluate_in(1.0), 1.0, places=2)

    def test_legacy_overlap_without_stored_fadeout_keeps_equal_power_complement(self):
        # dp-190: a brand new Overlap() now has its own independently
        # editable fade-out curve, so the equal-power complement no longer
        # holds for it (see test_independent_fadeout_curve_is_decoupled_
        # from_fadein below). But a layout saved *before* dp-190 has no
        # curve_out_p1/p2 keys at all -- Overlap.from_dict() must still
        # derive evaluate_out() as the old equal-power complement in that
        # case, so loading (and resaving without touching fade-out) an old
        # save file doesn't silently change its sound.
        legacy = Overlap.from_dict({"duration": 5.0})
        self.assertIsNone(legacy.curve_out_p1)
        self.assertIsNone(legacy.curve_out_p2)
        for i in range(11):
            t = i / 10.0
            in_gain = legacy.evaluate_in(t)
            out_gain = legacy.evaluate_out(t)
            energy = in_gain ** 2 + out_gain ** 2
            self.assertAlmostEqual(energy, 1.0, places=6)

    def test_independent_fadeout_curve_is_decoupled_from_fadein(self):
        # dp-190: a fresh Overlap() has its own fade-out curve from
        # creation, so editing the fade-in curve must not move the
        # fade-out curve at all -- the two are fully independent.
        ov = Overlap()
        before = [ov.evaluate_out(i / 10.0) for i in range(11)]
        ov.curve_p1 = (0.9, 0.1)
        ov.curve_p2 = (0.1, 0.9)
        after = [ov.evaluate_out(i / 10.0) for i in range(11)]
        self.assertEqual(before, after)
        # And its own curve's endpoints are gain 1 at the overlap's start,
        # gain 0 at its end (falling, not rising like fade-in).
        self.assertAlmostEqual(ov.evaluate_out(0.0), 1.0, places=2)
        self.assertAlmostEqual(ov.evaluate_out(1.0), 0.0, places=2)

    def test_persist_and_reload_round_trip(self):
        # Isolate settings persistence to a throwaway temp path so this test
        # never touches the real config/sava_settings.json (dp-181).
        # dp-258 added a suite-wide autouse fixture (tests/conftest.py) that
        # already does this for every test, so under pytest the block below is
        # redundant. Kept deliberately as belt-and-braces: conftest.py is a
        # pytest mechanism, and this keeps the test isolated when run through
        # `python -m unittest discover tests`, which ignores conftest entirely.
        original_path = settings.file_path
        original_data = copy.deepcopy(settings._data)
        tmp_dir = tempfile.mkdtemp(prefix="sava_test_settings_")
        settings._path = Path(tmp_dir) / "sava_settings.json"
        try:
            settings.set("crossfade_layout", {})
            layout = CrossfadeLayout.from_playlist_tracks(make_tracks())
            layout.set_overlap_duration(0, 15.0)
            layout.set_overlap_curve(0, (0.2, 0.1), (0.8, 0.9))
            layout.save()

            reloaded = CrossfadeLayout.load()
            self.assertIsNotNone(reloaded)
            self.assertEqual(reloaded.track_positions(), layout.track_positions())
            self.assertEqual(reloaded.overlaps[0].curve_p1, (0.2, 0.1))
            self.assertEqual(reloaded.overlaps[0].curve_p2, (0.8, 0.9))

            CrossfadeLayout.clear_persisted()
            self.assertIsNone(CrossfadeLayout.load())
        finally:
            settings._path = original_path
            settings._data = original_data
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def test_collinear_control_points_reduce_to_linear_ramp(self):
        # dp-180: the linear-curve action places both control points on the
        # P0-P3 diagonal. All four bezier points collinear should reduce
        # the cubic bezier to the identity ramp x(u) == y(u) == u.
        ov = Overlap(curve_p1=(1 / 3, 1 / 3), curve_p2=(2 / 3, 2 / 3))
        for i in range(11):
            t = i / 10.0
            self.assertAlmostEqual(ov.evaluate_in(t), t, places=4)

    def test_reset_hook_clears_in_memory_state(self):
        layout = CrossfadeLayout.from_playlist_tracks(make_tracks())
        layout.reset()
        self.assertEqual(layout.tracks, [])
        self.assertEqual(layout.overlaps, [])
        self.assertEqual(layout.track_positions(), [])

    def test_from_dict_truncates_excess_overlaps(self):
        # dp-245 D6: a hand-edited or partially-written settings file can
        # carry more overlaps than tracks - 1. Without truncation,
        # track_positions() raises IndexError indexing self._tracks[i].
        d = {
            "tracks": [
                {"filepath": "a.mp3", "duration": 100.0, "track_id": "row-a"},
                {"filepath": "b.mp3", "duration": 80.0, "track_id": "row-b"},
            ],
            "overlaps": [
                {"duration": 5.0},
                {"duration": 5.0},
                {"duration": 5.0},
            ],
        }
        layout = CrossfadeLayout.from_dict(d)
        self.assertEqual(len(layout.overlaps), 1)
        # Must not raise, and must return one position per track.
        positions = layout.track_positions()
        self.assertEqual(len(positions), 2)

    def test_from_dict_pads_missing_overlaps(self):
        # Fewer overlaps than tracks - 1 must be padded with default
        # (zero-duration) Overlap instances, not left short.
        #
        # NOTE: this deliberately uses a NON-EMPTY short list (1 overlap for
        # 3 tracks). An empty "overlaps" list does not discriminate: the
        # constructor already builds len(tracks) - 1 default overlaps, so a
        # broken pad would still pass. Only a partially-populated list
        # actually reaches the padding branch.
        d = {
            "tracks": [
                {"filepath": "a.mp3", "duration": 100.0, "track_id": "row-a"},
                {"filepath": "b.mp3", "duration": 100.0, "track_id": "row-b"},
                {"filepath": "c.mp3", "duration": 100.0, "track_id": "row-c"},
            ],
            "overlaps": [{"duration": 5.0}],
        }
        layout = CrossfadeLayout.from_dict(d)
        self.assertEqual(len(layout.overlaps), 2)
        # The authored overlap survives; the missing one defaults to zero.
        self.assertEqual(layout.overlaps[0].duration, 5.0)
        self.assertEqual(layout.overlaps[1].duration, 0.0)
        # Without padding this returns only 2 positions and total_duration()
        # drops track c entirely (195.0 instead of 295.0).
        self.assertEqual(layout.track_positions(), [0.0, 95.0, 195.0])
        self.assertEqual(layout.total_duration(), 295.0)

    def test_from_dict_empty_overlaps_still_yields_one_per_gap(self):
        """The all-defaults path (no overlaps key content at all) must still
        produce len(tracks) - 1 overlaps."""
        d = {
            "tracks": [
                {"filepath": "a.mp3", "duration": 100.0, "track_id": "row-a"},
                {"filepath": "b.mp3", "duration": 80.0, "track_id": "row-b"},
                {"filepath": "c.mp3", "duration": 60.0, "track_id": "row-c"},
            ],
            "overlaps": [],
        }
        layout = CrossfadeLayout.from_dict(d)
        self.assertEqual(len(layout.overlaps), 2)
        self.assertEqual(layout.track_positions(), [0.0, 100.0, 180.0])


if __name__ == "__main__":
    unittest.main()

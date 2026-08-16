"""dp-218/dp-238: pure marker-lookup logic for the read-only next-track
preview waveform, no Qt.

    ./venv/Scripts/python.exe -m pytest tests/test_preview_waveform_markers.py
"""

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.crossfade_model import CrossfadeLayout, LayoutTrack
from ui.preview_waveform_widget import resolve_preview_markers


def _make_layout(durations, overlap_durations):
    tracks = [LayoutTrack(filepath=f"t{i}.wav", duration=d) for i, d in enumerate(durations)]
    layout = CrossfadeLayout(tracks)
    for i, ov_dur in enumerate(overlap_durations):
        layout.set_overlap_duration(i, ov_dur)
    return layout


class TestResolvePreviewMarkers(unittest.TestCase):

    def _settings_get(self, cue_points=None, end_markers=None, start_markers=None):
        cue_points = cue_points or {}
        end_markers = end_markers or {}
        start_markers = start_markers or {}

        def fake_get(key, default=None):
            if key == "row_cue_points":
                return cue_points
            if key == "row_end_markers":
                return end_markers
            if key == "row_start_markers":
                return start_markers
            return default

        return fake_get

    def test_cues_present(self):
        # Discrimination check: this must FAIL if the cue lookup is stubbed
        # to return {} -- i.e. the assertion actually depends on the patched
        # data, not just a coincidental default.
        get = self._settings_get(cue_points={"row-a": [1.5, 3.0]})
        with mock.patch("ui.preview_waveform_widget.settings.get", side_effect=get):
            result = resolve_preview_markers("row-a", "a.wav", None, 0)
        self.assertEqual(result["cues"], {0: 1.5, 1: 3.0})

    def test_cues_absent_gives_empty_dict(self):
        get = self._settings_get(cue_points={"row-other": [1.0]})
        with mock.patch("ui.preview_waveform_widget.settings.get", side_effect=get):
            result = resolve_preview_markers("row-a", "a.wav", None, 0)
        self.assertEqual(result["cues"], {})

    def test_end_marker_present(self):
        get = self._settings_get(end_markers={"row-a": 9.5})
        with mock.patch("ui.preview_waveform_widget.settings.get", side_effect=get):
            result = resolve_preview_markers("row-a", "a.wav", None, 0)
        self.assertEqual(result["end_marker"], 9.5)

    def test_end_marker_absent_gives_none(self):
        get = self._settings_get(end_markers={"row-other": 9.5})
        with mock.patch("ui.preview_waveform_widget.settings.get", side_effect=get):
            result = resolve_preview_markers("row-a", "a.wav", None, 0)
        self.assertIsNone(result["end_marker"])

    def test_start_marker_present(self):
        get = self._settings_get(start_markers={"row-a": 1.5})
        with mock.patch("ui.preview_waveform_widget.settings.get", side_effect=get):
            result = resolve_preview_markers("row-a", "a.wav", None, 0)
        self.assertEqual(result["start_marker"], 1.5)

    def test_start_marker_absent_gives_none(self):
        get = self._settings_get(start_markers={"row-other": 1.5})
        with mock.patch("ui.preview_waveform_widget.settings.get", side_effect=get):
            result = resolve_preview_markers("row-a", "a.wav", None, 0)
        self.assertIsNone(result["start_marker"])

    def test_two_rows_of_the_same_file_show_different_markers(self):
        """dp-238 acceptance criterion: markers are keyed on track_id, not
        filepath, so two playlist rows of the same file diverge."""
        get = self._settings_get(
            start_markers={"row-a": 1.0, "row-b": 2.0},
            end_markers={"row-a": 10.0, "row-b": 20.0},
        )
        with mock.patch("ui.preview_waveform_widget.settings.get", side_effect=get):
            result_a = resolve_preview_markers("row-a", "dup.wav", None, 0)
            result_b = resolve_preview_markers("row-b", "dup.wav", None, 0)
        self.assertEqual(result_a["start_marker"], 1.0)
        self.assertEqual(result_a["end_marker"], 10.0)
        self.assertEqual(result_b["start_marker"], 2.0)
        self.assertEqual(result_b["end_marker"], 20.0)

    def test_fade_markers_present_with_overlap_layout(self):
        layout = _make_layout([10.0, 10.0], [3.0])
        get = self._settings_get()
        with mock.patch("ui.preview_waveform_widget.settings.get", side_effect=get):
            result = resolve_preview_markers("row-a", "t1.wav", layout, 1)
        self.assertAlmostEqual(result["fade_in_end"], 3.0)
        self.assertIsNone(result["fade_out_start"])

    def test_fade_markers_none_when_layout_is_none(self):
        get = self._settings_get()
        with mock.patch("ui.preview_waveform_widget.settings.get", side_effect=get):
            result = resolve_preview_markers("row-a", "t1.wav", None, 1)
        self.assertIsNone(result["fade_in_end"])
        self.assertIsNone(result["fade_out_start"])

    def test_fade_markers_none_when_index_stale(self):
        # Layout exists but the filepath at that index doesn't match --
        # crossfade_marker_positions' own staleness guard.
        layout = _make_layout([10.0, 10.0], [3.0])
        get = self._settings_get()
        with mock.patch("ui.preview_waveform_widget.settings.get", side_effect=get):
            result = resolve_preview_markers("row-a", "not_in_layout.wav", layout, 1)
        self.assertIsNone(result["fade_in_end"])
        self.assertIsNone(result["fade_out_start"])

    def test_unknown_track_gives_all_empty(self):
        get = self._settings_get()
        with mock.patch("ui.preview_waveform_widget.settings.get", side_effect=get):
            result = resolve_preview_markers("row-unknown", "unknown.wav", None, -1)
        self.assertEqual(result["cues"], {})
        self.assertIsNone(result["end_marker"])
        self.assertIsNone(result["start_marker"])
        self.assertIsNone(result["fade_in_end"])
        self.assertIsNone(result["fade_out_start"])


class _FakePreviewWidget:
    """Captures the last marker dict pushed at it."""

    def __init__(self):
        self.markers = None

    def set_markers(self, markers):
        self.markers = markers


class _FakeWindow:
    """The attributes _refresh_preview_markers touches.

    Bound to the real unbound method below rather than constructing a
    MainWindow -- that would need a QApplication, an audio device, and the
    whole engine/playlist singleton graph for a method that is a handful of
    lines of pure lookup.
    """

    def __init__(self, target, target_id, layout):
        self._preview_target = target
        self._preview_target_id = target_id
        self._crossfade_layout = layout
        self._preview_waveform = _FakePreviewWidget()


class TestRefreshPreviewMarkers(unittest.TestCase):
    """dp-218 fix: markers were resolved once at re-target time, so editing a
    cue / the Fin marker / the crossfade layout for the queued track left the
    preview showing a stale set until the next track advance. dp-238: the
    lookup must key on the queued row's track_id, not just its filepath."""

    def _run(self, window, tracks):
        from ui.main_window import MainWindow

        get = TestResolvePreviewMarkers._settings_get(self)
        with mock.patch("ui.main_window.playlist") as fake_playlist, \
                mock.patch("ui.preview_waveform_widget.settings.get", side_effect=get):
            fake_playlist.tracks = tracks
            MainWindow._refresh_preview_markers(window)

    def test_no_op_when_nothing_queued(self):
        window = _FakeWindow(None, None, None)
        self._run(window, [])
        self.assertIsNone(window._preview_waveform.markers)

    def test_picks_up_a_layout_change_without_retargeting(self):
        tracks = [{"filepath": f"t{i}.wav", "id": f"row-{i}"} for i in range(2)]
        window = _FakeWindow("t1.wav", "row-1", _make_layout([10.0, 10.0], [3.0]))
        self._run(window, tracks)
        self.assertEqual(window._preview_waveform.markers["fade_in_end"], 3.0)

        # Same target, new layout -- exactly the case that used to go stale.
        window._crossfade_layout = _make_layout([10.0, 10.0], [7.0])
        self._run(window, tracks)
        self.assertEqual(window._preview_waveform.markers["fade_in_end"], 7.0)

    def test_uses_track_id_to_disambiguate_duplicate_filepaths(self):
        """Two rows of the same file: the queued row's OWN markers must be
        the ones shown, not whichever row of that filepath comes first."""
        tracks = [
            {"filepath": "dup.wav", "id": "row-0"},
            {"filepath": "dup.wav", "id": "row-1"},
        ]
        window = _FakeWindow("dup.wav", "row-1", None)
        get = TestResolvePreviewMarkers()._settings_get(
            start_markers={"row-0": 1.0, "row-1": 2.0},
        )
        with mock.patch("ui.main_window.playlist") as fake_playlist, \
                mock.patch("ui.preview_waveform_widget.settings.get", side_effect=get):
            fake_playlist.tracks = tracks
            from ui.main_window import MainWindow
            MainWindow._refresh_preview_markers(window)
        self.assertEqual(window._preview_waveform.markers["start_marker"], 2.0)


if __name__ == "__main__":
    unittest.main()

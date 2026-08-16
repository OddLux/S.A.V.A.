"""dp-236: selective crossfade preservation on playlist reorder.

Reverses dp-160's "any playlist edit resets the whole layout" rule.
`MainWindow._invalidate_crossfade_layout_if_stale` now rebuilds the layout
from the live playlist every edit (so it can never disagree with the
playlist — dp-236's hard consistency constraint) but carries forward any
overlap whose ordered pair of dp-237 track ids is STILL adjacent in the
same order after the edit. Everything else — the moved track's own edges,
an added/removed track's edges, and whichever old neighbours meet each
other once a track is gone — comes back as the layout's normal zero-
overlap default.

No pytest dependency in this project's venv — plain unittest, runnable via:
    QT_QPA_PLATFORM=offscreen ./venv/Scripts/python.exe -m unittest discover tests
"""

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.crossfade_model import CrossfadeLayout  # noqa: E402


def _track(idx, tid=None, filepath=None, duration=100.0):
    return {
        "filepath": filepath or f"/music/t{idx}.wav",
        "duration": duration,
        "id": tid or f"id{idx}",
    }


def _layout_with_distinct_overlaps(tracks):
    """Build a layout from `tracks` and give every overlap a distinct,
    identifiable duration (10, 20, 30, ...) so a test can tell whether a
    specific overlap survived or was reset to the 0.0 default."""
    layout = CrossfadeLayout.from_playlist_tracks(tracks)
    for i in range(len(tracks) - 1):
        layout.set_overlap_duration(i, (i + 1) * 10.0)
    return layout


class _FakeWindow:
    """Only the attributes/methods `_invalidate_crossfade_layout_if_stale`
    touches. Bound to the REAL implementation via
    `MainWindow._invalidate_crossfade_layout_if_stale(window)`."""

    def __init__(self, layout):
        self._crossfade_layout = layout
        self._crossfade_dialog = None


def _invalidate(window, new_tracks):
    from ui.main_window import MainWindow

    with mock.patch("ui.main_window.playlist") as fake_playlist:
        fake_playlist.tracks = new_tracks
        MainWindow._invalidate_crossfade_layout_if_stale(window)
    return window._crossfade_layout


def _overlap_map(layout):
    """{(id_a, id_b): duration} for every adjacent pair, by track id."""
    ids = [t.track_id for t in layout.tracks]
    return {
        (ids[i], ids[i + 1]): ov.duration
        for i, ov in enumerate(layout.overlaps)
    }


class TestMoveNeighbourInvalidation(unittest.TestCase):

    def test_move_preserves_all_untouched_overlaps(self):
        """5 tracks, move index 1 to index 3 (A,B,C,D,E -> A,C,D,B,E). The
        C-D pair never changed adjacency and must keep its overlap."""
        tracks = [_track(i) for i in range(5)]
        layout = _layout_with_distinct_overlaps(tracks)
        window = _FakeWindow(layout)

        reordered = [tracks[0], tracks[2], tracks[3], tracks[1], tracks[4]]
        new_layout = _invalidate(window, reordered)

        pairs = _overlap_map(new_layout)
        self.assertEqual(pairs[("id2", "id3")], 30.0)  # C-D untouched

    def test_move_resets_own_edges_and_old_and_new_neighbours(self):
        tracks = [_track(i) for i in range(5)]
        layout = _layout_with_distinct_overlaps(tracks)
        window = _FakeWindow(layout)

        reordered = [tracks[0], tracks[2], tracks[3], tracks[1], tracks[4]]
        new_layout = _invalidate(window, reordered)
        pairs = _overlap_map(new_layout)

        # Discrimination check #1: FAILS under the old "reset everything"
        # behaviour would still pass (everything is 0 there) -- so pair it
        # with the "preserves untouched" test above, which that old
        # behaviour fails.
        # A-C: old neighbour of moved track met the other old neighbour --
        # new pair, must default to 0.
        self.assertEqual(pairs[("id0", "id2")], 0.0)
        # D-B: moved track's new left edge.
        self.assertEqual(pairs[("id3", "id1")], 0.0)
        # B-E: moved track's new right edge.
        self.assertEqual(pairs[("id1", "id4")], 0.0)

    def test_move_to_first_position_no_predecessor(self):
        tracks = [_track(i) for i in range(4)]
        layout = _layout_with_distinct_overlaps(tracks)
        window = _FakeWindow(layout)

        reordered = [tracks[2], tracks[0], tracks[1], tracks[3]]
        new_layout = _invalidate(window, reordered)
        pairs = _overlap_map(new_layout)

        self.assertEqual(pairs[("id2", "id0")], 0.0)   # moved track's new edge
        self.assertEqual(pairs[("id0", "id1")], 10.0)  # untouched, still adjacent
        self.assertEqual(pairs[("id1", "id3")], 0.0)   # old neighbours now adjacent

    def test_move_to_last_position_no_successor(self):
        tracks = [_track(i) for i in range(4)]
        layout = _layout_with_distinct_overlaps(tracks)
        window = _FakeWindow(layout)

        reordered = [tracks[0], tracks[2], tracks[3], tracks[1]]
        new_layout = _invalidate(window, reordered)
        pairs = _overlap_map(new_layout)

        self.assertEqual(pairs[("id0", "id2")], 0.0)   # old neighbours now adjacent
        self.assertEqual(pairs[("id2", "id3")], 30.0)  # untouched, still adjacent
        self.assertEqual(pairs[("id3", "id1")], 0.0)   # moved track's new edge

    def test_two_track_playlist_move(self):
        tracks = [_track(0), _track(1)]
        layout = _layout_with_distinct_overlaps(tracks)
        window = _FakeWindow(layout)

        reordered = [tracks[1], tracks[0]]
        new_layout = _invalidate(window, reordered)
        pairs = _overlap_map(new_layout)
        self.assertEqual(pairs[("id1", "id0")], 0.0)  # order flipped -> new pair

    def test_noop_move_is_never_even_seen_as_an_edit(self):
        """i == j: playlist.move() returns before notifying, so the
        invalidation hook never runs for it. Sanity-check the hook itself
        is a no-op when the ordered id/filepath list hasn't changed."""
        tracks = [_track(i) for i in range(3)]
        layout = _layout_with_distinct_overlaps(tracks)
        window = _FakeWindow(layout)

        new_layout = _invalidate(window, list(tracks))  # unchanged order
        self.assertIs(new_layout, layout)  # early return, no rebuild at all


class TestAddNeighbourInvalidation(unittest.TestCase):

    def test_add_between_resets_only_the_new_edges(self):
        """A,B,C with X inserted between A and B: A's fade-out, X's fade-
        in/out, B's fade-in reset. Nothing else does (trivial here with
        only 3 tracks, but the B-C pair -- the only untouched one --
        proves preservation isn't accidental)."""
        tracks = [_track(i) for i in range(3)]
        layout = _layout_with_distinct_overlaps(tracks)
        window = _FakeWindow(layout)

        x = _track(99, tid="idX")
        new_tracks = [tracks[0], x, tracks[1], tracks[2]]
        new_layout = _invalidate(window, new_tracks)
        pairs = _overlap_map(new_layout)

        self.assertEqual(pairs[("id0", "idX")], 0.0)
        self.assertEqual(pairs[("idX", "id1")], 0.0)
        self.assertEqual(pairs[("id1", "id2")], 20.0)  # untouched

    def test_add_at_start(self):
        tracks = [_track(i) for i in range(3)]
        layout = _layout_with_distinct_overlaps(tracks)
        window = _FakeWindow(layout)

        x = _track(99, tid="idX")
        new_layout = _invalidate(window, [x] + tracks)
        pairs = _overlap_map(new_layout)

        self.assertEqual(pairs[("idX", "id0")], 0.0)
        self.assertEqual(pairs[("id0", "id1")], 10.0)  # untouched
        self.assertEqual(pairs[("id1", "id2")], 20.0)  # untouched

    def test_add_at_end(self):
        tracks = [_track(i) for i in range(3)]
        layout = _layout_with_distinct_overlaps(tracks)
        window = _FakeWindow(layout)

        x = _track(99, tid="idX")
        new_layout = _invalidate(window, tracks + [x])
        pairs = _overlap_map(new_layout)

        self.assertEqual(pairs[("id0", "id1")], 10.0)  # untouched
        self.assertEqual(pairs[("id1", "id2")], 20.0)  # untouched
        self.assertEqual(pairs[("id2", "idX")], 0.0)


class TestRemoveNeighbourInvalidation(unittest.TestCase):

    def test_remove_middle_resets_only_the_single_new_overlap(self):
        """A,B,C,D remove B -> A,C,D. Only the new A-C overlap resets;
        C-D was already adjacent and must survive."""
        tracks = [_track(i) for i in range(4)]
        layout = _layout_with_distinct_overlaps(tracks)
        window = _FakeWindow(layout)

        new_tracks = [tracks[0], tracks[2], tracks[3]]
        new_layout = _invalidate(window, new_tracks)
        pairs = _overlap_map(new_layout)

        self.assertEqual(pairs[("id0", "id2")], 0.0)   # new adjacency
        self.assertEqual(pairs[("id2", "id3")], 30.0)  # untouched

    def test_remove_first(self):
        tracks = [_track(i) for i in range(3)]
        layout = _layout_with_distinct_overlaps(tracks)
        window = _FakeWindow(layout)

        new_layout = _invalidate(window, [tracks[1], tracks[2]])
        pairs = _overlap_map(new_layout)
        self.assertEqual(pairs[("id1", "id2")], 20.0)  # untouched, still adjacent

    def test_remove_last(self):
        tracks = [_track(i) for i in range(3)]
        layout = _layout_with_distinct_overlaps(tracks)
        window = _FakeWindow(layout)

        new_layout = _invalidate(window, [tracks[0], tracks[1]])
        pairs = _overlap_map(new_layout)
        self.assertEqual(pairs[("id0", "id1")], 10.0)  # untouched, still adjacent

    def test_remove_down_to_two_tracks(self):
        tracks = [_track(i) for i in range(3)]
        layout = _layout_with_distinct_overlaps(tracks)
        window = _FakeWindow(layout)

        new_layout = _invalidate(window, [tracks[0], tracks[2]])
        pairs = _overlap_map(new_layout)
        self.assertEqual(pairs[("id0", "id2")], 0.0)  # new adjacency

    def test_remove_last_remaining_track_clears_layout(self):
        tracks = [_track(0)]
        layout = _layout_with_distinct_overlaps(tracks)
        window = _FakeWindow(layout)

        new_layout = _invalidate(window, [])
        self.assertIsNone(new_layout)


class TestDuplicateFileDisambiguation(unittest.TestCase):

    def test_same_file_twice_disambiguated_by_id_not_filepath(self):
        """dp-237's case: the same file appears at two rows. Moving one
        copy must not be confused with the other -- overlaps are keyed on
        id, so an edit to one copy's neighbours never touches the other's."""
        same_fp = "/music/loop.wav"
        t_a = _track(0, tid="idA", filepath=same_fp)
        t_b = _track(0, tid="idB", filepath=same_fp)
        t_c = _track(2, tid="idC")
        tracks = [t_a, t_b, t_c]
        layout = _layout_with_distinct_overlaps(tracks)
        window = _FakeWindow(layout)

        # Move the SECOND copy (idB) to the front: idB, idA, idC.
        new_layout = _invalidate(window, [t_b, t_a, t_c])
        pairs = _overlap_map(new_layout)

        self.assertEqual(pairs[("idB", "idA")], 0.0)   # order flipped -> new pair
        self.assertEqual(pairs[("idA", "idC")], 0.0)   # new adjacency


class TestDiscriminationAgainstBothOldBehaviours(unittest.TestCase):
    """Explicit pair required by the ticket: one test that fails if the
    implementation still resets everything (old dp-160 behaviour), and one
    that fails if it resets nothing (a no-op bug)."""

    def test_fails_if_everything_still_resets(self):
        tracks = [_track(i) for i in range(4)]
        layout = _layout_with_distinct_overlaps(tracks)
        window = _FakeWindow(layout)

        # Move the LAST track to the front -- every pair except one
        # (the original middle pair) changes adjacency.
        reordered = [tracks[3], tracks[0], tracks[1], tracks[2]]
        new_layout = _invalidate(window, reordered)
        pairs = _overlap_map(new_layout)
        # id1-id2 was untouched by this move (both stayed adjacent, same
        # order) -- a blanket reset would zero this and fail the assert.
        self.assertEqual(pairs[("id1", "id2")], 20.0)

    def test_fails_if_nothing_resets(self):
        tracks = [_track(i) for i in range(4)]
        layout = _layout_with_distinct_overlaps(tracks)
        window = _FakeWindow(layout)

        reordered = [tracks[3], tracks[0], tracks[1], tracks[2]]
        new_layout = _invalidate(window, reordered)
        pairs = _overlap_map(new_layout)
        # id3-id0 is a brand-new pair -- a no-op "preserve everything" bug
        # would carry the old id2-id3 (=30.0) or id0-id1 (=10.0) overlap
        # here by position instead of defaulting to 0.
        self.assertEqual(pairs[("id3", "id0")], 0.0)


class TestConsistencyInvariant(unittest.TestCase):

    def test_rebuilt_layout_track_list_always_matches_playlist(self):
        """Hard constraint: the layout must never disagree with the
        playlist after any edit."""
        tracks = [_track(i) for i in range(4)]
        layout = _layout_with_distinct_overlaps(tracks)
        window = _FakeWindow(layout)

        reordered = [tracks[2], tracks[0], tracks[3], tracks[1]]
        new_layout = _invalidate(window, reordered)
        self.assertEqual(
            [t.filepath for t in new_layout.tracks],
            [t["filepath"] for t in reordered],
        )
        self.assertEqual(
            [t.track_id for t in new_layout.tracks],
            [t["id"] for t in reordered],
        )


class TestPersistenceRoundTrip(unittest.TestCase):

    def test_track_id_survives_to_dict_from_dict(self):
        tracks = [_track(i) for i in range(3)]
        layout = _layout_with_distinct_overlaps(tracks)
        d = layout.to_dict()
        reloaded = CrossfadeLayout.from_dict(d)
        self.assertEqual(
            [t.track_id for t in reloaded.tracks],
            [t.track_id for t in layout.tracks],
        )
        self.assertEqual(
            [ov.duration for ov in reloaded.overlaps],
            [ov.duration for ov in layout.overlaps],
        )

    def test_legacy_layout_without_track_id_falls_back_to_full_reset(self):
        """A layout persisted before dp-236 has no track_id on any track
        (from_dict defaults to None). Since no pair can ever match by id,
        the next edit resets it fully -- exactly dp-160's original
        behaviour, not a crash or a silent mismatch."""
        legacy_dict = {
            "tracks": [
                {"filepath": "/music/t0.wav", "duration": 100.0},
                {"filepath": "/music/t1.wav", "duration": 100.0},
                {"filepath": "/music/t2.wav", "duration": 100.0},
            ],
            "overlaps": [{"duration": 10.0}, {"duration": 20.0}],
        }
        layout = CrossfadeLayout.from_dict(legacy_dict)
        self.assertIsNone(layout.tracks[0].track_id)
        window = _FakeWindow(layout)

        tracks = [_track(0), _track(1), _track(2)]
        reordered = [tracks[1], tracks[0], tracks[2]]
        new_layout = _invalidate(window, reordered)
        pairs = _overlap_map(new_layout)
        # No id match possible against the legacy layout -- everything
        # defaults to 0, including the id0-id2 pair a preserving
        # implementation would otherwise have kept alive by position.
        self.assertEqual(pairs[("id1", "id0")], 0.0)
        self.assertEqual(pairs[("id0", "id2")], 0.0)


if __name__ == "__main__":
    unittest.main()

"""dp-237/dp-238: per-row track identity.

Two playlist rows pointing at the same file used to share one identity for
markers/cues/colour/end-action/volume, because every one of those settings
was keyed purely by filepath. dp-237 introduced row-keyed maps but kept a
"seed on add" layer (a new row inherited state from the legacy file-keyed
maps); dp-238 retired that layer entirely after live testing showed it
made "clear markers" non-permanent -- clearing removed the row entry but
left the file-keyed seed, which resurrected on the next add of that file.
Rows are now STRICTLY independent: nothing seeds a new row from a file.

Also covers the hard guarantees from both tickets:
  (B, dp-237) row-keyed settings entries are garbage-collected, not left to
      pile up forever (100 add/remove cycles return settings to baseline).
  (dp-238) migration: an existing playlist saved before dp-237 (or before
      dp-238) loses no saved state on load, and the migration is idempotent.
  (dp-238) discrimination: the four stale readers dp-237 left keying on
      filepath must actually read the row-keyed maps now.

Plain unittest, no pytest dependency:
    ./venv/Scripts/python.exe -m unittest discover tests
"""

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import core.deck_engine as deck_engine_module
import core.playlist as playlist_module
import core.track_identity as track_identity_module
from core.playlist import Playlist
from core.track_identity import (
    ROW_KEY_MAP,
    gc_row_state,
    migrate_legacy_track_state,
    remove_track_row_state,
)


class _FakeSettings:
    """Minimal settings stand-in: get(key, default)/set(key, value) over a
    backing dict, and a no-op save() -- mirrors _FakeSettings in
    test_deck_engine.py. Patched into every module that imports `settings`
    by name (core.playlist, core.track_identity, core.deck_engine) so these
    tests never touch config/sava_settings.json."""

    def __init__(self, seed=None):
        self._data = dict(seed or {})

    def get(self, key, default=None):
        import copy
        return copy.deepcopy(self._data.get(key, default))

    def set(self, key, value):
        self._data[key] = value

    def save(self):
        pass


def _patched_settings(seed=None):
    fake = _FakeSettings(seed)
    patchers = [
        mock.patch.object(playlist_module, "settings", fake),
        mock.patch.object(track_identity_module, "settings", fake),
        mock.patch.object(deck_engine_module, "settings", fake),
    ]
    for p in patchers:
        p.start()
    return fake, patchers


def _stop(patchers):
    for p in patchers:
        p.stop()


class TestNoSeedingOnAdd(unittest.TestCase):
    """dp-238: a brand-new row never inherits legacy file-keyed state --
    the seed layer that made "clear markers" non-permanent is gone."""

    def setUp(self):
        self.fake, self._patchers = _patched_settings(
            seed={"track_start_markers": {"song.wav": 5.0}}
        )

    def tearDown(self):
        _stop(self._patchers)

    def test_new_row_does_not_inherit_legacy_file_keyed_state(self):
        pl = Playlist()
        pl.add_files(["song.wav"])
        row_id = pl.tracks[0]["id"]

        self.assertNotIn(row_id, self.fake.get("row_start_markers", {}))
        # The legacy value is untouched -- not consulted, not deleted.
        self.assertEqual(
            self.fake.get("track_start_markers", {}).get("song.wav"), 5.0
        )


class TestClearThenReAddDoesNotResurrect(unittest.TestCase):
    """The actual user-reported bug (dp-238): set a marker, clear it,
    remove the row, re-add the same file -- the marker must not come
    back. Under the old seed-on-add model it did, because clearing only
    touched the row-keyed entry and left the file-keyed seed in place."""

    def setUp(self):
        self.fake, self._patchers = _patched_settings()

    def tearDown(self):
        _stop(self._patchers)

    def test_clear_then_readd_same_file_stays_cleared(self):
        pl = Playlist()
        pl.add_files(["song.wav"])
        row_id = pl.tracks[0]["id"]

        # Set a start marker on the row (mirrors DeckEngine.set_start_marker
        # writing into row_start_markers via _row_or_file_key).
        self.fake.set("row_start_markers", {row_id: 5.0})
        self.assertEqual(self.fake.get("row_start_markers", {}).get(row_id), 5.0)

        # Clear it (mirrors ui.main_window._on_clear_track_markers deleting
        # the row-keyed entry) and remove the row from the playlist.
        cleared = self.fake.get("row_start_markers", {})
        cleared.pop(row_id, None)
        self.fake.set("row_start_markers", cleared)
        pl.remove(0)

        # Re-add the same file -- a fresh row, fresh id, no inherited state.
        pl.add_files(["song.wav"])
        new_row_id = pl.tracks[0]["id"]
        self.assertNotEqual(new_row_id, row_id)
        self.assertNotIn(new_row_id, self.fake.get("row_start_markers", {}))


class TestRowIndependence(unittest.TestCase):
    """A row owns its state independently of any other row of the same
    file -- unaffected by the seed layer's retirement."""

    def setUp(self):
        self.fake, self._patchers = _patched_settings()

    def tearDown(self):
        _stop(self._patchers)

    def test_two_rows_of_the_same_file_are_independent(self):
        pl = Playlist()
        pl.add_files(["song.wav", "song.wav"])
        self.assertEqual(pl.count, 2)
        id_a, id_b = pl.tracks[0]["id"], pl.tracks[1]["id"]
        self.assertNotEqual(id_a, id_b)  # distinct identities from the start

        pl.set_track_color(0, "red")

        row_colors = self.fake.get("row_colors", {})
        self.assertEqual(row_colors.get(id_a), "red")
        self.assertNotIn(id_b, row_colors)  # DISCRIMINATION CHECK below

    def test_discrimination_clearing_row_0_does_not_touch_row_1(self):
        """A handler that ignored the row index and always wrote to
        "the first row" (or the last-seen row) would pass a naive
        independence check but fail this: colour set on row 1 must be
        readable back from row 1, and row 0 must stay at its own (seeded)
        value the whole time."""
        pl = Playlist()
        pl.add_files(["song.wav", "song.wav"])
        pl.set_track_color(0, "blue")
        pl.set_track_color(1, "green")

        self.assertEqual(pl.tracks[0]["color"], "blue")
        self.assertEqual(pl.tracks[1]["color"], "green")

        # Re-set row 0 again -- must not perturb row 1.
        pl.set_track_color(0, "yellow")
        self.assertEqual(pl.tracks[0]["color"], "yellow")
        self.assertEqual(pl.tracks[1]["color"], "green")

    def test_removing_one_duplicate_row_does_not_clear_the_others_state(self):
        pl = Playlist()
        pl.add_files(["song.wav", "song.wav"])
        pl.set_track_color(0, "red")
        pl.set_track_color(1, "blue")
        id_b = pl.tracks[1]["id"]

        pl.remove(0)

        self.assertEqual(pl.count, 1)
        self.assertEqual(pl.tracks[0]["id"], id_b)
        self.assertEqual(pl.tracks[0]["color"], "blue")
        self.assertEqual(self.fake.get("row_colors", {}).get(id_b), "blue")


class TestIdSurvivesReorderAndReload(unittest.TestCase):
    def setUp(self):
        self.fake, self._patchers = _patched_settings()

    def tearDown(self):
        _stop(self._patchers)

    def test_id_survives_reorder_and_save_reload(self):
        pl = Playlist()
        pl.add_files(["a.wav", "b.wav", "c.wav"])
        ids_before = [t["id"] for t in pl.tracks]

        pl.move(0, 2)  # a.wav -> end
        self.assertEqual(
            [t["filepath"] for t in pl.tracks], ["b.wav", "c.wav", "a.wav"]
        )
        self.assertEqual([t["id"] for t in pl.tracks], [
            ids_before[1], ids_before[2], ids_before[0]
        ])

        pl.save()

        reloaded = Playlist()
        self.assertEqual(
            [t["filepath"] for t in reloaded.tracks], ["b.wav", "c.wav", "a.wav"]
        )
        self.assertEqual(
            [t["id"] for t in reloaded.tracks],
            [ids_before[1], ids_before[2], ids_before[0]],
        )


class TestGarbageCollection(unittest.TestCase):
    """Guarantee (B): row-keyed settings entries never pile up forever."""

    def setUp(self):
        self.fake, self._patchers = _patched_settings()

    def tearDown(self):
        _stop(self._patchers)

    def _row_map_sizes(self):
        return {k: len(self.fake.get(k, {})) for k in ROW_KEY_MAP.values()}

    def test_100_add_remove_cycles_return_settings_to_baseline_size(self):
        pl = Playlist()
        baseline = self._row_map_sizes()
        self.assertEqual(baseline, {k: 0 for k in ROW_KEY_MAP.values()})

        for i in range(100):
            pl.add_files([f"cycle_{i}.wav"])
            pl.set_track_color(0, "red")  # give it row-keyed state to GC
            pl.remove(0)

        self.assertEqual(pl.count, 0)
        self.assertEqual(self._row_map_sizes(), baseline)

    def test_gc_row_state_prunes_ids_not_in_the_live_set(self):
        self.fake.set("row_colors", {"stale-id": "red", "live-id": "blue"})
        gc_row_state(["live-id"])

        self.assertEqual(self.fake.get("row_colors", {}), {"live-id": "blue"})

    def test_load_prunes_row_state_stranded_by_a_crashed_prior_session(self):
        """A row id in a row-keyed map with no corresponding playlist entry
        (e.g. the app crashed before a clean remove()) must not linger
        forever -- Playlist() prunes it at construction."""
        self.fake.set("row_colors", {"orphan-id": "red"})
        Playlist()

        self.assertEqual(self.fake.get("row_colors", {}), {})


class TestLegacyMigration(unittest.TestCase):
    """A pre-dp-237 settings.json (filepath-keyed only, no ids saved) must
    upgrade without losing any saved marker/cue/colour/end-action/volume."""

    def setUp(self):
        self.fake, self._patchers = _patched_settings(seed={
            "last_playlist": ["old.wav"],  # legacy: bare filepath list
            "track_start_markers": {"old.wav": 3.0},
            "track_end_markers": {"old.wav": 30.0},
            "cue_points": {"old.wav": [1.0, 2.0]},
            "track_colors": {"old.wav": "purple"},
            "track_end_actions": {"old.wav": "loop"},
            "track_volumes": {"old.wav": 60},
        })

    def tearDown(self):
        _stop(self._patchers)

    def test_legacy_settings_file_loses_nothing_on_load(self):
        pl = Playlist()

        self.assertEqual(pl.count, 1)
        row_id = pl.tracks[0]["id"]
        self.assertIsNotNone(row_id)

        # Every value survived and is reachable from the new row.
        self.assertEqual(
            self.fake.get("row_start_markers", {}).get(row_id), 3.0
        )
        self.assertEqual(
            self.fake.get("row_end_markers", {}).get(row_id), 30.0
        )
        self.assertEqual(
            self.fake.get("row_cue_points", {}).get(row_id), [1.0, 2.0]
        )
        self.assertEqual(pl.tracks[0]["color"], "purple")
        self.assertEqual(pl.tracks[0]["end_action"], "loop")
        self.assertEqual(
            self.fake.get("row_volumes", {}).get(row_id), 60
        )

        # The legacy maps are untouched -- never deleted, left as inert
        # history (dp-238). Nothing reads them anymore; a file added again
        # later will NOT inherit from them (see TestNoSeedingOnAdd).
        self.assertEqual(self.fake.get("track_start_markers", {}), {"old.wav": 3.0})


class TestMigrationIdempotentAndNonDestructive(unittest.TestCase):
    """dp-238: migrate_legacy_track_state must be safe to call more than
    once (loading the playlist twice must not double-apply or diverge) and
    must never clobber row-state a row already has of its own."""

    def test_running_migration_twice_is_a_no_op_the_second_time(self):
        fake, patchers = _patched_settings(
            seed={"track_end_markers": {"a.wav": 9.0}}
        )
        try:
            migrate_legacy_track_state([("id1", "a.wav")])
            first = fake.get("row_end_markers", {})
            migrate_legacy_track_state([("id1", "a.wav")])
            second = fake.get("row_end_markers", {})
            self.assertEqual(first, {"id1": 9.0})
            self.assertEqual(first, second)
        finally:
            _stop(patchers)

    def test_migration_never_overwrites_existing_row_state(self):
        fake, patchers = _patched_settings(seed={
            "track_end_markers": {"a.wav": 9.0},
            "row_end_markers": {"id1": 1.0},  # row already owns a value
        })
        try:
            migrate_legacy_track_state([("id1", "a.wav")])
            self.assertEqual(fake.get("row_end_markers", {}).get("id1"), 1.0)
        finally:
            _stop(patchers)


class TestDiscriminationNoFileKeyedReadsRemain(unittest.TestCase):
    """dp-238's root cause: dp-237 moved writes to row-keyed maps but left
    four readers keying on filepath, so they silently read empty maps. A
    grep-based check over the exact stale-reader sites named in the ticket
    is what would have caught that at review time -- this fails the moment
    either file goes back to reading a file-keyed map for per-track
    state."""

    def _source(self, relpath):
        path = Path(__file__).resolve().parent.parent / relpath
        return path.read_text(encoding="utf-8")

    def test_preview_widget_reads_row_keyed_maps_not_file_keyed(self):
        src = self._source("ui/preview_waveform_widget.py")
        for stale in ("cue_points", "track_end_markers", "track_start_markers"):
            self.assertNotIn(
                f'"{stale}"',
                src,
                f'{stale} is a retired file-keyed map -- '
                f'ui/preview_waveform_widget.py must not read it for '
                f'per-track state',
            )
        for expected in ("row_cue_points", "row_end_markers", "row_start_markers"):
            self.assertIn(f'"{expected}"', src)

    def test_crossfade_model_reads_row_keyed_end_markers_not_file_keyed(self):
        src = self._source("core/crossfade_model.py")
        self.assertNotIn('"track_end_markers"', src)
        self.assertIn('"row_end_markers"', src)


class TestEngineRowKeyedMarkersAndCues(unittest.TestCase):
    """Engine-level: a deck carrying a track_id reads/writes row-keyed
    settings; a deck without one (direct load(), no playlist) falls back to
    the legacy file-keyed maps -- unchanged pre-dp-237 behaviour."""

    def setUp(self):
        self.fake, self._patchers = _patched_settings()

    def tearDown(self):
        _stop(self._patchers)

    def test_row_or_file_key_helper(self):
        from core.deck_engine import _row_or_file_key

        self.assertEqual(
            _row_or_file_key("rowid", "fp.wav", "track_end_markers"),
            ("row_end_markers", "rowid"),
        )
        self.assertEqual(
            _row_or_file_key(None, "fp.wav", "track_end_markers"),
            ("track_end_markers", "fp.wav"),
        )


if __name__ == "__main__":
    unittest.main()

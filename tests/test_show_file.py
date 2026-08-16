"""dp-246: SAVA show file (.savashow) export/import.

Covers every acceptance criterion from the ticket, asserting on VALUES
(restored cue positions, marker times, ids, overlap durations, warning
content) rather than merely on row counts or "no exception raised":

  - cues/markers/volume/colour/end-action/crossfade all round-trip
    (test_export_import_roundtrip_restores_every_attribute)
  - paths resolve relative to the show file, portable across a folder
    move -- exercised with pytest's tmp_path, not this machine's paths
    (test_show_folder_is_portable_when_moved)
  - a track missing from the show folder is skipped, the rest of the
    show still loads, and the transition spanning it drops to zero
    overlap rather than inheriting either half's fade
    (test_missing_middle_track_is_skipped_and_crossfade_drops_to_zero)
  - importing the same show twice mints a fresh id for the second copy
    and copies (not aliases) its state
    (test_importing_same_show_twice_does_not_alias_state)
  - export warns (by name), never blocks, when a track lives outside
    the destination folder
    (test_export_warns_by_name_for_tracks_outside_the_show_folder)
  - import warns (by name), never blocks, when a track file is missing
    (test_missing_middle_track_is_skipped_and_crossfade_drops_to_zero,
    test_importing_same_show_twice_does_not_alias_state covers the
    plain-missing-file message content too)

The first four are plain core-layer tests (settings patched the same way
tests/test_track_identity.py does -- no Qt). The crossfade/zero-overlap
case is UI-owned (ui/main_window.py._on_import_show reusing dp-236's
_invalidate_crossfade_layout_if_stale), so that one test drives a real
MainWindow under the offscreen Qt platform, following the pattern in
tests/test_playlist_clear_markers.py.
"""

import copy
import json
import os
import sys
import wave
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

import core.crossfade_model as crossfade_model_module
import core.deck_engine as deck_engine_module
import core.playlist as playlist_module
import core.track_identity as track_identity_module
from core.crossfade_model import CrossfadeLayout
from core.playlist import Playlist


def _make_wav(path: Path, seconds: float = 0.2):
    """A real, tiny, valid WAV file -- enough for Path.exists()/suffix
    checks and for ffprobe/mutagen to run without erroring, without the
    cost of a large fixture."""
    with wave.open(str(path), "w") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(8000)
        f.writeframes(b"\x00\x00" * int(8000 * seconds))


class _FakeSettings:
    """Same shape as tests/test_track_identity.py's stand-in: get/set over
    a backing dict, deep-copied on read -- mirrors the real settings.get()
    contract (mutate-then-set is required) that core/playlist.py relies
    on."""

    def __init__(self, seed=None):
        self._data = dict(seed or {})
        # Records whether save() was called, so a test can assert that state
        # was actually FLUSHED and not merely set in memory -- the real
        # settings object only writes to disk on save() (dp-245 D1).
        self.saved = False

    def get(self, key, default=None):
        return copy.deepcopy(self._data.get(key, default))

    def set(self, key, value):
        self._data[key] = value

    def save(self):
        self.saved = True


@pytest.fixture
def fake_settings():
    fake = _FakeSettings()
    patchers = [
        mock.patch.object(playlist_module, "settings", fake),
        mock.patch.object(track_identity_module, "settings", fake),
        mock.patch.object(deck_engine_module, "settings", fake),
        mock.patch.object(crossfade_model_module, "settings", fake),
    ]
    for p in patchers:
        p.start()
    yield fake
    for p in patchers:
        p.stop()


# ── Core-layer tests (no Qt) ────────────────────────────────────────────────


def test_export_import_roundtrip_restores_every_attribute(fake_settings, tmp_path):
    show_dir = tmp_path / "show"
    show_dir.mkdir()
    a_file = show_dir / "a.wav"
    b_file = show_dir / "b.wav"
    _make_wav(a_file, seconds=5.0)
    _make_wav(b_file, seconds=5.0)

    pl = Playlist()
    pl.add_files([str(a_file), str(b_file)])
    id_a, id_b = pl.tracks[0]["id"], pl.tracks[1]["id"]

    fake_settings.set("row_cue_points", {id_a: [10.0, None, 30.5]})
    fake_settings.set("row_start_markers", {id_a: 2.5})
    fake_settings.set("row_end_markers", {id_a: 45.0})
    fake_settings.set("row_volumes", {id_a: 65})
    pl.set_track_color(0, "#112233")
    pl.set_track_end_action(0, "loop")

    layout = CrossfadeLayout.from_playlist_tracks(pl.tracks)
    layout.set_overlap_duration(0, 3.0)
    layout.save()

    show_path = show_dir / "show.savashow"
    missing = pl.export_show(str(show_path))
    assert missing == []

    # Clearing the playlist also GCs row-keyed settings state -- doesn't
    # matter, the exported values already live in the JSON file, which is
    # exactly the point of the format.
    pl.clear()
    assert pl.count == 0

    missing2, id_map, layout_dict = pl.import_show(str(show_path))
    assert missing2 == []
    assert pl.count == 2

    # No collision (playlist was empty) -- ids are preserved verbatim.
    assert id_map[id_a] == id_a
    assert id_map[id_b] == id_b

    restored_a = pl.tracks[0]
    assert restored_a["id"] == id_a
    assert restored_a["color"] == "#112233"
    assert restored_a["end_action"] == "loop"
    assert fake_settings.get("row_cue_points", {})[id_a] == [10.0, None, 30.5]
    assert fake_settings.get("row_start_markers", {})[id_a] == 2.5
    assert fake_settings.get("row_end_markers", {})[id_a] == 45.0
    assert fake_settings.get("row_volumes", {})[id_a] == 65

    assert layout_dict["overlaps"][0]["duration"] == 3.0
    assert layout_dict["tracks"][0]["track_id"] == id_a
    assert layout_dict["tracks"][1]["track_id"] == id_b


def test_show_folder_is_portable_when_moved(fake_settings, tmp_path):
    """A show folder copied to a different location -- standing in for a
    different drive letter / different machine, since a test can't
    fabricate an actual drive letter -- must still resolve every track,
    because paths are stored relative to the show file itself."""
    origin_dir = tmp_path / "machine_a" / "show"
    origin_dir.mkdir(parents=True)
    track_file = origin_dir / "track.wav"
    _make_wav(track_file)

    pl = Playlist()
    pl.add_files([str(track_file)])
    show_path = origin_dir / "show.savashow"
    assert pl.export_show(str(show_path)) == []

    # Move the whole show folder (copy, since the source stays put in a
    # real move too) to an unrelated location -- a different absolute
    # prefix, standing in for a different drive/machine.
    import shutil

    moved_dir = tmp_path / "machine_b" / "relocated_show"
    shutil.copytree(origin_dir, moved_dir)

    pl2 = Playlist()
    missing = pl2.import_show(str(moved_dir / "show.savashow"))[0]
    assert missing == []
    assert pl2.count == 1
    assert Path(pl2.tracks[0]["filepath"]).resolve() == (moved_dir / "track.wav").resolve()


def test_importing_same_show_twice_does_not_alias_state(fake_settings, tmp_path):
    show_dir = tmp_path / "show"
    show_dir.mkdir()
    a_file = show_dir / "a.wav"
    _make_wav(a_file)

    pl = Playlist()
    pl.add_files([str(a_file)])
    id_a = pl.tracks[0]["id"]
    fake_settings.set("row_cue_points", {id_a: [5.0]})

    show_path = show_dir / "show.savashow"
    pl.export_show(str(show_path))
    pl.clear()

    missing1, id_map1, _ = pl.import_show(str(show_path))
    missing2, id_map2, _ = pl.import_show(str(show_path))
    assert missing1 == [] and missing2 == []

    row_id_first = id_map1[id_a]
    row_id_second = id_map2[id_a]
    # decision 4: the second import's row must not share the first's id...
    assert row_id_first != row_id_second
    # ...or its cue-point storage -- mutating one must not move the other.
    cues = fake_settings.get("row_cue_points", {})
    assert cues[row_id_first] == [5.0]
    assert cues[row_id_second] == [5.0]
    cues[row_id_first][0] = 999.0
    fake_settings.set("row_cue_points", cues)
    assert fake_settings.get("row_cue_points", {})[row_id_second] == [5.0]
    assert pl.count == 2


def test_export_warns_by_name_for_tracks_outside_the_show_folder(fake_settings, tmp_path):
    show_dir = tmp_path / "show"
    show_dir.mkdir()
    outside_dir = tmp_path / "elsewhere"
    outside_dir.mkdir()
    stray_file = outside_dir / "stray.wav"
    _make_wav(stray_file)

    pl = Playlist()
    pl.add_files([str(stray_file)])
    show_path = show_dir / "show.savashow"

    missing = pl.export_show(str(show_path))
    assert missing == ["stray"]  # title defaults to the file stem
    # Export never blocks -- the file is written regardless of the warning.
    assert show_path.exists()
    data = json.loads(show_path.read_text(encoding="utf-8"))
    assert len(data["tracks"]) == 1


def test_import_warns_by_name_for_a_missing_track(fake_settings, tmp_path):
    show_dir = tmp_path / "show"
    show_dir.mkdir()
    a_file = show_dir / "keep.wav"
    b_file = show_dir / "gone.wav"
    _make_wav(a_file)
    _make_wav(b_file)

    pl = Playlist()
    pl.add_files([str(a_file), str(b_file)])
    show_path = show_dir / "show.savashow"
    pl.export_show(str(show_path))

    b_file.unlink()  # simulate a deleted track before the show is opened
    pl.clear()

    missing, _, _ = pl.import_show(str(show_path))
    assert missing == ["gone"]
    assert pl.count == 1
    assert Path(pl.tracks[0]["filepath"]).stem == "keep"


# ── UI-layer test: crossfade drops to zero across a missing track ──────────


_app = None


def setup_module(module):
    from PyQt6.QtWidgets import QApplication

    global _app
    _app = QApplication.instance() or QApplication(sys.argv)


def test_missing_middle_track_is_skipped_and_crossfade_drops_to_zero(tmp_path):
    """dp-246 decision 3: with tracks A, B, C authored with real overlaps
    on both sides of B, deleting B before import must load A and C but
    give the A->C transition ZERO overlap -- never B's leftover fade in
    either direction. This is the existing dp-236
    _invalidate_crossfade_layout_if_stale ordered-pair-identity mechanism,
    reused via ui.main_window._on_import_show, not new logic -- so this
    test exercises the real UI slot, not a hand-rolled substitute."""
    from config.settings import settings
    from core.playlist import playlist
    from ui.main_window import MainWindow

    show_dir = tmp_path / "show"
    show_dir.mkdir()
    a_file, b_file, c_file = (show_dir / n for n in ("a.wav", "b.wav", "c.wav"))
    for f in (a_file, b_file, c_file):
        _make_wav(f, seconds=2.0)

    # Snapshot every real-settings key this test touches so it can be
    # restored afterward -- same discipline as
    # tests/test_playlist_clear_markers.py's tearDown.
    saved_tracks = list(playlist._tracks)
    saved_settings = {
        k: settings.get(k, {})
        for k in (
            "row_cue_points", "row_start_markers", "row_end_markers",
            "row_volumes", "row_colors", "row_end_actions", "crossfade_layout",
        )
    }

    window = MainWindow()
    try:
        playlist._tracks = []
        playlist.add_files([str(a_file), str(b_file), str(c_file)])

        layout = CrossfadeLayout.from_playlist_tracks(playlist.tracks)
        layout.set_overlap_duration(0, 1.0)  # A -> B
        layout.set_overlap_duration(1, 1.0)  # B -> C
        layout.save()
        window._crossfade_layout = CrossfadeLayout.load()

        show_path = show_dir / "show.savashow"
        assert playlist.export_show(str(show_path)) == []

        b_file.unlink()  # the missing-middle-track scenario
        playlist._tracks = []
        window._crossfade_layout = None

        # Call the REAL slot body. `_apply_imported_show` is everything
        # _on_import_show does once the file dialog has returned a path,
        # split out precisely so a test can drive it. An earlier version
        # of this test re-implemented that logic inline instead -- which
        # tests the copy, not the shipped code, and would stay green
        # while the real slot rotted.
        missing = window._apply_imported_show(str(show_path))
        assert missing == ["b"]
        assert playlist.count == 2

        rebuilt = window._crossfade_layout
        assert [t.filepath for t in rebuilt.tracks] == [
            str(a_file.resolve()), str(c_file.resolve())
        ]
        assert len(rebuilt.overlaps) == 1
        # The load-bearing assertion: A->C inherits NEITHER A->B's 1.0s
        # fade-out nor B->C's 1.0s fade-in. It is a pair that was never
        # authored, so it must come back at the zero default.
        assert rebuilt.overlaps[0].duration == 0.0
    finally:
        window.close()
        playlist._tracks = saved_tracks
        for k, v in saved_settings.items():
            settings.set(k, v)


# ── Review-pass regressions (added by the review, not the initial build) ────
#
# Each of these covers a real defect found by probing the first implementation
# rather than by reading it. They are here because the original test set was
# green while all four were broken -- a reminder that a passing suite bounds
# what was CHECKED, not what works.


def test_cross_drive_export_is_flagged_as_missing(fake_settings, tmp_path, monkeypatch):
    """A track on a different drive from the show file has NO relative form,
    so export stores a bare filename that will not resolve.

    That must warn. Originally it did not: the bare filename does not start
    with "..", and the source file does exist (over on the other drive), so
    both existing checks passed and export wrote a silently broken show. The
    operator would only find out at load time, on the show machine -- exactly
    the failure the export-side warning exists to prevent.

    A real second drive can't be assumed on a test machine, so the ValueError
    that os.path.relpath raises across drives is simulated directly.
    """
    show_dir = tmp_path / "show"
    show_dir.mkdir()
    other_dir = tmp_path / "other"
    other_dir.mkdir()
    track = other_dir / "song.wav"
    _make_wav(track)

    pl = Playlist()
    pl._tracks = []
    pl.add_files([str(track)])

    def _raise_cross_drive(*_args, **_kwargs):
        raise ValueError("path is on mount 'D:', start on mount 'E:'")

    monkeypatch.setattr(playlist_module.os.path, "relpath", _raise_cross_drive)

    missing = pl.export_show(str(show_dir / "s.savashow"))

    assert missing == ["song"]


def test_import_persists_restored_state_immediately(fake_settings, tmp_path):
    """Importing a show must hit the disk, not just memory.

    settings.set() only mutates an in-memory dict (dp-245 D1), so without an
    explicit save() every restored cue and marker was lost if SAVA was killed
    before a clean exit -- while the .savashow file looked like it had worked.
    """
    show_dir = tmp_path / "show"
    show_dir.mkdir()
    track = show_dir / "a.wav"
    _make_wav(track)

    pl = Playlist()
    pl._tracks = []
    pl.add_files([str(track)])
    fake_settings.set("row_cue_points", {pl.tracks[0]["id"]: [1.0]})
    show_path = show_dir / "s.savashow"
    pl.export_show(str(show_path))
    pl.clear()

    fake_settings.saved = False
    pl.import_show(str(show_path))

    assert fake_settings.saved is True


@pytest.mark.parametrize(
    "content, why",
    [
        ("this is not json at all", "plain text"),
        ('{"tracks": []}', "JSON but no format marker"),
        ('{"format": "something-else", "tracks": []}', "wrong format marker"),
        ('{"format": "sava-show"}', "no track list"),
    ],
)
def test_a_file_that_is_not_a_show_raises_a_clean_error(
    fake_settings, tmp_path, content, why
):
    """Picking the wrong file in the dialog is an ORDINARY mistake. It must
    raise ShowFileError -- one predictable type the UI turns into a sentence --
    not a raw JSONDecodeError that unwinds out of the Qt slot into the
    excepthook, where the user sees nothing and the menu item appears to do
    nothing at all."""
    bad = tmp_path / "bad.savashow"
    bad.write_text(content, encoding="utf-8")

    pl = Playlist()
    pl._tracks = []
    with pytest.raises(playlist_module.ShowFileError):
        pl.import_show(str(bad))


def test_a_valid_show_still_imports_after_the_format_check(fake_settings, tmp_path):
    """Guard against the validation above being too strict -- the happy path
    must still work. Without this, a typo in the format check would show up
    only as 'import silently does nothing'."""
    show_dir = tmp_path / "show"
    show_dir.mkdir()
    track = show_dir / "a.wav"
    _make_wav(track)

    pl = Playlist()
    pl._tracks = []
    pl.add_files([str(track)])
    show_path = show_dir / "s.savashow"
    pl.export_show(str(show_path))
    pl.clear()

    missing, id_map, _ = pl.import_show(str(show_path))

    assert missing == []
    assert pl.count == 1


def test_second_import_remaps_ids_and_keeps_its_own_crossfade(tmp_path):
    """Importing the SAME show twice must give the second copy its own
    working crossfade, attached to its own freshly-minted ids.

    This is the test that actually exercises `_apply_imported_show`'s id
    remap. The single-import case cannot: with an empty playlist there are no
    id collisions, so `id_map` is the identity mapping and deleting the remap
    entirely changes nothing (verified -- the remap was sabotaged and the
    single-import test still passed). Only a collision makes the remap
    load-bearing.

    It also pins the merge path: a show imported on top of an existing layout
    must keep BOTH groups' overlaps, with only the seam between them at zero.
    """
    from config.settings import settings
    from core.playlist import playlist
    from ui.main_window import MainWindow

    show_dir = tmp_path / "show"
    show_dir.mkdir()
    a_file, b_file = (show_dir / n for n in ("a.wav", "b.wav"))
    for f in (a_file, b_file):
        _make_wav(f, seconds=2.0)

    saved_tracks = list(playlist._tracks)
    saved_settings = {
        k: settings.get(k, {})
        for k in (
            "row_cue_points", "row_start_markers", "row_end_markers",
            "row_volumes", "row_colors", "row_end_actions", "crossfade_layout",
        )
    }

    window = MainWindow()
    try:
        playlist._tracks = []
        playlist.add_files([str(a_file), str(b_file)])
        layout = CrossfadeLayout.from_playlist_tracks(playlist.tracks)
        layout.set_overlap_duration(0, 1.5)
        layout.save()

        show_path = show_dir / "show.savashow"
        assert playlist.export_show(str(show_path)) == []

        playlist._tracks = []
        window._crossfade_layout = None

        assert window._apply_imported_show(str(show_path)) == []
        first_ids = [t["id"] for t in playlist.tracks]

        # Second import -- every id in the file now collides with a live row,
        # so import_show mints fresh ids and _apply_imported_show must remap
        # the layout to match them.
        assert window._apply_imported_show(str(show_path)) == []
        all_ids = [t["id"] for t in playlist.tracks]
        second_ids = all_ids[2:]

        assert playlist.count == 4
        assert len(set(all_ids)) == 4, "every row must have a distinct id"
        assert second_ids != first_ids

        rebuilt = window._crossfade_layout
        assert [t.track_id for t in rebuilt.tracks] == all_ids
        durations = [ov.duration for ov in rebuilt.overlaps]
        # first copy's fade | seam between the two copies | second copy's fade
        assert durations == [1.5, 0.0, 1.5]
    finally:
        window.close()
        playlist._tracks = saved_tracks
        for k, v in saved_settings.items():
            settings.set(k, v)

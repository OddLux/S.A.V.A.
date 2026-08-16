"""dp-245: `Playlist._current_index` is a POSITION, so every structural edit
that shifts positions must shift it too.

`remove()` only clamped the index to the new length and `move()` only handled
"the dragged row IS the current row". Any other edit above/across the current
row left the index pointing at a slot that now holds a DIFFERENT track. That
is not cosmetic: `current_index` drives the playlist highlight, the "Track N
of M" counter, `get_track_end_action()` (which decides loop/stop/next at the
end of the playing track) and `peek_next()` (which decides what gets decoded
into the idle deck for the gapless swap). A wrong index therefore plays the
wrong track next.

These tests assert on the track IDENTITY at `current_index`, never on the bare
number -- an assertion on the number alone would pass for a fix that shifted
the index correctly by accident while the list moved the other way.
"""

import pytest

from core.playlist import Playlist


def _playlist(names):
    """A Playlist with `names` as rows, bypassing disk/mutagen entirely.

    Constructed via __new__: Playlist.__init__ reads settings and shells out
    to ffprobe per row, none of which this pure index arithmetic needs.
    """
    pl = Playlist.__new__(Playlist)
    pl._tracks = [{"filepath": n, "id": n, "title": n} for n in names]
    pl._current_index = 0
    pl._shuffle = False
    pl._repeat = "none"
    pl._shuffle_order = list(range(len(names)))
    pl.on_track_changed = None
    pl.on_playlist_changed = None
    pl.on_files_scanned = None
    return pl


def _current_name(pl):
    cur = pl.current
    return cur["filepath"] if cur else None


# -- remove() -----------------------------------------------------------


def test_remove_above_current_keeps_the_same_track_current(monkeypatch):
    """The regression: removing a row ABOVE the playing one used to leave
    current_index unmoved, so it silently pointed one row too far down."""
    monkeypatch.setattr("core.playlist.remove_track_row_state", lambda _id: None)
    pl = _playlist(["a", "b", "c", "d"])
    pl._current_index = 2  # "c" is playing

    pl.remove(0)  # remove "a"

    assert _current_name(pl) == "c"
    assert pl._current_index == 1


def test_remove_below_current_leaves_index_alone(monkeypatch):
    monkeypatch.setattr("core.playlist.remove_track_row_state", lambda _id: None)
    pl = _playlist(["a", "b", "c", "d"])
    pl._current_index = 1  # "b" is playing

    pl.remove(3)  # remove "d", below the current row

    assert _current_name(pl) == "b"
    assert pl._current_index == 1


def test_remove_current_row_promotes_the_row_below_it(monkeypatch):
    monkeypatch.setattr("core.playlist.remove_track_row_state", lambda _id: None)
    pl = _playlist(["a", "b", "c"])
    pl._current_index = 1  # "b" is playing

    pl.remove(1)

    assert _current_name(pl) == "c"


def test_remove_last_row_while_it_is_current_clamps(monkeypatch):
    monkeypatch.setattr("core.playlist.remove_track_row_state", lambda _id: None)
    pl = _playlist(["a", "b"])
    pl._current_index = 1

    pl.remove(1)

    assert _current_name(pl) == "a"
    assert pl._current_index == 0


def test_remove_only_row_leaves_no_current(monkeypatch):
    monkeypatch.setattr("core.playlist.remove_track_row_state", lambda _id: None)
    pl = _playlist(["a"])

    pl.remove(0)

    assert pl.current is None


# -- move() -------------------------------------------------------------


@pytest.mark.parametrize(
    "from_index, to_index, current_index",
    [
        (from_index, to_index, current_index)
        for from_index in range(4)
        for to_index in range(4)
        for current_index in range(4)
    ],
)
def test_move_always_keeps_the_same_track_current(
    from_index, to_index, current_index
):
    """Exhaustive over every (from, to, current) triple on a 4-row playlist.

    The invariant is stated in terms of the track OBJECT, not the index:
    whatever was current before the move must still be current after it,
    whichever rows moved. That is the property the old code violated and the
    one a future refactor must not break.
    """
    names = ["a", "b", "c", "d"]
    pl = _playlist(names)
    pl._current_index = current_index
    expected = names[current_index]

    pl.move(from_index, to_index)

    assert pl._tracks[pl._current_index]["filepath"] == expected


def test_move_dragged_row_is_the_current_one():
    """The one case the old code did handle -- pinned so the rewrite keeps
    it working."""
    pl = _playlist(["a", "b", "c"])
    pl._current_index = 0  # "a" is playing

    pl.move(0, 2)

    assert _current_name(pl) == "a"
    assert pl._current_index == 2

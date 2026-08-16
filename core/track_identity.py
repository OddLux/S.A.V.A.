"""dp-237/dp-238: per-row track identity.

Every playlist ROW gets a stable id (uuid4 hex) minted when the row is
added (core/playlist.py). Per-track settings (markers, cues, colour, end
action, volume) used to be keyed purely by filepath, so two rows pointing
at the same file shared one identity -- editing a marker on one copy
changed both.

dp-237 introduced row-keyed maps but kept a "seed on add" layer: every new
row inherited state from the legacy file-keyed maps, and the file-keyed
maps were never retired. That meant clearing a row's markers left the
file-keyed seed in place, and re-adding the same file resurrected the
"cleared" markers on a fresh row -- worse than either a purely row-keyed
or a purely file-keyed model.

dp-238 retires the seed layer. Markers, cues, colour, end-action and
volume are now STRICTLY per-row: nothing seeds a new row from a file
anymore, so markers no longer follow a file into a new playlist (accepted
capability loss). The only remaining use of the six legacy file-keyed
maps is `migrate_legacy_track_state`, a one-time upgrade path that copies
a row's state across ONCE when an existing playlist entry (loaded from
`last_playlist`) has no row-state of its own yet but its filepath does.
The file-keyed maps themselves are left in settings afterward as inert
history rather than deleted -- see that function's docstring for why.

Row-keyed entries MUST be garbage-collected (removing a row deletes its
row-keyed entries; loading prunes anything not in the live playlist) or
sava_settings.json grows without bound over an add/remove-heavy session.
"""

import uuid

from config.settings import settings

# file-keyed legacy map -> row-keyed map, one pair per per-track setting.
ROW_KEY_MAP = {
    "track_start_markers": "row_start_markers",
    "track_end_markers":   "row_end_markers",
    "cue_points":           "row_cue_points",
    "track_colors":         "row_colors",
    "track_end_actions":    "row_end_actions",
    "track_volumes":        "row_volumes",
}


def new_track_id() -> str:
    """Mint a stable id for a newly added playlist row."""
    return uuid.uuid4().hex


def migrate_legacy_track_state(id_filepath_pairs):
    """One-time upgrade path (dp-238): for each (track_id, filepath) pair
    in the playlist being restored, copy any pre-dp-237 file-keyed state
    into the row-keyed map, but ONLY if the row doesn't already own state
    of its own there. Must run before anything reads a row-keyed map for
    these rows (core/playlist.py._load_last_playlist calls it before
    building any row).

    Idempotent: a row that already has a row-keyed entry (from a previous
    migration, or normal per-row use) is left untouched on every later
    call -- the `if track_id in row_map: continue` guard is what makes
    re-running this safe.

    Additive only -- never overwrites, never deletes. The six file-keyed
    maps are NOT cleared after migrating; they are left in settings as
    inert history. Two reasons: (1) a file can carry state for a track
    that isn't in the CURRENT playlist at migration time -- deleting the
    file-keyed maps would destroy that data even though nothing migrated
    it, whereas leaving them costs nothing but disk bytes; (2) an unread
    dead key is strictly safer than a deleted user marker if this function
    ever has a bug. Nothing in the codebase reads them anymore once this
    migration runs -- see the discrimination check in
    tests/test_track_identity.py.
    """
    for track_id, filepath in id_filepath_pairs:
        if not track_id or not filepath:
            continue
        for file_key, row_key in ROW_KEY_MAP.items():
            file_map = settings.get(file_key, {})
            if filepath not in file_map:
                continue
            row_map = settings.get(row_key, {})
            if track_id in row_map:
                continue
            row_map[track_id] = file_map[filepath]
            settings.set(row_key, row_map)


def remove_track_row_state(track_id: str):
    """Delete `track_id`'s entries from every row-keyed map. Called when a
    playlist row is removed, so row-keyed state does not pile up forever."""
    if not track_id:
        return
    for row_key in ROW_KEY_MAP.values():
        row_map = settings.get(row_key, {})
        if track_id in row_map:
            del row_map[track_id]
            settings.set(row_key, row_map)


def gc_row_state(live_ids):
    """Prune every row-keyed map down to `live_ids`. Called after loading
    the playlist so ids stranded by a previous session that crashed/exited
    without a clean remove() don't linger forever."""
    live = set(live_ids)
    for row_key in ROW_KEY_MAP.values():
        row_map = settings.get(row_key, {})
        stale = [rid for rid in row_map if rid not in live]
        if stale:
            for rid in stale:
                del row_map[rid]
            settings.set(row_key, row_map)

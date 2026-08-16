import json
import os
import random
import threading
from pathlib import Path
from mutagen import File as MutagenFile

from config.settings import settings
from core.crossfade_model import CrossfadeLayout
from core.deck_engine import _ffprobe_duration
from core.track_identity import (
    gc_row_state,
    migrate_legacy_track_state,
    new_track_id,
    remove_track_row_state,
)

#: Marker written into every .savashow and required on import (dp-246). A
#: format tag makes "you picked the wrong file" a clean, explainable error
#: rather than a confusing partial import of whatever JSON happened to parse.
SHOW_FORMAT = "sava-show"
SHOW_VERSION = 1
SHOW_EXTENSION = ".savashow"


class ShowFileError(Exception):
    """A .savashow file could not be read or is not a show file.

    Deliberately distinct from the "some tracks were missing" path, which is
    NOT an error: a show with missing audio still loads (decision 2). This is
    only for a file that cannot be interpreted as a show at all."""


SUPPORTED_EXTENSIONS = {
    ".mp3", ".flac", ".wav", ".ogg", ".aiff", ".aif",
    ".aac", ".wma", ".opus", ".m4a"
}


def _read_metadata(filepath: str) -> dict:
    """Extract title, artist, album, duration from audio tags."""
    meta = {
        "title":    Path(filepath).stem,
        "artist":   "Unknown",
        "album":    "Unknown",
        "duration": 0.0,
        "filepath": filepath,
        "color":    None,
        "end_action": "next",  # "next" | "loop" | "stop"
    }
    try:
        mf = MutagenFile(filepath, easy=True)
        if mf is not None:
            if "title"  in mf:
                meta["title"]  = str(mf["title"][0])
            if "artist" in mf:
                meta["artist"] = str(mf["artist"][0])
            if "album"  in mf:
                meta["album"]  = str(mf["album"][0])
            if mf.info and hasattr(mf.info, "length"):
                meta["duration"] = float(mf.info.length)
    except Exception:
        pass

    # dp-234: mutagen reports 0.0 for some WAV files (no duration in the
    # tags it reads) even though the audio decodes and plays fine -- the
    # playlist would show 0:00 until the track was played and the analyzer's
    # real figure silently replaced it. Fall back to ffprobe (the same
    # authoritative source core/deck_engine.py::_read_duration uses for
    # playback, W1) ONLY when mutagen came back empty. Inverted from that
    # method's ffprobe-first order on purpose: mutagen succeeds with no
    # subprocess cost for the vast majority of files, so this keeps a bulk
    # add (drag a folder, import M3U/PLS) from shelling out to ffprobe once
    # per track -- only the zero-duration minority pays for it.
    if not meta["duration"]:
        dur = _ffprobe_duration(filepath)
        if dur is not None:
            meta["duration"] = dur

    return meta


def _new_row(filepath: str, track_id: str = None) -> dict:
    """Build a track dict for a playlist row. `track_id` is None for a
    brand-new row (mints a fresh id -- strictly no state, dp-238); reload
    passes the id restored from `last_playlist` so it survives a
    save/load cycle. Row-keyed state (if any) is read here, but seeding it
    from legacy file-keyed maps is `_load_last_playlist`'s job, once, up
    front -- never this function's."""
    meta = _read_metadata(filepath)
    meta["id"] = track_id or new_track_id()
    meta["color"]      = settings.get("row_colors", {}).get(meta["id"])
    meta["end_action"] = settings.get("row_end_actions", {}).get(meta["id"], "next")
    return meta


class Playlist:
    """
    Manages an ordered list of tracks with shuffle, repeat,
    per-track metadata and colour labels.
    """

    def __init__(self):
        self._tracks        = []   # list of metadata dicts
        self._current_index = -1
        self._shuffle       = settings.get("shuffle", False)
        self._repeat        = settings.get("repeat", "none")  # none | one | all
        self._shuffle_order = []

        # Callbacks
        self.on_track_changed = None   # callback(index, meta)
        self.on_playlist_changed = None
        self.on_files_scanned = None   # callback(metas) — fired off the main thread

        self._load_last_playlist()

    # ── Loading ───────────────────────────────────────────────────────────────

    def add_files(self, filepaths):
        added = 0
        for fp in filepaths:
            if Path(fp).suffix.lower() in SUPPORTED_EXTENSIONS:
                meta = _new_row(fp)
                self._tracks.append(meta)
                added += 1
        if added:
            self._rebuild_shuffle()
            self._notify_playlist_changed()
        return added

    def add_folder(self, folder: str) -> int:
        files = sorted(
            str(p) for p in Path(folder).rglob("*")
            if p.suffix.lower() in SUPPORTED_EXTENSIONS
        )
        return self.add_files(files)

    def add_files_async(self, filepaths=None, folder=None):
        """
        Scan filepaths (or a folder) for metadata on a background thread so
        the Qt main thread stays responsive for large batches. Emits
        on_files_scanned(metas) when done — callers must marshal that back
        onto the main thread (e.g. via a pyqtSignal) and call
        commit_scanned(metas) there before touching the UI or the tracklist.
        """
        threading.Thread(
            target=self._scan_worker,
            args=(filepaths, folder),
            daemon=True,
        ).start()

    def commit_scanned(self, metas: list) -> int:
        """Append background-scanned metadata. Must run on the main thread."""
        if metas:
            self._tracks.extend(metas)
            self._rebuild_shuffle()
            self._notify_playlist_changed()
        return len(metas)

    def _scan_worker(self, filepaths, folder):
        try:
            if folder is not None:
                filepaths = sorted(
                    str(p) for p in Path(folder).rglob("*")
                    if p.suffix.lower() in SUPPORTED_EXTENSIONS
                )
            metas = []
            for fp in filepaths or []:
                if Path(fp).suffix.lower() in SUPPORTED_EXTENSIONS:
                    metas.append(_new_row(fp))
        except Exception as e:
            print(f"[Playlist] background scan failed: {e}")
            metas = []
        if self.on_files_scanned:
            try:
                self.on_files_scanned(metas)
            except Exception as e:
                print(f"[Playlist] on_files_scanned error: {e}")

    def remove(self, index: int):
        if 0 <= index < len(self._tracks):
            removed = self._tracks.pop(index)
            remove_track_row_state(removed.get("id"))
            # `_current_index` is a POSITION, so removing a row ABOVE the
            # current one shifts the current track down by one. Without this
            # the index kept pointing at the same slot, which now holds a
            # different track: the playlist highlight, the "Track N of M"
            # counter and -- via peek_next() -- the preloaded next track all
            # silently referred to the wrong row after removing anything above
            # the one playing. Removing the current row itself keeps the index
            # (the row that slid up into the slot becomes current), clamped
            # below for the case where it was the last row.
            if index < self._current_index:
                self._current_index -= 1
            if self._current_index >= len(self._tracks):
                self._current_index = len(self._tracks) - 1
            self._rebuild_shuffle()
            self._notify_playlist_changed()

    def clear(self):
        for track in self._tracks:
            remove_track_row_state(track.get("id"))
        self._tracks        = []
        self._current_index = -1
        self._shuffle_order = []
        self._notify_playlist_changed()

    def move(self, from_index: int, to_index: int):
        """Reorder tracks by dragging."""
        if from_index == to_index:
            return
        track = self._tracks.pop(from_index)
        self._tracks.insert(to_index, track)
        # A move is a pop + an insert, and BOTH shift positions. Only the
        # "the current track is the one being dragged" case was handled, so
        # dragging any OTHER row across the current one left `_current_index`
        # pointing at a slot that now holds a different track -- same failure
        # as remove(): wrong highlight, wrong track counter, and a wrong
        # peek_next() prediction feeding the idle deck. Mirror the list
        # operation exactly: the pop shifts everything above `from_index`
        # down one, the insert shifts everything at/above `to_index` up one.
        cur = self._current_index
        if cur == from_index:
            cur = to_index
        else:
            if from_index < cur:
                cur -= 1
            if to_index <= cur:
                cur += 1
        self._current_index = cur
        self._rebuild_shuffle()
        self._notify_playlist_changed()

    # ── Navigation ────────────────────────────────────────────────────────────

    def select(self, index: int) -> dict | None:
        if 0 <= index < len(self._tracks):
            self._current_index = index
            self._notify_track_changed()
            return self._tracks[index]
        return None

    def next(self) -> dict | None:
        if not self._tracks:
            return None
        if self._repeat == "one":
            return self.current
        idx = self._next_index(reshuffle_on_wrap=True)
        if idx is None:
            return None
        self._current_index = idx
        self._notify_track_changed()
        return self._tracks[idx]

    def previous(self) -> dict | None:
        if not self._tracks:
            return None
        idx = self._prev_index()
        if idx is None:
            return None
        self._current_index = idx
        self._notify_track_changed()
        return self._tracks[idx]

    def peek_next(self) -> dict | None:
        """Return the track .next() would select, without mutating state.

        Used for speculative pre-fetch (dp-178) and gapless buffer-bridge
        prediction (dp-192) — lets a caller warm/buffer the adjacent track
        ahead of an actual Next press. Pure: passes reshuffle_on_wrap=False so
        peeking never reshuffles _shuffle_order. At the shuffle+repeat=all wrap
        boundary the actual next() reshuffles, so the prediction there is
        best-effort (the caller falls back) — every non-wrap step is exact."""
        if not self._tracks:
            return None
        if self._repeat == "one":
            return self.current
        idx = self._next_index()
        return self._tracks[idx] if idx is not None else None

    def peek_previous(self) -> dict | None:
        """Return the track .previous() would select, without mutating
        state. See peek_next()."""
        if not self._tracks:
            return None
        idx = self._prev_index()
        return self._tracks[idx] if idx is not None else None

    # ── Shuffle / Repeat ──────────────────────────────────────────────────────

    def set_shuffle(self, enabled: bool):
        self._shuffle = enabled
        settings.set("shuffle", enabled)
        self._rebuild_shuffle()

    def set_repeat(self, mode: str):
        """mode: 'none' | 'one' | 'all'"""
        self._repeat = mode
        settings.set("repeat", mode)

    def toggle_shuffle(self):
        self.set_shuffle(not self._shuffle)

    def toggle_repeat(self):
        order = ["none", "one", "all"]
        self.set_repeat(order[(order.index(self._repeat) + 1) % 3])

    @property
    def shuffle(self):
        return self._shuffle

    @property
    def repeat(self):
        return self._repeat

    # ── Per-track metadata ────────────────────────────────────────────────────

    def set_track_color(self, index: int, color: str | None):
        if 0 <= index < len(self._tracks):
            self._tracks[index]["color"] = color
            tid = self._tracks[index].get("id")
            if tid is None:
                self._notify_playlist_changed()
                return
            tc = settings.get("row_colors", {})
            if color:
                tc[tid] = color
            elif tid in tc:
                del tc[tid]
            settings.set("row_colors", tc)
            self._notify_playlist_changed()

    def set_track_end_action(self, index: int, action: str):
        """action: 'next' | 'loop' | 'stop'"""
        if 0 <= index < len(self._tracks):
            if action not in ("next", "loop", "stop"):
                action = "next"
            self._tracks[index]["end_action"] = action
            tid = self._tracks[index].get("id")
            if tid is None:
                self._notify_playlist_changed()
                return
            ea = settings.get("row_end_actions", {})
            ea[tid] = action
            settings.set("row_end_actions", ea)
            self._notify_playlist_changed()

    def get_track_end_action(self, index: int) -> str:
        if 0 <= index < len(self._tracks):
            return self._tracks[index].get("end_action", "next")
        return "next"

    def update_duration(self, index: int, duration: float):
        if 0 <= index < len(self._tracks):
            self._tracks[index]["duration"] = duration

    # ── Persistence ───────────────────────────────────────────────────────────

    def save(self):
        # dp-237: persist id alongside filepath so a row's identity (and
        # therefore its independent markers/cues/colour) survives a
        # save/reload cycle instead of re-minting a fresh id every launch.
        entries = [
            {"filepath": t["filepath"], "id": t.get("id")} for t in self._tracks
        ]
        settings.set("last_playlist", entries)
        settings.save()

    def export_m3u(self, filepath: str):
        lines = ["#EXTM3U\n"]
        for t in self._tracks:
            dur = int(t.get("duration", -1))
            lines.append(f"#EXTINF:{dur},{t['artist']} - {t['title']}\n")
            lines.append(t["filepath"] + "\n")
        Path(filepath).write_text("".join(lines), encoding="utf-8")

    def import_m3u(self, filepath: str):
        files = []
        for line in Path(filepath).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                files.append(line)
        self.add_files(files)

    def export_pls(self, filepath: str):
        lines = ["[playlist]\n"]
        for i, t in enumerate(self._tracks, 1):
            lines.append(f"File{i}={t['filepath']}\n")
            lines.append(f"Title{i}={t['artist']} - {t['title']}\n")
            lines.append(f"Length{i}={int(t.get('duration', -1))}\n")
        lines.append(f"NumberOfEntries={len(self._tracks)}\n")
        lines.append("Version=2\n")
        Path(filepath).write_text("".join(lines), encoding="utf-8")

    def import_pls(self, filepath: str):
        files = []
        for line in Path(filepath).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.lower().startswith("file") and "=" in line:
                files.append(line.split("=", 1)[1])
        self.add_files(files)

    def export_show(self, filepath: str) -> list:
        """Export the playlist plus every row's authored state -- cues,
        start/end markers, per-track volume, colour, end action, and the
        live crossfade layout -- to a `.savashow` JSON file (dp-246).

        Unlike export_m3u/export_pls, each track's path is stored RELATIVE
        to the show file's own directory, never with an absolute fallback
        (confirmed decision 1) -- a stale absolute path that happened to
        resolve on another machine would silently load the WRONG copy of
        a track, which is worse than failing to find it. Does not copy
        audio (decision 5); the operator gathers the tracks into the show
        folder themselves.

        Returns the display name of every track that does not resolve
        inside the show folder (missing on disk, or outside the folder
        entirely) at export time. The file is written regardless -- the
        caller (UI) surfaces this list as a non-blocking warning
        (decision 6); export never refuses to write.
        """
        show_dir = Path(filepath).resolve().parent
        cue_points    = settings.get("row_cue_points", {})
        start_markers = settings.get("row_start_markers", {})
        end_markers   = settings.get("row_end_markers", {})
        volumes       = settings.get("row_volumes", {})

        missing = []
        track_entries = []
        for t in self._tracks:
            tid = t.get("id")
            abs_path = Path(t["filepath"]).resolve()
            unresolvable = False
            try:
                rel_path = os.path.relpath(abs_path, show_dir)
            except ValueError:
                # Different drive on Windows -- there is no relative form
                # between them at all. Store the bare filename so the entry
                # is still well-formed JSON, and flag it: the resulting
                # path will NOT resolve in the show folder.
                #
                # Flagging here is the whole point of the export-side
                # warning. This branch used to fall through to the check
                # below, which asks "does the path start with ..?" (no, it
                # is a bare filename) and "does the source file exist?"
                # (yes, over on the other drive) -- so a cross-drive export
                # wrote a silently broken show file with NO warning, and
                # the operator only discovered it at load time on the show
                # machine. That is exactly the failure decision 6 exists to
                # catch BEFORE the show leaves the building.
                rel_path = abs_path.name
                unresolvable = True
            rel_path = rel_path.replace(os.sep, "/")
            if unresolvable or rel_path.startswith("..") or not abs_path.exists():
                missing.append(t.get("title") or abs_path.name)
            track_entries.append({
                "id":           tid,
                "filepath":     rel_path,
                "title":        t.get("title"),
                "artist":       t.get("artist"),
                "cues":         cue_points.get(tid),
                "start_marker": start_markers.get(tid),
                "end_marker":   end_markers.get(tid),
                "volume":       volumes.get(tid),
                "color":        t.get("color"),
                "end_action":   t.get("end_action", "next"),
            })

        layout = CrossfadeLayout.load()
        show = {
            "format": SHOW_FORMAT,
            "version": SHOW_VERSION,
            "tracks": track_entries,
            "crossfade_layout": layout.to_dict() if layout is not None else None,
        }
        Path(filepath).write_text(json.dumps(show, indent=2), encoding="utf-8")
        return missing

    def import_show(self, filepath: str):
        """Import a `.savashow` file (dp-246) -- the counterpart to
        export_show. Additive, same as import_m3u/import_pls: appends to
        whatever is already in the playlist rather than replacing it.

        Each track's path is resolved RELATIVE to the show file's own
        directory (decision 1), so a show folder copied to a different
        drive letter or machine still loads. A track that cannot be found
        is SKIPPED -- the rest of the show still loads (decision 2); it
        is never a reason to refuse the whole import.

        Row ids are preserved from the file, which is what reattaches a
        row's cues/markers/volume/colour/end-action via the normal
        row-keyed state model (decision 4) -- UNLESS that id is already
        live in the current playlist (e.g. importing the same show
        twice), in which case a fresh id is minted and the saved state is
        copied across under it, so the two rows never alias one shared
        state entry.

        Returns (missing, id_map, crossfade_layout):
          missing         -- display names of tracks that could not be
                             resolved (decision 6's import-side warning).
          id_map          -- {file's original id: id actually used for
                             the row}, one entry per track that WAS
                             imported. The caller uses this to remap the
                             file's crossfade_layout ids before applying
                             it (see ui/main_window.py._on_import_show) --
                             an id with no entry here belonged to a
                             skipped track and is correctly dropped there,
                             which is what makes a crossfade spanning a
                             missing track come back at zero overlap
                             (decision 3) without any special-case logic
                             in this function.
          crossfade_layout -- the raw dict from the file (or None), still
                             keyed by the ORIGINAL ids -- unremapped.
        """
        show_dir = Path(filepath).resolve().parent
        # Validate before trusting anything in here. A user picking the wrong
        # file in the dialog is an ORDINARY mistake, not an exceptional one,
        # and `json.loads` on a non-JSON file raises JSONDecodeError -- which,
        # raised from a Qt slot, unwinds into the excepthook and shows the user
        # nothing at all. Raise one predictable exception type the caller can
        # turn into a plain sentence instead.
        try:
            raw = Path(filepath).read_text(encoding="utf-8")
            data = json.loads(raw)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
            raise ShowFileError(f"Not a readable show file: {e}") from e
        if not isinstance(data, dict) or data.get("format") != SHOW_FORMAT:
            raise ShowFileError(
                "This file is not a SAVA show file (missing or wrong "
                f"\"format\" marker; expected \"{SHOW_FORMAT}\")."
            )
        if not isinstance(data.get("tracks"), list):
            raise ShowFileError("Show file is malformed: no track list.")

        cue_points    = settings.get("row_cue_points", {})
        start_markers = settings.get("row_start_markers", {})
        end_markers   = settings.get("row_end_markers", {})
        volumes       = settings.get("row_volumes", {})
        colors        = settings.get("row_colors", {})
        end_actions   = settings.get("row_end_actions", {})

        live_ids = {t.get("id") for t in self._tracks if t.get("id")}
        missing = []
        id_map = {}
        new_rows = []

        for entry in data.get("tracks", []):
            rel_path = entry.get("filepath")
            if not rel_path:
                continue
            abs_path = (show_dir / rel_path).resolve()
            if (
                not abs_path.exists()
                or abs_path.suffix.lower() not in SUPPORTED_EXTENSIONS
            ):
                missing.append(entry.get("title") or rel_path)
                continue

            old_id = entry.get("id")
            # Decision 4: mint a fresh id on collision (re-importing the
            # same show, or an id that happens to already be live in this
            # playlist) so the new row never aliases the live row's
            # state -- and COPY the saved state across under the new id
            # rather than the row simply losing it.
            new_id = old_id if old_id and old_id not in live_ids else new_track_id()
            live_ids.add(new_id)
            if old_id:
                id_map[old_id] = new_id

            if entry.get("cues") is not None:
                cue_points[new_id] = entry["cues"]
            if entry.get("start_marker") is not None:
                start_markers[new_id] = entry["start_marker"]
            if entry.get("end_marker") is not None:
                end_markers[new_id] = entry["end_marker"]
            if entry.get("volume") is not None:
                volumes[new_id] = entry["volume"]
            if entry.get("color") is not None:
                colors[new_id] = entry["color"]
            if entry.get("end_action") is not None:
                end_actions[new_id] = entry["end_action"]

            # _new_row() reads row_colors/row_end_actions itself, but the
            # settings.set() calls below only happen once, after this
            # loop -- so it would not see new_id's entries yet. Stamp them
            # onto the row directly rather than relying on read-after-
            # write ordering within the loop.
            row = _new_row(str(abs_path), track_id=new_id)
            if entry.get("color") is not None:
                row["color"] = entry["color"]
            if entry.get("end_action") is not None:
                row["end_action"] = entry["end_action"]
            new_rows.append(row)

        settings.set("row_cue_points", cue_points)
        settings.set("row_start_markers", start_markers)
        settings.set("row_end_markers", end_markers)
        settings.set("row_volumes", volumes)
        settings.set("row_colors", colors)
        settings.set("row_end_actions", end_actions)
        # Flush to disk NOW, not at the next clean exit. `settings.set()` only
        # writes an in-memory dict (see dp-245 D1), so without this an import
        # left every restored cue and marker unpersisted -- a crash, or a kill,
        # any time before quitting would discard the whole show the operator
        # just loaded, with the .savashow file looking like it had worked.
        # Importing a show is exactly the kind of deliberate, infrequent,
        # user-initiated action that should hit the disk immediately; the same
        # reasoning already applies at `_on_clear_track_markers` and
        # `CrossfadeLayout.save()`.
        settings.save()

        if new_rows:
            self._tracks.extend(new_rows)
            self._rebuild_shuffle()
            self._notify_playlist_changed()

        return missing, id_map, data.get("crossfade_layout")

    # ── Queries ───────────────────────────────────────────────────────────────

    @property
    def current(self) -> dict | None:
        if 0 <= self._current_index < len(self._tracks):
            return self._tracks[self._current_index]
        return None

    @property
    def current_index(self) -> int:
        return self._current_index

    @property
    def tracks(self) -> list:
        return list(self._tracks)

    @property
    def count(self) -> int:
        return len(self._tracks)

    @property
    def total_duration(self) -> float:
        return sum(t.get("duration", 0) for t in self._tracks)

    def track_at(self, index: int) -> dict | None:
        if 0 <= index < len(self._tracks):
            return self._tracks[index]
        return None

    # ── Internal ──────────────────────────────────────────────────────────────

    def _next_index(self, reshuffle_on_wrap: bool = False) -> int | None:
        """Compute the index .next() would advance to.

        Pure by default. Only the mutating caller (next()) passes
        reshuffle_on_wrap=True; peek_next() leaves it False so a peek never
        reshuffles _shuffle_order (dp-192). At the shuffle+repeat=all wrap this
        means peek returns the pre-reshuffle order[0] while next() reshuffles
        and returns a fresh order[0] — an accepted one-per-cycle divergence."""
        n = len(self._tracks)
        if n == 0:
            return None
        if self._shuffle:
            order = self._shuffle_order
            try:
                pos = order.index(self._current_index)
                next_pos = pos + 1
            except ValueError:
                next_pos = 0
            if next_pos >= n:
                if self._repeat == "all":
                    if reshuffle_on_wrap:
                        random.shuffle(order)
                    return order[0]
                return None
            return order[next_pos]
        else:
            nxt = self._current_index + 1
            if nxt >= n:
                return 0 if self._repeat == "all" else None
            return nxt

    def _prev_index(self) -> int | None:
        n = len(self._tracks)
        if n == 0:
            return None
        if self._shuffle:
            order = self._shuffle_order
            try:
                pos = order.index(self._current_index)
                prev_pos = pos - 1
            except ValueError:
                prev_pos = n - 1
            if prev_pos < 0:
                return order[-1] if self._repeat == "all" else None
            return order[prev_pos]
        else:
            prv = self._current_index - 1
            if prv < 0:
                return n - 1 if self._repeat == "all" else None
            return prv

    def _rebuild_shuffle(self):
        self._shuffle_order = list(range(len(self._tracks)))
        if self._shuffle:
            random.shuffle(self._shuffle_order)

    def _load_last_playlist(self):
        entries = settings.get("last_playlist", [])
        if not entries:
            # dp-237(B): still prune -- an empty playlist means every
            # row-keyed entry is stranded (e.g. the app was closed after a
            # playlist-clear that predates this GC, or a corrupted
            # last_playlist).
            gc_row_state([])
            return
        if isinstance(entries[0], str):
            # Legacy format (pre-dp-237): bare filepath list, no ids saved
            # yet -- mint one per entry so the one-time migration below has
            # an id to key each row's inherited state on.
            entries = [{"filepath": fp, "id": new_track_id()} for fp in entries]
        # dp-238: one-time upgrade -- before building any row (and before
        # _new_row reads a single row-keyed map), copy legacy file-keyed
        # state into rows that don't already own row-keyed state of their
        # own. Idempotent; a no-op for rows that already have row-state.
        migrate_legacy_track_state(
            (e.get("id"), e.get("filepath")) for e in entries
        )
        added = 0
        for entry in entries:
            fp = entry.get("filepath")
            if fp and Path(fp).suffix.lower() in SUPPORTED_EXTENSIONS:
                self._tracks.append(_new_row(fp, track_id=entry.get("id")))
                added += 1
        if added:
            self._rebuild_shuffle()
            self._notify_playlist_changed()
        if self._tracks:
            self._current_index = 0
        # dp-237(B): prune any row-keyed state stranded by a previous
        # session (e.g. a track removed without a clean remove() call, or
        # an id from a playlist that no longer loads) so it doesn't linger
        # in settings forever.
        gc_row_state(t["id"] for t in self._tracks if t.get("id"))

    def _notify_track_changed(self):
        if self.on_track_changed and self._current_index >= 0:
            try:
                self.on_track_changed(self._current_index, self._tracks[self._current_index])
            except Exception as e:
                print(f"[Playlist] on_track_changed error: {e}")

    def _notify_playlist_changed(self):
        if self.on_playlist_changed:
            try:
                self.on_playlist_changed()
            except Exception as e:
                print(f"[Playlist] on_playlist_changed error: {e}")


# ── Singleton ─────────────────────────────────────────────────────────────────
playlist = Playlist()
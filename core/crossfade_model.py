"""
Pure-data layer for a crossfade timeline layout — no Qt, no audio playback.

A layout is an ordered list of tracks with a per-overlap crossfade duration
and bezier fade curve between each adjacent pair. Track positions on the
timeline are *derived* from cumulative (duration - overlap), never stored
independently, which makes two of the confirmed constraints structural
rather than caller-enforced:

- Left-only positioning: a track's default (rightmost) position is zero
  overlap. Overlap duration is clamped to >= 0, so a track can never move
  right of its flush default.
- Rigid chain, no gaps: because every track's start position is computed
  from the previous track's start plus its own (duration - overlap), a gap
  can never exist between adjacent tracks — moving one overlap shifts every
  later track's derived position automatically.
"""

from dataclasses import dataclass, field

from config.settings import settings

# Cubic bezier control points (P1, P2) approximating an equal-power
# (sin/cos) quarter-wave fade. Endpoints are fixed at (0,0)/(1,1) and not
# stored — only the two middle control points are user-editable.
DEFAULT_CURVE_P1 = (0.34, 0.0)
DEFAULT_CURVE_P2 = (0.64, 1.0)

# dp-190: default independent fade-out curve. Endpoints for a fade-out are
# fixed at (0,1)/(1,0) (full gain at overlap start, silent at overlap end)
# instead of the fade-in's (0,0)/(1,1) — same x-timing as the fade-in
# default, y flipped so the shape falls instead of rises.
DEFAULT_CURVE_OUT_P1 = (DEFAULT_CURVE_P1[0], 1.0 - DEFAULT_CURVE_P1[1])
DEFAULT_CURVE_OUT_P2 = (DEFAULT_CURVE_P2[0], 1.0 - DEFAULT_CURVE_P2[1])

SETTINGS_KEY = "crossfade_layout"


def _clamp01(v: float) -> float:
    return max(0.0, min(1.0, v))


def _cubic_bezier(u: float, p0, p1, p2, p3) -> tuple:
    """Evaluate a cubic bezier at parameter u in [0,1]."""
    mu = 1.0 - u
    x = (mu ** 3) * p0[0] + 3 * (mu ** 2) * u * p1[0] + 3 * mu * (u ** 2) * p2[0] + (u ** 3) * p3[0]
    y = (mu ** 3) * p0[1] + 3 * (mu ** 2) * u * p1[1] + 3 * mu * (u ** 2) * p2[1] + (u ** 3) * p3[1]
    return x, y


@dataclass
class Overlap:
    """The crossfade region between one track and the next."""

    duration: float = 0.0                       # seconds, always >= 0
    curve_p1: tuple = field(default_factory=lambda: DEFAULT_CURVE_P1)
    curve_p2: tuple = field(default_factory=lambda: DEFAULT_CURVE_P2)
    # dp-190: independently-editable fade-out curve for the outgoing track.
    # None only ever occurs for a layout loaded from a pre-dp-190 saved
    # file (see from_dict) — it means "no independent curve was ever
    # authored", and evaluate_out() falls back to the original derived
    # equal-power complement so already-saved crossfades don't silently
    # change sound. Any newly-constructed Overlap (including from_playlist_
    # tracks) gets the mirrored default curve, not None, so it's
    # independent from creation.
    curve_out_p1: "tuple | None" = field(default_factory=lambda: DEFAULT_CURVE_OUT_P1)
    curve_out_p2: "tuple | None" = field(default_factory=lambda: DEFAULT_CURVE_OUT_P2)

    def clamp_duration(self, max_duration: float):
        """Clamp to [0, max_duration] — enforces the left-only invariant
        (never negative) and prevents overlapping more than either
        neighboring track's own length."""
        self.duration = max(0.0, min(self.duration, max(0.0, max_duration)))

    @staticmethod
    def _bezier_gain(t: float, p0: tuple, p1: tuple, p2: tuple, p3: tuple) -> float:
        """Evaluate a cubic bezier's y at normalized time t in [0,1] by
        bisecting on x(u) == t. Shared by evaluate_in/evaluate_out so the
        24-iteration bisection loop isn't duplicated per curve."""
        t = _clamp01(t)
        lo, hi = 0.0, 1.0
        for _ in range(24):  # bisection on x(u) == t; 24 iters is plenty for float precision
            mid = (lo + hi) / 2
            x, _ = _cubic_bezier(mid, p0, p1, p2, p3)
            if x < t:
                lo = mid
            else:
                hi = mid
        _, y = _cubic_bezier((lo + hi) / 2, p0, p1, p2, p3)
        return _clamp01(y)

    def evaluate_in(self, t: float) -> float:
        """Incoming-track gain at normalized overlap time t in [0,1], via
        the user-editable bezier curve. Endpoints are fixed (0,0)->(1,1)."""
        return self._bezier_gain(t, (0.0, 0.0), self.curve_p1, self.curve_p2, (1.0, 1.0))

    def evaluate_out(self, t: float) -> float:
        """Outgoing-track gain at normalized overlap time t in [0,1].

        dp-190: independently editable via its own bezier curve (endpoints
        fixed at (0,1)->(1,0), since fade-out starts at full gain and ends
        silent) instead of being derived from the incoming curve. Legacy
        layouts saved before dp-190 (curve_out_p1/p2 is None — see
        from_dict) keep the original equal-power complement behavior so
        loading and resaving them without editing fade-out doesn't change
        their sound.
        """
        if self.curve_out_p1 is None or self.curve_out_p2 is None:
            in_gain = self.evaluate_in(t)
            return _clamp01((1.0 - in_gain ** 2) ** 0.5)
        return self._bezier_gain(t, (0.0, 1.0), self.curve_out_p1, self.curve_out_p2, (1.0, 0.0))

    def to_dict(self) -> dict:
        return {
            "duration": self.duration,
            "curve_p1": list(self.curve_p1),
            "curve_p2": list(self.curve_p2),
            "curve_out_p1": list(self.curve_out_p1) if self.curve_out_p1 is not None else None,
            "curve_out_p2": list(self.curve_out_p2) if self.curve_out_p2 is not None else None,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Overlap":
        curve_out_p1 = d.get("curve_out_p1")
        curve_out_p2 = d.get("curve_out_p2")
        return cls(
            duration=float(d.get("duration", 0.0)),
            curve_p1=tuple(d.get("curve_p1", DEFAULT_CURVE_P1)),
            curve_p2=tuple(d.get("curve_p2", DEFAULT_CURVE_P2)),
            curve_out_p1=tuple(curve_out_p1) if curve_out_p1 is not None else None,
            curve_out_p2=tuple(curve_out_p2) if curve_out_p2 is not None else None,
        )


@dataclass
class LayoutTrack:
    """Informational reference to a playlist track — duration drives
    position math but is not the source of truth (playlist is)."""

    filepath: str
    duration: float = 0.0
    # dp-194: the playlist's per-track color, carried through so the
    # timeline's marker line and the dialog's shortcut button can use it
    # instead of dp-176's position-derived auto-palette. None means the
    # track has no playlist color assigned — callers fall back to the
    # auto-palette in that case.
    color: str | None = None
    # dp-236: the playlist row's stable dp-237 id. Overlaps are keyed on
    # this (not filepath — the same file can appear twice, dp-237) so a
    # playlist edit can preserve the overlaps between tracks that are
    # still adjacent in the same order, instead of resetting the whole
    # layout. None only for a layout loaded from a pre-dp-236 saved file
    # (see from_dict) — such a layout has no identity to match against and
    # falls back to a full reset on the next edit, same as dp-160's
    # original behaviour.
    track_id: str | None = None


class CrossfadeLayout:
    """An ordered crossfade timeline over a fixed set of tracks."""

    def __init__(self, tracks: list = None):
        self._tracks: list[LayoutTrack] = list(tracks or [])
        self._overlaps: list[Overlap] = [
            Overlap() for _ in range(max(0, len(self._tracks) - 1))
        ]

    # ── Construction ──────────────────────────────────────────────────────

    @classmethod
    def from_playlist_tracks(cls, tracks: list) -> "CrossfadeLayout":
        """Build a fresh layout (zero overlap everywhere) from playlist
        track dicts (must have 'filepath' and 'duration').

        dp-212: a track with a per-track "Fin" end marker (dp-238: the
        row-keyed settings map, keyed by track id — see DeckEngine.load())
        treats that marker as its effective end for the timeline, same
        fallback as _poll_position: marker if set, else full duration."""
        end_markers = settings.get("row_end_markers", {})
        layout_tracks = []
        for t in tracks:
            duration = float(t.get("duration", 0.0))
            marker = end_markers.get(t.get("id"))
            effective_end = marker if marker is not None else duration
            layout_tracks.append(
                LayoutTrack(
                    filepath=t["filepath"],
                    duration=float(effective_end),
                    color=t.get("color"),
                    track_id=t.get("id"),
                )
            )
        return cls(layout_tracks)

    # ── Queries ───────────────────────────────────────────────────────────

    @property
    def tracks(self) -> list:
        return list(self._tracks)

    @property
    def overlaps(self) -> list:
        return list(self._overlaps)

    def track_positions(self) -> list:
        """Timeline start position (seconds) of each track. Position[0] is
        always 0.0. Structurally gap-free — see module docstring."""
        if not self._tracks:
            return []
        positions = [0.0]
        for i, ov in enumerate(self._overlaps):
            start = positions[i] + self._tracks[i].duration - ov.duration
            positions.append(start)
        return positions

    def total_duration(self) -> float:
        if not self._tracks:
            return 0.0
        return self.track_positions()[-1] + self._tracks[-1].duration

    # ── Mutation ──────────────────────────────────────────────────────────

    def set_overlap_duration(self, index: int, seconds: float):
        """Set the overlap between track[index] and track[index+1]. This is
        the "drag track index+1 left" operation — every later track shifts
        automatically via track_positions(), no explicit chain-shift needed."""
        if not (0 <= index < len(self._overlaps)):
            return
        ov = self._overlaps[index]
        # dp-221: clamp against each neighbour's REMAINING duration -- what is
        # left after the overlap on that track's OTHER edge has taken its cut --
        # not the full track duration. Clamping against the full duration let
        # both overlaps around one track each be as long as the whole track, so
        # the track was entirely consumed from both sides. DeckEngine's trigger
        # (`read_idx >= end - _crossfade_len`) is then satisfied from frame 0
        # and the fade starts before the track has played anything.
        prev_dur = self._tracks[index].duration - self._overlap_duration(index - 1)
        next_dur = self._tracks[index + 1].duration - self._overlap_duration(index + 1)
        ov.duration = seconds
        ov.clamp_duration(min(prev_dur, next_dur))

    def _overlap_duration(self, index: int) -> float:
        """This layout's overlap duration at `index`, or 0.0 when `index` is
        off either end (the first track has no overlap to its left, the last
        none to its right)."""
        if 0 <= index < len(self._overlaps):
            return self._overlaps[index].duration
        return 0.0

    def set_overlap_curve(self, index: int, p1: tuple, p2: tuple):
        if 0 <= index < len(self._overlaps):
            self._overlaps[index].curve_p1 = (_clamp01(p1[0]), _clamp01(p1[1]))
            self._overlaps[index].curve_p2 = (_clamp01(p2[0]), _clamp01(p2[1]))

    def set_overlap_curve_out(self, index: int, p1: tuple, p2: tuple):
        """dp-190: set the outgoing track's independent fade-out curve."""
        if 0 <= index < len(self._overlaps):
            self._overlaps[index].curve_out_p1 = (_clamp01(p1[0]), _clamp01(p1[1]))
            self._overlaps[index].curve_out_p2 = (_clamp01(p2[0]), _clamp01(p2[1]))

    def replace_overlap(self, index: int, overlap: "Overlap"):
        """dp-236: install an already-built Overlap verbatim (no clamping —
        the caller is copying one preserved across a playlist edit, already
        valid against the track durations it was authored for). Used by
        MainWindow's selective invalidation to carry surviving overlaps
        into a freshly rebuilt layout without reaching into `_overlaps`."""
        if 0 <= index < len(self._overlaps):
            self._overlaps[index] = overlap

    def reset(self):
        """Clear entry point for playlist-change invalidation (wired in
        dp-160 via playlist.on_playlist_changed)."""
        self._tracks = []
        self._overlaps = []

    # ── Persistence ───────────────────────────────────────────────────────

    def to_dict(self) -> dict:
        return {
            "tracks": [
                {"filepath": t.filepath, "duration": t.duration, "track_id": t.track_id}
                for t in self._tracks
            ],
            "overlaps": [ov.to_dict() for ov in self._overlaps],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "CrossfadeLayout":
        tracks = [
            LayoutTrack(
                filepath=t["filepath"],
                duration=float(t.get("duration", 0.0)),
                track_id=t.get("track_id"),
            )
            for t in d.get("tracks", [])
        ]
        layout = cls(tracks)
        # dp-245 D6: a hand-edited or partially-written settings file can carry
        # an overlap count that doesn't match len(tracks) - 1, and BOTH
        # directions are damaging:
        #   too many  -> track_positions() indexes self._tracks[i] per overlap
        #                and raises IndexError.
        #   too few   -> no crash, but track_positions() silently returns fewer
        #                positions than tracks, so total_duration() (which reads
        #                positions[-1]) drops whole tracks off the end of the
        #                timeline. The quiet case is the nastier one.
        # len(tracks) - 1 is the only ever-valid length, so pad/truncate to it.
        parsed = [Overlap.from_dict(o) for o in d.get("overlaps", [])]
        needed = max(0, len(tracks) - 1)
        del parsed[needed:]
        parsed.extend(Overlap() for _ in range(needed - len(parsed)))
        layout._overlaps = parsed
        return layout

    def save(self):
        settings.set(SETTINGS_KEY, self.to_dict())
        settings.save()

    @classmethod
    def load(cls) -> "CrossfadeLayout | None":
        d = settings.get(SETTINGS_KEY, {})
        if not d or not d.get("tracks"):
            return None
        return cls.from_dict(d)

    @staticmethod
    def clear_persisted():
        settings.set(SETTINGS_KEY, {})
        settings.save()

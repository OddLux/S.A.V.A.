"""Pure marker-position math for the waveform's crossfade fade-in/fade-out
markers (dp-216 Phase 3, Part B). No Qt, no engine -- keeps
core/crossfade_model.py untouched and this logic unit-testable in isolation.
"""


def crossfade_marker_positions(layout, idx, current_filepath, next_matches=True):
    """Return (fade_in_end, fade_out_start) in seconds for the track at
    layout index `idx`, or (None, None) if the layout is missing/stale.

    dp-221: `next_matches` is False when the track actually coming up is NOT
    this layout's linear successor -- i.e. shuffle is on, or repeat=one. No
    crossfade is armed in that case (see
    MainWindow._overlap_for_transition), so drawing a fade-out marker would
    promise a fade that never happens. `fade_out_start` is suppressed;
    `fade_in_end` is not, because it describes a fade that already occurred
    on the way IN to this track rather than one being predicted. Defaults to
    True so callers that do not model playback order keep the old behavior.

    - fade_in_end: set when the PREVIOUS track has an overlap into this one
      (this track is the incoming side) -- the point where its fade-in ends.
    - fade_out_start: set when this track has an overlap into the NEXT one
      (this track is the outgoing side) -- the point where its fade-out
      begins, computed against the layout's effective (Fin-marker-aware)
      duration.

    `current_filepath` guards against a layout that no longer matches the
    live playlist -- same staleness check `_maybe_trigger_crossfade` uses.
    """
    if layout is None or not layout.tracks:
        return (None, None)
    tracks, overlaps = layout.tracks, layout.overlaps
    if idx < 0 or idx >= len(tracks):
        return (None, None)
    if tracks[idx].filepath != current_filepath:
        return (None, None)

    fade_in_end = None
    if idx >= 1 and overlaps[idx - 1].duration > 0:
        fade_in_end = overlaps[idx - 1].duration

    fade_out_start = None
    if next_matches and idx < len(overlaps) and overlaps[idx].duration > 0:
        fade_out_start = max(0.0, tracks[idx].duration - overlaps[idx].duration)

    return (fade_in_end, fade_out_start)

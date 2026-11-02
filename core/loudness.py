"""dp-266: playlist loudness leveling.

Measures integrated loudness (EBU R128, LUFS) per file with the bundled ffmpeg
and turns the measurements into per-track fader positions that make the
playlist roughly even. Cut only: the quietest regular track keeps 100 (0 dB)
and every louder one is pulled down to match it.

Everything here is UI-agnostic. `compute_levels` and `parse_lufs` are pure.
"""

import os
import re
import statistics
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from core.deck_engine import _FFMPEG_BIN
from core.subproc import CREATE_NO_WINDOW, popen_kwargs
from core.volume_curve import db_to_slider

#: A track this many LU (or more) below the playlist median is an outlier
#: (spoken intro, ambience, near-silence) and is left alone so it cannot drag
#: every other track down to its level.
OUTLIER_LU = 12.0

#: ebur128 reports -70 for gated silence; treat it as "no measurement".
SILENCE_LUFS = -70.0

MAX_WORKERS = 2

CACHE_KEY = "loudness_cache"

SKIP_OUTLIER = "outlier"
SKIP_UNMEASURED = "unmeasured"

# Lowers ffmpeg's CPU priority so measuring never starves the realtime audio
# callback while a show is playing. 0 off Windows (nice is not needed there).
_BELOW_NORMAL = getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0)

_I_LINE = re.compile(r"^\s*I:\s*(-?inf|-?\d+(?:\.\d+)?)\s*LUFS", re.MULTILINE)


def parse_lufs(stderr_text):
    """Return the integrated loudness (float LUFS) from ffmpeg's ebur128
    summary, or None when absent, `-inf`, or at/below the -70 silence floor.

    The summary block is printed last, so the LAST `I:` line wins (with
    framelog on, per-frame lines also contain `I:`).
    """
    matches = _I_LINE.findall(stderr_text or "")
    if not matches:
        return None
    try:
        value = float(matches[-1])
    except ValueError:
        return None
    if value != value or value <= SILENCE_LUFS:
        return None
    return value


def measure_file(filepath, cancel=None):
    """Measure one file; return LUFS float, or None (silent / failed /
    cancelled)."""
    cmd = [
        _FFMPEG_BIN, "-hide_banner", "-nostats", "-nostdin",
        "-i", str(filepath),
        "-af", "ebur128=framelog=quiet",
        "-f", "null", "-",
    ]
    try:
        proc = subprocess.Popen(
            cmd,
            **popen_kwargs(
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                creationflags=CREATE_NO_WINDOW | _BELOW_NORMAL,
            ),
        )
    except OSError as exc:
        print(f"[Loudness] cannot launch ffmpeg: {exc}")
        return None
    chunks = []
    reader = threading.Thread(
        target=lambda: chunks.append(proc.stderr.read()), daemon=True
    )
    reader.start()
    while proc.poll() is None:
        if cancel is not None and cancel.is_set():
            proc.kill()
            break
        time.sleep(0.05)
    proc.wait()
    reader.join(timeout=5)
    if cancel is not None and cancel.is_set():
        return None
    text = chunks[0].decode("utf-8", "replace") if chunks else ""
    return parse_lufs(text)


def file_signature(filepath):
    """(mtime, size) used to invalidate cache entries, or None if the file
    cannot be stat'ed."""
    try:
        st = os.stat(filepath)
    except OSError:
        return None
    return st.st_mtime, st.st_size


def cache_lookup(cache, filepath, signature):
    """Return the cached LUFS for `filepath` if its mtime/size still match
    `signature`, else None."""
    entry = cache.get(filepath)
    if not entry or signature is None:
        return None
    if entry.get("mtime") != signature[0] or entry.get("size") != signature[1]:
        return None
    return entry.get("lufs")


def measure_many(
    filepaths,
    cache,
    progress=None,
    cancel=None,
    measure=measure_file,  # called as measure(path, cancel)
    signature=file_signature,
):
    """Measure each unique file once, reusing `cache` (a dict in the stored
    settings shape). Returns (results, new_cache); results maps filepath ->
    LUFS or None. `new_cache` is `cache` plus fresh measurements - the caller
    persists it once. `progress(done, total)` is called from worker threads.
    Failed/silent measurements are not cached (a retry may succeed).
    """
    unique = list(dict.fromkeys(filepaths))
    results = {}
    new_cache = dict(cache)
    todo = []
    for path in unique:
        sig = signature(path)
        hit = cache_lookup(cache, path, sig)
        if hit is not None:
            results[path] = hit
        else:
            todo.append((path, sig))
    total = len(unique)
    done = [total - len(todo)]
    lock = threading.Lock()
    if progress and done[0]:
        progress(done[0], total)

    def work(item):
        path, sig = item
        if cancel is not None and cancel.is_set():
            return path, sig, None
        lufs = measure(path, cancel)
        return path, sig, lufs

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        for path, sig, lufs in pool.map(work, todo):
            results[path] = lufs
            if lufs is not None and sig is not None:
                new_cache[path] = {"lufs": lufs, "mtime": sig[0], "size": sig[1]}
            with lock:
                done[0] += 1
                count = done[0]
            if progress:
                progress(count, total)
    return results, new_cache


def compute_levels(loudness_by_row):
    """Pure: {row: LUFS or None} -> (levels, skipped).

    `levels` maps row -> fader position (int 0-100) for every row to change;
    `skipped` maps row -> SKIP_OUTLIER / SKIP_UNMEASURED for rows left alone.
    Target is the quietest non-outlier; outliers are rows more than
    OUTLIER_LU below the median of all measured rows.
    """
    measured = {r: v for r, v in loudness_by_row.items() if v is not None}
    skipped = {r: SKIP_UNMEASURED for r, v in loudness_by_row.items() if v is None}
    if not measured:
        return {}, skipped
    median = statistics.median(measured.values())
    regular = {}
    for row, lufs in measured.items():
        if lufs < median - OUTLIER_LU:
            skipped[row] = SKIP_OUTLIER
        else:
            regular[row] = lufs
    target = min(regular.values())
    levels = {
        row: max(0, min(100, round(db_to_slider(target - lufs))))
        for row, lufs in regular.items()
    }
    return levels, skipped

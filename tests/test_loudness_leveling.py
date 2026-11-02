"""dp-266: playlist loudness leveling (parser, level maths, cache, real ffmpeg)."""

import math
import os
import shutil
import sys
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pytest

from core import loudness
from core.loudness import (
    SKIP_OUTLIER,
    SKIP_UNMEASURED,
    cache_lookup,
    compute_levels,
    measure_many,
    parse_lufs,
)

SUMMARY = """\
[Parsed_ebur128_0 @ 000001] Summary:

  Integrated loudness:
    I:         -23.4 LUFS
    Threshold: -33.5 LUFS

  Loudness range:
    LRA:         6.2 LU
"""


class TestParser:
    def test_reads_summary_integrated(self):
        assert parse_lufs(SUMMARY) == pytest.approx(-23.4)

    def test_last_i_line_wins(self):
        text = "  I:  -50.0 LUFS\n" + SUMMARY
        assert parse_lufs(text) == pytest.approx(-23.4)

    @pytest.mark.parametrize(
        "text",
        ["", None, "garbage", "    I:         -inf LUFS\n", "    I:  -70.0 LUFS\n"],
    )
    def test_unusable_is_none(self, text):
        assert parse_lufs(text) is None


class TestComputeLevels:
    def test_equal_loudness_all_full(self):
        levels, skipped = compute_levels({0: -20.0, 1: -20.0, 2: -20.0})
        assert levels == {0: 100, 1: 100, 2: 100}
        assert skipped == {}

    def test_six_lu_louder_gets_90(self):
        # 6 dB cut on the dB taper (0.6 dB per step) = 10 steps. A linear
        # mapping would give 50 (or 94), so this pins the curve conversion.
        levels, _ = compute_levels({0: -20.0, 1: -26.0, 2: -20.0})
        assert levels[1] == 100
        assert levels[0] == 90
        assert levels[2] == 90

    def test_outlier_excluded_and_reported(self):
        # median -20; -45 is 25 LU below -> outlier. Without outlier logic
        # the target would be -45 and the others would drop to ~58.
        levels, skipped = compute_levels(
            {0: -18.0, 1: -20.0, 2: -22.0, 3: -45.0}
        )
        assert skipped == {3: SKIP_OUTLIER}
        assert 3 not in levels
        assert levels[2] == 100  # quietest NON-outlier is the target
        assert levels[1] == round(100 * (1 - 2 / 60))
        assert levels[0] == round(100 * (1 - 4 / 60))

    def test_just_inside_threshold_is_not_outlier(self):
        _, skipped = compute_levels({0: -20.0, 1: -20.0, 2: -31.0})
        assert skipped == {}

    def test_never_above_100_and_unmeasured_skipped(self):
        levels, skipped = compute_levels({0: -30.0, 1: None, 2: -10.0})
        assert skipped == {1: SKIP_UNMEASURED}
        assert all(0 <= v <= 100 for v in levels.values())
        assert levels[0] == 100
        assert levels[2] < 100

    def test_nothing_measured(self):
        assert compute_levels({0: None}) == ({}, {0: SKIP_UNMEASURED})


class TestCache:
    def test_lookup_hit_and_invalidation(self):
        cache = {"a.wav": {"lufs": -20.0, "mtime": 10.0, "size": 5}}
        assert cache_lookup(cache, "a.wav", (10.0, 5)) == -20.0
        assert cache_lookup(cache, "a.wav", (11.0, 5)) is None  # mtime changed
        assert cache_lookup(cache, "a.wav", (10.0, 6)) is None  # size changed
        assert cache_lookup(cache, "b.wav", (10.0, 5)) is None

    def test_measure_many_uses_cache_and_remeasures_on_change(self):
        calls = []

        def fake_measure(path, cancel):
            calls.append(path)
            return -25.0

        sigs = {"a": (1.0, 1), "b": (1.0, 1)}
        results, cache = measure_many(
            ["a", "b", "a"], {}, measure=fake_measure, signature=sigs.get
        )
        assert sorted(calls) == ["a", "b"]  # duplicate measured once
        assert results == {"a": -25.0, "b": -25.0}

        calls.clear()
        measure_many(["a", "b"], cache, measure=fake_measure, signature=sigs.get)
        assert calls == []  # all cached

        sigs["b"] = (2.0, 1)  # mtime changed
        measure_many(["a", "b"], cache, measure=fake_measure, signature=sigs.get)
        assert calls == ["b"]

    def test_failed_measurement_not_cached(self):
        _, cache = measure_many(
            ["a"], {}, measure=lambda p, c: None, signature=lambda p: (1.0, 1)
        )
        assert cache == {}


_FFMPEG = loudness._FFMPEG_BIN
_HAVE_FFMPEG = os.path.isabs(_FFMPEG) or shutil.which(_FFMPEG) is not None


def _write_sine(path, amplitude, seconds=3, rate=44100):
    t = np.arange(int(seconds * rate)) / rate
    data = (amplitude * 32767 * np.sin(2 * math.pi * 440 * t)).astype("<i2")
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(data.tobytes())


@pytest.mark.skipif(not _HAVE_FFMPEG, reason="ffmpeg not available")
def test_real_ffmpeg_measures_amplitude_ratio(tmp_path):
    loud = tmp_path / "loud.wav"
    quiet = tmp_path / "quiet.wav"
    _write_sine(loud, 0.5)
    _write_sine(quiet, 0.125)  # 12 dB lower
    lufs_loud = loudness.measure_file(str(loud))
    lufs_quiet = loudness.measure_file(str(quiet))
    assert lufs_loud is not None and lufs_quiet is not None
    assert lufs_loud - lufs_quiet == pytest.approx(12.0, abs=1.0)

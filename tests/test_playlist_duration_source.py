"""dp-234: playlist durations must be correct at add time, not only after a
track has been played once.

mutagen reports 0.0 for some WAV files even though they decode and play
fine (measured on the reporting user's own files). `_read_metadata` must
fall back to ffprobe -- the same authoritative source
core/deck_engine.py::_read_duration uses for playback -- when mutagen comes
back empty.

Plain unittest, no pytest dependency:
    ./venv/Scripts/python.exe -m unittest discover tests
"""

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import core.playlist as playlist_module
from core.playlist import _read_metadata


class _FakeMutagenInfo:
    def __init__(self, length):
        self.length = length


class _FakeMutagenFile(dict):
    def __init__(self, length):
        super().__init__()
        self.info = _FakeMutagenInfo(length)


class TestReadMetadataDurationFallback(unittest.TestCase):
    def test_mutagen_zero_falls_back_to_ffprobe(self):
        # Discrimination check: ffprobe returns a distinct, nonzero value
        # that could not appear by coincidence if the fallback were dead
        # code -- mutagen's (stubbed) answer is 0.0.
        with mock.patch.object(
            playlist_module, "MutagenFile", return_value=_FakeMutagenFile(0.0)
        ), mock.patch.object(
            playlist_module, "_ffprobe_duration", return_value=262.583
        ):
            meta = _read_metadata("Aphex Twin - Come To Daddy (Pappy Mix).wav")

        self.assertEqual(meta["duration"], 262.583)

    def test_mutagen_success_skips_ffprobe(self):
        # When mutagen already has a real duration, ffprobe must NOT be
        # invoked at all -- the whole point of the fallback ordering is to
        # avoid a subprocess per track on a bulk add.
        ffprobe = mock.Mock(return_value=999.0)
        with mock.patch.object(
            playlist_module, "MutagenFile", return_value=_FakeMutagenFile(180.0)
        ), mock.patch.object(playlist_module, "_ffprobe_duration", ffprobe):
            meta = _read_metadata("track.mp3")

        self.assertEqual(meta["duration"], 180.0)
        ffprobe.assert_not_called()

    def test_mutagen_and_ffprobe_both_fail_gives_zero(self):
        with mock.patch.object(
            playlist_module, "MutagenFile", return_value=_FakeMutagenFile(0.0)
        ), mock.patch.object(
            playlist_module, "_ffprobe_duration", return_value=None
        ):
            meta = _read_metadata("broken.wav")

        self.assertEqual(meta["duration"], 0.0)


if __name__ == "__main__":
    unittest.main()

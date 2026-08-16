"""dp-223: output device resolution for the Audio Output picker.

Why this exists: SAVA played out of an NVIDIA HDMI monitor endpoint because
PortAudio reported it as the default for EVERY host API, and there was no
override. That endpoint also comes and goes as the monitor sleeps, so the
"default" is not even stable across launches. Selection is therefore keyed by
device NAME (indices are positional in PortAudio's enumeration and shift when
devices appear/disappear) with a safe fallback when the saved device is gone.

    QT_QPA_PLATFORM=offscreen ./venv/Scripts/python.exe -m unittest discover tests
"""

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import core.deck_engine as deck_engine_module  # noqa: E402
from core.deck_engine import (  # noqa: E402
    FOLLOW_SYSTEM_DEFAULT,
    _output_device_candidates,
    _resolve_device_by_name,
    list_output_devices,
)

# Mirrors the real machine: the same physical output is exposed once per host
# API, and the HDMI monitor endpoint is present only while the monitor is awake.
FAKE_HOSTAPIS = [
    {"name": "MME", "default_output_device": 1},
    {"name": "Windows DirectSound", "default_output_device": 3},
    {"name": "Windows WASAPI", "default_output_device": 5},
    {"name": "Windows WDM-KS", "default_output_device": 7},
]

FAKE_DEVICES = [
    {"name": "Mic", "max_output_channels": 0, "hostapi": 0, "default_samplerate": 44100},
    {"name": "HDMI Monitor", "max_output_channels": 2, "hostapi": 0, "default_samplerate": 48000},
    {"name": "Speakers", "max_output_channels": 2, "hostapi": 0, "default_samplerate": 44100},
    {"name": "Primary Sound Driver", "max_output_channels": 2, "hostapi": 1, "default_samplerate": 44100},
    {"name": "Speakers", "max_output_channels": 2, "hostapi": 1, "default_samplerate": 44100},
    {"name": "HDMI Monitor", "max_output_channels": 2, "hostapi": 2, "default_samplerate": 48000},
    {"name": "Speakers", "max_output_channels": 2, "hostapi": 2, "default_samplerate": 48000},
    {"name": "KS Out", "max_output_channels": 2, "hostapi": 3, "default_samplerate": 48000},
]


def _fake_query_devices(index=None, kind=None):
    if kind == "output":
        info = dict(FAKE_DEVICES[5])
        info["index"] = 5
        return info
    if index is None:
        return FAKE_DEVICES
    return FAKE_DEVICES[index]


def _patched():
    return mock.patch.multiple(
        deck_engine_module.sd,
        query_hostapis=mock.DEFAULT,
        query_devices=mock.DEFAULT,
    )


class _DeviceCase(unittest.TestCase):
    def setUp(self):
        patcher = _patched()
        mocks = patcher.start()
        self.addCleanup(patcher.stop)
        mocks["query_hostapis"].side_effect = (
            lambda index=None: FAKE_HOSTAPIS if index is None else FAKE_HOSTAPIS[index]
        )
        mocks["query_devices"].side_effect = _fake_query_devices


class TestListOutputDevices(_DeviceCase):

    def test_excludes_devices_that_cannot_play_stereo(self):
        names = [name for _i, name, _api in list_output_devices()]
        self.assertNotIn("Mic", names)

    def test_excludes_host_apis_sava_does_not_use(self):
        """WDM-KS is not in _HOST_API_PREFERENCE -- exposing it would offer
        the user an exclusive-mode device that typically refuses to open."""
        names = [name for _i, name, _api in list_output_devices()]
        self.assertNotIn("KS Out", names)

    def test_preferred_host_api_comes_first(self):
        first_api = list_output_devices()[0][2]
        self.assertEqual(first_api, "Windows WASAPI")

    def test_same_device_on_multiple_host_apis_is_listed_once_each(self):
        speakers = [row for row in list_output_devices() if row[1] == "Speakers"]
        apis = sorted(api for _i, _n, api in speakers)
        self.assertEqual(apis, ["MME", "Windows DirectSound", "Windows WASAPI"])


class TestResolveDeviceByName(_DeviceCase):

    def test_resolves_to_the_preferred_host_api_instance(self):
        self.assertEqual(_resolve_device_by_name("Speakers"), 6)  # WASAPI row

    def test_follow_default_sentinel_resolves_to_nothing(self):
        self.assertIsNone(_resolve_device_by_name(FOLLOW_SYSTEM_DEFAULT))

    def test_empty_name_resolves_to_nothing(self):
        self.assertIsNone(_resolve_device_by_name(""))
        self.assertIsNone(_resolve_device_by_name(None))

    def test_vanished_device_resolves_to_nothing(self):
        """The monitor went to sleep and its endpoint disappeared. Must not
        raise -- the caller falls back to the default chain."""
        self.assertIsNone(_resolve_device_by_name("Unplugged Dock"))


class TestOutputDeviceCandidates(_DeviceCase):

    def test_saved_device_is_tried_first(self):
        candidates = _output_device_candidates("Speakers")
        self.assertEqual(candidates[0][0], 6)

    def test_saved_device_carries_its_own_sample_rate(self):
        """PortAudio does not resample: the stream rate must be the rate of
        the device actually opened, not a global assumption."""
        self.assertEqual(_output_device_candidates("Speakers")[0][1], 48000)

    def test_vanished_saved_device_falls_back_to_the_default_chain(self):
        candidates = _output_device_candidates("Unplugged Dock")
        self.assertEqual(candidates[0][0], 5)  # WASAPI default
        self.assertTrue(candidates)

    def test_follow_default_matches_the_pre_dp223_behavior(self):
        self.assertEqual(
            _output_device_candidates(FOLLOW_SYSTEM_DEFAULT),
            _output_device_candidates(None),
        )

    def test_chain_always_ends_with_a_last_resort_entry(self):
        """A machine with no usable output at all must still construct an
        engine rather than raising at import time (CI/headless)."""
        self.assertEqual(_output_device_candidates("Speakers")[-1], (None, 44100))


if __name__ == "__main__":
    unittest.main()

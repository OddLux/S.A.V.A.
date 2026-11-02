"""dp-265: volume faders use a dB taper, not a linear gain.

Every assertion here fails under the old linear law (gain = position / 100).
"""

import math
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from core.volume_curve import MIN_DB, db_to_slider, slider_to_gain  # noqa: E402
from test_deck_engine import _make_bare_engine, _make_deck  # noqa: E402


def _db(gain):
    return 20.0 * math.log10(gain)


class TestCurve(unittest.TestCase):

    def test_endpoints(self):
        self.assertEqual(slider_to_gain(0), 0.0)
        self.assertAlmostEqual(slider_to_gain(100), 1.0)

    def test_clamps_out_of_range(self):
        self.assertEqual(slider_to_gain(-5), 0.0)
        self.assertAlmostEqual(slider_to_gain(150), 1.0)

    def test_equal_travel_is_equal_db(self):
        # 0->10% used to be +inf dB of change and 50->100% only +6 dB.
        step_low = _db(slider_to_gain(20)) - _db(slider_to_gain(10))
        step_high = _db(slider_to_gain(100)) - _db(slider_to_gain(90))
        self.assertAlmostEqual(step_low, step_high)
        self.assertAlmostEqual(step_high, -MIN_DB / 10)

    def test_ten_percent_is_quiet(self):
        # Linear law: 10% = 0.1 (-20 dB). Taper: -54 dB.
        self.assertLess(slider_to_gain(10), 0.01)

    def test_db_to_slider_inverts(self):
        for pos in (1, 25, 50, 77, 100):
            self.assertAlmostEqual(db_to_slider(_db(slider_to_gain(pos))), pos)
        self.assertEqual(db_to_slider(3.0), 100.0)
        self.assertEqual(db_to_slider(-200.0), 0.0)


class TestEngineUsesCurve(unittest.TestCase):

    def _engine(self):
        engine = _make_bare_engine()
        engine._active = _make_deck()
        engine._active.filepath = "a.wav"
        engine._idle = _make_deck()
        engine._idle.filepath = "b.wav"
        return engine

    def test_master_volume_gain_and_readback(self):
        engine = self._engine()
        with mock.patch("core.deck_engine.settings"):
            engine.set_master_volume(50)
        self.assertAlmostEqual(engine._master_volume, slider_to_gain(50))
        # Readback must be the fader position, not int(gain * 100).
        self.assertEqual(engine.master_volume, 50)

    def test_track_volume_gain_and_readback(self):
        engine = self._engine()
        with mock.patch("core.deck_engine.settings") as fake_settings:
            fake_settings.get.return_value = {}
            engine.set_track_volume(70, "a.wav")
        self.assertAlmostEqual(engine._active.track_volume, slider_to_gain(70))
        self.assertEqual(engine.track_volume, 70)
        self.assertEqual(engine._idle.track_volume_level, 100)


if __name__ == "__main__":
    unittest.main()

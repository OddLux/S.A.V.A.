"""dp-245 D7 -- the 200ms `QTimer.singleShot` deferrals in MainWindow used
bare lambdas closing over `self`. If the window is destroyed inside that
200ms window the callable fires against a dead C++ widget, which is a hard
crash in a real run rather than a catchable exception.

PyQt6's `QTimer.singleShot` has NO context-object overload (only
`(msec, slot)` and `(msec, timerType, slot)`), so Qt cannot be told to drop
the call itself; the deferred targets check `sip.isdeleted(self)` instead.

The original fix landed without a test, on the grounds that the destroy-race
had no test infrastructure. It does: `sip.delete()` destroys the C++ object
while leaving the Python wrapper alive -- exactly the state the race
produces.

The two deferrals fail DIFFERENTLY on a dead window, and the tests below say
so rather than assuming one shape for both:

  primary (`_kick_primary_analysis`) -- touches a widget immediately
      (`self._lbl_buffering.setText(...)`), so it raises RuntimeError
      synchronously.
  preview (`_kick_preview_analysis`) -- starts with a plain attribute
      compare, which survives C++ deletion, then spawns a decoder whose
      on_ready emits a signal on the dead QObject LATER, from the analyzer
      thread. Nothing raises synchronously; the damage is the spawned decode.
      So the discriminating assertion there is "no analyzer was constructed".

    ./venv/Scripts/python.exe -m pytest tests -q
"""

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PyQt6 import sip
from PyQt6.QtWidgets import QApplication

from ui.main_window import MainWindow

_app = None


def setUpModule():
    global _app
    _app = QApplication.instance() or QApplication(sys.argv)


class TestDeferredCallsSurviveWindowDestruction(unittest.TestCase):
    """Each deferred target must no-op once its window's C++ half is gone."""

    def setUp(self):
        self.window = MainWindow()

    def tearDown(self):
        if not sip.isdeleted(self.window):
            self.window.close()
            self.window.deleteLater()

    def test_deferred_primary_analysis_no_ops_after_destruction(self):
        sip.delete(self.window)
        self.assertTrue(sip.isdeleted(self.window))
        # Must not raise.
        self.window._deferred_primary_analysis("nonexistent.mp3")

    def test_unguarded_primary_analysis_would_have_crashed(self):
        """Discrimination: prove the primary guard is load-bearing.

        `_kick_primary_analysis` is what the old bare lambda called
        directly. It sets text on `self._lbl_buffering` before doing
        anything else, so on a destroyed window it raises immediately -- in
        a real (non-test) run that is the access violation the guard
        prevents."""
        sip.delete(self.window)
        with self.assertRaises(RuntimeError) as ctx:
            self.window._kick_primary_analysis("nonexistent.mp3")
        self.assertIn("has been deleted", str(ctx.exception))

    def test_deferred_preview_analysis_spawns_no_decode_after_destruction(self):
        """The preview guard's real job: don't start a decode whose result
        will emit a signal on a destroyed QObject from a worker thread."""
        self.window._preview_target = "x.mp3"
        sip.delete(self.window)
        with patch("ui.main_window.WaveformAnalyzer") as analyzer_cls:
            self.window._deferred_preview_analysis("x.mp3")
        self.assertFalse(
            analyzer_cls.called,
            "guard let a decode start against a destroyed window",
        )

    def test_unguarded_preview_analysis_would_have_spawned_a_decode(self):
        """Discrimination for the preview path. Note it does NOT raise
        synchronously -- asserting RuntimeError here would be wrong, and a
        test written that way would fail for the right-looking reason while
        describing the bug incorrectly."""
        self.window._preview_target = "x.mp3"
        sip.delete(self.window)
        with patch("ui.main_window.WaveformAnalyzer") as analyzer_cls:
            self.window._kick_preview_analysis("x.mp3")
        self.assertTrue(
            analyzer_cls.called,
            "expected the UNGUARDED path to start a decode -- if this fails "
            "the preview guard is being credited for protection it does not "
            "actually provide",
        )


class TestNoBareLambdaDeferralsRemain(unittest.TestCase):
    """The fix was applied per-call-site, so a NEW unguarded
    `singleShot(..., lambda ...)` can be added later and nothing would fail.
    This is the check that notices. It reads the source rather than the
    behaviour precisely because the risk is a site nobody remembered to
    guard -- the first pass at D7 fixed two of the three that existed, and
    the third (`_load_and_play`, the most reachable of them) was found only
    by grepping during review.
    """

    def test_no_singleshot_lambda_in_main_window(self):
        source = (
            Path(__file__).resolve().parent.parent / "ui" / "main_window.py"
        ).read_text(encoding="utf-8")
        offenders = [
            line.strip()
            for line in source.splitlines()
            if "singleShot" in line
            and "lambda" in line
            and not line.lstrip().startswith("#")
        ]
        self.assertEqual(
            offenders,
            [],
            "singleShot with a bare lambda closing over self -- route it "
            "through a _deferred_* helper that checks sip.isdeleted(self). "
            f"Offending line(s): {offenders}",
        )


if __name__ == "__main__":
    unittest.main()

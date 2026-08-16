"""
Regression test for dp-255 -- "Always on top" used to only persist the
setting; the window flag itself wasn't touched until the app restarted.
`_on_always_on_top` must now flip `WindowStaysOnTopHint` live and preserve
the window's geometry across the required re-show.

Plain unittest (matching the other UI tests here), collected by the suite:
    ./venv/Scripts/python.exe -m pytest tests -q
"""

import os
import sys
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication

from ui.main_window import MainWindow

_app = None


def setUpModule():
    global _app
    _app = QApplication.instance() or QApplication(sys.argv)


class TestAlwaysOnTopLiveToggle(unittest.TestCase):

    def setUp(self):
        self.window = MainWindow()

    def tearDown(self):
        self.window.close()
        self.window.deleteLater()

    def test_toggle_on_sets_flag_live(self):
        geo = self.window.geometry()
        self.window._on_always_on_top(True)
        self.assertTrue(
            bool(self.window.windowFlags() & Qt.WindowType.WindowStaysOnTopHint)
        )
        self.assertEqual(self.window.geometry(), geo)

    def test_toggle_off_clears_flag_live(self):
        self.window._on_always_on_top(True)
        geo = self.window.geometry()
        self.window._on_always_on_top(False)
        self.assertFalse(
            bool(self.window.windowFlags() & Qt.WindowType.WindowStaysOnTopHint)
        )
        self.assertEqual(self.window.geometry(), geo)


if __name__ == "__main__":
    unittest.main()

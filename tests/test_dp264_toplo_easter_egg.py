"""dp-264 -- the "Toplo" theme is a hidden Easter egg. It must be absent from
the Theme submenu on a fresh install, and only become selectable after the
About-dialog logo is clicked 6 times and the app is restarted (persisted flag
read once at menu-build time, matching how every other restart-applied theme
choice already works).

Plain unittest (matching the other UI tests here), collected by the suite:
    ./venv/Scripts/python.exe -m pytest tests -q
"""

import os
import re
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PyQt6.QtWidgets import QApplication

from config.settings import settings
from ui.main_window import MainWindow

_app = None


def setUpModule():
    global _app
    _app = QApplication.instance() or QApplication(sys.argv)


def _theme_menu_labels(window):
    """Pull the checkable action texts out of the live View -> Theme menu."""
    return [act.text() for act in window._theme_group.actions()]


class TestToploHiddenByDefault(unittest.TestCase):

    def setUp(self):
        settings.set("toplo_unlocked", False)

    def tearDown(self):
        self.window.close()
        self.window.deleteLater()
        settings.set("toplo_unlocked", False)

    def test_toplo_absent_from_menu_when_locked(self):
        self.window = MainWindow()
        self.assertNotIn("Toplo", _theme_menu_labels(self.window))

    def test_other_themes_still_present_when_locked(self):
        self.window = MainWindow()
        labels = _theme_menu_labels(self.window)
        for expected in ("Orange (default)", "Green", "Blue", "Purple"):
            self.assertIn(expected, labels)


class TestToploUnlockedAfterRestart(unittest.TestCase):

    def setUp(self):
        settings.set("toplo_unlocked", True)

    def tearDown(self):
        self.window.close()
        self.window.deleteLater()
        settings.set("toplo_unlocked", False)

    def test_toplo_present_once_unlocked(self):
        self.window = MainWindow()
        self.assertIn("Toplo", _theme_menu_labels(self.window))


class TestActiveThemeIsNeverHidden(unittest.TestCase):
    """settings["theme"] and settings["toplo_unlocked"] are independent, so
    they can disagree (hand-edited JSON, partial settings migration). The app
    applies `theme` regardless of the flag -- so hiding the entry for the
    theme currently in force would leave the window rendering in Toplo with
    nothing checked in the submenu and no way to tell what is active."""

    def setUp(self):
        settings.set("theme", "toplo")
        settings.set("toplo_unlocked", False)

    def tearDown(self):
        self.window.close()
        self.window.deleteLater()
        settings.set("theme", "orange")
        settings.set("toplo_unlocked", False)

    def test_active_toplo_is_listed_even_when_flag_is_false(self):
        self.window = MainWindow()
        self.assertIn("Toplo", _theme_menu_labels(self.window))

    def test_some_theme_is_always_checked(self):
        self.window = MainWindow()
        checked = [
            act.text() for act in self.window._theme_group.actions()
            if act.isChecked()
        ]
        self.assertEqual(checked, ["Toplo"])


class TestAboutLogoClickUnlock(unittest.TestCase):

    def setUp(self):
        settings.set("toplo_unlocked", False)
        self.window = MainWindow()

    def tearDown(self):
        self.window.close()
        self.window.deleteLater()
        settings.set("toplo_unlocked", False)

    def test_five_clicks_do_not_unlock(self):
        for _ in range(5):
            self.window._on_about_logo_clicked()
        self.assertFalse(settings.get("toplo_unlocked", False))

    def test_sixth_click_unlocks_and_persists(self):
        # The 6th click pops a modal confirmation (matching the existing
        # theme-change confirmation pattern in _on_theme_selected) -- patched
        # out here since exec()'ing a real modal in an offscreen test run
        # would hang forever with nothing to dismiss it.
        with patch("ui.main_window.QMessageBox.information"):
            for _ in range(6):
                self.window._on_about_logo_clicked()
        self.assertTrue(settings.get("toplo_unlocked", False))

    def test_unlock_does_not_change_the_menu_built_before_it(self):
        """The menu is only built once, at startup -- the ticket's spec
        requires a restart before the newly-unlocked theme appears. Clicking
        the logo mid-session must not mutate the already-built menu."""
        with patch("ui.main_window.QMessageBox.information"):
            for _ in range(6):
                self.window._on_about_logo_clicked()
        self.assertNotIn("Toplo", _theme_menu_labels(self.window))

    def test_clicks_do_not_carry_over_a_fresh_window(self):
        """The counter lives on the MainWindow instance, not settings --
        it is not meant to persist partial progress across app runs."""
        for _ in range(5):
            self.window._on_about_logo_clicked()
        self.window.close()
        self.window.deleteLater()

        fresh = MainWindow()
        try:
            fresh._on_about_logo_clicked()
            self.assertFalse(settings.get("toplo_unlocked", False))
        finally:
            fresh.close()
            fresh.deleteLater()


class TestAboutTextSliceStaysClean(unittest.TestCase):
    """tests/test_help_and_about_accuracy.py reads the About dialog's
    displayed text by slicing main_window.py's source from `def _on_about`
    to `def _on_help_instructions`, and asserts things like "pydub is not
    credited here". Any helper method parked between those two definitions
    silently becomes "About text" for that test -- so an unrelated string in
    such a helper could fail a credits assertion, or (worse) a real credits
    regression could be masked. This pins the slice to exactly one method.
    """

    def test_only_on_about_lives_in_the_about_slice(self):
        source = (
            Path(__file__).resolve().parent.parent / "ui" / "main_window.py"
        ).read_text(encoding="utf-8")
        start = source.index("def _on_about")
        end = source.index("def _on_help_instructions", start)
        defined = re.findall(r"def (\w+)", source[start:end])
        self.assertEqual(
            defined,
            ["_on_about"],
            "a method was defined between _on_about and _on_help_instructions "
            "-- move it elsewhere or test_help_and_about_accuracy.py will "
            f"treat its source as About text. Found: {defined}",
        )


if __name__ == "__main__":
    unittest.main()

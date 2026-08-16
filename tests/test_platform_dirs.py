"""dp-262: per-platform config/log directory resolution.

`core/platform_dirs.py` is the single shared helper for `config/settings.py`
(USER_CONFIG_DIR), `core/artnet_config.py` (_config_path), and `main.py`
(_get_log_dir). Windows behavior must not drift -- that's the shipping
platform. macOS/darwin gets the idiomatic Application Support / Logs dirs
instead of the old bare `~/SAVA`.
"""

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.platform_dirs import user_data_dir, user_log_dir


class TestUserDataDir(unittest.TestCase):

    def test_win32_uses_appdata(self):
        with mock.patch("sys.platform", "win32"), \
                mock.patch.dict("os.environ", {"APPDATA": r"C:\Users\test\AppData\Roaming"}):
            self.assertEqual(
                user_data_dir(),
                Path(r"C:\Users\test\AppData\Roaming") / "SAVA",
            )

    def test_win32_falls_back_to_home_without_appdata(self):
        with mock.patch("sys.platform", "win32"), \
                mock.patch.dict("os.environ", {}, clear=False):
            os.environ.pop("APPDATA", None)
            self.assertEqual(user_data_dir(), Path.home() / "SAVA")

    def test_darwin_uses_application_support(self):
        with mock.patch("sys.platform", "darwin"):
            self.assertEqual(
                user_data_dir(),
                Path.home() / "Library" / "Application Support" / "SAVA",
            )

    def test_other_platform_falls_back_to_home(self):
        with mock.patch("sys.platform", "linux"):
            self.assertEqual(user_data_dir(), Path.home() / "SAVA")


class TestUserLogDir(unittest.TestCase):

    def test_win32_matches_user_data_dir(self):
        with mock.patch("sys.platform", "win32"), \
                mock.patch.dict("os.environ", {"APPDATA": r"C:\Users\test\AppData\Roaming"}):
            self.assertEqual(user_log_dir(), user_data_dir())

    def test_darwin_uses_library_logs(self):
        with mock.patch("sys.platform", "darwin"):
            self.assertEqual(
                user_log_dir(),
                Path.home() / "Library" / "Logs" / "SAVA",
            )

    def test_other_platform_matches_user_data_dir(self):
        with mock.patch("sys.platform", "linux"):
            self.assertEqual(user_log_dir(), user_data_dir())


class TestSettingsCallSite(unittest.TestCase):
    """config/settings.py:USER_CONFIG_DIR is computed at import time from
    user_data_dir() on whatever platform imported it -- assert it matches
    the helper's result for the *current* platform (can't retroactively
    monkeypatch an already-evaluated module attribute)."""

    def test_user_config_dir_matches_helper(self):
        from config.settings import USER_CONFIG_DIR

        self.assertEqual(USER_CONFIG_DIR, user_data_dir())


class TestArtnetConfigCallSite(unittest.TestCase):
    """core/artnet_config.py:_config_path() -- exercised directly (unlike the
    module-level CONFIG_PATH constant) so each platform branch is testable."""

    def test_frozen_win32_uses_appdata(self):
        from core import artnet_config

        with mock.patch("core.artnet_config.sys.frozen", True, create=True), \
                mock.patch("sys.platform", "win32"), \
                mock.patch.dict("os.environ", {"APPDATA": r"C:\Users\test\AppData\Roaming"}), \
                mock.patch("pathlib.Path.mkdir", return_value=None):
            path = artnet_config._config_path()
        self.assertEqual(
            path,
            Path(r"C:\Users\test\AppData\Roaming") / "SAVA" / "artnet_config.ini",
        )

    def test_frozen_darwin_uses_application_support(self):
        from core import artnet_config

        with mock.patch("core.artnet_config.sys.frozen", True, create=True), \
                mock.patch("sys.platform", "darwin"), \
                mock.patch("pathlib.Path.mkdir", return_value=None):
            path = artnet_config._config_path()
        self.assertEqual(
            path,
            Path.home() / "Library" / "Application Support" / "SAVA" / "artnet_config.ini",
        )

    def test_not_frozen_uses_repo_config_dir(self):
        from core import artnet_config

        with mock.patch("core.artnet_config.sys.frozen", False, create=True):
            path = artnet_config._config_path()
        self.assertEqual(
            path,
            Path(artnet_config.__file__).resolve().parent.parent / "config" / "artnet_config.ini",
        )


class TestMainLogDirCallSite(unittest.TestCase):
    """main.py:_get_log_dir() -- import the module fresh under mocked
    platform/frozen state so each branch is exercised without relying on
    the real process's frozen-ness."""

    def _load_main_get_log_dir(self):
        import importlib.util

        main_path = Path(__file__).resolve().parent.parent / "main.py"
        spec = importlib.util.spec_from_file_location("_sava_main_under_test", main_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_frozen_win32_uses_appdata(self):
        with mock.patch("sys.platform", "win32"), \
                mock.patch("sys.frozen", True, create=True), \
                mock.patch.dict("os.environ", {"APPDATA": r"C:\Users\test\AppData\Roaming"}), \
                mock.patch("pathlib.Path.mkdir", return_value=None):
            module = self._load_main_get_log_dir()
        self.assertEqual(
            module.LOG_DIR,
            Path(r"C:\Users\test\AppData\Roaming") / "SAVA",
        )

    def test_frozen_darwin_uses_library_logs(self):
        with mock.patch("sys.platform", "darwin"), \
                mock.patch("sys.frozen", True, create=True), \
                mock.patch("pathlib.Path.mkdir", return_value=None):
            module = self._load_main_get_log_dir()
        self.assertEqual(
            module.LOG_DIR,
            Path.home() / "Library" / "Logs" / "SAVA",
        )


if __name__ == "__main__":
    unittest.main()

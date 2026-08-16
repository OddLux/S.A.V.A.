"""dp-252 / dp-258: prove the test suite cannot reach the real settings file.

dp-258 added the autouse fixture in `tests/conftest.py` that re-points the
`settings` singleton at a temp path. This module is the missing half dp-252
asked for -- a test that ASSERTS the isolation holds, so a future edit that
removes or weakens the fixture fails loudly here instead of silently
corrupting the developer's `config/sava_settings.json` (window position,
master volume and row-keyed maps all drifted before dp-258; the damage was
invisible because the suite stayed green either way).
"""

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import CONFIG_FILE, settings


class TestSettingsIsolation(unittest.TestCase):

    def test_settings_path_is_not_the_repo_config_file(self):
        self.assertNotEqual(
            Path(settings._path).resolve(),
            Path(CONFIG_FILE).resolve(),
            "settings singleton is pointed at the REAL dev config file -- the "
            "conftest.py isolation fixture (dp-258) is missing or broken",
        )

    def test_settings_path_is_outside_the_repo(self):
        repo_root = Path(__file__).resolve().parent.parent
        active = Path(settings._path).resolve()
        self.assertFalse(
            str(active).startswith(str(repo_root)),
            f"settings path {active} is inside the repo at {repo_root}; a test "
            "run would dirty the working tree",
        )

    def test_writing_a_setting_does_not_touch_the_real_file(self):
        # The discriminating half: actually persist something and prove the
        # real file's bytes are unchanged. Asserting on the path alone would
        # still pass if some other code path wrote to CONFIG_FILE directly.
        real = Path(CONFIG_FILE)
        before = real.read_bytes() if real.exists() else None

        settings.set("master_volume", 42)
        settings.save()

        after = real.read_bytes() if real.exists() else None
        self.assertEqual(
            before, after, "settings.save() wrote to the real dev config file"
        )
        self.assertTrue(
            Path(settings._path).exists(),
            "settings.save() did not write to the isolated temp path either",
        )


if __name__ == "__main__":
    unittest.main()

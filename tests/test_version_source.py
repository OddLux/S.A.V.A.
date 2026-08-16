"""dp-227: the VERSION file is the single source of version truth.

Two toolchains read it -- core/version.py (About dialog) and Inno Setup's
preprocessor (installer.iss `AppVersion`). These tests pin the contract that
keeps them from drifting, and guard the V1-path regression that made the
installer package the old V1 project's build instead of this project's.

    QT_QPA_PLATFORM=offscreen ./venv/Scripts/python.exe -m unittest discover tests
"""

import re
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from core import version as version_module  # noqa: E402

SEMVER = re.compile(r"^\d+\.\d+\.\d+")


class TestVersionFile(unittest.TestCase):

    def test_version_file_exists_at_repo_root(self):
        """installer.iss reads it with a path relative to itself, so it has
        to sit beside installer.iss at the repo root."""
        self.assertTrue((REPO_ROOT / "VERSION").is_file())

    def test_version_file_is_a_single_bare_version_line(self):
        """Inno Setup's FileRead takes the first line verbatim -- no comments,
        no `version = ...` prefix, nothing it would have to parse."""
        lines = (REPO_ROOT / "VERSION").read_text(encoding="utf-8").splitlines()
        non_empty = [ln for ln in lines if ln.strip()]
        self.assertEqual(len(non_empty), 1)
        self.assertRegex(non_empty[0].strip(), SEMVER)

    def test_get_version_matches_the_file(self):
        expected = (REPO_ROOT / "VERSION").read_text(encoding="utf-8").strip()
        self.assertEqual(version_module.get_version(), expected)

    def test_module_version_is_not_the_fallback(self):
        self.assertNotEqual(version_module.__version__, version_module._FALLBACK)

    def test_falls_back_instead_of_raising_when_unreadable(self):
        """Called while building the About dialog -- a missing bundled data
        file must not take the window down."""
        with mock.patch.object(
            version_module, "_version_file_candidates",
            return_value=iter([Path("nope/VERSION")]),
        ):
            self.assertEqual(version_module.get_version(), version_module._FALLBACK)

    def test_frozen_build_looks_in_meipass_first(self):
        """SAVA.spec bundles VERSION at the bundle root; a frozen build has no
        repo layout to fall back on."""
        with mock.patch.object(version_module.sys, "_MEIPASS", "C:/frozen", create=True):
            candidates = list(version_module._version_file_candidates())
        self.assertEqual(candidates[0], Path("C:/frozen") / "VERSION")


class TestInstallerScript(unittest.TestCase):

    def setUp(self):
        self.iss = (REPO_ROOT / "installer.iss").read_text(
            encoding="utf-8", errors="replace"
        )
        # Comment lines (`;`) are stripped for the V1/absolute-path guards
        # below: those must fail on a DIRECTIVE that points at another
        # project, not on prose explaining why it must not. The file's own
        # header names the old V1 paths deliberately, so asserting against
        # the raw text would make the comment unwritable.
        self.directives = "\n".join(
            line for line in self.iss.splitlines()
            if not line.lstrip().startswith(";")
        )

    def test_no_reference_to_the_v1_project(self):
        """The regression this ticket exists for: installer.iss sourced from
        an absolute path into a sibling V1 project directory, so the
        installer shipped V1's build and no V2 work ever reached an
        installed copy."""
        self.assertNotIn("SAVA_Claude_V1", self.directives)

    def test_no_absolute_paths_into_another_project(self):
        """Relative paths cannot silently resolve to a sibling project."""
        self.assertNotRegex(self.directives, r"(?i)[A-Z]:\\PythonProj")

    def test_sources_this_projects_own_build_output(self):
        self.assertIn(r'Source: "dist\SAVA\*"', self.directives)

    def test_app_version_is_read_from_the_version_file(self):
        self.assertIn('FileOpen("VERSION")', self.directives)
        self.assertIn("AppVersion={#AppVer}", self.directives)
        self.assertNotRegex(self.directives, r"AppVersion=\d")  # not hardcoded

    def test_icon_comes_from_this_projects_assets(self):
        self.assertIn(r"SetupIconFile=assets\sava.ico", self.directives)
        self.assertTrue((REPO_ROOT / "assets" / "sava.ico").is_file())


class TestSpecBundlesVersion(unittest.TestCase):

    def test_version_is_in_the_pyinstaller_datas(self):
        """Without this the frozen About dialog reports the fallback
        sentinel."""
        spec = (REPO_ROOT / "SAVA.spec").read_text(encoding="utf-8")
        self.assertIn("('VERSION', '.')", spec)


if __name__ == "__main__":
    unittest.main()

"""Suite-wide pytest fixtures.

dp-258: point the `settings` singleton at a throwaway file for the whole
test run. Without this, any test that persists a setting writes straight
into the developer's real `config/sava_settings.json` -- master volume,
window position, cue points and last playlist have all been clobbered by
test runs. dp-181 fixed exactly one offending test with a local
try/finally; that did not scale, and the leak came back the moment another
test persisted a value.

Isolation deliberately uses dp-181's ratified mechanism (swap
`settings._path`) rather than changing `config/settings.py`, so production
path resolution is untouched for real users.

NOTE: `conftest.py` is a pytest mechanism. It does NOT apply to
`python -m unittest discover tests`. CLAUDE.md documents pytest as this
project's runner (`./venv/Scripts/python.exe -m pytest tests -q`); if you
run the suite through unittest directly, your real settings file is still
exposed.
"""

import copy
import shutil
import tempfile
from pathlib import Path

import pytest


@pytest.fixture(autouse=True, scope="session")
def _isolate_settings():
    """Redirect the settings singleton to a temp file for the whole session.

    Session-scoped rather than per-test: the singleton is created once at
    import time, and re-pointing it per test would fight with modules that
    cache `settings.get(...)` results at import.
    """
    from config.settings import settings

    tmp_dir = Path(tempfile.mkdtemp(prefix="sava-test-settings_"))
    original_path = settings._path
    original_data = copy.deepcopy(settings._data)

    settings._path = tmp_dir / "sava_settings.json"
    try:
        yield
    finally:
        settings._path = original_path
        settings._data = original_data
        shutil.rmtree(tmp_dir, ignore_errors=True)

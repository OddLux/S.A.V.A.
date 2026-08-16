"""v3.0.0 release defaults (dp-251): a fresh-install shipping-default check.

`DEFAULT_INI` is what every new install actually gets -- `ArtNetConfig.
ensure_file_exists()` writes it verbatim the first time SAVA runs and no
`artnet_config.ini` exists yet (installed builds always hit this path, since
they always start from a fresh %APPDATA%). Pinned here so a future edit to
the default channel map is a deliberate, reviewed change, not an accidental
diff nobody noticed until a customer's console stopped responding.
"""

from pathlib import Path

import pytest

from core.artnet_config import DEFAULT_INI, FUNCTION_NAMES
from core.version import get_version


REPO_ROOT = Path(__file__).resolve().parent.parent


def _parse_default_ini():
    """Minimal INI parse -- avoids configparser's inline-comment handling
    differing subtly from ArtNetConfig.reload()'s own parser, which would
    make this test check its own parsing quirks instead of the file."""
    import configparser

    parser = configparser.ConfigParser(inline_comment_prefixes=("#", ";"))
    parser.read_string(DEFAULT_INI)
    return parser


@pytest.fixture(scope="module")
def parsed():
    return _parse_default_ini()


def test_default_channels_are_sequential_from_one_with_no_gaps_or_duplicates(parsed):
    """The pre-v3 defaults had a real bug alongside the ones the user asked to
    change: seek and track_select_enable both defaulted to channel 7, and
    channels 9-10 were skipped entirely before loop_ab. Fixed as a side effect
    of the requested renumbering -- pinned so it can't quietly come back."""
    channels = sorted(parsed.getint(fn, "channel") for fn in FUNCTION_NAMES)
    assert channels == list(range(1, len(FUNCTION_NAMES) + 1))


@pytest.mark.parametrize(
    "function, expected_channel",
    [
        ("play", 1), ("pause", 2), ("stop", 3), ("next_track", 4),
        ("prev_track", 5), ("master_volume", 6), ("seek", 7),
        ("track_select_enable", 8), ("track_select", 9), ("loop_ab", 10),
        ("cue_1", 11), ("cue_2", 12), ("cue_3", 13), ("cue_4", 14),
        ("cue_5", 15), ("cue_6", 16), ("cue_7", 17), ("cue_8", 18),
    ],
)
def test_default_channel_assignment(parsed, function, expected_channel):
    assert parsed.getint(function, "channel") == expected_channel


@pytest.mark.parametrize(
    "function", ["master_volume", "seek", "track_select_enable"]
)
def test_the_three_requested_functions_default_to_disabled(parsed, function):
    assert parsed.get(function, "enabled").strip() == "-"


@pytest.mark.parametrize(
    "function",
    [
        "play", "pause", "stop", "next_track", "prev_track",
        "track_select", "loop_ab",
        "cue_1", "cue_2", "cue_3", "cue_4", "cue_5", "cue_6", "cue_7", "cue_8",
    ],
)
def test_every_other_function_defaults_to_enabled(parsed, function):
    """Guards the flip side: the three explicitly-requested functions are
    disabled and NOTHING ELSE was accidentally caught in the same edit."""
    assert parsed.get(function, "enabled").strip() == "+"


def test_version_is_on_the_v3_line():
    """Pins the RELEASE LINE, not an exact string.

    This originally asserted `== "3.0.0"`, which broke on the very next patch
    release -- a test that has to be edited every time the version changes is
    not pinning anything, it is just friction, and the pressure is always to
    bump the literal without thinking about what it was guarding.

    What actually matters here is that these v3.0 defaults ship on a v3 build.
    That `get_version()` agrees with the VERSION file is a separate contract,
    already covered by tests/test_version_source.py -- not duplicated here.
    """
    raw = (REPO_ROOT / "VERSION").read_text(encoding="utf-8").strip()
    assert raw == get_version()
    major = int(raw.split(".")[0])
    assert major >= 3, f"v3.0 defaults are pinned but VERSION is {raw}"


def test_default_theme_is_still_orange():
    from config.settings import DEFAULT_SETTINGS

    assert DEFAULT_SETTINGS["theme"] == "orange"

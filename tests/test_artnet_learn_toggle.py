"""dp-249: the ArtNet dialog's Learn button is a TOGGLE.

Arming Learn used to disable the button that armed it, so the only ways out
were to send a DMX change (assigning a channel you may not have wanted) or to
close the dialog. Arming a row by mistake left it listening with no way to
stop.

The load-bearing assertion here is `isEnabled()`, not just the internal
`_learning_row` state: a fix that cleared the flag correctly but left the
button disabled would still be unusable, because the user has nothing to click.
"""

import sys

import pytest

pytest.importorskip("PyQt6")

from PyQt6.QtWidgets import QApplication

from ui.artnet_map_window import ArtNetMapWindow


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication(sys.argv)


class _FakeBridge:
    """Stands in for ArtNetBridge: records arm/disarm without a socket."""

    def __init__(self):
        self.is_running = True
        self.armed_callback = None
        self.disarm_calls = 0

    def arm_learn(self, callback):
        self.armed_callback = callback

    def disarm_learn(self):
        self.armed_callback = None
        self.disarm_calls += 1


@pytest.fixture
def dialog(app, monkeypatch):
    bridge = _FakeBridge()
    monkeypatch.setattr("ui.artnet_map_window._bridge", lambda: bridge)
    dlg = ArtNetMapWindow()
    dlg._fake_bridge = bridge
    return dlg


def _btn(dlg, row):
    return dlg._row_widgets[row]["learn_btn"]


def test_first_click_arms_the_row(dialog):
    dialog._on_learn_clicked(0)
    assert dialog._learning_row == 0
    assert dialog._fake_bridge.armed_callback is not None


def test_armed_button_stays_clickable(dialog):
    """The regression, stated directly. The old code disabled this button,
    which is what made Learn impossible to turn off."""
    dialog._on_learn_clicked(0)
    assert _btn(dialog, 0).isEnabled() is True


def test_second_click_on_the_same_row_disarms(dialog):
    dialog._on_learn_clicked(0)
    dialog._on_learn_clicked(0)

    assert dialog._learning_row is None
    assert dialog._fake_bridge.armed_callback is None
    assert dialog._fake_bridge.disarm_calls >= 1
    assert _btn(dialog, 0).text() == "Learn"
    assert _btn(dialog, 0).isEnabled() is True


def test_toggling_is_repeatable(dialog):
    """On/off/on/off must keep working -- not just survive one cycle."""
    for _ in range(3):
        dialog._on_learn_clicked(1)
        assert dialog._learning_row == 1
        dialog._on_learn_clicked(1)
        assert dialog._learning_row is None


def test_clicking_a_different_row_moves_the_arm(dialog):
    """Exactly one row may listen at a time, and the abandoned row must be
    restored to a usable state."""
    dialog._on_learn_clicked(0)
    dialog._on_learn_clicked(2)

    assert dialog._learning_row == 2
    assert _btn(dialog, 0).text() == "Learn"
    assert _btn(dialog, 0).isEnabled() is True
    assert _btn(dialog, 2).text() == "Cancel"
    assert _btn(dialog, 2).isEnabled() is True


def test_a_learned_channel_restores_the_button(dialog):
    """The success path must also leave the button usable, not stuck on
    'Cancel'."""
    dialog._on_learn_clicked(0)
    dialog._fake_bridge.armed_callback(7)

    assert dialog._learning_row is None
    assert dialog._row_widgets[0]["channel"].value() == 7
    assert _btn(dialog, 0).text() == "Learn"
    assert _btn(dialog, 0).isEnabled() is True


def test_cancel_does_not_assign_a_channel(dialog):
    """Cancelling must leave the row's channel untouched -- the whole reason
    to cancel is usually 'I armed the wrong row'."""
    before = dialog._row_widgets[0]["channel"].value()
    dialog._on_learn_clicked(0)
    dialog._on_learn_clicked(0)
    assert dialog._row_widgets[0]["channel"].value() == before


def test_cancel_is_safe_when_the_listener_is_stopped(dialog):
    """Disarming must not be gated behind the is_running check that guards
    ARMING -- otherwise stopping the listener while a row is armed would trap
    it again, which is the exact bug in a different disguise."""
    dialog._on_learn_clicked(0)
    dialog._fake_bridge.is_running = False

    dialog._on_learn_clicked(0)

    assert dialog._learning_row is None
    assert _btn(dialog, 0).text() == "Learn"

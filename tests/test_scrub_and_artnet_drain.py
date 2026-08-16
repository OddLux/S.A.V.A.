"""dp-247: waveform drag no longer seeks per mouse-move, and the ArtNet drain
cap is a runaway guard rather than a throughput ceiling.

Tests assert on the observable side effect (how many seek signals a drag
actually emits), never on internal flags alone -- a flag can be set correctly
while the behaviour it is supposed to gate still happens.

An analyzer-cancellation fix was reverted (dp-248): it needed `analyze()` to
reset its cancelled flag, and without that a reused analyzer would silently
suppress every later decode. Not reintroduced without that reset and a test
for it.
"""

import sys

import pytest

pytest.importorskip("PyQt6")

from PyQt6.QtWidgets import QApplication

from ui.waveform_widget import WaveformWidget


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication(sys.argv)


# -- D2: scrubbing is silent until release -----------------------------


class _Recorder:
    def __init__(self):
        self.seeks = []

    def __call__(self, pos):
        self.seeks.append(pos)


def _press(widget, x):
    from PyQt6.QtCore import QPointF, Qt
    from PyQt6.QtGui import QMouseEvent

    return QMouseEvent(
        QMouseEvent.Type.MouseButtonPress, QPointF(x, 10.0),
        Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )


def _move(widget, x):
    from PyQt6.QtCore import QPointF, Qt
    from PyQt6.QtGui import QMouseEvent

    return QMouseEvent(
        QMouseEvent.Type.MouseMove, QPointF(x, 10.0),
        Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )


def _release(widget, x):
    from PyQt6.QtCore import QPointF, Qt
    from PyQt6.QtGui import QMouseEvent

    return QMouseEvent(
        QMouseEvent.Type.MouseButtonRelease, QPointF(x, 10.0),
        Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
    )


@pytest.fixture
def waveform(app):
    import numpy as np

    w = WaveformWidget()
    w.resize(1000, 100)
    w.set_waveform(np.zeros(2000, dtype=np.float32), duration=100.0)
    return w


def test_drag_emits_exactly_one_seek_on_release(waveform):
    """The regression. A 20-move drag used to emit 21 seeks -- each one a
    blocking engine.seek() plus a full _rearm_preload() on the Qt thread."""
    rec = _Recorder()
    waveform.seek_requested.connect(rec)

    waveform.mousePressEvent(_press(waveform, 100.0))
    for x in range(110, 310, 10):
        waveform.mouseMoveEvent(_move(waveform, float(x)))
    waveform.mouseReleaseEvent(_release(waveform, 300.0))

    assert len(rec.seeks) == 1


def test_the_single_seek_is_at_the_release_position(waveform):
    """Committing on release must commit where the user LET GO, not where
    they first clicked -- otherwise the drag would be visually correct and
    audibly wrong."""
    rec = _Recorder()
    waveform.seek_requested.connect(rec)

    waveform.mousePressEvent(_press(waveform, 100.0))
    waveform.mouseMoveEvent(_move(waveform, 750.0))
    waveform.mouseReleaseEvent(_release(waveform, 750.0))

    # 750/1000 of a 100s track
    assert rec.seeks == [pytest.approx(75.0)]


def test_needle_follows_the_cursor_during_the_drag(waveform):
    """Silent does not mean frozen -- the needle must still track the drag,
    otherwise there is no feedback about where you are about to land."""
    waveform.mousePressEvent(_press(waveform, 100.0))
    waveform.mouseMoveEvent(_move(waveform, 400.0))

    assert waveform._position == pytest.approx(40.0)
    assert waveform.is_scrubbing is True


def test_scrubbing_flag_clears_on_release(waveform):
    waveform.mousePressEvent(_press(waveform, 100.0))
    assert waveform.is_scrubbing is True
    waveform.mouseReleaseEvent(_release(waveform, 100.0))
    assert waveform.is_scrubbing is False


def test_moves_without_a_press_emit_nothing(waveform):
    """Plain hover must not scrub."""
    rec = _Recorder()
    waveform.seek_requested.connect(rec)
    waveform.mouseMoveEvent(_move(waveform, 500.0))
    assert rec.seeks == []


# -- D5: the ArtNet drain cap is a guard, not a throughput budget --------


def test_artnet_drain_cap_is_far_above_realistic_traffic():
    """The old cap of 50 packets per 50ms tick = 1000 pkt/s, which a rig
    broadcasting ~23 universes at the standard 44Hz refresh exceeds -- past
    that, packets are dropped by the kernel and a dropped packet on a trigger
    channel is a MISSED CUE.

    Pinned as a number so a future 'tidy up' cannot quietly reintroduce a
    throughput ceiling: 4000/tick = 80k pkt/s, ~1800 universes at 44Hz.
    """
    from core.artnet_bridge import _MAX_PACKETS_PER_TICK

    ticks_per_second = 1000 / 50
    packets_per_second = _MAX_PACKETS_PER_TICK * ticks_per_second
    universes_at_44hz = packets_per_second / 44

    assert universes_at_44hz > 1000


def test_artnet_requests_a_larger_receive_buffer():
    """The kernel default (~64KB) holds only ~120 Art-Net packets."""
    from core.artnet_bridge import _RCVBUF_BYTES

    assert _RCVBUF_BYTES >= 512 * 1024

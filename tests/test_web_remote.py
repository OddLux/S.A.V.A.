"""dp-267: LAN web viewer/control server. Headless: binds 127.0.0.1, port 0."""

import json
import os
import socket
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from core.web_remote import WebRemoteServer, downsample_waveform, parse_command


@pytest.fixture
def remote():
    srv = WebRemoteServer()
    srv.start(0, host="127.0.0.1")
    yield srv
    srv.stop()


def _url(srv, path):
    return f"http://127.0.0.1:{srv.port}{path}"


def _get(srv, path):
    try:
        with urllib.request.urlopen(_url(srv, path), timeout=3) as res:
            return res.status, res.read()
    except urllib.error.HTTPError as err:
        return err.code, err.read()


def _post(srv, payload, raw=None):
    body = raw if raw is not None else json.dumps(payload).encode()
    req = urllib.request.Request(
        _url(srv, "/api/command"), data=body, method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=3) as res:
            return res.status
    except urllib.error.HTTPError as err:
        return err.code


def test_pages_404_when_disabled(remote):
    for path in ("/", "/view", "/control", "/api/state", "/api/playlist"):
        assert _get(remote, path)[0] == 404, path


def test_viewer_and_control_gated_independently(remote):
    remote.set_viewer_enabled(True)
    assert _get(remote, "/view")[0] == 200
    assert _get(remote, "/control")[0] == 404
    remote.set_viewer_enabled(False)
    remote.set_control_enabled(True)
    assert _get(remote, "/view")[0] == 404
    status, body = _get(remote, "/control")
    assert status == 200
    assert b"play_index" in body  # the control JS is present


def test_viewer_page_has_no_control_wiring(remote):
    remote.set_viewer_enabled(True)
    _, view = _get(remote, "/view")
    assert b"var CTRL=false" in view
    remote.set_control_enabled(True)
    _, ctrl = _get(remote, "/control")
    assert b"var CTRL=true" in ctrl


def test_pages_are_offline_only(remote):
    remote.set_viewer_enabled(True)
    remote.set_control_enabled(True)
    for path in ("/", "/view", "/control"):
        text = _get(remote, path)[1].decode()
        assert "http://" not in text and "https://" not in text, path


def test_command_403_when_control_disabled(remote):
    remote.set_viewer_enabled(True)  # viewer alone must not grant commands
    seen = []
    remote.on_command = lambda name, arg: seen.append((name, arg))
    assert _post(remote, {"cmd": "play"}) == 403
    assert seen == []


def test_command_rejects_non_json_content_type(remote):
    # CSRF guard: a cross-site "simple" POST (text/plain) must not drive playback.
    remote.set_control_enabled(True)
    seen = []
    remote.on_command = lambda name, arg: seen.append((name, arg))
    req = urllib.request.Request(
        _url(remote, "/api/command"), data=b'{"cmd": "stop"}', method="POST",
        headers={"Content-Type": "text/plain"},
    )
    try:
        with urllib.request.urlopen(req, timeout=3) as res:
            status = res.status
    except urllib.error.HTTPError as err:
        status = err.code
    assert status == 415
    assert seen == []


def test_command_dispatch_parses_name_and_arg(remote):
    remote.set_control_enabled(True)
    seen = []
    remote.on_command = lambda name, arg: seen.append((name, arg))
    assert _post(remote, {"cmd": "play"}) == 200
    assert _post(remote, {"cmd": "volume", "arg": 42}) == 200
    assert _post(remote, {"cmd": "seek", "arg": 12.5}) == 200
    assert _post(remote, {"cmd": "play_index", "arg": 3}) == 200
    assert seen == [
        ("play", None), ("volume", 42), ("seek", 12.5), ("play_index", 3),
    ]


@pytest.mark.parametrize(
    "payload",
    [
        {"cmd": "format_disk"},
        {"cmd": "volume"},
        {"cmd": "volume", "arg": 101},
        {"cmd": "volume", "arg": -1},
        {"cmd": "volume", "arg": 5.5},
        {"cmd": "volume", "arg": "50"},
        {"cmd": "volume", "arg": True},
        {"cmd": "seek", "arg": -3},
        {"cmd": "seek", "arg": "abc"},
        {"cmd": "play_index", "arg": -1},
        {"cmd": "play_index", "arg": 1.5},
        {"arg": 1},
        [1, 2],
        "play",
    ],
)
def test_bad_payloads_rejected_with_400(remote, payload):
    remote.set_control_enabled(True)
    seen = []
    remote.on_command = lambda name, arg: seen.append((name, arg))
    assert _post(remote, payload) == 400
    assert seen == []


def test_non_json_and_oversize_body_400(remote):
    remote.set_control_enabled(True)
    assert _post(remote, None, raw=b"not json") == 400
    assert _post(remote, None, raw=b"x" * 10000) == 400


def test_parse_command_rejects_nan_inf():
    with pytest.raises(ValueError):
        parse_command({"cmd": "seek", "arg": float("inf")})
    with pytest.raises(ValueError):
        parse_command({"cmd": "seek", "arg": float("nan")})


def test_state_endpoint_returns_published_snapshot(remote):
    remote.set_viewer_enabled(True)
    remote.publish_state({"title": "Song", "pos": 1.5})
    status, body = _get(remote, "/api/state")
    data = json.loads(body)
    assert status == 200
    assert data["title"] == "Song" and data["pos"] == 1.5


def test_playlist_and_waveform_revisions_bump_only_on_change(remote):
    remote.set_viewer_enabled(True)
    remote.publish_state({})
    rev0 = json.loads(_get(remote, "/api/state")[1])
    remote.publish_playlist([{"t": "A", "d": 1.0}])
    remote.publish_playlist([{"t": "A", "d": 1.0}])  # identical: no bump
    remote.publish_waveform([1, 2, 3])
    remote.publish_waveform([1, 2, 3])
    remote.publish_state({})
    rev1 = json.loads(_get(remote, "/api/state")[1])
    assert rev1["pl_rev"] == rev0["pl_rev"] + 1
    assert rev1["wf_rev"] == rev0["wf_rev"] + 1
    assert json.loads(_get(remote, "/api/playlist")[1]) == [{"t": "A", "d": 1.0}]
    assert json.loads(_get(remote, "/api/waveform")[1]) == [1, 2, 3]


def test_sse_emits_data_event(remote):
    remote.set_viewer_enabled(True)
    remote.publish_state({"title": "Live"})
    with urllib.request.urlopen(_url(remote, "/api/events"), timeout=3) as res:
        assert res.headers["Content-Type"].startswith("text/event-stream")
        line = res.readline().decode()
    assert line.startswith("data: ")
    assert json.loads(line[6:])["title"] == "Live"


def test_sse_404_when_disabled(remote):
    assert _get(remote, "/api/events")[0] == 404


def test_sse_client_disconnect_does_not_break_server(remote):
    remote.set_viewer_enabled(True)
    remote.publish_state({"n": 0})
    sock = socket.create_connection(("127.0.0.1", remote.port), timeout=3)
    sock.sendall(b"GET /api/events HTTP/1.0\r\n\r\n")
    buf = b""
    while b"data:" not in buf:
        chunk = sock.recv(4096)
        assert chunk, "server closed before sending an event"
        buf += chunk
    sock.close()
    for idx in range(1, 4):
        remote.publish_state({"n": idx})
    assert _get(remote, "/api/state")[0] == 200


def test_stop_releases_the_port():
    srv = WebRemoteServer()
    srv.start(0, host="127.0.0.1")
    port = srv.port
    srv.stop()
    assert not srv.running
    probe = socket.socket()
    try:
        probe.bind(("127.0.0.1", port))  # fails if the port is still held
    finally:
        probe.close()


def test_start_on_busy_port_raises_oserror():
    holder = socket.socket()
    holder.bind(("127.0.0.1", 0))
    holder.listen(1)
    try:
        srv = WebRemoteServer()
        with pytest.raises(OSError):
            srv.start(holder.getsockname()[1], host="127.0.0.1")
        assert not srv.running
    finally:
        holder.close()


def test_downsample_waveform():
    pts = downsample_waveform([0.0, 1.0] * 1000, points=500)
    assert len(pts) == 500 and set(pts) == {100}
    assert downsample_waveform([]) == []
    assert downsample_waveform([0.5, 0.25]) == [50, 25]


# -- MainWindow dispatch ------------------------------------------------------


def test_main_window_dispatch_maps_commands_to_handlers():
    from PyQt6.QtWidgets import QApplication

    from ui.main_window import MainWindow

    app = QApplication.instance() or QApplication(sys.argv)
    win = MainWindow()
    try:
        calls = []
        win._on_play = lambda: calls.append(("play",))
        win._on_pause = lambda: calls.append(("pause",))
        win._on_stop = lambda: calls.append(("stop",))
        win._on_next = lambda: calls.append(("next",))
        win._on_prev = lambda: calls.append(("prev",))
        win._on_seek_requested = lambda pos: calls.append(("seek", pos))
        win._on_playlist_activated = lambda idx: calls.append(("idx", idx))
        for name in ("play", "pause", "stop", "next", "prev"):
            win._on_web_command(name, None)
        win._on_web_command("seek", 7.5)
        win._on_web_command("play_index", 10**6)  # out of range: ignored
        assert calls == [
            ("play",), ("pause",), ("stop",), ("next",), ("prev",), ("seek", 7.5),
        ]
        # Volume must reach the transport slider, not just the engine.
        from core.engine import engine

        before = engine.master_volume
        try:
            win._on_web_command("volume", 33)
            assert engine.master_volume == 33
            assert win._transport._master_vol.value() == 33
        finally:
            engine.set_master_volume(before)
        app.processEvents()
    finally:
        win.close()
        win.deleteLater()

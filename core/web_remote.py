"""LAN web viewer / remote control server (dp-267).

Stdlib only. The server runs on daemon threads and NEVER touches Qt or the
engine: the app publishes state snapshots into it from the Qt main thread, and
incoming commands are handed to ``on_command(name, arg)`` on the HTTP thread --
MainWindow wires that callback to a pyqtSignal emit, which marshals the command
back onto the main thread (see CLAUDE.md, Threading model).
"""

import json
import math
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from core.web_pages import render_landing, render_page

DEFAULT_PORT = 8765
MAX_BODY_BYTES = 4096
SSE_KEEPALIVE_SECONDS = 15.0

# Commands that take no argument.
_SIMPLE_COMMANDS = ("play", "pause", "stop", "next", "prev")
COMMAND_NAMES = _SIMPLE_COMMANDS + ("volume", "seek", "play_index")


def parse_command(payload):
    """Validate a decoded JSON command body; return (name, arg).

    Raises ValueError on anything malformed or not whitelisted.
    """
    if not isinstance(payload, dict):
        raise ValueError("body must be a JSON object")
    name = payload.get("cmd")
    if not isinstance(name, str) or name not in COMMAND_NAMES:
        raise ValueError("unknown command")
    arg = payload.get("arg")
    if name in _SIMPLE_COMMANDS:
        return name, None
    if isinstance(arg, bool) or not isinstance(arg, (int, float)):
        raise ValueError("%s needs a numeric arg" % name)
    if isinstance(arg, float) and not math.isfinite(arg):
        raise ValueError("arg must be finite")
    if name == "volume":
        if arg != int(arg) or not 0 <= arg <= 100:
            raise ValueError("volume must be an integer 0-100")
        return name, int(arg)
    if name == "seek":
        if arg < 0:
            raise ValueError("seek must be >= 0")
        return name, float(arg)
    if arg != int(arg) or arg < 0:
        raise ValueError("play_index must be an integer >= 0")
    return name, int(arg)


def lan_addresses():
    """Return this machine's LAN IPv4 addresses; works with no internet.

    Connecting a UDP socket sends no packets; it only makes the OS pick the
    outgoing interface for a private address. 127.x is returned only when
    nothing better exists.
    """
    found = []
    for target in ("10.255.255.255", "192.168.255.255", "172.16.255.255"):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.connect((target, 1))
            addr = sock.getsockname()[0]
        except OSError:
            continue
        finally:
            sock.close()
        if addr and addr not in found:
            found.append(addr)
    try:
        for addr in socket.gethostbyname_ex(socket.gethostname())[2]:
            if addr not in found:
                found.append(addr)
    except OSError:
        pass
    real = [a for a in found if not a.startswith(("127.", "0.", "169.254."))]
    return real or [a for a in found if a.startswith("127.")] or ["127.0.0.1"]


def downsample_waveform(values, points=500):
    """Reduce a 0-1 waveform to at most `points` ints in 0-100 (max per bucket)."""
    values = list(values)
    count = len(values)
    if count == 0:
        return []
    points = min(points, count)
    out = []
    for idx in range(points):
        lo = idx * count // points
        hi = max(lo + 1, (idx + 1) * count // points)
        peak = max(values[lo:hi])
        out.append(int(round(max(0.0, min(1.0, float(peak))) * 100)))
    return out


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    # Reuse would let a second SAVA silently share the port on Windows.
    allow_reuse_address = False

    def handle_error(self, request, client_address):
        # A browser closing a tab mid-response is routine, not a traceback.
        pass


class _Handler(BaseHTTPRequestHandler):
    server_version = "SAVA"

    def log_message(self, format, *args):
        pass

    # -- helpers -----------------------------------------------------------

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, code, obj):
        self._send(code, json.dumps(obj, separators=(",", ":")))

    def _gate_data(self, remote):
        """Data endpoints are available to either interface."""
        if remote.viewer_enabled or remote.control_enabled:
            return True
        self._send_json(404, {"error": "disabled"})
        return False

    # -- routes ------------------------------------------------------------

    def do_GET(self):
        remote = self.server.remote
        path = self.path.split("?", 1)[0]
        try:
            if path == "/":
                if not (remote.viewer_enabled or remote.control_enabled):
                    self._send(404, "disabled", "text/plain")
                    return
                self._send(
                    200,
                    render_landing(remote.viewer_enabled, remote.control_enabled),
                    "text/html; charset=utf-8",
                )
            elif path == "/view":
                if not remote.viewer_enabled:
                    self._send(404, "Web viewer is disabled", "text/plain")
                    return
                self._send(200, render_page(False), "text/html; charset=utf-8")
            elif path == "/control":
                if not remote.control_enabled:
                    self._send(404, "Web control is disabled", "text/plain")
                    return
                self._send(200, render_page(True), "text/html; charset=utf-8")
            elif path == "/api/state":
                if self._gate_data(remote):
                    self._send(200, remote.state_json())
            elif path == "/api/waveform":
                if self._gate_data(remote):
                    self._send(200, remote.waveform_json())
            elif path == "/api/playlist":
                if self._gate_data(remote):
                    self._send(200, remote.playlist_json())
            elif path == "/api/events":
                if self._gate_data(remote):
                    self._serve_events(remote)
            else:
                self._send(404, "not found", "text/plain")
        except (OSError, ValueError):
            # Client went away mid-write.
            pass

    def _serve_events(self, remote):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        seen = -1
        try:
            while True:
                payload, seen = remote.wait_for_state(seen, SSE_KEEPALIVE_SECONDS)
                if remote.stopping:
                    return
                if payload is None:
                    self.wfile.write(b": keepalive\n\n")
                else:
                    self.wfile.write(b"data: " + payload + b"\n\n")
                self.wfile.flush()
        except (OSError, ValueError):
            return

    def do_POST(self):
        remote = self.server.remote
        path = self.path.split("?", 1)[0]
        if path != "/api/command":
            self._send(404, "not found", "text/plain")
            return
        if not remote.control_enabled:
            self._send_json(403, {"error": "control disabled"})
            return
        # CSRF guard: a cross-site page can only send a "simple" POST
        # (text/plain, form) without a CORS preflight. Requiring JSON forces
        # the preflight, which this server never answers, so browsers block
        # foreign pages from driving playback. Our own page sends JSON.
        ctype = self.headers.get("Content-Type", "").split(";", 1)[0].strip()
        if ctype.lower() != "application/json":
            self._send_json(415, {"error": "Content-Type must be application/json"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = -1
        if length < 0 or length > MAX_BODY_BYTES:
            self._send_json(400, {"error": "bad body size"})
            return
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            name, arg = parse_command(payload)
        except (ValueError, UnicodeDecodeError) as err:
            self._send_json(400, {"error": str(err)})
            return
        callback = remote.on_command
        if callback is not None:
            callback(name, arg)
        self._send_json(200, {"ok": True})


class WebRemoteServer:
    """Threaded HTTP/SSE server exposing a read-only viewer and a control page."""

    def __init__(self):
        self.on_command = None  # callable(name, arg); runs on an HTTP thread
        self.viewer_enabled = False
        self.control_enabled = False
        self.stopping = False
        self._httpd = None
        self._thread = None
        self._cond = threading.Condition()
        self._state = {}
        self._state_rev = 0
        self._state_bytes = b"{}"
        self._playlist = []
        self._playlist_rev = 0
        self._playlist_bytes = b"[]"
        self._waveform = []
        self._waveform_rev = 0
        self._waveform_bytes = b"[]"

    # -- lifecycle -----------------------------------------------------------

    @property
    def running(self):
        return self._httpd is not None

    @property
    def port(self):
        return self._httpd.server_address[1] if self._httpd else None

    def start(self, port=DEFAULT_PORT, host="0.0.0.0"):
        """Bind and serve on a daemon thread. Raises OSError if the bind fails."""
        if self._httpd is not None:
            return
        httpd = _Server((host, port), _Handler)
        httpd.remote = self
        self.stopping = False
        self._httpd = httpd
        self._thread = threading.Thread(
            target=httpd.serve_forever,
            kwargs={"poll_interval": 0.2},
            name="web-remote",
            daemon=True,
        )
        self._thread.start()

    def stop(self):
        """Stop serving and release the port."""
        httpd, thread = self._httpd, self._thread
        if httpd is None:
            return
        self._httpd = None
        self._thread = None
        with self._cond:
            self.stopping = True
            self._cond.notify_all()
        httpd.shutdown()
        httpd.server_close()
        if thread is not None:
            thread.join(timeout=2.0)

    def set_viewer_enabled(self, enabled):
        self.viewer_enabled = bool(enabled)

    def set_control_enabled(self, enabled):
        self.control_enabled = bool(enabled)

    # -- publishing (called from the Qt main thread) ---------------------------

    def publish_state(self, state):
        """Replace the latest snapshot and wake SSE clients."""
        with self._cond:
            snap = dict(state)
            snap["pl_rev"] = self._playlist_rev
            snap["wf_rev"] = self._waveform_rev
            self._state = snap
            self._state_rev += 1
            self._state_bytes = json.dumps(snap, separators=(",", ":")).encode()
            self._cond.notify_all()

    def publish_playlist(self, items):
        """Publish the playlist (list of dicts); the revision bumps only on change."""
        items = [dict(item) for item in items]
        with self._cond:
            if items == self._playlist:
                return
            self._playlist = items
            self._playlist_rev += 1
            self._playlist_bytes = json.dumps(items, separators=(",", ":")).encode()

    def publish_waveform(self, points):
        """Publish waveform points (already downsampled); bumps rev on change."""
        points = list(points)
        with self._cond:
            if points == self._waveform:
                return
            self._waveform = points
            self._waveform_rev += 1
            self._waveform_bytes = json.dumps(points, separators=(",", ":")).encode()

    # -- readers (HTTP threads) ------------------------------------------------

    def state_json(self):
        with self._cond:
            return self._state_bytes

    def playlist_json(self):
        with self._cond:
            return self._playlist_bytes

    def waveform_json(self):
        with self._cond:
            return self._waveform_bytes

    def wait_for_state(self, seen_rev, timeout):
        """Block until a state newer than `seen_rev` exists, or timeout.

        Returns (payload_bytes_or_None, rev). None means keepalive/stopping.
        """
        with self._cond:
            if self._state_rev == seen_rev and not self.stopping:
                self._cond.wait(timeout)
            if self.stopping or self._state_rev == seen_rev:
                return None, seen_rev
            return self._state_bytes, self._state_rev

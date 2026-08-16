"""
ArtNet bridge — single-threaded, QTimer-based.
Reads config from the INI file via artnet_config.
"""

import socket
import struct
from PyQt6.QtCore import QObject, QTimer

from core.artnet_config import artnet_config

ARTNET_PORT   = 6454
ARTNET_HEADER = b"Art-Net\x00"
OPCODE_DMX    = 0x5000

#: Requested kernel receive-buffer size. ~2000 Art-Net DMX packets at 530
#: bytes each -- roughly a second of headroom for a very busy rig, versus the
#: ~120 packets the OS default holds. See `start()`.
_RCVBUF_BYTES = 1 << 20  # 1 MiB

#: Upper bound on packets consumed per 50ms timer tick. This is a runaway
#: guard, NOT a throughput budget: the loop normally exits on BlockingIOError
#: (socket empty) long before reaching it.
#:
#: It replaces a cap of 50, which WAS a throughput budget -- 50 packets per
#: 50ms tick = 1000 packets/sec, and Art-Net is broadcast so SAVA receives
#: every universe on the wire, not just its own. One console sending one
#: universe at the standard 44Hz refresh is 44 packets/sec (22x headroom), but
#: ~23 universes broadcasting at 44Hz saturates it. Past that point the socket
#: backlog grows until the kernel buffer is full and then packets are DROPPED,
#: which for a trigger channel means a missed cue, not merely a late one.
#:
#: 4000 packets of parse work is ~a few ms on the Qt main thread, so even a
#: pathological burst cannot stall the UI for a visible interval.
_MAX_PACKETS_PER_TICK = 4000


class ArtNetBridge(QObject):

    def __init__(self):
        super().__init__()
        self._socket  = None
        self._running = False
        self._dmx_state = {}

        self.track_select_active = False
        self.on_action = None

        self._learn_callback = None
        self._learn_baseline = None

        self._timer = QTimer()
        self._timer.setInterval(50)
        self._timer.timeout.connect(self._poll)

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self):
        if self._running:
            return
        try:
            port = artnet_config.port
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            # dp-247: enlarge the kernel receive buffer. Art-Net is broadcast,
            # so SAVA receives EVERY universe on the network whether or not it
            # is the one configured -- on a big rig that is far more traffic
            # than SAVA's own mapping needs. The default buffer (~64KB on
            # Windows) holds only ~120 Art-Net DMX packets; once it is full the
            # OS silently DROPS newly arriving packets, and a dropped packet is
            # a dropped CUE (a trigger that goes 0 -> 255 -> 0 inside the drop
            # window is never observed at all). A larger buffer absorbs bursts.
            # Best-effort: the OS may clamp the request, and a failure here is
            # not worth refusing to listen over.
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, _RCVBUF_BYTES)
            except OSError as e:
                print(f"[ArtNet] Could not enlarge receive buffer: {e}")
            sock.bind(("", port))
            sock.setblocking(False)
            self._socket  = sock
            self._running = True
            self._timer.start()
            print(f"[ArtNet] Listening on UDP port {port} "
                  f"(subnet={artnet_config.subnet}, universe={artnet_config.universe})")
        except Exception as e:
            print(f"[ArtNet] Failed to start: {e}")
            self._cleanup()
            self._running = False

    def stop(self):
        if not self._running and self._socket is None:
            return
        self._running = False
        try:
            self._timer.stop()
        except Exception:
            pass
        self._cleanup()
        self._dmx_state = {}
        print("[ArtNet] Stopped.")

    def _cleanup(self):
        if self._socket:
            try:
                self._socket.close()
            except Exception:
                pass
            self._socket = None

    @property
    def is_running(self) -> bool:
        return self._running

    def reload_config(self):
        artnet_config.reload()
        self._dmx_state = {}   # forget last values so changes re-fire

    # ── Learn (one-shot channel assignment) ─────────────────────────────────

    def arm_learn(self, callback):
        """Arm one-shot channel learn on the currently configured universe.
        The next DMX channel whose value changes is reported as
        callback(channel_1_indexed), then learn disarms itself."""
        self._learn_callback = callback
        self._learn_baseline = None

    def disarm_learn(self):
        self._learn_callback = None
        self._learn_baseline = None

    # ── Polling on Qt main thread ─────────────────────────────────────────────

    def _poll(self):
        if not self._running or self._socket is None:
            return
        # Drain until the socket is EMPTY (BlockingIOError), not until a fixed
        # packet budget is spent -- see _MAX_PACKETS_PER_TICK for why the old
        # 50-per-tick cap silently became a throughput ceiling on a busy rig.
        for _ in range(_MAX_PACKETS_PER_TICK):
            try:
                data, _addr = self._socket.recvfrom(1024)
            except BlockingIOError:
                return
            except OSError:
                return
            except Exception as e:
                print(f"[ArtNet] Poll error: {e}")
                return
            try:
                if _is_artnet_dmx(data):
                    universe, dmx_data = _parse_artnet_dmx(data)
                    if dmx_data is not None:
                        self._process(universe, dmx_data)
            except Exception as e:
                print(f"[ArtNet] Packet error: {e}")

    def _process(self, universe: int, dmx_data: bytes):
        if universe != artnet_config.full_universe:
            return

        if self._learn_callback:
            if self._learn_baseline is None:
                self._learn_baseline = dmx_data
            else:
                n = min(len(dmx_data), len(self._learn_baseline))
                for ch in range(n):
                    if dmx_data[ch] != self._learn_baseline[ch]:
                        cb = self._learn_callback
                        self.disarm_learn()
                        cb(ch + 1)
                        break

        for fn_name, mapping in artnet_config.all_mappings().items():
            if not mapping.get("enabled", True):
                continue
            ch  = mapping.get("channel", 1)
            thr = mapping.get("threshold", 128)
            if ch < 1 or ch > len(dmx_data):
                continue

            new_val = dmx_data[ch - 1]
            key     = (universe, ch)
            if self._dmx_state.get(key, -1) == new_val:
                continue
            self._dmx_state[key] = new_val

            if fn_name == "track_select_enable":
                self.track_select_active = new_val >= thr

            cb = self.on_action
            if cb:
                try:
                    cb(fn_name, new_val, thr)
                except Exception as e:
                    print(f"[ArtNet] callback error: {e}")


def _is_artnet_dmx(data: bytes) -> bool:
    if len(data) < 18 or not data.startswith(ARTNET_HEADER):
        return False
    try:
        return struct.unpack_from("<H", data, 8)[0] == OPCODE_DMX
    except Exception:
        return False


def _parse_artnet_dmx(data: bytes):
    try:
        universe = struct.unpack_from("<H", data, 14)[0]
        length   = struct.unpack_from(">H", data, 16)[0]
        return universe, data[18: 18 + length]
    except Exception:
        return None, None


# Lazy singleton — created in main.py after QApplication
artnet_bridge = None
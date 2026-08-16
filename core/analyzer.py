import subprocess
import threading
import numpy as np

from core.subproc import popen_kwargs
# dp-244: share deck_engine's platform-aware resolution rather than repeating
# a hardcoded "ffmpeg.exe" here -- the two must never disagree about which
# binary decodes, and only one of them should own the .exe-suffix rule.
from core.deck_engine import _FFMPEG_BIN

# Decode target for waveform analysis only (not playback). Downsampling in
# ffmpeg itself, rather than decoding at native rate and downsampling in
# numpy afterward, drastically cuts the amount of PCM ffmpeg has to emit and
# pydub/numpy has to hold in memory for large/long files.
_ANALYSIS_SAMPLE_RATE = 4000


class WaveformAnalyzer:
    """
    Decodes an audio file into a downsampled waveform array
    suitable for drawing in the UI.
    Runs in a background thread so the UI stays responsive.
    """

    def __init__(self):
        self._lock        = threading.Lock()
        self._waveform    = None   # np.ndarray, shape (N,), values 0.0–1.0
        self._sample_rate = 44100
        self._duration    = 0.0
        self._filepath    = None
        self._thread      = None
        self.on_ready     = None   # callback(waveform_array) fired when done

    # ── Public API ────────────────────────────────────────────────────────────

    def analyze(self, filepath: str, points: int = 2000):
        """
        Start a background analysis of filepath.
        When complete, self.waveform is populated and on_ready is called.
        """
        with self._lock:
            self._waveform = None
            self._filepath = filepath

        self._thread = threading.Thread(
            target=self._run,
            args=(filepath, points),
            daemon=True
        )
        self._thread.start()

    @property
    def waveform(self):
        with self._lock:
            return self._waveform

    @property
    def duration(self):
        return self._duration

    @property
    def sample_rate(self):
        return self._sample_rate

    def get_rms_at(self, position_sec: float, window_sec: float = 0.05) -> float:
        """
        Return the RMS amplitude (0.0–1.0) around a position.
        Used for VU-meter style display.
        """
        with self._lock:
            if self._waveform is None or self._duration == 0:
                return 0.0
            n    = len(self._waveform)
            frac = position_sec / self._duration
            idx  = int(frac * n)
            half = max(1, int(window_sec / self._duration * n))
            lo   = max(0, idx - half)
            hi   = min(n, idx + half)
            chunk = self._waveform[lo:hi]
            if len(chunk) == 0:
                return 0.0
            return float(np.sqrt(np.mean(chunk ** 2)))

    # ── Internal ──────────────────────────────────────────────────────────────

    def _run(self, filepath: str, points: int):
        try:
            samples = self._decode_downsampled(filepath)
            self._sample_rate = _ANALYSIS_SAMPLE_RATE
            self._duration     = len(samples) / float(_ANALYSIS_SAMPLE_RATE)

            # Downsample to `points` buckets by taking RMS of each chunk
            chunk_size = max(1, len(samples) // points)
            n_chunks   = len(samples) // chunk_size
            trimmed    = samples[:n_chunks * chunk_size]
            buckets    = trimmed.reshape(n_chunks, chunk_size)
            waveform   = np.sqrt(np.mean(buckets ** 2, axis=1))  # RMS per bucket

            # Normalise to 0..1
            peak = waveform.max()
            if peak > 0:
                waveform /= peak

            with self._lock:
                self._waveform = waveform

            if self.on_ready:
                try:
                    self.on_ready(waveform)
                except Exception as e:
                    print(f"[Analyzer] on_ready callback error: {e}")

        except Exception as e:
            print(f"[Analyzer] Analysis failed for {filepath}: {e}")
            with self._lock:
                self._waveform = np.zeros(points, dtype=np.float32)
            if self.on_ready:
                try:
                    self.on_ready(self._waveform)
                except Exception:
                    pass

    def _decode_downsampled(self, filepath: str) -> np.ndarray:
        """
        Decode filepath directly to mono PCM at _ANALYSIS_SAMPLE_RATE via a
        single ffmpeg subprocess call, bypassing pydub's AudioSegment (which
        decodes at native sample rate/width and holds the full-quality PCM
        in memory before any downsampling happens). This is waveform-display
        quality only — playback uses the native file, untouched.
        """
        try:
            proc = subprocess.run(
                [
                    _FFMPEG_BIN, "-nostdin", "-v", "error", "-i", filepath,
                    "-ac", "1", "-ar", str(_ANALYSIS_SAMPLE_RATE),
                    "-f", "s16le", "-acodec", "pcm_s16le", "pipe:1",
                ],
                # See core/subproc.py: without stdin=DEVNULL this call emits
                # nothing in a windowed frozen build and silently falls
                # through to the much slower pydub path below; without
                # CREATE_NO_WINDOW it flashes a console window per analysis.
                **popen_kwargs(
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
                ),
            )
            raw = proc.stdout
        except Exception:
            # Fall back to pydub if ffmpeg isn't available or the direct
            # subprocess call fails for some codec-specific reason.
            #
            # Imported HERE, not at module scope. pydub is documented as a
            # phantom dependency (CLAUDE.md) and is not needed by any path
            # SAVA actually takes -- but a module-level `from pydub import
            # AudioSegment` made it a HARD import: an environment without
            # pydub failed to import core.analyzer, which ui.main_window
            # imports, which took the whole app down at startup over a
            # never-executed fallback. A local import degrades that to "the
            # fallback is unavailable", which the caller already handles.
            from pydub import AudioSegment

            audio = AudioSegment.from_file(filepath)
            audio = audio.set_channels(1).set_frame_rate(_ANALYSIS_SAMPLE_RATE)
            audio = audio.set_sample_width(2)
            raw = audio.raw_data

        samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32)
        samples /= 32768.0  # normalise to -1..1
        return samples


# ── Singleton ─────────────────────────────────────────────────────────────────
analyzer = WaveformAnalyzer()
"""dp-266: the "Level playlist volumes" flow (measure -> confirm -> apply).

Measurement runs on a worker thread; progress and the result come back to the
Qt thread through signals, never by touching widgets from the worker.
"""

import threading

from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtWidgets import QMessageBox, QProgressDialog

from config.settings import settings
from core import loudness
from core.engine import engine, STATE_STOPPED
from core.playlist import playlist


class LevelPlaylistFlow(QObject):
    _progress = pyqtSignal(int, int)
    _finished = pyqtSignal(object, object)  # results, new_cache
    applied = pyqtSignal()

    def __init__(self, parent_window):
        super().__init__(parent_window)
        self._win = parent_window
        self._dialog = None
        self._cancel = threading.Event()
        self._rows = []
        self._running = False
        self._progress.connect(self._on_progress)
        self._finished.connect(self._on_finished)

    def start(self):
        if self._running or playlist.count == 0:
            return
        self._rows = [
            (meta["filepath"], meta.get("id"), meta.get("title") or meta["filepath"])
            for meta in playlist.tracks
        ]
        paths = [row[0] for row in self._rows]
        cache = settings.get(loudness.CACHE_KEY, {})
        self._cancel = threading.Event()
        self._running = True
        dlg = QProgressDialog(
            "Measuring loudness...", "Cancel", 0, len(set(paths)), self._win
        )
        dlg.setWindowTitle("Level Playlist Volumes")
        dlg.setMinimumDuration(0)
        dlg.setAutoClose(False)
        dlg.setAutoReset(False)
        cancel = self._cancel
        dlg.canceled.connect(cancel.set)
        dlg.show()
        self._dialog = dlg

        def work():
            results, new_cache = loudness.measure_many(
                paths,
                cache,
                progress=lambda done, total: self._progress.emit(done, total),
                cancel=cancel,
            )
            self._finished.emit(results, (new_cache, cancel.is_set()))

        threading.Thread(target=work, daemon=True).start()

    def _on_progress(self, done, total):
        if self._dialog is not None:
            self._dialog.setValue(min(done, total))

    def _on_finished(self, results, payload):
        new_cache, cancelled = payload
        self._running = False
        if self._dialog is not None:
            self._dialog.close()
            self._dialog.deleteLater()
            self._dialog = None
        if cancelled:
            return
        # Persist measurements once (cache survives even if the user declines).
        settings.set(loudness.CACHE_KEY, new_cache)
        settings.save()
        levels, skipped = loudness.compute_levels(
            {idx: results.get(row[0]) for idx, row in enumerate(self._rows)}
        )
        if not levels:
            QMessageBox.information(
                self._win,
                "Level Playlist Volumes",
                "No track could be measured, so nothing was changed.",
            )
            return
        if not self._confirm(levels, skipped):
            return
        for idx, level in levels.items():
            filepath, track_id, _title = self._rows[idx]
            engine.set_track_volume(level, filepath, track_id)
        settings.save()  # once, after every row is applied
        self.applied.emit()

    def _confirm(self, levels, skipped):
        outliers = [
            self._rows[idx][2]
            for idx, why in skipped.items()
            if why == loudness.SKIP_OUTLIER
        ]
        unmeasured = [
            idx for idx, why in skipped.items() if why == loudness.SKIP_UNMEASURED
        ]
        lines = [
            f"{len(levels)} track(s) will have their volume changed. "
            "This overwrites any per-track volumes you set manually.",
            "Tracks are only turned down, never up.",
        ]
        if outliers:
            lines.append(
                f"\n{len(outliers)} much quieter track(s) left untouched:\n  "
                + "\n  ".join(outliers[:10])
                + ("\n  ..." if len(outliers) > 10 else "")
            )
        if unmeasured:
            lines.append(
                f"\n{len(unmeasured)} track(s) could not be measured (left untouched)."
            )
        if engine.state != STATE_STOPPED:
            lines.append("\nA track is playing: its volume will change immediately.")
        box = QMessageBox(self._win)
        box.setWindowTitle("Level Playlist Volumes")
        box.setText("\n".join(lines))
        ok = QMessageBox.StandardButton.Ok
        cancel = QMessageBox.StandardButton.Cancel
        box.setStandardButtons(ok | cancel)
        box.setDefaultButton(cancel)
        return box.exec() == ok

"""Progress window for the one-click update."""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QProgressBar, QPushButton, QVBoxLayout, QWidget

from app.services import self_update
from app.services.errors import FriendlyError, OperationCanceled
from app.services.update_service import UpdateInfo

log = logging.getLogger("uvs.update")


class _Bridge(QObject):
    progress = Signal(object, str)
    done = Signal(object)
    failed = Signal(str)
    canceled = Signal()


class UpdateDialog(QDialog):
    """Downloads and prepares the update. ``new_dir`` is set when everything is ready to install."""

    def __init__(self, info: UpdateInfo, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Updating")
        self.setModal(True)
        self.setMinimumWidth(420)
        self.info = info
        self.new_dir: Optional[Path] = None
        self.installed = False                      # True after a .deb update was installed (Linux package install)
        self.cancel_event = threading.Event()
        lay = QVBoxLayout(self)
        self.title = QLabel(f"<b>Updating to version {info.version}</b>")
        self.status = QLabel("Starting...")
        self.status.setWordWrap(True)
        self.bar = QProgressBar()
        self.bar.setRange(0, 0)
        row = QHBoxLayout()
        row.addStretch(1)
        self.btn = QPushButton("Cancel")
        row.addWidget(self.btn)
        for w in (self.title, self.status, self.bar):
            lay.addWidget(w)
        lay.addLayout(row)
        self.btn.clicked.connect(self._cancel)
        self._b = _Bridge()
        self._b.progress.connect(self._progress)
        self._b.done.connect(self._done)
        self._b.failed.connect(self._failed)
        self._b.canceled.connect(self.reject)
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self) -> None:
        try:
            if self.info.deb_url and self_update.can_pkg_update():
                prog = lambda p, m, s: self._b.progress.emit(p, m)  # noqa: E731
                deb = self_update.prepare_deb(self.info.deb_url, self.info.deb_sha256, prog, self.cancel_event)
                self_update.install_deb(deb, prog, self.cancel_event)
                self._b.done.emit(None)
                return
            d = self_update.prepare(self.info.asset_url, self.info.sha256,
                                    lambda p, m, s: self._b.progress.emit(p, m), self.cancel_event)
            self._b.done.emit(d)
        except OperationCanceled:
            self._b.canceled.emit()
        except FriendlyError as exc:
            log.warning("update failed: %s | %s", exc.message, exc.details)
            self._b.failed.emit(exc.message + (f"\n({exc.details[:300]})" if exc.details else ""))
        except Exception as exc:  # noqa: BLE001
            log.exception("update crashed")
            self._b.failed.emit(f"Update failed: {type(exc).__name__}: {exc}")

    def _progress(self, pct, msg: str) -> None:
        self.status.setText(msg)
        if pct is None:
            self.bar.setRange(0, 0)
        else:
            self.bar.setRange(0, 100)
            self.bar.setValue(int(pct))

    def _done(self, d) -> None:
        self.new_dir = d
        self.installed = d is None
        self.accept()

    def _failed(self, msg: str) -> None:
        self.bar.setRange(0, 1)
        self.status.setText(msg + "\n\nNothing was changed. You can download the update manually from the release page.")
        self.btn.setText("Close")
        self.btn.clicked.disconnect()
        self.btn.clicked.connect(self.reject)

    def _cancel(self) -> None:
        self.cancel_event.set()
        self.btn.setEnabled(False)
        self.status.setText("Canceling...")

    def closeEvent(self, e) -> None:  # noqa: N802
        self.cancel_event.set()
        super().closeEvent(e)

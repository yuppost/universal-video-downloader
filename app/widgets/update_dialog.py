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


class UpdateAvailableDialog(QDialog):
    """"A new version is available": version header, release notes rendered from Markdown and clear actions.

    ``action`` is "update" (one-click update), "page" (open the release page) or "" (Later).
    """

    def __init__(self, info: UpdateInfo, current: str, one_click: bool, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        from PySide6.QtWidgets import QTextBrowser
        self.info = info
        self.action = ""
        self.setWindowTitle("Update available")
        self.setModal(True)
        self.setMinimumWidth(520)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(22, 20, 22, 18)
        lay.setSpacing(10)
        head = QLabel(f"<span style='font-size:18px;font-weight:600'>Version {info.version} is available</span>")
        sub = QLabel(f"You have version {current}.")
        sub.setObjectName("muted")
        lay.addWidget(head)
        lay.addWidget(sub)
        notes = (info.notes or "").strip()
        import re
        notes = re.sub(r"^#{1,3}\s*(what'?s\s+(new|changed)|changelog)\s*\n+", "", notes, flags=re.I)   # the box has its own title
        if notes:
            title = QLabel("<b>What's new</b>")
            lay.addWidget(title)
            self.notes = QTextBrowser()
            self.notes.setOpenExternalLinks(True)
            self.notes.setReadOnly(True)
            self.notes.document().setDefaultStyleSheet("a { color: #6cb6ff; }")
            self.notes.setMarkdown(notes)                     # headings, **bold**, lists and links instead of raw symbols
            html = self.notes.document().toHtml().replace("#0000ff", "#6cb6ff")   # Qt's default link blue is unreadable on dark
            self.notes.setHtml(html)
            from PySide6.QtGui import QColor, QPalette
            pal = self.notes.palette()
            pal.setColor(QPalette.Link, QColor("#6cb6ff"))
            pal.setColor(QPalette.LinkVisited, QColor("#a5b4fc"))
            self.notes.setPalette(pal)
            self.notes.document().setDocumentMargin(10)
            self.notes.document().setTextWidth(470)
            h = int(self.notes.document().size().height()) + 14
            self.notes.setFixedHeight(max(90, min(320, h)))      # fits the text, scrolls only when long
            lay.addWidget(self.notes)
        hint = QLabel("Update now downloads the new version, replaces the app files and restarts the app. "
                      "Your settings, sign-ins and history are kept." if one_click else
                      "Download the new version from the release page, close the app and replace your old folder with it.")
        hint.setWordWrap(True)
        hint.setObjectName("muted")
        lay.addWidget(hint)
        row = QHBoxLayout()
        self.btn_page = QPushButton("Release page")
        self.btn_later = QPushButton("Later")
        self.btn_main = QPushButton("Update now" if one_click else "Open download page")
        self.btn_main.setObjectName("primary")
        self.btn_main.setDefault(True)
        row.addWidget(self.btn_page)
        row.addStretch(1)
        row.addWidget(self.btn_later)
        row.addWidget(self.btn_main)
        lay.addLayout(row)
        if not one_click:
            self.btn_page.hide()                              # the main button already opens the page
        self.btn_main.clicked.connect(lambda: self._finish("update" if one_click else "page"))
        self.btn_page.clicked.connect(lambda: self._finish("page", close=False))
        self.btn_later.clicked.connect(self.reject)

    def _finish(self, action: str, close: bool = True) -> None:
        self.action = action
        if action == "page":
            from PySide6.QtCore import QUrl
            from PySide6.QtGui import QDesktopServices
            QDesktopServices.openUrl(QUrl(self.info.url))
        if close:
            self.accept()

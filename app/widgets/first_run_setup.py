"""First-launch setup screen: downloads FFmpeg and Deno (official sources, SHA-256 verified) into the app-managed folder.

Shown before the main window only when something is missing. Progress, errors and a Retry button are built in; when
everything is ready the dialog closes by itself and the main window opens. Files live in
``%APPDATA%\\UniversalVideoStudio\\tools`` (writable, not touched by app updates) and are reused on every later launch.
"""

from __future__ import annotations

import logging
import threading
from typing import Callable, Dict, List, Optional, Tuple

from PySide6.QtCore import QObject, QTimer, Qt, Signal
from PySide6.QtWidgets import (QDialog, QHBoxLayout, QLabel, QProgressBar, QPushButton, QVBoxLayout, QWidget)

from app.services import dependency_service as deps
from app.services.errors import FriendlyError, OperationCanceled
from app.utils.paths import human_size

log = logging.getLogger("uvs.setup")

LABELS = {"ffmpeg": "FFmpeg (video processing)", "deno": "Deno (YouTube helper)"}
Installer = Callable[[Callable, threading.Event], object]


def missing_tools(settings) -> List[str]:
    """Which managed tools are not installed yet. Only looks at files; nothing is started."""
    st = deps.quick_check(getattr(settings, "ffmpeg_path", ""))
    out: List[str] = []
    if not st.ffmpeg_ok:
        out.append("ffmpeg")
    if not st.js_runtime and not getattr(settings, "setup_prompt_dismissed", False):
        out.append("deno")
    return out


def needs_setup(settings) -> bool:
    return bool(missing_tools(settings))


class _Bridge(QObject):
    progress = Signal(str, object, str, object)       # key, percent, text, speed
    step = Signal(str)                                # key that is starting
    finished = Signal(str, bool, str, str)            # key, ok, message, details
    all_done = Signal()


class FirstRunSetup(QDialog):
    """``result_ok`` is True when every required tool is ready (the dialog then closes by itself)."""

    def __init__(self, settings, tools: Optional[List[str]] = None, installers: Optional[Dict[str, Installer]] = None,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.settings = settings
        self.tools = list(tools if tools is not None else missing_tools(settings))
        self.installers: Dict[str, Installer] = installers or {"ffmpeg": deps.install_ffmpeg, "deno": deps.install_deno}
        self.result_ok = False
        self.skipped = False
        self._cancel = threading.Event()
        self._running = False
        self._bridge = _Bridge()
        self._bridge.progress.connect(self._on_progress)
        self._bridge.step.connect(self._on_step)
        self._bridge.finished.connect(self._on_finished)
        self._bridge.all_done.connect(self._on_all_done)
        self.setWindowTitle("Universal Video Studio - first-time setup")
        self.setModal(True)
        self.setMinimumWidth(520)
        self.setWindowFlag(Qt.WindowContextHelpButtonHint, False)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(22, 20, 22, 18)
        lay.setSpacing(10)
        head = QLabel("<b style='font-size:16px'>Setting things up</b>")
        sub = QLabel("A few free helper tools are downloaded once from their official websites, checked, and stored in "
                     "your user folder. After that the app opens by itself and never asks again.")
        sub.setWordWrap(True)
        sub.setObjectName("muted")
        lay.addWidget(head)
        lay.addWidget(sub)
        self.rows: Dict[str, QLabel] = {}
        for key in self.tools:
            lab = QLabel(f"○  {LABELS.get(key, key)}")
            self.rows[key] = lab
            lay.addWidget(lab)
        self.bar = QProgressBar()
        self.bar.setRange(0, 0)
        self.status = QLabel("Starting...")
        self.status.setWordWrap(True)
        self.error = QLabel("")
        self.error.setWordWrap(True)
        self.error.setStyleSheet("color:#ff8a8a;")
        self.error.hide()
        lay.addWidget(self.bar)
        lay.addWidget(self.status)
        lay.addWidget(self.error)
        row = QHBoxLayout()
        row.addStretch(1)
        self.btn_retry = QPushButton("Retry")
        self.btn_retry.setObjectName("primary")
        self.btn_retry.hide()
        self.btn_skip = QPushButton("Skip for now")
        row.addWidget(self.btn_skip)
        row.addWidget(self.btn_retry)
        lay.addLayout(row)
        self.btn_retry.clicked.connect(self.start)
        self.btn_skip.clicked.connect(self._skip)

    # --- run -----------------------------------------------------------------------------
    def showEvent(self, e) -> None:  # noqa: N802
        super().showEvent(e)
        if not self._running and not self.result_ok and not getattr(self, "_started", False):
            self._started = True
            QTimer.singleShot(0, self.start)

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._cancel = threading.Event()
        self.error.hide()
        self.btn_retry.hide()
        self.bar.setRange(0, 0)
        pending = [k for k in self.tools if not self._ready(k)]
        self._thread = threading.Thread(target=self._work, args=(pending, self._cancel), daemon=True)
        self._thread.start()

    def _ready(self, key: str) -> bool:
        return key not in missing_tools_of(self.settings, self.tools)

    def _work(self, pending: List[str], cancel: threading.Event) -> None:
        for key in pending:
            self._bridge.step.emit(key)
            if cancel.is_set():
                return                                   # window closed / skipped: never touch the (gone) UI
            try:
                self.installers[key](lambda p, t, s, k=key: (None if cancel.is_set() else self._bridge.progress.emit(k, p, t, s)),
                                     cancel)
                if cancel.is_set():
                    return
                self._bridge.finished.emit(key, True, "", "")
            except OperationCanceled:
                self._bridge.finished.emit(key, False, "Canceled.", "")
                return
            except FriendlyError as exc:
                if not cancel.is_set():
                    self._bridge.finished.emit(key, False, exc.message, exc.details or "")
                return
            except Exception as exc:  # noqa: BLE001
                log.exception("setup of %s crashed", key)
                if not cancel.is_set():
                    self._bridge.finished.emit(key, False, f"Unexpected error: {exc}", "")
                return
        if not cancel.is_set():
            self._bridge.all_done.emit()

    # --- UI updates ------------------------------------------------------------------------
    def _on_step(self, key: str) -> None:
        self.rows[key].setText(f"▶  {LABELS.get(key, key)}")
        self.status.setText(f"Downloading {LABELS.get(key, key)}...")

    def _on_progress(self, key: str, pct, text: str, speed) -> None:
        if pct is None:
            self.bar.setRange(0, 0)
        else:
            self.bar.setRange(0, 100)
            self.bar.setValue(int(pct))
        self.status.setText(text + (f"   ({human_size(speed)}/s)" if speed else ""))

    def _on_finished(self, key: str, ok: bool, msg: str, details: str) -> None:
        if ok:
            self.rows[key].setText(f"✔  {LABELS.get(key, key)}")
            return
        self._running = False
        self.rows[key].setText(f"✖  {LABELS.get(key, key)}")
        self.bar.setRange(0, 1)
        self.bar.setValue(0)
        self.status.setText("Setup could not finish.")
        self.error.setText(msg + ("\n" + details[:300] if details else "")
                           + "\n\nCheck your internet connection (or antivirus/firewall) and press Retry.")
        self.error.show()
        self.btn_retry.show()

    def _on_all_done(self) -> None:
        self._running = False
        self.result_ok = True
        self.bar.setRange(0, 100)
        self.bar.setValue(100)
        self.status.setText("All set - opening the app...")
        QTimer.singleShot(700, self.accept)

    def _skip(self) -> None:
        self._cancel.set()
        self.skipped = True
        if "deno" in self.tools and not self._ready("deno"):
            self.settings.setup_prompt_dismissed = True       # YouTube helper is optional: do not nag about it again
        self.reject()

    def closeEvent(self, e) -> None:  # noqa: N802
        self._cancel.set()
        super().closeEvent(e)


def missing_tools_of(settings, tools: List[str]) -> List[str]:
    """Subset of *tools* that is still missing (ignores the 'dismissed' flag)."""
    st = deps.quick_check(getattr(settings, "ffmpeg_path", ""))
    out = []
    if "ffmpeg" in tools and not st.ffmpeg_ok:
        out.append("ffmpeg")
    if "deno" in tools and not st.js_runtime:
        out.append("deno")
    return out

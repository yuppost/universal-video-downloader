"""Updates dialog: the app itself, FFmpeg, yt-dlp and the YouTube helper - each shows whether it is up to date and
updates with one click (missing tools are installed the same way)."""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Optional

from PySide6.QtCore import QObject, QTimer, Qt, Signal
from PySide6.QtWidgets import (QDialog, QFrame, QGridLayout, QHBoxLayout, QLabel, QMessageBox, QProgressBar,
                               QPushButton, QVBoxLayout, QWidget)

from app import APP_NAME, UPDATE_INFO_URL, __version__
from app.services import dependency_service as deps
from app.services.errors import FriendlyError, OperationCanceled
from app.utils.paths import human_size

log = logging.getLogger("uvs.depdialog")


class _Bridge(QObject):
    progress = Signal(str, object, str, object)     # key, percent|None, text, speed|None
    done = Signal(str, bool, str)                   # key, ok, message


class _CheckBridge(QObject):
    """Lives as long as the check thread (not owned by the dialog, so a closed dialog never receives a late signal)."""
    one = Signal(str, object, object)               # key, (state, newest) for a tool / (status, UpdateInfo|None) for the app


GOOD, WARN, MUTED = "#22c55e", "#f59e0b", "#94a3b8"


class DependencyDialog(QDialog):
    """Rows: FFmpeg (+FFprobe), yt-dlp, GPU encoder (information only)."""

    changed = Signal()
    updateRequested = Signal(object)              # the app has a newer version: an UpdateInfo

    def __init__(self, ffmpeg_setting: str = "", parent: Optional[QWidget] = None, hw_mode: str = "auto",
                 check_on_open: bool = True) -> None:
        super().__init__(parent)
        self.setWindowTitle("Updates")
        self._app_info = None
        self._app_state = ""
        self.setMinimumWidth(520)
        self._ffmpeg_setting = ffmpeg_setting
        self._hw_mode = hw_mode if hw_mode in ("auto", "nvenc", "cpu") else "auto"
        self._busy: Optional[str] = None
        self._notes = {}                                  # key -> result line of the last Update click
        self._cancel = threading.Event()
        self._bridge = _Bridge()
        self._bridge.progress.connect(self._on_progress)
        self._bridge.done.connect(self._on_done)

        lay = QVBoxLayout(self)
        intro = QLabel("Keep the app and its tools up to date. Each item shows whether a newer version exists. Missing "
                       "tools can be installed here too - no command prompt required. Tools are stored inside the app's "
                       "own data folder.")
        intro.setWordWrap(True)
        intro.setObjectName("muted")
        lay.addWidget(intro)

        self.rows = {}
        self.badges = {}
        for key, title in (("app", APP_NAME), ("ffmpeg", "FFmpeg + FFprobe"), ("ytdlp", "yt-dlp"), ("deno", "YouTube helper (JavaScript runtime)"),
                           ("gpu", "Video encoder (GPU / CPU)")):
            card = QFrame()
            card.setObjectName("card")
            g = QGridLayout(card)
            g.setContentsMargins(12, 10, 12, 10)
            name = QLabel(f"<b>{title}</b>")
            status = QLabel("")
            status.setWordWrap(True)
            status.setObjectName("muted")
            btn = QPushButton("")
            bar = QProgressBar()
            bar.setRange(0, 100)
            bar.setVisible(False)
            badge = QLabel("")
            badge.setWordWrap(True)
            self.badges[key] = badge
            g.addWidget(name, 0, 0)
            g.addWidget(status, 1, 0)
            g.addWidget(badge, 2, 0)
            g.addWidget(bar, 3, 0, 1, 2)
            g.addWidget(btn, 0, 1, 3, 1, Qt.AlignRight | Qt.AlignVCenter)
            g.setColumnStretch(0, 1)
            lay.addWidget(card)
            self.rows[key] = (status, btn, bar)
        self.rows["gpu"][1].setVisible(False)
        self.rows["app"][0].setText(f"Version {__version__}")
        self.rows["app"][1].setText("Update")
        self.rows["app"][1].clicked.connect(self._app_clicked)
        self.badges["gpu"].setVisible(False)
        self.rows["ffmpeg"][1].clicked.connect(lambda: self._start("ffmpeg"))
        self.rows["ytdlp"][1].clicked.connect(lambda: self._start("ytdlp"))
        self.rows["deno"][1].clicked.connect(lambda: self._start("deno"))

        row = QHBoxLayout()
        self.btn_cancel = QPushButton("Cancel download")
        self.btn_cancel.setVisible(False)
        self.btn_cancel.clicked.connect(self._cancel.set)
        self.btn_refresh = QPushButton("Re-check")
        self.btn_refresh.clicked.connect(self._recheck)
        self.btn_close = QPushButton("Close")
        self.btn_close.setObjectName("primary")
        self.btn_close.clicked.connect(self.accept)
        row.addWidget(self.btn_cancel)
        row.addStretch(1)
        row.addWidget(self.btn_refresh)
        row.addWidget(self.btn_close)
        lay.addLayout(row)
        self.refresh()
        if check_on_open:
            self._check_all()

    # --- status ----------------------------------------------------------
    def refresh(self) -> None:
        st = deps.check(self._ffmpeg_setting)
        s, b, _bar = self.rows["ffmpeg"]
        if st.ffmpeg_ok:
            s.setText(f"Installed - {st.ffmpeg_version.split(' Copyright')[0]}\n{st.ffmpeg_path}")
            b.setText("Update")
            b.setToolTip("Checks for a newer version and downloads it only if there is one")
        else:
            missing = " and ".join(n for n, p in (("FFmpeg", st.ffmpeg_path), ("FFprobe", st.ffprobe_path)) if not p)
            s.setText(f"MISSING: {missing}. Required for trimming, cropping and merging.")
            b.setText("Download FFmpeg (~90 MB)")
        s, b, _bar = self.rows["ytdlp"]
        if st.ytdlp_ok:
            s.setText(f"Installed - version {st.ytdlp_version}")
            b.setText("Update")
        else:
            s.setText("MISSING: yt-dlp is needed to read and download videos.")
            b.setText("Install yt-dlp")
        s, b, _bar = self.rows["deno"]
        if st.youtube_ok:
            s.setText(f"Ready - {st.js_runtime}. YouTube downloads can solve their challenges.")
            b.setText("Update")
            b.setToolTip("Checks for a newer version and downloads it only if there is one")
        elif not st.ejs_ok:
            s.setText("MISSING: the yt-dlp YouTube component (yt-dlp-ejs). Click 'Install yt-dlp' above to add it.")
            b.setText("Download Deno (~40 MB)")
        else:
            s.setText("MISSING: YouTube now needs a JavaScript runtime. Without it many YouTube videos fail with "
                      "errors. Free, one click, stored inside the app folder.")
            b.setText("Download Deno (~40 MB)")
        s, _b, _bar = self.rows["gpu"]
        s.setText(self._encoder_text(st))
        for key in ("app", "ffmpeg", "ytdlp", "deno"):
            self.rows[key][1].setEnabled(self._busy is None)
        self.btn_refresh.setEnabled(self._busy is None)
        self.btn_close.setEnabled(self._busy is None)

    # --- "is there a newer version?" --------------------------------------
    def _set_badge(self, key: str, state: str, newest: str = "") -> None:
        text, color = {
            "checking": ("Checking for updates...", MUTED),
            "uptodate": ("\u2714 Up to date", GOOD),
            "available": (f"\u2B06 Update available{f': {newest}' if newest else ''}", WARN),
            "unknown": ("Could not check for updates (no internet?)", MUTED),
            "missing": ("", MUTED),
            "none": ("", MUTED),
        }.get(state, ("", MUTED))
        b = self.badges[key]
        b.setText(f'<span style="color:{color}"><b>{text}</b></span>' if text else "")
        btn = self.rows[key][1]
        btn.setObjectName("primary" if state == "available" else "")
        btn.style().unpolish(btn)
        btn.style().polish(btn)

    def _recheck(self) -> None:
        self.refresh()
        self._check_all()

    CHECK_TIMEOUT_MS = 25000

    def _check_all(self) -> None:
        """Compare versions in the background: every item is checked on its own thread and shows its result as soon as
        it is known, so one slow site can never leave the rest on "Checking..." (a timer ends any that take too long).
        Only a few bytes are read from the internet; nothing is downloaded."""
        keys = ("app", "ffmpeg", "ytdlp", "deno")
        for k in keys:
            self._set_badge(k, "checking")
        self._pending = set(keys)
        self._check_gen = getattr(self, "_check_gen", 0) + 1
        gen = self._check_gen
        bridge = _CheckBridge()
        bridge.one.connect(lambda key, a, b, g=gen: self._on_one(g, key, a, b))
        self._check_bridge = bridge                      # keeps the connection alive while the threads run
        setting = self._ffmpeg_setting

        def app_check():
            from app.services.update_service import check_update
            return check_update(UPDATE_INFO_URL, __version__)

        jobs = {"app": app_check, "ffmpeg": lambda: deps.check_ffmpeg_update(setting), "ytdlp": deps.check_ytdlp_update,
                "deno": deps.check_deno_update}

        def run(key: str, fn) -> None:
            t0 = time.time()
            try:
                res = fn()
            except Exception:  # noqa: BLE001 - a failed check must never break the dialog
                log.exception("Update check failed: %s", key)
                res = ("error", None) if key == "app" else ("unknown", "")
            log.info("Update check %s: %s (%.1fs)", key, res[0], time.time() - t0)
            bridge.one.emit(key, res[0], res[1])

        for key, fn in jobs.items():
            threading.Thread(target=run, args=(key, fn), daemon=True).start()
        QTimer.singleShot(self.CHECK_TIMEOUT_MS, lambda: self._check_timeout(gen))

    def _on_one(self, gen: int, key: str, state, extra) -> None:
        if gen != self._check_gen or key not in self._pending:
            return                                       # a newer check started, or this one already timed out
        self._pending.discard(key)
        if key == "app":
            self._app_state, self._app_info = state, (extra if state == "available" else None)
            self._set_badge("app", {"available": "available", "latest": "uptodate", "unconfigured": "none"}.get(state, "unknown"),
                            getattr(extra, "version", ""))
        else:
            self._set_badge(key, state, extra)

    def _on_checked(self, tools: dict, app_status: str, info) -> None:
        """Apply a complete set of results at once (also used by tests)."""
        self._check_gen = getattr(self, "_check_gen", 0)
        self._pending = {"app", "ffmpeg", "ytdlp", "deno"}
        for key in ("ffmpeg", "ytdlp", "deno"):
            state, newest = tools.get(key, ("unknown", ""))
            self._on_one(self._check_gen, key, state, newest)
        self._on_one(self._check_gen, "app", app_status, info)

    def _check_timeout(self, gen: int) -> None:
        if gen != getattr(self, "_check_gen", -1):
            return
        for key in list(getattr(self, "_pending", ())):
            self._pending.discard(key)
            self._set_badge(key, "unknown")
            log.warning("Update check %s timed out", key)

    def _app_clicked(self) -> None:
        if self._app_info is not None:
            info = self._app_info
            self.accept()                                # close this box first, then the update window takes over
            self.updateRequested.emit(info)
            return
        text = {"latest": f"You have the latest version of {APP_NAME} ({__version__}).",
                "unconfigured": "No update source is configured in this build."}.get(
            self._app_state, "Could not check for updates yet. Check your internet connection, press Re-check and try again."
            if self._app_state else "Still checking - try again in a moment.")
        QMessageBox.information(self, APP_NAME, text)

    def _encoder_text(self, st) -> str:
        """What rendering will use, following Settings > Hardware encoding (the GPU is not probed in CPU mode)."""
        if self._hw_mode == "cpu":
            return ("CPU rendering is selected in Settings. Clips are encoded with libx264 / libx265 on the processor; "
                    "the GPU is not used.")
        if not st.ffmpeg_ok:
            return "Checked after FFmpeg is installed."
        from app.services.ffmpeg_service import probe_gpu
        info = probe_gpu(st.ffmpeg_path)
        chosen = "GPU (NVENC)" if self._hw_mode == "nvenc" else "Auto"
        if info.available:
            return f"{info.describe()}.\nSetting: {chosen} - rendering will use your GPU."
        extra = " You selected GPU, but it cannot be used." if self._hw_mode == "nvenc" else ""
        return ("No usable NVIDIA GPU encoder was found with this FFmpeg / driver." + extra +
                "\nRendering will use the CPU (libx264 / libx265).")

    # --- install ---------------------------------------------------------
    def _start(self, key: str) -> None:
        if self._busy:
            return
        self._busy = key
        self._cancel = threading.Event()
        _s, btn, bar = self.rows[key]
        bar.setVisible(True)
        bar.setRange(0, 0)
        for k in ("app", "ffmpeg", "ytdlp", "deno"):
            self.rows[k][1].setEnabled(False)
        self.btn_refresh.setEnabled(False)
        self.btn_close.setEnabled(False)
        self.btn_cancel.setVisible(key in ("ffmpeg", "deno"))
        cancel = self._cancel

        def prog(pct, text, speed) -> None:
            self._bridge.progress.emit(key, pct, text, speed)

        def work() -> None:
            try:
                if key == "ffmpeg":
                    msg = deps.update_ffmpeg(prog, cancel, self._ffmpeg_setting)
                elif key == "deno":
                    msg = deps.update_deno(prog, cancel)
                else:
                    msg = deps.install_ytdlp(prog)
                self._bridge.done.emit(key, True, msg)
            except OperationCanceled:
                self._bridge.done.emit(key, False, "Canceled")
            except FriendlyError as exc:
                self._bridge.done.emit(key, False, exc.message)
            except Exception as exc:  # noqa: BLE001
                log.exception("Dependency install crashed")
                self._bridge.done.emit(key, False, f"Unexpected error: {exc}")

        threading.Thread(target=work, daemon=True).start()

    def _on_progress(self, key: str, pct, text: str, speed) -> None:
        status, _b, bar = self.rows[key]
        if pct is None:
            bar.setRange(0, 0)
        else:
            bar.setRange(0, 100)
            bar.setValue(int(pct))
        status.setText(text + (f"   ({human_size(speed)}/s)" if speed else ""))

    def _on_done(self, key: str, ok: bool, msg: str) -> None:
        self._busy = None
        self.rows[key][2].setVisible(False)
        self.btn_cancel.setVisible(False)
        if ok:
            self._notes[key] = msg
        self.refresh()
        if ok:
            self._set_badge(key, "uptodate")
            self.changed.emit()
            title = {"ffmpeg": "FFmpeg", "ytdlp": "yt-dlp", "deno": "YouTube helper"}.get(key, "Update")
            QMessageBox.information(self, title, msg or "Done.")
        elif msg != "Canceled":
            QMessageBox.warning(self, "Installation failed",
                                f"{msg}\n\nYou can try again, or install it manually (see README).")

    def reject(self) -> None:  # Esc / window close
        if self._busy:
            return
        super().reject()

    def closeEvent(self, e) -> None:  # noqa: N802
        if self._busy:
            e.ignore()
        else:
            super().closeEvent(e)

"""Setup dialog: shows what is missing and installs FFmpeg / yt-dlp with one click."""

from __future__ import annotations

import logging
import threading
from typing import Callable, Optional

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import (QDialog, QFrame, QGridLayout, QHBoxLayout, QLabel, QMessageBox, QProgressBar,
                               QPushButton, QVBoxLayout, QWidget)

from app.services import dependency_service as deps
from app.services.errors import FriendlyError, OperationCanceled
from app.utils.paths import human_size

log = logging.getLogger("uvs.depdialog")


class _Bridge(QObject):
    progress = Signal(str, object, str, object)     # key, percent|None, text, speed|None
    done = Signal(str, bool, str)                   # key, ok, message


class DependencyDialog(QDialog):
    """Rows: FFmpeg (+FFprobe), yt-dlp, GPU encoder (information only)."""

    changed = Signal()

    def __init__(self, ffmpeg_setting: str = "", parent: Optional[QWidget] = None, hw_mode: str = "auto") -> None:
        super().__init__(parent)
        self.setWindowTitle("Setup - required tools")
        self.setMinimumWidth(520)
        self._ffmpeg_setting = ffmpeg_setting
        self._hw_mode = hw_mode if hw_mode in ("auto", "nvenc", "cpu") else "auto"
        self._busy: Optional[str] = None
        self._cancel = threading.Event()
        self._bridge = _Bridge()
        self._bridge.progress.connect(self._on_progress)
        self._bridge.done.connect(self._on_done)

        lay = QVBoxLayout(self)
        intro = QLabel("These tools are needed to download, trim and crop videos. Missing items can be installed here "
                       "- no command prompt required. They are stored inside the app's own data folder.")
        intro.setWordWrap(True)
        intro.setObjectName("muted")
        lay.addWidget(intro)

        self.rows = {}
        for key, title in (("ffmpeg", "FFmpeg + FFprobe"), ("ytdlp", "yt-dlp"), ("deno", "YouTube helper (JavaScript runtime)"),
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
            g.addWidget(name, 0, 0)
            g.addWidget(status, 1, 0)
            g.addWidget(bar, 2, 0, 1, 2)
            g.addWidget(btn, 0, 1, 2, 1, Qt.AlignRight | Qt.AlignVCenter)
            g.setColumnStretch(0, 1)
            lay.addWidget(card)
            self.rows[key] = (status, btn, bar)
        self.rows["gpu"][1].setVisible(False)
        self.rows["ffmpeg"][1].clicked.connect(lambda: self._start("ffmpeg"))
        self.rows["ytdlp"][1].clicked.connect(lambda: self._start("ytdlp"))
        self.rows["deno"][1].clicked.connect(lambda: self._start("deno"))

        row = QHBoxLayout()
        self.btn_cancel = QPushButton("Cancel download")
        self.btn_cancel.setVisible(False)
        self.btn_cancel.clicked.connect(self._cancel.set)
        self.btn_refresh = QPushButton("Re-check")
        self.btn_refresh.clicked.connect(self.refresh)
        self.btn_close = QPushButton("Close")
        self.btn_close.setObjectName("primary")
        self.btn_close.clicked.connect(self.accept)
        row.addWidget(self.btn_cancel)
        row.addStretch(1)
        row.addWidget(self.btn_refresh)
        row.addWidget(self.btn_close)
        lay.addLayout(row)
        self.refresh()

    # --- status ----------------------------------------------------------
    def refresh(self) -> None:
        st = deps.check(self._ffmpeg_setting)
        s, b, _bar = self.rows["ffmpeg"]
        if st.ffmpeg_ok:
            s.setText(f"Installed - {st.ffmpeg_version.split(' Copyright')[0]}\n{st.ffmpeg_path}")
            b.setText("Reinstall")
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
            b.setText("Reinstall Deno")
        elif not st.ejs_ok:
            s.setText("MISSING: the yt-dlp YouTube component (yt-dlp-ejs). Click 'Install yt-dlp' above to add it.")
            b.setText("Download Deno (~40 MB)")
        else:
            s.setText("MISSING: YouTube now needs a JavaScript runtime. Without it many YouTube videos fail with "
                      "errors. Free, one click, stored inside the app folder.")
            b.setText("Download Deno (~40 MB)")
        s, _b, _bar = self.rows["gpu"]
        s.setText(self._encoder_text(st))
        for key in ("ffmpeg", "ytdlp", "deno"):
            self.rows[key][1].setEnabled(self._busy is None)
        self.btn_refresh.setEnabled(self._busy is None)
        self.btn_close.setEnabled(self._busy is None)

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
        for k in ("ffmpeg", "ytdlp", "deno"):
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
                    deps.install_ffmpeg(prog, cancel)
                elif key == "deno":
                    deps.install_deno(prog, cancel)
                else:
                    deps.install_ytdlp(prog)
                self._bridge.done.emit(key, True, "")
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
        self.refresh()
        if ok:
            self.changed.emit()
            if key == "ytdlp":
                QMessageBox.information(self, "yt-dlp", "yt-dlp is ready. If you updated an existing version, "
                                                         "restart the app to use the new one.")
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

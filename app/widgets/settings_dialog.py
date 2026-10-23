"""Settings dialog (all values persisted by :mod:`app.config`)."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFileDialog,
                               QFormLayout, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton, QScrollArea,
                               QSpinBox, QVBoxLayout, QWidget)

from app.config import BROWSERS, QUALITIES, Settings
from app.models.crop import ASPECT_PRESETS
from app.services import ytdlp_service as yd
from app.services.ffmpeg_service import ffmpeg_version, find_executable
from app.utils.paths import dir_size, human_size


class _UpdateBridge(QObject):
    done = Signal(bool, str)


class SettingsDialog(QDialog):
    clearCacheRequested = Signal()
    cookiesRequested = Signal()

    def __init__(self, settings: Settings, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setMinimumWidth(360)
        self.resize(680, 720)
        self.s = settings
        self._bridge = _UpdateBridge()
        self._bridge.done.connect(self._update_done)

        self.download_dir = self._path_row(settings.download_dir, folder=True)
        self.temp_dir = self._path_row(settings.temp_dir, folder=True)
        self.ffmpeg = self._path_row(settings.ffmpeg_path, folder=False)
        self.ffmpeg[0].setPlaceholderText("Leave empty to auto-detect (PATH or ./ffmpeg/bin)")
        self.fmt = QComboBox()
        self.fmt.addItem("MP4 video", "mp4")
        self.fmt.addItem("MP3 audio only", "mp3")
        self.quality = QComboBox()
        for q in QUALITIES:
            self.quality.addItem("Best available" if q == "best" else f"{q}p", q)
        self.crop = QComboBox()
        for k, (label, _r) in ASPECT_PRESETS.items():
            if k not in ("custom", "free"):
                self.crop.addItem(label, k)
        self.trim = QComboBox()
        self.trim.addItem("Fast - stream copy, keyframe-limited (not frame-accurate)", "fast")
        self.trim.addItem("Accurate - re-encode, precise cuts (slower)", "accurate")
        self.codec = QComboBox()
        self.codec.addItem("H.264 + AAC (most compatible)", "h264")
        self.codec.addItem("H.265/HEVC + AAC (smaller, less compatible)", "h265")
        self.hw = QComboBox()
        self.hw.addItem("Auto - detect the NVIDIA GPU, use it when possible", "auto")
        self.hw.addItem("GPU (NVIDIA NVENC) - CPU only if the GPU cannot be used", "nvenc")
        self.hw.addItem("CPU only (libx264/libx265) - the GPU is never used", "cpu")
        self.hw.setToolTip("Only used when a clip is re-encoded (Accurate trim, crop, H.265). Fast stream-copy trimming "
                           "needs no encoder at all. The choice is remembered; check what it means for this PC in Setup.")
        self.concurrent = QSpinBox()
        self.concurrent.setRange(1, 8)
        self.template = QLineEdit(settings.filename_template)
        self.template.setToolTip("Tokens: {title} {index} {index:02d} {start} {end} {uploader} {id} {site}")
        self.theme = QComboBox()
        self.theme.addItem("Dark", "dark")
        self.theme.addItem("Light", "light")
        self.cleanup = QComboBox()
        self.cleanup.addItem("Keep everything (recommended)", "keep_all")
        self.cleanup.addItem("Delete leftover partial files on exit", "clean_partials")
        self.keep_inter = QCheckBox("Keep intermediate section files (for debugging)")
        self.cache_limit = QDoubleSpinBox()
        self.cache_limit.setRange(1, 5000)
        self.cache_limit.setSuffix(" GB")
        self.sections = QCheckBox("Prefer downloading only the selected sections when it makes sense")
        self.sections.setToolTip("Uses yt-dlp section download. Whether it saves data depends on the website.")
        self.browser = QComboBox()
        for b in BROWSERS:
            self.browser.addItem(b.capitalize() if b else "None (use the saved sign-ins only)", b)
        self.browser.setToolTip("Advanced: for sites you have NOT signed in to in the app, let yt-dlp read the cookies "
                                "from this browser while downloading. Saved sign-ins always win.")
        self.check_start = QCheckBox("Check the yt-dlp version when the app starts")
        self.check_app = QCheckBox("Tell me when a new version of this app is available")
        self.icon_row = self._path_row(settings.icon_path, folder=False, title="Choose an icon image",
                                       filt="Images (*.png *.ico *.jpg *.jpeg *.bmp *.svg)")
        self.icon_row[0].setPlaceholderText("Leave empty for the built-in icon")
        self.btn_setup = QPushButton("Setup: install / check FFmpeg and yt-dlp...")
        self.btn_setup.clicked.connect(self._open_setup)

        form = QFormLayout()
        form.addRow("Video folder (downloads)", self.download_dir[1])
        form.addRow("Default format", self.fmt)
        form.addRow("Preferred quality", self.quality)
        form.addRow("Default crop", self.crop)
        form.addRow("Trim mode", self.trim)
        form.addRow("Re-encode codec", self.codec)
        form.addRow("Hardware encoding", self.hw)
        form.addRow("Filename template", self.template)
        form.addRow("Max simultaneous tasks", self.concurrent)
        form.addRow("Theme", self.theme)
        form.addRow("App icon", self.icon_row[1])
        form.addRow("", self.btn_setup)
        form.addRow("FFmpeg executable", self.ffmpeg[1])
        self.ff_status = QLabel("")
        self.ff_status.setObjectName("muted")
        form.addRow("", self.ff_status)
        form.addRow("Cache video folder (preview/temp)", self.temp_dir[1])
        form.addRow("Cleanup", self.cleanup)
        form.addRow("", self.keep_inter)
        form.addRow("Cache size warning", self.cache_limit)
        self.cache_label = QLabel("")
        self.cache_label.setObjectName("muted")
        btn_clear = QPushButton("Clear downloaded source cache...")
        btn_clear.clicked.connect(self._clear_cache)
        row = QHBoxLayout()
        row.addWidget(self.cache_label, 1)
        row.addWidget(btn_clear)
        form.addRow("", row)
        form.addRow("", self.sections)
        btn_cookies = QPushButton("Cookies && sign-ins (private / age-restricted videos)...")
        btn_cookies.clicked.connect(self.cookiesRequested)
        form.addRow("Sign-in", btn_cookies)
        form.addRow("Fallback browser", self.browser)
        self.yt_label = QLabel(f"yt-dlp version: {yd.ytdlp_version()}")
        self.btn_update = QPushButton("Update yt-dlp")
        self.btn_update.clicked.connect(self._update)
        yrow = QHBoxLayout()
        yrow.addWidget(self.yt_label, 1)
        yrow.addWidget(self.btn_update)
        form.addRow("yt-dlp", yrow)
        form.addRow("", self.check_start)
        form.addRow("", self.check_app)

        box = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        box.accepted.connect(self.accept)
        box.rejected.connect(self.reject)
        host = QWidget()
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        form.setRowWrapPolicy(QFormLayout.WrapLongRows)     # labels move above fields on narrow screens
        host.setLayout(form)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        scroll.setWidget(host)
        lay = QVBoxLayout(self)
        lay.addWidget(scroll, 1)
        lay.addWidget(box)
        scr = self.screen().availableGeometry() if self.screen() else None
        if scr:
            self.resize(min(self.width(), scr.width() - 40), min(self.height(), scr.height() - 80))
        self._load()
        self.ffmpeg[0].textChanged.connect(self._ff_check)
        self._ff_check()

    def _open_setup(self) -> None:
        from app.widgets.dependency_dialog import DependencyDialog
        DependencyDialog(self.ffmpeg[0].text().strip(), self, self.hw.currentData()).exec()
        self._ff_check()
        self.yt_label.setText(f"yt-dlp version: {yd.ytdlp_version()}")

    def _path_row(self, value: str, folder: bool, title: str = "Locate ffmpeg executable", filt: str = ""):
        edit = QLineEdit(value)
        btn = QPushButton("Browse...")
        clear = QPushButton("Reset") if title.startswith("Choose an icon") else None
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(0, 0, 0, 0)
        h.addWidget(edit, 1)
        h.addWidget(btn)
        if clear:
            h.addWidget(clear)
            clear.clicked.connect(lambda: edit.setText(""))

        def pick() -> None:
            if folder:
                p = QFileDialog.getExistingDirectory(self, "Choose folder", edit.text() or str(Path.home()))
            else:
                p, _ = QFileDialog.getOpenFileName(self, title, edit.text() or str(Path.home()), filt)
            if p:
                edit.setText(p)

        btn.clicked.connect(pick)
        return edit, w

    def _load(self) -> None:
        s = self.s
        for combo, val in ((self.fmt, s.output_format), (self.quality, s.preferred_quality), (self.crop, s.default_crop),
                           (self.trim, s.trim_mode), (self.codec, s.video_codec), (self.hw, s.hw_encoder), (self.theme, s.theme),
                           (self.cleanup, s.cleanup_mode), (self.browser, s.cookies_browser)):
            combo.setCurrentIndex(max(0, combo.findData(val)))
        self.concurrent.setValue(s.max_concurrent)
        self.keep_inter.setChecked(s.keep_intermediates)
        self.cache_limit.setValue(s.cache_limit_gb)
        self.sections.setChecked(s.prefer_sections)
        self.check_start.setChecked(s.ytdlp_check_on_start)
        self.check_app.setChecked(s.check_app_updates)
        self.cache_label.setText(f"Cache in use: {human_size(dir_size(s.cache_dir))}")

    def _ff_check(self) -> None:
        p = find_executable("ffmpeg", self.ffmpeg[0].text().strip())
        self.ff_status.setText(f"Found: {ffmpeg_version(p)}\n{p}" if p else
                               "FFmpeg not found - install it or browse to ffmpeg.exe (see README).")

    def _clear_cache(self) -> None:
        size = human_size(dir_size(self.s.cache_dir))
        if QMessageBox.question(self, "Clear cache", f"Delete downloaded source files in the cache folder ({size})?\n"
                                "Exported clips are never touched.") == QMessageBox.Yes:
            self.clearCacheRequested.emit()
            self.cache_label.setText(f"Cache in use: {human_size(dir_size(self.s.cache_dir))}")

    def _update(self) -> None:
        self.btn_update.setEnabled(False)
        self.btn_update.setText("Updating...")
        threading.Thread(target=lambda: self._bridge.done.emit(*yd.update_ytdlp()), daemon=True).start()

    def _update_done(self, ok: bool, msg: str) -> None:
        self.btn_update.setEnabled(True)
        self.btn_update.setText("Update yt-dlp")
        (QMessageBox.information if ok else QMessageBox.warning)(self, "yt-dlp update", msg)

    def result_settings(self) -> Settings:
        s = self.s
        s.download_dir = self.download_dir[0].text().strip() or s.download_dir
        s.temp_dir = self.temp_dir[0].text().strip() or s.temp_dir
        s.ffmpeg_path = self.ffmpeg[0].text().strip()
        s.output_format = self.fmt.currentData()
        s.preferred_quality = self.quality.currentData()
        s.default_crop = self.crop.currentData()
        s.trim_mode = self.trim.currentData()
        s.video_codec = self.codec.currentData()
        s.hw_encoder = self.hw.currentData()
        s.max_concurrent = self.concurrent.value()
        s.filename_template = self.template.text().strip()
        s.theme = self.theme.currentData()
        s.cleanup_mode = self.cleanup.currentData()
        s.keep_intermediates = self.keep_inter.isChecked()
        s.cache_limit_gb = self.cache_limit.value()
        s.prefer_sections = self.sections.isChecked()
        s.cookies_browser = self.browser.currentData()
        s.ytdlp_check_on_start = self.check_start.isChecked()
        s.check_app_updates = self.check_app.isChecked()
        s.icon_path = self.icon_row[0].text().strip()
        s.sanitize()
        return s

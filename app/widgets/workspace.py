"""One tab of the studio: link -> analysis -> preview -> multi-clip timeline -> export for ONE video.

Several workspaces live side by side in the main window's tabs. They share the settings, the job controller
(download queue) and the cookie vault; everything else (video, clips, crop, preview) is per tab.
"""

from __future__ import annotations

import logging
import shutil
import threading
import time
from pathlib import Path
from typing import List, Optional

from PySide6.QtCore import Qt, QThread, QTimer, Signal, Slot
from PySide6.QtGui import QGuiApplication, QPixmap
from PySide6.QtWidgets import (QComboBox, QDialog, QFileDialog, QFrame, QGridLayout, QHBoxLayout, QInputDialog, QLabel,
                               QLineEdit, QMessageBox, QPushButton, QScrollArea, QSizePolicy, QSplitter, QTabWidget,
                               QVBoxLayout, QWidget)

from app import theme
from app.config import Settings
from app.models.clip import MIN_CLIP_DURATION, Clip, ClipCollection, ClipError, OutputSettings
from app.models.crop import CropSettings, output_size
from app.models.download_job import DownloadJob, JobKind, JobStatus
from app.services import cookie_vault as cv
from app.services import signin_service as ss
from app.services import ytdlp_service as yd
from app.services.errors import FFmpegNotFound
from app.services.export_service import ClipSpec, SourceSpec, plan_export
from app.services.ffmpeg_service import find_executable, require_ffmpeg
from app.utils.paths import human_size, sanitize_filename
from app.utils.validation import UrlValidationError, format_timestamp, site_name, validate_url
from app.widgets import browser_login
from app.widgets.clip_list import ClipListWidget
from app.widgets.collapsible import CollapsibleSection
from app.widgets.crop_editor import CropControls
from app.widgets.playlist_dialog import PlaylistDialog
from app.widgets.timeline import TimelineWidget
from app.widgets.video_preview import VideoPreview
from app.workers.analyze_worker import AnalyzeWorker
from app.workers.download_worker import PreviewDownloadTask
from app.workers.export_worker import ThumbnailTask
from app.workers.job_controller import JobController
from app.workers.task_signals import TaskSignals

log = logging.getLogger("uvs.workspace")
PREVIEW_SELECTOR = "b[height<=480]/bv*[height<=480]+ba/w"
THUMB_COUNT = 40


def card(name: str = "card") -> QFrame:
    f = QFrame()
    f.setObjectName(name)
    return f


def _kv_value() -> QLabel:
    lab = QLabel("-")
    lab.setTextFormat(Qt.PlainText)          # titles/uploaders come from the web: never interpret them as HTML
    lab.setWordWrap(True)
    lab.setTextInteractionFlags(Qt.TextSelectableByMouse)
    lab.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
    return lab


class Workspace(QWidget):
    statusMessage = Signal(str, int)       # text, ms
    titleChanged = Signal(str)
    setupRequested = Signal(str)           # "" = just open Setup, otherwise an explanation to show first
    cookiesChanged = Signal()
    exportQueued = Signal()
    busyChanged = Signal(str)             # text while analysing/downloading a preview, '' when idle

    COMPACT_W = 900

    def __init__(self, settings: Settings, controller: JobController, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.settings = settings
        self.controller = controller
        self.title = "New tab"
        self.video: Optional[yd.VideoInfo] = None
        self.local_media: Optional[Path] = None
        self.clips = ClipCollection()
        self.tag = ""                                     # shown in front of job names when several tabs exist
        self.tsignals = TaskSignals(self)                 # preview/thumbnail signals of THIS tab only
        self._an_thread: Optional[QThread] = None
        self._an_worker: Optional[AnalyzeWorker] = None
        self._last_analyze = ("auto", None)
        self._thumb_cancel = threading.Event()
        self._preview_cancel = threading.Event()
        self._preview_busy = False
        self._last_seek = 0.0
        self._compact = False
        self._tiny = False
        self._short: Optional[bool] = None
        self._asked_signin: set = set()
        self._job_clip_ids: set = set()
        self._build_ui()
        self._wire()
        self._apply_defaults()
        self._set_enabled_state()

    def status(self, text: str, ms: int = 0) -> None:
        self.statusMessage.emit(text, ms)

    # ------------------------------------------------------------------ UI
    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 10, 12, 6)
        outer.setSpacing(10)

        # --- link bar
        bar = QHBoxLayout()
        bar.setSpacing(8)
        urlbar = card("urlbar")
        ul = QHBoxLayout(urlbar)
        ul.setContentsMargins(12, 3, 4, 3)
        ul.setSpacing(6)
        self.url_edit = QLineEdit()
        self.url_edit.setPlaceholderText("Paste a video link (YouTube, Facebook, or any site supported by yt-dlp)...")
        self.url_edit.setMinimumHeight(32)
        self.btn_paste = QPushButton(theme.icon("paste", size=16), "Paste")
        self.btn_clear = QPushButton(theme.icon("cancel", size=14), "Clear")
        self.btn_paste.setToolTip("Paste the link from the clipboard (Ctrl+L focuses the box)")
        self.btn_clear.setToolTip("Clear the link AND the loaded video, timeline and clips")
        ul.addWidget(self.url_edit, 1)
        ul.addWidget(self.btn_paste)
        ul.addWidget(self.btn_clear)
        self.btn_analyze = QPushButton("Analyze")
        self.btn_analyze.setObjectName("primary")
        self.btn_analyze.setMinimumHeight(38)
        self.btn_analyze.setMinimumWidth(96)
        self.btn_analyze.setToolTip("Fetch title, duration and formats (Enter)")
        self.btn_cancel_an = QPushButton("Cancel")
        self.btn_cancel_an.setMinimumHeight(38)
        self.btn_cancel_an.setVisible(False)
        bar.addWidget(urlbar, 1)
        bar.addWidget(self.btn_analyze)
        bar.addWidget(self.btn_cancel_an)
        outer.addLayout(bar)

        # --- left: preview over timeline
        self.preview = VideoPreview()
        preview_card = card()
        pl = QVBoxLayout(preview_card)
        pl.setContentsMargins(8, 8, 8, 8)
        pl.addWidget(self.preview)

        self.btn_delete = QPushButton(theme.icon("delete", size=16), "Delete")
        self.btn_dup = QPushButton(theme.icon("duplicate", size=16), "Duplicate")
        self.btn_prev_clip = QPushButton(theme.icon("play_range", size=16), "Preview clip")
        self.btn_delete.setToolTip("Delete the selected clip (Delete)")
        self.btn_dup.setToolTip("Duplicate the selected clip (Ctrl+D)")
        self.btn_prev_clip.setToolTip("Play only the selected clip")
        self.timeline = TimelineWidget(self.clips)
        self.timeline.setMinimumHeight(170)
        tl_card = card()
        tl = QVBoxLayout(tl_card)
        tl.setContentsMargins(10, 8, 10, 8)
        head = QHBoxLayout()
        head.setSpacing(6)
        self.timeline_title = QLabel("Multi-Trim Timeline")
        self.timeline_title.setObjectName("panelTitle")
        head.addWidget(self.timeline_title)
        self.sel_label = QLabel("")
        self.sel_label.setObjectName("muted")
        self.sel_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)     # never forces the panel wider
        head.addSpacing(8)
        head.addWidget(self.sel_label, 1)
        for b in (self.btn_prev_clip, self.btn_dup, self.btn_delete):
            head.addWidget(b)
        self.timeline.move_controls_to(head)
        tl.addLayout(head)
        tl.addWidget(self.timeline, 1)

        self.left = QSplitter(Qt.Vertical)
        self.left.setChildrenCollapsible(False)
        self.left.addWidget(preview_card)
        self.left.addWidget(tl_card)
        self.left.setStretchFactor(0, 3)
        self.left.setStretchFactor(1, 2)

        # --- right: info / output / crop / clips / export
        side = QWidget()
        sl = QVBoxLayout(side)
        sl.setContentsMargins(0, 0, 4, 0)
        sl.setSpacing(10)

        info = QWidget()
        ig = QGridLayout(info)
        ig.setContentsMargins(10, 8, 10, 8)
        ig.setVerticalSpacing(6)
        self.info_title, self.info_uploader, self.info_duration = _kv_value(), _kv_value(), _kv_value()
        self.info_resolution, self.info_meta = _kv_value(), _kv_value()
        for i, (k, w) in enumerate((("Title", self.info_title), ("Uploader", self.info_uploader),
                                    ("Duration", self.info_duration), ("Resolution", self.info_resolution))):
            key = QLabel(k)
            key.setObjectName("key")
            key.setAlignment(Qt.AlignTop)
            ig.addWidget(key, i, 0)
            ig.addWidget(w, i, 1)
        ig.setColumnStretch(1, 1)
        self.info_title.setObjectName("title")
        self.sec_info = CollapsibleSection("Video Info", info, True)

        self.export_panel = self._export_panel()
        self.sec_output = CollapsibleSection("Download & Output", self.export_panel, True)
        self.crop_controls = CropControls()
        self.sec_crop = CollapsibleSection("Crop / Aspect Ratio", self.crop_controls, True)
        opts = card()
        ol = QVBoxLayout(opts)
        ol.setContentsMargins(4, 0, 4, 4)
        ol.setSpacing(0)
        for s in (self.sec_info, self.sec_output, self.sec_crop):
            ol.addWidget(s)
        sl.addWidget(opts)

        clip_card = card()
        cl = QVBoxLayout(clip_card)
        cl.setContentsMargins(10, 8, 10, 10)
        cl.setSpacing(8)
        ch = QHBoxLayout()
        t = QLabel("Clip List & Export")
        t.setObjectName("panelTitle")
        ch.addWidget(t)
        ch.addStretch(1)
        self.clip_count = QLabel("")
        self.clip_count.setObjectName("muted")
        ch.addWidget(self.clip_count)
        cl.addLayout(ch)
        self.clip_list = ClipListWidget(self.clips)
        self.clip_list.setMinimumHeight(150)
        cl.addWidget(self.clip_list, 1)
        row = QHBoxLayout()
        self.btn_add = QPushButton(theme.icon("add", size=16), "Add Clip")
        self.btn_reset = QPushButton(theme.icon("reset", size=16), "Reset Range")
        self.btn_add.setToolTip("Add a clip at the playhead (Ctrl+N) - or double-click an empty spot on the timeline")
        self.btn_reset.setToolTip("Reset the selected clip to 5 seconds")
        row.addWidget(self.btn_add, 1)
        row.addWidget(self.btn_reset, 1)
        cl.addLayout(row)
        sl.addWidget(clip_card, 1)

        # export buttons stay visible under the scrolling options (like the bottom-right buttons of the mockup)
        foot = card()
        fl = QVBoxLayout(foot)
        fl.setContentsMargins(10, 10, 10, 10)
        fl.setSpacing(8)
        self.btn_all = QPushButton(theme.icon("download", size=18, color="#04121a"), "Download All Clips")
        self.btn_all.setObjectName("bigPrimary")
        self.btn_all.setToolTip("Export every clip as its own file (Ctrl+E)")
        self.btn_sel = QPushButton(theme.icon("download", size=16), "Selected Clip")
        self.btn_sel.setObjectName("bigSecondary")
        self.btn_sel.setToolTip("Download only the selected clip")
        self.btn_full = QPushButton(theme.icon("download", size=16), "Full Video")
        self.btn_full.setObjectName("bigSecondary")
        self.btn_full.setToolTip("Export the whole video (no trimming, no crop) with the format and quality chosen above")
        fl.addWidget(self.btn_all)
        two = QHBoxLayout()
        two.setSpacing(8)
        two.addWidget(self.btn_sel, 1)
        two.addWidget(self.btn_full, 1)
        fl.addLayout(two)

        self.side_scroll = QScrollArea()
        self.side_scroll.setWidgetResizable(True)
        self.side_scroll.setFrameShape(QScrollArea.NoFrame)
        self.side_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.side_scroll.setWidget(side)
        self.side_holder = QWidget()
        shl = QVBoxLayout(self.side_holder)
        shl.setContentsMargins(0, 0, 0, 0)
        shl.setSpacing(8)
        shl.addWidget(self.side_scroll, 1)
        shl.addWidget(foot)
        self.side_holder.setMinimumWidth(320)

        # wide: [left | side] splitter. compact: two tabs (so a small window never squeezes everything together)
        self.body = QSplitter(Qt.Horizontal)
        self.body.setChildrenCollapsible(False)
        self.body.addWidget(self.left)
        self.body.addWidget(self.side_holder)
        self.body.setStretchFactor(0, 1)
        self.body.setStretchFactor(1, 0)
        self.body.setSizes([760, 380])
        self.compact_tabs = QTabWidget()
        self.compact_tabs.hide()
        holder = QWidget()
        hl = QVBoxLayout(holder)
        hl.setContentsMargins(0, 0, 0, 0)
        hl.addWidget(self.body, 1)
        hl.addWidget(self.compact_tabs, 1)
        self._scroll = QScrollArea()                 # last resort on tiny screens: scroll instead of overlapping
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QScrollArea.NoFrame)
        self._scroll.setWidget(holder)
        outer.addWidget(self._scroll, 1)

    def _export_panel(self) -> QWidget:
        box = QWidget()
        g = QGridLayout(box)
        g.setContentsMargins(10, 8, 10, 8)
        g.setVerticalSpacing(8)
        self.fmt = QComboBox()
        self.fmt.addItem("MP4 video", "mp4")
        self.fmt.addItem("MP3 audio only", "mp3")
        self.quality = QComboBox()
        self.trim = QComboBox()
        self.trim.addItem("Fast (stream copy)", "fast")
        self.trim.addItem("Accurate (re-encode)", "accurate")
        self.trim.setToolTip("Fast: cut points snap to the nearest keyframe, so a clip may start a little early - "
                             "NOT frame-accurate.\nAccurate: re-encodes for precise start/end. "
                             "Cropping always re-encodes.")
        self.codec = QComboBox()
        self.codec.addItem("H.264 + AAC", "h264")
        self.codec.addItem("H.265 + AAC", "h265")
        self.out_dir = QLineEdit()
        self.btn_out = QPushButton(theme.icon("folder", size=16), "")
        self.btn_out.setToolTip("Choose output folder")
        self.est_label = QLabel("")
        self.est_label.setObjectName("muted")
        self.est_label.setWordWrap(True)
        def key(text: str) -> QLabel:
            k = QLabel(text)
            k.setObjectName("key")
            return k

        g.addWidget(key("Format"), 0, 0)
        g.addWidget(key("Quality"), 0, 1)
        g.addWidget(self.fmt, 1, 0)
        g.addWidget(self.quality, 1, 1)
        g.addWidget(key("Trim"), 2, 0)
        g.addWidget(key("Codec"), 2, 1)
        g.addWidget(self.trim, 3, 0)
        g.addWidget(self.codec, 3, 1)
        g.addWidget(key("Save to"), 4, 0, 1, 2)
        g.addWidget(self.out_dir, 5, 0)
        g.addWidget(self.btn_out, 5, 1)
        g.addWidget(self.est_label, 6, 0, 1, 2)
        g.setColumnStretch(0, 1)
        g.setColumnStretch(1, 1)
        g.setVerticalSpacing(4)
        return box

    # ------------------------------------------------------------ responsive layout
    def resizeEvent(self, e) -> None:  # noqa: N802
        super().resizeEvent(e)
        self.update_layout_mode()

    def update_layout_mode(self, w: Optional[int] = None, h: Optional[int] = None) -> None:
        """Adapt to the space this tab has (explicit sizes make it testable)."""
        w = self.width() if w is None else w
        h = self.height() if h is None else h
        self._set_short(h < 760)
        self._set_compact(w < self.COMPACT_W)
        avail = w if self._compact else w - 400            # width left for the video + timeline column
        self._set_tiny(avail < 780)

    def _set_compact(self, compact: bool) -> None:
        if compact == self._compact:
            return
        self._compact = compact
        if compact:
            self.compact_tabs.addTab(self.left, "Video && Timeline")
            self.compact_tabs.addTab(self.side_holder, "Clips && Export")
            self.body.hide()
            self.compact_tabs.show()
        else:
            for _ in range(self.compact_tabs.count()):
                self.compact_tabs.removeTab(0)
            self.body.addWidget(self.left)
            self.body.addWidget(self.side_holder)
            self.body.setStretchFactor(0, 1)
            self.body.setStretchFactor(1, 0)
            self.compact_tabs.hide()
            self.body.show()
            self.left.show()
            self.side_holder.show()
        self.timeline.hint.setVisible(not compact)

    def _set_short(self, short: bool) -> None:
        """Low windows: fold the info/crop sections, shrink the preview and timeline strip."""
        if short == self._short:
            return
        first = self._short is None
        self._short = short
        if short:
            self.sec_info.header.setChecked(False)      # settings are rarely touched: leave room for the clip list
            self.sec_output.header.setChecked(False)
        elif not first:
            self.sec_info.header.setChecked(True)
            self.sec_output.header.setChecked(True)
        self.crop_controls.info.setVisible(not short)
        self.preview.view.setMinimumHeight(110 if short else 150)
        self.timeline.setMinimumHeight(140 if short else 170)
        self.timeline.set_strip_height(30 if short else 40)
        self.clip_list.setMinimumHeight(90 if short else 150)

    def _set_tiny(self, tiny: bool) -> None:
        """Very narrow: icon-only buttons (tooltips keep their names)."""
        if tiny == self._tiny:
            return
        self._tiny = tiny
        names = {self.btn_paste: "Paste", self.btn_clear: "Clear", self.btn_delete: "Delete",
                 self.btn_dup: "Duplicate", self.btn_prev_clip: "Preview clip"}
        for b, t in names.items():
            b.setText("" if tiny else t)
            b.setToolTip(t)
        self.preview.btn_clip.setText("" if tiny else "Preview clip")
        self.preview.goto.setVisible(not tiny)

    def _wire(self) -> None:
        self.btn_paste.clicked.connect(self._paste)
        self.btn_clear.clicked.connect(lambda: self.clear_all())
        self.btn_analyze.clicked.connect(lambda: self.analyze("auto"))
        self.url_edit.returnPressed.connect(lambda: self.analyze("auto"))
        self.btn_cancel_an.clicked.connect(self._cancel_analysis)

        self.btn_add.clicked.connect(lambda: self.add_clip_at(self.timeline.canvas.playhead))
        self.btn_delete.clicked.connect(lambda: self._clip_action("delete", self.selected_id()))
        self.btn_dup.clicked.connect(lambda: self._clip_action("duplicate", self.selected_id()))
        self.btn_reset.clicked.connect(lambda: self._clip_action("reset", self.selected_id()))
        self.btn_prev_clip.clicked.connect(lambda: self._clip_action("preview", self.selected_id()))
        self.preview.btn_clip.clicked.connect(lambda: self._clip_action("preview", self.selected_id()))

        self.timeline.seekRequested.connect(self._seek)
        self.timeline.selectionChanged.connect(self._on_timeline_selection)
        self.timeline.clipActionRequested.connect(self._clip_action)
        self.timeline.addClipAt.connect(lambda t: self.add_clip_at(t, exact=True))
        self.timeline.addClipSpan.connect(lambda t, n: self.add_clip_at(t, exact=True, length=n))
        self.timeline.selectionSetChanged.connect(lambda _ids: self._set_enabled_state())
        self.timeline.handleDragged.connect(self._handle_dragged)
        self.clip_list.selectionChanged.connect(self._on_list_selection)
        self.clip_list.clipActionRequested.connect(self._clip_action)
        self.clip_list.editError.connect(lambda m: self.status(m, 6000))
        self.clips.subscribe(self._on_clips_changed)

        self.preview.positionChanged.connect(self._on_play_position)
        self.preview.downloadPreviewRequested.connect(self.request_preview_download)
        self.crop_controls.cropChanged.connect(self._crop_from_controls)
        self.crop_controls.applyToAll.connect(self._crop_apply_all)
        self.preview.overlay.cropChanged.connect(self._crop_from_overlay)

        for w in (self.fmt, self.quality, self.trim, self.codec):
            w.currentIndexChanged.connect(self._export_settings_changed)
        self.btn_out.clicked.connect(self._browse_out)
        self.btn_sel.clicked.connect(lambda: self.export_clips(selected_only=True))
        self.btn_all.clicked.connect(lambda: self.export_clips(selected_only=False))
        self.btn_full.clicked.connect(lambda: self.export_clips(selected_only=False, full=True))
        self.tsignals.preview_ready.connect(self._on_preview_ready)
        self.tsignals.preview_progress.connect(self._on_preview_progress)
        self.tsignals.thumbnail.connect(self._on_thumbnail)

    def _if_not_typing(self, fn) -> None:
        from PySide6.QtWidgets import QAbstractSpinBox, QApplication, QLineEdit as LE, QTableWidget
        w = QApplication.focusWidget()
        if isinstance(w, (LE, QAbstractSpinBox)):
            return
        if isinstance(w, QTableWidget) and w.state() == QTableWidget.EditingState:
            return
        fn()

    def _apply_defaults(self) -> None:
        s = self.settings
        self.out_dir.setText(s.download_dir)
        for combo, val in ((self.fmt, s.output_format), (self.trim, s.trim_mode), (self.codec, s.video_codec)):
            combo.setCurrentIndex(max(0, combo.findData(val)))
        self._fill_quality([])

    def apply_settings(self, settings: Settings) -> None:
        """Called after the Settings dialog saved: use the new defaults for folders; keep this tab's own choices."""
        self.settings = settings
        self.out_dir.setText(settings.download_dir)
        self._update_estimate()

    def refresh_icons(self) -> None:
        for b, n in ((self.btn_paste, "paste"), (self.btn_clear, "cancel"), (self.btn_add, "add"),
                     (self.btn_delete, "delete"), (self.btn_dup, "duplicate"), (self.btn_reset, "reset"),
                     (self.btn_prev_clip, "play_range"), (self.btn_sel, "download"), (self.btn_full, "download"),
                     (self.btn_out, "folder")):
            b.setIcon(theme.icon(n, size=16))
        self.btn_all.setIcon(theme.icon("download", size=18, color="#04121a"))
        self.timeline.refresh_icons()
        self.clip_list.refresh_icons()
        self.preview.refresh_icons()

    # ------------------------------------------------------------ helpers
    # ------------------------------------------------------------ helpers
    def selected_id(self) -> Optional[str]:
        return self.timeline.canvas.selected

    def _set_enabled_state(self) -> None:
        loaded = self.video is not None
        sel = self.selected_id() is not None
        has_clips = len(self.clips) > 0
        self.btn_add.setEnabled(loaded)
        for b in (self.btn_delete, self.btn_dup, self.btn_reset, self.btn_prev_clip):
            b.setEnabled(loaded and sel)
        self.btn_sel.setEnabled(loaded and sel)
        n_sel = len(self.selected_ids())
        self.btn_sel.setText(f"Selected ({n_sel})" if n_sel > 1 else "Selected Clip")
        self.btn_all.setEnabled(loaded and has_clips)
        self.clip_count.setText(f"{len(self.clips)} clip{'s' if len(self.clips) != 1 else ''}" if loaded else "")
        self.btn_full.setEnabled(loaded)
        mp4 = self.fmt.currentData() == "mp4"
        self.crop_controls.setEnabled(loaded and sel and mp4)
        self.codec.setEnabled(mp4)
        self.quality.setEnabled(loaded and mp4)
        self.preview.btn_clip.setEnabled(loaded and sel and self.preview.has_media())
        self.sel_label.setText(self._selection_text())

    def _selection_text(self) -> str:
        cid = self.selected_id()
        if len(self.selected_ids()) > 1:
            total = sum(c.duration for c in self.clips.clips if c.id in self.selected_ids())
            return f"{len(self.selected_ids())} clips selected ({total:.1f}s)   |   {len(self.clips)} clip(s)"
        if not cid:
            return f"{len(self.clips)} clip(s)" if self.video else ""
        try:
            c = self.clips.get(cid)
        except ClipError:
            return ""
        return (f"{c.name}: {format_timestamp(c.start, True)} - {format_timestamp(c.end, True)}"
                f"  ({c.duration:.2f}s)   |   {len(self.clips)} clip(s)")

    def _fill_quality(self, heights: List[int]) -> None:
        self.quality.blockSignals(True)
        self.quality.clear()
        self.quality.addItem("Best available", None)
        for h in heights:
            self.quality.addItem(f"{h}p", h)
        want = self.settings.max_height
        h_use, note = yd.resolve_quality(want, heights) if heights else (want, "")
        self.quality.setCurrentIndex(max(0, self.quality.findData(h_use)))
        self._quality_note = note
        self.quality.blockSignals(False)

    def _current_output(self) -> OutputSettings:
        return OutputSettings(container=self.fmt.currentData(), trim_mode=self.trim.currentData(),
                              max_height=self.quality.currentData(), video_codec=self.codec.currentData())

    def _export_settings_changed(self) -> None:
        if len(self.clips):
            self.clips.apply_output_to_all(self._current_output())
        self._update_estimate()
        self._set_enabled_state()

    def _update_estimate(self) -> None:
        if not self.video:
            self.est_label.setText("")
            return
        out = self._current_output()
        txt = []
        if out.container == "mp3":
            txt.append("Audio only: only the audio stream is downloaded.")
        else:
            size = self.video.estimate_size(out.max_height)
            if size:
                txt.append(f"Full source about {human_size(size)} (estimate). "
                           "Section downloads fetch only part of this when the site allows.")
            else:
                txt.append("Download size unknown for this site.")
            note = getattr(self, "_quality_note", "")
            if note:
                txt.append(note)
        if out.trim_mode == "fast" and out.container == "mp4":
            txt.append("Fast trim is keyframe-limited, not frame-accurate; cropping forces a re-encode.")
        self.est_label.setText(txt[0] if txt else "")
        self.est_label.setToolTip("\n".join(txt))

    def _on_clips_changed(self) -> None:
        self._set_enabled_state()
        self.timeline.canvas.update()
    # ------------------------------------------------------------ URL / analysis
    def _paste(self) -> None:
        text = QGuiApplication.clipboard().text().strip()
        if text:
            self.url_edit.setText(text)
            self.url_edit.setFocus()

    def _clear_url(self) -> None:
        self.url_edit.clear()
        self.url_edit.setFocus()

    def clear_all(self, ask: bool = True) -> None:
        """Clear the link box and everything loaded from it: preview, timeline, clips, info and crop."""
        if ask and len(self.clips) and QMessageBox.question(
                self, "Clear", f"Clear the video and its {len(self.clips)} clip(s) on the timeline?\n"
                               "Downloads already queued keep running.") != QMessageBox.Yes:
            return
        if self._an_worker:
            self._an_worker.cancel()
        self._preview_cancel.set()
        self._preview_busy = False
        self._reset_media()
        self.video = None
        self.clips.reset_source("", 0.0)
        self.timeline.set_duration(0.0)
        self.timeline.set_playhead(0.0)
        self.preview.set_busy(None)
        self.preview.set_duration_hint(0.0)
        self.preview.set_frame_size(16, 9)
        self.preview.show_message("Paste a video URL and click Analyze to begin.")
        for lab in (self.info_title, self.info_uploader, self.info_duration, self.info_resolution):
            lab.setText("-")
        self.info_resolution.setToolTip("")
        self.info_meta.setText("")
        self._fill_quality([])
        self._update_estimate()
        self._load_crop_for(None)
        self._asked_signin.clear()
        self.title = "New tab"
        self.url_edit.clear()
        self.url_edit.setFocus()
        self.titleChanged.emit("")
        self._set_enabled_state()
        self.status("Cleared", 3000)

    def analyze(self, mode: str, url_override: Optional[str] = None) -> None:
        if self._an_thread is not None:   # duplicate-job guard
            return
        self._last_analyze = (mode, url_override)
        try:
            url = validate_url(url_override or self.url_edit.text())
        except UrlValidationError as exc:
            QMessageBox.warning(self, "Invalid link", str(exc))
            return
        self.url_edit.setText(url)
        self.btn_analyze.setEnabled(False)
        self.btn_analyze.setText("Analyzing...")
        self.btn_cancel_an.setVisible(True)
        self.status("Analyzing link...")
        self.preview.set_busy("Analyzing link...")
        self.busyChanged.emit("Analyzing...")
        worker = AnalyzeWorker(url, mode, "")        # cookie source comes from yt_dlp service (set_cookie_spec)
        thread = QThread(self)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(self._on_analyzed)
        worker.playlist.connect(self._on_playlist)
        worker.failed.connect(self._on_analysis_failed)
        worker.canceled.connect(self._on_analysis_canceled)
        worker.done.connect(thread.quit)
        thread.finished.connect(self._on_analysis_thread_done)
        self._an_thread, self._an_worker = thread, worker
        thread.start()

    def _cancel_analysis(self) -> None:
        if self._an_worker:
            self._an_worker.cancel()
            self.btn_cancel_an.setEnabled(False)

    @Slot()
    def _on_analysis_thread_done(self) -> None:
        if self._an_thread:
            self._an_thread.deleteLater()
        if self._an_worker:
            self._an_worker.deleteLater()
        self._an_thread = self._an_worker = None
        self.btn_analyze.setEnabled(True)
        self.btn_analyze.setText("Analyze")
        self.preview.set_busy(None)
        self.busyChanged.emit("")
        self.btn_cancel_an.setVisible(False)
        self.btn_cancel_an.setEnabled(True)

    @Slot()
    def _on_analysis_canceled(self) -> None:
        self.status("Analysis canceled", 5000)
        self.preview.show_message("Analysis canceled." if not self.video else "")
        if self.video:
            self.preview.show_video()

    @Slot(str, str, str)
    def _on_analysis_failed(self, message: str, details: str, hint: str = "") -> None:
        self.status("Analysis failed", 5000)
        self.preview.show_message(message)
        if message.startswith("yt-dlp is not installed"):
            self.setupRequested.emit("yt-dlp is not installed yet.")
            return
        self.prompt_problem("Could not analyze the link", message, details, hint, self.url_edit.text().strip(),
                            retry=lambda: self.analyze(*self._last_analyze))

    def prompt_problem(self, title: str, message: str, details: str, hint: str, url: str, retry=None) -> None:
        """Explain a failure and offer the button that fixes it (sign in / setup / update)."""
        if hint in ("signin", "cookies") and url:
            e = cv.get_vault().entry_for(url)
            if e is not None and e.signed_in is False:
                message += (f"\n\nA {e.label} session is saved, but it contains no login "
                            f"({cv.missing_login_cookies(e.site)} is missing) - the sign-in was probably saved before "
                            "the login finished. Sign in again and press Save only after you see your account.")
            elif e is not None:
                message += (f"\n\nA {e.label} sign-in is saved but the site did not accept it (it may have expired, "
                            "or the account cannot see this video). Sign in again to refresh it.")
        box = QMessageBox(QMessageBox.Warning, title, message, QMessageBox.NoButton, self)
        if details:
            box.setDetailedText(details)
        site = cv.site_label(cv.site_key(url)) if url else ""
        b_sign = b_setup = b_cookies = None
        if hint in ("signin", "cookies", "update") and url:
            b_sign = box.addButton(f"Sign in to {site}...", QMessageBox.AcceptRole)
            b_cookies = box.addButton("Cookies && sign-ins...", QMessageBox.ActionRole)
        if hint in ("js", "update"):
            b_setup = box.addButton("Open Setup / Update", QMessageBox.ActionRole)
        box.addButton("Close", QMessageBox.RejectRole)
        box.exec()
        clicked = box.clickedButton()
        if clicked is b_setup and b_setup is not None:
            self.setupRequested.emit("")
        elif clicked is b_sign and b_sign is not None:
            if self.sign_in(url) and retry:
                QTimer.singleShot(0, retry)
        elif clicked is b_cookies and b_cookies is not None:
            from app.widgets.cookie_manager import CookieManagerDialog
            CookieManagerDialog(self, url).exec()
            self.cookiesChanged.emit()
            if cv.get_vault().has_cookies_for(url) and retry:
                QTimer.singleShot(0, retry)

    def sign_in(self, url: str) -> bool:
        """Open the in-app browser for this link's site; the saved cookies are used from the next request on."""
        entry = browser_login.sign_in(url, self)
        if entry:
            self._asked_signin.discard(cv.site_key(url))
            self.cookiesChanged.emit()
            self.status(f"Signed in to {entry.label} - {entry.count} cookies saved.", 8000)
        return entry is not None

    @Slot(object)
    def _on_playlist(self, info: yd.PlaylistInfo) -> None:
        self.preview.show_message("Playlist detected - choose what to do.")
        dlg = PlaylistDialog(info, self)
        if dlg.exec() != QDialog.Accepted:
            self.preview.show_message("Paste a video URL and click Analyze to begin.")
            return
        if dlg.choice == "download_all":
            self._download_playlist(info)
        elif dlg.choice == "open" and dlg.selected_url:
            QTimer.singleShot(0, lambda: self.analyze("single", dlg.selected_url))
        elif dlg.choice == "single":
            QTimer.singleShot(0, lambda: self.analyze("single"))

    def _download_playlist(self, info: yd.PlaylistInfo) -> None:
        try:
            ffmpeg = require_ffmpeg(self.settings.ffmpeg_path)
        except FFmpegNotFound:
            self.setupRequested.emit("FFmpeg is required for this and is not installed yet.")
            return
        out = Path(self.out_dir.text().strip() or self.settings.download_dir)
        sel = yd.build_format_selector(self.settings.max_height)
        for e in info.entries:
            key = sanitize_filename(e.title, 100, "video")
            spec = SourceSpec(e.url, e.title, sel, key, out, ffmpeg, "")
            self.controller.add(DownloadJob(JobKind.SOURCE, f"{self.tag}{e.title} (full video)", site_name(e.url), spec))
        self.status(f"Queued {len(info.entries)} playlist videos", 6000)

    @Slot(object)
    def _on_analyzed(self, info: yd.VideoInfo) -> None:
        self.preview.set_busy(None)
        self._reset_media()
        self.video = info
        dur = info.duration or 0.0
        self.clips.reset_source(info.id or info.url, dur)
        self.timeline.set_duration(dur)
        self.info_title.setText(info.title)
        self.info_uploader.setText(info.uploader or "-")
        self.info_duration.setText(format_timestamp(dur))
        self.title = info.title
        self.titleChanged.emit(info.title)
        heights = ", ".join(f"{h}p" for h in info.heights[:8]) or "audio only"
        self.info_meta.setText(f"{site_name(info.url)}: {heights}")     # not shown as a row; used as tooltip below
        self.info_resolution.setToolTip(f"Available: {heights}")
        w, h = info.source_size
        self.info_resolution.setText(f"{w} x {h}" if w and h else (f"{info.heights[0]}p" if info.heights else "-"))
        self.preview.set_frame_size(w, h)
        self.preview.set_poster(info.thumbnail_bytes)
        self.preview.set_duration_hint(dur)
        self._fill_quality(info.heights)
        self._update_estimate()
        selector = yd.build_format_selector(self.quality.currentData(), self.fmt.currentData() == "mp3")
        cached = yd.find_cached_source(self.settings.cache_dir, yd.source_key(info.url, selector))
        if cached:
            self._use_local_media(cached, "Using the downloaded source for preview.")
        elif info.preview_url:
            self.preview.set_source(info.preview_url, is_file=False)
        else:
            self.preview.show_message("Live preview is not available for this site/format. You can still select "
                                      "clips on the timeline. To see the video, download a small preview file.",
                                      offer_fetch=True)
        self.status(f"Loaded: {info.title}", 6000)
        self._set_enabled_state()

    def _reset_media(self) -> None:
        self._thumb_cancel.set()
        self._thumb_cancel = threading.Event()
        self.preview.clear_media()
        self.local_media = None
        self.timeline.canvas.thumbs.clear()
        self.preview.set_poster(None)

    # ------------------------------------------------------------ preview download / thumbnails
    def request_preview_download(self) -> None:
        if not self.video or self._preview_busy:
            return
        size = self.video.estimate_size(480)
        msg = ("To show the video and timeline thumbnails, a low-resolution (max 480p) copy of the whole video must be "
               f"downloaded{f' (about {human_size(size)})' if size else ''}.\n\nDownload it now?")
        if QMessageBox.question(self, "Download preview file", msg) != QMessageBox.Yes:
            return
        try:
            ffmpeg = require_ffmpeg(self.settings.ffmpeg_path)
        except FFmpegNotFound:
            self.setupRequested.emit("FFmpeg is required for this and is not installed yet.")
            return
        self._preview_busy = True
        self._preview_cancel = threading.Event()
        key = yd.source_key(self.video.url, PREVIEW_SELECTOR)
        self.preview.set_busy("Downloading preview file...")
        self.busyChanged.emit("Preview...")
        self.controller.pool.start(PreviewDownloadTask(
            self.video.url, PREVIEW_SELECTOR, Path(self.settings.temp_dir) / "preview", key, ffmpeg,
            "", self.tsignals, self._preview_cancel))

    @Slot(float)
    def _on_preview_progress(self, pct: float) -> None:
        if pct >= 0:
            self.status(f"Downloading preview file... {pct:.0f}%")
            if self._preview_busy:
                self.preview.set_busy("Downloading preview file...", pct)

    @Slot(str, str)
    def _on_preview_ready(self, path: str, error: str) -> None:
        self._preview_busy = False
        self.preview.set_busy(None)
        self.busyChanged.emit("")
        if not self.video:                       # cleared or closed meanwhile
            return
        if path:
            self._use_local_media(Path(path), "Preview file ready.")
        else:
            self.preview.show_message(f"Preview file could not be downloaded: {error}", offer_fetch=True)

    def _use_local_media(self, path: Path, msg: str) -> None:
        self.local_media = path
        self.preview.set_source(str(path), is_file=True)
        self.status(msg, 5000)
        self._start_thumbnails(path)
        self._set_enabled_state()

    def _start_thumbnails(self, path: Path) -> None:
        if not self.video or not self.video.duration:
            return
        ffmpeg = find_executable("ffmpeg", self.settings.ffmpeg_path)
        if not ffmpeg:
            return
        self._thumb_cancel.set()
        self._thumb_cancel = threading.Event()
        self.timeline.canvas.thumbs.clear()
        out_dir = Path(self.settings.temp_dir) / "thumbs" / yd.source_key(self.video.url, "t")
        self.controller.pool.start(ThumbnailTask(ffmpeg, path, self.video.duration, out_dir,
                                                 self.tsignals, self._thumb_cancel))

    @Slot(int, float, str)
    def _on_thumbnail(self, idx: int, t: float, path: str) -> None:
        pm = QPixmap(path)
        if not pm.isNull() and self.video and self.video.duration:
            self.timeline.canvas.add_thumbnail(idx, t, pm, self.video.duration / THUMB_COUNT)

    # ------------------------------------------------------------ playback / timeline sync
    def _seek(self, t: float) -> None:
        self.timeline.set_playhead(t, follow=False)
        self.preview.seek_silent(t)
        self.preview.time_label.setText(f"{format_timestamp(t)} / {format_timestamp(self.video.duration if self.video else None)}")

    def _on_play_position(self, t: float) -> None:
        self.timeline.set_playhead(t)

    def _handle_dragged(self, t: float) -> None:
        now = time.monotonic()
        if now - self._last_seek > 0.08:
            self._last_seek = now
            self.timeline.set_playhead(t, follow=False)
            self.preview.seek_silent(t)

    # ------------------------------------------------------------ clips
    def add_clip_at(self, t: float, exact: bool = False, length: float = 5.0) -> None:
        """Add a clip. ``exact`` (double-click / right-click on the timeline) puts it right where the user pointed -
        even on top of an existing clip, which then moves the new one to its own lane; otherwise a free gap is used."""
        if not self.video:
            return
        if exact:
            dur = self.video.duration or (t + length)
            length = max(MIN_CLIP_DURATION, min(length, dur))
            start = max(0.0, min(t, dur - length))
            end = start + length
        else:
            start, end = self.clips.find_gap(length, near=t)
        try:
            c = self.clips.add(start, end, crop=CropSettings(self.settings.default_crop), output=self._current_output())
        except ClipError as exc:
            self.status(str(exc), 5000)
            return
        self.timeline.select(c.id)
        self.clip_list.select(c.id)

    def selected_ids(self) -> List[str]:
        return list(self.timeline.canvas.selected_ids)

    def _on_timeline_selection(self, cid: Optional[str]) -> None:
        self.clip_list.select(cid)
        self._load_crop_for(cid)
        self._set_enabled_state()

    def _on_list_selection(self, cid: Optional[str]) -> None:
        self.timeline.select(cid)
        self._load_crop_for(cid)
        self._set_enabled_state()
        if cid:
            c = self.clips.get(cid)
            self.timeline.canvas.ensure_visible(c.start)
            self.timeline._sync_scroll()

    def _clip_action(self, action: str, cid: Optional[str]) -> None:
        if not cid:
            return
        try:
            c = self.clips.get(cid)
        except ClipError:
            return
        group = self.selected_ids()
        if action in ("delete", "duplicate") and len(group) > 1 and cid in group:     # act on every selected clip
            if action == "delete":
                for gid in group:
                    self.clips.remove(gid)
            else:
                self.timeline.canvas.select_many([self.clips.duplicate(gid).id for gid in group])
            self._set_enabled_state()
            return
        if action == "delete":
            self.clips.remove(cid)
        elif action == "duplicate":
            d = self.clips.duplicate(cid)
            self.timeline.select(d.id)
            self.clip_list.select(d.id)
        elif action == "rename":
            name, ok = QInputDialog.getText(self, "Rename clip", "Clip name:", text=c.name)
            if ok:
                try:
                    self.clips.rename(cid, name)
                except ClipError as exc:
                    self.status(str(exc), 5000)
        elif action == "reset":
            self.clips.reset(cid)
        elif action == "preview":
            if not self.preview.play_range(c.start, c.end):
                self.preview.show_message("Preview playback is not available yet. Download a small preview file to "
                                          "preview clips.", offer_fetch=True)
        self._set_enabled_state()

    # ------------------------------------------------------------ crop
    def _load_crop_for(self, cid: Optional[str]) -> None:
        if not cid:
            self.preview.overlay.load(CropSettings())
            self.crop_controls.set_settings(CropSettings())
            self.crop_controls.set_info("Select a clip to crop it")
            return
        c = self.clips.get(cid)
        self.preview.overlay.load(c.crop)
        self.crop_controls.set_settings(c.crop)
        self._crop_info(c.crop)

    def _crop_info(self, s: CropSettings) -> None:
        if not self.video:
            return
        w, h = self.video.source_size
        ow, oh = output_size(s, w, h)
        self.crop_controls.set_info(f"Output framing: {ow} x {oh} px (of {w} x {h} source)")

    def _crop_from_controls(self, s: CropSettings) -> None:
        cid = self.selected_id()
        if not cid:
            return
        self.preview.overlay.load(s)
        self.clips.set_crop(cid, s)
        self._crop_info(s)

    def _crop_from_overlay(self, s: CropSettings) -> None:
        cid = self.selected_id()
        if not cid:
            return
        self.crop_controls.set_settings(s)
        self.clips.set_crop(cid, s)
        self._crop_info(s)

    def _crop_apply_all(self) -> None:
        cid = self.selected_id()
        if cid:
            self.clips.apply_crop_to_all(self.clips.get(cid).crop)
            self.status("Crop applied to all clips", 4000)
    # ------------------------------------------------------------ export
    def _browse_out(self) -> None:
        p = QFileDialog.getExistingDirectory(self, "Choose output folder", self.out_dir.text() or str(Path.home()))
        if p:
            self.out_dir.setText(p)

    def export_clips(self, selected_only: bool, full: bool = False) -> None:
        if not self.video:
            return
        clips: List[Clip] = []
        if full:
            # The whole video as one export: not stored on the timeline, never cropped or trimmed.
            dur = self.video.duration or 0.0
            stem = sanitize_filename(self.video.title, 100, "video") + "_full"
            clips = [Clip(self.video.id or self.video.url, 0.0, dur, "Full video", filename=stem,
                          output=self._current_output())]
        elif selected_only:
            ids = self.selected_ids() or ([self.selected_id()] if self.selected_id() else [])
            clips = [c for c in self.clips.clips if c.id in ids]          # timeline order
        else:
            clips = list(self.clips.clips)
        if not clips:
            self.status("Add at least one clip first", 5000)
            return
        try:
            ffmpeg = require_ffmpeg(self.settings.ffmpeg_path)
        except FFmpegNotFound:
            self.setupRequested.emit("FFmpeg is required for this and is not installed yet.")
            return
        for c in clips:
            try:
                c.validate(self.video.duration)
            except ClipError as exc:
                QMessageBox.warning(self, "Invalid clip", f"{c.name}: {exc}")
                return
        out_dir = Path(self.out_dir.text().strip() or self.settings.download_dir)
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
            test = out_dir / ".uvs_write_test"
            test.write_text("x")
            test.unlink()
        except OSError as exc:
            QMessageBox.warning(self, "Output folder", f"Cannot write to this folder:\n{exc}")
            return
        # skip clips that are already queued or running (duplicate-job guard)
        busy = {j.clip_id for j in self.controller.queue.jobs if j.kind == JobKind.CLIP and not j.is_finished}
        todo = [c for c in clips if c.id not in busy]
        if not todo:
            self.status("Those clips are already in the queue", 5000)
            return

        out = self._current_output()
        audio_only = out.container == "mp3"
        plan = plan_export(todo, self.video.duration or 0, self.video.url, self.video.title, self.video.uploader,
                           self.video.id, site_name(self.video.url), self.settings.filename_template,
                           self.settings.cache_dir, out.max_height, audio_only,
                           self.settings.prefer_sections and not full)
        est = self.video.estimate_size(out.max_height) if not audio_only else None
        if plan.needs_source_download and est:
            try:
                free = shutil.disk_usage(self.settings.cache_dir.anchor if not self.settings.cache_dir.exists()
                                         else self.settings.cache_dir).free
                if free < est * 1.3:
                    if QMessageBox.question(self, "Low disk space",
                                            f"The download may need about {human_size(est)} but only {human_size(free)} "
                                            "is free on the temporary drive. Continue anyway?") != QMessageBox.Yes:
                        return
            except OSError:
                pass

        source_job: Optional[DownloadJob] = None
        site = site_name(self.video.url)
        if plan.strategy in ("download_once", "local_cache"):
            source_job = self.controller.source_job_for(plan.key)
            if source_job is None:
                label = "Source (cached)" if plan.strategy == "local_cache" else "Source download"
                spec = SourceSpec(self.video.url, self.video.title, plan.selector, plan.key,
                                  self.settings.cache_dir, ffmpeg, "")
                source_job = self.controller.add(DownloadJob(JobKind.SOURCE, f"{self.tag}{label}: {self.video.title}", site, spec))
        sw, sh = self.video.source_size
        for pc in plan.clips:
            c = pc.clip
            spec = ClipSpec(
                clip_id=c.id, name=c.name, start=c.start, end=c.end, crop=c.crop.copy(), output=c.output.copy(),
                src_w=sw, src_h=sh, output_dir=out_dir, stem=pc.stem,
                mode="section" if plan.strategy == "remote_sections" else "local", url=self.video.url,
                selector=plan.selector, ffmpeg_path=ffmpeg, temp_dir=Path(self.settings.temp_dir),
                cookies_browser="", keep_intermediates=self.settings.keep_intermediates, hw_encoder=self.settings.hw_encoder)
            self._job_clip_ids.add(c.id)
            job = DownloadJob(JobKind.CLIP, f"{self.tag}{pc.stem}.{c.output.container}", site, spec,
                              depends_on=source_job.id if source_job else None, clip_id=c.id)
            self.clip_list.set_status(c.id, "Queued")
            self.controller.add(job)
        notes = " ".join(plan.notes)
        if out.trim_mode == "fast" and out.container == "mp4":
            notes += " Fast mode is keyframe-limited (not frame-accurate)."
        self.status(f"Queued {len(plan.clips)} clip(s). {notes}", 12000)
        self.exportQueued.emit()

    # ------------------------------------------------------------ jobs of this tab
    def owns_job(self, job: DownloadJob) -> bool:
        if job.clip_id:
            return job.clip_id in self._job_clip_ids
        return bool(self.video and getattr(job.payload, "url", None) == self.video.url)

    def on_job_changed(self, job: DownloadJob) -> None:
        if not self.owns_job(job):
            return
        if job.clip_id:
            label = {JobStatus.COMPLETED: "Done", JobStatus.FAILED: "Failed", JobStatus.CANCELED: "Canceled",
                     JobStatus.WAITING: "Queued"}.get(job.status, job.status.value)
            if job.progress is not None and job.is_active:
                label += f" {job.progress:.0f}%"
            self.clip_list.set_status(job.clip_id, label)
        if job.kind == JobKind.SOURCE and job.status == JobStatus.COMPLETED and job.output_path \
                and not self.local_media and self.video:
            self._use_local_media(Path(job.output_path), "Source downloaded - preview and thumbnails use it.")
        if job.status == JobStatus.FAILED and job.error:
            hint = ss.classify(job.error)
            url = getattr(job.payload, "url", "") or (self.video.url if self.video else "")
            key = cv.site_key(url)
            if hint in ("signin", "cookies") and url and key not in self._asked_signin:
                self._asked_signin.add(key)           # ask once per site until the user signed in
                QTimer.singleShot(0, lambda j=job, u=url, h=hint: self._offer_job_signin(j, u, h))

    def _offer_job_signin(self, job: DownloadJob, url: str, hint: str) -> None:
        first = (job.error or "").split("\n\nDetails:")[0]
        self.prompt_problem("Sign-in needed", first, "", hint, url, retry=lambda: self._retry_failed())

    def _retry_failed(self) -> None:
        for j in list(self.controller.queue.jobs):
            if j.status == JobStatus.FAILED and self.owns_job(j):
                try:
                    self.controller.retry(j.id)
                except Exception as exc:  # noqa: BLE001
                    self.status(f"Cannot retry: {exc}", 5000)

    def is_busy(self) -> bool:
        return self._an_thread is not None

    def shutdown(self) -> None:
        self._thumb_cancel.set()
        self._preview_cancel.set()
        if self._an_worker:
            self._an_worker.cancel()
        if self._an_thread:
            self._an_thread.quit()
            self._an_thread.wait(3000)

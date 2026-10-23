"""Preview panel: Qt Multimedia playback inside a graphics scene (so a crop overlay can sit on top)."""

from __future__ import annotations

import logging
import os
from typing import Optional, Tuple

from PySide6.QtCore import QRectF, QSizeF, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QConicalGradient, QFont, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (QGraphicsPixmapItem, QGraphicsScene, QGraphicsView, QHBoxLayout, QLabel,
                               QLineEdit, QPushButton, QSlider, QStackedLayout, QVBoxLayout, QWidget)

from app import theme
from app.utils.validation import format_timestamp, parse_timestamp
from app.widgets.crop_editor import CropOverlay

log = logging.getLogger("uvs.preview")
try:  # Qt Multimedia can be missing/broken on some systems; keep the app usable.
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtMultimedia import QAudioOutput, QMediaDevices, QMediaPlayer
    from PySide6.QtMultimediaWidgets import QGraphicsVideoItem
    HAVE_MULTIMEDIA = True
except Exception as exc:  # noqa: BLE001
    log.warning("Qt Multimedia unavailable: %s", exc)
    HAVE_MULTIMEDIA = False

FRAME_W = 1280.0


class PreviewView(QGraphicsView):
    """Fits the frame rectangle into the view, keeping aspect ratio."""

    def __init__(self, scene: QGraphicsScene) -> None:
        super().__init__(scene)
        self.setRenderHint(self.renderHints())
        self.setFrameShape(QGraphicsView.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setBackgroundBrush(Qt.black)
        self.setMinimumHeight(150)

    def resizeEvent(self, e) -> None:  # noqa: N802
        super().resizeEvent(e)
        self.fitInView(self.sceneRect(), Qt.KeepAspectRatio)


class BusyOverlay(QWidget):
    """Dimmed layer with a spinning arc, a text and (when the percentage is known) a progress bar.

    It only animates while visible, so an idle window costs nothing."""

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.text = ""
        self.pct: Optional[float] = None
        self.angle = 0
        self.timer = QTimer(self)
        self.timer.setInterval(33)
        self.timer.timeout.connect(self._tick)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, False)
        self.hide()

    def start(self, text: str, pct: Optional[float] = None) -> None:
        self.text, self.pct = text, pct
        if not self.isVisible():
            self.setGeometry(self.parentWidget().rect())
            self.show()
            self.raise_()
            self.timer.start()
        self.update()

    def stop(self) -> None:
        self.timer.stop()
        self.hide()

    def _tick(self) -> None:
        self.angle = (self.angle - 12) % 360
        self.update()

    def paintEvent(self, _e) -> None:  # noqa: N802
        pal = theme.palette()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.fillRect(self.rect(), QColor(6, 10, 18, 238))
        cx, cy = self.width() / 2, self.height() / 2 - 14
        r = max(14, min(26, min(self.width(), self.height()) / 8))
        g = QConicalGradient(cx, cy, self.angle)
        g.setColorAt(0.0, QColor(pal["accent"]))
        g.setColorAt(0.75, QColor(pal["accent2"]))
        tail = QColor(pal["accent2"])
        tail.setAlpha(0)
        g.setColorAt(1.0, tail)
        pen = QPen(g, max(4, r / 5))
        pen.setCapStyle(Qt.RoundCap)
        p.setPen(pen)
        p.drawArc(int(cx - r), int(cy - r), int(2 * r), int(2 * r), 0, 16 * 360)
        p.setPen(QColor("#e9edf6"))
        font = QFont(self.font())
        font.setPointSizeF(font.pointSizeF() + 1)
        p.setFont(font)
        label = self.text + (f"  {self.pct:.0f}%" if self.pct is not None else "")
        p.drawText(0, int(cy + r + 12), self.width(), 28, Qt.AlignHCenter | Qt.AlignTop, label)
        bw = min(260.0, self.width() * 0.6)
        bx, by = (self.width() - bw) / 2, cy + r + 46
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(255, 255, 255, 40))
        p.drawRoundedRect(bx, by, bw, 6, 3, 3)
        p.setBrush(QColor(pal["accent"]))
        if self.pct is not None:
            p.drawRoundedRect(bx, by, bw * max(0.0, min(100.0, self.pct)) / 100.0, 6, 3, 3)
        else:                                           # unknown length: a block sliding back and forth
            phase = (-self.angle % 360) / 360.0
            pos = abs(2 * phase - 1)
            p.drawRoundedRect(bx + (bw - bw * 0.3) * pos, by, bw * 0.3, 6, 3, 3)


class VideoPreview(QWidget):
    positionChanged = Signal(float)      # seconds
    seekedByUser = Signal(float)
    downloadPreviewRequested = Signal()  # user asked to fetch a local preview file
    clipPlaybackFinished = Signal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.duration = 0.0
        self._play_end: Optional[float] = None
        self._has_media = False
        self._dragging = False
        self.scene = QGraphicsScene(self)
        self.view = PreviewView(self.scene)
        self.poster = QGraphicsPixmapItem()
        self.scene.addItem(self.poster)
        self.overlay = CropOverlay()
        self.player = None
        self.video_item = None
        if HAVE_MULTIMEDIA:
            self.video_item = QGraphicsVideoItem()
            self.video_item.setAspectRatioMode(Qt.IgnoreAspectRatio)
            self.scene.addItem(self.video_item)
            self.player = QMediaPlayer(self)
            # QAudioOutput can crash on machines without any audio device: only create it when one exists.
            self.audio = None
            no_audio = os.environ.get("UVS_NO_AUDIO") == "1" or QGuiApplication.platformName() == "offscreen"
            if not no_audio and QMediaDevices.audioOutputs():
                self.audio = QAudioOutput(self)
                self.player.setAudioOutput(self.audio)
            self.player.setVideoOutput(self.video_item)
            self.player.positionChanged.connect(self._on_position)
            self.player.durationChanged.connect(self._on_duration)
            self.player.errorOccurred.connect(self._on_error)
            self.player.playbackStateChanged.connect(lambda _s: self._sync_play_icon())
            self.player.mediaStatusChanged.connect(self._on_status)
        self.scene.addItem(self.overlay)
        self.set_frame_size(16, 9)

        self.message = QLabel("Paste a video URL and click Analyze to begin.")
        self.message.setAlignment(Qt.AlignCenter)
        self.message.setWordWrap(True)
        self.message.setObjectName("muted")
        self.btn_fetch = QPushButton("Download a small preview file...")
        self.btn_fetch.setToolTip("Downloads a low-resolution copy only for preview and timeline thumbnails.")
        self.btn_fetch.setVisible(False)
        self.btn_fetch.clicked.connect(self.downloadPreviewRequested)
        overlay_msg = QVBoxLayout()
        overlay_msg.addStretch(1)
        overlay_msg.addWidget(self.message)
        overlay_msg.addWidget(self.btn_fetch, 0, Qt.AlignHCenter)
        overlay_msg.addStretch(1)
        self.msg_widget = QWidget()
        self.msg_widget.setLayout(overlay_msg)
        self.msg_widget.setStyleSheet("background: transparent;")

        stack_host = QWidget()
        self.stack = QStackedLayout(stack_host)
        self.stack.setStackingMode(QStackedLayout.StackAll)
        self.stack.addWidget(self.view)
        self.stack.addWidget(self.msg_widget)
        self.stack.setCurrentWidget(self.msg_widget)
        self.busy = BusyOverlay(stack_host)
        stack_host.installEventFilter(self)
        self._stack_host = stack_host

        # controls
        self.btn_play = QPushButton(theme.icon("play", size=18), "")
        self.btn_back = QPushButton(theme.icon("back", size=18), "")
        self.btn_fwd = QPushButton(theme.icon("forward", size=18), "")
        self.btn_clip = QPushButton(theme.icon("play_range", size=18), "Preview clip")
        self.btn_play.setToolTip("Play / pause (Space)")
        self.btn_back.setToolTip("Back 5 seconds (Left)")
        self.btn_fwd.setToolTip("Forward 5 seconds (Right)")
        self.btn_clip.setToolTip("Play only the selected clip")
        for b, n in ((self.btn_play, "Play or pause"), (self.btn_back, "Back 5 seconds"), (self.btn_fwd, "Forward 5 seconds")):
            b.setAccessibleName(n)
        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(0, 1000)
        self.time_label = QLabel("00:00 / --:--")
        self.time_label.setMinimumWidth(110)
        self.goto = QLineEdit()
        self.goto.setPlaceholderText("Go to  mm:ss")
        self.goto.setMaximumWidth(110)
        self.goto.setToolTip("Type a time such as 1:30 or 01:02:03 and press Enter")
        ctl = QHBoxLayout()
        for w in (self.btn_back, self.btn_play, self.btn_fwd, self.slider, self.time_label, self.goto, self.btn_clip):
            ctl.addWidget(w, 1 if w is self.slider else 0)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(stack_host, 1)
        lay.addLayout(ctl)

        self.btn_play.clicked.connect(self.toggle_play)
        self.btn_back.clicked.connect(lambda: self.seek(self.position() - 5))
        self.btn_fwd.clicked.connect(lambda: self.seek(self.position() + 5))
        self.slider.sliderPressed.connect(lambda: setattr(self, "_dragging", True))
        self.slider.sliderReleased.connect(self._slider_released)
        self.slider.sliderMoved.connect(lambda v: self.time_label.setText(self._time_text(v / 1000 * self.duration)))
        self.goto.returnPressed.connect(self._goto)
        self._set_controls(False)

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        if obj is getattr(self, "_stack_host", None) and event.type() == event.Type.Resize:
            self.busy.setGeometry(self._stack_host.rect())
        return super().eventFilter(obj, event)

    def set_busy(self, text: Optional[str], pct: Optional[float] = None) -> None:
        """Show an animated "working" layer over the preview (text=None hides it)."""
        if text:
            self.busy.start(text, pct)
        else:
            self.busy.stop()

    def is_busy(self) -> bool:
        return self.busy.isVisible()

    # --- helpers --------------------------------------------------------
    def refresh_icons(self) -> None:
        self.btn_back.setIcon(theme.icon("back", size=18))
        self.btn_fwd.setIcon(theme.icon("forward", size=18))
        self.btn_clip.setIcon(theme.icon("play_range", size=18))
        self._sync_play_icon()

    def _set_controls(self, on: bool) -> None:
        for w in (self.btn_play, self.btn_back, self.btn_fwd, self.slider, self.goto, self.btn_clip):
            w.setEnabled(on and (self.player is not None))
        # the slider/goto still work for timeline sync even w/o player; keep disabled without media

    def _time_text(self, t: float) -> str:
        return f"{format_timestamp(t)} / {format_timestamp(self.duration) if self.duration else '--:--'}"

    def set_frame_size(self, w: int, h: int) -> None:
        """Define the logical frame from the source aspect ratio (video item stretched to it)."""
        fh = FRAME_W * h / max(1, w)
        frame = QRectF(0, 0, FRAME_W, fh)
        self.scene.setSceneRect(frame)
        self.poster.setPos(0, 0)
        if self.video_item is not None:
            self.video_item.setPos(0, 0)
            self.video_item.setSize(QSizeF(FRAME_W, fh))
        self.overlay.set_frame(frame, (w, h))
        self.view.fitInView(frame, Qt.KeepAspectRatio)

    def set_poster(self, data: Optional[bytes]) -> None:
        pm = QPixmap()
        if data and pm.loadFromData(data):
            r = self.scene.sceneRect()
            self.poster.setPixmap(pm.scaled(int(r.width()), int(r.height()), Qt.IgnoreAspectRatio, Qt.SmoothTransformation))
        else:
            self.poster.setPixmap(QPixmap())

    def show_message(self, text: str, offer_fetch: bool = False) -> None:
        self.busy.stop()
        self.message.setText(text)
        self.btn_fetch.setVisible(offer_fetch)
        self.stack.setCurrentWidget(self.msg_widget)

    def show_video(self) -> None:
        self.msg_widget.hide() if False else None
        self.message.setText("")
        self.btn_fetch.setVisible(False)
        self.stack.setCurrentWidget(self.view)

    def set_duration_hint(self, d: float) -> None:
        self.duration = d
        self.time_label.setText(self._time_text(0))

    # --- media -------------------------------------------------------------
    def clear_media(self) -> None:
        self._has_media = False
        if self.player:
            self.player.stop()
            self.player.setSource(QUrl())
        self._set_controls(False)

    def set_source(self, url_or_path: str, is_file: bool) -> None:
        if not self.player:
            self.show_message("Video playback is not available (Qt Multimedia could not be loaded). "
                              "You can still select clips on the timeline and export them.")
            return
        self.show_message("Loading preview...")
        url = QUrl.fromLocalFile(url_or_path) if is_file else QUrl(url_or_path)
        self.player.setSource(url)
        self._has_media = True
        self._set_controls(True)
        self.player.pause()

    def has_media(self) -> bool:
        return self._has_media

    def position(self) -> float:
        return (self.player.position() / 1000.0) if self.player else 0.0

    def seek(self, t: float) -> None:
        if not self.player or not self._has_media:
            return
        t = max(0.0, min(t, self.duration or t))
        self.player.setPosition(int(t * 1000))
        self.seekedByUser.emit(t)

    def seek_silent(self, t: float) -> None:
        """Seek without emitting seekedByUser (used while dragging trim handles)."""
        if self.player and self._has_media:
            self.player.setPosition(int(max(0.0, t) * 1000))

    def toggle_play(self) -> None:
        if not self.player or not self._has_media:
            return
        self._play_end = None
        if self.player.playbackState() == QMediaPlayer.PlayingState:
            self.player.pause()
        else:
            self.player.play()

    def pause(self) -> None:
        if self.player:
            self.player.pause()

    def play_range(self, start: float, end: float) -> bool:
        if not self.player or not self._has_media:
            return False
        self._play_end = end
        self.player.setPosition(int(start * 1000))
        self.player.play()
        return True

    # --- player callbacks ------------------------------------------------------
    def _sync_play_icon(self) -> None:
        playing = bool(self.player) and self.player.playbackState() == QMediaPlayer.PlayingState
        self.btn_play.setIcon(theme.icon("pause" if playing else "play", size=18))

    def _on_position(self, ms: int) -> None:
        t = ms / 1000.0
        if self._play_end is not None and t >= self._play_end:
            self._play_end = None
            self.player.pause()
            self.clipPlaybackFinished.emit()
        if not self._dragging and self.duration:
            self.slider.setValue(int(min(1.0, t / self.duration) * 1000))
            self.time_label.setText(self._time_text(t))
        self.positionChanged.emit(t)

    def _on_duration(self, ms: int) -> None:
        if ms > 0 and not self.duration:
            self.duration = ms / 1000.0

    def _on_status(self, status) -> None:
        if status in (QMediaPlayer.LoadedMedia, QMediaPlayer.BufferedMedia, QMediaPlayer.BufferingMedia):
            self.show_video()

    def _on_error(self, _err, text: str) -> None:
        log.warning("Playback error: %s", text)
        self._has_media = False
        self._set_controls(False)
        self.show_message("The preview could not be streamed (" + (text or "unsupported source") +
                          ").\nYou can still pick clips on the timeline, or download a small preview file.",
                          offer_fetch=True)

    def _slider_released(self) -> None:
        self._dragging = False
        self.seek(self.slider.value() / 1000.0 * self.duration)

    def _goto(self) -> None:
        try:
            self.seek(parse_timestamp(self.goto.text()))
        except ValueError:
            self.goto.setStyleSheet("border: 1px solid #ff6b6b;")
            QTimer.singleShot(1200, lambda: self.goto.setStyleSheet(""))

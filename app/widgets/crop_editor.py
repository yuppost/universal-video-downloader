"""Crop overlay (drawn on the preview scene) and the crop control panel."""

from __future__ import annotations

from typing import Optional, Tuple

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (QDoubleSpinBox, QGraphicsObject, QHBoxLayout, QLabel, QPushButton,
                               QComboBox, QVBoxLayout, QWidget)

from app.models.crop import (ASPECT_PRESETS, CropSettings, center_rect, clamp_rect, compute_crop_rect,
                             resize_with_ratio, target_ratio)

HANDLE = 14.0


class CropOverlay(QGraphicsObject):
    """Dimmed mask + movable/resizable crop rectangle in *frame* coordinates.

    The frame is the full video picture (0,0,W,H in scene units). The rectangle is
    exposed to the outside as a normalised rect so it is resolution independent.
    """

    cropChanged = Signal(object)  # CropSettings

    def __init__(self) -> None:
        super().__init__()
        self.frame = QRectF(0, 0, 1280, 720)
        self.src_size: Tuple[int, int] = (1920, 1080)
        self.settings = CropSettings()
        self._mode: Optional[str] = None
        self._anchor = QPointF()
        self._grab_off = QPointF()
        self.setAcceptHoverEvents(True)
        self.setZValue(10)
        self.setVisible(False)

    # --- geometry --------------------------------------------------------
    def boundingRect(self) -> QRectF:  # noqa: N802
        return self.frame.adjusted(-HANDLE, -HANDLE, HANDLE, HANDLE)

    def set_frame(self, frame: QRectF, src_size: Tuple[int, int]) -> None:
        self.prepareGeometryChange()
        self.frame, self.src_size = frame, src_size
        self.update()

    def current_norm_rect(self) -> Optional[Tuple[float, float, float, float]]:
        s = self.settings
        if s.preset == "original" and s.rect is None:
            return None
        if s.rect is not None:
            return clamp_rect(s.rect)
        ratio = target_ratio(s, *self.src_size)
        if ratio is None:
            return (0.0, 0.0, 1.0, 1.0)
        return center_rect(ratio, *self.src_size)

    def _rect_scene(self) -> Optional[QRectF]:
        n = self.current_norm_rect()
        if n is None:
            return None
        f = self.frame
        return QRectF(f.left() + n[0] * f.width(), f.top() + n[1] * f.height(), n[2] * f.width(), n[3] * f.height())

    def load(self, settings: CropSettings) -> None:
        self.settings = settings.copy()
        self.setVisible(settings.preset != "original" or settings.rect is not None)
        self.update()

    def _norm(self, p: QPointF) -> Tuple[float, float]:
        f = self.frame
        return ((p.x() - f.left()) / f.width(), (p.y() - f.top()) / f.height())

    # --- painting -----------------------------------------------------------
    def paint(self, painter: QPainter, _opt, _w=None) -> None:
        r = self._rect_scene()
        if r is None:
            return
        painter.setRenderHint(QPainter.Antialiasing)
        mask = QPainterPath()
        mask.addRect(self.frame)
        hole = QPainterPath()
        hole.addRect(r)
        painter.fillPath(mask - hole, QColor(0, 0, 0, 150))
        painter.setPen(QPen(QColor("#e040fb"), 2))
        painter.setBrush(Qt.NoBrush)
        painter.drawRect(r)
        painter.setPen(QPen(QColor(255, 255, 255, 90), 1, Qt.DashLine))
        for i in (1, 2):  # rule-of-thirds guides
            painter.drawLine(QPointF(r.left() + r.width() * i / 3, r.top()), QPointF(r.left() + r.width() * i / 3, r.bottom()))
            painter.drawLine(QPointF(r.left(), r.top() + r.height() * i / 3), QPointF(r.right(), r.top() + r.height() * i / 3))
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor("#e040fb"))
        for c in self._corners(r).values():
            painter.drawRect(QRectF(c.x() - HANDLE / 2, c.y() - HANDLE / 2, HANDLE, HANDLE))
        cr = compute_crop_rect(self.settings, *self.src_size)
        if cr:
            painter.setPen(QColor("#ffffff"))
            painter.setBrush(QColor("#e040fb"))
            txt = f"{cr.w} x {cr.h}"
            painter.drawRect(QRectF(r.left() + 4, r.top() + 4, 9 * len(txt) + 10, 22))
            painter.drawText(QPointF(r.left() + 9, r.top() + 20), txt)

    @staticmethod
    def _corners(r: QRectF):
        return {"tl": r.topLeft(), "tr": r.topRight(), "bl": r.bottomLeft(), "br": r.bottomRight()}

    # --- interaction ----------------------------------------------------------
    def _hit(self, pos: QPointF) -> Optional[str]:
        r = self._rect_scene()
        if r is None:
            return None
        for k, c in self._corners(r).items():
            if abs(pos.x() - c.x()) <= HANDLE and abs(pos.y() - c.y()) <= HANDLE:
                return k
        return "move" if r.contains(pos) else None

    def hoverMoveEvent(self, e) -> None:  # noqa: N802
        h = self._hit(e.pos())
        self.setCursor({"tl": Qt.SizeFDiagCursor, "br": Qt.SizeFDiagCursor, "tr": Qt.SizeBDiagCursor,
                        "bl": Qt.SizeBDiagCursor, "move": Qt.SizeAllCursor}.get(h, Qt.ArrowCursor))

    def mousePressEvent(self, e) -> None:  # noqa: N802
        h = self._hit(e.pos())
        r = self._rect_scene()
        if h is None or r is None:
            e.ignore()
            return
        self._mode = h
        if h == "move":
            self._grab_off = e.pos() - r.topLeft()
        else:
            opposite = {"tl": r.bottomRight(), "tr": r.bottomLeft(), "bl": r.topRight(), "br": r.topLeft()}[h]
            self._anchor = opposite
        e.accept()

    def mouseMoveEvent(self, e) -> None:  # noqa: N802
        r = self._rect_scene()
        if self._mode is None or r is None:
            return
        n = self.current_norm_rect()
        assert n is not None
        if self._mode == "move":
            tl = e.pos() - self._grab_off
            x, y = self._norm(tl)
            new = clamp_rect((x, y, n[2], n[3]))
        else:
            ratio = target_ratio(self.settings, *self.src_size)
            new = resize_with_ratio(self._norm(self._anchor), self._norm(e.pos()), ratio, *self.src_size)
        self.settings.rect = new
        if self.settings.preset == "original":
            self.settings.preset = "free"
        self.prepareGeometryChange()
        self.update()
        self.cropChanged.emit(self.settings.copy())

    def mouseReleaseEvent(self, _e) -> None:  # noqa: N802
        self._mode = None


class CropControls(QWidget):
    """Aspect-ratio selector with custom ratio, center and reset buttons."""

    cropChanged = Signal(object)   # CropSettings
    applyToAll = Signal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._settings = CropSettings()
        self._loading = False
        self.combo = QComboBox()
        for key, (label, _r) in ASPECT_PRESETS.items():
            self.combo.addItem(label, key)
        self.combo.setToolTip("Crop shape for the selected clip. Cropping re-encodes the video.")
        self.rw, self.rh = QDoubleSpinBox(), QDoubleSpinBox()
        for s, v in ((self.rw, 4.0), (self.rh, 3.0)):
            s.setRange(0.1, 100.0)
            s.setDecimals(2)
            s.setValue(v)
            s.setToolTip("Custom aspect ratio (width : height)")
        self.btn_center = QPushButton("Center")
        self.btn_center.setToolTip("Re-center the crop rectangle")
        self.btn_reset = QPushButton("No crop")
        self.btn_reset.setToolTip("Remove cropping from this clip")
        self.btn_all = QPushButton("Apply to all")
        self.btn_all.setToolTip("Copy this crop to every clip in the list")
        self.info = QLabel("")
        self.info.setObjectName("muted")
        self.info.setWordWrap(True)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 8, 8, 8)
        outer.setSpacing(8)
        outer.addWidget(self.combo)
        ratio = QHBoxLayout()
        self.ratio_row = QWidget()
        self.ratio_row.setLayout(ratio)
        ratio.setContentsMargins(0, 0, 0, 0)
        ratio.addWidget(QLabel("Ratio"))
        ratio.addWidget(self.rw, 1)
        ratio.addWidget(QLabel(":"))
        ratio.addWidget(self.rh, 1)
        outer.addWidget(self.ratio_row)
        row = QHBoxLayout()
        row.addWidget(self.btn_center, 1)
        row.addWidget(self.btn_reset, 1)
        row.addWidget(self.btn_all, 1)
        outer.addLayout(row)
        outer.addWidget(self.info)
        self.combo.currentIndexChanged.connect(self._changed)
        self.rw.valueChanged.connect(self._changed)
        self.rh.valueChanged.connect(self._changed)
        self.btn_center.clicked.connect(self._center)
        self.btn_reset.clicked.connect(lambda: self.set_settings(CropSettings(), emit=True))
        self.btn_all.clicked.connect(self.applyToAll)
        self._sync_enabled()

    def _sync_enabled(self) -> None:
        custom = self.combo.currentData() == "custom"
        self.ratio_row.setVisible(custom)
        active = self.combo.currentData() != "original"
        self.btn_center.setEnabled(active)

    def set_settings(self, s: CropSettings, emit: bool = False) -> None:
        self._loading = True
        self._settings = s.copy()
        self.combo.setCurrentIndex(max(0, self.combo.findData(s.preset)))
        self.rw.setValue(s.custom_ratio[0])
        self.rh.setValue(s.custom_ratio[1])
        self._sync_enabled()
        self._loading = False
        if emit:
            self.cropChanged.emit(self._settings.copy())

    def set_info(self, text: str) -> None:
        self.info.setText(text)

    def _changed(self, *_a) -> None:
        if self._loading:
            return
        preset = self.combo.currentData()
        self._settings = CropSettings(preset, None, (self.rw.value(), self.rh.value()))
        self._sync_enabled()
        self.cropChanged.emit(self._settings.copy())

    def _center(self) -> None:
        s = self._settings.copy()
        s.rect = None
        self._settings = s
        self.cropChanged.emit(s.copy())

"""Application icon: a built-in default plus an optional user-chosen image."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QImage, QLinearGradient, QPainter, QPainterPath, QPixmap

ICON_EXTS = (".png", ".ico", ".jpg", ".jpeg", ".bmp", ".svg")
APP_ID = "UniversalVideoStudio.App.1"


def _draw(size: int) -> QPixmap:
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    g = QLinearGradient(0, 0, size, size)
    g.setColorAt(0, QColor("#6c8cff"))
    g.setColorAt(1, QColor("#8a6cff"))
    p.setBrush(g)
    p.setPen(Qt.NoPen)
    p.drawRoundedRect(QRectF(0, 0, size, size), size * 0.22, size * 0.22)
    tri = QPainterPath()
    tri.moveTo(size * 0.38, size * 0.28)
    tri.lineTo(size * 0.74, size * 0.50)
    tri.lineTo(size * 0.38, size * 0.72)
    tri.closeSubpath()
    p.fillPath(tri, QColor("white"))
    p.setBrush(QColor(255, 255, 255, 200))           # little "trim" bar under the triangle
    p.drawRoundedRect(QRectF(size * 0.2, size * 0.82, size * 0.6, size * 0.05), size * 0.025, size * 0.025)
    p.end()
    return pm


def default_icon() -> QIcon:
    ic = QIcon()
    for s in (16, 24, 32, 48, 64, 128, 256):
        ic.addPixmap(_draw(s))
    return ic


def load_icon(path: str) -> QIcon:
    """User icon if the file is a readable image, else the built-in default."""
    if path:
        p = Path(path)
        if p.is_file() and p.suffix.lower() in ICON_EXTS:
            ic = QIcon(str(p))
            if not ic.isNull() and not ic.pixmap(32, 32).isNull():
                return ic
    return default_icon()


def export_default_icons(folder: Path) -> None:
    """Write assets/icon.png and assets/icon.ico (used by build.bat for the .exe icon)."""
    folder.mkdir(parents=True, exist_ok=True)
    _draw(256).save(str(folder / "icon.png"))
    img = _draw(256).toImage()
    img.save(str(folder / "icon.ico"))


def set_windows_app_id() -> None:
    """Makes Windows group the taskbar button under our own icon instead of python.exe's."""
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
        except Exception:  # noqa: BLE001 - cosmetic only
            pass

"""Dark/light themes (Qt style sheets) and programmatically drawn vector icons."""

from __future__ import annotations

from typing import Callable, Dict

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import QApplication

PALETTES: Dict[str, Dict[str, str]] = {
    "dark": dict(bg="#090c12", panel="#10151f", panel2="#182032", border="#232c42", text="#e9edf6",
                 muted="#8792a8", accent="#2fc6ee", accent2="#5b7cff", accent_text="#04121a", danger="#ff6b7a",
                 ok="#3fe0a5", ruler="#0c1018", lane="#0b0f17", hover="#1f2940"),
    "light": dict(bg="#eef2f8", panel="#ffffff", panel2="#eaeef6", border="#d6dcea", text="#141a26",
                  muted="#647089", accent="#1099c9", accent2="#4f6df5", accent_text="#ffffff", danger="#d92d4a",
                  ok="#13976a", ruler="#e4e9f3", lane="#f3f6fb", hover="#dfe6f3"),
}
_current = "dark"


def palette() -> Dict[str, str]:
    return PALETTES[_current]


def stylesheet(name: str) -> str:
    p = PALETTES[name]
    return f"""
    * {{ font-family: "Segoe UI Variable Text", "Segoe UI", "Inter", "Noto Sans", sans-serif; }}
    QWidget {{ background: {p['bg']}; color: {p['text']}; font-size: 13px; }}
    QMainWindow, QDialog {{ background: {p['bg']}; }}
    QFrame#card, QGroupBox {{ background: {p['panel']}; border: 1px solid {p['border']}; border-radius: 14px; }}
    QGroupBox {{ margin-top: 14px; padding: 8px; }}
    QGroupBox::title {{ subcontrol-origin: margin; left: 12px; padding: 0 4px; color: {p['muted']}; }}
    QLabel {{ background: transparent; }}
    QLabel#muted {{ color: {p['muted']}; }}
    QLabel#title {{ font-size: 15px; font-weight: 600; }}
    QLabel#brand {{ font-size: 17px; font-weight: 700; letter-spacing: 0.3px; }}
    QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QPlainTextEdit {{
        background: {p['panel2']}; border: 1px solid {p['border']}; border-radius: 9px; padding: 7px 11px;
        selection-background-color: {p['accent']}; selection-color: {p['accent_text']}; }}
    QLineEdit:hover, QComboBox:hover, QSpinBox:hover {{ border-color: {p['muted']}; }}
    QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus {{ border: 1px solid {p['accent']}; }}
    QLineEdit:disabled, QComboBox:disabled {{ color: {p['muted']}; }}
    QComboBox::drop-down {{ border: none; width: 24px; }}
    QComboBox QAbstractItemView {{ background: {p['panel']}; selection-background-color: {p['accent']};
        selection-color: {p['accent_text']}; border: 1px solid {p['border']}; border-radius: 8px; padding: 4px; outline: 0; }}
    QPushButton, QToolButton {{ background: {p['panel2']}; border: 1px solid transparent; border-radius: 9px;
        padding: 8px 14px; }}
    QPushButton:hover, QToolButton:hover {{ background: {p['hover']}; border-color: {p['border']}; }}
    QPushButton:pressed, QToolButton:pressed {{ background: {p['border']}; }}
    QPushButton:disabled, QToolButton:disabled {{ color: {p['muted']}; background: {p['panel']}; }}
    QPushButton:focus, QToolButton:focus {{ border: 1px solid {p['accent']}; }}
    QPushButton:checked {{ background: {p['accent']}; color: {p['accent_text']}; }}
    QPushButton#primary {{ color: {p['accent_text']}; font-weight: 600; border: none;
        background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 {p['accent']}, stop:1 {p['accent2']}); }}
    QPushButton#primary:hover {{ background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 {p['accent2']}, stop:1 {p['accent']}); }}
    QPushButton#primary:disabled {{ background: {p['panel2']}; color: {p['muted']}; }}
    QPushButton#danger {{ color: {p['danger']}; }}
    QPushButton#bigPrimary {{ color: {p['accent_text']}; font-weight: 700; font-size: 14px; border: none;
        border-radius: 12px; min-height: 28px; padding: 10px 16px;
        background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 {p['accent']}, stop:1 {p['accent2']}); }}
    QPushButton#bigPrimary:hover {{ background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 {p['accent2']}, stop:1 {p['accent']}); }}
    QPushButton#bigPrimary:disabled {{ background: {p['panel2']}; color: {p['muted']}; }}
    QPushButton#bigSecondary {{ font-weight: 600; font-size: 14px; border-radius: 12px; min-height: 28px; padding: 10px 16px;
        background: {p['panel2']}; border: 1px solid {p['border']}; }}
    QPushButton#bigSecondary:hover {{ border-color: {p['accent']}; }}
    QPushButton#bigSecondary:disabled {{ color: {p['muted']}; background: {p['panel']}; }}
    QFrame#urlbar {{ background: {p['panel2']}; border: 1px solid {p['border']}; border-radius: 14px; }}
    QFrame#urlbar:hover {{ border-color: {p['accent']}; }}
    QFrame#urlbar QLineEdit {{ background: transparent; border: none; padding: 6px 8px; }}
    QFrame#urlbar QPushButton {{ background: {p['hover']}; border: 1px solid {p['border']}; border-radius: 9px; padding: 6px 12px; }}
    QFrame#urlbar QPushButton:hover {{ border-color: {p['accent']}; }}
    QFrame#subcard {{ background: {p['panel2']}; border: 1px solid {p['border']}; border-radius: 10px; }}
    QFrame#subcard QLabel {{ background: transparent; }}
    QLabel#panelTitle {{ font-size: 14px; font-weight: 700; padding: 2px 4px; }}
    QLabel#key {{ color: {p['muted']}; font-size: 12px; }}
    QToolButton#sectionHeader {{ background: transparent; border: none; border-bottom: 1px solid {p['border']};
        border-radius: 0; padding: 9px 6px; font-weight: 600; text-align: left; }}
    QToolButton#sectionHeader:hover {{ background: {p['hover']}; }}
    QPushButton#signin {{ background: transparent; border: none; color: {p['muted']}; padding: 2px 8px; }}
    QPushButton#signin:hover {{ color: {p['accent']}; }}
    QTableWidget, QListWidget, QTreeWidget {{ background: {p['panel']}; border: 1px solid {p['border']};
        border-radius: 12px; gridline-color: transparent; alternate-background-color: {p['panel']};
        selection-background-color: {p['accent']}; selection-color: {p['accent_text']}; outline: 0; }}
    QTableWidget::item {{ padding: 6px 8px; border-bottom: 1px solid {p['panel2']}; }}
    QTableWidget::item:hover {{ background: {p['hover']}; }}
    QTableWidget::item:selected {{ background: {p['accent']}; color: {p['accent_text']}; }}
    QHeaderView::section {{ background: {p['panel']}; color: {p['muted']}; border: none;
        border-bottom: 1px solid {p['border']}; padding: 8px; font-size: 11px; font-weight: 600; }}
    QTabWidget::pane {{ border: 1px solid {p['border']}; border-radius: 12px; top: -1px; background: {p['panel']}; }}
    QTabBar::tab {{ background: transparent; color: {p['muted']}; padding: 9px 16px; border: none;
        border-bottom: 2px solid transparent; margin-right: 4px; font-weight: 600; }}
    QTabBar::tab:hover {{ color: {p['text']}; }}
    QTabWidget#studioTabs::pane {{ border: none; background: transparent; top: 0px; }}
    QTabWidget#studioTabs QTabBar::tab {{ background: {p['panel']}; color: {p['muted']}; padding: 8px 12px 8px 16px;
        margin: 4px 4px 0 0; border: 1px solid {p['border']}; border-bottom: none; border-radius: 10px 10px 0 0;
        min-width: 80px; max-width: 230px; }}
    QTabWidget#studioTabs QTabBar::tab:selected {{ background: {p['panel2']}; color: {p['text']};
        border-top: 2px solid {p['accent']}; margin-top: 2px; padding-top: 9px; }}
    QTabWidget#studioTabs QTabBar::tab:hover {{ color: {p['text']}; }}
    QTabWidget#studioTabs QToolButton {{ background: transparent; border: none; border-radius: 6px; padding: 2px; }}
    QTabWidget#studioTabs QToolButton:hover {{ background: {p['hover']}; }}
    QTabBar::tab:selected {{ color: {p['accent']}; border-bottom: 2px solid {p['accent']}; }}
    QProgressBar {{ background: {p['panel2']}; border: none; border-radius: 8px; text-align: center;
        height: 16px; font-size: 11px; }}
    QProgressBar::chunk {{ border-radius: 8px;
        background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 {p['accent']}, stop:1 {p['accent2']}); }}
    QSlider::groove:horizontal {{ height: 5px; background: {p['panel2']}; border-radius: 2px; }}
    QSlider::sub-page:horizontal {{ background: {p['accent']}; border-radius: 2px; }}
    QSlider::handle:horizontal {{ background: #ffffff; width: 14px; height: 14px; margin: -5px 0; border-radius: 7px;
        border: 2px solid {p['accent']}; }}
    QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 0; }}
    QScrollBar::handle:horizontal {{ background: {p['border']}; border-radius: 5px; min-width: 36px; }}
    QScrollBar::handle:horizontal:hover {{ background: {p['muted']}; }}
    QScrollBar:vertical {{ background: transparent; width: 10px; margin: 0; }}
    QScrollBar::handle:vertical {{ background: {p['border']}; border-radius: 5px; min-height: 36px; }}
    QScrollBar::handle:vertical:hover {{ background: {p['muted']}; }}
    QScrollBar::add-line, QScrollBar::sub-line, QScrollBar::add-page, QScrollBar::sub-page {{ background: none; width: 0; height: 0; }}
    QSplitter::handle {{ background: transparent; }}
    QSplitter::handle:horizontal {{ width: 8px; }}
    QSplitter::handle:vertical {{ height: 8px; }}
    QToolTip {{ background: {p['panel2']}; color: {p['text']}; border: 1px solid {p['border']}; padding: 6px;
        border-radius: 6px; }}
    QMenu {{ background: {p['panel']}; border: 1px solid {p['border']}; border-radius: 10px; padding: 6px; }}
    QMenu::item {{ padding: 8px 24px; border-radius: 6px; }}
    QMenu::item:selected {{ background: {p['accent']}; color: {p['accent_text']}; }}
    QMenu::separator {{ height: 1px; background: {p['border']}; margin: 5px 8px; }}
    QStatusBar {{ background: {p['bg']}; color: {p['muted']}; }}
    QCheckBox::indicator {{ width: 18px; height: 18px; border-radius: 5px; border: 1px solid {p['border']};
        background: {p['panel2']}; }}
    QCheckBox::indicator:checked {{ background: {p['accent']}; border-color: {p['accent']}; }}
    """


def apply_theme(app: QApplication, name: str) -> None:
    global _current
    _current = name if name in PALETTES else "dark"
    app.setStyle("Fusion")
    app.setStyleSheet(stylesheet(_current))


# --- icons --------------------------------------------------------------------
def _poly(pts):
    path = QPainterPath(QPointF(*pts[0]))
    for p in pts[1:]:
        path.lineTo(QPointF(*p))
    path.closeSubpath()
    return path


def _draw_play(p, c):  p.fillPath(_poly([(7, 4), (20, 12), (7, 20)]), c)
def _draw_pause(p, c): p.fillRect(QRectF(6, 4, 4, 16), c); p.fillRect(QRectF(14, 4, 4, 16), c)
def _draw_back(p, c):  p.fillPath(_poly([(12, 5), (3, 12), (12, 19)]), c); p.fillPath(_poly([(21, 5), (12, 12), (21, 19)]), c)
def _draw_fwd(p, c):   p.fillPath(_poly([(12, 5), (21, 12), (12, 19)]), c); p.fillPath(_poly([(3, 5), (12, 12), (3, 19)]), c)
def _draw_plus(p, c):  p.fillRect(QRectF(10.5, 4, 3, 16), c); p.fillRect(QRectF(4, 10.5, 16, 3), c)
def _draw_trash(p, c):
    p.fillRect(QRectF(5, 6, 14, 2), c); p.fillRect(QRectF(9, 3, 6, 3), c)
    p.setPen(QPen(c, 2)); p.drawRect(QRectF(7, 9, 10, 11))
def _draw_copy(p, c):
    p.setPen(QPen(c, 2)); p.drawRect(QRectF(8, 8, 11, 12)); p.drawRect(QRectF(4, 4, 11, 12))
def _draw_folder(p, c):
    p.fillPath(_poly([(3, 7), (10, 7), (12, 9), (21, 9), (21, 19), (3, 19)]), c)
def _draw_gear(p, c):
    p.setPen(QPen(c, 2.4)); p.drawEllipse(QPointF(12, 12), 6, 6)
    for a in range(0, 360, 45):
        p.save(); p.translate(12, 12); p.rotate(a); p.fillRect(QRectF(-1.5, -10, 3, 4), c); p.restore()
def _draw_retry(p, c):
    p.setPen(QPen(c, 2.4)); p.drawArc(QRectF(5, 5, 14, 14), 40 * 16, 280 * 16)
    p.fillPath(_poly([(17, 2), (21, 9), (13, 8)]), c)
def _draw_x(p, c):
    p.setPen(QPen(c, 2.6)); p.drawLine(6, 6, 18, 18); p.drawLine(18, 6, 6, 18)
def _draw_scissors(p, c):
    p.setPen(QPen(c, 2)); p.drawEllipse(QPointF(7, 17), 3, 3); p.drawEllipse(QPointF(17, 17), 3, 3)
    p.drawLine(9, 15, 18, 4); p.drawLine(15, 15, 6, 4)
def _draw_clipboard(p, c):
    p.setPen(QPen(c, 2)); p.drawRect(QRectF(6, 5, 12, 15)); p.fillRect(QRectF(9, 3, 6, 4), c)
def _draw_download(p, c):
    p.fillRect(QRectF(10.5, 3, 3, 10), c); p.fillPath(_poly([(5, 12), (19, 12), (12, 20)]), c)
    p.fillRect(QRectF(4, 20, 16, 2), c)
def _draw_up(p, c):    p.fillPath(_poly([(12, 5), (20, 17), (4, 17)]), c)
def _draw_down(p, c):  p.fillPath(_poly([(12, 19), (20, 7), (4, 7)]), c)
def _draw_zoom_in(p, c):
    p.setPen(QPen(c, 2)); p.drawEllipse(QPointF(10, 10), 6, 6); p.drawLine(15, 15, 21, 21)
    p.drawLine(10, 7, 10, 13); p.drawLine(7, 10, 13, 10)
def _draw_zoom_out(p, c):
    p.setPen(QPen(c, 2)); p.drawEllipse(QPointF(10, 10), 6, 6); p.drawLine(15, 15, 21, 21); p.drawLine(7, 10, 13, 10)
def _draw_fit(p, c):
    p.setPen(QPen(c, 2)); p.drawLine(4, 12, 20, 12)
    p.fillPath(_poly([(4, 12), (9, 8), (9, 16)]), c); p.fillPath(_poly([(20, 12), (15, 8), (15, 16)]), c)
def _draw_reset(p, c):
    p.setPen(QPen(c, 2.4)); p.drawArc(QRectF(5, 5, 14, 14), 90 * 16, 270 * 16)
    p.fillPath(_poly([(12, 1), (12, 9), (6, 5)]), c)
def _draw_play_range(p, c):
    p.fillPath(_poly([(6, 5), (16, 12), (6, 19)]), c); p.fillRect(QRectF(18, 5, 3, 14), c)


_DRAW: Dict[str, Callable] = {
    "play": _draw_play, "pause": _draw_pause, "back": _draw_back, "forward": _draw_fwd,
    "add": _draw_plus, "delete": _draw_trash, "duplicate": _draw_copy, "folder": _draw_folder,
    "settings": _draw_gear, "retry": _draw_retry, "cancel": _draw_x, "scissors": _draw_scissors,
    "paste": _draw_clipboard, "download": _draw_download, "up": _draw_up, "down": _draw_down,
    "zoom_in": _draw_zoom_in, "zoom_out": _draw_zoom_out, "fit": _draw_fit, "reset": _draw_reset,
    "play_range": _draw_play_range,
}


def icon(name: str, color: str | None = None, size: int = 24) -> QIcon:
    """Render a crisp (HiDPI) vector icon in the current theme's text colour."""
    col = QColor(color or palette()["text"])
    scale = 2
    pm = QPixmap(size * scale, size * scale)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.scale(size * scale / 24, size * scale / 24)
    _DRAW[name](p, col)
    p.end()
    pm.setDevicePixelRatio(scale)
    ic = QIcon(pm)
    dis = QPixmap(pm)
    dp = QPainter(dis)
    dp.setCompositionMode(QPainter.CompositionMode_SourceIn)
    dp.fillRect(dis.rect(), QColor(palette()["muted"]))
    dp.end()
    ic.addPixmap(dis, QIcon.Disabled)
    return ic

"""Create assets/splash.png (shown by the PyInstaller bootloader the instant the .exe is started)."""
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from PySide6.QtGui import QColor, QFont, QGuiApplication, QPainter, QPixmap  # noqa: E402

app = QGuiApplication([])
from app import APP_NAME, appicon  # noqa: E402

pm = QPixmap(480, 210)
pm.fill(QColor("#0f1421"))
p = QPainter(pm)
p.setRenderHint(QPainter.Antialiasing)
p.drawPixmap(28, 30, appicon.load_icon("").pixmap(64, 64))
p.setPen(QColor("#e8ecf6"))
f = QFont("Segoe UI")
f.setPixelSize(24)
f.setBold(True)
p.setFont(f)
p.drawText(112, 66, APP_NAME)
f.setPixelSize(13)
f.setBold(False)
p.setFont(f)
p.setPen(QColor("#8b95ad"))
p.drawText(112, 92, "Starting...")
p.fillRect(28, 176, 424, 4, QColor("#1c2438"))
p.fillRect(28, 176, 150, 4, QColor("#35c8f0"))
p.end()
out = ROOT / "assets" / "splash.png"
pm.save(str(out), "PNG")
print("wrote", out)

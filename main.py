"""Universal Video Studio - entry point.

Run:   python main.py            (start the app)
       python main.py --check    (print environment diagnostics and exit)
"""

from __future__ import annotations

import sys


def diagnostics() -> int:
    import platform
    from app import __version__
    from app.config import load_settings
    from app.services import ytdlp_service as yd
    from app.services.ffmpeg_service import ffmpeg_version, find_executable
    from app.utils.paths import app_data_dir

    s = load_settings()
    print(f"Universal Video Studio {__version__}")
    print(f"Python       : {platform.python_version()} ({sys.executable})")
    print(f"Platform     : {platform.platform()}")
    try:
        import PySide6
        print(f"PySide6      : {PySide6.__version__}")
    except ImportError:
        print("PySide6      : NOT INSTALLED (pip install -r requirements.txt)")
    print(f"yt-dlp       : {yd.ytdlp_version()}")
    for name in ("ffmpeg", "ffprobe"):
        p = find_executable(name, s.ffmpeg_path)
        print(f"{name:<13}: {p or 'NOT FOUND'}" + (f"  [{ffmpeg_version(p)}]" if p and name == "ffmpeg" else ""))
    print(f"Data folder  : {app_data_dir()}")
    print(f"Download dir : {s.download_dir}")
    print(f"Temp dir     : {s.temp_dir}")
    return 0


def main() -> int:
    if "--check" in sys.argv:
        return diagnostics()
    if "--probe" in sys.argv:
        from app.services.probe import run_probe
        i = sys.argv.index("--probe")
        print(run_probe(sys.argv[i + 1] if i + 1 < len(sys.argv) else ""))
        return 0
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QColor, QFont, QPainter, QPixmap
    from PySide6.QtWidgets import QApplication, QSplashScreen

    import os
    pyi_splash = None
    if os.environ.get("_PYI_SPLASH_IPC"):    # set by the exe bootloader only when it really shows a splash (--splash)
        try:
            import pyi_splash
        except Exception:                    # noqa: BLE001 - a splash problem must never stop the app
            pyi_splash = None
    app = QApplication(sys.argv)
    from app import APP_NAME, appicon
    app.setApplicationName(APP_NAME)
    if sys.platform.startswith("linux"):
        app.setDesktopFileName("UniversalVideoStudio")      # matches the .desktop file: right icon/name in the dock
    app.setOrganizationName("UniversalVideoStudio")
    # Show something at once: loading the rest (Qt multimedia, yt-dlp, ...) takes a few seconds on Windows.
    pm = QPixmap(420, 190)
    pm.fill(QColor("#0f1421"))
    pt = QPainter(pm)
    try:
        icon = appicon.load_icon("").pixmap(56, 56)
        pt.drawPixmap(24, 26, icon)
    except Exception:  # noqa: BLE001
        pass
    pt.setPen(QColor("#e8ecf6"))
    f = QFont()
    f.setPointSize(15)
    f.setBold(True)
    pt.setFont(f)
    pt.drawText(100, 62, APP_NAME)
    f.setPointSize(9)
    f.setBold(False)
    pt.setFont(f)
    pt.setPen(QColor("#8b95ad"))
    pt.drawText(100, 88, "Starting...")
    pt.end()
    splash = QSplashScreen(pm, Qt.WindowStaysOnTopHint)
    if pyi_splash is None:
        splash.show()
        app.processEvents()

    from app import theme
    from app.config import load_settings
    from app.main_window import MainWindow
    from app.utils.logging_config import setup_logging
    from app.utils.paths import app_data_dir

    log = setup_logging(app_data_dir() / "logs")
    appicon.set_windows_app_id()
    settings = load_settings()
    theme.apply_theme(app, settings.theme)
    app.setWindowIcon(appicon.load_icon(settings.icon_path))
    from app.widgets.first_run_setup import FirstRunSetup, needs_setup
    if needs_setup(settings):                      # FFmpeg / Deno are downloaded here once, then reused
        if pyi_splash is not None:
            pyi_splash.close()
        splash.close()
        dlg = FirstRunSetup(settings)
        dlg.exec()
        if dlg.skipped:
            from app.config import save_settings
            save_settings(settings)
    win = MainWindow(settings)
    win.show()
    splash.finish(win)
    if pyi_splash is not None:
        try:
            pyi_splash.close()
        except Exception:  # noqa: BLE001 - already closed
            pass
    log.info("Application started")
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())

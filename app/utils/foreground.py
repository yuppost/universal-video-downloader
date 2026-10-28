"""Bring a window to the front even when Windows would only flash its taskbar button.

Windows refuses ``SetForegroundWindow`` for a process that is not in the foreground. The usual remedies are used:
restore if minimized, a synthetic Alt key press (counts as user input) and a brief "topmost" toggle.
"""

from __future__ import annotations

import sys


def bring_to_front(window) -> None:
    from PySide6.QtCore import Qt
    if window.isMinimized():
        window.setWindowState(window.windowState() & ~Qt.WindowMinimized | Qt.WindowActive)
    window.show()
    window.raise_()
    window.activateWindow()
    if sys.platform != "win32":
        return
    try:
        import ctypes
        user32 = ctypes.windll.user32
        hwnd = int(window.winId())
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, 9)                       # SW_RESTORE
        user32.keybd_event(0x12, 0, 0, 0)                     # Alt down / up: lets us take the foreground
        user32.keybd_event(0x12, 0, 2, 0)
        HWND_TOPMOST, HWND_NOTOPMOST, FLAGS = -1, -2, 0x0001 | 0x0002 | 0x0040   # NOSIZE | NOMOVE | SHOWWINDOW
        user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, FLAGS)
        user32.SetWindowPos(hwnd, HWND_NOTOPMOST, 0, 0, 0, 0, FLAGS)
        user32.SetForegroundWindow(hwnd)
    except Exception:  # noqa: BLE001 - never break link handling over window focus
        pass

"""Native Messaging host for the Universal Video Studio browser extension (built as uvs-native-host.exe).

The browser starts this program, sends it ONE JSON message ``{"url": "https://..."}`` on stdin and reads the answer on
stdout. The host passes the link to the running app, or starts the app (next to this exe) with the link.
Standard library only, so the exe is small and starts instantly.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

from app.services import link_protocol as lp

APP_EXE = "UniversalVideoStudio.exe"


def _stdio():
    # Windows opens stdin/stdout in text mode: binary mode is required for the length-prefixed protocol.
    if sys.platform == "win32":
        import msvcrt
        msvcrt.setmode(sys.stdin.fileno(), os.O_BINARY)
        msvcrt.setmode(sys.stdout.fileno(), os.O_BINARY)
    return sys.stdin.buffer, sys.stdout.buffer


def start_app(url: str) -> bool:
    here = os.path.dirname(os.path.abspath(sys.executable if getattr(sys, "frozen", False) else __file__))
    exe = os.path.join(here, APP_EXE)
    if not os.path.isfile(exe):
        return False
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    subprocess.Popen([exe, "--open-url", url], cwd=here, creationflags=flags, close_fds=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return True


def allow_foreground() -> None:
    """The browser (foreground) started this process: let the app we are about to contact take the foreground."""
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.user32.AllowSetForegroundWindow(-1)        # ASFW_ANY
        except Exception:  # noqa: BLE001
            pass


def handle(msg: dict) -> dict:
    if msg.get("type") == "ping":
        return {"ok": True, "pong": True}
    url = lp.clean_url(msg.get("url"))
    if not url:
        return {"ok": False, "error": "invalid-url"}
    allow_foreground()
    if lp.send_to_running_app(url):
        return {"ok": True, "started": False}
    if start_app(url):
        return {"ok": True, "started": True}
    return {"ok": False, "error": "app-not-found"}


def main() -> int:
    stdin, stdout = _stdio()
    msg = lp.read_native_message(stdin)
    if msg is None:
        return 1
    try:
        reply = handle(msg)
    except Exception as exc:  # noqa: BLE001 - always answer, the extension shows the reason
        reply = {"ok": False, "error": f"{type(exc).__name__}"}
    lp.write_native_message(stdout, reply)
    time.sleep(0.05)
    return 0


if __name__ == "__main__":
    sys.exit(main())

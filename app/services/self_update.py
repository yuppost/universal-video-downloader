"""One-click update of the packaged (portable) app.

Steps: download the release zip -> verify (SHA-256 when known) -> unpack to a temp folder -> start a tiny helper script
that waits until this app has exited, copies the new files over the app folder (``data`` and ``portable.flag`` are never
touched) and starts the app again. User data (settings, cookies, history) lives outside the app folder, so it survives.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import zipfile
from pathlib import Path
from typing import Callable, Optional

from app.services.errors import FriendlyError, OperationCanceled

EXE_NAME = "UniversalVideoStudio.exe"
Progress = Callable[[Optional[float], str, Optional[float]], None]


def app_dir() -> Path:
    return Path(sys.executable).parent


def work_dir() -> Path:
    return Path(tempfile.gettempdir()) / "UVS-update"


def can_self_update() -> bool:
    """Only the packaged Windows app whose folder is writable (not C:\\Program Files) can replace itself."""
    if os.name != "nt" or not getattr(sys, "frozen", False):
        return False
    try:
        probe = app_dir() / ".write_test"
        probe.write_text("x")
        probe.unlink()
        return (app_dir() / EXE_NAME).is_file()
    except OSError:
        return False


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def safe_extract(zip_path: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    base = dest.resolve()
    with zipfile.ZipFile(zip_path) as z:
        for info in z.infolist():
            target = (dest / info.filename).resolve()
            if base != target and base not in target.parents:
                raise FriendlyError("The update archive contains an unsafe path and was rejected.")
        z.extractall(dest)


def find_root(extracted: Path) -> Path:
    """Folder that holds the exe (the zip has one top folder, but accept the exe at the top as well)."""
    if (extracted / EXE_NAME).is_file():
        return extracted
    for d in sorted(p for p in extracted.iterdir() if p.is_dir()):
        if (d / EXE_NAME).is_file():
            return d
    raise FriendlyError(f"The update archive does not contain {EXE_NAME}.")


def prepare(asset_url: str, sha256: str, progress: Progress, cancel: threading.Event,
            work: Optional[Path] = None, allow_http: bool = False) -> Path:
    """Download + verify + unpack. Returns the folder with the new app files."""
    from app.services import dependency_service as deps
    work = work or work_dir()
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)
    z = work / "update.zip"
    deps.download_file(asset_url, z, progress, cancel, allow_http, "Downloading update")
    progress(None, "Verifying download...", None)
    if sha256 and _sha256(z) != sha256.lower():
        raise FriendlyError("The downloaded update failed its integrity check (SHA-256 mismatch). Nothing was changed.")
    if cancel.is_set():
        raise OperationCanceled()
    progress(None, "Unpacking...", None)
    try:
        safe_extract(z, work / "new")
    except zipfile.BadZipFile as exc:
        raise FriendlyError("The downloaded update is not a valid zip file.") from exc
    root = find_root(work / "new")
    z.unlink(missing_ok=True)
    progress(100.0, "Ready to install", None)
    return root


def build_script() -> str:
    """The helper (Windows cmd). Arguments: PID, app folder, new folder, exe path, log file. No trailing backslashes."""
    return "\r\n".join([
        "@echo off",
        "setlocal",
        'set "PID=%~1"',
        'set "APP=%~2"',
        'set "NEW=%~3"',
        'set "EXE=%~4"',
        'set "LOG=%~5"',
        'set "CHK=%~dp5pid_check.txt"',
        'echo [update] waiting for the app to exit (pid %PID%) > "%LOG%"',
        "set /a W=0",
        ":wait",
        'tasklist /FI "PID eq %PID%" /NH > "%CHK%" 2>nul',
        'find "%PID%" "%CHK%" >nul 2>&1',
        "if errorlevel 1 goto copy",
        "set /a W+=1",
        "if %W% GEQ 90 goto copy",
        "ping -n 2 127.0.0.1 >nul",
        "goto wait",
        ":copy",
        "set /a N=0",
        ":again",
        'robocopy "%NEW%" "%APP%" /MIR /XD "%APP%\\data" /XF portable.flag /R:5 /W:2 /NFL /NDL /NJH /NJS /NP >> "%LOG%" 2>&1',
        "if errorlevel 8 (",
        "  set /a N+=1",
        "  if %N% LSS 5 (",
        "    ping -n 4 127.0.0.1 >nul",
        "    goto again",
        "  )",
        '  echo [update] FAILED: files could not be replaced >> "%LOG%"',
        "  goto launch",
        ")",
        'echo [update] files replaced >> "%LOG%"',
        ":launch",
        'start "" "%EXE%"',
        "endlocal",
        "exit /b 0",
        "",
    ])


def launch_helper(new_dir: Path) -> None:
    """Start the helper detached from this process. The caller must quit the app right afterwards."""
    work = work_dir()
    work.mkdir(parents=True, exist_ok=True)
    script = work / "apply_update.cmd"
    script.write_bytes(build_script().encode("ascii"))
    # CREATE_NO_WINDOW (not DETACHED_PROCESS): the helper gets one hidden console that tasklist/robocopy/ping inherit,
    # so no black terminal windows pop up while the update is applied.
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    startup = None
    if os.name == "nt":
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = 0                                  # SW_HIDE
    args = ["cmd.exe", "/c", str(script), str(os.getpid()), str(app_dir()).rstrip("\\/"), str(new_dir).rstrip("\\/"),
            str(app_dir() / EXE_NAME), str(work / "update.log")]
    subprocess.Popen(args, creationflags=flags, startupinfo=startup, close_fds=True, stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

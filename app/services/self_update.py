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
import tarfile
import tempfile
import threading
import zipfile
from pathlib import Path
from typing import Callable, Optional

from app.services.errors import FriendlyError, OperationCanceled

EXE_NAME = "UniversalVideoStudio.exe" if os.name == "nt" else "UniversalVideoStudio"
Progress = Callable[[Optional[float], str, Optional[float]], None]


def app_dir() -> Path:
    return Path(sys.executable).parent


def work_dir() -> Path:
    return Path(tempfile.gettempdir()) / "UVS-update"


def can_self_update() -> bool:
    """Only the packaged app (Windows / Linux) whose folder is writable (not C:\\Program Files, /opt) can replace itself."""
    if not (os.name == "nt" or sys.platform.startswith("linux")) or not getattr(sys, "frozen", False):
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
    """Unpack a .zip or .tar.gz update, rejecting any path that would leave *dest*."""
    dest.mkdir(parents=True, exist_ok=True)
    base = dest.resolve()
    if str(zip_path).lower().endswith((".tar.gz", ".tgz")):
        with tarfile.open(zip_path, "r:gz") as t:
            for m in t.getmembers():
                target = (dest / m.name).resolve()
                if base != target and base not in target.parents:
                    raise FriendlyError("The update archive contains an unsafe path and was rejected.")
                if m.issym() or m.islnk():                       # links may point anywhere: allow only inside dest
                    link = (dest / Path(m.name).parent / m.linkname).resolve() if m.issym() else (dest / m.linkname).resolve()
                    if base != link and base not in link.parents:
                        raise FriendlyError("The update archive contains an unsafe link and was rejected.")
            try:
                t.extractall(dest, filter="data")
            except TypeError:                                    # Python without extraction filters
                t.extractall(dest)
        return
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
    z = work / ("update.tar.gz" if asset_url.lower().endswith((".tar.gz", ".tgz")) else "update.zip")
    deps.download_file(asset_url, z, progress, cancel, allow_http, "Downloading update")
    progress(None, "Verifying download...", None)
    if sha256 and _sha256(z) != sha256.lower():
        raise FriendlyError("The downloaded update failed its integrity check (SHA-256 mismatch). Nothing was changed.")
    if cancel.is_set():
        raise OperationCanceled()
    progress(None, "Unpacking...", None)
    try:
        safe_extract(z, work / "new")
    except (zipfile.BadZipFile, tarfile.TarError, EOFError) as exc:
        raise FriendlyError("The downloaded update is not a valid archive.") from exc
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
        'set "EXENAME=%~nx4"',
        'set "CHK=%~dp5pid_check.txt"',
        'echo [update] waiting for the app to exit (pid %PID%) > "%LOG%"',
        "set /a W=0",
        ":wait",
        'tasklist /FI "PID eq %PID%" /NH > "%CHK%" 2>nul',
        'find "%PID%" "%CHK%" >nul 2>&1',
        "if errorlevel 1 goto copy",
        "set /a W+=1",
        "if %W% GEQ 40 goto force",
        "ping -n 2 127.0.0.1 >nul",
        "goto wait",
        ":force",
        # the old app did not exit by itself: end it, otherwise its locked files can never be replaced
        'echo [update] the app did not exit - ending it >> "%LOG%"',
        'taskkill /F /PID %PID% >nul 2>&1',
        "ping -n 3 127.0.0.1 >nul",
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
        'echo [update] starting %EXE% >> "%LOG%"',
        'start "" /D "%APP%" "%EXE%"',
        # make sure the new app is really running; if not, start it once more through Explorer (clean environment)
        "ping -n 9 127.0.0.1 >nul",
        'tasklist /FI "IMAGENAME eq %EXENAME%" /NH > "%CHK%" 2>nul',
        'find /I "%EXENAME%" "%CHK%" >nul 2>&1',
        "if errorlevel 1 (",
        '  echo [update] the app is not running - starting it again via Explorer >> "%LOG%"',
        '  explorer.exe "%EXE%"',
        ")",
        'echo [update] done >> "%LOG%"',
        "endlocal",
        "exit /b 0",
        "",
    ])


def clean_env() -> dict:
    """Environment for the helper and, through it, the new app.

    A PyInstaller app that starts *itself* hands its internal variables to the child, which then believes it is
    a worker of the old process (and can silently fail to start after its files were replaced). They are removed,
    and PyInstaller is told to treat the new process as a fresh one. On Linux the library search path that the
    bootloader set for the old app is put back to what the user had (it points into the old, replaced folder).
    """
    env = {k: v for k, v in os.environ.items() if not (k.startswith("_PYI") or k in ("_MEIPASS2", "_MEIPASS"))}
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    if sys.platform.startswith("linux") and getattr(sys, "frozen", False):
        orig = env.pop("LD_LIBRARY_PATH_ORIG", None)
        if orig:
            env["LD_LIBRARY_PATH"] = orig
        else:
            env.pop("LD_LIBRARY_PATH", None)
    return env


def build_script_posix() -> str:
    """The helper for Linux (POSIX sh). Arguments: PID, app folder, new folder, exe path, log file."""
    return "\n".join([
        "#!/bin/sh",
        'PID="$1"; APP="$2"; NEW="$3"; EXE="$4"; LOG="$5"',
        'echo "[update] waiting for the app to exit (pid $PID)" > "$LOG"',
        "n=0",
        'while kill -0 "$PID" 2>/dev/null; do',
        "  n=$((n+1))",
        '  if [ "$n" -ge 40 ]; then',
        '    echo "[update] the app did not exit - ending it" >> "$LOG"',
        '    kill -9 "$PID" 2>/dev/null',
        "    sleep 2",
        "    break",
        "  fi",
        "  sleep 1",
        "done",
        "tries=0",
        "while :; do",
        # mirror: remove everything except the user's data/portable flag, then copy the new files (permissions kept)
        '  find "$APP" -mindepth 1 -maxdepth 1 ! -name data ! -name portable.flag -exec rm -rf {} + >> "$LOG" 2>&1',
        '  if cp -a "$NEW"/. "$APP"/ >> "$LOG" 2>&1; then',
        '    echo "[update] files replaced" >> "$LOG"',
        "    break",
        "  fi",
        "  tries=$((tries+1))",
        '  if [ "$tries" -ge 5 ]; then echo "[update] FAILED: files could not be replaced" >> "$LOG"; break; fi',
        "  sleep 3",
        "done",
        'chmod +x "$EXE" 2>/dev/null',
        'echo "[update] starting $EXE" >> "$LOG"',
        'cd "$APP" 2>/dev/null',
        'nohup "$EXE" >/dev/null 2>&1 &',
        "sleep 8",
        # make sure the new app is really running (process names are cut to 15 characters)
        'NAME=$(basename "$EXE" | cut -c1-15)',
        'if command -v pgrep >/dev/null 2>&1 && ! pgrep -x "$NAME" >/dev/null 2>&1; then',
        '  echo "[update] the app is not running - starting it again" >> "$LOG"',
        '  nohup "$EXE" >/dev/null 2>&1 &',
        "fi",
        'echo "[update] done" >> "$LOG"',
        "exit 0",
        "",
    ])


def launch_helper(new_dir: Path) -> None:
    """Start the helper detached from this process. The caller must quit the app right afterwards."""
    work = work_dir()
    work.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        script = work / "apply_update.sh"
        script.write_text(build_script_posix(), encoding="ascii")
        script.chmod(0o755)
        args = ["/bin/sh", str(script), str(os.getpid()), str(app_dir()), str(new_dir), str(app_dir() / EXE_NAME),
                str(work / "update.log")]
        subprocess.Popen(args, start_new_session=True, close_fds=True, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=clean_env())
        return
    script = work / "apply_update.cmd"
    script.write_bytes(build_script().encode("ascii"))
    # CREATE_NO_WINDOW (not DETACHED_PROCESS): the helper gets one hidden console that tasklist/robocopy/ping inherit,
    # so no black terminal windows pop up while the update is applied.
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = 0                                  # SW_HIDE
    args = ["cmd.exe", "/c", str(script), str(os.getpid()), str(app_dir()).rstrip("\\/"), str(new_dir).rstrip("\\/"),
            str(app_dir() / EXE_NAME), str(work / "update.log")]
    subprocess.Popen(args, creationflags=flags, startupinfo=startup, close_fds=True, stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=clean_env())

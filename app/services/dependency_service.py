"""One-click installation of FFmpeg and yt-dlp (no command prompt needed).

FFmpeg is downloaded from a public build site over HTTPS, checked against its published SHA-256 when
available, and unpacked into the app's own ``tools/ffmpeg`` folder (inside the data folder, so portable
mode stays portable). Nothing is installed system-wide.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from app.services import ytdlp_service as yd
from app.services.errors import FriendlyError, OperationCanceled
from app.services import jsruntime
from app.services.ffmpeg_service import ffmpeg_version, find_executable, detect_nvenc
from app.utils.paths import app_data_dir, human_size

log = logging.getLogger("uvs.deps")
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# (zip url, sha256 url or None). Tried in order.
FFMPEG_SOURCES: List[Tuple[str, Optional[str]]] = [
    ("https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip",
     "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip.sha256"),
    ("https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip",
     "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/checksums.sha256"),
]

# percent (None = unknown), message, bytes/s
Progress = Callable[[Optional[float], str, Optional[float]], None]


def managed_ffmpeg_dir() -> Path:
    return app_data_dir() / "tools" / "ffmpeg"


@dataclass
class DepStatus:
    ffmpeg_path: Optional[str]
    ffprobe_path: Optional[str]
    ffmpeg_version: str
    ytdlp_version: str
    ytdlp_ok: bool
    nvenc: bool
    js_runtime: str = ""          # e.g. "deno 2.5.0" / "node 22.22.0", "" when none
    ejs_ok: bool = True

    @property
    def youtube_ok(self) -> bool:
        return bool(self.js_runtime) and self.ejs_ok

    @property
    def ffmpeg_ok(self) -> bool:
        return bool(self.ffmpeg_path and self.ffprobe_path)

    @property
    def all_ok(self) -> bool:
        return self.ffmpeg_ok and self.ytdlp_ok


def check(configured_ffmpeg: str = "", probe_gpu: bool = False) -> DepStatus:
    ff = find_executable("ffmpeg", configured_ffmpeg)
    fp = find_executable("ffprobe", configured_ffmpeg)
    ver = yd.ytdlp_version()
    ok = ver != "not installed"
    rt = jsruntime.find_js_runtime()
    return DepStatus(ff, fp, ffmpeg_version(ff) if ff else "", ver, ok, bool(ff and probe_gpu and detect_nvenc(ff)),
                     f"{rt[0]} {'.'.join(map(str, rt[2]))}" if rt else "", yd.ejs_installed())


def quick_check(configured_ffmpeg: str = "") -> DepStatus:
    """Startup check that only looks for files - it starts no program (that costs seconds on Windows)."""
    import importlib.util
    ff = find_executable("ffmpeg", configured_ffmpeg)
    fp = find_executable("ffprobe", configured_ffmpeg)
    ytdlp = importlib.util.find_spec("yt_dlp") is not None
    rt = jsruntime.js_runtime_present()
    return DepStatus(ff, fp, "", "present" if ytdlp else "not installed", ytdlp, False, rt,
                     importlib.util.find_spec("yt_dlp_ejs") is not None)


def download_file(url: str, dest: Path, progress: Optional[Progress], cancel: threading.Event,
                  allow_http: bool = False, label: str = "Downloading") -> Path:
    """Stream *url* to *dest* (HTTPS only). Raises OperationCanceled / FriendlyError."""
    if not (url.lower().startswith("https://") or (allow_http and url.lower().startswith("http://"))):
        raise FriendlyError("Refusing to download from a non-HTTPS address.")
    dest.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": "UniversalVideoStudio"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r, open(dest, "wb") as out:  # noqa: S310 - scheme checked
            total = int(r.headers.get("Content-Length") or 0) or None
            done, t0, last = 0, time.monotonic(), 0.0
            while True:
                if cancel.is_set():
                    raise OperationCanceled()
                chunk = r.read(256 * 1024)
                if not chunk:
                    break
                out.write(chunk)
                done += len(chunk)
                now = time.monotonic()
                if progress and now - last > 0.15:
                    last = now
                    speed = done / max(0.001, now - t0)
                    pct = done / total * 100 if total else None
                    progress(pct, f"{label} {human_size(done)}" + (f" / {human_size(total)}" if total else ""), speed)
    except OperationCanceled:
        dest.unlink(missing_ok=True)
        raise
    except OSError as exc:
        dest.unlink(missing_ok=True)
        if getattr(exc, "errno", None) == 28:
            raise FriendlyError("The disk is full. Free up space and try again.") from exc
        raise FriendlyError("Download failed. Check your internet connection and try again.", str(exc)) from exc
    return dest


def _expected_sha256(sha_url: Optional[str], filename: str, cancel: threading.Event) -> Optional[str]:
    if not sha_url:
        return None
    try:
        req = urllib.request.Request(sha_url, headers={"User-Agent": "UniversalVideoStudio"})
        with urllib.request.urlopen(req, timeout=20) as r:  # noqa: S310 - https fixed constants
            text = r.read(2_000_000).decode("utf-8", "replace")
    except OSError:
        return None
    for line in text.splitlines():
        parts = line.split()
        if parts and len(parts[0]) == 64 and (len(parts) == 1 or parts[-1].lstrip("*").endswith(filename)):
            return parts[0].lower()
    return None


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def extract_ffmpeg(zip_path: Path, dest_dir: Path) -> None:
    """Extract only ffmpeg(.exe) and ffprobe(.exe) from the archive's ``bin`` folder (no path traversal)."""
    wanted = {"ffmpeg.exe", "ffprobe.exe", "ffmpeg", "ffprobe"}
    found = set()
    dest_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as z:
        for info in z.infolist():
            name = Path(info.filename)
            if (info.is_dir() or name.name.lower() not in wanted or name.is_absolute() or ".." in name.parts
                    or "bin" not in [p.lower() for p in name.parts[:-1]]
                    or name.name.lower() in found):          # first match only: later duplicates cannot replace it
                continue
            target = dest_dir / name.name                       # basename only -> cannot escape dest_dir
            with z.open(info) as src, open(target.with_suffix(target.suffix + ".new"), "wb") as out:
                shutil.copyfileobj(src, out)
            os.replace(target.with_suffix(target.suffix + ".new"), target)
            if os.name != "nt":
                target.chmod(0o755)
            found.add(name.name.lower())
    if not ({"ffmpeg.exe", "ffprobe.exe"} <= found or {"ffmpeg", "ffprobe"} <= found):
        raise FriendlyError("The downloaded archive did not contain ffmpeg and ffprobe.")


def install_ffmpeg(progress: Progress, cancel: threading.Event,
                   sources: Optional[List[Tuple[str, Optional[str]]]] = None,
                   dest_dir: Optional[Path] = None, allow_http: bool = False) -> Path:
    """Download and unpack FFmpeg. Returns the folder containing the executables."""
    if os.name != "nt" and sources is None:
        raise FriendlyError("Automatic FFmpeg download is only available on Windows. "
                            "Install it with your package manager (for example: sudo apt install ffmpeg).")
    dest_dir = dest_dir or managed_ffmpeg_dir()
    tmp_dir = dest_dir.parent / "download"
    shutil.rmtree(tmp_dir, ignore_errors=True)
    last_error: Optional[FriendlyError] = None
    for url, sha_url in sources or FFMPEG_SOURCES:
        zpath = tmp_dir / "ffmpeg.zip"
        try:
            progress(None, "Connecting...", None)
            download_file(url, zpath, progress, cancel, allow_http, "Downloading FFmpeg")
            progress(None, "Verifying download...", None)
            want = _expected_sha256(sha_url, url.rsplit("/", 1)[-1], cancel)
            if want and _sha256(zpath) != want:
                raise FriendlyError("The downloaded file failed its integrity check (SHA-256 mismatch).")
            progress(None, "Unpacking...", None)
            extract_ffmpeg(zpath, dest_dir)
            exe = dest_dir / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg")
            try:
                res = subprocess.run([str(exe), "-version"], capture_output=True, timeout=30, creationflags=_NO_WINDOW)
            except (OSError, subprocess.SubprocessError) as exc:
                raise FriendlyError("The downloaded FFmpeg could not be started.", str(exc)) from exc
            if res.returncode != 0:
                raise FriendlyError("The downloaded FFmpeg could not be started.")
            shutil.rmtree(tmp_dir, ignore_errors=True)
            progress(100.0, "FFmpeg installed", None)
            return dest_dir
        except FriendlyError as exc:
            log.warning("FFmpeg source failed: %s", exc.message)
            last_error = exc
        except zipfile.BadZipFile:
            last_error = FriendlyError("The downloaded file is not a valid archive.")
        finally:
            zpath.unlink(missing_ok=True)
    shutil.rmtree(tmp_dir, ignore_errors=True)
    raise last_error or FriendlyError("FFmpeg could not be downloaded.")


DENO_SOURCES: List[Tuple[str, Optional[str]]] = [
    ("https://github.com/denoland/deno/releases/latest/download/deno-x86_64-pc-windows-msvc.zip",
     "https://github.com/denoland/deno/releases/latest/download/deno-x86_64-pc-windows-msvc.zip.sha256sum"),
]


def install_deno(progress: Progress, cancel: threading.Event,
                 sources: Optional[List[Tuple[str, Optional[str]]]] = None,
                 dest_dir: Optional[Path] = None, allow_http: bool = False) -> Path:
    """Download Deno (single executable) for YouTube's JavaScript challenges into tools/deno."""
    if os.name != "nt" and sources is None:
        raise FriendlyError("Automatic Deno download is only available on Windows. Install Deno or Node.js 22+ yourself.")
    dest_dir = dest_dir or jsruntime.managed_deno_dir()
    tmp_dir = dest_dir.parent / "download_deno"
    shutil.rmtree(tmp_dir, ignore_errors=True)
    last: Optional[FriendlyError] = None
    for url, sha_url in sources or DENO_SOURCES:
        zpath = tmp_dir / "deno.zip"
        try:
            progress(None, "Connecting...", None)
            download_file(url, zpath, progress, cancel, allow_http, "Downloading Deno")
            progress(None, "Verifying download...", None)
            want = _expected_sha256(sha_url, url.rsplit("/", 1)[-1], cancel)
            if want and _sha256(zpath) != want:
                raise FriendlyError("The downloaded file failed its integrity check (SHA-256 mismatch).")
            progress(None, "Unpacking...", None)
            exe_name = "deno.exe" if os.name == "nt" else "deno"
            dest_dir.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(zpath) as z:
                member = next((i for i in z.infolist() if not i.is_dir() and Path(i.filename).name.lower() == exe_name
                               and ".." not in Path(i.filename).parts), None)
                if member is None:
                    raise FriendlyError("The downloaded archive did not contain deno.")
                tmp = dest_dir / (exe_name + ".new")
                with z.open(member) as src, open(tmp, "wb") as out:
                    shutil.copyfileobj(src, out)
                os.replace(tmp, dest_dir / exe_name)
            if os.name != "nt":
                (dest_dir / exe_name).chmod(0o755)
            try:
                res = subprocess.run([str(dest_dir / exe_name), "--version"], capture_output=True, timeout=30,
                                     creationflags=_NO_WINDOW)
            except (OSError, subprocess.SubprocessError) as exc:
                raise FriendlyError("The downloaded Deno could not be started.", str(exc)) from exc
            if res.returncode != 0:
                raise FriendlyError("The downloaded Deno could not be started.")
            shutil.rmtree(tmp_dir, ignore_errors=True)
            progress(100.0, "Deno installed", None)
            return dest_dir
        except FriendlyError as exc:
            last = exc
        except zipfile.BadZipFile:
            last = FriendlyError("The downloaded file is not a valid archive.")
        finally:
            zpath.unlink(missing_ok=True)
    shutil.rmtree(tmp_dir, ignore_errors=True)
    raise last or FriendlyError("Deno could not be downloaded.")


def install_ytdlp(progress: Progress) -> str:
    """Install or update yt-dlp with pip inside this environment."""
    progress(None, "Installing yt-dlp with pip...", None)
    ok, msg = yd.update_ytdlp()
    if not ok:
        raise FriendlyError(msg.splitlines()[0], msg)
    import importlib
    importlib.invalidate_caches()
    progress(100.0, "yt-dlp installed", None)
    return msg

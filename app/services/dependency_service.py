"""One-click installation of FFmpeg and yt-dlp (no command prompt needed).

FFmpeg is downloaded from a public build site over HTTPS, checked against its published SHA-256 when
available, and unpacked into the app's own ``tools/ffmpeg`` folder (inside the data folder, so portable
mode stays portable). Nothing is installed system-wide.
"""

from __future__ import annotations

import hashlib
import http.client
import logging
import os
import platform
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from app.utils.net import urlopen as _net_urlopen
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

def _deno_target() -> str:
    return "x86_64-pc-windows-msvc" if os.name == "nt" else ""


# (archive url, checksum url or None). Tried in order. .zip with a SHA-256 file.
def default_ffmpeg_sources() -> List[Tuple[str, Optional[str]]]:
    if os.name == "nt":
        return [
            ("https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip",
             "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip.sha256"),
            ("https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip",
             "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/checksums.sha256"),
        ]
    return []


FFMPEG_SOURCES: List[Tuple[str, Optional[str]]] = default_ffmpeg_sources()

# Where the newest released version number can be read (plain text / JSON, a few bytes).
FFMPEG_VERSION_URL = "https://www.gyan.dev/ffmpeg/builds/release-version"
DENO_LATEST_URL = "https://api.github.com/repos/denoland/deno/releases/latest"

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
        with _net_urlopen(req, timeout=30) as r, open(dest, "wb") as out:  # noqa: S310 - scheme checked
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
    except (OSError, http.client.HTTPException) as exc:
        dest.unlink(missing_ok=True)
        if getattr(exc, "errno", None) == 28:
            raise FriendlyError("The disk is full. Free up space and try again.") from exc
        log.warning("download failed (%s): %s", url, exc)
        raise FriendlyError("Download failed. Check your internet connection and try again.", f"{url.split('/')[2]}: {exc}") from exc
    return dest


def _expected_sha256(sha_url: Optional[str], filename: str, cancel: threading.Event) -> Optional[str]:
    if not sha_url:
        return None
    try:
        req = urllib.request.Request(sha_url, headers={"User-Agent": "UniversalVideoStudio"})
        with _net_urlopen(req, timeout=20) as r:  # noqa: S310 - https fixed constants
            text = r.read(2_000_000).decode("utf-8", "replace")
    except OSError:
        return None
    for line in text.splitlines():
        parts = line.split()
        if parts and len(parts[0]) == 64 and (len(parts) == 1 or parts[-1].lstrip("*").endswith(filename)):
            return parts[0].lower()
    return None


def _expected_digest(sha_url: Optional[str], filename: str) -> Optional[Tuple[str, str]]:
    """("sha256"|"md5", hex) from a checksum file; MD5 is accepted for sites that publish nothing stronger."""
    if not sha_url:
        return None
    try:
        req = urllib.request.Request(sha_url, headers={"User-Agent": "UniversalVideoStudio"})
        with _net_urlopen(req, timeout=20) as r:  # noqa: S310 - https fixed constants
            text = r.read(2_000_000).decode("utf-8", "replace")
    except OSError:
        return None
    for line in text.splitlines():
        parts = line.split()
        if parts and re.fullmatch(r"[0-9a-fA-F]{32}|[0-9a-fA-F]{64}", parts[0]) \
                and (len(parts) == 1 or parts[-1].lstrip("*").endswith(filename)):
            return ("sha256" if len(parts[0]) == 64 else "md5"), parts[0].lower()
    return None


def _file_digest(path: Path, algo: str) -> str:
    h = hashlib.new(algo)
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _verify_download(path: Path, sha_url: Optional[str], url: str) -> None:
    want = _expected_digest(sha_url, url.rsplit("/", 1)[-1])
    if want and _file_digest(path, want[0]) != want[1]:
        raise FriendlyError("The downloaded file failed its integrity check (checksum mismatch).")


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
    sources = sources if sources is not None else default_ffmpeg_sources()
    if not sources:
        raise FriendlyError("Automatic FFmpeg download is not available for this system. "
                            "Install FFmpeg yourself and put it in the app's ffmpeg folder.")
    dest_dir = dest_dir or managed_ffmpeg_dir()
    tmp_dir = dest_dir.parent / "download"
    shutil.rmtree(tmp_dir, ignore_errors=True)
    last_error: Optional[FriendlyError] = None
    reasons: List[str] = []
    for url, sha_url in sources:
        zpath = tmp_dir / "ffmpeg.zip"
        try:
            progress(None, "Connecting...", None)
            download_file(url, zpath, progress, cancel, allow_http, "Downloading FFmpeg")
            progress(None, "Verifying download...", None)
            _verify_download(zpath, sha_url, url)
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
            log.warning("FFmpeg source failed: %s (%s) %s", exc.message, exc.details, url)
            last_error = exc
            reasons.append(f"{url.split('/')[2]}: {exc.message} {exc.details or ''}".strip())
        except (zipfile.BadZipFile, EOFError):
            last_error = FriendlyError("The downloaded file is not a valid archive.")
        finally:
            zpath.unlink(missing_ok=True)
    shutil.rmtree(tmp_dir, ignore_errors=True)
    if last_error and len(reasons) > 1:
        raise FriendlyError(last_error.message, " | ".join(reasons)[:600])
    raise last_error or FriendlyError("FFmpeg could not be downloaded.")


def default_deno_sources() -> List[Tuple[str, Optional[str]]]:
    target = _deno_target()
    if not target:
        return []
    base = "https://github.com/denoland/deno/releases/latest/download"
    return [(f"{base}/deno-{target}.zip", f"{base}/deno-{target}.zip.sha256sum")]


DENO_SOURCES: List[Tuple[str, Optional[str]]] = default_deno_sources()


def install_deno(progress: Progress, cancel: threading.Event,
                 sources: Optional[List[Tuple[str, Optional[str]]]] = None,
                 dest_dir: Optional[Path] = None, allow_http: bool = False) -> Path:
    """Download Deno (single executable) for YouTube's JavaScript challenges into tools/deno."""
    sources = sources if sources is not None else default_deno_sources()
    if not sources:
        raise FriendlyError("Automatic Deno download is not available for this system. Install Deno or Node.js 22+ yourself.")
    dest_dir = dest_dir or jsruntime.managed_deno_dir()
    tmp_dir = dest_dir.parent / "download_deno"
    shutil.rmtree(tmp_dir, ignore_errors=True)
    last: Optional[FriendlyError] = None
    for url, sha_url in sources:
        zpath = tmp_dir / "deno.zip"
        try:
            progress(None, "Connecting...", None)
            download_file(url, zpath, progress, cancel, allow_http, "Downloading Deno")
            progress(None, "Verifying download...", None)
            _verify_download(zpath, sha_url, url)
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


# ---------------------------------------------------------------------------
# "Update" = check first, download only when a newer version exists
# ---------------------------------------------------------------------------
def version_tuple(text: str) -> Optional[Tuple[int, ...]]:
    """First dotted number in *text* ("ffmpeg version 9.0.2-essentials..." / "v2.9.8" -> (9,0,2) / (2,9,8))."""
    import re
    m = re.search(r"(\d+(?:\.\d+)+)", text or "")
    return tuple(int(x) for x in m.group(1).split(".")) if m else None


def _fetch_text(url: str, timeout: float = 10, allow_http: bool = False) -> Optional[str]:
    if not (url.startswith("https://") or (allow_http and url.startswith("http://"))):
        return None
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "UniversalVideoStudio", "Accept": "application/json, text/plain"})
        with _net_urlopen(req, timeout=timeout) as r:  # noqa: S310 - https constants / tests use http
            return r.read(500_000).decode("utf-8", "replace")
    except (OSError, ValueError):
        return None


def latest_ffmpeg_version(url: str = FFMPEG_VERSION_URL, allow_http: bool = False) -> Optional[str]:
    """Newest release number: gyan.dev's one-line file."""
    text = _fetch_text(url, allow_http=allow_http)
    if not text or not text.strip():
        return None
    m = re.search(r"^\s*version:\s*(\d+(?:\.\d+)+)", text, re.M | re.I)
    return m.group(1) if m else text.strip().splitlines()[0]


def latest_deno_version(url: str = DENO_LATEST_URL, allow_http: bool = False) -> Optional[str]:
    import json
    if url == DENO_LATEST_URL:                            # the REST API is limited to 60 requests/hour per IP: avoid it
        try:
            from app.utils.net import github_latest_tag
            tag, _page = github_latest_tag("denoland/deno")
            if tag and re.fullmatch(r"v?\d+(\.\d+)+", tag):
                return tag.lstrip("v")
        except Exception:  # noqa: BLE001 - fall back to the API below
            pass
    text = _fetch_text(url, allow_http=allow_http)
    try:
        return str(json.loads(text)["tag_name"]).lstrip("v") if text else None
    except (ValueError, KeyError, TypeError):
        return None


def _fmt(v: Optional[Tuple[int, ...]]) -> str:
    return ".".join(map(str, v)) if v else "unknown"


def update_ffmpeg(progress: Progress, cancel: threading.Event, configured: str = "", version_url: str = FFMPEG_VERSION_URL,
                  sources: Optional[List[Tuple[str, Optional[str]]]] = None, dest_dir: Optional[Path] = None,
                  allow_http: bool = False) -> str:
    """Install FFmpeg when it is missing; otherwise download only if a newer release exists. Returns a message."""
    exe = find_executable("ffmpeg", configured)
    if exe is None:
        install_ffmpeg(progress, cancel, sources, dest_dir, allow_http)
        return "FFmpeg was installed."
    progress(None, "Checking for a newer FFmpeg...", None)
    cur = version_tuple(ffmpeg_version(exe))
    latest_text = latest_ffmpeg_version(version_url, allow_http)
    latest = version_tuple(latest_text or "")
    if latest is None:
        raise FriendlyError("Could not check for a newer FFmpeg. Check your internet connection and try again.")
    if cur is not None and latest <= cur:
        return f"FFmpeg is up to date ({_fmt(cur)})."
    install_ffmpeg(progress, cancel, sources, dest_dir, allow_http)
    return f"FFmpeg was updated to {_fmt(latest)}."


def update_deno(progress: Progress, cancel: threading.Event, version_url: str = DENO_LATEST_URL,
                sources: Optional[List[Tuple[str, Optional[str]]]] = None, dest_dir: Optional[Path] = None,
                allow_http: bool = False) -> str:
    """Install Deno when no JavaScript runtime exists; otherwise download only if a newer release exists."""
    found = jsruntime.find_js_runtime()
    if found is None or found[0] != "deno":
        install_deno(progress, cancel, sources, dest_dir, allow_http)
        return "Deno was installed."
    progress(None, "Checking for a newer Deno...", None)
    latest = version_tuple(latest_deno_version(version_url, allow_http) or "")
    if latest is None:
        raise FriendlyError("Could not check for a newer Deno. Check your internet connection and try again.")
    if latest <= found[2]:
        return f"Deno is up to date ({_fmt(found[2])})."
    install_deno(progress, cancel, sources, dest_dir, allow_http)
    return f"Deno was updated to {_fmt(latest)}."


def check_ffmpeg_update(configured: str = "", url: str = FFMPEG_VERSION_URL, allow_http: bool = False) -> Tuple[str, str]:
    ff = find_executable("ffmpeg", configured)
    if not ff:
        return "missing", ""
    cur, lt = version_tuple(ffmpeg_version(ff)), version_tuple(latest_ffmpeg_version(url, allow_http) or "")
    if lt is None:
        return "unknown", ""
    return ("available" if cur is None or lt > cur else "uptodate"), _fmt(lt)


def check_ytdlp_update() -> Tuple[str, str]:
    ver = yd.ytdlp_version()
    if ver == "not installed":
        return "missing", ""
    latest = yd.latest_ytdlp_version()
    lt, cur = version_tuple(latest or ""), version_tuple(ver)
    if lt is None:
        return "unknown", ""
    return ("available" if cur is None or lt > cur else "uptodate"), str(latest)


def check_deno_update(url: str = DENO_LATEST_URL, allow_http: bool = False) -> Tuple[str, str]:
    found = jsruntime.find_js_runtime()
    if found is None:
        return "missing", ""
    if found[0] != "deno":                                 # Node is used: Deno is optional, offer it as an update
        return "available", ""
    lt = version_tuple(latest_deno_version(url, allow_http) or "")
    if lt is None:
        return "unknown", ""
    return ("available" if lt > found[2] else "uptodate"), _fmt(lt)


def check_tool_updates(configured: str = "", ffmpeg_url: str = FFMPEG_VERSION_URL, deno_url: str = DENO_LATEST_URL,
                       allow_http: bool = False) -> dict:
    """Compare installed and newest versions without downloading anything.

    Returns ``{"ffmpeg"|"ytdlp"|"deno": (state, newest)}`` where state is ``missing``, ``uptodate``, ``available`` or
    ``unknown`` (newest version could not be read - offline).
    """
    return {"ffmpeg": check_ffmpeg_update(configured, ffmpeg_url, allow_http), "ytdlp": check_ytdlp_update(),
            "deno": check_deno_update(deno_url, allow_http)}

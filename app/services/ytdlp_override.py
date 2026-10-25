"""Updating yt-dlp inside a packaged (PyInstaller) build.

The packaged app contains its own copy of yt-dlp that cannot be changed with pip. An update therefore
downloads the official pure-Python wheel from PyPI (SHA-256 verified), unpacks the ``yt_dlp`` package into
``<data folder>/tools/ytdlp`` and a small import hook makes ``import yt_dlp`` prefer that folder - but only
while it is newer than the bundled copy (so a rebuilt app is never shadowed by an old update).
"""

from __future__ import annotations

import hashlib
import importlib.abc
import importlib.machinery
import json
import logging
import re
import shutil
import sys
import threading
import urllib.request
from app.utils.net import urlopen as _net_urlopen
import zipfile
from pathlib import Path
from typing import Callable, Optional, Tuple

from app.services.errors import FriendlyError

log = logging.getLogger("uvs.ytdlp")
PYPI_JSON = "https://pypi.org/pypi/yt-dlp/json"
_installed = False


def override_root() -> Path:
    from app.utils.paths import app_data_dir
    return app_data_dir() / "tools" / "ytdlp"


def _vtuple(v: str) -> Tuple[int, ...]:
    return tuple(int("".join(c for c in p if c.isdigit()) or 0) for p in v.split("."))


def override_version(root: Optional[Path] = None) -> Optional[str]:
    f = (root or override_root()) / "yt_dlp" / "version.py"
    try:
        m = re.search(r"__version__\s*=\s*['\"]([^'\"]+)['\"]", f.read_text(encoding="utf-8"))
    except OSError:
        return None
    return m.group(1) if m else None


def bundled_version() -> Optional[str]:
    try:
        from importlib.metadata import version
        return version("yt-dlp")
    except Exception:  # noqa: BLE001 - metadata may be absent in a frozen app
        return None


class _Finder(importlib.abc.MetaPathFinder):
    def __init__(self, root: Path) -> None:
        self.root = str(root)

    def find_spec(self, name, path=None, target=None):
        if name == "yt_dlp":
            return importlib.machinery.PathFinder.find_spec(name, [self.root])
        if name.startswith("yt_dlp."):
            return importlib.machinery.PathFinder.find_spec(name, path)
        return None


def install_finder(force: bool = False, root: Optional[Path] = None) -> bool:
    """Make ``yt_dlp`` come from the downloaded update when one exists and is newer. Returns True if active."""
    global _installed
    if _installed:
        return True
    if not force and not getattr(sys, "frozen", False):
        return False                       # development: pip manages yt-dlp
    root = root or override_root()
    ov = override_version(root)
    if not ov or "yt_dlp" in sys.modules and not force:
        return False
    bv = bundled_version()
    if bv and _vtuple(ov) <= _vtuple(bv):
        return False
    sys.meta_path.insert(0, _Finder(root))
    _installed = True
    log.info("Using updated yt-dlp %s from %s", ov, root)
    return True


def _latest_wheel(url: str = PYPI_JSON) -> Tuple[str, str, str]:
    """(version, wheel_url, sha256) of the newest yt-dlp release."""
    req = urllib.request.Request(url, headers={"User-Agent": "UniversalVideoStudio"})
    try:
        with _net_urlopen(req, timeout=30) as r:  # noqa: S310 - fixed https URL
            data = json.loads(r.read(5_000_000))
    except Exception as exc:  # noqa: BLE001
        raise FriendlyError("Could not reach PyPI to check for a yt-dlp update. Check your internet connection.",
                            str(exc)) from exc
    for f in data.get("urls", []):
        if f.get("packagetype") == "bdist_wheel" and str(f.get("filename", "")).endswith("none-any.whl"):
            return data["info"]["version"], f["url"], f["digests"]["sha256"]
    raise FriendlyError("PyPI did not list a yt-dlp wheel.")


def _safe_extract(wheel: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    base = dest.resolve()
    with zipfile.ZipFile(wheel) as z:
        for info in z.infolist():
            parts = Path(info.filename).parts
            if info.is_dir() or not parts or parts[0] != "yt_dlp" or ".." in parts or Path(info.filename).is_absolute():
                continue
            target = (dest / info.filename).resolve()
            if base not in target.parents:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with z.open(info) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out)
    if not (dest / "yt_dlp" / "version.py").is_file():
        raise FriendlyError("The downloaded yt-dlp package was incomplete.")


def update_packaged(progress: Optional[Callable] = None, cancel: Optional[threading.Event] = None,
                    meta_url: str = PYPI_JSON, root: Optional[Path] = None, allow_http: bool = False) -> str:
    """Download and install the newest yt-dlp for a packaged build. Returns a user message."""
    from app.services import dependency_service as deps
    cancel = cancel or threading.Event()
    root = root or override_root()
    progress = progress or (lambda *a: None)
    progress(None, "Checking PyPI...", None)
    ver, url, want = _latest_wheel(meta_url)
    current = override_version(root) or bundled_version() or ""
    if current and _vtuple(ver) <= _vtuple(current):
        return f"yt-dlp is already up to date ({current})."
    tmp = root.parent / "download_ytdlp"
    shutil.rmtree(tmp, ignore_errors=True)
    try:
        wheel = tmp / "yt_dlp.whl"
        deps.download_file(url, wheel, progress, cancel, allow_http, "Downloading yt-dlp")
        h = hashlib.sha256(wheel.read_bytes()).hexdigest()
        if h != want:
            raise FriendlyError("The downloaded yt-dlp failed its integrity check (SHA-256 mismatch).")
        staged = root.parent / "ytdlp.new"
        shutil.rmtree(staged, ignore_errors=True)
        _safe_extract(wheel, staged)
        shutil.rmtree(root, ignore_errors=True)
        staged.replace(root)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    progress(100.0, "yt-dlp updated", None)
    return f"yt-dlp was updated to {ver}. Restart the application to use it."

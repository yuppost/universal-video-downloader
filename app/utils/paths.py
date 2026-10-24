"""Filesystem helpers: app-data location, filename sanitising, collision-safe naming."""

from __future__ import annotations

import os
import re
import sys
import threading
from pathlib import Path
from typing import Iterable, Mapping, Optional

_INVALID_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
             *(f"LPT{i}" for i in range(1, 10))}
_finalize_lock = threading.Lock()


def sanitize_filename(name: str, max_length: int = 120, fallback: str = "clip") -> str:
    """Return a Windows-safe file *stem* (no directory parts, no reserved names)."""
    name = (name or "").replace("‮", "")  # strip RTL-override tricks
    name = _INVALID_CHARS.sub("_", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    # Neutralise traversal remnants such as '..' that survived as a whole name.
    if name in {"", ".", ".."}:
        name = fallback
    if name.split(".")[0].upper() in _RESERVED:
        name = f"_{name}"
    if len(name) > max_length:
        name = name[:max_length].rstrip(" .")
    return name or fallback


def unique_path(directory: Path, stem: str, ext: str, reserved: Optional[Iterable[Path]] = None) -> Path:
    """Return a path in *directory* that does not exist yet (adds ' (2)', ' (3)', ...)."""
    ext = ext if ext.startswith(".") else f".{ext}"
    taken = {str(p).lower() for p in (reserved or [])}
    candidate = directory / f"{stem}{ext}"
    n = 2
    while candidate.exists() or str(candidate).lower() in taken:
        candidate = directory / f"{stem} ({n}){ext}"
        n += 1
    return candidate


def finalize_file(tmp: Path, directory: Path, stem: str, ext: str) -> Path:
    """Atomically move *tmp* to a collision-free final name and return it.

    Never overwrites an existing user file.
    """
    directory.mkdir(parents=True, exist_ok=True)
    with _finalize_lock:
        final = unique_path(directory, stem, ext)
        os.replace(tmp, final)
    return final


def is_within(base: Path, target: Path) -> bool:
    """True if *target* resolves to a location inside *base* (traversal guard)."""
    try:
        target.resolve().relative_to(base.resolve())
        return True
    except (ValueError, OSError):
        return False


_TOKEN_RE = re.compile(r"\{(title|index|start|end|uploader|id|site)(?::0?(\d{1,2})d)?\}")
DEFAULT_TEMPLATE = "{title}_clip_{index:02d}"


def render_filename_template(template: str, values: Mapping[str, object]) -> str:
    """Safely expand ``{title}``, ``{index:02d}``, ... (no attribute access, no eval)."""

    def repl(m: re.Match) -> str:
        key, width = m.group(1), m.group(2)
        val = values.get(key, "")
        if width and isinstance(val, int):
            return str(val).zfill(int(width))
        return str(val)

    rendered = _TOKEN_RE.sub(repl, template or DEFAULT_TEMPLATE)
    return sanitize_filename(rendered)


def portable_root() -> Optional[Path]:
    """Return the folder holding a ``data`` dir next to the app, enabling portable mode."""
    base = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parents[2]
    # Only an explicit portable.flag enables portable mode. A stray "data" folder must never
    # switch it on, so sign-in cookies never end up next to the program by accident.
    if (base / "portable.flag").exists():
        return base / "data"
    return None


def app_data_dir() -> Path:
    """Directory for settings, history and logs (override with UVS_DATA_DIR)."""
    override = os.environ.get("UVS_DATA_DIR")
    if override:
        p = Path(override)
    elif (pr := portable_root()) is not None:
        p = pr
    elif os.name == "nt":
        p = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / "UniversalVideoStudio"
    else:
        p = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "UniversalVideoStudio"
    p.mkdir(parents=True, exist_ok=True)
    return p


def videos_root() -> Path:
    """<user's Videos folder>/UniversalVideoStudio (on Windows: C:\\Users\\<name>\\Videos)."""
    return Path.home() / "Videos" / "UniversalVideoStudio"


def default_download_dir() -> Path:
    """Finished downloads and exported clips."""
    return videos_root() / "Video"


def default_audio_dir() -> Path:
    """MP3 downloads and exported audio clips."""
    return videos_root() / "Audio"


def default_temp_dir() -> Path:
    """Cache: low-quality preview files, source downloads, thumbnails."""
    return videos_root() / "Cache video"


def legacy_defaults() -> tuple:
    """Folders used as defaults by older versions (settings that still equal them are migrated)."""
    return videos_root(), app_data_dir() / "temp"


def dir_size(path: Path) -> int:
    """Total size in bytes of files below *path* (0 if missing)."""
    total = 0
    if not path.exists():
        return 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += (Path(root) / f).stat().st_size
            except OSError:
                pass
    return total


def human_size(num: Optional[float]) -> str:
    if num is None:
        return "unknown"
    n = float(num)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"

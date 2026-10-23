"""Thumbnail strip generation for the timeline (from a *local* media file)."""

from __future__ import annotations

import logging
import subprocess
import threading
from pathlib import Path
from typing import Callable, List, Optional

from app.services.errors import OperationCanceled

log = logging.getLogger("uvs.thumbs")
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def thumbnail_times(duration: float, count: int) -> List[float]:
    """Evenly spaced sample times (centre of each of *count* slots)."""
    if duration <= 0 or count <= 0:
        return []
    step = duration / count
    return [min(duration - 0.05, step * (i + 0.5)) for i in range(count)]


def generate_strip(ffmpeg: str, source: Path, duration: float, out_dir: Path, count: int = 40,
                   height: int = 56, cancel: Optional[threading.Event] = None,
                   on_thumb: Optional[Callable[[int, float, Path], None]] = None) -> List[Path]:
    """Extract *count* small JPEG frames. Calls *on_thumb(index, time, path)* as each is ready.

    Uses fast input seeking, so cost is roughly constant per frame regardless of video length.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    made: List[Path] = []
    for i, t in enumerate(thumbnail_times(duration, count)):
        if cancel is not None and cancel.is_set():
            raise OperationCanceled()
        dst = out_dir / f"t{i:03d}.jpg"
        cmd = [ffmpeg, "-hide_banner", "-nostdin", "-y", "-loglevel", "error", "-ss", f"{t:.2f}",
               "-i", str(source), "-frames:v", "1", "-vf", f"scale=-2:{height}", "-q:v", "5", str(dst)]
        try:
            subprocess.run(cmd, capture_output=True, timeout=30, creationflags=_NO_WINDOW)
        except (OSError, subprocess.SubprocessError):
            log.warning("Thumbnail %d failed", i)
            continue
        if dst.exists() and dst.stat().st_size > 0:
            made.append(dst)
            if on_thumb:
                on_thumb(i, t, dst)
    return made

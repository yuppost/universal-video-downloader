"""Media inspection through FFprobe."""

from __future__ import annotations

import json
import logging
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from app.services.errors import FriendlyError
from app.services.ffmpeg_service import require_ffprobe

log = logging.getLogger("uvs.probe")
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


@dataclass
class MediaInfo:
    duration: Optional[float]
    width: Optional[int]
    height: Optional[int]
    vcodec: Optional[str]
    acodec: Optional[str]
    fps: Optional[float]

    @property
    def has_video(self) -> bool:
        return self.vcodec is not None

    @property
    def has_audio(self) -> bool:
        return self.acodec is not None


def _fps(rate: str) -> Optional[float]:
    try:
        n, d = rate.split("/")
        return float(n) / float(d) if float(d) else None
    except (ValueError, ZeroDivisionError, AttributeError):
        return None


def probe(path: Path, ffmpeg_hint: str = "") -> MediaInfo:
    """Inspect a local media file. Raises FriendlyError on failure."""
    ffprobe = require_ffprobe(ffmpeg_hint)
    cmd = [ffprobe, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=60, creationflags=_NO_WINDOW)
    except (OSError, subprocess.SubprocessError) as exc:
        raise FriendlyError("Could not run FFprobe.", str(exc)) from exc
    if res.returncode != 0:
        raise FriendlyError("The media file could not be read (it may be damaged or incomplete).", res.stderr[-400:])
    data = json.loads(res.stdout or "{}")
    v = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"
              and not s.get("disposition", {}).get("attached_pic")), None)
    a = next((s for s in data.get("streams", []) if s.get("codec_type") == "audio"), None)
    dur = None
    for src in ((data.get("format") or {}), v or {}):
        try:
            dur = float(src["duration"])
            break
        except (KeyError, TypeError, ValueError):
            continue
    return MediaInfo(
        duration=dur,
        width=int(v["width"]) if v and v.get("width") else None,
        height=int(v["height"]) if v and v.get("height") else None,
        vcodec=v.get("codec_name") if v else None,
        acodec=a.get("codec_name") if a else None,
        fps=_fps(v.get("avg_frame_rate", "")) if v else None,
    )

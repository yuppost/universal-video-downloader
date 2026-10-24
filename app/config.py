"""Persistent user settings stored as JSON in the application-data folder."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any, Dict

from app.utils.paths import app_data_dir, default_audio_dir, default_download_dir, default_temp_dir, legacy_defaults

log = logging.getLogger("uvs.config")

QUALITIES = ["best", "2160", "1440", "1080", "720", "480", "360"]
BROWSERS = ["", "chrome", "edge", "firefox", "brave", "opera", "vivaldi", "chromium"]


@dataclass
class Settings:
    download_dir: str = ""
    audio_dir: str = ""                   # MP3 downloads / audio clips
    output_format: str = "mp4"            # mp4 | mp3
    preferred_quality: str = "best"       # best | 2160 | ... | 360
    default_crop: str = "original"
    trim_mode: str = "fast"               # fast | accurate
    video_codec: str = "h264"             # h264 | h265
    hw_encoder: str = "auto"              # auto (NVIDIA NVENC if available) | cpu
    ffmpeg_path: str = ""
    ytdlp_check_on_start: bool = False
    check_app_updates: bool = True
    max_concurrent: int = 2
    filename_template: str = "{title}_clip_{index:02d}"
    theme: str = "dark"                   # dark | light
    temp_dir: str = ""
    cleanup_mode: str = "keep_all"        # keep_all | clean_partials (never deletes sources/outputs)
    keep_intermediates: bool = False
    cache_limit_gb: float = 20.0
    prefer_sections: bool = True
    cookies_browser: str = ""             # browser name only; cookies are read by yt-dlp at runtime
    cookies_file: str = ""                # optional cookies.txt exported by the user (path only)
    icon_path: str = ""                   # optional custom app icon (png/ico/jpg/svg)
    setup_prompt_dismissed: bool = False
    window_geometry: str = ""

    def __post_init__(self) -> None:
        if not self.download_dir:
            self.download_dir = str(default_download_dir())
        if not self.audio_dir:
            self.audio_dir = str(default_audio_dir())
        if not self.temp_dir:
            self.temp_dir = str(default_temp_dir())

    # --- validation ------------------------------------------------------
    def sanitize(self) -> None:
        """Replace invalid values with defaults (settings files can be hand-edited)."""
        d = Settings()
        if self.output_format not in ("mp4", "mp3"):
            self.output_format = d.output_format
        if self.preferred_quality not in QUALITIES:
            self.preferred_quality = d.preferred_quality
        if self.trim_mode not in ("fast", "accurate"):
            self.trim_mode = d.trim_mode
        if self.video_codec not in ("h264", "h265"):
            self.video_codec = d.video_codec
        if self.hw_encoder not in ("auto", "nvenc", "cpu"):
            self.hw_encoder = d.hw_encoder
        if self.theme not in ("dark", "light"):
            self.theme = d.theme
        if self.cleanup_mode not in ("keep_all", "clean_partials"):
            self.cleanup_mode = d.cleanup_mode
        if self.cookies_browser not in BROWSERS:
            self.cookies_browser = ""
        try:
            self.max_concurrent = max(1, min(8, int(self.max_concurrent)))
        except (TypeError, ValueError):
            self.max_concurrent = d.max_concurrent
        try:
            self.cache_limit_gb = max(1.0, float(self.cache_limit_gb))
        except (TypeError, ValueError):
            self.cache_limit_gb = d.cache_limit_gb
        if not self.filename_template.strip():
            self.filename_template = d.filename_template

    def cookie_spec(self) -> str:
        from app.services.signin_service import cookie_spec
        return cookie_spec(self.cookies_browser, self.cookies_file)

    @property
    def max_height(self):
        return None if self.preferred_quality == "best" else int(self.preferred_quality)

    @property
    def cache_dir(self) -> Path:
        return Path(self.temp_dir) / "sources"


def settings_path() -> Path:
    return app_data_dir() / "settings.json"


def load_settings(path: Path | None = None) -> Settings:
    path = path or settings_path()
    s = Settings()
    try:
        data: Dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        known = {f.name for f in fields(Settings)}
        for k, v in data.items():
            if k in known:
                setattr(s, k, v)
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as exc:
        log.warning("Could not read settings (%s); using defaults", exc)
    # Older versions defaulted to <Videos>/UniversalVideoStudio and %APPDATA%/.../temp; move those
    # untouched defaults to the new Video / Cache video folders. Custom choices are kept.
    try:
        old_dl, old_tmp = legacy_defaults()
        if Path(s.download_dir) == old_dl:
            s.download_dir = str(default_download_dir())
        if Path(s.temp_dir) == old_tmp:
            s.temp_dir = str(default_temp_dir())
    except OSError:
        pass
    s.sanitize()
    return s


def save_settings(s: Settings, path: Path | None = None) -> None:
    path = path or settings_path()
    tmp = path.with_suffix(".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(asdict(s), indent=2), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        log.error("Could not save settings: %s", exc)

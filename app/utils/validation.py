"""Input validation and time-format helpers. Pure functions, no Qt."""

from __future__ import annotations

import re
from typing import Optional
from urllib.parse import urlparse

MAX_URL_LENGTH = 2048
_PLAYLIST_RE = re.compile(r"(?:[?&]list=|/playlist\b|/playlists?/|/sets/|/album/)", re.I)


class UrlValidationError(ValueError):
    """Raised when user-supplied URL text is not acceptable."""


def validate_url(text: str) -> str:
    """Validate and normalise a user-supplied URL.

    Only plain http(s) URLs are accepted. The URL is only ever passed to yt-dlp
    as a data value - never to a shell.
    """
    if text is None:
        raise UrlValidationError("Please paste a video URL.")
    url = text.strip()
    if not url:
        raise UrlValidationError("Please paste a video URL.")
    if len(url) > MAX_URL_LENGTH:
        raise UrlValidationError("That URL is too long.")
    if re.search(r"[\s\x00-\x1f\x7f]", url):
        raise UrlValidationError("The URL contains spaces or control characters.")
    if "://" not in url:
        # Be forgiving: "youtube.com/watch?v=..." -> add https
        if re.match(r"^[\w.-]+\.[a-z]{2,}(/|$)", url, re.I):
            url = "https://" + url
        else:
            raise UrlValidationError("Enter a full web address starting with http:// or https://")
    parsed = urlparse(url)
    if parsed.scheme.lower() not in ("http", "https"):
        raise UrlValidationError("Only http:// and https:// links are supported.")
    host = parsed.hostname
    if not host or "." not in host:
        raise UrlValidationError("The URL has no valid website address.")
    if parsed.username or parsed.password or "@" in parsed.netloc:
        raise UrlValidationError("URLs containing a username or password are not accepted.")
    return url


def looks_like_playlist(url: str) -> bool:
    """Heuristic: does the URL reference a playlist (alone or alongside a video)?"""
    return bool(_PLAYLIST_RE.search(url))


def site_name(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def format_timestamp(seconds: Optional[float], with_ms: bool = False) -> str:
    """Format seconds as ``MM:SS`` / ``H:MM:SS`` (optionally with ``.mmm``)."""
    if seconds is None or seconds != seconds:  # None or NaN
        return "--:--"
    seconds = max(0.0, float(seconds))
    total_ms = int(round(seconds * 1000))
    ms = total_ms % 1000
    total_s = total_ms // 1000
    h, rem = divmod(total_s, 3600)
    m, s = divmod(rem, 60)
    base = f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"
    return f"{base}.{ms:03d}" if with_ms else base


def parse_timestamp(text: str) -> float:
    """Parse ``SS``, ``MM:SS``, ``H:MM:SS`` (optionally with decimals) into seconds."""
    text = (text or "").strip().replace(",", ".")
    if not text:
        raise ValueError("Empty time")
    parts = text.split(":")
    if len(parts) > 3:
        raise ValueError("Too many ':' separators")
    total = 0.0
    for p in parts:
        if not re.fullmatch(r"\d+(\.\d+)?", p.strip()):
            raise ValueError(f"Invalid time: {text!r}")
        total = total * 60 + float(p)
    return total

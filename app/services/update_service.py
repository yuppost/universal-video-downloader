"""Check for a newer version of this app.

The app reads one small JSON file that you publish (for example on Google Drive or GitHub):

    {"version": "1.1.0", "url": "https://drive.google.com/...", "notes": "What is new"}

Nothing is installed automatically. The user is told that a new version exists and the download link is opened
in the browser only when the user clicks it. No user data is sent (a plain GET request).
"""

from __future__ import annotations

import json
import re
import urllib.request
from dataclasses import dataclass
from typing import Optional, Tuple
from urllib.parse import urlsplit


@dataclass
class UpdateInfo:
    version: str
    url: str
    notes: str = ""


def version_tuple(v: str) -> Tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", v or "")[:4]) or (0,)


def is_newer(remote: str, local: str) -> bool:
    a, b = version_tuple(remote), version_tuple(local)
    n = max(len(a), len(b))
    return a + (0,) * (n - len(a)) > b + (0,) * (n - len(b))


def resolve_source(source: str) -> str:
    """Accepts a plain https URL, ``github:<owner>/<repo>`` (its latest Release), ``drive:<file id>`` or a Google Drive share link; returns the direct download URL."""
    s = (source or "").strip()
    if not s:
        return ""
    if s.lower().startswith("github:"):
        return f"https://api.github.com/repos/{s[7:].strip().strip('/')}/releases/latest"
    if s.lower().startswith("drive:"):
        fid = s[6:].strip()
    else:
        m = re.search(r"drive\.google\.com/(?:file/d/|open\?id=|uc\?(?:[^#]*&)?id=)([\w-]{10,})", s)
        fid = m.group(1) if m else ""
    if fid:
        return f"https://drive.google.com/uc?export=download&id={fid}"
    return s


def parse_info(raw: bytes) -> Optional[UpdateInfo]:
    try:
        data = json.loads(raw.decode("utf-8-sig"))
        if "tag_name" in data:                        # GitHub "latest release" API answer
            if data.get("draft") or data.get("prerelease"):
                return None
            ver = str(data["tag_name"]).strip().lstrip("vV")
            url = str(data["html_url"]).strip()
            data = {"notes": data.get("body") or data.get("name") or ""}
        else:
            ver, url = str(data["version"]).strip(), str(data["url"]).strip()
    except (ValueError, KeyError, TypeError, UnicodeDecodeError, AttributeError):
        return None
    if not re.fullmatch(r"\d+(\.\d+){0,3}", ver) or urlsplit(url).scheme.lower() != "https":
        return None
    return UpdateInfo(ver, url, str(data.get("notes", ""))[:2000])


def fetch_update(source: str, current: str, timeout: int = 8) -> Optional[UpdateInfo]:
    """The newer release described at *source*, or None (no update, offline, bad file)."""
    url = resolve_source(source)
    if not url.lower().startswith("https://"):
        return None
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "UniversalVideoStudio"})
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 - https only, checked above
            raw = r.read(100_000)
    except Exception:  # noqa: BLE001 - an update check must never disturb the app
        return None
    info = parse_info(raw)
    return info if info and is_newer(info.version, current) else None

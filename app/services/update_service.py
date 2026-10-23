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
    url: str                      # page to open in the browser (release page)
    notes: str = ""
    asset_url: str = ""           # direct https link of the zip, enables the one-click update
    sha256: str = ""
    size: int = 0


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


def _pick_asset(assets) -> dict:
    zips = [a for a in (assets or []) if isinstance(a, dict) and str(a.get("name", "")).lower().endswith(".zip")]
    zips.sort(key=lambda a: ("windows" not in str(a.get("name", "")).lower(), str(a.get("name", ""))))
    return zips[0] if zips else {}


def parse_info(raw: bytes) -> Optional[UpdateInfo]:
    asset_url = sha = ""
    size = 0
    try:
        data = json.loads(raw.decode("utf-8-sig"))
        if "tag_name" in data:                        # GitHub "latest release" API answer
            if data.get("draft") or data.get("prerelease"):
                return None
            ver = str(data["tag_name"]).strip().lstrip("vV")
            url = str(data["html_url"]).strip()
            a = _pick_asset(data.get("assets"))
            asset_url = str(a.get("browser_download_url", ""))
            sha = str(a.get("digest", "")).split(":", 1)[-1] if str(a.get("digest", "")).startswith("sha256:") else ""
            size = int(a.get("size") or 0)
            data = {"notes": data.get("body") or data.get("name") or ""}
        else:
            ver, url = str(data["version"]).strip(), str(data["url"]).strip()
            asset_url, sha, size = str(data.get("zip", "")), str(data.get("sha256", "")), int(data.get("size") or 0)
    except (ValueError, KeyError, TypeError, UnicodeDecodeError, AttributeError):
        return None
    if not re.fullmatch(r"\d+(\.\d+){0,3}", ver) or urlsplit(url).scheme.lower() != "https":
        return None
    if asset_url and urlsplit(asset_url).scheme.lower() != "https":
        asset_url = ""
    if sha and not re.fullmatch(r"[0-9a-fA-F]{64}", sha):
        sha = ""
    return UpdateInfo(ver, url, str(data.get("notes", ""))[:2000], asset_url, sha.lower(), size)


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

"""Facebook posts (group posts, share links): find the post's own video instead of trusting yt-dlp's pick.

A group post page contains many videos (the post, stories, suggestions). yt-dlp can pick the wrong one. We read the
page with the user's saved sign-in, list the video ids in order of appearance and let yt-dlp inspect each one.
"""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, List, Optional, Tuple

_VIDEO_OBJ = re.compile(r'"__typename":"Video","id":"(\d{6,})"')
_VIDEO_ID_KEYS = re.compile(r'"(?:video_id|videoId|videoID)":"?(\d{6,})')
_POST_PATH = re.compile(r"^/(?:groups/[^/]+/(?:permalink|posts)/|share/|[^/]+/posts/|permalink\.php|story\.php)", re.I)
MAX_CANDIDATES = 6


def is_post_url(url: str) -> bool:
    """True for post-like Facebook links, where the id in the link is a post (not a video)."""
    from urllib.parse import urlsplit
    sp = urlsplit(url)
    host = (sp.hostname or "").lower()
    if not (host.endswith("facebook.com") or host.endswith("fb.com")):
        return False
    if sp.path.startswith(("/share/r/", "/reel/", "/watch")):
        return False
    return bool(_POST_PATH.match(sp.path))


def video_ids(page: str) -> List[str]:
    """Video ids in order of first appearance."""
    seen: Dict[str, int] = {}
    for rx in (_VIDEO_OBJ, _VIDEO_ID_KEYS):
        for m in rx.finditer(page):
            seen.setdefault(m.group(1), m.start())
    return [v for v, _ in sorted(seen.items(), key=lambda kv: kv[1])]


def fetch_page(ydl, url: str, limit: int = 6_000_000) -> Tuple[str, str]:
    """(final address, html) fetched with the same cookies / browser impersonation yt-dlp uses."""
    from yt_dlp.networking import Request
    resp = ydl.urlopen(Request(url))
    return resp.url, resp.read(limit).decode("utf-8", "replace")


def _label(info: dict, vid: str) -> str:
    t = (info.get("title") or "").strip()
    if t.lower() in ("", "facebook", "video"):
        d = (info.get("description") or "").strip().splitlines()
        t = d[0][:80] if d and d[0].strip() else f"Video {vid}"
    return t


def find_post_videos(url: str, opts: Dict[str, Any], extract: Callable[[Any, str], dict] = None) -> List[dict]:
    """Return info dicts (id, title, uploader, duration, url) of the videos found in a post page; [] when unsure."""
    import yt_dlp
    o = dict(opts, quiet=True, no_warnings=True, skip_download=True)
    try:
        with yt_dlp.YoutubeDL(o) as ydl:
            _final, page = fetch_page(ydl, url)
    except Exception:  # noqa: BLE001 - fall back to the normal analysis
        return []
    ids = video_ids(page)[:MAX_CANDIDATES]
    if not ids:
        return []

    def one(vid: str):
        run = extract or (lambda yy, uu: yy.extract_info(uu, download=False))
        for u in (f"https://www.facebook.com/watch/?v={vid}", f"https://www.facebook.com/video.php?v={vid}"):
            try:
                with yt_dlp.YoutubeDL(o) as y:
                    info = run(y, u)
                if not info or info.get("entries") or not (info.get("formats") or info.get("url")):
                    continue
                dur = info.get("duration")
                if not dur:
                    from app.services import ytdlp_service as yd
                    dur = yd._duration_from_formats(info) or page_duration(o, u, vid)
                return {"id": vid, "url": u, "title": _label(info, vid),
                        "uploader": info.get("uploader") or info.get("channel") or "", "duration": dur}
            except Exception:  # noqa: BLE001 - stories/ads that cannot be opened are simply not offered
                continue
        return None

    with ThreadPoolExecutor(max_workers=3) as pool:
        found = list(pool.map(one, ids))
    found = [f for f in found if f]
    if len(found) > 1:
        found[0]["title"] += "   <- first video of the post"
    return found


_DURATION_KEYS = [
    (re.compile(r'"playable_duration_in_ms":\s*(\d+)'), 0.001),
    (re.compile(r'"duration_in_ms":\s*(\d+)'), 0.001),
    (re.compile(r'"length_in_second":\s*"?([\d.]+)'), 1.0),
    (re.compile(r'"video_duration":\s*"?([\d.]+)'), 1.0),
    (re.compile(r'"playable_duration":\s*"?([\d.]+)'), 1.0),
    (re.compile(r'mediaPresentationDuration=\\?"PT(?:(\d+)H)?(?:(\d+)M)?([\d.]+)S'), None),
]


def duration_from_page(page: str, vid: str = "") -> Optional[float]:
    """Duration (seconds) that the page itself states for video *vid* (searched near the id, else anywhere)."""
    spots = [m.start() for m in re.finditer(re.escape(vid), page)] if vid else []
    windows = [page[p: p + 6000] for p in spots[:12]] + [page[:3_000_000]]
    for win in windows:
        for rx, scale in _DURATION_KEYS:
            m = rx.search(win)
            if not m:
                continue
            if scale is None:
                h, mi, sec = (float(x) if x else 0.0 for x in m.groups())
                val = h * 3600 + mi * 60 + sec
            else:
                val = float(m.group(1)) * scale
            if val > 0:
                return round(val, 3)
    return None


def page_duration(opts: Dict[str, Any], url: str, vid: str) -> Optional[float]:
    import yt_dlp
    o = dict(opts, quiet=True, no_warnings=True, skip_download=True)
    try:
        with yt_dlp.YoutubeDL(o) as ydl:
            _final, page = fetch_page(ydl, url)
    except Exception:  # noqa: BLE001
        return None
    return duration_from_page(page, vid)

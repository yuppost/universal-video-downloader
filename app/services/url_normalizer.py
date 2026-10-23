"""Turn the many link shapes that Facebook, X/Twitter and Instagram produce into forms yt-dlp understands.

Pure functions (no network). Typical problem: a link copied from a Facebook *group* such as
``/groups/<id>/videos/<video id>`` or ``/groups/<name>/?multi_permalinks=<post id>`` is not recognised by
yt-dlp's Facebook extractor, so it falls back to the generic extractor and fails with "Unsupported URL".
"""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_FB_HOST = re.compile(r"^(?:www\.|m\.|web\.|mbasic\.|touch\.|mobile\.|l\.|business\.)?(?:facebook\.com|fb\.com)$", re.I)
_TW_HOST = re.compile(r"^(?:www\.|mobile\.|m\.)?(?:twitter\.com|x\.com)$", re.I)
_IG_HOST = re.compile(r"^(?:www\.|m\.)?instagram\.com$", re.I)
_TRACKING = re.compile(r"^(?:__cft__.*|__tn__|__xts__.*|mibextid|fbclid|ref|sfnsn|refsrc|comment_id|"
                       r"notif_id|notif_t|utm_.*|igshid|igsh|s|t|ref_src|ref_url|ssr|rdid|share_url)$", re.I)
_ID = r"(\d{6,})"


def _clean_query(query: str, keep=()) -> str:
    pairs = [(k, v) for k, v in parse_qsl(query, keep_blank_values=True) if k in keep or not _TRACKING.match(k)]
    return urlencode(pairs)


def normalize_facebook(url: str) -> str:
    sp = urlsplit(url)
    path = sp.path or "/"
    q = dict(parse_qsl(sp.query))
    # fb.watch / share links: leave alone, yt-dlp follows the redirect itself
    if sp.hostname and sp.hostname.lower() in ("fb.watch",):
        return url
    base = "https://www.facebook.com"
    m = re.match(r"^/groups/([^/]+)/(?:videos|video)/(?:[^/]+/)?" + _ID, path)          # group video
    if m:
        return f"{base}/watch/?v={m.group(2)}"
    m = re.match(r"^/groups/([^/]+)/(?:permalink|posts)/" + _ID, path)                      # group post (supported)
    if m:
        return f"{base}/groups/{m.group(1)}/permalink/{m.group(2)}/"
    m = re.match(r"^/groups/([^/]+)/?$", path)                                              # ?multi_permalinks=ID
    if m:
        post = q.get("multi_permalinks") or q.get("story_fbid") or q.get("id")
        if post and re.fullmatch(r"\d{6,}", post):
            return f"{base}/groups/{m.group(1)}/permalink/{post}/"
    m = re.match(r"^/(?:[^/]+/)?(?:videos|reel|reels)/(?:[^/]+/)?" + _ID + r"/?$", path)   # page video / reel
    if m and "/groups/" not in path:
        return f"{base}/watch/?v={m.group(1)}" if "/reel" not in path else f"{base}/reel/{m.group(1)}"
    if path.startswith(("/watch", "/video.php", "/story.php", "/photo", "/share/", "/reel/")):
        keep = ("v", "video_id", "story_fbid", "id", "fbid", "set")
        return urlunsplit(("https", "www.facebook.com", path, _clean_query(sp.query, keep), ""))
    return urlunsplit(("https", "www.facebook.com", path, _clean_query(sp.query), ""))


def normalize_twitter(url: str) -> str:
    sp = urlsplit(url)
    m = re.match(r"^/i/(?:web/)?status/" + _ID, sp.path)
    if m:
        return f"https://x.com/i/status/{m.group(1)}"
    m = re.match(r"^/([^/]+)/status(?:es)?/" + _ID, sp.path)
    if m:
        return f"https://x.com/{m.group(1)}/status/{m.group(2)}"
    return url


def normalize_instagram(url: str) -> str:
    sp = urlsplit(url)
    m = re.match(r"^/(?:[^/]+/)?(?:share/)?(p|reel|reels|tv)/([A-Za-z0-9_-]+)", sp.path)
    if m:
        kind = "reel" if m.group(1) == "reels" else m.group(1)
        return f"https://www.instagram.com/{kind}/{m.group(2)}/"
    return url


def normalize_url(url: str) -> str:
    """Best-effort canonical form of *url*; unknown sites are returned unchanged."""
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return url
    try:
        if _FB_HOST.match(host):
            return normalize_facebook(url)
        if _TW_HOST.match(host):
            return normalize_twitter(url)
        if _IG_HOST.match(host):
            return normalize_instagram(url)
    except Exception:  # noqa: BLE001 - normalising must never break a download
        return url
    return url

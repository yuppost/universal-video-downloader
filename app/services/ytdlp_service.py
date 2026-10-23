"""yt-dlp integration: URL analysis, format selection, source and section downloads."""

from __future__ import annotations

import hashlib
import logging
import subprocess
import sys
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

from app.services.errors import FriendlyError, OperationCanceled
from app.utils.validation import looks_like_playlist, validate_url, UrlValidationError

from app.services import ytdlp_override as _override  # noqa: E402

_override.install_finder()       # packaged build: prefer a downloaded yt-dlp update (before any yt_dlp import)

log = logging.getLogger("uvs.ytdlp")

# status, task text, percent (None = unknown), bytes/s (None), eta seconds (None)
Reporter = Callable[[str, str, Optional[float], Optional[float], Optional[float]], None]
MEDIA_EXTS = {".mp4", ".mkv", ".webm", ".m4a", ".mp3", ".mov", ".flv", ".ts", ".opus", ".aac", ".ogg", ".wav"}


_COOKIE_SPEC = ""


def set_cookie_spec(spec: str) -> None:
    """Cookie source used by every yt-dlp call ('' | browser name | 'file:<path>'). Set by the UI."""
    global _COOKIE_SPEC
    _COOKIE_SPEC = spec or ""


def get_cookie_spec() -> str:
    return _COOKIE_SPEC


def ejs_installed() -> bool:
    try:
        import yt_dlp_ejs  # noqa: F401
        return True
    except ImportError:
        return False


def ytdlp_version() -> str:
    """Installed yt-dlp version, read without importing yt-dlp (importing it takes ~1 s)."""
    if getattr(_override, "_installed", False):
        v = _override.override_version()
        if v:
            return v
    try:
        from importlib.metadata import version
        return version("yt-dlp")
    except Exception:  # noqa: BLE001 - metadata can be missing (frozen app): import it instead
        pass
    try:
        from yt_dlp.version import __version__
        return __version__
    except Exception:  # pragma: no cover
        return "not installed"


def prewarm() -> None:
    """Import yt-dlp's big extractor tree in the background so the first Analyze click is fast."""
    try:
        import yt_dlp.extractor  # noqa: F401
        import yt_dlp.YoutubeDL  # noqa: F401
    except Exception:  # noqa: BLE001
        pass


def update_ytdlp() -> Tuple[bool, str]:
    """Upgrade yt-dlp with pip inside the current environment. Not possible in a frozen build."""
    if getattr(sys, "frozen", False):
        from app.services import ytdlp_override
        try:
            return True, ytdlp_override.update_packaged()
        except FriendlyError as exc:
            return False, exc.message
        except Exception as exc:  # noqa: BLE001
            return False, f"Update failed: {exc}"
    try:
        res = subprocess.run([sys.executable, "-m", "pip", "install", "--upgrade", "yt-dlp[default]"],
                             capture_output=True, text=True, timeout=300,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"Update failed: {exc}"
    if res.returncode != 0:
        return False, "pip could not update yt-dlp:\n" + res.stderr[-600:]
    return True, "yt-dlp was updated. Restart the application to use the new version."


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------
@dataclass
class FormatInfo:
    format_id: str
    ext: str
    height: Optional[int]
    width: Optional[int]
    fps: Optional[float]
    vcodec: Optional[str]
    acodec: Optional[str]
    filesize: Optional[int]
    tbr: Optional[float]
    protocol: str = ""
    url: str = ""

    @property
    def has_video(self) -> bool:
        return bool(self.vcodec) and self.vcodec != "none"

    @property
    def has_audio(self) -> bool:
        return bool(self.acodec) and self.acodec != "none"


@dataclass
class VideoInfo:
    url: str
    id: str
    title: str
    uploader: str
    duration: Optional[float]
    thumbnail_url: str
    extractor: str
    formats: List[FormatInfo] = field(default_factory=list)
    thumbnail_bytes: Optional[bytes] = None
    preview_url: Optional[str] = None
    is_live: bool = False
    drm_only: bool = False

    @property
    def heights(self) -> List[int]:
        return sorted({f.height for f in self.formats if f.has_video and f.height}, reverse=True)

    @property
    def source_size(self) -> Tuple[int, int]:
        """Largest known frame size, used for crop geometry before any file exists."""
        best = max((f for f in self.formats if f.has_video and f.width and f.height),
                   key=lambda f: (f.height or 0), default=None)
        return (best.width, best.height) if best else (1920, 1080)

    def estimate_size(self, max_height: Optional[int]) -> Optional[int]:
        """Approximate download size for the video+audio chosen for *max_height* (if known)."""
        vids = [f for f in self.formats if f.has_video and f.height and (max_height is None or f.height <= max_height)]
        if not vids:
            return None
        v = max(vids, key=lambda f: (f.height or 0, f.tbr or 0))
        total = v.filesize or 0
        if not v.has_audio:
            auds = [f for f in self.formats if f.has_audio and not f.has_video]
            if auds:
                total += (max(auds, key=lambda f: f.tbr or 0).filesize or 0)
        return total or None


@dataclass
class PlaylistEntry:
    url: str
    title: str
    duration: Optional[float]


@dataclass
class PlaylistInfo:
    url: str
    title: str
    entries: List[PlaylistEntry]
    single_url_available: bool = False
    kind: str = "playlist"            # "playlist" | "post" (one page/post that contains several videos)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def friendly_ytdlp_error(exc: BaseException) -> FriendlyError:
    """Map a yt-dlp/network exception to a short, understandable message (+ a hint for the UI)."""
    from app.services.signin_service import classify, explain_cookie_error
    raw = str(exc)
    t = raw.lower()
    hint = classify(raw)
    if hint == "cookies":
        return FriendlyError(explain_cookie_error(raw), raw[-800:], "cookies")
    if hint == "js":
        return FriendlyError("YouTube needs a JavaScript runtime to unlock this video. Open Setup and download "
                             "Deno (one click), then try again.", raw[-800:], "js")
    if "cannot parse data" in t and "facebook" in t:
        if impersonate_target() is None:
            msg = ("Facebook only answers browser-like requests, and the browser-impersonation component (curl_cffi) is "
                   "missing. Run run.bat again (it installs it) or rebuild with build.bat, then retry.")
        else:
            msg = ("Facebook did not return the video data. The sign-in may be missing or expired, your account may not be "
                   "allowed to see this video (for a group video you must be a member), or yt-dlp needs an update.")
        return FriendlyError(msg, raw[-800:], "signin")
    rules = [
        ("needs to be reloaded", "YouTube returned a temporary error (\"page needs to be reloaded\"). Try again; if it "
                                 "keeps happening, update yt-dlp and sign in with your browser."),
        ("not a bot", "YouTube asks you to prove you are not a bot. Sign in with your browser (it uses your own account)."),
        ("unsupported url", "This website or link type is not supported by the installed yt-dlp. Try updating yt-dlp."),
        ("drm", "This video is protected by DRM and cannot be downloaded."),
        ("sign in", "This video requires you to be signed in. Use 'Sign in with browser' if you are allowed to watch it."),
        ("log in", "This video requires you to be signed in."),
        ("login", "This video requires you to be signed in."),
        ("private video", "This video is private. Sign in with an account that has access."),
        ("members-only", "This video is for members only. Sign in with an account that has access."),
        ("age-restricted", "This video is age-restricted and requires a signed-in session."),
        ("confirm your age", "This video is age-restricted and requires a signed-in session."),
        ("not available in your country", "This video is not available in your region."),
        ("geo", "This video is region-restricted."),
        ("video unavailable", "This video is unavailable (removed, private or blocked). Updating yt-dlp or signing in can help."),
        ("http error 404", "The video was not found (HTTP 404)."),
        ("http error 403", "The server refused access (HTTP 403). Signing in with your browser or updating yt-dlp usually fixes it."),
        ("http error 429", "Too many requests - the site is rate limiting you. Wait a while and retry, or sign in."),
        ("no space left", "The disk is full. Free up space or change the temporary folder in Settings."),
        ("unable to download webpage", "Could not reach the website. Check your internet connection."),
        ("timed out", "The connection timed out. Check your internet connection and retry."),
        ("name or service not known", "Could not resolve the website address. Check your connection."),
        ("getaddrinfo failed", "Could not resolve the website address. Check your connection."),
        ("ffmpeg", "FFmpeg is required for this operation but could not be used. Open Setup to install it."),
        ("requested format is not available", "The requested quality/format is not available for this video."),
    ]
    for needle, msg in rules:
        if needle in t:
            return FriendlyError(msg, raw[-800:], hint)
    first = raw.splitlines()[0][:200] if raw else "Unknown error."
    return FriendlyError("The video could not be processed: " + first, raw[-800:], hint)


def run_cancellable(fn: Callable[[], Any], cancel: threading.Event) -> Any:
    """Run a blocking call in a helper thread and stop *waiting* when canceled.

    yt-dlp's metadata extraction cannot be interrupted mid-request; on cancel the
    request is abandoned (its thread ends on its own) and its result is discarded.
    """
    box: Dict[str, Any] = {}

    def target() -> None:
        try:
            box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 - re-raised in caller
            box["error"] = exc

    t = threading.Thread(target=target, daemon=True)
    t.start()
    while t.is_alive():
        t.join(0.1)
        if cancel.is_set():
            raise OperationCanceled()
    if "error" in box:
        raise box["error"]
    return box.get("value")


_IMPERSONATE_HOSTS = ("facebook.com", "fb.watch", "fb.com")


def needs_impersonation(url: str) -> bool:
    """Facebook rejects cookie-carrying requests whose TLS/HTTP fingerprint is not a real browser's
    ("Cannot parse data"), so these links are fetched with browser impersonation (curl_cffi)."""
    from urllib.parse import urlsplit
    host = (urlsplit(url).hostname or "").lower()
    return any(host == h or host.endswith("." + h) for h in _IMPERSONATE_HOSTS)


def impersonate_target():
    """A yt-dlp ImpersonateTarget for Chrome, or None when curl_cffi is not available."""
    import importlib.util
    if importlib.util.find_spec("curl_cffi") is None:
        return None
    try:
        from yt_dlp.networking.impersonate import ImpersonateTarget
        return ImpersonateTarget.from_str("chrome")
    except Exception:  # noqa: BLE001
        return None


def base_options(cookies_browser: str = "", ffmpeg_path: str = "", url: str = "") -> Dict[str, Any]:
    """Common yt-dlp options. ``ignoreconfig`` stops user config files injecting options."""
    opts: Dict[str, Any] = {
        "quiet": True, "no_warnings": True, "noprogress": True, "ignoreconfig": True,
        "socket_timeout": 20, "retries": 3, "fragment_retries": 3, "no_color": True,
        "noplaylist": True, "allow_unplayable_formats": False,
    }
    spec = cookies_browser or _COOKIE_SPEC
    vault_file = None
    if url and not cookies_browser:
        try:                                    # sign-ins saved in the app's cookie vault win for their own site
            from app.services.cookie_vault import get_vault
            vault_file = get_vault().cookiefile_for(url)
        except Exception:  # noqa: BLE001 - never let a vault problem break a download
            vault_file = None
    if vault_file:
        opts["cookiefile"] = str(vault_file)
    elif spec.startswith("file:"):
        opts["cookiefile"] = spec[5:]
    elif spec:
        opts["cookiesfrombrowser"] = (spec,)
    from app.services.jsruntime import js_runtimes_option
    rt = js_runtimes_option()
    if rt:
        opts["js_runtimes"] = rt                 # needed by YouTube's challenge solver
    opts["extractor_retries"] = 3
    if url and needs_impersonation(url):
        tgt = impersonate_target()
        if tgt is not None:
            opts["impersonate"] = tgt
    if ffmpeg_path:
        opts["ffmpeg_location"] = str(Path(ffmpeg_path).parent)
    return opts


def _fetch_bytes(url: str, limit: int = 4_000_000, timeout: int = 10) -> Optional[bytes]:
    if not url.lower().startswith(("http://", "https://")):
        return None
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 - scheme checked above
            data = r.read(limit + 1)
        return data if len(data) <= limit else None
    except Exception:  # noqa: BLE001 - thumbnails are optional
        return None


def _parse_formats(info: Dict[str, Any]) -> Tuple[List[FormatInfo], bool]:
    out: List[FormatInfo] = []
    drm_count = 0
    raw_formats = info.get("formats") or []
    for f in raw_formats:
        if f.get("has_drm") or f.get("drm"):
            drm_count += 1
            continue
        vc, ac = f.get("vcodec"), f.get("acodec")
        if (vc in (None, "none")) and (ac in (None, "none")):
            continue  # storyboards etc.
        out.append(FormatInfo(
            format_id=str(f.get("format_id", "")), ext=f.get("ext", ""), height=f.get("height"),
            width=f.get("width"), fps=f.get("fps"), vcodec=vc, acodec=ac,
            filesize=f.get("filesize") or f.get("filesize_approx"), tbr=f.get("tbr"),
            protocol=f.get("protocol", ""), url=f.get("url", "")))
    return out, bool(raw_formats) and drm_count == len(raw_formats)


def probe_duration(src: str, headers: Optional[Dict[str, str]] = None, timeout: int = 25,
                   debug: Optional[List[str]] = None) -> Optional[float]:
    """Duration in seconds read from the media itself with ffprobe (None if it cannot be read)."""
    from app.services.ffmpeg_service import find_executable
    ffprobe = find_executable("ffprobe")
    if not ffprobe:
        return None
    cmd = [ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1"]
    if src.lower().startswith(("http://", "https://")):
        h = dict(headers or {})
        ua = h.pop("User-Agent", None) or h.pop("user-agent", None)
        if ua:
            cmd += ["-user_agent", ua]
        if h:
            cmd += ["-headers", "".join(f"{k}: {v}\r\n" for k, v in h.items())]
        cmd += ["-rw_timeout", str(timeout * 1_000_000)]
    cmd.append(src)
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 5,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if debug is not None:
            debug.append(f"ffprobe rc={res.returncode} out={res.stdout.strip()[:60]!r} err={res.stderr.strip()[:160]!r}")
        val = float(res.stdout.strip().splitlines()[0])
        return val if val > 0 else None
    except (OSError, subprocess.SubprocessError, ValueError, IndexError) as exc:
        if debug is not None:
            debug.append(f"ffprobe failed: {exc}")
        return None


def _duration_from_formats(info: Dict[str, Any], debug: Optional[List[str]] = None) -> Optional[float]:
    """Facebook (and a few other sites) give no duration in the metadata: read it from the stream itself."""
    for f in info.get("formats") or []:                 # some extractors put it on the format instead
        if f.get("duration"):
            return float(f["duration"])
    raw = [f for f in (info.get("formats") or []) if f.get("url") and str(f.get("url")).startswith("http")]
    raw.sort(key=lambda f: (f.get("vcodec") in (None, "none"), f.get("height") or 0))      # smallest video first = fast
    sources = [(f["url"], f.get("http_headers")) for f in raw[:3]]
    for f in (info.get("formats") or []):                # DASH manifests carry the total duration
        mu = f.get("manifest_url")
        if mu and all(mu != u for u, _ in sources):
            sources.append((mu, f.get("http_headers")))
            break
    for u, h in sources[:4]:
        d = probe_duration(u, h, debug=debug)
        if d:
            return d
    return None


def _duration_from_page(opts: Dict[str, Any], url: str, info: Dict[str, Any]) -> Optional[float]:
    from app.services import fb_post
    if "facebook" not in str(info.get("extractor") or "").lower():
        return None
    return fb_post.page_duration(opts, url, str(info.get("id") or ""))


def _nice_title(info: Dict[str, Any]) -> str:
    t = (info.get("title") or "").strip()
    if t.lower() in ("", "facebook", "video", "facebook video"):
        desc = (info.get("description") or "").strip().splitlines()
        who = info.get("uploader") or info.get("channel") or ""
        t = (desc[0][:90] if desc and desc[0].strip() else
             (f"Facebook video by {who}" if who else f"Video {info.get('id', '')}".strip()))
    return t or "video"


def choose_preview_url(formats: List[FormatInfo], max_height: int = 720) -> Optional[str]:
    """Pick a progressive (video+audio in one file) http(s) URL that Qt can stream."""
    prog = [f for f in formats if f.has_video and f.has_audio and f.url
            and f.protocol in ("http", "https") and (f.height or 0) <= max_height
            and f.ext in ("mp4", "webm", "mov")]
    if not prog:
        return None
    return max(prog, key=lambda f: ((f.ext == "mp4"), f.height or 0)).url


def analyze_url(url: str, mode: str = "auto", cookies_browser: str = "",
                cancel: Optional[threading.Event] = None,
                fetch_thumbnail: bool = True) -> Union[VideoInfo, PlaylistInfo]:
    """Analyse *url*. ``mode``: "single" (never a playlist), "playlist", or "auto".

    In "auto" mode a URL that looks like a playlist returns a :class:`PlaylistInfo`
    so the caller can ask the user what to do.
    """
    try:
        import yt_dlp
    except ImportError as exc:
        raise FriendlyError("yt-dlp is not installed. Open Settings > Setup (FFmpeg / yt-dlp) to install it.") from exc

    from app.services.url_normalizer import normalize_url
    url = normalize_url(validate_url(url))     # group / mobile / tracking-parameter links -> forms yt-dlp knows
    cancel = cancel or threading.Event()
    want_playlist = mode == "playlist" or (mode == "auto" and looks_like_playlist(url))

    def work() -> Union[VideoInfo, PlaylistInfo]:
        opts = base_options(cookies_browser, url=url)
        if want_playlist:
            opts.update({"noplaylist": False, "extract_flat": "in_playlist", "skip_download": True})
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
            if info and info.get("entries"):
                entries = []
                for e in info["entries"]:
                    if not e:
                        continue
                    eu = e.get("url") or e.get("webpage_url") or ""
                    if eu and not eu.startswith("http") and e.get("id") and "youtube" in (info.get("extractor") or ""):
                        eu = f"https://www.youtube.com/watch?v={e['id']}"
                    try:
                        eu = validate_url(eu)
                    except UrlValidationError:
                        continue
                    entries.append(PlaylistEntry(eu, e.get("title") or eu, e.get("duration")))
                if entries:
                    return PlaylistInfo(url, info.get("title") or "Playlist", entries,
                                        single_url_available="v=" in url)
        opts = base_options(cookies_browser, url=url)
        opts["skip_download"] = True
        from app.services import fb_post
        eff = url
        if fb_post.is_post_url(url):          # a Facebook post: use the video(s) the post page really contains
            vids = fb_post.find_post_videos(url, opts)
            if len(vids) > 1:
                picks = [PlaylistEntry(v["url"], v["title"] + (f"  -  {v['uploader']}" if v["uploader"] else ""),
                                       v["duration"]) for v in vids]
                return PlaylistInfo(url, "Videos found in this Facebook post", picks, False, "post")
            if vids:
                eff = vids[0]["url"]
        info = None
        for attempt in range(3):   # YouTube sometimes answers "page needs to be reloaded"; a retry usually works
            try:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(eff, download=False)
                break
            except Exception as exc:  # noqa: BLE001
                if "needs to be reloaded" not in str(exc).lower() or attempt == 2 or cancel.is_set():
                    raise
                time.sleep(1.5)
        if not info:
            raise FriendlyError("No video information was returned for this link.")
        if info.get("entries"):  # single-video mode but page yielded entries
            found = [e for e in info["entries"] if e]
            if len(found) > 1 and "youtube" not in (info.get("extractor") or "").lower():
                # A Facebook post/page can hold several videos (also suggested ones). Never guess: let the user pick.
                picks, seen = [], set()
                for e in found:
                    vid = str(e.get("id") or "")
                    if vid in seen:
                        continue
                    seen.add(vid)
                    eu = (f"https://www.facebook.com/watch/?v={vid}"
                          if vid.isdigit() and "facebook" in (info.get("extractor") or "").lower()
                          else (e.get("webpage_url") or e.get("original_url") or url))
                    who = e.get("uploader") or e.get("channel") or ""
                    label = _nice_title(e) + (f"  -  {who}" if who else "")
                    picks.append(PlaylistEntry(eu, label, e.get("duration")))
                if len(picks) > 1:
                    return PlaylistInfo(url, info.get("title") or "Videos found on this page", picks, False, "post")
            info = found[0] if found else info
        formats, drm_only = _parse_formats(info)
        if drm_only:
            raise FriendlyError("This video is protected by DRM and cannot be downloaded.")
        vi = VideoInfo(
            url=eff, id=str(info.get("id", "")), title=_nice_title(info),
            uploader=info.get("uploader") or info.get("channel") or "",
            duration=info.get("duration") or _duration_from_formats(info) or _duration_from_page(opts, eff, info),
            thumbnail_url=info.get("thumbnail") or "", extractor=info.get("extractor_key") or info.get("extractor") or "",
            formats=formats, is_live=bool(info.get("is_live")))
        vi.preview_url = choose_preview_url(formats)
        return vi

    try:
        result = run_cancellable(work, cancel)
    except OperationCanceled:
        raise
    except FriendlyError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise friendly_ytdlp_error(exc) from exc
    if isinstance(result, VideoInfo):
        if result.is_live:
            raise FriendlyError("Live streams are not supported. Try again after the stream has ended.")
        if not result.duration:
            raise FriendlyError("This video has no known duration, so it cannot be trimmed on a timeline.")
        if fetch_thumbnail and result.thumbnail_url:
            result.thumbnail_bytes = _fetch_bytes(result.thumbnail_url)
    return result


# ---------------------------------------------------------------------------
# Quality helpers (pure)
# ---------------------------------------------------------------------------
def resolve_quality(requested: Optional[int], available: List[int]) -> Tuple[Optional[int], str]:
    """Return (height to use, explanation). Never pretends to upscale."""
    if requested is None or not available:
        return (None, "")
    if requested in available:
        return (requested, "")
    lower = [h for h in available if h < requested]
    if lower:
        h = max(lower)
        return (h, f"{requested}p is not available for this video; using the best available up to {requested}p ({h}p).")
    h = min(available)
    return (h, f"{requested}p is not available; the lowest available quality is {h}p, which will be used.")


def build_format_selector(max_height: Optional[int], audio_only: bool = False) -> str:
    """yt-dlp format string. Prefers H.264/AAC (copy-friendly) up to 1080p."""
    if audio_only:
        return "ba/b"
    h = f"[height<={max_height}]" if max_height else ""
    if max_height is None or max_height > 1080:
        return f"bv*{h}+ba/b{h}/bv*+ba/b"
    return f"bv*[vcodec^=avc1]{h}+ba[ext=m4a]/bv*{h}+ba/b{h}/bv*+ba/b"


def source_key(url: str, selector: str) -> str:
    """Stable, filesystem-safe cache key for (url, format selector)."""
    return "src_" + hashlib.sha1(f"{url}|{selector}".encode()).hexdigest()[:16]


def find_cached_source(cache_dir: Path, key: str) -> Optional[Path]:
    """Return a finished cached download for *key*, ignoring partial/intermediate files."""
    if not cache_dir.is_dir():
        return None
    for p in sorted(cache_dir.glob(f"{key}.*")):
        if p.stem == key and p.suffix.lower() in MEDIA_EXTS and p.stat().st_size > 0:
            return p
    return None


# ---------------------------------------------------------------------------
# Downloads
# ---------------------------------------------------------------------------
def _hooks(report: Reporter, cancel: threading.Event, label: str):
    def progress(d: Dict[str, Any]) -> None:
        if cancel.is_set():
            raise OperationCanceled()
        if d.get("status") == "downloading":
            done = d.get("downloaded_bytes")
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            pct = (done / total * 100.0) if (done is not None and total) else None
            info = d.get("info_dict") or {}
            kind = ""
            if info.get("vcodec") in (None, "none") and info.get("acodec") not in (None, "none"):
                kind = " (audio stream)"
            elif info.get("vcodec") not in (None, "none") and info.get("acodec") in (None, "none"):
                kind = " (video stream)"
            report("Downloading", f"{label}{kind}", pct, d.get("speed"), d.get("eta"))

    def post(d: Dict[str, Any]) -> None:
        if cancel.is_set():
            raise OperationCanceled()
        if d.get("status") == "started":
            report("Converting", f"Post-processing: {d.get('postprocessor', '')}", None, None, None)

    return progress, post


def download_source(url: str, selector: str, cache_dir: Path, key: str, ffmpeg_path: str,
                    cookies_browser: str, report: Reporter, cancel: threading.Event) -> Path:
    """Download (or reuse) the full source media for *url*. Resumes partial files."""
    import yt_dlp

    cached = find_cached_source(cache_dir, key)
    if cached:
        report("Downloading", "Using previously downloaded source", 100.0, None, None)
        return cached
    cache_dir.mkdir(parents=True, exist_ok=True)
    progress, post = _hooks(report, cancel, "Downloading source video")
    opts = base_options(cookies_browser, ffmpeg_path, url)
    opts.update({"format": selector, "outtmpl": str(cache_dir / f"{key}.%(ext)s"),
                 "merge_output_format": "mp4/mkv", "progress_hooks": [progress],
                 "postprocessor_hooks": [post], "continuedl": True, "overwrites": False})
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
    except OperationCanceled:
        raise
    except Exception as exc:  # noqa: BLE001
        if cancel.is_set():
            raise OperationCanceled() from exc
        raise friendly_ytdlp_error(exc) from exc
    if cancel.is_set():
        raise OperationCanceled()
    path = find_cached_source(cache_dir, key)
    if not path:
        raise FriendlyError("The download finished but no media file was found.")
    return path


def download_section(url: str, selector: str, start: float, end: float, dest_dir: Path, name: str,
                     accurate: bool, ffmpeg_path: str, cookies_browser: str,
                     report: Reporter, cancel: threading.Event) -> Path:
    """Fetch only [start, end] using yt-dlp's section download (needs FFmpeg).

    Whether bytes outside the range are skipped depends on the site's stream format.
    """
    import yt_dlp
    from yt_dlp.utils import download_range_func

    dest_dir.mkdir(parents=True, exist_ok=True)
    progress, post = _hooks(report, cancel, "Downloading section")
    opts = base_options(cookies_browser, ffmpeg_path, url)
    opts.update({"format": selector, "outtmpl": str(dest_dir / f"{name}.%(ext)s"),
                 "merge_output_format": "mp4/mkv", "progress_hooks": [progress],
                 "postprocessor_hooks": [post],
                 "download_ranges": download_range_func(None, [(start, end)]),
                 "force_keyframes_at_cuts": accurate, "overwrites": True})
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
    except OperationCanceled:
        raise
    except Exception as exc:  # noqa: BLE001
        if cancel.is_set():
            raise OperationCanceled() from exc
        raise friendly_ytdlp_error(exc) from exc
    if cancel.is_set():
        raise OperationCanceled()
    found = find_cached_source(dest_dir, name)
    if not found:
        raise FriendlyError("The section download finished but no media file was found.")
    return found


def latest_ytdlp_version(timeout: int = 6) -> Optional[str]:
    """Ask PyPI for the newest yt-dlp release (None when offline). Sends no user data."""
    import json
    try:
        req = urllib.request.Request("https://pypi.org/pypi/yt-dlp/json", headers={"User-Agent": "UniversalVideoStudio"})
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 - fixed https URL
            return json.loads(r.read(2_000_000))["info"]["version"]
    except Exception:  # noqa: BLE001 - purely informational
        return None


def version_tuple(v: str) -> Tuple[int, ...]:
    out = []
    for part in v.split("."):
        digits = "".join(ch for ch in part if ch.isdigit())
        out.append(int(digits) if digits else 0)
    return tuple(out)

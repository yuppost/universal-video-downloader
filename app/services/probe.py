"""Diagnostics for a link that will not analyse: ``run.bat --probe "<url>"``.

Prints what the site really returned (redirect target, which videos the page contains, what yt-dlp makes of them)
and writes the same text to ``logs/probe.txt`` so it can be sent for troubleshooting. Cookie values and the page
itself are never written out - only video ids, titles and owner names found in it.
"""

from __future__ import annotations

import html as htmllib
import re
import traceback
from pathlib import Path
from typing import List

_META = re.compile(r'<meta[^>]+(?:property|name)="(og:title|og:url|og:video|og:video:url|og:description)"[^>]+content="([^"]*)"', re.I)
_VIDEO_OBJ = re.compile(r'"__typename":"Video","id":"(\d{6,})"')
_VIDEO_ID_KEYS = re.compile(r'"(?:video_id|videoId|videoID)":"?(\d{6,})')
_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.S | re.I)


def scan_page(page: str, limit: int = 40) -> List[str]:
    """Human-readable lines describing the videos and meta tags found in a Facebook-like page."""
    out: List[str] = []
    m = _TITLE.search(page)
    if m:
        out.append("page title : " + htmllib.unescape(m.group(1)).strip()[:200])
    for k, v in _META.findall(page):
        out.append(f"meta {k}: {htmllib.unescape(v)[:200]}")
    seen: dict = {}
    for rx in (_VIDEO_OBJ, _VIDEO_ID_KEYS):
        for mm in rx.finditer(page):
            vid = mm.group(1)
            if vid not in seen:
                seen[vid] = mm.start()
    out.append(f"video ids found in page, in order of appearance ({len(seen)}):")
    for vid, pos in sorted(seen.items(), key=lambda kv: kv[1])[:limit]:
        ctx = page[pos: pos + 1500]
        title = re.search(r'"title":\{"text":"([^"]{1,120})"', ctx) or re.search(r'"name":"([^"]{1,80})"', ctx)
        out.append(f"  {vid}  @{pos}  {title.group(1) if title else ''}")
        around = page[max(0, pos - 4000): pos + 3000]
        owners = re.findall(r'"__typename":"(?:User|Page|Group)","id":"(\d+)"', around)
        names = re.findall(r'"name":"([^"]{2,60})"', around)
        if owners or names:
            out.append(f"        near: ids={sorted(set(owners))[:6]} names={list(dict.fromkeys(names))[:6]}")
    return out


def run_probe(url: str) -> str:
    from app.services import ytdlp_service as yd
    from app.services.url_normalizer import normalize_url
    from app.utils.validation import validate_url
    from app import __version__
    lines: List[str] = [f"app version: {__version__}  (post-aware probe v2)", f"input      : {url}"]
    try:
        import yt_dlp
        from yt_dlp.networking import Request
        url = validate_url(url)
        norm = normalize_url(url)
        lines.append(f"normalised : {norm}")
        lines.append(f"yt-dlp     : {yd.ytdlp_version()}   impersonation: {'on' if yd.impersonate_target() else 'OFF (curl_cffi missing)'}")
        opts = yd.base_options(url=norm)
        lines.append(f"cookies    : {'saved sign-in in use' if opts.get('cookiefile') else 'none for this site'}")
        opts.update(quiet=True, no_warnings=True)
        with yt_dlp.YoutubeDL(opts) as ydl:
            try:
                resp = ydl.urlopen(Request(norm))
                final = resp.url
                page = resp.read(6_000_000).decode("utf-8", "replace")
                lines.append(f"page fetch : HTTP {resp.status}, final address: {final}, {len(page)} characters")
                if final != norm:
                    lines.append(f"normalised final: {normalize_url(final)}")
                lines += scan_page(page)
                if re.search(r'id="login_form"|royal_login_form|/login/\?next', page):
                    lines.append("!! the page is a LOGIN page: the saved sign-in was not accepted")
                from app.services import fb_post
                if fb_post.is_post_url(norm):
                    lines.append("--- each video id of the post, opened one by one ---")
                    for vid in fb_post.video_ids(page)[:fb_post.MAX_CANDIDATES]:
                        for u in (f"https://www.facebook.com/watch/?v={vid}", f"https://www.facebook.com/video.php?v={vid}"):
                            try:
                                with yt_dlp.YoutubeDL(dict(opts, skip_download=True)) as y:
                                    inf = y.extract_info(u, download=False)
                                lines.append(f"  OK   {u}  title={inf.get('title')!r} uploader={inf.get('uploader')!r} "
                                             f"duration={inf.get('duration')} id={inf.get('id')}")
                                if not inf.get("duration"):
                                    dbg: List[str] = []
                                    d1 = yd._duration_from_formats(inf, dbg)
                                    lines.append(f"       duration from stream (ffprobe): {d1}")
                                    for fm in (inf.get("formats") or [])[:8]:
                                        lines.append(f"       format {fm.get('format_id')} {fm.get('ext')} {fm.get('protocol')} "
                                                     f"v={fm.get('vcodec')} h={fm.get('height')} url={'yes' if fm.get('url') else 'no'} "
                                                     f"manifest={'yes' if fm.get('manifest_url') else 'no'}")
                                    lines += ["       " + x for x in dbg[:6]]
                                    try:
                                        _f, pg = fb_post.fetch_page(y, u)
                                        lines.append(f"       duration stated in the video page: {fb_post.duration_from_page(pg, vid)}")
                                    except Exception as exc2:  # noqa: BLE001
                                        lines.append(f"       video page fetch failed: {exc2}")
                                break
                            except Exception as exc:  # noqa: BLE001
                                lines.append(f"  FAIL {u}  {str(exc).splitlines()[0][:220]}")
            except Exception as exc:  # noqa: BLE001
                lines.append(f"page fetch failed: {exc}")
        lines.append("--- yt-dlp analysis (what the app shows) ---")
        try:
            res = yd.analyze_url(norm, "single", fetch_thumbnail=False)
            if isinstance(res, yd.PlaylistInfo):
                lines.append(f"several videos ({res.kind}): {res.title}")
                for e in res.entries:
                    lines.append(f"  {e.url}  {e.title}  [{e.duration}]")
            else:
                lines.append(f"video: id={res.id} title={res.title!r} uploader={res.uploader!r} duration={res.duration} "
                             f"extractor={res.extractor} formats={len(res.formats)}")
        except Exception as exc:  # noqa: BLE001
            lines.append(f"analysis failed: {getattr(exc, 'message', exc)}")
            d = getattr(exc, "details", "")
            if d:
                lines.append("details: " + str(d)[:600])
    except Exception:  # noqa: BLE001
        lines.append(traceback.format_exc())
    text = "\n".join(lines)
    try:
        from app.utils.paths import app_data_dir
        p = Path(app_data_dir()) / "logs" / "probe.txt"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        text += f"\n\n(saved to {p})"
    except OSError:
        pass
    return text

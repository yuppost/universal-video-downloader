"""Cookie vault, site matching, yt-dlp cookie selection and sign-in error hints (no network)."""
import os
import time

import pytest

from app.services import cookie_vault as cv
from app.services import signin_service as ss
from app.services import ytdlp_service as yd
from app.services.errors import FriendlyError


@pytest.fixture
def vault(tmp_path):
    v = cv.CookieVault(tmp_path / "cookies")
    cv.set_vault(v)
    yield v
    cv.set_vault(None)


def rec(domain, name, value="v", expires=None, **kw):
    return cv.CookieRec(domain, "/", name, value, expires=int(time.time()) + 86400 if expires is None else expires, **kw)


# ---- sites ---------------------------------------------------------------------
@pytest.mark.parametrize("url,key", [
    ("https://www.youtube.com/watch?v=x", "youtube.com"), ("https://youtu.be/x", "youtube.com"),
    ("https://accounts.google.com/", "youtube.com"), ("https://m.facebook.com/watch/?v=1", "facebook.com"),
    ("https://fb.watch/abc", "facebook.com"), ("https://www.bbc.co.uk/iplayer", "bbc.co.uk"),
    ("https://player.vimeo.com/video/1", "vimeo.com"), ("http://127.0.0.1:8000/x", "127.0.0.1"),
    ("example.org", "example.org"), ("", ""),
])
def test_site_key_groups_aliases(url, key):
    assert cv.site_key(url) == key


def test_login_url_for_known_and_unknown_sites():
    assert "accounts.google.com" in cv.login_url_for("https://youtu.be/x")
    assert cv.login_url_for("https://www.facebook.com/watch/?v=1").endswith("facebook.com/login")
    assert cv.login_url_for("https://media.example.com/v/1") == "https://media.example.com/v/1"
    assert cv.login_url_for("example.com") == "https://example.com"


def test_filter_for_site_drops_foreign_and_tracker_cookies():
    cookies = [rec(".youtube.com", "a"), rec(".google.com", "b"), rec("doubleclick.net", "t"), rec(".evilyoutube.com", "e")]
    assert [c.name for c in cv.filter_for_site(cookies, "youtube.com")] == ["a", "b"]    # suffix match is label-aware


# ---- Netscape file ---------------------------------------------------------------
def test_netscape_roundtrip_httponly_session_and_bad_values(tmp_path):
    f = tmp_path / "c.txt"
    n = cv.write_netscape(f, [rec(".a.com", "x", "1", secure=True), rec("a.com", "h", "2", http_only=True, expires=0),
                              rec(".a.com", "bad", "tab\there"), rec(".a.com", "bad2", "line\nbreak")])
    assert n == 2
    back = {c.name: c for c in cv.read_netscape(f)}
    assert back["x"].secure and back["x"].domain == ".a.com" and back["x"].value == "1"
    assert back["h"].http_only and back["h"].expires > time.time()          # session cookies get a real expiry
    assert set(back) == {"x", "h"}
    if os.name != "nt":
        assert (f.stat().st_mode & 0o077) == 0                                # owner-only file


def test_read_netscape_ignores_garbage(tmp_path):
    f = tmp_path / "g.txt"
    f.write_text("# comment\nnot a cookie line\n.a.com\tTRUE\t/\tFALSE\t99999999999\tk\tv\n")
    assert [c.name for c in cv.read_netscape(f)] == ["k"]


# ---- vault -----------------------------------------------------------------------
def test_save_list_replace_and_remove(vault):
    e = vault.save("youtube.com", [rec(".youtube.com", "SID"), rec(".google.com", "__Secure-3PSID"), rec("ads.com", "t")],
                   "app-browser")
    assert e.count == 2 and e.signed_in is True and e.label == "YouTube / Google" and not e.expired
    assert [x.id for x in vault.entries()] == ["youtube.com"]
    vault.save("youtube.com", [rec(".youtube.com", "PREF")], "file")             # same site -> replaced, not duplicated
    e2 = vault.entries()[0]
    assert e2.count == 1 and e2.signed_in is False and e2.source == "file"
    vault.remove("youtube.com")
    assert vault.entries() == [] and not (vault.root / "youtube.com.txt").exists()


def test_save_without_matching_cookies_is_an_error(vault):
    with pytest.raises(ValueError):
        vault.save("facebook.com", [rec(".youtube.com", "a")], "app-browser")


def test_expired_entry_is_flagged(vault):
    e = vault.save("facebook.com", [rec(".facebook.com", "c_user", expires=int(time.time()) - 5)], "file")
    assert e.expired


def test_cookiefile_contains_only_that_sites_cookies(vault):
    vault.save("youtube.com", [rec(".youtube.com", "YT", "yt-secret")], "app-browser")
    vault.save("facebook.com", [rec(".facebook.com", "FB", "fb-secret")], "app-browser")
    f = vault.cookiefile_for("https://youtu.be/abc")
    text = f.read_text()
    assert "yt-secret" in text and "fb-secret" not in text
    assert "fb-secret" in vault.cookiefile_for("https://www.facebook.com/watch?v=1").read_text()
    assert vault.cookiefile_for("https://vimeo.com/1") is None
    assert vault.has_cookies_for("https://fb.watch/x") and not vault.has_cookies_for("https://vimeo.com/1")


def test_runtime_files_are_cleaned(vault):
    f = vault.cookiefile_for("https://youtu.be/x") or None
    vault.save("youtube.com", [rec(".youtube.com", "YT")], "file")
    f = vault.cookiefile_for("https://youtu.be/x")
    assert f.exists()
    vault.clean_runtime()
    assert not f.exists()


def test_import_netscape_file(vault, tmp_path):
    f = tmp_path / "exported.txt"
    cv.write_netscape(f, [rec(".vimeo.com", "vimeo_sess"), rec(".other.com", "x")])
    e = vault.import_netscape(f, "vimeo.com")
    assert e.count == 1 and e.source == "file"
    empty = tmp_path / "empty.txt"
    empty.write_text("# nothing\n")
    with pytest.raises(ValueError):
        vault.import_netscape(empty, "vimeo.com")


def test_corrupt_index_does_not_crash(vault):
    (vault.root / "index.json").write_text("{not json")
    assert vault.entries() == []


# ---- yt-dlp option selection ----------------------------------------------------------
def test_base_options_use_vault_for_matching_url_only(vault):
    vault.save("youtube.com", [rec(".youtube.com", "SID")], "app-browser")
    o = yd.base_options(url="https://www.youtube.com/watch?v=x")
    assert os.path.isfile(o["cookiefile"]) and "cookiesfrombrowser" not in o
    assert "cookiefile" not in yd.base_options(url="https://vimeo.com/1")
    assert "cookiefile" not in yd.base_options()
    assert yd.base_options("firefox", url="https://www.youtube.com/watch?v=x").get("cookiesfrombrowser") == ("firefox",)


def test_global_cookie_spec_is_the_fallback(vault):
    yd.set_cookie_spec("file:/tmp/some.txt")
    try:
        assert yd.base_options(url="https://vimeo.com/1")["cookiefile"] == "/tmp/some.txt"
    finally:
        yd.set_cookie_spec("")


def test_vault_failure_never_breaks_options(monkeypatch):
    class Boom:
        def cookiefile_for(self, _u):
            raise OSError("disk")
    cv.set_vault(Boom())
    try:
        assert "cookiefile" not in yd.base_options(url="https://youtu.be/x")
    finally:
        cv.set_vault(None)


# ---- classification / messages --------------------------------------------------------
@pytest.mark.parametrize("raw,hint", [
    ("Sign in to confirm you're not a bot", "signin"), ("This video is private", "signin"),
    ("Sign in to confirm your age", "signin"), ("This video is only available for Music Premium members", ""),
    ("No supported JavaScript runtime could be found", "js"), ("Could not copy Chrome cookie database", "cookies"),
    ("ERROR: unable to extract uploader id", "update"), ("some random failure", ""),
])
def test_classify(raw, hint):
    assert ss.classify(raw) == hint


def test_friendly_error_carries_hint_for_sign_in():
    e = yd.friendly_ytdlp_error(Exception("ERROR: [youtube] x: Sign in to confirm you're not a bot"))
    assert isinstance(e, FriendlyError) and e.hint == "signin" and "sign" in e.message.lower()


def test_detect_browsers_uses_profile_folders(tmp_path, monkeypatch):
    ff = tmp_path / "ff"
    ff.mkdir()
    monkeypatch.setattr(ss, "_dirs", lambda: {"firefox": [ff], "chrome": [tmp_path / "nope"], "edge": [tmp_path / "e"]})
    assert ss.detect_browsers() == ["firefox"]


# ---- yt-dlp really loads and sends the vault cookies -----------------------------------
def test_yt_dlp_loads_vault_file_and_sends_cookie_to_matching_host(vault):
    import http.server
    import threading

    seen = []

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(self.headers.get("Cookie", ""))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *a):
            pass
    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}/x"
    try:
        vault.save("127.0.0.1", [rec("127.0.0.1", "SID", "abc123"), rec("127.0.0.1", "hid", "h1", http_only=True, expires=0)], "file")
        import yt_dlp
        with yt_dlp.YoutubeDL(yd.base_options(url=url)) as ydl:
            ydl.urlopen(url).read()
        assert "SID=abc123" in seen[0] and "hid=h1" in seen[0]
        seen.clear()
        with yt_dlp.YoutubeDL(yd.base_options()) as ydl:                  # no URL -> vault not used -> no cookie
            ydl.urlopen(url).read()
        assert seen[0] == ""
    finally:
        srv.shutdown()


def test_delete_single_cookies_updates_index_and_removes_empty_entry(vault):
    vault.save("youtube.com", [rec(".youtube.com", "SID"), rec(".youtube.com", "PREF"), rec(".google.com", "LOGIN_INFO")], "app-browser")
    assert vault.entries()[0].signed_in is True
    n = vault.delete_cookies("youtube.com", [(".youtube.com", "/", "SID"), (".youtube.com", "/", "nope")])
    e = vault.entries()[0]
    assert n == 1 and e.count == 2 and e.signed_in is True                       # LOGIN_INFO still counts as signed in
    assert vault.delete_cookies("youtube.com", [(".youtube.com", "/", "SID")]) == 0
    vault.delete_cookies("youtube.com", [(".youtube.com", "/", "PREF"), (".google.com", "/", "LOGIN_INFO")])
    assert vault.entries() == [] and not (vault.root / "youtube.com.txt").exists()


# ---- link normalising, login detection, browser restore ------------------------------------
def test_url_normalizer_group_and_social_links():
    from app.services.url_normalizer import normalize_url as n
    assert n("https://www.facebook.com/groups/123456789/videos/987654321/") == "https://www.facebook.com/watch/?v=987654321"
    assert n("https://m.facebook.com/groups/mygroup/?multi_permalinks=987654321&__cft__[0]=x") == \
        "https://www.facebook.com/groups/mygroup/permalink/987654321/"
    assert n("https://www.facebook.com/groups/1234567/posts/987654321/?__tn__=z") == \
        "https://www.facebook.com/groups/1234567/permalink/987654321/"
    assert n("https://web.facebook.com/watch/?v=987654321&mibextid=a") == "https://www.facebook.com/watch/?v=987654321"
    assert n("https://twitter.com/i/web/status/1234567890") == "https://x.com/i/status/1234567890"
    assert n("https://twitter.com/u/status/1234567890?s=20") == "https://x.com/u/status/1234567890"
    assert n("https://www.instagram.com/someone/reel/ABC123/?igsh=1") == "https://www.instagram.com/reel/ABC123/"
    assert n("https://www.youtube.com/watch?v=abc&list=x") == "https://www.youtube.com/watch?v=abc&list=x"
    assert n("https://example.com/a?b=1") == "https://example.com/a?b=1"


def test_normalised_group_links_are_known_to_ytdlp():
    from yt_dlp.extractor import gen_extractors
    from app.services.url_normalizer import normalize_url as n
    ies = [i for i in gen_extractors() if i.ie_key() != "Generic"]
    for u in ("https://www.facebook.com/groups/123456789/videos/987654321/",
              "https://www.facebook.com/groups/mygroup/?multi_permalinks=987654321"):
        assert any(i.suitable(n(u)) for i in ies), u


def test_login_state_per_site():
    assert cv.login_state("facebook.com", ["datr", "sb"]) is False
    assert cv.login_state("facebook.com", ["c_user", "xs", "datr"]) is True
    assert cv.login_state("facebook.com", ["c_user"]) is False
    assert cv.login_state("instagram.com", ["sessionid"]) is True
    assert cv.login_state("x.com", ["guest_id", "ct0"]) is False and cv.login_state("x.com", ["auth_token"]) is True
    assert cv.login_state("example.org", ["a"]) is None


def test_vault_entry_reports_login(tmp_path):
    v = cv.CookieVault(tmp_path / "c")
    fb = [cv.CookieRec(".facebook.com", "/", "datr", "1")]
    assert v.save("facebook.com", fb, "t").signed_in is False
    fb += [cv.CookieRec(".facebook.com", "/", "c_user", "1"), cv.CookieRec(".facebook.com", "/", "xs", "2", True, 0, True)]
    assert v.save("facebook.com", fb, "t").signed_in is True
    raw = (tmp_path / "c" / "facebook.com.txt").read_bytes()
    assert b"\r" not in raw                                         # never CRLF (would corrupt the last field)


def test_facebook_requests_use_browser_impersonation_only_when_available():
    from app.services import ytdlp_service as yd
    assert yd.needs_impersonation("https://www.facebook.com/watch/?v=1234567")
    assert yd.needs_impersonation("https://fb.watch/abc/") and not yd.needs_impersonation("https://www.youtube.com/watch?v=a")
    opts = yd.base_options(url="https://www.facebook.com/watch/?v=1234567")
    assert ("impersonate" in opts) == (yd.impersonate_target() is not None)
    assert "impersonate" not in yd.base_options(url="https://x.com/u/status/12345678")


def test_facebook_cannot_parse_data_error_is_explained():
    from app.services import ytdlp_service as yd
    e = yd.friendly_ytdlp_error(Exception("ERROR: [facebook] 1409879191356544: Cannot parse data; please report this issue"))
    assert "Facebook" in e.message and e.hint == "signin"


def test_page_with_several_videos_asks_instead_of_guessing(monkeypatch):
    import yt_dlp
    from app.services import ytdlp_service as yd

    info = {"extractor": "facebook", "title": "Group post", "entries": [
        {"id": "111111111", "title": "Suggested", "uploader": "Other page", "duration": 100},
        {"id": "222222222", "title": "My video", "uploader": "Me", "duration": 50},
        {"id": "222222222", "title": "dup", "duration": 50}]}

    class Fake:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def extract_info(self, url, download=False): return info

    monkeypatch.setattr(yt_dlp, "YoutubeDL", Fake)
    res = yd.analyze_url("https://www.facebook.com/groups/1234567/permalink/9876543/", mode="single", fetch_thumbnail=False)
    assert isinstance(res, yd.PlaylistInfo) and res.kind == "post" and len(res.entries) == 2
    assert res.entries[1].url == "https://www.facebook.com/watch/?v=222222222" and "Me" in res.entries[1].title
    info["entries"] = info["entries"][:1] + [None]
    info["entries"][0].update(formats=[{"format_id": "1", "ext": "mp4", "height": 720, "vcodec": "avc1", "acodec": "aac",
                                        "url": "http://x/v.mp4"}])
    res = yd.analyze_url("https://www.facebook.com/groups/1234567/permalink/9876543/", mode="single", fetch_thumbnail=False)
    assert isinstance(res, yd.VideoInfo) and res.id == "111111111"


def test_probe_scan_lists_videos_in_order():
    from app.services.probe import scan_page
    page = ('<title>Group | Facebook</title><meta property="og:title" content="Hello"/>'
            '{"__typename":"Video","id":"111111111","title":{"text":"First"}} x '
            '{"video_id":"222222222"} {"__typename":"Video","id":"111111111"}')
    out = "\n".join(scan_page(page))
    assert "111111111" in out and "222222222" in out and out.index("111111111") < out.index("222222222")
    assert "First" in out and "og:title: Hello" in out


def test_facebook_post_finds_its_own_video(monkeypatch):
    import yt_dlp
    from app.services import fb_post, ytdlp_service as yd
    assert fb_post.is_post_url("https://www.facebook.com/share/v/19ZVzkLjsU/")
    assert fb_post.is_post_url("https://www.facebook.com/groups/g/permalink/1450505726594443/")
    assert not fb_post.is_post_url("https://www.facebook.com/watch/?v=1234567") and not fb_post.is_post_url("https://x.com/a/status/1")
    page = '{"__typename":"Video","id":"1410275507941067"} .. {"video_id":"1684707403178205"} {"video_id":"999"}'
    assert fb_post.video_ids(page) == ["1410275507941067", "1684707403178205"]
    monkeypatch.setattr(fb_post, "fetch_page", lambda ydl, url, limit=0: (url, page))

    def extract(y, u):
        if "1684707403178205" in u:
            raise RuntimeError("story")
        return {"title": "My video", "uploader": "Me", "duration": 30.0, "formats": [{"format_id": "1"}]}
    vids = fb_post.find_post_videos("https://www.facebook.com/share/v/x/", {}, extract)
    assert [v["id"] for v in vids] == ["1410275507941067"] and vids[0]["url"].endswith("watch/?v=1410275507941067")

    seen = []

    class Fake:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def extract_info(self, url, download=False):
            seen.append(url)
            return {"id": "1410275507941067", "title": "My video", "uploader": "Me", "duration": 30.0, "extractor": "facebook",
                    "formats": [{"format_id": "1", "ext": "mp4", "height": 720, "vcodec": "avc1", "acodec": "aac", "url": "http://x"}]}
    monkeypatch.setattr(yt_dlp, "YoutubeDL", Fake)
    monkeypatch.setattr(fb_post, "find_post_videos", lambda url, opts, extract=None: vids)
    res = yd.analyze_url("https://www.facebook.com/share/v/19ZVzkLjsU/", "single", fetch_thumbnail=False)
    assert isinstance(res, yd.VideoInfo) and seen == ["https://www.facebook.com/watch/?v=1410275507941067"]
    assert res.url.endswith("v=1410275507941067")


def test_duration_stated_in_facebook_page():
    from app.services.fb_post import duration_from_page as d
    page = 'x {"id":"123456789"} ... "playable_duration_in_ms":31000,"other":1 .. "playable_duration_in_ms":999000'
    assert d(page, "123456789") == 31.0
    assert d('"length_in_second":12.5', "") == 12.5
    assert d('mediaPresentationDuration=\\"PT1M31.5S\\"', "") == 91.5
    assert d("nothing here", "1") is None

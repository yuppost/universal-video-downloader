"""App update check (local server, no real network)."""
import http.server
import json
import threading

import pytest

from app.services import update_service as u


def test_versions_and_sources():
    assert u.is_newer("1.1.0", "1.0.0") and u.is_newer("1.0.10", "1.0.9") and u.is_newer("2", "1.9.9")
    assert not u.is_newer("1.0.0", "1.0.0") and not u.is_newer("1.0", "1.0.0") and not u.is_newer("0.9", "1.0.0")
    assert u.resolve_source("drive:ABCDEFGHIJKLMNOP") == "https://drive.google.com/uc?export=download&id=ABCDEFGHIJKLMNOP"
    assert u.resolve_source("https://drive.google.com/file/d/1AbCdEfGhIjKlMn/view?usp=sharing").endswith("id=1AbCdEfGhIjKlMn")
    assert u.resolve_source("https://example.com/latest.json") == "https://example.com/latest.json"
    assert u.resolve_source("") == ""


def test_parse_info_rejects_bad_files():
    ok = json.dumps({"version": "1.2.0", "url": "https://x.example/app.zip", "notes": "hi"}).encode()
    assert u.parse_info(ok).version == "1.2.0"
    assert u.parse_info(b"<html>virus scan warning</html>") is None
    assert u.parse_info(json.dumps({"version": "1.2.0", "url": "http://x/app.zip"}).encode()) is None       # https only
    assert u.parse_info(json.dumps({"version": "latest", "url": "https://x/a"}).encode()) is None
    assert u.parse_info(json.dumps({"version": "1.2.0", "url": "javascript:alert(1)"}).encode()) is None


def test_fetch_requires_https_and_never_raises():
    assert u.fetch_update("http://127.0.0.1:1/latest.json", "1.0.0") is None
    assert u.fetch_update("https://127.0.0.1:1/latest.json", "1.0.0", timeout=1) is None
    assert u.fetch_update("", "1.0.0") is None


def test_github_latest_release_answer():
    gh = json.dumps({"tag_name": "v1.2.0", "html_url": "https://github.com/o/r/releases/tag/v1.2.0",
                     "body": "Fixes", "draft": False, "prerelease": False}).encode()
    i = u.parse_info(gh)
    assert i and i.version == "1.2.0" and i.url.startswith("https://github.com/") and i.notes == "Fixes"
    assert u.parse_info(json.dumps({"tag_name": "v9.0.0", "html_url": "https://x/y", "prerelease": True}).encode()) is None
    assert u.resolve_source("github:yuppost/universal-video-downloader") == \
        "https://api.github.com/repos/yuppost/universal-video-downloader/releases/latest"


def test_github_assets_enable_one_click_update():
    sha = "a" * 64
    gh = json.dumps({"tag_name": "v1.2.0", "html_url": "https://github.com/o/r/releases/tag/v1.2.0", "assets": [
        {"name": "source.zip", "browser_download_url": "https://github.com/o/r/a/source.zip", "size": 5},
        {"name": "UniversalVideoStudio-windows.zip", "browser_download_url": "https://github.com/o/r/a/w.zip",
         "size": 123, "digest": "sha256:" + sha}]}).encode()
    i = u.parse_info(gh)
    assert i.asset_url.endswith("/w.zip") and i.sha256 == sha and i.size == 123
    assert u.parse_info(json.dumps({"version": "1.0.1", "url": "https://x/y", "zip": "http://insecure/z.zip"}).encode()).asset_url == ""
    assert u.parse_info(json.dumps({"version": "1.0.1", "url": "https://x/y", "zip": "https://x/z.zip",
                                    "sha256": "zz"}).encode()).sha256 == ""


# ---- self update (download, verify, unpack) --------------------------------------------------
def _serve(tmp_path, zip_name="up.zip"):
    import zipfile
    www = tmp_path / "www"
    www.mkdir()

    class H(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **k):
            super().__init__(*a, directory=str(www), **k)

        def log_message(self, *a):
            pass
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return www, srv, f"http://127.0.0.1:{srv.server_address[1]}"


def test_prepare_update_downloads_verifies_and_unpacks(tmp_path):
    import hashlib
    import zipfile
    from app.services import self_update as su
    from app.services.errors import FriendlyError
    www, srv, base = _serve(tmp_path)
    try:
        with zipfile.ZipFile(www / "up.zip", "w") as z:
            z.writestr("UniversalVideoStudio/UniversalVideoStudio.exe", "exe")
            z.writestr("UniversalVideoStudio/_internal/a.dll", "dll")
        sha = hashlib.sha256((www / "up.zip").read_bytes()).hexdigest()
        seen = []
        root = su.prepare(f"{base}/up.zip", sha, lambda p, m, s: seen.append(m), threading.Event(),
                          work=tmp_path / "w", allow_http=True)
        assert (root / "UniversalVideoStudio.exe").is_file() and (root / "_internal" / "a.dll").is_file()
        assert any("Verifying" in m for m in seen)
        with pytest.raises(FriendlyError) as e:
            su.prepare(f"{base}/up.zip", "0" * 64, lambda *a: None, threading.Event(), work=tmp_path / "w2", allow_http=True)
        assert "integrity" in e.value.message and not (tmp_path / "w2" / "new").exists()
        with zipfile.ZipFile(www / "evil.zip", "w") as z:
            z.writestr("../evil.txt", "x")
        with pytest.raises(FriendlyError):
            su.prepare(f"{base}/evil.zip", "", lambda *a: None, threading.Event(), work=tmp_path / "w3", allow_http=True)
        assert not (tmp_path / "evil.txt").exists()
        with zipfile.ZipFile(www / "noexe.zip", "w") as z:
            z.writestr("x/readme.txt", "x")
        with pytest.raises(FriendlyError) as e:
            su.prepare(f"{base}/noexe.zip", "", lambda *a: None, threading.Event(), work=tmp_path / "w4", allow_http=True)
        assert "UniversalVideoStudio.exe" in e.value.message
    finally:
        srv.shutdown()


def test_helper_script_protects_user_data_and_restarts():
    from app.services import self_update as su
    s = su.build_script()
    assert "\r\n" in s and "robocopy" in s and '/XD "%APP%\\data"' in s and "portable.flag" in s
    assert 'start "" "%EXE%"' in s and "tasklist" in s and "if errorlevel 8" in s
    assert "timeout" not in s                       # no console input in a detached helper: use ping for sleeping
    assert not su.can_self_update()                 # development / non-Windows: never replaces itself


def test_check_update_statuses(monkeypatch):
    assert u.check_update("", "1.0.0")[0] == "unconfigured"
    assert u.check_update("http://x/y.json", "1.0.0")[0] == "error"
    import io
    body = json.dumps({"version": "1.0.0", "url": "https://x/y"}).encode()
    monkeypatch.setattr(u.urllib.request, "urlopen", lambda *a, **k: io.BytesIO(body))
    assert u.check_update("https://x/latest.json", "1.0.0")[0] == "latest"
    assert u.check_update("https://x/latest.json", "0.9.0")[0] == "available"
    monkeypatch.setattr(u.urllib.request, "urlopen", lambda *a, **k: io.BytesIO(b"<html>"))
    assert u.check_update("https://x/latest.json", "1.0.0")[0] == "error"

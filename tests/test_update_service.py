"""App update check (local server, no real network)."""
import http.server
import os
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


def test_github_assets_enable_one_click_update(monkeypatch):
    monkeypatch.setattr(u.sys, "platform", "win32")
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
            z.writestr(f"UniversalVideoStudio/{su.EXE_NAME}", "exe")
            z.writestr("UniversalVideoStudio/_internal/a.dll", "dll")
        sha = hashlib.sha256((www / "up.zip").read_bytes()).hexdigest()
        seen = []
        root = su.prepare(f"{base}/up.zip", sha, lambda p, m, s: seen.append(m), threading.Event(),
                          work=tmp_path / "w", allow_http=True)
        assert (root / su.EXE_NAME).is_file() and (root / "_internal" / "a.dll").is_file()
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
        assert su.EXE_NAME in e.value.message
    finally:
        srv.shutdown()


def test_helper_script_protects_user_data_and_restarts():
    from app.services import self_update as su
    s = su.build_script()
    assert "\r\n" in s and "robocopy" in s and '/XD "%APP%\\data"' in s and "portable.flag" in s
    assert 'start "" /D "%APP%" "%EXE%"' in s and "tasklist" in s and "if errorlevel 8" in s
    assert "| find" not in s and "tasklist" in s     # no pipes: they spawn extra console windows
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


def test_helper_kills_a_stuck_app_verifies_the_restart_and_env_is_clean(monkeypatch):
    from app.services import self_update as su
    s = su.build_script()
    assert "taskkill /F /PID %PID%" in s and "goto force" in s            # an app that never exits cannot block the update
    assert 'start "" /D "%APP%" "%EXE%"' in s                             # runs from its own folder
    assert "explorer.exe" in s and 'find /I "%EXENAME%"' in s              # second try when the new app is not running
    assert "| find" not in s
    monkeypatch.setenv("_PYI_APPLICATION_HOME_DIR", "x")
    monkeypatch.setenv("_MEIPASS2", "y")
    monkeypatch.setenv("KEEP_ME", "1")
    env = su.clean_env()
    assert not any(k.startswith("_PYI") for k in env) and "_MEIPASS2" not in env
    assert env["KEEP_ME"] == "1" and env["PYINSTALLER_RESET_ENVIRONMENT"] == "1"


# ---- Linux -----------------------------------------------------------------------------------
def test_release_asset_is_chosen_per_platform(monkeypatch):
    assets = [{"name": "UniversalVideoStudio-windows.zip", "browser_download_url": "https://h/w.zip"},
              {"name": "UniversalVideoStudio-Setup.exe", "browser_download_url": "https://h/s.exe"},
              {"name": "UniversalVideoStudio-linux-aarch64.tar.gz", "browser_download_url": "https://h/arm.tar.gz"},
              {"name": "UniversalVideoStudio-linux-x86_64.tar.gz", "browser_download_url": "https://h/x64.tar.gz"}]
    monkeypatch.setattr(u.sys, "platform", "linux")
    monkeypatch.setattr(u.platform, "machine", lambda: "x86_64")
    assert u._pick_asset(assets)["browser_download_url"].endswith("x64.tar.gz")
    monkeypatch.setattr(u.platform, "machine", lambda: "aarch64")
    assert u._pick_asset(assets)["browser_download_url"].endswith("arm.tar.gz")
    monkeypatch.setattr(u.platform, "machine", lambda: "riscv64")
    assert u._pick_asset(assets) == {} or "tar.gz" in u._pick_asset(assets).get("name", "")
    monkeypatch.setattr(u.sys, "platform", "win32")
    assert u._pick_asset(assets)["browser_download_url"].endswith("w.zip")


def test_prepare_update_from_tar_gz_keeps_permissions_and_rejects_unsafe_paths(tmp_path):
    import io
    import tarfile
    from app.services import self_update as su
    from app.services.errors import FriendlyError
    www, srv, base = _serve(tmp_path)
    try:
        with tarfile.open(www / "up.tar.gz", "w:gz") as t:
            data = b"#!/bin/sh\necho hi\n"
            ti = tarfile.TarInfo(f"UniversalVideoStudio/{su.EXE_NAME}")
            ti.size, ti.mode = len(data), 0o755
            t.addfile(ti, io.BytesIO(data))
            lib = tarfile.TarInfo("UniversalVideoStudio/_internal/a.so")
            lib.size = 3
            t.addfile(lib, io.BytesIO(b"lib"))
        root = su.prepare(f"{base}/up.tar.gz", "", lambda *a: None, threading.Event(), work=tmp_path / "w", allow_http=True)
        exe = root / su.EXE_NAME
        assert exe.is_file() and (root / "_internal" / "a.so").is_file()
        if os.name != "nt":
            assert os.access(exe, os.X_OK)                                   # the program stays runnable
        with tarfile.open(www / "evil.tar.gz", "w:gz") as t:
            ti = tarfile.TarInfo("../evil.txt")
            ti.size = 1
            t.addfile(ti, io.BytesIO(b"x"))
        with pytest.raises(FriendlyError):
            su.prepare(f"{base}/evil.tar.gz", "", lambda *a: None, threading.Event(), work=tmp_path / "w2", allow_http=True)
        assert not (tmp_path / "evil.txt").exists()
        with tarfile.open(www / "link.tar.gz", "w:gz") as t:                  # a link pointing outside the folder
            ti = tarfile.TarInfo("UniversalVideoStudio/escape")
            ti.type, ti.linkname = tarfile.SYMTYPE, "../../../../etc"
            t.addfile(ti)
        with pytest.raises(FriendlyError):
            su.prepare(f"{base}/link.tar.gz", "", lambda *a: None, threading.Event(), work=tmp_path / "w3", allow_http=True)
    finally:
        srv.shutdown()


@pytest.mark.skipif(os.name == "nt", reason="POSIX helper")
def test_linux_helper_replaces_files_keeps_data_and_restarts(tmp_path):
    """Runs the real helper script: waits for a process, mirrors the new files, keeps data/portable.flag, restarts."""
    import subprocess
    import sys as _sys
    import time
    from app.services import self_update as su
    app = tmp_path / "app"
    new = tmp_path / "new"
    (app / "data").mkdir(parents=True)
    (app / "data" / "settings.json").write_text("keep")
    (app / "portable.flag").write_text("")
    (app / "_internal").mkdir()
    (app / "_internal" / "old.so").write_text("old")
    (app / su.EXE_NAME).write_text("#!/bin/sh\necho OLD\n")
    new.mkdir()
    (new / "_internal").mkdir()
    (new / "_internal" / "new.so").write_text("new")
    marker = tmp_path / "started.txt"
    (new / su.EXE_NAME).write_text(f"#!/bin/sh\necho started > '{marker}'\n")
    (new / su.EXE_NAME).chmod(0o755)
    script = tmp_path / "apply.sh"
    script.write_text(su.build_script_posix())
    waiter = subprocess.Popen(["sleep", "2"])                      # "the app": the helper must wait for it
    log = tmp_path / "update.log"
    t0 = time.time()
    helper = subprocess.Popen(["/bin/sh", str(script), str(waiter.pid), str(app), str(new), str(app / su.EXE_NAME), str(log)])
    deadline = time.time() + 25
    while time.time() < deadline and not marker.exists():
        waiter.poll()
        time.sleep(0.2)
    helper.wait(timeout=30)
    assert marker.exists() and time.time() - t0 >= 2                  # started only after the old process had exited
    assert (app / "_internal" / "new.so").is_file() and not (app / "_internal" / "old.so").exists()
    assert (app / "data" / "settings.json").read_text() == "keep" and (app / "portable.flag").exists()
    assert "files replaced" in log.read_text()


def test_clean_env_restores_library_path_on_linux(monkeypatch):
    from app.services import self_update as su
    monkeypatch.setattr(su.sys, "platform", "linux")
    monkeypatch.setattr(su.sys, "frozen", True, raising=False)
    monkeypatch.setenv("LD_LIBRARY_PATH", "/old/_internal")
    monkeypatch.setenv("LD_LIBRARY_PATH_ORIG", "/usr/local/lib")
    assert su.clean_env()["LD_LIBRARY_PATH"] == "/usr/local/lib" and "LD_LIBRARY_PATH_ORIG" not in su.clean_env()
    monkeypatch.delenv("LD_LIBRARY_PATH_ORIG")
    assert "LD_LIBRARY_PATH" not in su.clean_env()


@pytest.mark.skipif(not __import__("shutil").which("dpkg-deb"), reason="dpkg-deb not installed")
def test_deb_package_layout(tmp_path):
    import subprocess
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    app = tmp_path / "UniversalVideoStudio"
    (app / "_internal").mkdir(parents=True)
    (app / "UniversalVideoStudio").write_text("#!/bin/sh\necho hi\n")
    (app / "UniversalVideoStudio").chmod(0o755)
    (app / "_internal" / "lib.so").write_text("x")
    out = tmp_path / "x.deb"
    subprocess.run([str(root / "packaging/linux/make_deb.sh"), str(app), "1.2.3", "amd64", str(out)], check=True,
                   capture_output=True)
    info = subprocess.run(["dpkg-deb", "-I", str(out)], capture_output=True, text=True).stdout
    assert "Package: universal-video-studio" in info and "Version: 1.2.3" in info and "Architecture: amd64" in info
    assert "Recommends: ffmpeg" in info
    files = subprocess.run(["dpkg-deb", "-c", str(out)], capture_output=True, text=True).stdout
    assert "opt/universal-video-studio/UniversalVideoStudio" in files and "usr/bin/universal-video-studio ->" in files
    assert "usr/share/applications/UniversalVideoStudio.desktop" in files and "hicolor/256x256/apps/universal-video-studio.png" in files
    assert "root/root" in files                                              # files are owned by root, not the builder
    assert "ffmpeg" not in files.replace("Recommends", "") and "deno" not in files
    ex = tmp_path / "x"
    subprocess.run(["dpkg-deb", "-x", str(out), str(ex)], check=True)
    assert os.access(ex / "opt/universal-video-studio/UniversalVideoStudio", os.X_OK)

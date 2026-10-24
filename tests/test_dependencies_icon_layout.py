"""Setup/installer, app icon and responsive-layout tests (no internet needed)."""
import hashlib
import http.server
import os
import shutil
import threading
import zipfile
from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication

from app import appicon
from app.config import Settings
from app.services import dependency_service as deps
from app.services.errors import FriendlyError, OperationCanceled
from tests.conftest import FFMPEG, needs_ffmpeg


def EXE(name: str) -> str:
    return name + (".exe" if os.name == "nt" else "")


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def server(tmp_path):
    """Local HTTP server for the files in tmp_path/www."""
    www = tmp_path / "www"
    www.mkdir()

    class Quiet(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **k):
            super().__init__(*a, directory=str(www), **k)

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Quiet)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield www, f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def make_zip(path: Path, with_tools=True, evil=False):
    with zipfile.ZipFile(path, "w") as z:
        if with_tools:
            ext = ".exe" if os.name == "nt" else ""      # on Windows the installer looks for *.exe
            for name in ("ffmpeg", "ffprobe"):
                z.write(shutil.which(name), f"ffmpeg-7.0-essentials_build/bin/{name}{ext}")
        z.writestr("ffmpeg-7.0-essentials_build/README.txt", "hi")
        if evil:
            z.writestr("../../evil/bin/ffmpeg" + (".exe" if os.name == "nt" else ""), "x")


@needs_ffmpeg
def test_install_ffmpeg_from_local_server(server, tmp_path):
    www, base = server
    make_zip(www / "ff.zip")
    sha = hashlib.sha256((www / "ff.zip").read_bytes()).hexdigest()
    (www / "ff.zip.sha256").write_text(f"{sha} *ff.zip\n")
    seen = []
    dest = tmp_path / "tools" / "ffmpeg"
    out = deps.install_ffmpeg(lambda p, t, s: seen.append((p, t)), threading.Event(),
                              sources=[(f"{base}/ff.zip", f"{base}/ff.zip.sha256")], dest_dir=dest, allow_http=True)
    assert (out / EXE("ffmpeg")).exists() and (out / EXE("ffprobe")).exists()
    assert any("Verifying" in t for _p, t in seen) and seen[-1][0] == 100.0
    assert not (dest.parent / "download").exists()                # temp download removed


@needs_ffmpeg
def test_checksum_mismatch_is_rejected_then_next_source_used(server, tmp_path):
    www, base = server
    make_zip(www / "bad.zip")
    (www / "bad.zip.sha256").write_text("0" * 64 + " bad.zip\n")
    make_zip(www / "good.zip")
    dest = tmp_path / "ff"
    deps.install_ffmpeg(lambda *a: None, threading.Event(),
                        sources=[(f"{base}/bad.zip", f"{base}/bad.zip.sha256"), (f"{base}/good.zip", None)],
                        dest_dir=dest, allow_http=True)
    assert (dest / EXE("ffmpeg")).exists()
    with pytest.raises(FriendlyError) as ei:                       # only the bad source -> clear error, nothing installed
        deps.install_ffmpeg(lambda *a: None, threading.Event(), sources=[(f"{base}/bad.zip", f"{base}/bad.zip.sha256")],
                            dest_dir=tmp_path / "ff2", allow_http=True)
    assert "integrity" in ei.value.message and not (tmp_path / "ff2" / EXE("ffmpeg")).exists()


@needs_ffmpeg
def test_archive_without_tools_and_path_traversal(server, tmp_path):
    www, base = server
    make_zip(www / "empty.zip", with_tools=False)
    with pytest.raises(FriendlyError):
        deps.install_ffmpeg(lambda *a: None, threading.Event(), sources=[(f"{base}/empty.zip", None)],
                            dest_dir=tmp_path / "a", allow_http=True)
    make_zip(www / "evil.zip", evil=True)
    deps.install_ffmpeg(lambda *a: None, threading.Event(), sources=[(f"{base}/evil.zip", None)],
                        dest_dir=tmp_path / "b" / "ff", allow_http=True)
    assert not (tmp_path / "evil").exists() and not (tmp_path.parent / "evil").exists()


def test_http_refused_by_default_and_missing_file(tmp_path):
    with pytest.raises(FriendlyError):
        deps.download_file("http://example.com/x.zip", tmp_path / "x.zip", None, threading.Event())
    with pytest.raises(FriendlyError):
        deps.download_file("https://127.0.0.1:9/x.zip", tmp_path / "x.zip", None, threading.Event())


def test_download_cancel(server, tmp_path):
    www, base = server
    (www / "big.bin").write_bytes(os.urandom(3_000_000))
    ev = threading.Event()
    ev.set()
    with pytest.raises(OperationCanceled):
        deps.download_file(f"{base}/big.bin", tmp_path / "big.bin", None, ev, allow_http=True)
    assert not (tmp_path / "big.bin").exists()


@needs_ffmpeg
def test_managed_folder_is_searched_first_after_config(tmp_path, monkeypatch):
    monkeypatch.setenv("UVS_DATA_DIR", str(tmp_path))
    managed = tmp_path / "tools" / "ffmpeg"
    managed.mkdir(parents=True)
    exe = ".exe" if os.name == "nt" else ""
    shutil.copy(FFMPEG, managed / f"ffmpeg{exe}")
    shutil.copy(shutil.which("ffprobe"), managed / f"ffprobe{exe}")
    from app.services.ffmpeg_service import find_executable
    assert find_executable("ffmpeg") == str(managed / f"ffmpeg{exe}")
    st = deps.check()
    assert st.ffmpeg_ok and st.ytdlp_ok and "ffmpeg" in st.ffmpeg_version.lower()


def test_status_when_nothing_found(monkeypatch):
    monkeypatch.setattr(deps, "find_executable", lambda n, c="": None)
    monkeypatch.setattr(deps.yd, "ytdlp_version", lambda: "not installed")
    st = deps.check()
    assert not st.ffmpeg_ok and not st.ytdlp_ok and not st.all_ok


def test_non_windows_gives_manual_hint(monkeypatch):
    monkeypatch.setattr(deps.platform, "machine", lambda: "riscv64")      # a CPU with no automatic download
    monkeypatch.setattr(deps.os, "name", "posix")
    with pytest.raises(FriendlyError) as ei:
        deps.install_ffmpeg(lambda *a: None, threading.Event())
    assert "package manager" in ei.value.message
    with pytest.raises(FriendlyError) as ei:
        deps.install_deno(lambda *a: None, threading.Event())
    assert "Node.js" in ei.value.message


@pytest.mark.skipif(os.name == "nt", reason="uses a fake shell-script 'deno'")
def test_install_deno_from_local_server(server, tmp_path):
    www, base = server
    with zipfile.ZipFile(www / "deno.zip", "w") as z:
        z.writestr("deno", "#!/bin/sh\necho deno 2.4.0\n")
        z.writestr("../evil/deno", "x")
    sha = hashlib.sha256((www / "deno.zip").read_bytes()).hexdigest()
    (www / "deno.zip.sha256sum").write_text(f"{sha}  deno.zip\n")
    dest = tmp_path / "tools" / "deno"
    out = deps.install_deno(lambda *a: None, threading.Event(), sources=[(f"{base}/deno.zip", f"{base}/deno.zip.sha256sum")],
                            dest_dir=dest, allow_http=True)
    assert (out / "deno").is_file() and not (tmp_path / "evil").exists()
    (www / "deno.zip.sha256sum").write_text("0" * 64 + "  deno.zip\n")
    with pytest.raises(FriendlyError) as ei:
        deps.install_deno(lambda *a: None, threading.Event(), sources=[(f"{base}/deno.zip", f"{base}/deno.zip.sha256sum")],
                          dest_dir=tmp_path / "d2", allow_http=True)
    assert "integrity" in ei.value.message


# ---- icon --------------------------------------------------------------------
def test_icon_default_custom_and_invalid(qapp, tmp_path):
    assert not appicon.load_icon("").isNull()
    png = tmp_path / "mine.png"
    appicon._draw(64).save(str(png))
    assert not appicon.load_icon(str(png)).pixmap(32, 32).isNull()
    bad = tmp_path / "bad.png"
    bad.write_bytes(b"not an image")
    assert not appicon.load_icon(str(bad)).isNull()               # falls back to default
    assert not appicon.load_icon(str(tmp_path / "missing.png")).isNull()
    assert not appicon.load_icon(str(tmp_path / "x.exe")).isNull()


def test_default_icon_files_ship_with_project():
    root = Path(__file__).resolve().parents[1] / "assets"
    assert (root / "icon.ico").stat().st_size > 1000 and (root / "icon.png").stat().st_size > 1000


def test_icon_setting_roundtrip(tmp_path):
    from app.config import load_settings, save_settings
    s = Settings()
    s.icon_path = "C:/icons/mine.ico"
    save_settings(s, tmp_path / "s.json")
    assert load_settings(tmp_path / "s.json").icon_path == "C:/icons/mine.ico"


# ---- responsive layout ---------------------------------------------------------
@pytest.fixture
def window(qapp, tmp_path, monkeypatch):
    import app.main_window as mw
    monkeypatch.setattr(mw.QTimer, "singleShot", staticmethod(lambda *a, **k: None))   # no modal setup dialog
    s = Settings(temp_dir=str(tmp_path / "t"), download_dir=str(tmp_path / "o"))
    w = mw.MainWindow(s)
    w.show()
    yield w
    w.controller.shutdown()


def test_workspace_layout_modes(window):
    """Explicit sizes (no event loop) keep this deterministic whatever the real screen allows."""
    ws = window.current()
    ws.update_layout_mode(1300, 900)
    assert not ws._compact and not ws._tiny and ws.body.isVisibleTo(ws) and not ws.compact_tabs.isVisibleTo(ws)
    assert ws.body.indexOf(ws.side_holder) == 1
    ws.update_layout_mode(800, 900)
    assert ws._compact and not ws._tiny and ws.compact_tabs.isVisibleTo(ws) and not ws.body.isVisibleTo(ws)
    assert [ws.compact_tabs.tabText(i) for i in range(2)] == ["Video && Timeline", "Clips && Export"]   # && = literal & in a Qt tab
    assert ws.body.indexOf(ws.side_holder) == -1
    ws.update_layout_mode(700, 900)
    assert ws._tiny and ws.btn_delete.text() == "" and ws.btn_delete.toolTip()
    ws.update_layout_mode(1300, 900)                                  # back to wide: everything restored
    assert ws.btn_delete.text() == "Delete" and ws.body.indexOf(ws.side_holder) == 1
    assert ws.body.isVisibleTo(ws) and ws.side_holder.isVisibleTo(ws.body) and ws.left.isVisibleTo(ws.body)


def test_short_windows_fold_info_and_output(window):
    ws = window.current()
    ws.update_layout_mode(1300, 700)
    assert ws._short and not ws.sec_info.is_expanded() and not ws.sec_output.is_expanded()
    assert ws.sec_crop.is_expanded()
    ws.update_layout_mode(1300, 900)
    assert not ws._short and ws.sec_info.is_expanded() and ws.sec_output.is_expanded()


def test_nothing_is_lost_when_small(window, qapp):
    """Every essential control stays reachable (inside the workspace, which scrolls or tabs when space runs out)."""
    ws = window.current()
    for size in ((640, 480), (800, 600), (1024, 640), (1366, 700), (1500, 900)):
        ws.update_layout_mode(*size)
        for b in (ws.btn_analyze, ws.url_edit, ws.btn_add, ws.btn_sel, ws.btn_all, ws.btn_full, ws.timeline):
            assert ws.isAncestorOf(b)
        assert ws._scroll.widget() is not None and window.minimumWidth() <= 640


def test_window_never_larger_than_screen(window, qapp):
    scr = window.screen().availableGeometry()
    assert window.width() <= scr.width() and window.height() <= scr.height()


# ---- "Update" downloads only when something newer exists ------------------------
def test_version_tuple_parsing():
    assert deps.version_tuple("ffmpeg version 9.0.2-essentials_build-www.gyan.dev") == (9, 0, 2)
    assert deps.version_tuple("v2.9.8") == (2, 9, 8) and deps.version_tuple("nothing") is None


@pytest.mark.skipif(os.name == "nt", reason="uses a fake shell-script 'ffmpeg'")
def test_update_ffmpeg_only_downloads_when_newer(server, tmp_path, monkeypatch):
    www, base = server
    fake = tmp_path / "bin"
    fake.mkdir()
    for n in ("ffmpeg", "ffprobe"):
        (fake / n).write_text("#!/bin/sh\necho 'ffmpeg version 9.0.2-essentials_build'\n")
        (fake / n).chmod(0o755)
    (www / "release-version").write_text("9.0.2\n")
    make_zip(www / "ffmpeg.zip")
    sources = [(f"{base}/ffmpeg.zip", None)]
    calls = []
    real = deps.install_ffmpeg
    monkeypatch.setattr(deps, "install_ffmpeg", lambda *a, **k: calls.append(1) or real(*a, **k))
    kw = dict(configured=str(fake), version_url=f"{base}/release-version", sources=sources, dest_dir=tmp_path / "m", allow_http=True)
    msg = deps.update_ffmpeg(lambda *a: None, threading.Event(), **kw)
    assert "up to date" in msg and not calls                                  # same version: nothing downloaded
    (www / "release-version").write_text("9.1.0\n")
    msg = deps.update_ffmpeg(lambda *a: None, threading.Event(), **kw)
    assert "updated to 9.1.0" in msg and calls == [1]                         # newer: downloaded
    (www / "release-version").write_text("")
    with pytest.raises(FriendlyError):                                        # cannot tell: no pointless reinstall
        deps.update_ffmpeg(lambda *a: None, threading.Event(), **kw)
    assert calls == [1]


@pytest.mark.skipif(os.name == "nt", reason="uses a fake shell-script 'deno'")
def test_update_deno_only_downloads_when_newer(server, tmp_path, monkeypatch):
    www, base = server
    monkeypatch.setattr(deps.jsruntime, "find_js_runtime", lambda: ("deno", "/x/deno", (2, 9, 8)))
    (www / "latest.json").write_text('{"tag_name": "v2.9.8"}')
    calls = []
    monkeypatch.setattr(deps, "install_deno", lambda *a, **k: calls.append(1))
    kw = dict(version_url=f"{base}/latest.json", allow_http=True)
    assert "up to date" in deps.update_deno(lambda *a: None, threading.Event(), **kw) and not calls
    (www / "latest.json").write_text('{"tag_name": "v2.10.0"}')
    assert "updated to 2.10.0" in deps.update_deno(lambda *a: None, threading.Event(), **kw) and calls == [1]
    monkeypatch.setattr(deps.jsruntime, "find_js_runtime", lambda: None)      # nothing installed: plain install
    assert "installed" in deps.update_deno(lambda *a: None, threading.Event(), **kw) and calls == [1, 1]


@pytest.mark.skipif(os.name == "nt", reason="uses a fake shell-script 'ffmpeg'")
def test_check_tool_updates_reports_states_without_downloading(server, tmp_path, monkeypatch):
    www, base = server
    fake = tmp_path / "bin"
    fake.mkdir()
    for n in ("ffmpeg", "ffprobe"):
        (fake / n).write_text("#!/bin/sh\necho 'ffmpeg version 9.0.2-essentials_build'\n")
        (fake / n).chmod(0o755)
    (www / "release-version").write_text("9.0.2")
    (www / "latest.json").write_text('{"tag_name": "v3.0.0"}')
    monkeypatch.setattr(deps.yd, "ytdlp_version", lambda: "2026.10.21")
    monkeypatch.setattr(deps.yd, "latest_ytdlp_version", lambda *a, **k: "2026.10.21")
    monkeypatch.setattr(deps.jsruntime, "find_js_runtime", lambda: ("deno", "/x/deno", (2, 9, 8)))
    calls = []
    monkeypatch.setattr(deps, "install_ffmpeg", lambda *a, **k: calls.append(1))
    monkeypatch.setattr(deps, "install_deno", lambda *a, **k: calls.append(1))
    r = deps.check_tool_updates(str(fake), f"{base}/release-version", f"{base}/latest.json", allow_http=True)
    assert r["ffmpeg"] == ("uptodate", "9.0.2") and r["ytdlp"][0] == "uptodate" and r["deno"] == ("available", "3.0.0")
    (www / "release-version").write_text("")
    assert deps.check_tool_updates(str(fake), f"{base}/release-version", f"{base}/latest.json", allow_http=True)["ffmpeg"][0] == "unknown"
    monkeypatch.setattr(deps.jsruntime, "find_js_runtime", lambda: None)
    assert deps.check_tool_updates(str(fake), f"{base}/release-version", f"{base}/latest.json", allow_http=True)["deno"][0] == "missing"
    assert not calls


# ---- Linux static FFmpeg / Deno ---------------------------------------------------------------
@pytest.mark.skipif(os.name == "nt", reason="Linux static build: fake shell-script 'ffmpeg' without .exe")
def test_install_ffmpeg_from_tar_xz_with_md5_and_version_from_readme(server, tmp_path):
    import io
    import tarfile
    www, base = server
    with tarfile.open(www / "ff.tar.xz", "w:xz") as t:
        for name, mode in (("ffmpeg-7.0.2-amd64-static/ffmpeg", 0o755), ("ffmpeg-7.0.2-amd64-static/ffprobe", 0o755),
                           ("ffmpeg-7.0.2-amd64-static/manpages/ffmpeg", 0o644)):
            body = b"#!/bin/sh\necho 'ffmpeg version 7.0.2-static'\n"
            ti = tarfile.TarInfo(name)
            ti.size, ti.mode = len(body), mode
            t.addfile(ti, io.BytesIO(body))
    md5 = hashlib.md5((www / "ff.tar.xz").read_bytes()).hexdigest()
    (www / "ff.tar.xz.md5").write_text(f"{md5}  ff.tar.xz\n")
    dest = tmp_path / "ffmpeg"
    out = deps.install_ffmpeg(lambda *a: None, threading.Event(), sources=[(f"{base}/ff.tar.xz", f"{base}/ff.tar.xz.md5")],
                              dest_dir=dest, allow_http=True)
    assert (out / "ffmpeg").is_file() and (out / "ffprobe").is_file() and os.access(out / "ffmpeg", os.X_OK)
    assert not (out / "manpages").exists()
    (www / "ff.tar.xz.md5").write_text("0" * 32 + "  ff.tar.xz\n")                 # wrong checksum is refused
    with pytest.raises(FriendlyError) as ei:
        deps.install_ffmpeg(lambda *a: None, threading.Event(), sources=[(f"{base}/ff.tar.xz", f"{base}/ff.tar.xz.md5")],
                            dest_dir=tmp_path / "f2", allow_http=True)
    assert "integrity" in ei.value.message
    (www / "release-readme.txt").write_text("FFmpeg Static Build\n\nversion: 7.1.1\n\nlicense: GPLv3\n")
    assert deps.latest_ffmpeg_version(f"{base}/release-readme.txt", allow_http=True) == "7.1.1"


def test_default_sources_follow_the_platform(monkeypatch):
    monkeypatch.setattr(deps.os, "name", "posix")
    monkeypatch.setattr(deps.sys, "platform", "linux")
    monkeypatch.setattr(deps.platform, "machine", lambda: "x86_64")
    ff, dn = deps.default_ffmpeg_sources(), deps.default_deno_sources()
    assert ff[0][0].endswith("ffmpeg-release-amd64-static.tar.xz") and ff[0][1].endswith(".md5")
    assert dn[0][0].endswith("deno-x86_64-unknown-linux-gnu.zip")
    monkeypatch.setattr(deps.platform, "machine", lambda: "aarch64")
    assert deps.default_ffmpeg_sources()[0][0].endswith("arm64-static.tar.xz")
    assert deps.default_deno_sources()[0][0].endswith("aarch64-unknown-linux-gnu.zip")
    monkeypatch.setattr(deps.os, "name", "nt")
    monkeypatch.setattr(deps.sys, "platform", "win32")
    assert deps.default_ffmpeg_sources()[0][0].endswith("ffmpeg-release-essentials.zip")
    assert deps.default_deno_sources()[0][0].endswith("windows-msvc.zip")

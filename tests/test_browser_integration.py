import io
import json
import os
import sys
import threading
import time
from pathlib import Path

import pytest

from app.services import browser_integration as bi
from app.services import link_protocol as lp

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


# ---- link protocol ----------------------------------------------------------------------------
@pytest.mark.parametrize("raw,ok", [
    ("https://www.youtube.com/watch?v=abc", True), ("http://example.com/v.mp4", True), ("  https://a.b/c  ", True),
    ("javascript:alert(1)", False), ("file:///C:/Windows/win.ini", False), ("ftp://x/y", False), ("", False),
    ("https://a.b/c d", False), ("https://a.b/\nrm -rf", False), ("https://", False), (None, False), (5, False),
    ("https://a.b/" + "x" * 5000, False),
])
def test_clean_url_accepts_only_plain_http_links(raw, ok):
    assert (lp.clean_url(raw) is not None) == ok


def test_request_roundtrip_and_garbage():
    assert lp.parse_request(lp.encode_request("https://v.com/1")) == "https://v.com/1"
    assert lp.parse_request(b"not json") is None
    assert lp.parse_request(b'{"type":"quit","url":"https://v.com"}') is None
    assert lp.parse_request(b'{"type":"open","url":"javascript:1"}') is None
    assert lp.parse_request(b"\xff\xfe") is None


def test_native_message_framing():
    buf = io.BytesIO()
    lp.write_native_message(buf, {"ok": True, "n": "ü"})
    buf.seek(0)
    assert lp.read_native_message(buf) == {"ok": True, "n": "ü"}
    assert lp.read_native_message(io.BytesIO(b"\x05\x00")) is None                       # truncated header
    assert lp.read_native_message(io.BytesIO(b"\xff\xff\xff\x7f{}")) is None             # absurd length
    assert lp.read_native_message(io.BytesIO(b"\x03\x00\x00\x00[1]")) is None            # not an object


# ---- native host ------------------------------------------------------------------------------
def test_host_validates_and_answers(monkeypatch):
    import native_host as nh
    sent = []
    monkeypatch.setattr(lp, "send_to_running_app", lambda u, timeout=2.0: sent.append(u) or True)
    assert nh.handle({"type": "ping"}) == {"ok": True, "pong": True}
    assert nh.handle({"url": "javascript:alert(1)"}) == {"ok": False, "error": "invalid-url"}
    assert nh.handle({"type": "open", "url": "https://v.com/1"}) == {"ok": True, "started": False}
    assert sent == ["https://v.com/1"]
    monkeypatch.setattr(lp, "send_to_running_app", lambda u, timeout=2.0: False)
    started = []
    monkeypatch.setattr(nh, "start_app", lambda u: started.append(u) or True)
    assert nh.handle({"url": "https://v.com/2"}) == {"ok": True, "started": True} and started == ["https://v.com/2"]
    monkeypatch.setattr(nh, "start_app", lambda u: False)
    assert nh.handle({"url": "https://v.com/3"})["error"] == "app-not-found"


def test_host_main_speaks_the_native_protocol(monkeypatch):
    import native_host as nh
    monkeypatch.setattr(lp, "send_to_running_app", lambda u, timeout=2.0: True)
    inp = io.BytesIO()
    lp.write_native_message(inp, {"type": "open", "url": "https://v.com/9"})
    inp.seek(0)
    out = io.BytesIO()

    class Std:
        buffer = None
    monkeypatch.setattr(nh, "_stdio", lambda: (inp, out))
    assert nh.main() == 0
    out.seek(0)
    assert lp.read_native_message(out) == {"ok": True, "started": False}


# ---- the app receives links (real local pipe/socket) -----------------------------------------
def test_link_server_receives_and_rejects(qapp):
    from PySide6.QtCore import QCoreApplication
    from app.services.link_server import LinkServer, forward_to_running_app, instance_running
    name = f"uvs-test-{os.getpid()}-{int(time.time())}"
    srv = LinkServer(name=name)
    assert srv.start() and instance_running(name)
    got = []
    srv.urlReceived.connect(got.append)
    assert forward_to_running_app("https://v.com/ok", name)
    deadline = time.time() + 3
    while not got and time.time() < deadline:
        QCoreApplication.processEvents()
        time.sleep(0.02)
    assert got == ["https://v.com/ok"]
    # a hostile/garbage client is ignored
    import socket
    if sys.platform != "win32":
        s = socket.socket(socket.AF_UNIX)
        import tempfile
        s.connect(os.path.join(tempfile.gettempdir(), name))
        s.sendall(b'{"type":"open","url":"file:///etc/passwd"}\n')
        s.close()
        for _ in range(30):
            QCoreApplication.processEvents()
            time.sleep(0.02)
    assert got == ["https://v.com/ok"]
    srv.stop()
    assert not instance_running(name)


def test_second_launch_without_server_returns_false(qapp):
    from app.services.link_server import forward_to_running_app
    assert forward_to_running_app("https://v.com/1", name="uvs-nobody-home-xyz") is False


def test_main_window_opens_external_link(qapp, tmp_path, monkeypatch):
    import app.main_window as mw
    from app.config import Settings
    from app.services import cookie_vault as cv
    from app.widgets.workspace import Workspace
    monkeypatch.setattr(mw.QTimer, "singleShot", staticmethod(lambda *a, **k: None))
    monkeypatch.setenv("UVS_DATA_DIR", str(tmp_path / "data"))
    cv.set_vault(cv.CookieVault(tmp_path / "cookies"))
    win = mw.MainWindow(Settings(temp_dir=str(tmp_path / "t"), download_dir=str(tmp_path / "o")))
    analyzed = []
    monkeypatch.setattr(Workspace, "analyze", lambda self, mode, url_override=None: analyzed.append(self.url_edit.text()))
    first = win.current()
    win.open_external_url("https://v.com/a")                       # empty tab is reused
    assert win.current() is first and analyzed == ["https://v.com/a"]
    win.open_external_url("https://v.com/b")                       # tab in use -> new tab
    assert win.current() is not first and analyzed[-1] == "https://v.com/b"
    n = len(win.workspaces())
    win.open_external_url("javascript:alert(1)")
    assert len(win.workspaces()) == n
    win.controller.shutdown()
    cv.set_vault(None)


# ---- registration -----------------------------------------------------------------------------
class FakeReg:
    HKEY_CURRENT_USER = "HKCU"
    REG_SZ = 1

    def __init__(self):
        self.values = {}
        self.deleted = []

    class _K:
        def __init__(self, reg, path): self.reg, self.path = reg, path
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def CreateKey(self, root, path): return self._K(self, path)
    def SetValueEx(self, k, name, _r, _t, value): self.values[k.path] = value
    def DeleteKey(self, root, path): self.deleted.append(path)


def test_register_writes_manifest_and_all_three_browsers(tmp_path, monkeypatch):
    monkeypatch.setenv("UVS_DATA_DIR", str(tmp_path))
    host = tmp_path / "uvs-native-host.exe"
    host.write_bytes(b"x")
    reg = FakeReg()
    assert bi.register(host, winreg_module=reg)
    assert len(reg.values) == 3
    assert any("Google" in k for k in reg.values) and any("Edge" in k for k in reg.values) and any("Brave" in k for k in reg.values)
    mf = json.loads(Path(next(iter(reg.values.values()))).read_text())
    assert mf["name"] == lp.HOST_NAME and mf["path"] == str(host) and mf["type"] == "stdio"
    assert mf["allowed_origins"] == [f"chrome-extension://{bi.dev_extension_id()}/"]
    bi.unregister(winreg_module=reg)
    assert len(reg.deleted) == 3 and not Path(next(iter(reg.values.values()))).exists()


def test_extra_extension_ids_from_file(tmp_path, monkeypatch):
    monkeypatch.setenv("UVS_DATA_DIR", str(tmp_path))
    (tmp_path / "extension_ids.txt").write_text("abcdefghijklmnopabcdefghijklmnop\nbad id\nSHORT\n")
    assert bi.extension_ids() == [bi.dev_extension_id(), "abcdefghijklmnopabcdefghijklmnop"]


def test_extension_id_matches_the_manifest_key():
    mf = json.loads((ROOT / "browser_extension" / "manifest.json").read_text())
    assert mf["key"] == bi.EXTENSION_KEY
    assert len(bi.dev_extension_id()) == 32 and set(bi.dev_extension_id()) <= set("abcdefghijklmnop")


def test_extension_package_is_complete_and_least_privilege():
    d = ROOT / "browser_extension"
    mf = json.loads((d / "manifest.json").read_text())
    assert mf["manifest_version"] == 3
    assert set(mf["permissions"]) == {"nativeMessaging", "storage", "contextMenus", "scripting"}
    assert "host_permissions" not in mf                              # all-sites access is optional, asked on demand
    files = {mf["background"]["service_worker"], mf["action"]["default_popup"], mf["options_ui"]["page"],
             *mf["content_scripts"][0]["js"], *mf["icons"].values()}
    for f in files:
        assert (d / f).is_file(), f
    assert "*://*.youtube.com/*" in mf["content_scripts"][0]["matches"]
    assert not any(m in ("<all_urls>", "*://*/*") for m in mf["content_scripts"][0]["matches"])
    assert lp.HOST_NAME in (d / "background.js").read_text()


def test_installer_and_build_ship_the_integration():
    iss = (ROOT / "installer" / "UniversalVideoStudio.iss").read_text()
    bat = (ROOT / "build.bat").read_text()
    for name in ("NativeMessagingHosts", "uvs-native-host", "browser_extension"):
        assert name in iss + bat


def test_native_host_script_end_to_end(qapp, tmp_path):
    """The real host process: framed JSON on stdin -> link arrives in the app via the local socket -> framed answer."""
    import subprocess
    from PySide6.QtCore import QCoreApplication
    from app.services.link_server import LinkServer
    if sys.platform == "win32":
        pytest.skip("uses the POSIX socket path")
    srv = LinkServer(name=lp.pipe_name())
    assert srv.start()
    got = []
    srv.urlReceived.connect(got.append)
    msg = io.BytesIO()
    lp.write_native_message(msg, {"type": "open", "url": "https://v.com/e2e"})
    p = subprocess.Popen([sys.executable, str(ROOT / "native_host.py")], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         cwd=str(ROOT), env={**os.environ, "PYTHONPATH": str(ROOT)})
    p.stdin.write(msg.getvalue())
    p.stdin.flush()
    deadline = time.time() + 10
    while p.poll() is None and time.time() < deadline:
        QCoreApplication.processEvents()
        time.sleep(0.02)
    for _ in range(20):
        QCoreApplication.processEvents()
        time.sleep(0.01)
    out = p.stdout.read()
    srv.stop()
    assert p.returncode == 0 and got == ["https://v.com/e2e"]
    assert lp.read_native_message(io.BytesIO(out)) == {"ok": True, "started": False}


def test_running_from_source_registers_a_bat_wrapper(tmp_path, monkeypatch):
    monkeypatch.setenv("UVS_DATA_DIR", str(tmp_path))
    import types
    monkeypatch.setattr(bi, "os", types.SimpleNamespace(name="nt"))
    monkeypatch.setattr(sys, "executable", str(tmp_path / "python.exe"))
    bat = bi.host_exe_path()
    text = bat.read_text()
    assert bat.name == "uvs-native-host-dev.bat" and "native_host.py" in text and str(tmp_path / "python.exe") in text
    reg = FakeReg()
    assert bi.register(winreg_module=reg) and len(reg.values) == 3
    assert json.loads(Path(next(iter(reg.values.values()))).read_text())["path"] == str(bat)


def test_bring_to_front_never_raises(qapp):
    from PySide6.QtWidgets import QWidget
    from app.utils.foreground import bring_to_front
    w = QWidget()
    bring_to_front(w)
    assert w.isVisible()

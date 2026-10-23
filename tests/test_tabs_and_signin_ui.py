"""Multi-tab window, per-tab isolation, cookie manager dialog, and the in-app sign-in browser."""
import http.server
import os
import threading
import time

import pytest
from PySide6.QtWidgets import QApplication

from app.config import Settings
from app.models.download_job import DownloadJob, JobKind, JobStatus
from app.services import cookie_vault as cv
from app.services import ytdlp_service as yd


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def win(qapp, tmp_path, monkeypatch):
    import app.main_window as mw
    monkeypatch.setattr(mw.QTimer, "singleShot", staticmethod(lambda *a, **k: None))   # no modal setup dialog
    monkeypatch.setenv("UVS_DATA_DIR", str(tmp_path / "data"))
    cv.set_vault(cv.CookieVault(tmp_path / "cookies"))
    w = mw.MainWindow(Settings(temp_dir=str(tmp_path / "t"), download_dir=str(tmp_path / "o")))
    w.show()
    yield w
    w.controller.shutdown()
    cv.set_vault(None)


def info(url="https://example.com/v", title="My Video"):
    return yd.VideoInfo(url, "id1", title, "Me", 60.0, "", "G",
                        [yd.FormatInfo("1", "mp4", 360, 640, 25, "avc1", "aac", None, None)])


def test_starts_with_one_tab_and_new_tab_pseudo_tab(win):
    assert len(win.workspaces()) == 1 and win.tabs.tabText(win.tabs.count() - 1).strip("+ ") == "New Tab"
    assert win.tabs.widget(win.tabs.count() - 1) is win._plus


def test_new_tabs_are_independent_and_clicking_plus_adds_one(win):
    a = win.current()
    a._on_analyzed(info())
    a.clips.add(1, 5)
    win.tabs.setCurrentIndex(win.tabs.count() - 1)            # click "+ New Tab"
    b = win.current()
    assert b is not a and len(win.workspaces()) == 2 and win.tabs.widget(win.tabs.count() - 1) is win._plus
    assert b.video is None and len(b.clips) == 0 and len(a.clips) == 1
    assert win.tabs.tabText(0).startswith("My Video") and win.tabs.tabText(1) == "Tab 2"
    assert a.tag == "[Tab 1] " and b.tag == "[Tab 2] "


def test_closing_tabs_keeps_at_least_one(win, monkeypatch):
    win.new_tab()
    assert len(win.workspaces()) == 2
    win._close_tab(0)
    assert len(win.workspaces()) == 1 and win.current().tag == ""
    win._close_tab(0)
    assert len(win.workspaces()) == 1 and win.current() is not None


def test_closing_a_tab_with_clips_asks_first(win, monkeypatch):
    from PySide6.QtWidgets import QMessageBox
    ws = win.current()
    ws._on_analyzed(info())
    ws.clips.add(1, 5)
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.No))
    win._close_tab(0)
    assert win.current() is ws
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.Yes))
    win._close_tab(0)
    assert win.current() is not ws


def test_jobs_are_routed_to_the_tab_that_queued_them(win):
    a = win.current()
    b = win.new_tab()
    a._on_analyzed(info("https://example.com/a", "A"))
    b._on_analyzed(info("https://example.com/b", "B"))
    ca, cb = a.clips.add(1, 5), b.clips.add(1, 5)
    a._job_clip_ids.add(ca.id)
    job = DownloadJob(JobKind.CLIP, "x", clip_id=ca.id)
    assert a.owns_job(job) and not b.owns_job(job)
    a.on_job_changed(job)                                      # status text lands on A's clip list only
    b.on_job_changed(job)
    assert a.clip_list.table.rowCount() == 1


def test_title_tooltip_and_window_title(win):
    ws = win.current()
    ws._on_analyzed(info(title="A very long video title that should be shortened in the tab bar"))
    assert win.tabs.tabText(0).endswith("...") and "very long video title" in win.tabs.tabToolTip(0)
    assert "very long" in win.windowTitle()


def test_untrusted_title_is_not_rendered_as_html(win):
    ws = win.current()
    ws._on_analyzed(info(title="<img src=x onerror=1><b>bold</b>"))
    assert ws.info_title.textFormat().name == "PlainText" and "<b>" in ws.info_title.text()


def test_downloads_panel_toggle_and_queue_shows_it(win):
    win.btn_downloads.setChecked(False)
    assert not win.panel.isVisible()
    win._show_queue()
    assert win.panel.isVisible() and win.btn_downloads.isChecked()


def test_signin_chip_counts_saved_sign_ins(win):
    assert "none" in win.btn_chip.text()
    cv.get_vault().save("facebook.com", [cv.CookieRec(".facebook.com", "/", "c_user", "1", expires=int(time.time()) + 999)], "file")
    win._refresh_signin_chip()
    assert "1 saved" in win.btn_chip.text()


def test_cookie_manager_lists_views_and_removes(win, monkeypatch):
    from PySide6.QtWidgets import QMessageBox
    from app.widgets.cookie_manager import CookieManagerDialog, CookieViewDialog
    now = int(time.time())
    cv.get_vault().save("youtube.com", [cv.CookieRec(".youtube.com", "/", "SID", "secret1", expires=now + 999),
                                        cv.CookieRec(".youtube.com", "/", "PREF", "p", expires=0)], "app-browser")
    dlg = CookieManagerDialog(win)
    assert len(dlg.quick_buttons) >= 6 and {b.text() for b in dlg.quick_buttons} >= {"Facebook", "Instagram", "TikTok"}
    assert dlg.table.rowCount() == 1 and "Signed in" in dlg.table.item(0, 1).text()
    assert not dlg.empty.isVisibleTo(dlg) and not dlg.btn_remove.isEnabled() and not dlg.btn_view.isEnabled()
    dlg.table.selectRow(0)
    assert dlg.btn_remove.isEnabled() and dlg.btn_view.isEnabled()
    eid, site, label = dlg._selected()
    view = CookieViewDialog(eid, label)
    assert view.table.rowCount() == 2
    texts = [view.table.item(r, 2).text() for r in range(2)]
    assert "secret1" not in texts                                    # values hidden by default
    view.show_values.setChecked(True)
    assert "secret1" in [view.table.item(r, 2).text() for r in range(2)]
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.Yes))
    view.table.selectRow(0)
    view._delete_selected()
    assert view.table.rowCount() == 1 and cv.get_vault().entries()[0].count == 1
    dlg._remove()
    assert dlg.table.rowCount() == 0 and cv.get_vault().entries() == []


def test_remove_everything_button(win, monkeypatch):
    from PySide6.QtWidgets import QMessageBox
    from app.widgets.cookie_manager import CookieManagerDialog
    exp = int(time.time()) + 99
    cv.get_vault().save("facebook.com", [cv.CookieRec(".facebook.com", "/", "a", "1", expires=exp)], "file")
    cv.get_vault().save("vimeo.com", [cv.CookieRec(".vimeo.com", "/", "b", "1", expires=exp)], "file")
    dlg = CookieManagerDialog(win)
    assert dlg.table.rowCount() == 2 and dlg.btn_clear_all.isEnabled()
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.Yes))
    dlg._remove_all()
    assert dlg.table.rowCount() == 0 and not dlg.btn_clear_all.isEnabled()


def test_address_bar_input_is_normalised():
    from app.widgets.browser_login import normalize_address
    assert normalize_address("facebook.com") == "https://facebook.com"
    assert normalize_address("  http://a.b/x  ") == "http://a.b/x"
    assert normalize_address("javascript:alert(1)") == "" and normalize_address("two words") == "" and normalize_address("") == ""
    assert normalize_address("file:///etc/passwd") == ""


def test_clear_button_clears_video_timeline_and_clips(win, monkeypatch):
    from PySide6.QtWidgets import QMessageBox
    ws = win.current()
    ws.url_edit.setText("https://example.com/v")
    ws._on_analyzed(info())
    ws.clips.add(1, 5)
    asked = []
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: asked.append(1) or QMessageBox.No))
    ws.btn_clear.click()                                              # declined -> nothing lost
    assert asked and ws.video is not None and len(ws.clips) == 1 and ws.url_edit.text()
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.Yes))
    ws.btn_clear.click()
    assert ws.video is None and len(ws.clips) == 0 and ws.url_edit.text() == "" and ws.timeline.canvas.duration == 0
    assert ws.info_title.text() == "-" and ws.title == "New tab" and not ws.btn_all.isEnabled()
    assert win.tabs.tabText(0) == "Tab 1" and "Paste a video URL" in ws.preview.message.text()
    ws.btn_clear.click()                                              # nothing loaded: no question, no error
    assert len(asked) == 1


def test_busy_overlay_shows_during_analysis_and_hides_after(win):
    ws = win.current()
    ws.preview.set_busy("Analyzing link...")
    assert ws.preview.is_busy() and ws.preview.busy.timer.isActive()
    ws.preview.set_busy("Downloading preview file...", 42.0)
    assert ws.preview.busy.pct == 42.0
    ws.preview.show_message("done")                                   # any final message hides the spinner
    assert not ws.preview.is_busy() and not ws.preview.busy.timer.isActive()


def test_analyze_marks_tab_and_button_busy_until_done(win, monkeypatch):
    ws = win.current()
    texts = []
    ws.busyChanged.connect(texts.append)
    ws.preview.set_busy("Analyzing link...")
    ws.busyChanged.emit("Analyzing...")
    assert win.tabs.tabText(0) == "Analyzing..."
    ws.busyChanged.emit("")
    assert win.tabs.tabText(0) == "Tab 1"
    ws._on_analyzed(info(title="Done Title"))
    ws.busyChanged.emit("")
    assert win.tabs.tabText(0) == "Done Title"


def test_prompt_problem_offers_sign_in_for_the_failing_site(win, monkeypatch):
    """A sign-in error shows a 'Sign in to <site>' button; choosing it opens the browser window then retries."""
    import app.widgets.workspace as wsmod
    from PySide6.QtWidgets import QMessageBox
    ws = win.current()
    seen = {}

    def fake_exec(box):
        seen["buttons"] = [b.text() for b in box.buttons()]
        box_sign = [b for b in box.buttons() if b.text().startswith("Sign in to")][0]
        box._clicked = box_sign
        return 0
    monkeypatch.setattr(QMessageBox, "exec", fake_exec)
    monkeypatch.setattr(QMessageBox, "clickedButton", lambda box: box._clicked)
    calls = []
    monkeypatch.setattr(wsmod.browser_login, "sign_in", lambda url, parent=None: calls.append(url) or cv.VaultEntry(
        "youtube.com", "youtube.com", "YouTube / Google", "app-browser", 3))
    monkeypatch.setattr(wsmod.QTimer, "singleShot", staticmethod(lambda ms, fn: fn()))
    retried = []
    ws.prompt_problem("t", "Sign in to confirm you're not a bot", "", "signin", "https://www.youtube.com/watch?v=x",
                      retry=lambda: retried.append(1))
    assert any("Sign in to YouTube" in b for b in seen["buttons"]) and calls == ["https://www.youtube.com/watch?v=x"]
    assert retried == [1]


def test_failed_job_with_sign_in_error_prompts_once_per_site(win, monkeypatch):
    ws = win.current()
    ws._on_analyzed(info("https://www.facebook.com/watch/?v=1"))
    c = ws.clips.add(1, 5)
    ws._job_clip_ids.add(c.id)
    shown = []
    monkeypatch.setattr(ws, "_offer_job_signin", lambda j, u, h: shown.append(u))
    import app.widgets.workspace as wsmod
    monkeypatch.setattr(wsmod.QTimer, "singleShot", staticmethod(lambda ms, fn: fn()))

    class Spec:
        url = "https://www.facebook.com/watch/?v=1"
    job = DownloadJob(JobKind.CLIP, "x", payload=Spec(), clip_id=c.id)
    job.status, job.error = JobStatus.FAILED, "You must log in to view this private video"
    ws.on_job_changed(job)
    ws.on_job_changed(job)
    assert shown == ["https://www.facebook.com/watch/?v=1"]


# ---- the real in-app browser (needs QtWebEngine; opt-in because Chromium is heavy and may need --no-sandbox) -----
@pytest.mark.skipif(os.environ.get("UVS_TEST_WEBENGINE") != "1", reason="set UVS_TEST_WEBENGINE=1 to run the browser test")
def test_in_app_browser_collects_cookies_into_the_vault(qapp, tmp_path):
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Set-Cookie", "SID=abc; Path=/; Max-Age=86400")
            self.send_header("Set-Cookie", "sess=xyz; Path=/; HttpOnly")
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<h1>hi</h1>")

        def log_message(self, *a):
            pass
    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    cv.set_vault(cv.CookieVault(tmp_path / "cookies"))
    try:
        from app.widgets.browser_login import BrowserLoginDialog
        dlg = BrowserLoginDialog(f"http://127.0.0.1:{srv.server_address[1]}/")
        dlg.show()
        t0 = time.time()
        while time.time() - t0 < 15 and len(dlg.site_cookies()) < 2:
            qapp.processEvents()
            time.sleep(0.05)
        assert {c.name for c in dlg.site_cookies()} == {"SID", "sess"}
        dlg.save()
        e = dlg.entry
        assert e and e.count == 2 and cv.get_vault().cookiefile_for(f"http://127.0.0.1:{srv.server_address[1]}/")
    finally:
        srv.shutdown()
        cv.set_vault(None)


@pytest.mark.skipif(os.environ.get("UVS_TEST_WEBENGINE") != "1", reason="set UVS_TEST_WEBENGINE=1 to run the browser test")
def test_in_app_browser_is_still_signed_in_from_saved_cookies(qapp, tmp_path):
    seen = []

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(self.headers.get("Cookie", ""))
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<h1>hi</h1>")

        def log_message(self, *a):
            pass
    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    vault = cv.CookieVault(tmp_path / "cookies")
    vault.save("127.0.0.1", [cv.CookieRec("127.0.0.1", "/", "c_user", "u1", False, 0, False),
                             cv.CookieRec("127.0.0.1", "/", "xs", "s2", False, 0, True)], "test", "Local")
    cv.set_vault(vault)
    try:
        from app.widgets.browser_login import BrowserLoginDialog
        dlg = BrowserLoginDialog(f"http://127.0.0.1:{srv.server_address[1]}/")
        dlg.show()
        t0 = time.time()
        while time.time() - t0 < 15 and not seen:
            qapp.processEvents()
            time.sleep(0.05)
        assert dlg.restored == 2
        assert "c_user=u1" in seen[0] and "xs=s2" in seen[0], seen
    finally:
        srv.shutdown()
        cv.set_vault(None)


def test_history_loader_survives_old_or_broken_files(qapp, tmp_path, monkeypatch):
    import json
    from app.widgets import download_manager as dm
    monkeypatch.setenv("UVS_DATA_DIR", str(tmp_path))
    f = tmp_path / "history.json"
    f.write_text(json.dumps([{"path": "a.mp4"}, {"time": "t", "path": "b.mp4", "size": 5}, {"x": 1}, "junk", 3]))
    h = dm.HistoryWidget()
    assert [e["path"] for e in h.entries] == ["a.mp4", "b.mp4"] and h.table.rowCount() == 2
    f.write_text('{"not": "a list"}')
    assert dm.HistoryWidget().entries == []


def test_bad_saved_geometry_never_blocks_startup(qapp, tmp_path, monkeypatch):
    import app.main_window as mw
    monkeypatch.setattr(mw.QTimer, "singleShot", staticmethod(lambda *a, **k: None))
    assert mw.parse_geometry("") == b"" and mw.parse_geometry("zz not hex") == b""
    assert mw.parse_geometry("0a0b") == b"\x0a\x0b" and mw.parse_geometry("AQID") == b"\x01\x02\x03"
    for bad in ("zz not hex", "0x12", "b'\\x01'", "AQID", "00"):
        s = Settings(temp_dir=str(tmp_path / "t"), download_dir=str(tmp_path / "o"), window_geometry=bad)
        w = mw.MainWindow(s)
        w.controller.shutdown()


def test_encoder_comes_only_from_settings_and_is_not_in_the_tab(win, tmp_path):
    ws = win.current()
    assert not hasattr(ws, "encoder") and not hasattr(ws, "gpu_label")
    for mode in ("auto", "nvenc", "cpu"):
        ws.apply_settings(Settings(temp_dir=str(tmp_path / "t"), download_dir=str(tmp_path / "o"), hw_encoder=mode))
        assert ws.settings.hw_encoder == mode


def test_setup_dialog_explains_the_chosen_encoder(qapp, monkeypatch):
    from app.services import ffmpeg_service as fs
    from app.widgets.dependency_dialog import DependencyDialog

    class St:
        ffmpeg_ok, ffmpeg_path = True, "ff"
    monkeypatch.setattr(fs, "probe_gpu", lambda _f: fs.GpuInfo(True, True, "NVIDIA GeForce RTX 3060"))
    d = DependencyDialog("", None, "auto")
    t = d._encoder_text(St)
    assert "RTX 3060" in t and "will use your GPU" in t and "Auto" in t
    assert "GPU (NVENC)" in DependencyDialog("", None, "nvenc")._encoder_text(St)
    monkeypatch.setattr(fs, "probe_gpu", lambda _f: (_ for _ in ()).throw(AssertionError("must not probe in CPU mode")))
    cpu = DependencyDialog("", None, "cpu")._encoder_text(St)
    assert "CPU rendering is selected" in cpu and "GPU is not used" in cpu
    monkeypatch.setattr(fs, "probe_gpu", lambda _f: fs.GpuInfo(False, False))
    assert "Rendering will use the CPU" in DependencyDialog("", None, "auto")._encoder_text(St)
    assert "cannot be used" in DependencyDialog("", None, "nvenc")._encoder_text(St)
    St.ffmpeg_ok = False
    assert "after FFmpeg is installed" in DependencyDialog("", None, "auto")._encoder_text(St)


def test_update_button_appears_when_new_version_found(qapp, tmp_path, monkeypatch):
    monkeypatch.setenv("UVS_DATA_DIR", str(tmp_path))
    from app.config import Settings
    from app.main_window import MainWindow
    from app.services.update_service import UpdateInfo
    win = MainWindow(Settings())
    assert win.btn_new_version.isHidden()
    win._on_app_update(UpdateInfo("9.9.9", "https://example.com/app.zip", "notes"))
    assert not win.btn_new_version.isHidden() and "9.9.9" in win.btn_new_version.text()
    win.close()


def test_update_dialog_runs_and_reports_failure(qapp, tmp_path, monkeypatch):
    from app.services.update_service import UpdateInfo
    from app.widgets.update_dialog import UpdateDialog
    dlg = UpdateDialog(UpdateInfo("9.9.9", "https://x/y", "", "https://127.0.0.1:9/none.zip", "", 0))
    t0 = time.time()
    while time.time() - t0 < 15 and dlg.btn.text() != "Close":
        qapp.processEvents()
        time.sleep(0.05)
    assert dlg.btn.text() == "Close" and "Nothing was changed" in dlg.status.text() and dlg.new_dir is None


def test_settings_shows_version_update_check_and_no_icon_or_ytdlp_row(qapp, tmp_path, monkeypatch):
    monkeypatch.setenv("UVS_DATA_DIR", str(tmp_path))
    from app import __version__
    from app.config import Settings
    from app.services.update_service import UpdateInfo
    from app.widgets.settings_dialog import SettingsDialog
    dlg = SettingsDialog(Settings())
    assert __version__ in dlg.about.text()
    assert not hasattr(dlg, "icon_row") and not hasattr(dlg, "btn_update") and not hasattr(dlg, "yt_label")
    got = []
    dlg.updateRequested.connect(got.append)
    dlg._update_checked("latest", UpdateInfo(__version__, "https://x/y"))
    assert "latest version" in dlg.check_result.text() and dlg.btn_view.isHidden()
    dlg._update_checked("available", UpdateInfo("9.9.9", "https://x/y"))
    assert not dlg.btn_view.isHidden() and "9.9.9" in dlg.check_result.text()
    dlg.btn_view.click()
    assert got and got[0].version == "9.9.9"
    dlg._update_checked("error", None)
    assert "Could not check" in dlg.check_result.text()

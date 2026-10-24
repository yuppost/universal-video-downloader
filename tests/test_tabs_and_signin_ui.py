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
    d = DependencyDialog("", None, "auto", check_on_open=False)
    t = d._encoder_text(St)
    assert "RTX 3060" in t and "will use your GPU" in t and "Auto" in t
    assert "GPU (NVENC)" in DependencyDialog("", None, "nvenc", check_on_open=False)._encoder_text(St)
    monkeypatch.setattr(fs, "probe_gpu", lambda _f: (_ for _ in ()).throw(AssertionError("must not probe in CPU mode")))
    cpu = DependencyDialog("", None, "cpu", check_on_open=False)._encoder_text(St)
    assert "CPU rendering is selected" in cpu and "GPU is not used" in cpu
    monkeypatch.setattr(fs, "probe_gpu", lambda _f: fs.GpuInfo(False, False))
    assert "Rendering will use the CPU" in DependencyDialog("", None, "auto", check_on_open=False)._encoder_text(St)
    assert "cannot be used" in DependencyDialog("", None, "nvenc", check_on_open=False)._encoder_text(St)
    St.ffmpeg_ok = False
    assert "after FFmpeg is installed" in DependencyDialog("", None, "auto", check_on_open=False)._encoder_text(St)


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


def test_settings_has_one_update_button_that_opens_the_updates_box(qapp, tmp_path, monkeypatch):
    monkeypatch.setenv("UVS_DATA_DIR", str(tmp_path))
    from app import __version__
    from app.config import Settings
    from app.services.update_service import UpdateInfo
    from app.widgets import dependency_dialog as dd
    from app.widgets.settings_dialog import SettingsDialog
    dlg = SettingsDialog(Settings())
    assert __version__ in dlg.about.text()
    assert not hasattr(dlg, "icon_row") and not hasattr(dlg, "yt_label")
    assert not hasattr(dlg, "btn_check") and not hasattr(dlg, "btn_setup") and not hasattr(dlg, "btn_view")
    assert dlg.btn_update.text() == "Update"

    # the box lists the app and the tools, each with a clear "up to date / update available" line
    box = dd.DependencyDialog("", None, "auto", check_on_open=False)
    assert set(box.rows) == {"app", "ffmpeg", "ytdlp", "deno", "gpu"}
    box._on_checked({"ffmpeg": ("uptodate", "9.0.2"), "ytdlp": ("available", "2099.1.1"), "deno": ("unknown", "")},
                    "latest", None)
    assert "Up to date" in box.badges["ffmpeg"].text() and "Up to date" in box.badges["app"].text()
    assert "Update available: 2099.1.1" in box.badges["ytdlp"].text()
    assert "Could not check" in box.badges["deno"].text()
    assert box.rows["ytdlp"][1].objectName() == "primary" and box.rows["ffmpeg"][1].objectName() == ""
    # a newer app version: Update closes the box and hands the info on
    got = []
    box.updateRequested.connect(got.append)
    box._on_checked({}, "available", UpdateInfo("9.9.9", "https://x/y"))
    assert "9.9.9" in box.badges["app"].text()
    box.rows["app"][1].click()
    assert got and got[0].version == "9.9.9"
    # Settings closes itself (keeping edits) when the app update was chosen inside the box
    dlg._app_update_chosen(got[0])
    assert dlg.pending_update.version == "9.9.9" and dlg.result() == 1


def test_updates_box_app_row_says_so_when_there_is_no_update(qapp, monkeypatch):
    from PySide6.QtWidgets import QMessageBox
    from app.widgets import dependency_dialog as dd
    shown = []
    monkeypatch.setattr(QMessageBox, "information", staticmethod(lambda *a, **k: shown.append(a[2])))
    box = dd.DependencyDialog("", None, "auto", check_on_open=False)
    box._on_checked({}, "latest", None)
    box.rows["app"][1].click()
    assert shown and "latest version" in shown[0]


def test_crop_enabled_without_selection_and_section_arrows(tmp_path):
    """Crop works as soon as a video is loaded: it makes (or picks) a clip itself. Section headers use drawn chevrons."""
    from PySide6.QtWidgets import QApplication
    from app.config import Settings
    from app.main_window import MainWindow
    from app.services import ytdlp_service as yd
    app = QApplication.instance() or QApplication([])
    win = MainWindow(Settings(temp_dir=str(tmp_path / "t"), download_dir=str(tmp_path / "o")))
    w = win.current()
    w._on_analyzed(yd.VideoInfo("https://example.com/v", "vid", "T", "", 60.0, "", "G",
                                [yd.FormatInfo("1", "mp4", 360, 640, 25, "avc1", "aac", None, None)]))
    app.processEvents()
    assert w.crop_controls.isEnabled() and len(w.clips) == 0
    assert w.sec_crop.header.text() == "Crop / Aspect Ratio" and not w.sec_crop.header.icon().isNull()
    assert not w.out_dir.isVisible() and not w.btn_out.isVisible()
    idx = w.crop_controls.combo.findData("16:9") if w.crop_controls.combo.findData("16:9") >= 0 else 1
    w.crop_controls.combo.setCurrentIndex(idx)
    assert len(w.clips) == 1 and w.selected_id() is not None
    assert w.clips.clips[0].crop.preset == w.crop_controls.combo.currentData()
    win.controller.shutdown()


def test_downloads_toggle_is_an_icon_shown_only_while_hidden_and_preview_is_a_queue_job(tmp_path, monkeypatch):
    from PySide6.QtWidgets import QApplication, QMessageBox
    from app.config import Settings
    from app.main_window import MainWindow
    from app.models.download_job import JobKind
    from app.services import ytdlp_service as yd
    from app.widgets import workspace as wsmod
    app = QApplication.instance() or QApplication([])
    win = MainWindow(Settings(temp_dir=str(tmp_path / "t"), download_dir=str(tmp_path / "o"), ffmpeg_path="ffmpeg"))
    win.resize(1500, 900)
    win.show()
    win._panel_user_choice = True
    win._update_panel_visibility()
    app.processEvents()
    assert win.panel.isVisible() and not win.btn_downloads.isVisible()
    assert win.btn_hide_panel.text() == "" and not win.btn_hide_panel.icon().isNull()
    win.btn_hide_panel.click()
    assert not win.panel.isVisible() and win.btn_downloads.isVisible() and win.btn_downloads.text() == ""
    win.btn_downloads.click()
    assert win.panel.isVisible() and not win.btn_downloads.isVisible()

    w = win.current()
    assert not w.btn_preview_dl.isVisible()
    w._on_analyzed(yd.VideoInfo("https://example.com/v", "vid", "T", "", 60.0, "", "G",
                                [yd.FormatInfo("1", "mp4", 360, 640, 25, "avc1", "aac", None, None)]))
    app.processEvents()
    assert w.btn_preview_dl.isVisible()                                # the option is always there, not only on errors
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.Yes))
    monkeypatch.setattr(wsmod, "require_ffmpeg", lambda p: "ffmpeg")
    monkeypatch.setattr(win.controller, "_pump", lambda: None)           # do not really download
    w.request_preview_download()
    jobs = [j for j in win.controller.queue.jobs if j.kind == JobKind.SOURCE]
    assert len(jobs) == 1 and "Preview" in jobs[0].name and jobs[0].payload.selector == wsmod.PREVIEW_SELECTOR
    assert not w.btn_preview_dl.isVisible()
    win.controller.cancel(jobs[0].id)
    app.processEvents()
    assert not w._preview_busy
    win.controller.shutdown()


def test_settings_wheel_restore_defaults_and_trimmed_options(tmp_path, monkeypatch):
    from PySide6.QtCore import QPoint, QPointF, Qt
    from PySide6.QtGui import QWheelEvent
    from PySide6.QtWidgets import QApplication, QMessageBox
    from app.config import Settings
    from app.widgets.settings_dialog import SettingsDialog
    app = QApplication.instance() or QApplication([])
    s = Settings(temp_dir=str(tmp_path / "t"), download_dir=str(tmp_path / "o"), theme="light", max_concurrent=5,
                 trim_mode="accurate", filename_template="x_{index}")
    dlg = SettingsDialog(s)
    dlg.show()
    app.processEvents()
    assert not hasattr(dlg, "check_start")
    before = dlg.theme.currentIndex()
    ev = QWheelEvent(QPointF(5, 5), QPointF(5, 5), QPoint(0, 0), QPoint(0, -120), Qt.NoButton, Qt.NoModifier,
                     Qt.NoScrollPhase, False)
    app.sendEvent(dlg.theme, ev)
    app.sendEvent(dlg.concurrent, ev)
    assert dlg.theme.currentIndex() == before and dlg.concurrent.value() == 5      # not focused: the wheel does nothing
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.Yes))
    dlg.btn_restore.click()
    d = Settings()
    assert dlg.theme.currentData() == "dark" and dlg.concurrent.value() == d.max_concurrent
    assert dlg.trim.currentData() == "fast" and dlg.template.text() == d.filename_template
    assert dlg.download_dir[0].text() == d.download_dir
    assert s.theme == "light"                                                     # only applied on Save


def test_mp3_mode_labels_waveform_audio_folder_and_settings_rows(tmp_path, monkeypatch):
    from PySide6.QtWidgets import QApplication
    from app.config import Settings
    from app.main_window import MainWindow
    from app.services import ytdlp_service as yd
    from app.widgets.settings_dialog import SettingsDialog
    app = QApplication.instance() or QApplication([])
    s = Settings(temp_dir=str(tmp_path / "t"), download_dir=str(tmp_path / "v"), audio_dir=str(tmp_path / "a"),
                 ffmpeg_path="C:/custom/ffmpeg.exe")
    # settings: audio folder row, no FFmpeg executable row, custom path kept
    dlg = SettingsDialog(s)
    dlg.show()
    app.processEvents()
    assert dlg.audio_dir[0].text() == str(tmp_path / "a") and not hasattr(dlg, "ff_status")
    assert not dlg.ffmpeg[1].isVisible()
    assert dlg.result_settings().ffmpeg_path == "C:/custom/ffmpeg.exe"
    assert Settings().audio_dir.endswith("Audio") and Settings().audio_dir != Settings().download_dir

    win = MainWindow(s)
    w = win.current()
    w._on_analyzed(yd.VideoInfo("https://example.com/v", "vid", "T", "", 60.0, "", "G",
                                [yd.FormatInfo("1", "mp4", 360, 640, 25, "avc1", "aac", None, None)]))
    app.processEvents()
    assert w.btn_full.text() == "Full Video" and w.btn_all.text() == "Download All Clips"
    assert not w.timeline.canvas.wave_mode
    w.fmt.setCurrentIndex(w.fmt.findData("mp3"))
    app.processEvents()
    assert w.btn_full.text() == "Full MP3" and w.btn_all.text() == "Download All MP3 Clips"
    assert w.btn_sel.text() == "Selected MP3" and w.timeline.canvas.wave_mode
    assert "audio" in w.btn_preview_dl.toolTip().lower()
    w.timeline.set_peaks([0.1, 0.9, 0.4] * 100)
    w.timeline.canvas.grab()                                   # paints the waveform strip without errors
    w.fmt.setCurrentIndex(w.fmt.findData("mp4"))
    assert w.btn_full.text() == "Full Video" and not w.timeline.canvas.wave_mode
    win.controller.shutdown()


def test_waveform_peaks_math():
    from array import array
    from app.services.waveform_service import bin_count, peaks_from_samples
    a = array("h", [0] * 1000 + [16384] * 1000 + [-32768] * 1000)
    pk = peaks_from_samples(a, 3)
    assert pk == [0.0, 0.5, 1.0]
    assert bin_count(1) == 200 and bin_count(100) == 400 and bin_count(10 ** 6) == 4000
    assert peaks_from_samples(array("h"), 5) == []


def test_updates_box_never_stays_on_checking(qapp, monkeypatch):
    """Each item reports on its own; one that hangs is ended by the timer instead of spinning forever."""
    import threading as th
    from app.services import dependency_service as deps
    from app.widgets import dependency_dialog as dd
    release = th.Event()
    monkeypatch.setattr(deps, "check_ffmpeg_update", lambda *a, **k: ("uptodate", "9.0.2"))
    monkeypatch.setattr(deps, "check_ytdlp_update", lambda: (release.wait(30), ("uptodate", "x"))[1])       # hangs
    monkeypatch.setattr(deps, "check_deno_update", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr("app.services.update_service.check_update", lambda *a, **k: ("latest", None))
    monkeypatch.setattr(dd.DependencyDialog, "CHECK_TIMEOUT_MS", 600)
    box = dd.DependencyDialog("", None, "auto", check_on_open=True)
    t0 = time.time()
    while time.time() - t0 < 5 and any("Checking" in b.text() for k, b in box.badges.items() if k != "gpu"):
        qapp.processEvents()
        time.sleep(0.02)
    release.set()
    assert "Up to date" in box.badges["ffmpeg"].text() and "Up to date" in box.badges["app"].text()
    assert "Could not check" in box.badges["deno"].text()                  # an exception is not a hang either
    assert "Could not check" in box.badges["ytdlp"].text()                 # the hanging one was ended by the timer
    assert not any("Checking" in b.text() for b in box.badges.values())

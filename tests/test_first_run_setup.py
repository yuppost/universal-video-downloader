"""First-launch setup screen (fake installers, no network)."""
import threading
import time

import pytest
from PySide6.QtWidgets import QApplication

from app.config import Settings
from app.services.errors import FriendlyError
from app.widgets import first_run_setup as fr


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def wait(qapp, cond, secs=10):
    t0 = time.time()
    while time.time() - t0 < secs and not cond():
        qapp.processEvents()
        time.sleep(0.02)
    return cond()


def test_nothing_missing_means_no_setup(monkeypatch):
    from app.services import dependency_service as deps
    ok = deps.DepStatus("/f", "/p", "", "present", True, False, "deno", True)
    monkeypatch.setattr(deps, "quick_check", lambda *a, **k: ok)
    assert fr.missing_tools(Settings()) == [] and not fr.needs_setup(Settings())
    bad = deps.DepStatus(None, None, "", "present", True, False, "", True)
    monkeypatch.setattr(deps, "quick_check", lambda *a, **k: bad)
    assert fr.missing_tools(Settings()) == ["ffmpeg", "deno"]
    s = Settings()
    s.setup_prompt_dismissed = True                      # Deno is optional: not asked again after "Skip"
    assert fr.missing_tools(s) == ["ffmpeg"]


def test_downloads_missing_tools_then_closes_itself(qapp, monkeypatch):
    state = {"ffmpeg": False, "deno": False}
    calls = []
    monkeypatch.setattr(fr, "missing_tools_of", lambda s, tools: [t for t in tools if not state[t]])

    def make(key):
        def inst(progress, cancel):
            calls.append(key)
            progress(50.0, f"Downloading {key}", 1000.0)
            state[key] = True
        return inst
    dlg = fr.FirstRunSetup(Settings(), ["ffmpeg", "deno"], {"ffmpeg": make("ffmpeg"), "deno": make("deno")})
    dlg.show()
    assert wait(qapp, lambda: not dlg.isVisible())
    assert dlg.result_ok and calls == ["ffmpeg", "deno"] and "✔" in dlg.rows["ffmpeg"].text()


def test_error_shows_message_and_retry_continues(qapp, monkeypatch):
    state = {"ffmpeg": False}
    attempts = []
    monkeypatch.setattr(fr, "missing_tools_of", lambda s, tools: [t for t in tools if not state[t]])

    def flaky(progress, cancel):
        attempts.append(1)
        if len(attempts) == 1:
            raise FriendlyError("Could not download FFmpeg.", "HTTP 503")
        state["ffmpeg"] = True
    dlg = fr.FirstRunSetup(Settings(), ["ffmpeg"], {"ffmpeg": flaky})
    dlg.show()
    assert wait(qapp, lambda: not dlg.btn_retry.isHidden())
    assert "Could not download FFmpeg." in dlg.error.text() and not dlg.error.isHidden() and not dlg.result_ok
    dlg.btn_retry.click()
    assert wait(qapp, lambda: not dlg.isVisible())
    assert dlg.result_ok and len(attempts) == 2


def test_skip_remembers_that_deno_is_optional(qapp, monkeypatch):
    monkeypatch.setattr(fr, "missing_tools_of", lambda s, tools: list(tools))
    gate = threading.Event()

    def slow(progress, cancel):
        gate.wait(5)
    s = Settings()
    dlg = fr.FirstRunSetup(s, ["deno"], {"deno": slow})
    dlg.show()
    qapp.processEvents()
    dlg.btn_skip.click()
    gate.set()
    assert dlg.skipped and s.setup_prompt_dismissed
    dlg._thread.join(5)                                  # the worker must be finished before the dialog goes away
    qapp.processEvents()
    dlg.deleteLater()

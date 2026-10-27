"""Main window: browser-style tabs (one Workspace per video), a shared Downloads panel and the cookie/sign-in chip."""

from __future__ import annotations

import logging
import os
import shutil
import threading
from pathlib import Path
from typing import List, Optional

from PySide6.QtCore import QObject, Qt, QTimer, Signal, Slot
from PySide6.QtGui import QDragEnterEvent, QDropEvent, QKeySequence, QShortcut
from PySide6.QtWidgets import (QApplication, QDialog, QFrame, QHBoxLayout, QLabel, QMainWindow, QMessageBox, QPushButton,
                               QProgressBar, QSplitter, QTabBar, QTabWidget, QToolButton, QVBoxLayout, QWidget)

from app import APP_NAME, UPDATE_INFO_URL, __version__, appicon, theme
from app.config import Settings, save_settings
from app.models.download_job import DownloadJob, JobKind, JobStatus
from app.services import cookie_vault as cv
from app.services import dependency_service as deps
from app.services import ytdlp_service as yd
from app.utils.paths import dir_size, human_size
from app.widgets.cookie_manager import CookieManagerDialog
from app.widgets.dependency_dialog import DependencyDialog
from app.widgets.download_manager import DownloadManager
from app.widgets.settings_dialog import SettingsDialog
from app.widgets.workspace import Workspace
from app.workers.job_controller import JobController

log = logging.getLogger("uvs.main")
PANEL_AUTO_HIDE_W = 1250          # below this width the Downloads panel starts hidden (toggle button shows it)


def parse_geometry(saved: str) -> bytes:
    """Accepts the hex string we write; also tolerates base64 / stray text written by older versions."""
    import base64
    text = (saved or "").strip()
    if not text:
        return b""
    try:
        return bytes.fromhex(text)
    except ValueError:
        pass
    try:
        return base64.b64decode(text, validate=True)
    except ValueError:
        return b""


class _VersionBridge(QObject):
    latest = Signal(str)
    app_update = Signal(object)


class MainWindow(QMainWindow):
    def __init__(self, settings: Settings) -> None:
        super().__init__()
        self.settings = settings
        self.setWindowTitle(f"{APP_NAME} {__version__}")
        self.setMinimumSize(640, 480)
        scr = QApplication.primaryScreen().availableGeometry() if QApplication.primaryScreen() else None
        self.resize(min(1500, scr.width() - 40) if scr else 1500, min(900, scr.height() - 60) if scr else 900)
        self.setAcceptDrops(True)
        self.controller = JobController(settings.max_concurrent, self)
        self._tab_counter = 0
        self._panel_user_choice: Optional[bool] = None
        self._vbridge = _VersionBridge()
        self._vbridge.latest.connect(self._on_latest_version)
        self._vbridge.app_update.connect(self._on_app_update)
        self._update_info = None
        yd.set_cookie_spec(settings.cookie_spec())
        try:
            cv.get_vault()
        except OSError as exc:                       # unwritable data folder: the app still works without saved sign-ins
            log.warning("cookie vault unavailable: %s", exc)

        self._build_ui()
        self._wire()
        self._shortcuts()
        self.new_tab()
        self._restore_geometry(settings.window_geometry)
        self._fit_to_screen()
        self._update_panel_visibility()
        self._refresh_signin_chip()
        self.statusBar().showMessage(f"Ready - yt-dlp {yd.ytdlp_version()}")
        if settings.ytdlp_check_on_start:
            threading.Thread(target=self._check_latest, daemon=True).start()
        self.setWindowIcon(appicon.load_icon(settings.icon_path))
        st = deps.quick_check(settings.ffmpeg_path)      # files only: no program is started during startup
        QTimer.singleShot(1200, lambda: threading.Thread(target=yd.prewarm, daemon=True).start())
        if settings.check_app_updates and UPDATE_INFO_URL:
            QTimer.singleShot(4000, lambda: threading.Thread(target=self._check_app_update, daemon=True).start())
        if not st.ytdlp_ok:
            QTimer.singleShot(400, self.open_setup)       # development only: yt-dlp is bundled in the packaged app
        elif not st.ffmpeg_ok:                            # FFmpeg/Deno are fetched by the first-run screen (main.py)
            self.statusBar().showMessage("FFmpeg is not installed yet - open Settings > Setup to download it.")

    def _check_app_update(self) -> None:
        from app.services.update_service import fetch_update
        info = fetch_update(UPDATE_INFO_URL, __version__)
        if info:
            self._vbridge.app_update.emit(info)

    def _on_app_update(self, info) -> None:
        self._update_info = info
        self.btn_new_version.setText(f"Update {info.version} available")
        self.btn_new_version.setToolTip("A newer version of Universal Video Studio is available - click for details")
        self.btn_new_version.show()

    def _show_app_update(self) -> None:
        info = self._update_info
        if not info:
            return
        from app.services import self_update
        from app.widgets.update_dialog import UpdateAvailableDialog
        one_click = bool(info.asset_url) and self_update.can_self_update()
        dlg = UpdateAvailableDialog(info, __version__, one_click, self)
        dlg.exec()
        if dlg.action == "update":
            self._start_self_update(info)

    def _start_self_update(self, info) -> None:
        from app.services import self_update
        from app.widgets.update_dialog import UpdateDialog
        busy = [j for j in self.controller.queue.jobs if not j.is_finished]
        if busy and QMessageBox.question(
                self, "Update", f"{len(busy)} job(s) are still running or queued and will be cancelled by the update. "
                "Continue?") != QMessageBox.Yes:
            return
        dlg = UpdateDialog(info, self)
        if dlg.exec() != QDialog.Accepted or dlg.new_dir is None:
            return
        try:
            self._quitting_for_update = True
            self_update.launch_helper(dlg.new_dir)
        except OSError as exc:
            self._quitting_for_update = False
            QMessageBox.warning(self, "Update", f"The update could not be started: {exc}")
            return
        self.close()                                   # saves settings; the helper restarts the app with the new files
        QApplication.instance().quit()
        QTimer.singleShot(6000, lambda: os._exit(0))   # never linger: a still-running old process would block the update

    def _restore_geometry(self, saved: str) -> None:
        """Restore the saved window size; a missing or unreadable value (older versions) is simply ignored."""
        data = parse_geometry(saved)
        if data:
            try:
                self.restoreGeometry(data)
            except Exception:  # noqa: BLE001 - never block startup over window placement
                log.warning("Could not restore window geometry")

    # ------------------------------------------------------------------ UI
    def _build_ui(self) -> None:
        self.tabs = QTabWidget()
        self.tabs.setObjectName("studioTabs")
        self.tabs.setDocumentMode(True)
        self.tabs.setElideMode(Qt.ElideRight)
        self.tabs.tabBar().setExpanding(False)
        self.tabs.tabBar().setUsesScrollButtons(True)

        corner = QWidget()
        cl = QHBoxLayout(corner)
        cl.setContentsMargins(0, 0, 8, 0)
        cl.setSpacing(6)
        self.btn_signins = QPushButton("Cookies && sign-ins")
        self.btn_signins.setToolTip("Sign in to a site once - the app saves the cookies and reuses them "
                                    "(private, age-restricted and 'not a bot' videos)")
        self.btn_downloads = QPushButton(theme.icon("chev_left", size=18), "")     # only visible while the panel is hidden
        self.btn_downloads.setCheckable(True)
        self.btn_downloads.setToolTip("Show the downloads panel (queue and export history)")
        self.btn_downloads.setFixedWidth(40)
        self.btn_downloads.setVisible(False)
        self.btn_settings = QPushButton(theme.icon("settings", size=16), "Settings")
        self.btn_settings.setToolTip("Open settings (Ctrl+,)")
        for b in (self.btn_signins, self.btn_settings, self.btn_downloads):
            cl.addWidget(b)
        self.tabs.setCornerWidget(corner, Qt.TopRightCorner)

        self.manager = DownloadManager()
        self.panel = QFrame()
        self.panel.setObjectName("card")
        pl = QVBoxLayout(self.panel)
        pl.setContentsMargins(6, 6, 6, 6)
        head = QHBoxLayout()
        title = QLabel("Downloads")
        title.setObjectName("panelTitle")
        self.btn_hide_panel = QPushButton(theme.icon("chev_right", size=18), "")
        self.btn_hide_panel.setToolTip("Hide the downloads panel")
        self.btn_hide_panel.setFixedWidth(40)
        head.addWidget(title)
        head.addStretch(1)
        head.addWidget(self.btn_hide_panel)
        pl.addLayout(head)
        pl.addWidget(self.manager, 1)
        self.panel.setMinimumWidth(340)
        self.manager.set_compact(True)         # icon-only toolbar: the panel is narrow

        self.split = QSplitter(Qt.Horizontal)
        self.split.setChildrenCollapsible(False)
        self.split.addWidget(self.tabs)
        self.split.addWidget(self.panel)
        self.split.setStretchFactor(0, 1)
        self.split.setStretchFactor(1, 0)
        self.split.setSizes([1100, 460])
        wrap = QWidget()
        wl = QVBoxLayout(wrap)
        wl.setContentsMargins(8, 6, 8, 0)
        wl.addWidget(self.split)
        self.setCentralWidget(wrap)

        sb = self.statusBar()
        self.queue_label = QLabel("")
        self.queue_label.setObjectName("muted")
        self.btn_chip = QPushButton("")
        self.btn_chip.setObjectName("signin")
        self.btn_chip.setToolTip("Open the cookie manager")
        self.queue_bar = QProgressBar()                  # animated while any download/export job is running
        self.queue_bar.setFixedSize(140, 12)
        self.queue_bar.setTextVisible(False)
        self.queue_bar.hide()
        sb.addPermanentWidget(self.queue_label)
        sb.addPermanentWidget(self.queue_bar)
        sb.addPermanentWidget(self.btn_chip)
        self.btn_new_version = QPushButton("")           # appears only when a newer version of the app exists
        self.btn_new_version.setObjectName("signin")
        self.btn_new_version.hide()
        self.btn_new_version.clicked.connect(self._show_app_update)
        sb.addPermanentWidget(self.btn_new_version)

        self._plus = QWidget()                    # the "+ New Tab" pseudo tab at the end
        self.tabs.addTab(self._plus, "+  New Tab")
        self.tabs.tabBar().setTabButton(0, QTabBar.RightSide, None)

    def _wire(self) -> None:
        self.btn_settings.clicked.connect(self.open_settings)
        self.btn_signins.clicked.connect(self.open_cookie_manager)
        self.btn_chip.clicked.connect(self.open_cookie_manager)
        self.btn_downloads.toggled.connect(self._toggle_panel)
        self.btn_hide_panel.clicked.connect(lambda: self.btn_downloads.setChecked(False))
        self.tabs.currentChanged.connect(self._tab_changed)

        c = self.controller
        c.job_added.connect(self.manager.add_job)
        c.job_changed.connect(self._on_job_changed)
        c.job_removed.connect(self.manager.remove_job)
        c.clip_completed.connect(self._on_clip_completed)
        self.manager.startPauseToggled.connect(c.set_paused)
        self.manager.cancelRequested.connect(c.cancel)
        self.manager.retryRequested.connect(self._retry)
        self.manager.removeRequested.connect(c.remove)
        self.manager.clearFinished.connect(c.clear_finished)

    def _shortcuts(self) -> None:
        def sc(keys: str, fn) -> None:
            QShortcut(QKeySequence(keys), self, activated=fn)

        def cur(fn):
            return lambda: (lambda w: fn(w) if w else None)(self.current())

        sc("Ctrl+T", lambda: self.new_tab())
        sc("Ctrl+W", lambda: self._close_tab(self.tabs.currentIndex()))
        sc("Ctrl+L", cur(lambda w: (w.url_edit.setFocus(), w.url_edit.selectAll())))
        sc("Ctrl+N", cur(lambda w: w.add_clip_at(w.timeline.canvas.playhead)))
        sc("Ctrl+D", cur(lambda w: w._clip_action("duplicate", w.selected_id())))
        sc("Ctrl+E", cur(lambda w: w.export_clips(False)))
        sc("Ctrl+,", self.open_settings)
        sc("Space", cur(lambda w: w._if_not_typing(w.preview.toggle_play)))
        sc("Left", cur(lambda w: w._if_not_typing(lambda: w.preview.seek(w.preview.position() - 5))))
        sc("Right", cur(lambda w: w._if_not_typing(lambda: w.preview.seek(w.preview.position() + 5))))

    # ------------------------------------------------------------------ tabs
    def workspaces(self) -> List[Workspace]:
        return [self.tabs.widget(i) for i in range(self.tabs.count()) if isinstance(self.tabs.widget(i), Workspace)]

    def current(self) -> Optional[Workspace]:
        w = self.tabs.currentWidget()
        return w if isinstance(w, Workspace) else None

    def new_tab(self, url: str = "", analyze: bool = False) -> Workspace:
        ws = Workspace(self.settings, self.controller)
        self._tab_counter += 1
        ws.num = self._tab_counter
        ws.statusMessage.connect(lambda t, ms, w=ws: self._status_from(w, t, ms))
        ws.titleChanged.connect(lambda t, w=ws: self._title_changed(w, t))
        ws.setupRequested.connect(lambda t: self._offer_setup(t) if t else self.open_setup())
        ws.cookiesChanged.connect(self._refresh_signin_chip)
        ws.exportQueued.connect(self._show_queue)
        ws.busyChanged.connect(lambda t, w=ws: self._busy_changed(w, t))
        idx = self.tabs.count() - 1                       # before the "+ New Tab" pseudo tab
        self.tabs.insertTab(idx, ws, f"Tab {ws.num}")
        close = QToolButton()
        close.setAutoRaise(True)
        close.setIcon(theme.icon("cancel", size=12))
        close.setToolTip("Close tab (Ctrl+W)")
        close.setCursor(Qt.PointingHandCursor)
        close.clicked.connect(lambda _=False, w=ws: self._close_tab(self.tabs.indexOf(w)))
        self.tabs.tabBar().setTabButton(idx, QTabBar.RightSide, close)
        self.tabs.setCurrentIndex(idx)
        self._retag()
        ws.update_layout_mode()
        if url:
            ws.url_edit.setText(url)
            if analyze:
                ws.analyze("auto")
        return ws

    def _retag(self) -> None:
        many = len(self.workspaces()) > 1
        for ws in self.workspaces():
            ws.tag = f"[Tab {ws.num}] " if many else ""

    def _tab_changed(self, idx: int) -> None:
        if idx >= 0 and self.tabs.widget(idx) is self._plus:
            self.new_tab()
            return
        ws = self.current()
        if ws:
            self.setWindowTitle(f"{ws.title} - {APP_NAME}" if ws.video else f"{APP_NAME} {__version__}")

    def _close_tab(self, idx: int) -> None:
        w = self.tabs.widget(idx)
        if not isinstance(w, Workspace):
            return
        if w.video and len(w.clips) and QMessageBox.question(
                self, "Close tab", f"Close this tab? Its {len(w.clips)} clip(s) on the timeline will be lost.\n"
                                   "Downloads already queued keep running.") != QMessageBox.Yes:
            return
        w.shutdown()
        # keep the pseudo tab from becoming the current tab (that would open a new one)
        self.tabs.setCurrentIndex(max(0, idx - 1) if idx > 0 else 0) if self.tabs.currentIndex() == idx else None
        self.tabs.removeTab(idx)
        w.deleteLater()
        if not self.workspaces():
            self.new_tab()
        else:
            self._retag()
            if self.tabs.currentWidget() is self._plus:
                self.tabs.setCurrentIndex(self.tabs.count() - 2)

    def _busy_changed(self, ws: Workspace, text: str) -> None:
        i = self.tabs.indexOf(ws)
        if i < 0:
            return
        if text:
            self.tabs.setTabText(i, text)
        else:
            self._title_changed(ws, ws.title if ws.video else "")

    def _title_changed(self, ws: Workspace, title: str) -> None:
        i = self.tabs.indexOf(ws)
        if i < 0:
            return
        if not title:                                    # cleared: back to the numbered empty tab
            self.tabs.setTabText(i, f"Tab {ws.num}")
            self.tabs.setTabToolTip(i, "")
            if ws is self.current():
                self.setWindowTitle(f"{APP_NAME} {__version__}")
            return
        self.tabs.setTabText(i, title if len(title) <= 26 else title[:25] + "...")
        self.tabs.setTabToolTip(i, title)
        if ws is self.current():
            self.setWindowTitle(f"{title} - {APP_NAME}")

    def _status_from(self, ws: Workspace, text: str, ms: int) -> None:
        if ws is self.current() or ms == 0:
            self.statusBar().showMessage(text, ms)

    # ------------------------------------------------------------------ downloads panel
    def _toggle_panel(self, on: bool) -> None:
        self._panel_user_choice = on
        self.panel.setVisible(on)
        self.btn_downloads.setVisible(not on)

    def _update_panel_visibility(self) -> None:
        if self._panel_user_choice is None:
            show = self.width() >= PANEL_AUTO_HIDE_W
        else:
            show = self._panel_user_choice
        self.panel.setVisible(show)
        self.btn_downloads.blockSignals(True)
        self.btn_downloads.setChecked(show)
        self.btn_downloads.blockSignals(False)
        self.btn_downloads.setVisible(not show)

    def _show_queue(self) -> None:
        self.manager.tabs.setCurrentIndex(0)
        self._panel_user_choice = True
        self._update_panel_visibility()

    def resizeEvent(self, e) -> None:  # noqa: N802
        super().resizeEvent(e)
        if self._panel_user_choice is None:
            self._update_panel_visibility()
        self.btn_signins.setText("Sign-ins" if self.width() < 1000 else "Cookies && sign-ins")
        self.btn_settings.setText("" if self.width() < 760 else "Settings")

    def _fit_to_screen(self) -> None:
        """Never be bigger than the screen the window is on (e.g. geometry saved on a larger monitor)."""
        scr = self.screen().availableGeometry() if self.screen() else None
        if not scr:
            return
        w, h = min(self.width(), scr.width()), min(self.height(), scr.height())
        if (w, h) != (self.width(), self.height()):
            self.resize(w, h)
        if not scr.intersects(self.frameGeometry()):
            self.move(scr.topLeft())

    # ------------------------------------------------------------------ drag & drop
    def dragEnterEvent(self, e: QDragEnterEvent) -> None:  # noqa: N802
        if e.mimeData().hasUrls() or e.mimeData().hasText():
            e.acceptProposedAction()

    def dropEvent(self, e: QDropEvent) -> None:  # noqa: N802
        md = e.mimeData()
        text = md.urls()[0].toString() if md.hasUrls() else md.text().strip()
        if not text.lower().startswith(("http://", "https://")):
            return
        ws = self.current()
        if ws is None or ws.video or ws.is_busy() or ws.url_edit.text().strip():
            self.new_tab(text, analyze=True)           # a used tab is never overwritten by a drop
        else:
            ws.url_edit.setText(text)
            ws.analyze("auto")

    # ------------------------------------------------------------------ jobs
    def _retry(self, job_id: str) -> None:
        try:
            self.controller.retry(job_id)
        except Exception as exc:  # noqa: BLE001
            self.statusBar().showMessage(f"Cannot retry: {exc}", 5000)

    def _on_job_changed(self, job: DownloadJob) -> None:
        self.manager.update_job(job)
        for ws in self.workspaces():
            ws.on_job_changed(job)
        jobs = self.controller.queue.jobs
        active = [j for j in jobs if not j.is_finished]
        if active:
            pcts = [j.progress for j in active if j.progress is not None]
            avg = f" - {sum(pcts) / len(pcts):.0f}%" if pcts else ""
            self.queue_label.setText(f"{len(active)} job(s) active{avg}")
            self.queue_bar.show()
            if pcts:
                self.queue_bar.setRange(0, 100)
                self.queue_bar.setValue(int(sum(pcts) / len(pcts)))
            else:
                self.queue_bar.setRange(0, 0)            # busy / unknown length: animated stripe
        else:
            self.queue_label.setText("")
            self.queue_bar.hide()
        if job.kind == JobKind.SOURCE and job.status == JobStatus.COMPLETED:
            limit = self.settings.cache_limit_gb * 1024 ** 3
            used = dir_size(self.settings.cache_dir)
            if used > limit:
                self.statusBar().showMessage(f"Source cache is {human_size(used)} (limit {self.settings.cache_limit_gb:g} GB). "
                                             "Clear it in Settings when you no longer need it.", 15000)

    def _on_clip_completed(self, job: DownloadJob) -> None:
        if job.output_path:
            self.manager.history.add(Path(job.output_path))
        jobs = [j for j in self.controller.queue.jobs if j.kind == JobKind.CLIP]
        if jobs and all(j.is_finished for j in jobs):
            ok = sum(j.status == JobStatus.COMPLETED for j in jobs)
            bad = [j.name for j in jobs if j.status == JobStatus.FAILED]
            if bad:
                self.statusBar().showMessage(f"Finished: {ok} succeeded, {len(bad)} failed ({', '.join(bad[:3])}). "
                                             "Select a failed job and click Retry.", 20000)
            else:
                self.statusBar().showMessage(f"All {ok} clip(s) exported.", 10000)

    # ------------------------------------------------------------------ sign-ins / settings / setup
    def _refresh_signin_chip(self) -> None:
        try:
            n = len(cv.get_vault().entries())
        except OSError:
            n = 0
        self.btn_chip.setText(f"Sign-ins: {n} saved" if n else "Sign-ins: none")

    def open_cookie_manager(self) -> None:
        CookieManagerDialog(self).exec()
        self._refresh_signin_chip()

    def open_settings(self) -> None:
        dlg = SettingsDialog(self.settings, self)
        dlg.clearCacheRequested.connect(self._clear_cache)
        dlg.cookiesRequested.connect(self.open_cookie_manager)
        if dlg.exec() == QDialog.Accepted:
            self.settings = dlg.result_settings()
            save_settings(self.settings)
            yd.set_cookie_spec(self.settings.cookie_spec())
            theme.apply_theme(QApplication.instance(), self.settings.theme)
            self.controller.set_max_concurrent(self.settings.max_concurrent)
            for ws in self.workspaces():
                ws.apply_settings(self.settings)
            self._refresh_icons()
            ic = appicon.load_icon(self.settings.icon_path)
            QApplication.instance().setWindowIcon(ic)
            self.setWindowIcon(ic)
            self.statusBar().showMessage("Settings saved", 4000)
            if dlg.pending_update is not None:                   # "Update" for the app was chosen in the Updates box
                info = dlg.pending_update
                QTimer.singleShot(0, lambda: self._on_settings_update_requested(info))

    def _on_settings_update_requested(self, info) -> None:
        self._on_app_update(info)              # also shows the status-bar button
        self._show_app_update()

    def _refresh_icons(self) -> None:
        self.btn_settings.setIcon(theme.icon("settings", size=16))
        self.btn_downloads.setIcon(theme.icon("chev_left", size=18))
        self.btn_hide_panel.setIcon(theme.icon("chev_right", size=18))
        for ws in self.workspaces():
            ws.refresh_icons()
        self.manager.refresh_icons()

    def _clear_cache(self) -> None:
        busy = any(j.kind == JobKind.SOURCE and not j.is_finished for j in self.controller.queue.jobs)
        if busy:
            QMessageBox.information(self, "Cache in use", "Wait for running downloads to finish first.")
            return
        shutil.rmtree(self.settings.cache_dir, ignore_errors=True)
        for ws in self.workspaces():
            if ws.local_media and not ws.local_media.exists():
                ws.local_media = None

    def open_setup(self) -> None:
        dlg = DependencyDialog(self.settings.ffmpeg_path, self, self.settings.hw_encoder)
        dlg.updateRequested.connect(lambda info: QTimer.singleShot(0, lambda: self._on_settings_update_requested(info)))
        dlg.exec()
        st = deps.check(self.settings.ffmpeg_path)
        if not st.youtube_ok and not self.settings.setup_prompt_dismissed:
            self.settings.setup_prompt_dismissed = True       # do not nag at every start
            save_settings(self.settings)

    def _offer_setup(self, text: str) -> None:
        box = QMessageBox(QMessageBox.Question, "Setup needed", text + "\n\nInstall it now with one click?",
                          QMessageBox.Yes | QMessageBox.No, self)
        if box.exec() == QMessageBox.Yes:
            self.open_setup()

    def _check_latest(self) -> None:
        v = yd.latest_ytdlp_version()
        if v:
            self._vbridge.latest.emit(v)

    @Slot(str)
    def _on_latest_version(self, latest: str) -> None:
        cur = yd.ytdlp_version()
        if yd.version_tuple(latest) > yd.version_tuple(cur):
            self.statusBar().showMessage(f"yt-dlp {latest} is available (you have {cur}). Update it in Settings.", 15000)

    def closeEvent(self, e) -> None:  # noqa: N802
        busy = [j for j in self.controller.queue.jobs if not j.is_finished]
        if busy and not getattr(self, "_quitting_for_update", False) and QMessageBox.question(
                self, "Quit", f"{len(busy)} job(s) are still running or queued. Cancel them and quit?") != QMessageBox.Yes:
            e.ignore()
            return
        for ws in self.workspaces():
            ws.shutdown()
        self.controller.shutdown()
        if self.settings.cleanup_mode == "clean_partials":
            for pattern in ("*.part", "*.ytdl", "*.temp"):
                for f in Path(self.settings.temp_dir).rglob(pattern):
                    f.unlink(missing_ok=True)
        try:
            cv.get_vault().clean_runtime()
        except OSError:
            pass
        self.settings.window_geometry = bytes(self.saveGeometry()).hex()
        ws = self.current()
        if ws:
            self.settings.download_dir = ws.out_dir.text().strip() or self.settings.download_dir
        save_settings(self.settings)
        e.accept()

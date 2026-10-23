"""Download queue panel and export history."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices
from PySide6.QtWidgets import (QAbstractItemView, QHBoxLayout, QHeaderView, QLabel, QMenu, QMessageBox, QProgressBar,
                               QPushButton, QTableWidget, QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget)

from app import theme
from app.models.download_job import DownloadJob, JobKind, JobStatus
from app.utils.paths import app_data_dir, human_size

log = logging.getLogger("uvs.manager")
COLS = ["Name", "Site", "Task", "Progress", "Speed", "ETA", "Status", "Output"]


def open_folder(path: Path) -> None:
    """Open the containing folder (and select the file on Windows)."""
    path = Path(path)
    try:
        if os.name == "nt" and path.is_file():
            subprocess.Popen(["explorer", "/select,", str(path)])  # noqa: S603,S607 - fixed program, list args
        else:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.parent if path.is_file() else path)))
    except OSError as exc:
        log.warning("Could not open folder: %s", exc)


def fmt_speed(v: Optional[float]) -> str:
    return f"{human_size(v)}/s" if v else "-"


def fmt_eta(v: Optional[float]) -> str:
    if v is None:
        return "-"
    v = int(v)
    return f"{v // 60}:{v % 60:02d}" if v < 3600 else f"{v // 3600}:{v % 3600 // 60:02d}:{v % 60:02d}"


class DownloadManager(QWidget):
    startPauseToggled = Signal(bool)      # True = pause queue
    cancelRequested = Signal(str)
    retryRequested = Signal(str)
    removeRequested = Signal(str)
    clearFinished = Signal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.rows: Dict[str, int] = {}
        self.jobs: Dict[str, DownloadJob] = {}
        self.table = QTableWidget(0, len(COLS))
        self.table.setHorizontalHeaderLabels(COLS)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        for i, w in enumerate((150, 90, 160, 105, 80, 50, 95)):
            self.table.setColumnWidth(i, w)

        self.btn_queue = QPushButton("Pause queue")
        self.btn_queue.setCheckable(True)
        self.btn_queue.setToolTip("Stops *new* jobs from starting. Jobs already running continue - "
                                  "pausing a running download or FFmpeg step is not supported; cancel and retry resumes downloads.")
        self.btn_cancel = QPushButton(theme.icon("cancel", size=16), "Cancel")
        self.btn_retry = QPushButton(theme.icon("retry", size=16), "Retry")
        self.btn_open = QPushButton(theme.icon("folder", size=16), "Open folder")
        self.btn_remove = QPushButton(theme.icon("delete", size=16), "Remove")
        self.btn_clear = QPushButton("Clear finished")
        bar = QHBoxLayout()
        for b in (self.btn_queue, self.btn_cancel, self.btn_retry, self.btn_open, self.btn_remove, self.btn_clear):
            bar.addWidget(b)
        bar.addStretch(1)
        self.summary = QLabel("No jobs")
        self.summary.setObjectName("muted")
        bar.addWidget(self.summary)

        queue_tab = QWidget()
        ql = QVBoxLayout(queue_tab)
        ql.setContentsMargins(6, 6, 6, 6)
        ql.addLayout(bar)
        ql.addWidget(self.table, 1)

        self.history = HistoryWidget()
        self.tabs = QTabWidget()
        self.tabs.addTab(queue_tab, "Download queue")
        self.tabs.addTab(self.history, "Export history")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.tabs)

        self.btn_queue.toggled.connect(self._queue_toggled)
        self.btn_cancel.clicked.connect(lambda: self._with_sel(self.cancelRequested))
        self.btn_retry.clicked.connect(lambda: self._with_sel(self.retryRequested))
        self.btn_remove.clicked.connect(lambda: self._with_sel(self.removeRequested))
        self.btn_open.clicked.connect(self._open)
        self.btn_clear.clicked.connect(self.clearFinished)
        self.table.itemSelectionChanged.connect(self._update_buttons)
        self.table.cellDoubleClicked.connect(lambda r, _c: self._details())
        self.table.customContextMenuRequested.connect(self._menu)
        self._update_buttons()

    def set_compact(self, compact: bool) -> None:
        """Icon-only toolbar on narrow windows (tooltips keep the meaning)."""
        labels = {self.btn_cancel: "Cancel", self.btn_retry: "Retry", self.btn_open: "Open folder",
                  self.btn_remove: "Remove"}
        for b, t in labels.items():
            b.setText("" if compact else t)
            b.setToolTip(t)
        self.btn_clear.setText("Clear" if compact else "Clear finished")
        self.btn_queue.setText(("Resume" if self.btn_queue.isChecked() else "Pause") if compact
                               else ("Resume queue" if self.btn_queue.isChecked() else "Pause queue"))
        self.summary.setVisible(not compact)
        for col in (1, 4, 5, 7):                        # Site, Speed, ETA, Output: details are in the tooltip/row dialog
            self.table.setColumnHidden(col, compact)
        if compact:
            for col, w in ((0, 150), (2, 110), (3, 85), (6, 80)):
                self.table.setColumnWidth(col, w)

    def refresh_icons(self) -> None:
        self.btn_cancel.setIcon(theme.icon("cancel", size=16))
        self.btn_retry.setIcon(theme.icon("retry", size=16))
        self.btn_open.setIcon(theme.icon("folder", size=16))
        self.btn_remove.setIcon(theme.icon("delete", size=16))

    def _queue_toggled(self, paused: bool) -> None:
        self.btn_queue.setText(("Resume" if paused else "Pause") if not self.summary.isVisible()
                               else ("Resume queue" if paused else "Pause queue"))
        self.startPauseToggled.emit(paused)

    def selected_job(self) -> Optional[DownloadJob]:
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return None
        return self.jobs.get(self.table.item(rows[0].row(), 0).data(Qt.UserRole))

    def _with_sel(self, sig: Signal) -> None:
        j = self.selected_job()
        if j:
            sig.emit(j.id)

    def _update_buttons(self) -> None:
        j = self.selected_job()
        self.btn_cancel.setEnabled(bool(j and not j.is_finished and not j.cancel_requested))
        self.btn_retry.setEnabled(bool(j and j.can_retry))
        self.btn_remove.setEnabled(bool(j and j.is_finished))
        self.btn_open.setEnabled(bool(j and j.output_path))
        self.btn_clear.setEnabled(any(x.is_finished for x in self.jobs.values()))

    def _open(self) -> None:
        j = self.selected_job()
        if j and j.output_path:
            open_folder(Path(j.output_path))

    def _details(self) -> None:
        j = self.selected_job()
        if j and j.error:
            QMessageBox.information(self, f"Details - {j.name}", j.error)

    def _menu(self, pos) -> None:
        j = self.selected_job()
        if not j:
            return
        m = QMenu(self)
        a_c = m.addAction("Cancel")
        a_c.setEnabled(not j.is_finished and not j.cancel_requested)
        a_r = m.addAction("Retry")
        a_r.setEnabled(j.can_retry)
        a_o = m.addAction("Open output folder")
        a_o.setEnabled(bool(j.output_path))
        a_d = m.addAction("Show error details")
        a_d.setEnabled(bool(j.error))
        m.addSeparator()
        a_x = m.addAction("Remove from list")
        a_x.setEnabled(j.is_finished)
        ch = m.exec(self.table.viewport().mapToGlobal(pos))
        if ch is a_c:
            self.cancelRequested.emit(j.id)
        elif ch is a_r:
            self.retryRequested.emit(j.id)
        elif ch is a_o:
            self._open()
        elif ch is a_d:
            self._details()
        elif ch is a_x:
            self.removeRequested.emit(j.id)

    # --- model updates -------------------------------------------------------
    def add_job(self, job: DownloadJob) -> None:
        r = self.table.rowCount()
        self.table.insertRow(r)
        self.rows[job.id] = r
        self.jobs[job.id] = job
        it = QTableWidgetItem(job.name)
        it.setData(Qt.UserRole, job.id)
        self.table.setItem(r, 0, it)
        bar = QProgressBar()
        bar.setRange(0, 100)
        self.table.setCellWidget(r, 3, bar)
        self.update_job(job)

    def update_job(self, job: DownloadJob) -> None:
        r = self.rows.get(job.id)
        if r is None:
            return
        self.jobs[job.id] = job
        vals = {0: job.name, 1: job.site, 2: job.task, 4: fmt_speed(job.speed), 5: fmt_eta(job.eta),
                6: job.status.value, 7: str(job.output_path or "")}
        for c, v in vals.items():
            it = self.table.item(r, c)
            new = it is None
            if new:
                it = QTableWidgetItem()
            it.setText(v)
            if c == 0:
                it.setData(Qt.UserRole, job.id)
            if c == 6:
                col = {JobStatus.COMPLETED: "ok", JobStatus.FAILED: "danger"}.get(job.status)
                it.setForeground(QColor(theme.palette()[col]) if col else QColor(theme.palette()["text"]))
            if c == 7 and v:
                it.setToolTip(v)
            if c == 6 and job.error:
                it.setToolTip(job.error)
            if new:
                self.table.setItem(r, c, it)
        bar: QProgressBar = self.table.cellWidget(r, 3)
        if job.progress is None:
            bar.setValue(0)
            bar.setFormat("n/a" if job.is_active else "-")
        else:
            bar.setValue(int(job.progress))
            bar.setFormat("%p%")
        self.summary.setText(", ".join(f"{n} {k.lower()}" for k, n in self._counts().items()) or "No jobs")
        self._update_buttons()

    def remove_job(self, job_id: str) -> None:
        r = self.rows.pop(job_id, None)
        self.jobs.pop(job_id, None)
        if r is None:
            return
        self.table.removeRow(r)
        self.rows = {self.table.item(i, 0).data(Qt.UserRole): i for i in range(self.table.rowCount())}
        self.summary.setText(", ".join(f"{n} {k.lower()}" for k, n in self._counts().items()) or "No jobs")
        self._update_buttons()

    def _counts(self) -> Dict[str, int]:
        out: Dict[str, int] = {}
        for j in self.jobs.values():
            out[j.status.value] = out.get(j.status.value, 0) + 1
        return out


class HistoryWidget(QWidget):
    """Persistent list of completed exports (history.json in the app data folder)."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.path = app_data_dir() / "history.json"
        self.entries: List[dict] = []
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["When", "File", "Size", "Location"])
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.btn_open = QPushButton(theme.icon("folder", size=16), "Show in folder")
        self.btn_play = QPushButton("Open file")
        self.btn_remove = QPushButton("Remove entry")
        self.btn_remove.setToolTip("Removes the entry from this list only; the file is not deleted.")
        bar = QHBoxLayout()
        for b in (self.btn_open, self.btn_play, self.btn_remove):
            bar.addWidget(b)
        bar.addStretch(1)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 6)
        lay.addLayout(bar)
        lay.addWidget(self.table, 1)
        self.btn_open.clicked.connect(lambda: self._act("folder"))
        self.btn_play.clicked.connect(lambda: self._act("file"))
        self.btn_remove.clicked.connect(self._remove)
        self.table.cellDoubleClicked.connect(lambda *_: self._act("file"))
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = []
        # tolerate history files from older versions or hand edits: keep only usable entries, fill missing fields
        self.entries = [{"time": str(e.get("time", "")), "path": str(e["path"]), "size": e.get("size")}
                        for e in (data if isinstance(data, list) else [])
                        if isinstance(e, dict) and e.get("path")]
        self._render()

    def _save(self) -> None:
        try:
            self.path.write_text(json.dumps(self.entries[-500:], indent=1), encoding="utf-8")
        except OSError as exc:
            log.warning("Could not save history: %s", exc)

    def add(self, path: Path) -> None:
        try:
            size = Path(path).stat().st_size
        except OSError:
            size = None
        self.entries.append({"time": time.strftime("%Y-%m-%d %H:%M"), "path": str(path), "size": size})
        self._save()
        self._render()

    def _render(self) -> None:
        self.table.setRowCount(len(self.entries))
        for r, e in enumerate(reversed(self.entries)):
            p = Path(e["path"])
            for c, v in enumerate((e["time"], p.name, human_size(e.get("size")), str(p.parent))):
                self.table.setItem(r, c, QTableWidgetItem(v))

    def _sel_entry(self) -> Optional[dict]:
        rows = self.table.selectionModel().selectedRows()
        return list(reversed(self.entries))[rows[0].row()] if rows else None

    def _act(self, what: str) -> None:
        e = self._sel_entry()
        if not e:
            return
        p = Path(e["path"])
        if not p.exists():
            QMessageBox.information(self, "File not found", "This file has been moved or deleted.")
            return
        if what == "folder":
            open_folder(p)
        else:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(p)))

    def _remove(self) -> None:
        e = self._sel_entry()
        if e:
            self.entries.remove(e)
            self._save()
            self._render()

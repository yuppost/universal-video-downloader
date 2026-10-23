"""Queue controller: starts jobs on a thread pool, applies progress, handles retry/cancel.

All state changes happen on the GUI thread (worker threads only emit signals), so the
:class:`JobQueue` needs no locking against the UI and workers can never touch widgets.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable, List, Optional

from PySide6.QtCore import QObject, QThreadPool, Signal, Slot

from app.models.download_job import DownloadJob, JobKind, JobQueue, JobStatus
from app.workers.download_worker import SourceDownloadTask
from app.workers.export_worker import ClipExportTask
from app.workers.task_signals import TaskSignals

log = logging.getLogger("uvs.controller")


class JobController(QObject):
    job_added = Signal(object)
    job_changed = Signal(object)
    job_removed = Signal(str)
    clip_completed = Signal(object)     # DownloadJob (CLIP kind) that completed
    queue_idle = Signal()

    def __init__(self, max_concurrent: int = 2, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self.queue = JobQueue(max_concurrent)
        self.signals = TaskSignals(self)
        self.signals.progress.connect(self._on_progress)
        self.signals.finished.connect(self._on_finished)
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(max(2, max_concurrent + 1))
        self._shutting_down = False

    # --- configuration ---------------------------------------------------
    def set_max_concurrent(self, n: int) -> None:
        self.queue.max_concurrent = max(1, n)
        self.pool.setMaxThreadCount(max(2, n + 1))
        self._pump()

    def set_paused(self, paused: bool) -> None:
        self.queue.paused = paused
        if not paused:
            self._pump()
        for j in self.queue.jobs:
            if j.status == JobStatus.WAITING:
                j.task = "Queue paused" if paused else "Waiting in queue"
                self.job_changed.emit(j)

    # --- adding / controlling --------------------------------------------
    def add(self, job: DownloadJob) -> DownloadJob:
        self.queue.add(job)
        self.job_added.emit(job)
        self._pump()
        return job

    def cancel(self, job_id: str) -> None:
        j = self.queue.cancel(job_id)
        self.job_changed.emit(j)
        self._resolve_blocked()
        self._maybe_idle()

    def cancel_all(self) -> None:
        for j in list(self.queue.jobs):
            if not j.is_finished:
                self.cancel(j.id)

    def retry(self, job_id: str) -> None:
        """Retry a failed/canceled job. A clip whose source is gone re-queues the source too."""
        j = self.queue.get(job_id)
        if j.kind == JobKind.CLIP and j.depends_on:
            try:
                dep = self.queue.get(j.depends_on)
            except KeyError:
                dep = None
            if dep is not None:
                missing = dep.status == JobStatus.COMPLETED and not (dep.output_path and Path(dep.output_path).is_file())
                if dep.can_retry or missing:
                    if missing:
                        dep.status = JobStatus.FAILED  # allow a legal retry transition
                    self.queue.retry(dep.id)
                    self.job_changed.emit(dep)
        self.queue.retry(job_id)
        j.attempts += 1
        self.job_changed.emit(j)
        self._pump()

    def remove(self, job_id: str) -> None:
        self.queue.remove(job_id)
        self.job_removed.emit(job_id)

    def clear_finished(self) -> None:
        ids = [j.id for j in self.queue.jobs if j.is_finished]
        self.queue.clear_finished()
        for i in ids:
            self.job_removed.emit(i)

    def source_job_for(self, key: str) -> Optional[DownloadJob]:
        """An existing, not-failed source job for the same cache key (avoids duplicate downloads)."""
        for j in self.queue.jobs:
            if j.kind == JobKind.SOURCE and getattr(j.payload, "key", None) == key \
                    and j.status not in (JobStatus.FAILED, JobStatus.CANCELED):
                return j
        return None

    def shutdown(self, timeout_ms: int = 8000) -> None:
        """Cancel everything and wait for workers so none outlive the UI."""
        self._shutting_down = True
        for j in self.queue.jobs:
            if not j.is_finished:
                j.cancel_event.set()
        self.pool.waitForDone(timeout_ms)

    # --- internals -------------------------------------------------------
    def _pump(self) -> None:
        if self._shutting_down:
            return
        self._resolve_blocked()
        while (job := self.queue.next_runnable()) is not None:
            self._start(job)
        self._maybe_idle()

    def _resolve_blocked(self) -> None:
        for j in self.queue.blocked_jobs():
            j.transition(JobStatus.FAILED)
            j.error = "The source download did not complete."
            j.task = "Source unavailable"
            self.job_changed.emit(j)

    def _start(self, job: DownloadJob) -> None:
        job.cancel_event.clear()
        if job.kind == JobKind.SOURCE:
            job.transition(JobStatus.DOWNLOADING)
            job.task = "Starting download"
            self.job_changed.emit(job)
            self.pool.start(SourceDownloadTask(job, self.signals))
        else:
            src: Optional[Path] = None
            if job.depends_on:
                dep = self.queue.get(job.depends_on)
                src = dep.output_path
            job.transition(JobStatus.TRIMMING)
            job.task = "Starting"
            self.job_changed.emit(job)
            self.pool.start(ClipExportTask(job, src, self.signals))

    @Slot(str, str, str, object, object, object)
    def _on_progress(self, job_id: str, status: str, task: str, pct, speed, eta) -> None:
        try:
            job = self.queue.get(job_id)
        except KeyError:
            return
        if not job.is_active:
            return
        try:
            job.transition(JobStatus(status))
        except Exception:  # noqa: BLE001 - ignore illegal late updates
            return
        if not job.cancel_requested:
            job.task = task
        job.progress, job.speed, job.eta = pct, speed, eta
        self.job_changed.emit(job)

    @Slot(str, str, str, str, str)
    def _on_finished(self, job_id: str, outcome: str, path: str, message: str, details: str) -> None:
        try:
            job = self.queue.get(job_id)
        except KeyError:
            return
        if job.status in (JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELED):
            return
        if outcome == "completed" and not job.cancel_requested:
            job.output_path = Path(path)
            job.transition(JobStatus.COMPLETED)
            job.task = "Done"
            job.error = None
        elif outcome == "canceled" or job.cancel_requested:
            job.transition(JobStatus.CANCELED)
            job.task = "Canceled"
            if outcome == "completed" and path:
                job.output_path = Path(path)  # finished just before cancel took effect; keep file
        else:
            job.transition(JobStatus.FAILED)
            job.task = "Failed"
            job.error = message + (f"\n\nDetails:\n{details}" if details else "")
            log.error("Job %s failed: %s", job.name, message)
        self.job_changed.emit(job)
        if job.kind == JobKind.CLIP and job.status == JobStatus.COMPLETED:
            self.clip_completed.emit(job)
        self._pump()

    def _maybe_idle(self) -> None:
        if not any(not j.is_finished for j in self.queue.jobs):
            self.queue_idle.emit()

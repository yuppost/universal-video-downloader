"""Download/export job model and a thread-safe queue with a strict state machine."""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional


class JobStatus(str, Enum):
    WAITING = "Waiting"
    ANALYZING = "Analyzing"
    DOWNLOADING = "Downloading"
    TRIMMING = "Trimming"
    CROPPING = "Cropping"
    CONVERTING = "Converting"
    COMPLETED = "Completed"
    FAILED = "Failed"
    CANCELED = "Canceled"


ACTIVE = {JobStatus.ANALYZING, JobStatus.DOWNLOADING, JobStatus.TRIMMING,
          JobStatus.CROPPING, JobStatus.CONVERTING}
FINISHED = {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELED}


class JobKind(str, Enum):
    SOURCE = "source"   # download of the source media (shared by many clips)
    CLIP = "clip"       # one exported clip


class InvalidTransition(RuntimeError):
    """Raised when a job is moved to a status that is not allowed from its current one."""


_ALLOWED: Dict[JobStatus, set] = {
    JobStatus.WAITING: ACTIVE | {JobStatus.CANCELED, JobStatus.FAILED},
    **{s: (ACTIVE - {s}) | {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELED} for s in ACTIVE},
    JobStatus.COMPLETED: set(),                               # terminal
    JobStatus.FAILED: {JobStatus.WAITING},                    # retry
    JobStatus.CANCELED: {JobStatus.WAITING},                  # retry
}


@dataclass
class DownloadJob:
    """One unit of work shown in the download manager."""

    kind: JobKind
    name: str
    site: str = ""
    payload: Any = None                      # SourceSpec / ClipSpec (see export_service)
    depends_on: Optional[str] = None         # job id that must complete first
    clip_id: Optional[str] = None
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:10])
    status: JobStatus = JobStatus.WAITING
    task: str = "Waiting in queue"
    progress: Optional[float] = None         # 0..100, None = not measurable
    speed: Optional[float] = None            # bytes/s, None = unknown
    eta: Optional[float] = None              # seconds, None = unknown
    output_path: Optional[Path] = None
    error: Optional[str] = None
    attempts: int = 0
    created: float = field(default_factory=time.time)
    cancel_event: threading.Event = field(default_factory=threading.Event, repr=False)
    cancel_requested: bool = False

    @property
    def is_active(self) -> bool:
        return self.status in ACTIVE

    @property
    def is_finished(self) -> bool:
        return self.status in FINISHED

    @property
    def can_retry(self) -> bool:
        return self.status in (JobStatus.FAILED, JobStatus.CANCELED)

    def transition(self, new: JobStatus) -> None:
        if new == self.status:
            return
        if new not in _ALLOWED[self.status]:
            raise InvalidTransition(f"{self.status.value} -> {new.value} is not allowed")
        self.status = new
        if new in (JobStatus.COMPLETED,):
            self.progress, self.speed, self.eta = 100.0, None, None
        elif new in (JobStatus.FAILED, JobStatus.CANCELED):
            self.speed = self.eta = None
        elif new == JobStatus.WAITING:
            self.progress = self.speed = self.eta = None
            self.error = None


class JobQueue:
    """Ordered job list that decides which jobs may start next.

    Rules: at most ``max_concurrent`` active jobs; a job with ``depends_on`` only
    starts after that job COMPLETED; if the dependency FAILED/CANCELED the
    dependant is reported by :meth:`blocked_jobs`. ``paused`` stops new starts
    (running jobs continue).
    """

    def __init__(self, max_concurrent: int = 2) -> None:
        self._lock = threading.RLock()
        self.jobs: List[DownloadJob] = []
        self.max_concurrent = max(1, max_concurrent)
        self.paused = False

    def add(self, job: DownloadJob) -> DownloadJob:
        with self._lock:
            self.jobs.append(job)
        return job

    def get(self, job_id: str) -> DownloadJob:
        with self._lock:
            for j in self.jobs:
                if j.id == job_id:
                    return j
        raise KeyError(job_id)

    def running(self) -> List[DownloadJob]:
        with self._lock:
            return [j for j in self.jobs if j.is_active]

    def next_runnable(self) -> Optional[DownloadJob]:
        """Oldest WAITING job whose dependency is satisfied, if capacity allows."""
        with self._lock:
            if self.paused or len(self.running()) >= self.max_concurrent:
                return None
            for j in self.jobs:
                if j.status != JobStatus.WAITING:
                    continue
                if j.depends_on is None:
                    return j
                try:
                    dep = self.get(j.depends_on)
                except KeyError:
                    return j
                if dep.status == JobStatus.COMPLETED:
                    return j
            return None

    def blocked_jobs(self) -> List[DownloadJob]:
        """WAITING jobs whose dependency FAILED or was CANCELED (can never start)."""
        out = []
        with self._lock:
            for j in self.jobs:
                if j.status == JobStatus.WAITING and j.depends_on:
                    try:
                        dep = self.get(j.depends_on)
                    except KeyError:
                        continue
                    if dep.status in (JobStatus.FAILED, JobStatus.CANCELED):
                        out.append(j)
        return out

    def cancel(self, job_id: str) -> DownloadJob:
        """Cancel a queued job immediately, or flag a running one for safe cancellation."""
        with self._lock:
            j = self.get(job_id)
            if j.status == JobStatus.WAITING:
                j.transition(JobStatus.CANCELED)
                j.task = "Canceled before start"
            elif j.is_active:
                j.cancel_requested = True
                j.cancel_event.set()
                j.task = "Canceling..."
            return j

    def retry(self, job_id: str) -> DownloadJob:
        with self._lock:
            j = self.get(job_id)
            if not j.can_retry:
                raise InvalidTransition("Only failed or canceled jobs can be retried")
            j.transition(JobStatus.WAITING)
            j.cancel_event = threading.Event()
            j.cancel_requested = False
            j.task = "Waiting in queue"
            return j

    def remove(self, job_id: str) -> None:
        with self._lock:
            j = self.get(job_id)
            if not j.is_finished:
                raise InvalidTransition("Only finished jobs can be removed")
            self.jobs.remove(j)

    def clear_finished(self) -> int:
        with self._lock:
            keep = [j for j in self.jobs if not j.is_finished]
            n = len(self.jobs) - len(keep)
            self.jobs = keep
            return n

    def summary(self) -> Dict[str, int]:
        with self._lock:
            out: Dict[str, int] = {}
            for j in self.jobs:
                out[j.status.value] = out.get(j.status.value, 0) + 1
            return out

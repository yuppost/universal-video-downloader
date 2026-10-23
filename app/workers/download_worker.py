"""Pool task that downloads a shared source file (or a preview file)."""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import QRunnable

from app.models.download_job import DownloadJob, JobStatus
from app.services.errors import FriendlyError, OperationCanceled
from app.services.export_service import SourceSpec, execute_source_download
from app.workers.task_signals import TaskSignals

log = logging.getLogger("uvs.download")


class SourceDownloadTask(QRunnable):
    def __init__(self, job: DownloadJob, signals: TaskSignals) -> None:
        super().__init__()
        self.setAutoDelete(True)
        self.job, self.signals = job, signals

    def run(self) -> None:  # runs in a pool thread
        spec: SourceSpec = self.job.payload
        jid = self.job.id

        def report(status: JobStatus, task: str, pct, speed, eta) -> None:
            self.signals.progress.emit(jid, status.value, task, pct, speed, eta)

        try:
            path = execute_source_download(spec, report, self.job.cancel_event)
            self.signals.finished.emit(jid, "completed", str(path), "", "")
        except OperationCanceled:
            self.signals.finished.emit(jid, "canceled", "", "", "")
        except FriendlyError as exc:
            self.signals.finished.emit(jid, "failed", "", exc.message, exc.details)
        except Exception as exc:  # noqa: BLE001
            log.exception("Source download crashed")
            self.signals.finished.emit(jid, "failed", "", "Unexpected error during download.", str(exc))


class PreviewDownloadTask(QRunnable):
    """Downloads a small low-resolution file used only for preview playback."""

    def __init__(self, url: str, selector: str, cache_dir: Path, key: str, ffmpeg_path: str,
                 cookies_browser: str, signals: TaskSignals, cancel_event) -> None:
        super().__init__()
        self.setAutoDelete(True)
        self.args = (url, selector, cache_dir, key, ffmpeg_path, cookies_browser)
        self.signals, self.cancel_event = signals, cancel_event

    def run(self) -> None:
        from app.services import ytdlp_service as yd

        url, selector, cache_dir, key, ffmpeg_path, cookies = self.args

        def report(status, task, pct, speed, eta) -> None:
            self.signals.preview_progress.emit(pct if pct is not None else -1.0)

        try:
            path = yd.download_source(url, selector, cache_dir, key, ffmpeg_path, cookies, report, self.cancel_event)
            self.signals.preview_ready.emit(str(path), "")
        except OperationCanceled:
            self.signals.preview_ready.emit("", "canceled")
        except FriendlyError as exc:
            self.signals.preview_ready.emit("", exc.message)
        except Exception as exc:  # noqa: BLE001
            log.exception("Preview download crashed")
            self.signals.preview_ready.emit("", str(exc))

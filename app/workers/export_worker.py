"""Pool task that exports one clip, plus a thumbnail-strip task."""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QRunnable

from app.models.download_job import DownloadJob, JobStatus
from app.services.errors import FriendlyError, OperationCanceled
from app.services.export_service import ClipSpec, execute_clip_export
from app.services.thumbnail_service import generate_strip
from app.workers.task_signals import TaskSignals

log = logging.getLogger("uvs.exportworker")


class ClipExportTask(QRunnable):
    def __init__(self, job: DownloadJob, source_path: Optional[Path], signals: TaskSignals) -> None:
        super().__init__()
        self.setAutoDelete(True)
        self.job, self.source_path, self.signals = job, source_path, signals

    def run(self) -> None:
        spec: ClipSpec = self.job.payload
        jid = self.job.id

        def report(status: JobStatus, task: str, pct, speed, eta) -> None:
            self.signals.progress.emit(jid, status.value, task, pct, speed, eta)

        try:
            path = execute_clip_export(spec, self.source_path, report, self.job.cancel_event)
            self.signals.finished.emit(jid, "completed", str(path), "", "")
        except OperationCanceled:
            self.signals.finished.emit(jid, "canceled", "", "", "")
        except FriendlyError as exc:
            self.signals.finished.emit(jid, "failed", "", exc.message, exc.details)
        except Exception as exc:  # noqa: BLE001
            log.exception("Clip export crashed")
            self.signals.finished.emit(jid, "failed", "", "Unexpected error while exporting.", str(exc))


class ThumbnailTask(QRunnable):
    def __init__(self, ffmpeg: str, source: Path, duration: float, out_dir: Path,
                 signals: TaskSignals, cancel: threading.Event) -> None:
        super().__init__()
        self.setAutoDelete(True)
        self.a = (ffmpeg, source, duration, out_dir)
        self.signals, self.cancel = signals, cancel

    def run(self) -> None:
        ffmpeg, source, duration, out_dir = self.a
        try:
            generate_strip(ffmpeg, source, duration, out_dir, cancel=self.cancel,
                           on_thumb=lambda i, t, p: self.signals.thumbnail.emit(i, t, str(p)))
        except OperationCanceled:
            return
        except Exception:  # noqa: BLE001
            log.exception("Thumbnail generation failed")
        finally:
            self.signals.thumbnails_done.emit()

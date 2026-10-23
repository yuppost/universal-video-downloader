"""Background URL analysis (runs on its own QThread; never touches widgets)."""

from __future__ import annotations

import logging
import threading

from PySide6.QtCore import QObject, Signal, Slot

from app.services import ytdlp_service as yd
from app.services.errors import FriendlyError, OperationCanceled
from app.utils.validation import UrlValidationError

log = logging.getLogger("uvs.analyze")


class AnalyzeWorker(QObject):
    """Analyse a URL. Emits exactly one of finished / playlist / failed / canceled."""

    finished = Signal(object)            # VideoInfo
    playlist = Signal(object)            # PlaylistInfo
    failed = Signal(str, str, str)       # message, details, hint (signin|cookies|js|update|'')
    canceled = Signal()
    done = Signal()                      # always emitted last (thread can quit)

    def __init__(self, url: str, mode: str, cookies_browser: str) -> None:
        super().__init__()
        self.url, self.mode, self.cookies_browser = url, mode, cookies_browser
        self.cancel_event = threading.Event()

    def cancel(self) -> None:
        self.cancel_event.set()

    @Slot()
    def run(self) -> None:
        try:
            result = yd.analyze_url(self.url, self.mode, self.cookies_browser, self.cancel_event)
            if self.cancel_event.is_set():
                raise OperationCanceled()
            if isinstance(result, yd.PlaylistInfo):
                self.playlist.emit(result)
            else:
                self.finished.emit(result)
        except OperationCanceled:
            self.canceled.emit()
        except UrlValidationError as exc:
            self.failed.emit(str(exc), "", "")
        except FriendlyError as exc:
            self.failed.emit(exc.message, exc.details, exc.hint)
        except Exception as exc:  # noqa: BLE001 - last-resort guard so the thread never dies silently
            log.exception("Unexpected analysis error")
            self.failed.emit("Unexpected error while analysing the link.", str(exc), "")
        finally:
            self.done.emit()

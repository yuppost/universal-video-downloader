"""Signals shared by pool tasks. Owned by the controller (lives in the GUI thread)."""

from __future__ import annotations

from PySide6.QtCore import QObject, Signal


class TaskSignals(QObject):
    # job id, status name, task text, percent|None, speed|None, eta|None
    progress = Signal(str, str, str, object, object, object)
    # job id, outcome ("completed"|"failed"|"canceled"), output path, message, details
    finished = Signal(str, str, str, str, str)
    # preview file download
    preview_progress = Signal(float)
    preview_ready = Signal(str, str)   # path, error ("" if ok)
    thumbnail = Signal(int, float, str)
    thumbnails_done = Signal()
    waveform = Signal(str, object)      # media path, list of 0..1 peaks (empty = no audio / failed)

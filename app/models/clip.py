"""Clip data model: validated start/end ranges plus a change-notifying collection."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Callable, List, Optional

from app.models.crop import CropSettings

MIN_CLIP_DURATION = 0.5  # seconds
CLIP_COLORS = ["#3aa6ff", "#9b6cff", "#22d3a5", "#ffb020", "#ff6b8b", "#2fd6e6", "#8bd94f", "#ff8a4c"]


class ClipError(ValueError):
    """Raised for invalid clip operations (bad range, unknown id...)."""


@dataclass
class OutputSettings:
    """Per-clip output options."""

    container: str = "mp4"        # "mp4" or "mp3"
    trim_mode: str = "fast"       # "fast" (stream copy where possible) or "accurate"
    max_height: Optional[int] = None  # None = best available
    video_codec: str = "h264"     # "h264" or "h265" (used when re-encoding)

    def copy(self) -> "OutputSettings":
        return OutputSettings(self.container, self.trim_mode, self.max_height, self.video_codec)

    def validate(self) -> None:
        if self.container not in ("mp4", "mp3"):
            raise ClipError(f"Unsupported container: {self.container}")
        if self.trim_mode not in ("fast", "accurate"):
            raise ClipError(f"Unsupported trim mode: {self.trim_mode}")
        if self.video_codec not in ("h264", "h265"):
            raise ClipError(f"Unsupported video codec: {self.video_codec}")
        if self.max_height is not None and self.max_height <= 0:
            raise ClipError("Resolution must be positive")


@dataclass
class Clip:
    """One selected section of the source video."""

    source_id: str
    start: float
    end: float
    name: str = "Clip"
    filename: str = ""            # optional per-clip file name stem (empty -> template)
    crop: CropSettings = field(default_factory=CropSettings)
    output: OutputSettings = field(default_factory=OutputSettings)
    color_index: int = 0
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:10])

    @property
    def duration(self) -> float:
        return self.end - self.start

    @property
    def color(self) -> str:
        return CLIP_COLORS[self.color_index % len(CLIP_COLORS)]

    def validate(self, source_duration: Optional[float] = None) -> None:
        validate_range(self.start, self.end, source_duration)
        self.output.validate()

    def duplicate(self) -> "Clip":
        return Clip(self.source_id, self.start, self.end, f"{self.name} copy", self.filename and f"{self.filename}_copy",
                    self.crop.copy(), self.output.copy(), self.color_index + 1)


def validate_range(start: float, end: float, source_duration: Optional[float] = None,
                   min_duration: float = MIN_CLIP_DURATION) -> None:
    """Raise ClipError unless 0 <= start < end <= duration and length >= minimum."""
    if start != start or end != end:
        raise ClipError("Start and end must be numbers")
    if start < 0:
        raise ClipError("Start cannot be negative")
    if end <= start:
        raise ClipError("End must be later than start")
    if end - start < min_duration - 1e-9:
        raise ClipError(f"A clip must be at least {min_duration:g} seconds long")
    if source_duration is not None and end > source_duration + 1e-6:
        raise ClipError("End is beyond the end of the video")


class ClipCollection:
    """Ordered list of clips for one source video, with change notification.

    The order of ``clips`` is the export order. Mutating methods validate their
    input and notify subscribers *after* the change.
    """

    def __init__(self, source_id: str = "", duration: float = 0.0) -> None:
        self.source_id = source_id
        self.duration = duration
        self.clips: List[Clip] = []
        self._subs: List[Callable[[], None]] = []
        self._color_counter = 0

    # --- observers -------------------------------------------------------
    def subscribe(self, cb: Callable[[], None]) -> None:
        self._subs.append(cb)

    def _notify(self) -> None:
        for cb in list(self._subs):
            cb()

    # --- basic access ----------------------------------------------------
    def __len__(self) -> int:
        return len(self.clips)

    def get(self, clip_id: str) -> Clip:
        for c in self.clips:
            if c.id == clip_id:
                return c
        raise ClipError(f"Unknown clip: {clip_id}")

    def index_of(self, clip_id: str) -> int:
        for i, c in enumerate(self.clips):
            if c.id == clip_id:
                return i
        raise ClipError(f"Unknown clip: {clip_id}")

    def reset_source(self, source_id: str, duration: float) -> None:
        self.source_id, self.duration = source_id, duration
        self.clips.clear()
        self._color_counter = 0
        self._notify()

    # --- mutations -------------------------------------------------------
    def _next_name(self) -> str:
        used = {c.name for c in self.clips}
        n = len(self.clips) + 1
        while f"Clip {n:02d}" in used:
            n += 1
        return f"Clip {n:02d}"

    def add(self, start: float, end: float, name: Optional[str] = None,
            crop: Optional[CropSettings] = None, output: Optional[OutputSettings] = None) -> Clip:
        validate_range(start, end, self.duration or None)
        clip = Clip(self.source_id, start, end, name or self._next_name(),
                    crop=crop.copy() if crop else CropSettings(),
                    output=output.copy() if output else OutputSettings(),
                    color_index=self._color_counter)
        self._color_counter += 1
        self.clips.append(clip)
        self._notify()
        return clip

    def remove(self, clip_id: str) -> None:
        self.clips.pop(self.index_of(clip_id))
        self._notify()

    def duplicate(self, clip_id: str) -> Clip:
        src = self.get(clip_id)
        dup = src.duplicate()
        dup.color_index = self._color_counter
        self._color_counter += 1
        self.clips.insert(self.index_of(clip_id) + 1, dup)
        self._notify()
        return dup

    def rename(self, clip_id: str, name: str) -> None:
        name = name.strip()
        if not name:
            raise ClipError("Name cannot be empty")
        self.get(clip_id).name = name
        self._notify()

    def set_filename(self, clip_id: str, stem: str) -> None:
        self.get(clip_id).filename = stem.strip()
        self._notify()

    def set_range(self, clip_id: str, start: float, end: float) -> None:
        """Set both edges (validated against the source duration)."""
        validate_range(start, end, self.duration or None)
        c = self.get(clip_id)
        c.start, c.end = start, end
        self._notify()

    def set_start(self, clip_id: str, start: float) -> None:
        c = self.get(clip_id)
        start = max(0.0, min(start, c.end - MIN_CLIP_DURATION))
        self.set_range(clip_id, start, c.end)

    def set_end(self, clip_id: str, end: float) -> None:
        c = self.get(clip_id)
        hi = self.duration if self.duration else end
        end = min(hi, max(end, c.start + MIN_CLIP_DURATION))
        self.set_range(clip_id, c.start, end)

    def move_to(self, clip_id: str, new_start: float) -> None:
        """Move the whole clip, keeping its length and staying inside the video."""
        c = self.get(clip_id)
        length = c.duration
        hi = (self.duration - length) if self.duration else new_start
        new_start = max(0.0, min(new_start, hi))
        self.set_range(clip_id, new_start, new_start + length)

    def reset(self, clip_id: str, default_length: float = 5.0) -> None:
        """Reset a clip to a default range at the start of the video (keeps name/crop)."""
        end = min(default_length, self.duration) if self.duration else default_length
        self.set_range(clip_id, 0.0, max(end, MIN_CLIP_DURATION))

    def reorder(self, from_index: int, to_index: int) -> None:
        if not (0 <= from_index < len(self.clips) and 0 <= to_index < len(self.clips)):
            raise ClipError("Index out of range")
        self.clips.insert(to_index, self.clips.pop(from_index))
        self._notify()

    def set_crop(self, clip_id: str, crop: CropSettings) -> None:
        self.get(clip_id).crop = crop.copy()
        self._notify()

    def set_output(self, clip_id: str, output: OutputSettings) -> None:
        output.validate()
        self.get(clip_id).output = output.copy()
        self._notify()

    def apply_output_to_all(self, output: OutputSettings) -> None:
        output.validate()
        for c in self.clips:
            c.output = output.copy()
        self._notify()

    def apply_crop_to_all(self, crop: CropSettings) -> None:
        for c in self.clips:
            c.crop = crop.copy()
        self._notify()

    def find_gap(self, length: float = 5.0, near: float = 0.0) -> tuple[float, float]:
        """Choose a (start, end) for a new clip: at *near* if free, else the first free gap."""
        length = max(MIN_CLIP_DURATION, length)
        dur = self.duration or (near + length)
        length = min(length, dur)
        start = max(0.0, min(near, dur - length))
        busy = sorted((c.start, c.end) for c in self.clips)
        pos = start
        for s, e in busy:
            if pos + length <= s:
                break
            if e > pos:
                pos = e
        if pos + length > dur:
            pos = max(0.0, dur - length)  # overlapping is allowed when no gap exists
        return (pos, pos + length)

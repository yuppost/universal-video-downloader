"""Crop settings and pure crop geometry (no Qt, fully unit-testable).

A crop is stored as a *normalised* rectangle (fractions of the source frame) so
it is independent of the preview resolution. ``rect=None`` means "center crop".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Tuple

# preset key -> (label, ratio w/h or None)
ASPECT_PRESETS = {
    "original": ("Original (no crop)", None),
    "16:9": ("16:9 landscape", (16, 9)),
    "9:16": ("9:16 vertical (Shorts / Reels)", (9, 16)),
    "1:1": ("1:1 square", (1, 1)),
    "custom": ("Custom ratio", None),
    "free": ("Free-form rectangle", None),
}
MIN_CROP_FRACTION = 0.05

NormRect = Tuple[float, float, float, float]  # x, y, w, h in 0..1


@dataclass
class CropSettings:
    """Per-clip crop description."""

    preset: str = "original"
    rect: Optional[NormRect] = None
    custom_ratio: Tuple[float, float] = (4.0, 3.0)

    def is_active(self) -> bool:
        return self.preset != "original" or self.rect is not None and self.preset == "free"

    def copy(self) -> "CropSettings":
        return CropSettings(self.preset, self.rect, self.custom_ratio)

    def to_dict(self) -> dict:
        return {"preset": self.preset, "rect": list(self.rect) if self.rect else None,
                "custom_ratio": list(self.custom_ratio)}

    @staticmethod
    def from_dict(d: dict) -> "CropSettings":
        rect = d.get("rect")
        cr = d.get("custom_ratio") or (4.0, 3.0)
        preset = d.get("preset", "original")
        if preset not in ASPECT_PRESETS:
            preset = "original"
        return CropSettings(preset, tuple(rect) if rect else None, (float(cr[0]), float(cr[1])))


@dataclass(frozen=True)
class CropRect:
    """Pixel crop rectangle, always even-sized and even-aligned (yuv420p safe)."""

    x: int
    y: int
    w: int
    h: int

    def ffmpeg_filter(self) -> str:
        return f"crop={self.w}:{self.h}:{self.x}:{self.y}"


def target_ratio(settings: CropSettings, src_w: int, src_h: int) -> Optional[float]:
    """Return the pixel aspect ratio (w/h) the crop must have, or None if free."""
    if settings.preset == "original":
        return src_w / src_h
    if settings.preset == "free":
        return None
    if settings.preset == "custom":
        a, b = settings.custom_ratio
        if a <= 0 or b <= 0:
            raise ValueError("Custom ratio values must be positive")
        return a / b
    _label, ratio = ASPECT_PRESETS[settings.preset]
    return ratio[0] / ratio[1]


def center_rect(ratio: float, src_w: int, src_h: int) -> NormRect:
    """Largest centered normalised rect with pixel ratio *ratio* inside the frame."""
    if src_w / src_h > ratio:  # frame is wider -> limit by height
        h_n = 1.0
        w_n = ratio * src_h / src_w
    else:
        w_n = 1.0
        h_n = src_w / (ratio * src_h)
    return ((1 - w_n) / 2, (1 - h_n) / 2, w_n, h_n)


def clamp_rect(rect: NormRect) -> NormRect:
    """Keep a normalised rect inside the frame with a sensible minimum size."""
    x, y, w, h = rect
    w = min(max(w, MIN_CROP_FRACTION), 1.0)
    h = min(max(h, MIN_CROP_FRACTION), 1.0)
    x = min(max(x, 0.0), 1.0 - w)
    y = min(max(y, 0.0), 1.0 - h)
    return (x, y, w, h)


def resize_with_ratio(anchor: Tuple[float, float], pointer: Tuple[float, float],
                      ratio: Optional[float], src_w: int, src_h: int) -> NormRect:
    """New normalised rect from a fixed *anchor* corner to a dragged *pointer* corner.

    With a fixed pixel *ratio* the rect keeps that ratio; the larger drag axis wins.
    """
    ax, ay = anchor
    px = min(max(pointer[0], 0.0), 1.0)
    py = min(max(pointer[1], 0.0), 1.0)
    dx, dy = px - ax, py - ay
    sx = 1.0 if dx >= 0 else -1.0
    sy = 1.0 if dy >= 0 else -1.0
    w_n, h_n = abs(dx), abs(dy)
    if ratio is not None:
        # convert to pixel space to compare, then back
        w_px, h_px = w_n * src_w, h_n * src_h
        if h_px * ratio > w_px:
            w_px = h_px * ratio
        else:
            h_px = w_px / ratio
        w_n, h_n = w_px / src_w, h_px / src_h
        # shrink if it would leave the frame
        max_w = (1.0 - ax) if sx > 0 else ax
        max_h = (1.0 - ay) if sy > 0 else ay
        scale = min(1.0, max_w / w_n if w_n else 1.0, max_h / h_n if h_n else 1.0)
        w_n, h_n = w_n * scale, h_n * scale
    x = ax if sx > 0 else ax - w_n
    y = ay if sy > 0 else ay - h_n
    return clamp_rect((x, y, w_n, h_n))


def _even_down(v: float) -> int:
    return max(2, int(v) // 2 * 2)


def compute_crop_rect(settings: CropSettings, src_w: int, src_h: int) -> Optional[CropRect]:
    """Pixel crop rectangle for FFmpeg, or None when no crop is needed.

    * ``original`` without a custom rect -> None (no crop).
    * A ratio preset without a positioned rect -> centered crop (the default).
    * Width/height/x/y are forced even so yuv420p chroma stays aligned.
    """
    if src_w <= 0 or src_h <= 0:
        raise ValueError("Source dimensions must be positive")
    if settings.preset == "original" and settings.rect is None:
        return None
    ratio = target_ratio(settings, src_w, src_h)
    if settings.rect is not None:
        nx, ny, nw, nh = clamp_rect(settings.rect)
    elif ratio is not None:
        nx, ny, nw, nh = center_rect(ratio, src_w, src_h)
    else:  # free without rect -> whole frame
        return None
    w = _even_down(nw * src_w)
    h = _even_down(nh * src_h)
    x = int(round(nx * src_w)) // 2 * 2
    y = int(round(ny * src_h)) // 2 * 2
    w = min(w, _even_down(src_w - x)) if x + w > src_w else w
    h = min(h, _even_down(src_h - y)) if y + h > src_h else h
    if x + w > src_w:
        x = max(0, (src_w - w) // 2 * 2)
    if y + h > src_h:
        y = max(0, (src_h - h) // 2 * 2)
    if (x, y, w, h) == (0, 0, src_w // 2 * 2, src_h // 2 * 2) and settings.preset == "original":
        return None
    return CropRect(x, y, w, h)


def output_size(settings: CropSettings, src_w: int, src_h: int) -> Tuple[int, int]:
    """Expected output (width, height) after cropping."""
    r = compute_crop_rect(settings, src_w, src_h)
    if r is None:
        return (src_w // 2 * 2, src_h // 2 * 2)
    return (r.w, r.h)

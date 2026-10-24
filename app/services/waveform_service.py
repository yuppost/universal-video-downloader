"""Audio waveform peaks for the timeline (from a *local* media file), computed with one FFmpeg pass."""

from __future__ import annotations

import logging
import subprocess
import threading
from array import array
from pathlib import Path
from typing import List, Optional

from app.services.errors import OperationCanceled

log = logging.getLogger("uvs.wave")
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
SAMPLE_RATE = 2000                 # plenty for a picture of the loudness


def bin_count(duration: float) -> int:
    """About 4 bars per second, between 200 and 4000 (the timeline resamples to its own zoom)."""
    return max(200, min(4000, int(duration * 4)))


def peaks_from_samples(samples: array, bins: int) -> List[float]:
    """Largest absolute value per bin, scaled to 0..1."""
    n = len(samples)
    if n == 0 or bins <= 0:
        return []
    out: List[float] = []
    for i in range(bins):
        a, b = n * i // bins, max(n * i // bins + 1, n * (i + 1) // bins)
        seg = samples[a:b]
        out.append(max(max(seg), -min(seg)) / 32768.0 if seg else 0.0)
    return out


def compute_peaks(ffmpeg: str, source: Path, duration: float, cancel: Optional[threading.Event] = None) -> List[float]:
    """Decode the audio of *source* to 2 kHz mono and return the loudness peaks (empty list when it has no audio)."""
    bins = bin_count(duration)
    cmd = [ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-i", str(source), "-vn", "-ac", "1",
           "-ar", str(SAMPLE_RATE), "-f", "s16le", "-"]
    samples = array("h")
    # stdout is read to the end by communicate-style reads; the audio of a long video is small at 2 kHz (4 KB/s)
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                            creationflags=_NO_WINDOW)
    try:
        carry = b""
        while True:
            if cancel is not None and cancel.is_set():
                raise OperationCanceled()
            chunk = proc.stdout.read(1 << 16)
            if not chunk:
                break
            chunk = carry + chunk
            usable = len(chunk) - (len(chunk) % 2)
            carry = chunk[usable:]
            part = array("h")
            part.frombytes(chunk[:usable])
            if array("h", [1]).tobytes() != (1).to_bytes(2, "little"):      # big-endian host: FFmpeg wrote little-endian
                part.byteswap()
            samples.extend(part)
    finally:
        try:
            proc.kill()
        except OSError:
            pass
        proc.wait()
    return peaks_from_samples(samples, bins)

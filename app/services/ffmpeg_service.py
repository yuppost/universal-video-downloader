"""FFmpeg discovery, safe command construction and execution with progress/cancel."""

from __future__ import annotations

import collections
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional

from app.models.clip import OutputSettings
from app.models.crop import CropRect
from app.services.errors import FFmpegNotFound, FriendlyError, OperationCanceled

log = logging.getLogger("uvs.ffmpeg")
_EXE = ".exe" if os.name == "nt" else ""
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# Codecs that can be stream-copied into an MP4 that plays everywhere.
COPY_OK_VIDEO = {"h264"}
COPY_OK_VIDEO_H265 = {"hevc"}
COPY_OK_AUDIO = {"aac", "mp3", "ac3"}


def _app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parents[2]


def find_executable(name: str, configured: str = "") -> Optional[str]:
    """Locate ``ffmpeg``/``ffprobe``.

    Order: configured file or folder -> bundled ``ffmpeg/`` or ``ffmpeg/bin`` next to
    the app (portable layout) -> PATH.
    """
    exe = f"{name}{_EXE}"
    if configured:
        p = Path(configured)
        cand = [p] if p.is_file() else [p / exe, p / "bin" / exe]
        if p.is_file() and p.name.lower() not in (f"ffmpeg{_EXE}", f"ffprobe{_EXE}"):
            cand = [p]
        elif p.is_file():
            cand = [p.with_name(exe)]
        for c in cand:
            if c.is_file():
                return str(c)
    try:
        from app.utils.paths import app_data_dir
        managed = app_data_dir() / "tools" / "ffmpeg" / exe
        if managed.is_file():
            return str(managed)
    except OSError:
        pass
    base = _app_dir()
    for c in (base / "ffmpeg" / exe, base / "ffmpeg" / "bin" / exe, base / exe):
        if c.is_file():
            return str(c)
    return shutil.which(name)


def require_ffmpeg(configured: str = "") -> str:
    path = find_executable("ffmpeg", configured)
    if not path:
        raise FFmpegNotFound("FFmpeg")
    return path


def require_ffprobe(configured_ffmpeg: str = "") -> str:
    path = find_executable("ffprobe", configured_ffmpeg)
    if not path:
        raise FFmpegNotFound("FFprobe")
    return path


def ffmpeg_version(path: str) -> str:
    try:
        out = subprocess.run([path, "-version"], capture_output=True, text=True, timeout=10,
                             creationflags=_NO_WINDOW).stdout
        return out.splitlines()[0] if out else "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unavailable"


# ---------------------------------------------------------------------------
# Encoding decision (pure, testable)
# ---------------------------------------------------------------------------
@dataclass
class EncodingPlan:
    """What FFmpeg will do for one clip and why (shown to the user)."""

    video: str            # "copy" | "h264" | "h265" | "none"
    audio: str            # "copy" | "aac" | "mp3" | "none"
    crop: Optional[CropRect] = None
    reasons: List[str] = field(default_factory=list)

    @property
    def frame_accurate(self) -> bool:
        return self.video not in ("copy",)

    @property
    def reencodes(self) -> bool:
        return self.video in ("h264", "h265") or self.audio in ("aac", "mp3")


def decide_encoding(output: OutputSettings, vcodec: Optional[str], acodec: Optional[str],
                    crop: Optional[CropRect]) -> EncodingPlan:
    """Choose copy vs re-encode for a clip given the source codecs and options."""
    if output.container == "mp3":
        return EncodingPlan("none", "mp3", None, ["Audio extraction always re-encodes to MP3."])
    reasons: List[str] = []
    target_v = "h265" if output.video_codec == "h265" else "h264"
    ok_v = COPY_OK_VIDEO_H265 if target_v == "h265" else COPY_OK_VIDEO
    video = "copy"
    if vcodec is None:
        video = "none"
    elif crop is not None:
        video = target_v
        reasons.append("Cropping requires re-encoding the video.")
    elif output.trim_mode == "accurate":
        video = target_v
        reasons.append("Accurate mode re-encodes video for precise cut points.")
    elif vcodec not in ok_v:
        video = target_v
        reasons.append(f"Source video codec '{vcodec}' cannot be copied into a compatible MP4; transcoding.")
    audio = "copy"
    if acodec is None:
        audio = "none"
    elif output.trim_mode == "accurate" or acodec not in COPY_OK_AUDIO:
        audio = "aac"
        if acodec not in COPY_OK_AUDIO:
            reasons.append(f"Source audio codec '{acodec}' is converted to AAC for MP4 compatibility.")
    if video == "copy":
        reasons.append("Stream copy: cut points snap to keyframes, so start/end may be off by up to a few seconds.")
    return EncodingPlan(video, audio, crop, reasons)


_nvenc_cache: dict = {}
NVENC_NAMES = {"h264": "h264_nvenc", "h265": "hevc_nvenc"}


def detect_nvenc(ffmpeg: str, codec: str = "h264") -> bool:
    """True if this FFmpeg can really encode *codec* ("h264"/"h265") with NVIDIA NVENC on this PC.

    First checks that FFmpeg lists the encoder, then runs a tiny real encode (a driver or GPU without NVENC fails here)."""
    key = (ffmpeg, codec)
    if key in _nvenc_cache:
        return _nvenc_cache[key]
    enc = NVENC_NAMES.get(codec, "h264_nvenc")
    ok = False
    try:
        listed = subprocess.run([ffmpeg, "-hide_banner", "-nostdin", "-encoders"], capture_output=True, text=True,
                                timeout=30, creationflags=_NO_WINDOW).stdout or ""
        if enc in listed:
            r = subprocess.run([ffmpeg, "-hide_banner", "-nostdin", "-v", "error", "-f", "lavfi", "-i",
                                "color=c=black:s=640x360:d=0.2", "-c:v", enc, "-f", "null", "-"],
                               capture_output=True, timeout=30, creationflags=_NO_WINDOW)
            ok = r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        ok = False
    _nvenc_cache[key] = ok
    return ok


def gpu_name() -> str:
    """Name of the NVIDIA GPU (via nvidia-smi, which ships with the driver); '' when not found."""
    exe = shutil.which("nvidia-smi")
    if not exe and os.name == "nt":
        cand = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "nvidia-smi.exe"
        exe = str(cand) if cand.is_file() else None
    if not exe:
        return ""
    try:
        out = subprocess.run([exe, "--query-gpu=name", "--format=csv,noheader"], capture_output=True, text=True,
                             timeout=15, creationflags=_NO_WINDOW).stdout or ""
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.strip().splitlines()[0].strip() if out.strip() else ""


@dataclass
class GpuInfo:
    h264: bool
    h265: bool
    name: str = ""

    @property
    def available(self) -> bool:
        return self.h264 or self.h265

    def describe(self) -> str:
        gpu = self.name or "NVIDIA GPU"
        if self.h264 and self.h265:
            return f"{gpu}: NVENC ready (H.264 + H.265)"
        if self.h264:
            return f"{gpu}: NVENC ready (H.264 only; H.265 uses CPU)"
        return "No usable NVIDIA NVENC encoder found - the CPU will be used"


def probe_gpu(ffmpeg: str) -> GpuInfo:
    h264, h265 = detect_nvenc(ffmpeg, "h264"), detect_nvenc(ffmpeg, "h265")
    return GpuInfo(h264, h265, gpu_name() if (h264 or h265) else "")


def choose_encoder(mode: str, video: str, ffmpeg: str) -> tuple:
    """Decide the encoder for one clip: ``(use_gpu, label)``.

    * mode "cpu"  -> always the CPU; the GPU is never even probed.
    * mode "auto" -> NVENC when this PC can really use it, otherwise CPU.
    * mode "nvenc"-> NVENC when possible; if not, CPU (and the label says so)."""
    if video not in NVENC_NAMES:
        return False, ""                               # stream copy / audio: no video encoder involved
    if mode == "cpu":
        return False, "CPU"
    ok = detect_nvenc(ffmpeg, video)
    if ok:
        return True, "GPU NVENC"
    return False, "CPU (GPU not available)" if mode == "nvenc" else "CPU"


def build_clip_command(ffmpeg: str, src: str, dst: str, start: float, duration: float,
                       output: OutputSettings, plan: EncodingPlan, use_nvenc: bool = False) -> List[str]:
    """Build the FFmpeg argument list (never a shell string)."""
    if start < 0:
        raise ValueError("start must be >= 0")
    if duration <= 0:
        raise ValueError("duration must be > 0")
    args = [ffmpeg, "-hide_banner", "-nostdin", "-y", "-loglevel", "error",
            "-progress", "pipe:1", "-nostats",
            "-ss", f"{start:.3f}", "-i", src, "-t", f"{duration:.3f}"]
    if output.container == "mp3":
        args += ["-vn", "-map", "0:a:0", "-c:a", "libmp3lame", "-q:a", "2", "-f", "mp3", dst]
        return args
    args += ["-map", "0:v:0?", "-map", "0:a:0?"]
    if plan.video == "copy":
        args += ["-c:v", "copy"]
    elif plan.video in ("h264", "h265"):
        filters = []
        if plan.crop is not None:
            filters.append(plan.crop.ffmpeg_filter())
        filters.append("scale=trunc(iw/2)*2:trunc(ih/2)*2")  # even dimensions for yuv420p
        args += ["-vf", ",".join(filters)]
        if use_nvenc and plan.video == "h264":
            args += ["-c:v", "h264_nvenc", "-preset", "p5", "-rc", "vbr", "-cq", "21", "-b:v", "0"]
        elif use_nvenc:
            args += ["-c:v", "hevc_nvenc", "-preset", "p5", "-rc", "vbr", "-cq", "25", "-b:v", "0", "-tag:v", "hvc1"]
        elif plan.video == "h264":
            args += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20"]
        else:
            args += ["-c:v", "libx265", "-preset", "veryfast", "-crf", "24", "-tag:v", "hvc1"]
        args += ["-pix_fmt", "yuv420p"]
    if plan.audio == "copy":
        args += ["-c:a", "copy"]
    elif plan.audio == "aac":
        args += ["-c:a", "aac", "-b:a", "160k"]
    args += ["-avoid_negative_ts", "make_zero", "-movflags", "+faststart", "-f", "mp4", dst]
    return args


def validate_args(args: List[str]) -> None:
    """Defensive check: a list of plain strings, no NULs, no empty elements."""
    if not isinstance(args, list) or not args:
        raise ValueError("FFmpeg arguments must be a non-empty list")
    for a in args:
        if not isinstance(a, str) or not a or "\x00" in a:
            raise ValueError(f"Invalid FFmpeg argument: {a!r}")


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------
ProgressCb = Callable[[Optional[float]], None]


def summarize_error(stderr_tail: str) -> str:
    """Translate raw FFmpeg stderr into a short, understandable sentence."""
    t = stderr_tail.lower()
    rules = [
        ("no space left", "The disk is full. Free up space or choose another output folder."),
        ("permission denied", "FFmpeg was denied access to a file or folder. Check folder permissions."),
        ("invalid data found", "The source media looks damaged or incomplete."),
        ("moov atom not found", "The source file is incomplete (download may have been interrupted)."),
        ("unknown encoder", "This FFmpeg build lacks a required encoder (e.g. libx264/libx265/libmp3lame). Install a full FFmpeg build."),
        ("encoder (codec", "This FFmpeg build lacks a required encoder. Install a full FFmpeg build."),
        ("does not contain any stream", "The selected range contains no media. Check the clip's start/end."),
        ("no such file", "A required file is missing (it may have been moved or deleted)."),
        ("output file is empty", "Nothing was produced for that time range."),
    ]
    for needle, msg in rules:
        if needle in t:
            return msg
    last = [ln for ln in stderr_tail.strip().splitlines() if ln.strip()]
    return "FFmpeg failed: " + (last[-1][:200] if last else "unknown error")


def run_ffmpeg(args: List[str], total_duration: Optional[float], on_progress: Optional[ProgressCb],
               cancel_event: Optional[threading.Event]) -> None:
    """Run FFmpeg, report 0-100 progress, honor *cancel_event*, raise on failure."""
    validate_args(args)
    log.info("Running FFmpeg (%d args)", len(args))
    proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            encoding="utf-8", errors="replace", creationflags=_NO_WINDOW)
    tail: "collections.deque[str]" = collections.deque(maxlen=60)
    canceled = threading.Event()

    def drain_stderr() -> None:
        assert proc.stderr is not None
        for line in proc.stderr:
            tail.append(line.rstrip())

    def watch_cancel() -> None:
        while proc.poll() is None:
            if cancel_event is not None and cancel_event.wait(0.2):
                canceled.set()
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
                return

    t1 = threading.Thread(target=drain_stderr, daemon=True)
    t2 = threading.Thread(target=watch_cancel, daemon=True)
    t1.start()
    t2.start()
    assert proc.stdout is not None
    for line in proc.stdout:
        m = re.match(r"out_time_us=(\d+)|out_time_ms=(\d+)", line)
        if m and on_progress and total_duration:
            us = int(m.group(1) or m.group(2))  # both are microseconds in practice
            on_progress(max(0.0, min(99.9, us / 1_000_000 / total_duration * 100)))
    rc = proc.wait()
    t1.join(timeout=2)
    if canceled.is_set() or (cancel_event is not None and cancel_event.is_set()):
        raise OperationCanceled()
    if rc != 0:
        detail = "\n".join(tail)
        log.error("FFmpeg exited with %s: %s", rc, detail[-500:])
        raise FriendlyError(summarize_error(detail), detail)

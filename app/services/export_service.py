"""Export planning (pure) and execution of source-download / clip-export jobs (no Qt)."""

from __future__ import annotations

import logging
import shutil
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from app.models.clip import Clip, OutputSettings
from app.models.crop import CropSettings, compute_crop_rect
from app.models.download_job import JobStatus
from app.services import ytdlp_service as yd
from app.services.errors import FriendlyError, OperationCanceled
from app.services.ffmpeg_service import (build_clip_command, choose_encoder, decide_encoding, detect_nvenc,  # noqa: F401
                                         run_ffmpeg)
from app.services.media_info_service import probe
from app.utils.paths import finalize_file, render_filename_template, sanitize_filename

log = logging.getLogger("uvs.export")
Report = Callable[[JobStatus, str, Optional[float], Optional[float], Optional[float]], None]

SECTION_MAX_FRACTION = 0.5   # remote sections only when selection is <= 50% of the video
SECTION_PADDING = 0.0


@dataclass(frozen=True)
class SourceSpec:
    url: str
    title: str
    selector: str
    key: str
    cache_dir: Path
    ffmpeg_path: str
    cookies_browser: str = ""


@dataclass(frozen=True)
class ClipSpec:
    clip_id: str
    name: str
    start: float
    end: float
    crop: CropSettings
    output: OutputSettings
    src_w: int
    src_h: int
    output_dir: Path
    stem: str
    mode: str                      # "local" (trim a downloaded file) or "section" (remote range)
    url: str
    selector: str
    ffmpeg_path: str
    temp_dir: Path
    cookies_browser: str = ""
    keep_intermediates: bool = False
    hw_encoder: str = "auto"       # "auto" (NVENC if usable) | "nvenc" | "cpu"


@dataclass
class PlannedClip:
    clip: Clip
    index: int
    stem: str


@dataclass
class ExportPlan:
    strategy: str                  # "local_cache" | "download_once" | "remote_sections"
    clips: List[PlannedClip]
    selector: str
    key: str
    notes: List[str] = field(default_factory=list)

    @property
    def needs_source_download(self) -> bool:
        return self.strategy == "download_once"


def clip_stem(clip: Clip, index: int, template: str, title: str, uploader: str, vid: str, site: str) -> str:
    """Final file stem for a clip: explicit per-clip name, else the filename template."""
    if clip.filename.strip():
        return sanitize_filename(clip.filename)
    return render_filename_template(template, {
        "title": sanitize_filename(title, 80), "index": index, "start": int(clip.start),
        "end": int(clip.end), "uploader": sanitize_filename(uploader, 40) if uploader else "",
        "id": vid, "site": site})


def plan_export(clips: List[Clip], video_duration: float, url: str, title: str, uploader: str,
                video_id: str, site: str, template: str, cache_dir: Path, max_height: Optional[int],
                audio_only: bool, prefer_sections: bool, cached_source: Optional[Path] = None
                ) -> ExportPlan:
    """Decide how to obtain media for *clips* and name the output files.

    Never plans one full download per clip: either a single shared source download
    (or an already-cached file) serves every clip, or per-clip remote sections are
    used when the selection covers a small part of the video.
    """
    if not clips:
        raise ValueError("No clips to export")
    selector = yd.build_format_selector(max_height, audio_only)
    key = yd.source_key(url, selector)
    planned = [PlannedClip(c, i, clip_stem(c, i, template, title, uploader, video_id, site))
               for i, c in enumerate(clips, start=1)]
    cached = cached_source or yd.find_cached_source(cache_dir, key)
    total = sum(c.duration for c in clips)
    notes: List[str] = []
    if cached is not None:
        strategy = "local_cache"
        notes.append("Reusing the already downloaded source file; no new download needed.")
    elif prefer_sections and video_duration > 0 and total <= video_duration * SECTION_MAX_FRACTION:
        strategy = "remote_sections"
        notes.append("Downloading only the selected sections (support depends on the site; "
                     "some sites still transfer more data than the section).")
    else:
        strategy = "download_once"
        notes.append("Downloading the source once; every clip is trimmed locally from it.")
    return ExportPlan(strategy, planned, selector, key, notes)


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------
def execute_source_download(spec: SourceSpec, report: Report, cancel: threading.Event) -> Path:
    def rep(status: str, task: str, pct, speed, eta) -> None:
        report(JobStatus(status), task, pct, speed, eta)

    return yd.download_source(spec.url, spec.selector, spec.cache_dir, spec.key, spec.ffmpeg_path,
                              spec.cookies_browser, rep, cancel)


def execute_clip_export(spec: ClipSpec, source_path: Optional[Path], report: Report,
                        cancel: threading.Event) -> Path:
    """Produce one output file for a clip. Returns the final path.

    Writes to a hidden partial file next to the destination and only renames it on
    success, so failed/canceled jobs never leave a half-written file behind.
    """
    section_dir: Optional[Path] = None
    src: Optional[Path] = source_path
    start, end = spec.start, spec.end
    try:
        if spec.mode == "section":
            def rep(status: str, task: str, pct, speed, eta) -> None:
                report(JobStatus(status), task, pct, speed, eta)

            section_dir = spec.temp_dir / "sections" / f"{spec.clip_id}"
            name = f"sec_{spec.clip_id}"
            src = yd.download_section(spec.url, spec.selector, spec.start, spec.end, section_dir, name,
                                      spec.output.trim_mode == "accurate", spec.ffmpeg_path,
                                      spec.cookies_browser, rep, cancel)
            start, end = 0.0, spec.end - spec.start
        if src is None or not Path(src).is_file():
            raise FriendlyError("The source media file is missing. Retry the clip to download it again.")
        if cancel.is_set():
            raise OperationCanceled()

        info = probe(Path(src), spec.ffmpeg_path)
        if info.duration is not None:
            end = min(end, info.duration)
        if end - start <= 0:
            raise FriendlyError("The selected range is outside the downloaded media.")
        w = info.width or spec.src_w
        h = info.height or spec.src_h
        crop_rect = compute_crop_rect(spec.crop, w, h) if (spec.output.container == "mp4" and info.has_video) else None
        if spec.output.container == "mp3" and not info.has_audio:
            raise FriendlyError("This video has no audio track to extract.")
        plan = decide_encoding(spec.output, info.vcodec, info.acodec, crop_rect)

        if spec.output.container == "mp3":
            status, task = JobStatus.CONVERTING, "Extracting audio to MP3"
        elif crop_rect is not None:
            status, task = JobStatus.CROPPING, "Cropping and re-encoding (accurate)"
        elif plan.reencodes:
            status, task = JobStatus.TRIMMING, "Trimming with re-encode (accurate)"
        else:
            status, task = JobStatus.TRIMMING, "Trimming by stream copy (keyframe-limited)"
        report(status, task, 0.0, None, None)

        ext = spec.output.container
        spec.output_dir.mkdir(parents=True, exist_ok=True)
        partial = spec.output_dir / f".uvs_{spec.clip_id}.partial.{ext}"
        want_gpu, enc_label = choose_encoder(spec.hw_encoder, plan.video, spec.ffmpeg_path)
        tag = f" [{enc_label}]" if enc_label else ""
        report(status, task + tag, 0.0, None, None)
        try:
            for use_gpu in ((True, False) if want_gpu else (False,)):
                cmd = build_clip_command(spec.ffmpeg_path, str(src), str(partial), start, end - start,
                                         spec.output, plan, use_nvenc=use_gpu)
                shown = tag if (use_gpu or not want_gpu) else " [CPU - GPU failed]"
                try:
                    run_ffmpeg(cmd, end - start, lambda p, t=shown: report(status, task + t, p, None, None), cancel)
                    break
                except FriendlyError:
                    if not use_gpu:
                        raise
                    log.warning("NVENC encode failed; retrying on CPU")
                    partial.unlink(missing_ok=True)
            if not partial.exists() or partial.stat().st_size == 0:
                raise FriendlyError("FFmpeg produced an empty file.")
            final = finalize_file(partial, spec.output_dir, spec.stem, ext)
        except BaseException:
            partial.unlink(missing_ok=True)
            raise
        for r in plan.reasons:
            log.info("Clip %s: %s", spec.clip_id, r)
        return final
    except OSError as exc:
        if getattr(exc, "errno", None) == 28:
            raise FriendlyError("The disk is full. Free up space or choose another folder.") from exc
        raise FriendlyError(f"File error: {exc.strerror or exc}", str(exc)) from exc
    finally:
        if section_dir is not None and not spec.keep_intermediates:
            shutil.rmtree(section_dir, ignore_errors=True)

"""Real FFmpeg runs on a locally generated synthetic video."""
import threading
import time
from pathlib import Path

import pytest

from app.models.clip import OutputSettings
from app.models.crop import CropSettings
from app.models.download_job import JobStatus
from app.services.errors import FriendlyError, OperationCanceled
from app.services.export_service import ClipSpec, execute_clip_export
from app.services.ffmpeg_service import find_executable, run_ffmpeg
from app.services.media_info_service import probe
from app.services.thumbnail_service import generate_strip, thumbnail_times
from tests.conftest import needs_ffmpeg

pytestmark = needs_ffmpeg


def spec(tmp, name, start, end, crop=None, out=None, ffmpeg="ffmpeg"):
    return ClipSpec("c_" + name, name, start, end, crop or CropSettings(), out or OutputSettings(), 640, 360,
                    tmp / "out", name, "local", "u", "sel", ffmpeg, tmp / "tmp")


def run(tmp, src, name, start, end, **kw):
    reports = []
    path = execute_clip_export(spec(tmp, name, start, end, ffmpeg=find_executable("ffmpeg"), **kw), src,
                               lambda *a: reports.append(a), threading.Event())
    return path, reports


def test_probe(synthetic_video):
    i = probe(synthetic_video)
    assert (i.width, i.height, i.vcodec, i.acodec) == (640, 360, "h264", "aac")
    assert abs(i.duration - 20) < 0.2


def test_three_clips_become_three_separate_mp4_files(synthetic_video, tmp_path):
    paths = [run(tmp_path, synthetic_video, f"clip_{i}", s, e)[0] for i, (s, e) in enumerate([(2, 5), (6, 9), (12, 16)], 1)]
    assert len({p.name for p in paths}) == 3 and all(p.suffix == ".mp4" and p.stat().st_size > 0 for p in paths)
    assert not list((tmp_path / "out").glob(".uvs_*"))            # no partial files left behind


def test_fast_trim_is_keyframe_limited_accurate_is_precise(synthetic_video, tmp_path):
    fast, _ = run(tmp_path, synthetic_video, "fast", 5.0, 9.0)                       # keyframes every 2 s
    acc, _ = run(tmp_path, synthetic_video, "acc", 5.0, 9.0, out=OutputSettings(trim_mode="accurate"))
    assert abs(probe(acc).duration - 4.0) < 0.15
    assert probe(fast).duration >= 4.0 - 0.15                                         # snapped to keyframe -> never shorter
    assert probe(acc).vcodec == "h264"


def test_crop_changes_output_dimensions(synthetic_video, tmp_path):
    for preset, expected in (("9:16", (202, 360)), ("1:1", (360, 360)), ("16:9", (640, 360))):
        p, reps = run(tmp_path, synthetic_video, "c" + preset.replace(":", ""), 3, 6, crop=CropSettings(preset))
        i = probe(p)
        assert (i.width, i.height) == expected and i.width % 2 == 0 and i.height % 2 == 0
        assert any(r[0] == JobStatus.CROPPING for r in reps)


def test_mp3_extraction(synthetic_video, tmp_path):
    p, _ = run(tmp_path, synthetic_video, "audio", 2, 6, out=OutputSettings(container="mp3"))
    i = probe(p)
    assert p.suffix == ".mp3" and i.acodec == "mp3" and not i.has_video


def test_filename_collision_never_overwrites(synthetic_video, tmp_path):
    a, _ = run(tmp_path, synthetic_video, "same", 1, 3)
    b, _ = run(tmp_path, synthetic_video, "same", 1, 3)
    assert a != b and a.exists() and b.exists() and b.name == "same (2).mp4"


def test_progress_is_reported_and_bounded(synthetic_video, tmp_path):
    _p, reps = run(tmp_path, synthetic_video, "prog", 0, 8, out=OutputSettings(trim_mode="accurate"))
    pcts = [r[2] for r in reps if r[2] is not None]
    assert pcts and all(0 <= x <= 100 for x in pcts)


def test_missing_source_is_a_friendly_error(tmp_path):
    with pytest.raises(FriendlyError):
        execute_clip_export(spec(tmp_path, "x", 0, 3, ffmpeg=find_executable("ffmpeg")), tmp_path / "nope.mp4",
                            lambda *a: None, threading.Event())


def test_range_beyond_media_is_error(synthetic_video, tmp_path):
    with pytest.raises(FriendlyError):
        run(tmp_path, synthetic_video, "late", 50, 60)


def test_corrupt_source_gives_readable_error_and_no_partial(tmp_path):
    bad = tmp_path / "bad.mp4"
    bad.write_bytes(b"this is not a video" * 100)
    with pytest.raises(FriendlyError) as ei:
        execute_clip_export(spec(tmp_path, "bad", 0, 3, ffmpeg=find_executable("ffmpeg")), bad, lambda *a: None,
                            threading.Event())
    assert "damaged" in ei.value.message or "could not be read" in ei.value.message
    assert not list((tmp_path / "out").glob("*")) if (tmp_path / "out").exists() else True


def test_cancellation_stops_ffmpeg_and_cleans_up(synthetic_video, tmp_path):
    """Re-encode a long job and cancel almost immediately."""
    long_src = tmp_path / "long.mp4"
    import subprocess
    subprocess.run([find_executable("ffmpeg"), "-v", "error", "-y", "-f", "lavfi", "-i",
                    "testsrc2=duration=120:size=1280x720:rate=30", "-c:v", "libx264", "-preset", "ultrafast",
                    str(long_src)], check=True)
    ev = threading.Event()
    threading.Timer(0.5, ev.set).start()
    t0 = time.time()
    s = spec(tmp_path, "cancelme", 0, 120, out=OutputSettings(trim_mode="accurate"), ffmpeg=find_executable("ffmpeg"))
    with pytest.raises(OperationCanceled):
        execute_clip_export(s, long_src, lambda *a: None, ev)
    assert time.time() - t0 < 20
    assert not list((tmp_path / "out").glob("*"))                  # partial file removed


def test_pre_set_cancel_event_aborts_immediately(synthetic_video, tmp_path):
    ev = threading.Event()
    ev.set()
    with pytest.raises(OperationCanceled):
        execute_clip_export(spec(tmp_path, "z", 0, 3, ffmpeg=find_executable("ffmpeg")), synthetic_video,
                            lambda *a: None, ev)


def test_thumbnails(synthetic_video, tmp_path):
    assert thumbnail_times(20, 4) == [2.5, 7.5, 12.5, 17.5]
    got = []
    files = generate_strip(find_executable("ffmpeg"), synthetic_video, 20, tmp_path / "th", count=5,
                           on_thumb=lambda i, t, p: got.append(i))
    assert len(files) == 5 and got == [0, 1, 2, 3, 4]


def test_nvenc_failure_falls_back_to_cpu(synthetic_video, tmp_path, monkeypatch):
    """Pretend NVENC is detected; on a PC without it FFmpeg fails and the export must still succeed on CPU."""
    import app.services.ffmpeg_service as fs
    real = fs.detect_nvenc(find_executable("ffmpeg"))
    monkeypatch.setattr(fs, "detect_nvenc", lambda _f, _c="h264": True)
    p, reps = run(tmp_path, synthetic_video, "gpu", 2, 6, out=OutputSettings(trim_mode="accurate"))
    assert probe(p).vcodec == "h264" and abs(probe(p).duration - 4) < 0.2
    tasks = " ".join(r[1] for r in reps)
    assert ("GPU NVENC" in tasks) and (real or "CPU - GPU failed" in tasks)


def test_cpu_setting_never_tries_gpu(synthetic_video, tmp_path, monkeypatch):
    import app.services.ffmpeg_service as fs
    monkeypatch.setattr(fs, "detect_nvenc", lambda *_a: pytest.fail("must not probe GPU"))
    s = spec(tmp_path, "cpu", 2, 5, out=OutputSettings(trim_mode="accurate"), ffmpeg=find_executable("ffmpeg"))
    from dataclasses import replace
    reps = []
    execute_clip_export(replace(s, hw_encoder="cpu"), synthetic_video, lambda *a: reps.append(a), threading.Event())
    assert any("[CPU]" in r[1] for r in reps) and not any("GPU" in r[1] for r in reps)


def test_choose_encoder_modes(monkeypatch):
    import app.services.ffmpeg_service as fs
    monkeypatch.setattr(fs, "detect_nvenc", lambda _f, codec="h264": codec == "h264")      # GPU does H.264 only
    assert fs.choose_encoder("cpu", "h264", "ff") == (False, "CPU")
    assert fs.choose_encoder("auto", "h264", "ff") == (True, "GPU NVENC")
    assert fs.choose_encoder("nvenc", "h264", "ff") == (True, "GPU NVENC")
    assert fs.choose_encoder("auto", "h265", "ff") == (False, "CPU")
    assert fs.choose_encoder("nvenc", "h265", "ff") == (False, "CPU (GPU not available)")
    assert fs.choose_encoder("auto", "copy", "ff") == (False, "")                          # stream copy: no encoder
    monkeypatch.setattr(fs, "detect_nvenc", lambda *_a: False)
    assert fs.choose_encoder("auto", "h264", "ff") == (False, "CPU")
    assert fs.choose_encoder("nvenc", "h264", "ff") == (False, "CPU (GPU not available)")


def test_detect_nvenc_needs_the_encoder_to_be_listed(monkeypatch):
    import subprocess
    import app.services.ffmpeg_service as fs
    fs._nvenc_cache.clear()
    calls = []

    class R:
        def __init__(self, out="", rc=0):
            self.stdout, self.returncode = out, rc

    def fake_run(cmd, **k):
        calls.append(cmd)
        return R("V....D libx264" if "-encoders" in cmd else "", 0)
    monkeypatch.setattr(subprocess, "run", fake_run)
    assert fs.detect_nvenc("ffX", "h264") is False and len(calls) == 1             # not listed -> no test encode
    fake = lambda cmd, **k: R("V....D h264_nvenc hevc_nvenc" if "-encoders" in cmd else "", 0)
    monkeypatch.setattr(subprocess, "run", fake)
    assert fs.detect_nvenc("ffY", "h265") is True
    monkeypatch.setattr(subprocess, "run", lambda cmd, **k: R("V h264_nvenc" if "-encoders" in cmd else "", 1))
    assert fs.detect_nvenc("ffZ", "h264") is False                                # listed but the real encode failed
    fs._nvenc_cache.clear()


def test_gpu_info_text():
    from app.services.ffmpeg_service import GpuInfo
    assert "RTX 3060" in GpuInfo(True, True, "NVIDIA GeForce RTX 3060").describe()
    assert "H.265 uses CPU" in GpuInfo(True, False, "X").describe()
    assert "CPU will be used" in GpuInfo(False, False).describe() and not GpuInfo(False, False).available


@needs_ffmpeg
def test_duration_is_read_from_the_stream_when_metadata_has_none(tmp_path, ffmpeg_path):
    import subprocess
    from app.services import ytdlp_service as yd
    clip = tmp_path / "c.mp4"
    subprocess.run([ffmpeg_path, "-y", "-f", "lavfi", "-i", "testsrc=d=2:s=64x64:r=10", "-pix_fmt", "yuv420p", str(clip)],
                   capture_output=True, check=True)
    assert abs(yd.probe_duration(str(clip)) - 2.0) < 0.3
    info = {"formats": [{"url": "ftp://x"}]}
    assert yd._duration_from_formats(info) is None
    assert yd._nice_title({"title": "Facebook", "uploader": "Jonathan", "id": "1"}) == "Facebook video by Jonathan"
    assert yd._nice_title({"title": "Facebook", "description": "Hello world\nmore", "id": "1"}) == "Hello world"
    assert yd._nice_title({"title": "Real title"}) == "Real title"

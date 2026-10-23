from pathlib import Path

import pytest

from app.models.clip import ClipCollection, OutputSettings
from app.models.crop import CropSettings, CropRect
from app.services import ytdlp_service as yd
from app.services.export_service import clip_stem, plan_export
from app.services.ffmpeg_service import build_clip_command, decide_encoding, validate_args, summarize_error


def clips(n=3, dur=600.0):
    c = ClipCollection("v", dur)
    for i in range(n):
        c.add(10 + i * 100, 25 + i * 100)
    return c


def plan(c, tmp, **kw):
    args = dict(video_duration=600.0, url="https://x.com/v", title="My: Video?", uploader="me", video_id="abc",
                site="x.com", template="{title}_clip_{index:02d}", cache_dir=tmp, max_height=None,
                audio_only=False, prefer_sections=False)
    args.update(kw)
    return plan_export(c.clips, **args)


def test_naming_and_order(tmp_path):
    c = clips()
    p = plan(c, tmp_path)
    assert [pc.stem for pc in p.clips] == ["My_ Video__clip_01", "My_ Video__clip_02", "My_ Video__clip_03"]
    assert [pc.clip.id for pc in p.clips] == [x.id for x in c.clips]        # selected order preserved


def test_per_clip_filename_overrides_template():
    c = clips(1)
    c.set_filename(c.clips[0].id, "../evil/name")
    s = clip_stem(c.clips[0], 1, "{title}_{index}", "T", "", "id", "x.com")
    assert "/" not in s and "\\" not in s


def test_download_once_not_per_clip(tmp_path):
    p = plan(clips(5), tmp_path)
    assert p.strategy == "download_once" and p.needs_source_download   # ONE shared download, not five


def test_cached_source_reused(tmp_path):
    key = yd.source_key("https://x.com/v", yd.build_format_selector(None))
    (tmp_path / f"{key}.mp4").write_bytes(b"data")
    p = plan(clips(), tmp_path)
    assert p.strategy == "local_cache" and not p.needs_source_download


def test_partial_files_not_treated_as_cache(tmp_path):
    key = yd.source_key("https://x.com/v", yd.build_format_selector(None))
    (tmp_path / f"{key}.mp4.part").write_bytes(b"x")
    (tmp_path / f"{key}.f137.mp4").write_bytes(b"x")
    assert yd.find_cached_source(tmp_path, key) is None


def test_remote_sections_only_for_small_selection(tmp_path):
    small = plan(clips(3), tmp_path, prefer_sections=True)         # 45s of 600s
    assert small.strategy == "remote_sections"
    short = ClipCollection("v", 60.0)
    for a, b in ((0, 20), (20, 40), (40, 60)):          # selection covers the whole video
        short.add(a, b)
    big = plan(short, tmp_path, prefer_sections=True, video_duration=60.0)
    assert big.strategy == "download_once"


def test_empty_plan_rejected(tmp_path):
    with pytest.raises(ValueError):
        plan_export([], 10, "u", "t", "", "i", "s", "{title}", tmp_path, None, False, False)


# ---- quality ---------------------------------------------------------------
def test_quality_resolution_never_upscales():
    assert yd.resolve_quality(720, [1080, 720, 360]) == (720, "")
    h, note = yd.resolve_quality(2160, [1080, 720])
    assert h == 1080 and "not available" in note
    h, note = yd.resolve_quality(144, [360, 720])
    assert h == 360 and "lowest" in note
    assert yd.resolve_quality(None, [720]) == (None, "")


def test_format_selector():
    assert "height<=720" in yd.build_format_selector(720)
    assert yd.build_format_selector(None, audio_only=True) == "ba/b"


# ---- FFmpeg decisions & arguments --------------------------------------------
def test_fast_mode_copies_streams_when_compatible():
    p = decide_encoding(OutputSettings(), "h264", "aac", None)
    assert (p.video, p.audio) == ("copy", "copy") and not p.frame_accurate
    assert any("keyframe" in r for r in p.reasons)           # honest about accuracy


def test_accurate_mode_reencodes():
    p = decide_encoding(OutputSettings(trim_mode="accurate"), "h264", "aac", None)
    assert p.video == "h264" and p.audio == "aac" and p.frame_accurate


def test_crop_forces_reencode_and_incompatible_codecs_transcode():
    crop = CropRect(0, 0, 202, 360)
    assert decide_encoding(OutputSettings(), "h264", "aac", crop).video == "h264"
    p = decide_encoding(OutputSettings(), "vp9", "opus", None)
    assert (p.video, p.audio) == ("h264", "aac")
    assert decide_encoding(OutputSettings(container="mp3"), "h264", "aac", None).audio == "mp3"
    assert decide_encoding(OutputSettings(), "h264", None, None).audio == "none"


def test_command_is_argument_list_with_crop_and_even_scale():
    crop = CropRect(10, 0, 202, 360)
    plan_ = decide_encoding(OutputSettings(), "h264", "aac", crop)
    cmd = build_clip_command("ffmpeg", "in file; rm -rf x.mp4", "out.mp4", 5.0, 3.0, OutputSettings(), plan_)
    assert isinstance(cmd, list) and "in file; rm -rf x.mp4" in cmd      # path stays ONE argument
    vf = cmd[cmd.index("-vf") + 1]
    assert vf.startswith("crop=202:360:10:0") and "trunc(iw/2)*2" in vf
    assert "yuv420p" in cmd and "libx264" in cmd
    validate_args(cmd)


def test_command_copy_and_mp3():
    cmd = build_clip_command("ffmpeg", "a.mp4", "o.mp4", 1, 2, OutputSettings(), decide_encoding(OutputSettings(), "h264", "aac", None))
    assert cmd[cmd.index("-c:v") + 1] == "copy" and "-vf" not in cmd
    o = OutputSettings(container="mp3")
    cmd = build_clip_command("ffmpeg", "a.mp4", "o.mp3", 1, 2, o, decide_encoding(o, "h264", "aac", None))
    assert "libmp3lame" in cmd and "-vn" in cmd


def test_command_rejects_bad_values():
    p = decide_encoding(OutputSettings(), "h264", "aac", None)
    with pytest.raises(ValueError):
        build_clip_command("ffmpeg", "a", "b", -1, 2, OutputSettings(), p)
    with pytest.raises(ValueError):
        build_clip_command("ffmpeg", "a", "b", 0, 0, OutputSettings(), p)
    for bad in ([], ["a", ""], ["a", "b\x00"], "string"):
        with pytest.raises(ValueError):
            validate_args(bad)


def test_error_summaries():
    assert "disk is full" in summarize_error("write error: No space left on device")
    assert "damaged" in summarize_error("Invalid data found when processing input")
    assert "encoder" in summarize_error("Unknown encoder 'libx265'")


def test_nvenc_command_arguments():
    p = decide_encoding(OutputSettings(trim_mode="accurate"), "h264", "aac", None)
    cmd = build_clip_command("ffmpeg", "a.mp4", "o.mp4", 1, 2, OutputSettings(trim_mode="accurate"), p, use_nvenc=True)
    assert "h264_nvenc" in cmd and "libx264" not in cmd and "yuv420p" in cmd
    o = OutputSettings(video_codec="h265", trim_mode="accurate")
    cmd = build_clip_command("ffmpeg", "a.mp4", "o.mp4", 1, 2, o, decide_encoding(o, "h264", "aac", None), use_nvenc=True)
    assert "hevc_nvenc" in cmd and "hvc1" in cmd
    # stream copy never uses the GPU encoder
    cp = decide_encoding(OutputSettings(), "h264", "aac", None)
    assert "nvenc" not in " ".join(build_clip_command("ffmpeg", "a", "o", 1, 2, OutputSettings(), cp, use_nvenc=True))

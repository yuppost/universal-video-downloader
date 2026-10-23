import json

import pytest

from app.config import Settings, load_settings, save_settings
from app.services import ytdlp_service as yd
from app.services.errors import FriendlyError, OperationCanceled
import threading


def test_settings_roundtrip(tmp_path):
    p = tmp_path / "s.json"
    s = Settings()
    s.theme, s.max_concurrent, s.trim_mode = "light", 3, "accurate"
    save_settings(s, p)
    r = load_settings(p)
    assert (r.theme, r.max_concurrent, r.trim_mode) == ("light", 3, "accurate")


def test_settings_sanitize_bad_values(tmp_path):
    p = tmp_path / "s.json"
    p.write_text(json.dumps({"theme": "neon", "max_concurrent": 999, "output_format": "avi", "cookies_browser": "evil",
                             "unknown_key": 1, "filename_template": "  "}))
    s = load_settings(p)
    assert s.theme == "dark" and s.max_concurrent == 8 and s.output_format == "mp4" and s.cookies_browser == ""
    assert s.filename_template


def test_corrupt_settings_file_falls_back(tmp_path):
    p = tmp_path / "s.json"
    p.write_text("{not json")
    assert load_settings(p).theme == "dark"


def test_settings_contain_no_secrets():
    names = set(Settings().__dict__)
    assert not any(k in names for k in ("password", "cookies", "token"))


@pytest.mark.parametrize("raw,needle", [
    ("ERROR: Unsupported URL: https://x", "not supported"),
    ("ERROR: Sign in to confirm your age", "signed in"),
    ("ERROR: Private video", "private"),
    ("ERROR: HTTP Error 429", "rate"),
    ("ERROR: Video unavailable", "unavailable"),
    ("[Errno 28] No space left on device", "disk is full"),
    ("This video is DRM protected", "DRM"),
])
def test_friendly_ytdlp_errors(raw, needle):
    assert needle.lower() in yd.friendly_ytdlp_error(Exception(raw)).message.lower()


def test_formats_parsing_and_drm_and_preview_choice():
    info = {"formats": [
        {"format_id": "18", "ext": "mp4", "height": 360, "width": 640, "vcodec": "avc1", "acodec": "aac",
         "protocol": "https", "url": "https://cdn/x.mp4", "filesize": 1000},
        {"format_id": "sb0", "ext": "mhtml", "vcodec": "none", "acodec": "none"},
        {"format_id": "137", "ext": "mp4", "height": 1080, "width": 1920, "vcodec": "avc1", "acodec": "none",
         "protocol": "https", "url": "https://cdn/v", "filesize": 5000, "has_drm": False}]}
    formats, drm = yd._parse_formats(info)
    assert len(formats) == 2 and not drm                      # storyboard skipped
    assert yd.choose_preview_url(formats) == "https://cdn/x.mp4"
    _f, drm_only = yd._parse_formats({"formats": [{"format_id": "1", "vcodec": "avc1", "has_drm": True}]})
    assert drm_only


def test_estimate_size_adds_audio_track():
    v = yd.VideoInfo("u", "i", "t", "", 10, "", "x", [
        yd.FormatInfo("v", "mp4", 1080, 1920, 30, "avc1", "none", 5000, 1000),
        yd.FormatInfo("a", "m4a", None, None, None, "none", "aac", 700, 128)])
    assert v.estimate_size(None) == 5700 and v.estimate_size(144) is None and v.heights == [1080]


def test_unsupported_scheme_never_reaches_ytdlp():
    from app.utils.validation import UrlValidationError
    with pytest.raises(UrlValidationError):
        yd.analyze_url("file:///etc/passwd")


def test_run_cancellable_returns_and_cancels():
    assert yd.run_cancellable(lambda: 42, threading.Event()) == 42
    ev = threading.Event()
    threading.Timer(0.2, ev.set).start()
    import time
    with pytest.raises(OperationCanceled):
        yd.run_cancellable(lambda: time.sleep(5), ev)
    with pytest.raises(ValueError):
        yd.run_cancellable(lambda: (_ for _ in ()).throw(ValueError("x")), threading.Event())


def test_version_tuple():
    assert yd.version_tuple("2026.08.19") > yd.version_tuple("2025.12.31")


def test_reload_error_message():
    assert "temporary" in yd.friendly_ytdlp_error(Exception("The page needs to be reloaded.")).message


def test_default_folders_and_migration(tmp_path, monkeypatch):
    import json
    from pathlib import Path
    from app.config import load_settings, Settings
    from app.utils import paths
    monkeypatch.setenv("UVS_DATA_DIR", str(tmp_path / "d"))
    s = Settings()
    assert Path(s.download_dir).name == "Video" and Path(s.temp_dir).name == "Cache video"
    old_dl, old_tmp = paths.legacy_defaults()
    f = tmp_path / "s.json"
    f.write_text(json.dumps({"download_dir": str(old_dl), "temp_dir": str(old_tmp)}))
    s = load_settings(f)
    assert Path(s.download_dir).name == "Video" and Path(s.temp_dir).name == "Cache video"
    f.write_text(json.dumps({"download_dir": str(tmp_path / "mine")}))
    assert load_settings(f).download_dir == str(tmp_path / "mine")


def test_data_folder_alone_is_not_portable(tmp_path):
    from app.utils import paths
    assert paths.portable_root() is None or (paths.portable_root().parent / "portable.flag").exists()

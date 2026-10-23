import pytest

from app.models.crop import (CropSettings, center_rect, compute_crop_rect, output_size, resize_with_ratio,
                             target_ratio, clamp_rect)


def test_original_means_no_crop():
    assert compute_crop_rect(CropSettings(), 1920, 1080) is None


@pytest.mark.parametrize("preset,src,expected", [
    ("9:16", (1920, 1080), (606, 1080)),
    ("1:1", (1280, 720), (720, 720)),
    ("16:9", (1080, 1920), (1080, 606)),
    ("16:9", (1920, 1080), (1920, 1080)),
])
def test_preset_centered_sizes(preset, src, expected):
    r = compute_crop_rect(CropSettings(preset), *src)
    assert (r.w, r.h) == expected
    assert r.w % 2 == 0 and r.h % 2 == 0 and r.x % 2 == 0 and r.y % 2 == 0
    assert r.x + r.w <= src[0] and r.y + r.h <= src[1]


def test_center_crop_is_centered():
    r = compute_crop_rect(CropSettings("9:16"), 1920, 1080)
    assert abs((r.x + r.w / 2) - 960) <= 2


@pytest.mark.parametrize("w,h", [(641, 361), (853, 479), (1279, 719)])
def test_odd_source_sizes_stay_even_and_inside(w, h):
    for p in ("9:16", "1:1", "16:9"):
        r = compute_crop_rect(CropSettings(p), w, h)
        assert r.w % 2 == 0 and r.h % 2 == 0 and r.x + r.w <= w and r.y + r.h <= h


def test_custom_ratio_and_positioned_rect():
    s = CropSettings("custom", None, (4.0, 3.0))
    r = compute_crop_rect(s, 1920, 1080)
    assert abs(r.w / r.h - 4 / 3) < 0.01
    s2 = CropSettings("1:1", (0.0, 0.0, 0.5, 0.5 * 1280 / 720))
    r2 = compute_crop_rect(s2, 1280, 720)
    assert (r2.x, r2.y) == (0, 0)


def test_custom_ratio_must_be_positive():
    with pytest.raises(ValueError):
        target_ratio(CropSettings("custom", None, (0, 3)), 100, 100)


def test_resize_keeps_ratio_and_stays_in_frame():
    ratio = 1.0
    r = resize_with_ratio((0.2, 0.2), (0.9, 0.5), ratio, 1280, 720)
    px_w, px_h = r[2] * 1280, r[3] * 720
    assert abs(px_w / px_h - 1.0) < 0.01
    assert r[0] >= 0 and r[1] >= 0 and r[0] + r[2] <= 1.0001 and r[1] + r[3] <= 1.0001
    r = resize_with_ratio((0.9, 0.9), (2.0, 2.0), ratio, 1280, 720)   # pointer outside frame
    assert r[0] + r[2] <= 1.0001 and r[1] + r[3] <= 1.0001


def test_clamp_and_output_size_and_serialization():
    assert clamp_rect((-1, -1, 5, 5)) == (0.0, 0.0, 1.0, 1.0)
    assert output_size(CropSettings("9:16"), 1920, 1080) == (606, 1080)
    s = CropSettings("custom", (0.1, 0.1, 0.5, 0.5), (3.0, 2.0))
    assert CropSettings.from_dict(s.to_dict()).to_dict() == s.to_dict()
    assert CropSettings.from_dict({"preset": "bogus"}).preset == "original"
    with pytest.raises(ValueError):
        compute_crop_rect(CropSettings("1:1"), 0, 10)

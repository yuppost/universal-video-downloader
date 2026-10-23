import pytest

from app.models.clip import ClipCollection, ClipError, MIN_CLIP_DURATION, OutputSettings, validate_range


def make():
    return ClipCollection("src", 600.0)


@pytest.mark.parametrize("s,e", [(-1, 5), (5, 5), (10, 5), (0, 0.1), (590, 601), (float("nan"), 3)])
def test_invalid_ranges_rejected(s, e):
    with pytest.raises(ClipError):
        validate_range(s, e, 600)


def test_valid_range_ok():
    validate_range(0, 600, 600)
    validate_range(15, 15 + MIN_CLIP_DURATION, 600)


def test_three_non_contiguous_clips_keep_order_and_distinct_colors():
    c = make()
    for s, e in ((15, 30), (70, 105), (260, 290)):
        c.add(s, e)
    assert [(x.start, x.end) for x in c.clips] == [(15, 30), (70, 105), (260, 290)]
    assert len({x.color for x in c.clips}) == 3
    assert [x.name for x in c.clips] == ["Clip 01", "Clip 02", "Clip 03"]


def test_edit_operations():
    c = make()
    a = c.add(10, 20)
    c.set_start(a.id, 12)
    c.set_end(a.id, 25)
    assert (a.start, a.end) == (12, 25)
    c.move_to(a.id, 100)
    assert (a.start, a.end) == (100, 113)
    c.move_to(a.id, 10_000)           # clamped inside video
    assert a.end == 600
    c.set_start(a.id, 9999)           # clamped so min duration remains
    assert a.duration >= MIN_CLIP_DURATION - 1e-9
    c.rename(a.id, "Intro")
    assert a.name == "Intro"
    with pytest.raises(ClipError):
        c.rename(a.id, "  ")


def test_duplicate_delete_reorder_reset():
    c = make()
    a = c.add(10, 20)
    b = c.add(30, 40)
    d = c.duplicate(a.id)
    assert c.clips[1] is d and d.id != a.id and (d.start, d.end) == (10, 20)
    c.reorder(0, 2)
    assert c.clips[2] is a
    c.reset(b.id)
    assert (b.start, b.end) == (0, 5)
    c.remove(d.id)
    assert len(c) == 2
    with pytest.raises(ClipError):
        c.get(d.id)
    with pytest.raises(ClipError):
        c.reorder(0, 9)


def test_independent_crop_and_output_settings():
    from app.models.crop import CropSettings
    c = make()
    a, b = c.add(0, 5), c.add(10, 15)
    c.set_crop(a.id, CropSettings("9:16"))
    assert b.crop.preset == "original"
    dup = c.duplicate(a.id)
    dup.crop.preset = "1:1"
    assert a.crop.preset == "9:16"      # deep copy, not shared
    c.apply_output_to_all(OutputSettings(trim_mode="accurate"))
    assert all(x.output.trim_mode == "accurate" for x in c.clips)
    with pytest.raises(ClipError):
        c.set_output(a.id, OutputSettings(container="avi"))


def test_notifications_fire():
    c = make()
    calls = []
    c.subscribe(lambda: calls.append(1))
    a = c.add(0, 5)
    c.set_range(a.id, 1, 6)
    c.remove(a.id)
    assert len(calls) == 3


def test_find_gap_avoids_overlap():
    c = make()
    c.add(0, 5)
    s, e = c.find_gap(5, near=0)
    assert s >= 5 and e - s == 5

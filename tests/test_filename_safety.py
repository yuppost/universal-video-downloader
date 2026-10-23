from pathlib import Path

import pytest

from app.utils.paths import (finalize_file, is_within, render_filename_template, sanitize_filename, unique_path)
from app.utils.logging_config import redact


@pytest.mark.parametrize("raw,expected", [
    ('a<b>c:d"e/f\\g|h?i*j', "a_b_c_d_e_f_g_h_i_j"),
    ("  trailing dots...  ", "trailing dots"),
    ("", "clip"), ("..", "clip"), (".", "clip"),
    ("CON", "_CON"), ("nul.txt", "_nul.txt"), ("com1", "_com1"),
    ("../../etc/passwd", "_.._etc_passwd"),
])
def test_sanitize(raw, expected):
    assert sanitize_filename(raw) == expected


def test_sanitize_never_contains_separators_or_controls():
    out = sanitize_filename("a/b\\c\x00\x1fd‮e")
    assert not any(ch in out for ch in '/\\:\x00\x1f‮')


def test_length_limit():
    assert len(sanitize_filename("x" * 500)) <= 120


def test_unique_path_and_no_overwrite(tmp_path):
    (tmp_path / "clip.mp4").write_bytes(b"1")
    p = unique_path(tmp_path, "clip", "mp4")
    assert p.name == "clip (2).mp4"
    (tmp_path / "clip (2).mp4").write_bytes(b"2")
    assert unique_path(tmp_path, "clip", "mp4").name == "clip (3).mp4"
    # reserved names count as taken (parallel exports)
    assert unique_path(tmp_path, "new", "mp4", reserved=[tmp_path / "NEW.mp4"]).name == "new (2).mp4"


def test_finalize_never_overwrites(tmp_path):
    out = tmp_path / "out"
    names = []
    for i in range(3):
        tmp = tmp_path / f"t{i}.part"
        tmp.write_bytes(str(i).encode())
        names.append(finalize_file(tmp, out, "clip", "mp4").name)
    assert names == ["clip.mp4", "clip (2).mp4", "clip (3).mp4"]
    assert (out / "clip.mp4").read_bytes() == b"0"


def test_template_rendering_is_safe():
    v = {"title": "My Video", "index": 3, "start": 15, "end": 30}
    assert render_filename_template("{title}_clip_{index:02d}", v) == "My Video_clip_03"
    assert render_filename_template("{index}-{start}-{end}", v) == "3-15-30"
    # unknown tokens / attribute access are left as literal text, never evaluated
    out = render_filename_template("{title.__class__}{evil}", v)
    assert "class" in out and "<" not in out
    t = render_filename_template("../{title}", v)
    assert t == "_My Video" and not t.startswith(".") and "/" not in t


def test_is_within(tmp_path):
    assert is_within(tmp_path, tmp_path / "a" / "b")
    assert not is_within(tmp_path, tmp_path / ".." / "x")


def test_log_redaction():
    t = redact("GET https://site.com/watch?v=1&token=SECRET#frag cookie=abc123 password: hunter2")
    assert "SECRET" not in t and "abc123" not in t and "hunter2" not in t
    assert "https://site.com/watch" in t

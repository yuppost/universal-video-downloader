import pytest

from app.utils.validation import (UrlValidationError, format_timestamp, looks_like_playlist, parse_timestamp,
                                  site_name, validate_url)


@pytest.mark.parametrize("u", [
    "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
    "http://youtu.be/abc",
    "https://www.facebook.com/reel/123456789",
    "https://fb.watch/xyz/",
    "  https://example.com/v.mp4  ",
])
def test_accepts(u):
    assert validate_url(u).startswith("http")


@pytest.mark.parametrize("u", [
    "", "   ", None, "javascript:alert(1)", "file:///etc/passwd", "ftp://example.com/a", "not a url",
    "https://exa mple.com", "https://user:pw@example.com/x", "https://localhost/x", "https://a.com/\nrm -rf /",
    "https://" + "a" * 3000 + ".com", "https://example.com/x;rm -rf /\x00",
])
def test_rejects(u):
    with pytest.raises(UrlValidationError):
        validate_url(u)


def test_scheme_added_for_bare_domain():
    assert validate_url("youtube.com/watch?v=1") == "https://youtube.com/watch?v=1"


def test_shell_metacharacters_stay_data():
    # '&', ';', '|' inside a query string are legal URL characters; they are never given to a shell.
    assert validate_url("https://example.com/watch?v=1&t=5;x|y") .endswith("x|y")


def test_playlist_detection_and_site():
    assert looks_like_playlist("https://www.youtube.com/playlist?list=PL123")
    assert looks_like_playlist("https://www.youtube.com/watch?v=a&list=PL1")
    assert not looks_like_playlist("https://www.youtube.com/watch?v=a")
    assert site_name("https://www.youtube.com/x") == "youtube.com"


def test_timestamps_roundtrip():
    assert format_timestamp(75) == "01:15"
    assert format_timestamp(3725.5, with_ms=True) == "1:02:05.500"
    assert format_timestamp(None) == "--:--"
    assert parse_timestamp("1:15") == 75
    assert parse_timestamp("01:02:05.5") == 3725.5
    for bad in ("", "abc", "1:2:3:4", "-5", "1:xx"):
        with pytest.raises(ValueError):
            parse_timestamp(bad)

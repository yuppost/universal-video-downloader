"""App update check (local server, no real network)."""
import http.server
import json
import threading

import pytest

from app.services import update_service as u


def test_versions_and_sources():
    assert u.is_newer("1.1.0", "1.0.0") and u.is_newer("1.0.10", "1.0.9") and u.is_newer("2", "1.9.9")
    assert not u.is_newer("1.0.0", "1.0.0") and not u.is_newer("1.0", "1.0.0") and not u.is_newer("0.9", "1.0.0")
    assert u.resolve_source("drive:ABCDEFGHIJKLMNOP") == "https://drive.google.com/uc?export=download&id=ABCDEFGHIJKLMNOP"
    assert u.resolve_source("https://drive.google.com/file/d/1AbCdEfGhIjKlMn/view?usp=sharing").endswith("id=1AbCdEfGhIjKlMn")
    assert u.resolve_source("https://example.com/latest.json") == "https://example.com/latest.json"
    assert u.resolve_source("") == ""


def test_parse_info_rejects_bad_files():
    ok = json.dumps({"version": "1.2.0", "url": "https://x.example/app.zip", "notes": "hi"}).encode()
    assert u.parse_info(ok).version == "1.2.0"
    assert u.parse_info(b"<html>virus scan warning</html>") is None
    assert u.parse_info(json.dumps({"version": "1.2.0", "url": "http://x/app.zip"}).encode()) is None       # https only
    assert u.parse_info(json.dumps({"version": "latest", "url": "https://x/a"}).encode()) is None
    assert u.parse_info(json.dumps({"version": "1.2.0", "url": "javascript:alert(1)"}).encode()) is None


def test_fetch_requires_https_and_never_raises():
    assert u.fetch_update("http://127.0.0.1:1/latest.json", "1.0.0") is None
    assert u.fetch_update("https://127.0.0.1:1/latest.json", "1.0.0", timeout=1) is None
    assert u.fetch_update("", "1.0.0") is None


def test_github_latest_release_answer():
    gh = json.dumps({"tag_name": "v1.2.0", "html_url": "https://github.com/o/r/releases/tag/v1.2.0",
                     "body": "Fixes", "draft": False, "prerelease": False}).encode()
    i = u.parse_info(gh)
    assert i and i.version == "1.2.0" and i.url.startswith("https://github.com/") and i.notes == "Fixes"
    assert u.parse_info(json.dumps({"tag_name": "v9.0.0", "html_url": "https://x/y", "prerelease": True}).encode()) is None
    assert u.resolve_source("github:yuppost/universal-video-downloader") == \
        "https://api.github.com/repos/yuppost/universal-video-downloader/releases/latest"

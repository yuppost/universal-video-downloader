"""Updating yt-dlp inside a packaged build (local fake PyPI, no real network)."""
import hashlib
import http.server
import json
import subprocess
import sys
import textwrap
import threading
import zipfile

import pytest

from app.services import ytdlp_override as ov
from app.services.errors import FriendlyError


@pytest.fixture
def pypi(tmp_path):
    www = tmp_path / "www"
    www.mkdir()

    class H(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **k):
            super().__init__(*a, directory=str(www), **k)

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield www, f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def publish(www, base, version="2099.1.1", sha=None, evil=False):
    whl = www / "yt_dlp-x-py3-none-any.whl"
    with zipfile.ZipFile(whl, "w") as z:
        z.writestr("yt_dlp/__init__.py", "MARK = 'override'\n")
        z.writestr("yt_dlp/version.py", f"__version__ = '{version}'\n")
        z.writestr("yt_dlp-x.dist-info/METADATA", "x")
        if evil:
            z.writestr("yt_dlp/../../evil.txt", "x")
    digest = sha or hashlib.sha256(whl.read_bytes()).hexdigest()
    (www / "meta.json").write_text(json.dumps({
        "info": {"version": version},
        "urls": [{"packagetype": "bdist_wheel", "filename": whl.name, "url": f"{base}/{whl.name}",
                  "digests": {"sha256": digest}}]}))
    return f"{base}/meta.json"


def test_update_installs_and_import_hook_uses_it(pypi, tmp_path):
    www, base = pypi
    root = tmp_path / "tools" / "ytdlp"
    msg = ov.update_packaged(meta_url=publish(www, base), root=root, allow_http=True)
    assert "2099.1.1" in msg and ov.override_version(root) == "2099.1.1"
    assert not (tmp_path / "tools" / "download_ytdlp").exists()
    code = textwrap.dedent(f"""
        import sys
        sys.path.insert(0, {str(__import__('pathlib').Path(__file__).resolve().parents[1])!r})
        from app.services import ytdlp_override as ov
        from pathlib import Path
        assert ov.install_finder(force=True, root=Path({str(root)!r}))
        import yt_dlp
        print(yt_dlp.MARK)
    """)
    out = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True)
    assert out.stdout.strip() == "override", out.stderr


def test_already_up_to_date_and_bad_checksum(pypi, tmp_path):
    www, base = pypi
    root = tmp_path / "t" / "ytdlp"
    ov.update_packaged(meta_url=publish(www, base), root=root, allow_http=True)
    assert "up to date" in ov.update_packaged(meta_url=publish(www, base), root=root, allow_http=True)
    root2 = tmp_path / "t2" / "ytdlp"
    with pytest.raises(FriendlyError) as e:
        ov.update_packaged(meta_url=publish(www, base, sha="0" * 64), root=root2, allow_http=True)
    assert "integrity" in e.value.message and not root2.exists()


def test_path_traversal_member_is_ignored(pypi, tmp_path):
    www, base = pypi
    root = tmp_path / "t" / "ytdlp"
    ov.update_packaged(meta_url=publish(www, base, evil=True), root=root, allow_http=True)
    assert not (tmp_path / "evil.txt").exists() and not (tmp_path / "t" / "evil.txt").exists()


def test_not_active_in_development():
    assert ov.install_finder() is False

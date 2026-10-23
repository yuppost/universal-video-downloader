import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("UVS_NO_AUDIO", "1")

FFMPEG = shutil.which("ffmpeg")
needs_ffmpeg = pytest.mark.skipif(not FFMPEG, reason="ffmpeg not installed")


@pytest.fixture(scope="session")
def synthetic_video(tmp_path_factory) -> Path:
    """20 s 640x360 H.264/AAC test video with a keyframe every 2 s (generated locally)."""
    if not FFMPEG:
        pytest.skip("ffmpeg not installed")
    p = tmp_path_factory.mktemp("media") / "synthetic.mp4"
    subprocess.run([FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=duration=20:size=640x360:rate=25",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=20", "-c:v", "libx264", "-g", "50",
                    "-pix_fmt", "yuv420p", "-c:a", "aac", str(p)], check=True)
    return p


@pytest.fixture
def ffmpeg_path() -> str:
    return FFMPEG or ""


# Qt objects (thread pools, media backend) can crash the interpreter while it is shutting down after the
# tests have already finished. Exit immediately with pytest's real status once the report is printed.
_status = {"code": 0}


def pytest_sessionfinish(session, exitstatus):
    _status["code"] = int(exitstatus)


@pytest.hookimpl(trylast=True)
def pytest_unconfigure(config):
    import sys
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(_status["code"])

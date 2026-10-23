"""Qt-level tests (offscreen): timeline interaction and the job controller with real workers."""
import shutil
import time
from pathlib import Path

import pytest
from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from app.models.clip import ClipCollection, OutputSettings
from app.models.crop import CropSettings
from app.models.download_job import DownloadJob, JobKind, JobStatus
from app.services.export_service import ClipSpec
from app.widgets.timeline import TimelineWidget, assign_lanes, choose_tick_step
from app.workers.job_controller import JobController
from tests.conftest import needs_ffmpeg


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def drag(canvas, x0, x1, y):
    QTest.mousePress(canvas, Qt.LeftButton, Qt.NoModifier, QPoint(int(x0), int(y)))
    QTest.mouseMove(canvas, QPoint(int(x1), int(y)))
    QTest.mouseRelease(canvas, Qt.LeftButton, Qt.NoModifier, QPoint(int(x1), int(y)))


def make_timeline(qapp):
    clips = ClipCollection("v", 100.0)
    tl = TimelineWidget(clips)
    tl.resize(1000, 220)
    tl.show()
    tl.set_duration(100.0)
    qapp.processEvents()
    return clips, tl


def test_handles_drag_and_move(qapp):
    """Distances are computed from the real pixels-per-second, so the test is independent of window size / DPI."""
    clips, tl = make_timeline(qapp)
    c = tl.canvas
    pps = c.pps
    tol = 8.0 / pps                                         # snapping tolerance is 7 px
    a = clips.add(20, 40)
    qapp.processEvents()
    r = c.clip_rect(a)
    y = r.center().y()
    drag(c, r.left() + 2, r.left() + 2 + 50, y)           # left handle +50 px
    assert abs(a.start - (20 + 50 / pps)) < tol and a.end == 40
    r = c.clip_rect(a)
    drag(c, r.right() - 2, r.right() - 2 + 100, y)        # right handle +100 px
    assert abs(a.end - (40 + 100 / pps)) < tol
    old = (a.start, a.end)
    r = c.clip_rect(a)
    drag(c, r.center().x(), r.center().x() + 60, y)       # move body +60 px
    assert abs((a.start - old[0]) - 60 / pps) < tol and abs((a.end - a.start) - (old[1] - old[0])) < 1e-6


def test_handle_cannot_cross_and_stays_in_video(qapp):
    clips, tl = make_timeline(qapp)
    c = tl.canvas
    a = clips.add(20, 25)
    r = c.clip_rect(a)
    drag(c, r.left() + 2, r.left() + 600, r.center().y())   # drag start far past end
    assert a.start < a.end and a.duration >= 0.5 - 1e-9
    clips.move_to(a.id, 500)
    assert a.end <= 100.0


def test_click_seeks_and_selection(qapp):
    clips, tl = make_timeline(qapp)
    a = clips.add(20, 30)
    seeks = []
    tl.seekRequested.connect(seeks.append)
    x = tl.canvas.width() - 40                              # empty spot near the right edge of the ruler area
    QTest.mouseClick(tl.canvas, Qt.LeftButton, Qt.NoModifier, QPoint(x, 30))
    assert seeks and abs(seeks[-1] - tl.canvas.x2t(x)) < 0.05
    r = tl.canvas.clip_rect(a)
    QTest.mouseClick(tl.canvas, Qt.LeftButton, Qt.NoModifier, QPoint(int(r.center().x()), int(r.center().y())))
    assert tl.canvas.selected == a.id


def test_zoom_and_scroll(qapp):
    clips, tl = make_timeline(qapp)
    c = tl.canvas
    base = c.pps
    tl.btn_in.click(); tl.btn_in.click()
    assert c.pps > base * 2 and tl.scroll.maximum() > 0
    c.set_view_start(30)
    assert 29 < c.view_start <= 30
    tl.btn_fit.click()
    assert abs(c.pps - base) < 1e-6 and c.view_start == 0
    for _ in range(40):
        tl.btn_in.click()
    assert c.pps <= 400.0
    tl.btn_fit.click()
    for _ in range(10):
        tl.btn_out.click()
    assert c.pps >= c.min_pps() - 1e-9


def test_many_clips_overlap_lanes_and_ticks(qapp):
    clips = ClipCollection("v", 3600.0)
    for i in range(300):
        clips.add(i * 10, i * 10 + 8)
    clips.add(0, 20)
    lanes = assign_lanes(clips.clips)
    assert max(lanes.values()) >= 1
    # no two clips sharing a lane overlap
    by = {}
    for c in clips.clips:
        by.setdefault(lanes[c.id], []).append(c)
    for L in by.values():
        L.sort(key=lambda c: c.start)
        assert all(L[i].end <= L[i + 1].start + 1e-9 for i in range(len(L) - 1))
    assert choose_tick_step(1.0) >= 60 and choose_tick_step(400) <= 0.5
    t0 = time.time()
    tl = TimelineWidget(clips); tl.resize(1200, 300); tl.show(); tl.set_duration(3600); tl.grab()
    assert time.time() - t0 < 5


def _spec(tmp, name, ffmpeg, src_h=360, **kw):
    return ClipSpec("c_" + name, name, 2, 6, kw.get("crop", CropSettings()), OutputSettings(), 640, src_h,
                    tmp / "out", name, "local", "u", "s", ffmpeg, tmp / "t")


def wait(qapp, ctl, timeout=60):
    t0 = time.time()
    while time.time() - t0 < timeout and any(not j.is_finished for j in ctl.queue.jobs):
        qapp.processEvents()
        time.sleep(0.02)


@needs_ffmpeg
def test_controller_runs_jobs_with_shared_source_and_partial_failure(qapp, synthetic_video, tmp_path, ffmpeg_path):
    ctl = JobController(2)
    src = DownloadJob(JobKind.SOURCE, "src", payload=type("S", (), {"key": "k"})())
    src.status = JobStatus.COMPLETED
    src.output_path = synthetic_video
    ctl.queue.add(src)
    good = [DownloadJob(JobKind.CLIP, f"good{i}", payload=_spec(tmp_path, f"good{i}", ffmpeg_path),
                        depends_on=src.id, clip_id=f"c{i}") for i in range(2)]
    bad = DownloadJob(JobKind.CLIP, "bad", payload=_spec(tmp_path, "bad", "/nonexistent/ffmpeg"), depends_on=src.id)
    done = []
    ctl.clip_completed.connect(done.append)
    for j in (good[0], bad, good[1]):
        ctl.add(j)
    wait(qapp, ctl)
    assert [j.status for j in good] == [JobStatus.COMPLETED] * 2
    assert bad.status == JobStatus.FAILED and bad.error
    assert all(Path(j.output_path).exists() for j in good) and len(done) == 2
    # retry only the failed job after fixing its spec
    bad.payload = _spec(tmp_path, "bad", ffmpeg_path)
    ctl.retry(bad.id)
    wait(qapp, ctl)
    assert bad.status == JobStatus.COMPLETED and good[0].attempts == 0 and bad.attempts == 1
    ctl.shutdown()


@needs_ffmpeg
def test_controller_cancel_and_dependency_failure(qapp, tmp_path, ffmpeg_path):
    ctl = JobController(1)
    src = DownloadJob(JobKind.SOURCE, "src")
    src.status = JobStatus.FAILED
    ctl.queue.add(src)
    clip = DownloadJob(JobKind.CLIP, "c", payload=_spec(tmp_path, "c", ffmpeg_path), depends_on=src.id)
    ctl.add(clip)
    qapp.processEvents()
    assert clip.status == JobStatus.FAILED and "source" in clip.error.lower()
    waiting = DownloadJob(JobKind.CLIP, "w", payload=_spec(tmp_path, "w", ffmpeg_path))
    ctl.set_paused(True)
    ctl.add(waiting)
    assert waiting.status == JobStatus.WAITING
    ctl.cancel(waiting.id)
    assert waiting.status == JobStatus.CANCELED
    ctl.shutdown()


@needs_ffmpeg
def test_download_full_video_button(qapp, synthetic_video, tmp_path, monkeypatch):
    """'Download full video' exports the whole video once, uncropped, without touching the clip list."""
    import shutil
    from app.config import Settings
    from app.main_window import MainWindow
    from app.services import ytdlp_service as yd
    from app.services.ffmpeg_service import find_executable
    from app.services.media_info_service import probe
    s = Settings(temp_dir=str(tmp_path / "t"), download_dir=str(tmp_path / "o"), ffmpeg_path=find_executable("ffmpeg"))
    win = MainWindow(s)
    w = win.current()                                     # the first tab holds everything about one video
    url = "https://example.com/v"
    w._on_analyzed(yd.VideoInfo(url, "vid", "My Video", "", 20.0, "", "G",
                                [yd.FormatInfo("1", "mp4", 360, 640, 25, "avc1", "aac", None, None)]))
    key = yd.source_key(url, yd.build_format_selector(None))
    s.cache_dir.mkdir(parents=True)
    shutil.copy(synthetic_video, s.cache_dir / f"{key}.mp4")
    assert w.btn_full.isEnabled() and len(w.clips) == 0
    w.out_dir.setText(str(tmp_path / "o"))
    w.export_clips(False, full=True)
    wait(qapp, win.controller)
    jobs = [j for j in win.controller.queue.jobs if j.kind == JobKind.CLIP]
    assert len(jobs) == 1 and jobs[0].status == JobStatus.COMPLETED, jobs[0].error
    out = Path(jobs[0].output_path)
    assert out.name == "My Video_full.mp4" and len(w.clips) == 0
    assert abs(probe(out).duration - 20) < 0.6 and probe(out).width == 640
    win.controller.shutdown()

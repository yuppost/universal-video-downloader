"""Timeline: exact add (double/right click), rubber-band multi-select, group actions."""
import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMenu

from app.models.clip import ClipCollection
from app.widgets import timeline as tlmod
from app.widgets.timeline import TimelineWidget


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def make(qapp, dur=100.0):
    clips = ClipCollection("v", dur)
    tl = TimelineWidget(clips)
    tl.resize(1000, 260)
    tl.show()
    tl.set_duration(dur)
    qapp.processEvents()
    return clips, tl


def lane_y(canvas, lane):
    return canvas._lane_top() + tlmod.LANE_GAP + lane * (tlmod.LANE_H + tlmod.LANE_GAP) + tlmod.LANE_H / 2


def px(canvas, t):
    return int(canvas.t2x(t))


def test_double_click_below_an_existing_clip_adds_exactly_there(qapp):
    clips, tl = make(qapp)
    clips.add(10, 20)
    c = tl.canvas
    got = []
    tl.addClipAt.connect(got.append)
    QTest.mouseDClick(c, Qt.LeftButton, Qt.NoModifier, QPoint(px(c, 15), int(lane_y(c, 1))))   # empty lane under the clip
    assert len(got) == 1 and abs(got[0] - 15) < 0.2
    QTest.mouseDClick(c, Qt.LeftButton, Qt.NoModifier, QPoint(px(c, 15), 5))                     # ruler: ignored
    QTest.mouseDClick(c, Qt.LeftButton, Qt.NoModifier, QPoint(px(c, 15), int(lane_y(c, 0))))     # on the clip: ignored
    assert len(got) == 1


def test_workspace_exact_add_overlaps_instead_of_jumping_away(qapp, tmp_path, monkeypatch):
    import app.main_window as mw
    from app.config import Settings
    from app.services import ytdlp_service as yd
    monkeypatch.setattr(mw.QTimer, "singleShot", staticmethod(lambda *a, **k: None))
    monkeypatch.setenv("UVS_DATA_DIR", str(tmp_path))
    win = mw.MainWindow(Settings(temp_dir=str(tmp_path / "t"), download_dir=str(tmp_path / "o")))
    ws = win.current()
    ws._on_analyzed(yd.VideoInfo("https://e.com/v", "i", "T", "", 100.0, "", "G",
                                 [yd.FormatInfo("1", "mp4", 360, 640, 25, "avc1", "aac", None, None)]))
    first = ws.clips.add(10, 40)
    ws.add_clip_at(20, exact=True)                       # inside the existing clip -> must not be moved to a gap
    second = ws.clips.clips[-1]
    assert (second.start, second.end) == (20, 25)
    lanes = tlmod.assign_lanes(ws.clips.clips)
    assert lanes[first.id] == 0 and lanes[second.id] == 1
    ws.add_clip_at(98, exact=True, length=10)            # near the end: clamped inside the video, full length kept
    last = ws.clips.clips[-1]
    assert last.end == 100.0 and last.duration == 10.0
    ws.add_clip_at(50)                                   # the Add Clip button keeps using free gaps
    assert ws.clips.clips[-1].start >= 40
    win.controller.shutdown()


def test_right_click_empty_space_offers_clip_lengths(qapp, monkeypatch):
    clips, tl = make(qapp)
    c = tl.canvas
    spans = []
    tl.addClipSpan.connect(lambda t, n: spans.append((round(t), n)))
    monkeypatch.setattr(c, "exec_menu", lambda menu, pos: menu.actions()[1])      # "Add a 10 seconds clip here"
    QTest.mouseClick(c, Qt.RightButton, Qt.NoModifier, QPoint(px(c, 30), int(lane_y(c, 1))))
    assert spans == [(30, 10.0)]
    menu, acts = tlmod.build_empty_menu(c, have_clips=False)
    assert [a.text() for a in menu.actions() if a.text()][:4] == [f"Add a {l} clip here" for _s, l in tlmod.ADD_LENGTHS]
    assert not any(v[0] == "all" for v in acts.values())


def test_right_click_on_clip_has_add_here(qapp, monkeypatch):
    clips, tl = make(qapp)
    clips.add(10, 20)
    c = tl.canvas
    spans = []
    tl.addClipSpan.connect(lambda t, n: spans.append((round(t), n)))
    monkeypatch.setattr(c, "exec_menu", lambda menu, pos: menu.actions()[0])
    QTest.mouseClick(c, Qt.RightButton, Qt.NoModifier, QPoint(px(c, 15), int(lane_y(c, 0))))
    assert spans == [(15, 5.0)]


def band(canvas, p0, p1, mods=Qt.NoModifier):
    QTest.mousePress(canvas, Qt.LeftButton, mods, p0)
    QTest.mouseMove(canvas, QPoint((p0.x() + p1.x()) // 2, (p0.y() + p1.y()) // 2))
    QTest.mouseMove(canvas, p1)
    QTest.mouseRelease(canvas, Qt.LeftButton, mods, p1)


def test_rubber_band_selects_intersecting_clips(qapp):
    clips, tl = make(qapp)
    a, b, c3 = clips.add(10, 20), clips.add(30, 40), clips.add(60, 70)
    cv = tl.canvas
    sets = []
    tl.selectionSetChanged.connect(sets.append)
    y_top, y_bot = int(lane_y(cv, 0)) - 25, int(lane_y(cv, 0)) + 25
    band(cv, QPoint(px(cv, 5), y_bot), QPoint(px(cv, 45), y_top))
    assert set(cv.selected_ids) == {a.id, b.id} and cv.selected in (a.id, b.id)
    assert sets and set(sets[-1]) == {a.id, b.id}
    band(cv, QPoint(px(cv, 55), y_bot), QPoint(px(cv, 80), y_top), Qt.ControlModifier)      # Ctrl adds
    assert set(cv.selected_ids) == {a.id, b.id, c3.id}
    QTest.mouseClick(cv, Qt.LeftButton, Qt.ControlModifier, QPoint(px(cv, 35), int(lane_y(cv, 0))))   # Ctrl-click removes
    assert set(cv.selected_ids) == {a.id, c3.id}
    QTest.mouseClick(cv, Qt.LeftButton, Qt.NoModifier, QPoint(px(cv, 90), int(lane_y(cv, 0))))        # click empty: clear + seek
    assert cv.selected_ids == [] and cv.selected is None


def test_band_that_misses_everything_clears_and_tiny_drag_is_a_click(qapp):
    clips, tl = make(qapp)
    a = clips.add(10, 20)
    cv = tl.canvas
    tl.select(a.id)
    seeks = []
    tl.seekRequested.connect(seeks.append)
    p = QPoint(px(cv, 50), int(lane_y(cv, 0)))
    QTest.mousePress(cv, Qt.LeftButton, Qt.NoModifier, p)
    QTest.mouseMove(cv, p + QPoint(2, 1))
    QTest.mouseRelease(cv, Qt.LeftButton, Qt.NoModifier, p + QPoint(2, 1))
    assert cv.selected_ids == [] and len(seeks) == 1 and abs(seeks[0] - 50) < 1
    band(cv, QPoint(px(cv, 60), int(lane_y(cv, 0)) - 20), QPoint(px(cv, 80), int(lane_y(cv, 0)) + 20))
    assert cv.selected_ids == []


def test_ctrl_a_escape_and_group_move(qapp):
    clips, tl = make(qapp)
    a, b = clips.add(10, 20), clips.add(30, 40)
    cv = tl.canvas
    cv.setFocus()
    QTest.keyClick(cv, Qt.Key_A, Qt.ControlModifier)
    assert set(cv.selected_ids) == {a.id, b.id}
    y = int(lane_y(cv, 0))
    QTest.mousePress(cv, Qt.LeftButton, Qt.NoModifier, QPoint(px(cv, 15), y))                  # drag one of the selected
    QTest.mouseMove(cv, QPoint(px(cv, 25), y))
    QTest.mouseRelease(cv, Qt.LeftButton, Qt.NoModifier, QPoint(px(cv, 25), y))
    assert set(cv.selected_ids) == {a.id, b.id}                                                # selection kept
    assert abs(clips.get(a.id).start - 20) < 1.5 and abs(clips.get(b.id).start - 40) < 1.5     # both moved together
    assert abs(clips.get(b.id).start - clips.get(a.id).start - 20) < 1e-6                      # same spacing
    QTest.keyClick(cv, Qt.Key_Escape)
    assert cv.selected_ids == []


def test_group_move_stops_at_video_bounds(qapp):
    clips, tl = make(qapp)
    a, b = clips.add(10, 20), clips.add(80, 90)
    cv = tl.canvas
    cv.select_all()
    y = int(lane_y(cv, 0))
    QTest.mousePress(cv, Qt.LeftButton, Qt.NoModifier, QPoint(px(cv, 15), y))
    QTest.mouseMove(cv, QPoint(px(cv, 60), y))                                                  # far right
    QTest.mouseRelease(cv, Qt.LeftButton, Qt.NoModifier, QPoint(px(cv, 60), y))
    assert clips.get(b.id).end <= 100.0 + 1e-9 and abs(clips.get(b.id).start - clips.get(a.id).start - 70) < 1e-6


def test_group_delete_duplicate_and_export_selection_in_workspace(qapp, tmp_path, monkeypatch):
    import app.main_window as mw
    from app.config import Settings
    from app.services import ytdlp_service as yd
    monkeypatch.setattr(mw.QTimer, "singleShot", staticmethod(lambda *a, **k: None))
    monkeypatch.setenv("UVS_DATA_DIR", str(tmp_path))
    win = mw.MainWindow(Settings(temp_dir=str(tmp_path / "t"), download_dir=str(tmp_path / "o")))
    ws = win.current()
    ws._on_analyzed(yd.VideoInfo("https://e.com/v", "i", "T", "", 100.0, "", "G",
                                 [yd.FormatInfo("1", "mp4", 360, 640, 25, "avc1", "aac", None, None)]))
    a, b, c = ws.clips.add(5, 10), ws.clips.add(20, 25), ws.clips.add(40, 45)
    ws.timeline.canvas.select_many([a.id, b.id])
    assert "2 clips selected" in ws.sel_label.text() and ws.btn_sel.text() == "Selected (2)"
    ws._clip_action("duplicate", ws.selected_id())
    assert len(ws.clips) == 5 and len(ws.selected_ids()) == 2
    new_ids = set(ws.selected_ids())
    ws._clip_action("delete", ws.selected_id())
    assert len(ws.clips) == 3 and not (new_ids & {c.id for c in ws.clips.clips})
    ws.timeline.canvas.select_many([a.id, c.id])
    ws._clip_action("delete", a.id)                                 # deletes the whole selection
    assert [x.id for x in ws.clips.clips] == [b.id]
    win.controller.shutdown()

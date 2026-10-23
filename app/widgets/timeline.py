"""Interactive multi-clip timeline: ruler, thumbnails, playhead, zoom, scroll, trim handles."""

from __future__ import annotations

import bisect
import math
from typing import Dict, List, Optional, Tuple

from PySide6.QtCore import QPoint, QRectF, Qt, Signal
from PySide6.QtGui import (QColor, QFont, QFontMetrics, QKeyEvent, QMouseEvent, QPainter, QPainterPath, QPen,
                           QPixmap, QWheelEvent)
from PySide6.QtWidgets import QHBoxLayout, QLabel, QMenu, QScrollBar, QSizePolicy, QToolButton, QVBoxLayout, QWidget

from app import theme
from app.models.clip import MIN_CLIP_DURATION, Clip, ClipCollection, ClipError
from app.utils.validation import format_timestamp

RULER_H, STRIP_H, LANE_H, LANE_GAP, HANDLE_W, SNAP_PX = 24, 40, 38, 5, 9, 7
NICE_STEPS = [0.1, 0.25, 0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200]
MAX_PPS = 400.0

# Context-menu actions emitted through ``clipActionRequested``
ACTIONS = [("preview", "Preview clip"), ("rename", "Rename..."), ("duplicate", "Duplicate"),
           ("reset", "Reset range"), ("delete", "Delete")]


ADD_LENGTHS = [(5.0, "5 seconds"), (10.0, "10 seconds"), (30.0, "30 seconds"), (60.0, "1 minute")]


def build_clip_menu(parent: QWidget, multi: int = 0) -> Tuple[QMenu, Dict[str, object]]:
    """Context menu of a clip. With several clips selected only the actions that make sense for a group stay."""
    menu = QMenu(parent)
    acts = {}
    acts["add_here"] = menu.addAction("Add a new clip here")
    menu.addSeparator()
    for key, label in ACTIONS:
        if multi > 1 and key in ("preview", "rename", "reset"):
            continue
        if multi > 1 and key in ("duplicate", "delete"):
            label = f"{label} {multi} selected clips"
        if key == "delete":
            menu.addSeparator()
        acts[key] = menu.addAction(label)
    return menu, acts


def build_empty_menu(parent: QWidget, have_clips: bool) -> Tuple[QMenu, Dict[object, Tuple[str, float]]]:
    """Context menu of an empty timeline spot: add a clip of a chosen length right there."""
    menu = QMenu(parent)
    acts: Dict[object, Tuple[str, float]] = {}
    for length, label in ADD_LENGTHS:
        acts[menu.addAction(f"Add a {label} clip here")] = ("add", length)
    menu.addSeparator()
    acts[menu.addAction("Move playhead here")] = ("seek", 0.0)
    if have_clips:
        acts[menu.addAction("Select all clips (Ctrl+A)")] = ("all", 0.0)
    return menu, acts


def assign_lanes(clips: List[Clip]) -> Dict[str, int]:
    """Greedy lane assignment so overlapping clips are drawn on separate rows."""
    lanes: List[float] = []
    out: Dict[str, int] = {}
    for c in sorted(clips, key=lambda c: (c.start, c.end)):
        for i, end in enumerate(lanes):
            if c.start >= end - 1e-9:
                lanes[i] = c.end
                out[c.id] = i
                break
        else:
            lanes.append(c.end)
            out[c.id] = len(lanes) - 1
    return out


def choose_tick_step(pps: float, min_px: float = 80.0) -> float:
    for s in NICE_STEPS:
        if s * pps >= min_px:
            return s
    return NICE_STEPS[-1]


class TimelineCanvas(QWidget):
    """Paints and handles mouse interaction; owned by :class:`TimelineWidget`."""

    seekRequested = Signal(float)
    selectionChanged = Signal(object)       # clip id or None
    clipActionRequested = Signal(str, str)  # action, clip id
    addClipAt = Signal(float)               # exact time (double-click)
    addClipSpan = Signal(float, float)      # exact time + length (context menu)
    selectionSetChanged = Signal(list)      # every selected clip id (marquee / Ctrl-click)
    handleDragged = Signal(float)           # time under the handle being dragged
    viewChanged = Signal()
    dragFinished = Signal(str)              # clip id whose range was edited

    def __init__(self, clips: ClipCollection, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.clips = clips
        self.duration = 0.0
        self.strip_h = STRIP_H
        self.pps = 20.0             # pixels per second
        self.view_start = 0.0       # seconds at the left edge
        self.playhead = 0.0
        self.selected: Optional[str] = None        # the "main" selected clip (crop panel, preview, list)
        self.selected_ids: List[str] = []          # all selected clips (marquee / Ctrl-click)
        self._band: Optional[Tuple[QPoint, QPoint, bool]] = None   # marquee: origin, current, active
        self._band_base: List[str] = []
        self._press_t = 0.0
        self._group: Dict[str, Tuple[float, float]] = {}
        self.thumbs: Dict[int, Tuple[float, QPixmap]] = {}
        self.thumb_slot = 0.0       # seconds per thumbnail slot
        self._drag: Optional[Tuple[str, str, float, float, float]] = None  # (mode, clip id, anchor t, start0, end0)
        self._scrub = False
        self._lanes: Dict[str, int] = {}
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setMinimumHeight(RULER_H + self.strip_h + LANE_H + 2 * LANE_GAP)
        clips.subscribe(self._on_clips_changed)

    # --- coordinate helpers --------------------------------------------
    def t2x(self, t: float) -> float:
        return (t - self.view_start) * self.pps

    def x2t(self, x: float) -> float:
        return self.view_start + x / self.pps

    def min_pps(self) -> float:
        return max(0.01, (self.width() - 2) / self.duration) if self.duration > 0 else 1.0

    def visible_seconds(self) -> float:
        return self.width() / self.pps

    def clamp_view(self) -> None:
        max_start = max(0.0, self.duration - self.visible_seconds())
        self.view_start = min(max(0.0, self.view_start), max_start)

    def set_duration(self, d: float) -> None:
        self.duration = max(0.0, d)
        self.thumbs.clear()
        self.fit()

    def fit(self) -> None:
        self.pps = self.min_pps()
        self.view_start = 0.0
        self.viewChanged.emit()
        self.update()

    def zoom(self, factor: float, anchor_x: Optional[float] = None) -> None:
        if self.duration <= 0:
            return
        ax = self.width() / 2 if anchor_x is None else anchor_x
        t_anchor = self.x2t(ax)
        self.pps = min(MAX_PPS, max(self.min_pps(), self.pps * factor))
        self.view_start = t_anchor - ax / self.pps
        self.clamp_view()
        self.viewChanged.emit()
        self.update()

    def set_view_start(self, s: float) -> None:
        self.view_start = s
        self.clamp_view()
        self.update()

    def ensure_visible(self, t: float) -> None:
        if t < self.view_start or t > self.view_start + self.visible_seconds():
            self.view_start = t - self.visible_seconds() * 0.1
            self.clamp_view()
            self.viewChanged.emit()

    def set_playhead(self, t: float) -> None:
        self.playhead = max(0.0, min(t, self.duration))
        self.update()

    def select(self, clip_id: Optional[str]) -> None:
        """Select exactly one clip (or none)."""
        self.select_many([clip_id] if clip_id else [], clip_id)

    def select_many(self, ids: List[str], primary: Optional[str] = None) -> None:
        ids = [i for i in dict.fromkeys(ids) if any(c.id == i for c in self.clips.clips)]
        if primary not in ids:
            primary = ids[-1] if ids else None
        changed_set = ids != self.selected_ids
        changed_main = primary != self.selected
        self.selected_ids, self.selected = ids, primary
        if changed_main:
            self.selectionChanged.emit(primary)
        if changed_set:
            self.selectionSetChanged.emit(list(ids))
        if changed_main or changed_set:
            self.update()

    def select_all(self) -> None:
        self.select_many([c.id for c in self.clips.clips])

    def add_thumbnail(self, index: int, t: float, pix: QPixmap, slot: float) -> None:
        self.thumbs[index] = (t, pix)
        self.thumb_slot = slot
        self.update()

    def _on_clips_changed(self) -> None:
        self._lanes = assign_lanes(self.clips.clips)
        lanes = (max(self._lanes.values()) + 1) if self._lanes else 1
        self.setMinimumHeight(RULER_H + self.strip_h + lanes * (LANE_H + LANE_GAP) + LANE_GAP)
        alive = {c.id for c in self.clips.clips}
        if any(i not in alive for i in self.selected_ids) or (self.selected and self.selected not in alive):
            self.select_many([i for i in self.selected_ids if i in alive])
        self.update()

    # --- geometry of clips -------------------------------------------------
    def clip_rect(self, c: Clip) -> QRectF:
        lane = self._lanes.get(c.id, 0)
        y = RULER_H + self.strip_h + LANE_GAP + lane * (LANE_H + LANE_GAP)
        x1, x2 = self.t2x(c.start), self.t2x(c.end)
        return QRectF(x1, y, max(2.0, x2 - x1), LANE_H)

    def hit_test(self, pos) -> Tuple[Optional[Clip], str]:
        """Return (clip, 'left'|'right'|'body') under *pos*; later clips win (drawn on top)."""
        for c in reversed(self.clips.clips):
            r = self.clip_rect(c)
            if not (r.top() <= pos.y() <= r.bottom()):
                continue
            hw = min(HANDLE_W, r.width() / 3)
            if r.left() - 3 <= pos.x() <= r.left() + hw:
                return c, "left"
            if r.right() - hw <= pos.x() <= r.right() + 3:
                return c, "right"
            if r.left() <= pos.x() <= r.right():
                return c, "body"
        return None, ""

    # --- painting ---------------------------------------------------------
    def paintEvent(self, _e) -> None:  # noqa: N802
        pal = theme.palette()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        p.fillRect(self.rect(), QColor(pal["lane"]))
        p.fillRect(0, 0, w, RULER_H, QColor(pal["ruler"]))
        if self.duration <= 0:
            p.setPen(QColor(pal["muted"]))
            p.drawText(self.rect(), Qt.AlignCenter, "Analyze a video to start selecting clips")
            return
        self._paint_strip(p, pal, w)
        self._paint_ruler(p, pal, w)
        self._paint_clips(p, pal, w)
        # video end marker
        x_end = self.t2x(self.duration)
        if 0 <= x_end <= w:
            p.fillRect(QRectF(x_end, RULER_H, w - x_end, h - RULER_H), QColor(0, 0, 0, 90))
        # playhead
        px = self.t2x(self.playhead)
        if -6 <= px <= w + 6:
            p.setPen(QPen(QColor("#ffffff"), 2))
            p.drawLine(int(px), 0, int(px), h)
            p.setBrush(QColor("#ffffff"))
            p.setPen(Qt.NoPen)
            p.drawRoundedRect(QRectF(px - 6, 0, 12, 10), 2, 2)
            p.drawPolygon([QPoint(int(px) - 6, 8), QPoint(int(px) + 6, 8), QPoint(int(px), 16)])
        if self._band and self._band[2]:                      # rubber band while selecting several clips
            r = self._band_rect()
            p.setBrush(QColor(59, 130, 246, 60))
            p.setPen(QPen(QColor(96, 165, 250), 1))
            p.drawRect(r)

    def _paint_strip(self, p: QPainter, pal, w: int) -> None:
        top = RULER_H
        p.fillRect(0, top, w, self.strip_h, QColor(pal["panel2"]))
        if not self.thumbs or self.thumb_slot <= 0:
            p.setPen(QColor(pal["muted"]))
            p.setFont(QFont(self.font().family(), 8))
            p.drawText(QRectF(6, top, w - 12, self.strip_h), Qt.AlignVCenter | Qt.AlignLeft,
                       "Thumbnails appear once video data is available locally")
            return
        for idx, (t, pix) in self.thumbs.items():
            x0 = self.t2x(t - self.thumb_slot / 2)
            x1 = self.t2x(t + self.thumb_slot / 2)
            if x1 < 0 or x0 > w:
                continue
            p.drawPixmap(QRectF(x0, top, x1 - x0 + 1, self.strip_h), pix, QRectF(pix.rect()))

    def _paint_ruler(self, p: QPainter, pal, w: int) -> None:
        step = choose_tick_step(self.pps)
        minor = step / 5 if step >= 1 else step / 2
        p.setFont(QFont(self.font().family(), 8))
        first = math.floor(self.view_start / minor) * minor
        t = first
        end_t = self.view_start + self.visible_seconds()
        pen_minor, pen_major = QPen(QColor(pal["border"])), QPen(QColor(pal["muted"]))
        i = 0
        while t <= end_t + minor:
            x = self.t2x(t)
            is_major = abs(t / step - round(t / step)) < 1e-6
            if t >= -1e-9 and t <= self.duration + 1e-9:
                if is_major:
                    p.setPen(pen_major)
                    p.drawLine(int(x), RULER_H - 12, int(x), RULER_H)
                    p.drawText(int(x) + 3, 12, format_timestamp(t, with_ms=step < 1))
                else:
                    p.setPen(pen_minor)
                    p.drawLine(int(x), RULER_H - 5, int(x), RULER_H)
            t = first + (i + 1) * minor
            i += 1

    def _paint_clips(self, p: QPainter, pal, w: int) -> None:
        fm = QFontMetrics(self.font())
        for c in self.clips.clips:
            r = self.clip_rect(c)
            if r.right() < 0 or r.left() > w:
                continue
            col = QColor(c.color)
            sel = c.id in self.selected_ids
            from PySide6.QtGui import QLinearGradient
            g = QLinearGradient(r.topLeft(), r.bottomLeft())
            top_c, bot_c = QColor(col), QColor(col).darker(135)
            top_c.setAlpha(235 if sel else 170)
            bot_c.setAlpha(235 if sel else 170)
            g.setColorAt(0, top_c)
            g.setColorAt(1, bot_c)
            p.setPen(QPen(QColor("#ffffff") if sel else col.lighter(120), 2 if sel else 1))
            p.setBrush(g)
            p.drawRoundedRect(r, 7, 7)
            self._paint_clip_thumb(p, c, r)
            # trim handles (clearly visible grips)
            for side in ("l", "r"):
                hx = r.left() if side == "l" else r.right() - HANDLE_W
                hr = QRectF(hx, r.top(), min(HANDLE_W, r.width() / 2), r.height())
                p.setPen(Qt.NoPen)
                p.setBrush(QColor("#ffffff") if sel else col.lighter(150))
                p.drawRoundedRect(hr, 3, 3)
                p.setPen(QPen(col.darker(200), 1))
                cx = hr.center().x()
                p.drawLine(int(cx), int(hr.top() + 8), int(cx), int(hr.bottom() - 8))
            # label
            label = f"{c.name}  {format_timestamp(c.duration, with_ms=c.duration < 10)}"
            used = (min(54.0, r.width() / 3) + 6) if (self.thumbs and self.thumb_slot > 0 and r.width() >= 90) else 0
            inner = r.adjusted(HANDLE_W + 3 + used, 0, -HANDLE_W - 3, 0)
            if inner.width() > 20:
                p.setPen(QColor("#0b1220") if col.lightness() > 150 else QColor("#ffffff"))
                p.drawText(inner, Qt.AlignVCenter | Qt.AlignLeft,
                           fm.elidedText(label, Qt.ElideRight, int(inner.width())))

    def _paint_clip_thumb(self, p: QPainter, c: Clip, r: QRectF) -> None:
        """Small frame from the clip's start, like the clip blocks of a video editor."""
        if not self.thumbs or self.thumb_slot <= 0 or r.width() < 90:
            return
        idx = min(self.thumbs, key=lambda i: abs(self.thumbs[i][0] - c.start))
        pix = self.thumbs[idx][1]
        box = QRectF(r.left() + HANDLE_W + 3, r.top() + 4, min(54.0, r.width() / 3), r.height() - 8)
        p.save()
        path = QPainterPath()
        path.addRoundedRect(box, 4, 4)
        p.setClipPath(path)
        p.drawPixmap(box, pix, QRectF(pix.rect()))
        p.restore()
        self._thumb_used = box.width() + 6

    # --- mouse ---------------------------------------------------------------
    def _snap(self, t: float, exclude: Optional[str], disabled: bool) -> float:
        if disabled:
            return t
        cands = [self.playhead]
        for c in self.clips.clips:
            if c.id != exclude:
                cands += [c.start, c.end]
        tol = SNAP_PX / self.pps
        best = min(cands, key=lambda v: abs(v - t))
        return best if abs(best - t) <= tol else t

    def exec_menu(self, menu: QMenu, global_pos):
        """Show a context menu and return the chosen action (a seam so tests can pick without a modal loop)."""
        return menu.exec(global_pos)

    def _lane_top(self) -> float:
        return RULER_H + self.strip_h

    def _band_rect(self) -> QRectF:
        o, c, _ = self._band
        return QRectF(QPoint(min(o.x(), c.x()), min(o.y(), c.y())), QPoint(max(o.x(), c.x()), max(o.y(), c.y())))

    def mousePressEvent(self, e: QMouseEvent) -> None:  # noqa: N802
        if self.duration <= 0:
            return
        self.setFocus()
        clip, part = self.hit_test(e.position())
        t = max(0.0, min(self.x2t(e.position().x()), self.duration))
        if e.button() == Qt.RightButton:
            if clip:
                if clip.id not in self.selected_ids:
                    self.select(clip.id)
                else:
                    self.select_many(self.selected_ids, clip.id)
                menu, acts = build_clip_menu(self, len(self.selected_ids))
                chosen = self.exec_menu(menu, e.globalPosition().toPoint())
                for k, a in acts.items():
                    if a is chosen:
                        if k == "add_here":
                            self.addClipSpan.emit(t, 5.0)
                        else:
                            self.clipActionRequested.emit(k, clip.id)
            else:
                menu, acts = build_empty_menu(self, bool(self.clips.clips))
                chosen = self.exec_menu(menu, e.globalPosition().toPoint())
                what = acts.get(chosen)
                if what is None:
                    return
                if what[0] == "add":
                    self.addClipSpan.emit(t, what[1])
                elif what[0] == "seek":
                    self.seekRequested.emit(t)
                elif what[0] == "all":
                    self.select_all()
            return
        if e.button() != Qt.LeftButton:
            return
        mods = e.modifiers()
        toggle = bool(mods & (Qt.ControlModifier | Qt.ShiftModifier))
        if clip:
            if toggle:                                            # Ctrl/Shift-click adds or removes a clip
                ids = list(self.selected_ids)
                if clip.id in ids:
                    ids.remove(clip.id)
                    self.select_many(ids)
                else:
                    self.select_many(ids + [clip.id], clip.id)
                return
            if clip.id not in self.selected_ids:
                self.select(clip.id)
            else:
                self.select_many(self.selected_ids, clip.id)
            self._drag = (part, clip.id, t, clip.start, clip.end)
            self._group = {c.id: (c.start, c.end) for c in self.clips.clips if c.id in self.selected_ids}
            return
        if e.position().y() >= self._lane_top():
            # empty lane area: a drag draws a blue selection rectangle, a plain click just moves the playhead
            self._band = (e.position().toPoint(), e.position().toPoint(), False)
            self._band_base = list(self.selected_ids) if toggle else []
            self._press_t = t
            return
        self.select(None)
        self._scrub = True
        self.seekRequested.emit(t)

    def mouseMoveEvent(self, e: QMouseEvent) -> None:  # noqa: N802
        if self.duration <= 0:
            return
        t = max(0.0, min(self.x2t(e.position().x()), self.duration))
        no_snap = bool(e.modifiers() & Qt.AltModifier)
        if self._band:
            o, _c, active = self._band
            pos = e.position().toPoint()
            if not active and (abs(pos.x() - o.x()) > 4 or abs(pos.y() - o.y()) > 4):
                active = True
            self._band = (o, pos, active)
            if active:
                r = self._band_rect()
                hit = [c.id for c in self.clips.clips if self.clip_rect(c).intersects(r)]
                self.select_many(self._band_base + [i for i in hit if i not in self._band_base])
                self.update()
            return
        if self._drag:
            mode, cid, anchor, s0, e0 = self._drag
            try:
                if mode == "left":
                    ts = self._snap(t, cid, no_snap)
                    self.clips.set_start(cid, min(ts, e0 - MIN_CLIP_DURATION))
                    self.handleDragged.emit(self.clips.get(cid).start)
                elif mode == "right":
                    te = self._snap(t, cid, no_snap)
                    self.clips.set_end(cid, max(te, s0 + MIN_CLIP_DURATION))
                    self.handleDragged.emit(self.clips.get(cid).end)
                else:
                    ns = self._snap(s0 + (t - anchor), cid, no_snap)
                    length = e0 - s0
                    if no_snap is False and abs((ns + length) - self._snap(ns + length, cid, False)) > 1e-9:
                        ns = self._snap(ns + length, cid, False) - length
                    if len(self._group) > 1:                       # several selected clips move together
                        delta = ns - s0
                        delta = max(delta, -min(v[0] for v in self._group.values()))
                        delta = min(delta, self.duration - max(v[1] for v in self._group.values()))
                        for gid, (gs, _ge) in self._group.items():
                            self.clips.move_to(gid, gs + delta)
                    else:
                        self.clips.move_to(cid, ns)
            except ClipError:
                pass
            self.setToolTip("")
            return
        if self._scrub:
            self.seekRequested.emit(t)
            return
        clip, part = self.hit_test(e.position())
        if clip:
            self.setCursor(Qt.SizeHorCursor if part in ("left", "right") else Qt.OpenHandCursor)
            self.setToolTip(f"{clip.name}: {format_timestamp(clip.start, True)} - {format_timestamp(clip.end, True)} "
                            f"({clip.duration:.2f}s)")
        else:
            self.setCursor(Qt.ArrowCursor)
            self.setToolTip(f"{format_timestamp(t, True)}  (double-click or right-click to add a clip, drag to select several)")

    def mouseReleaseEvent(self, _e: QMouseEvent) -> None:  # noqa: N802
        if self._band:
            active = self._band[2]
            self._band = None
            if not active:                                 # simple click on empty lane space
                if not self._band_base:
                    self.select(None)
                self.seekRequested.emit(self._press_t)
            self.update()
        if self._drag:
            cid = self._drag[1]
            self._drag = None
            self._group = {}
            self.dragFinished.emit(cid)
        self._scrub = False

    def mouseDoubleClickEvent(self, e: QMouseEvent) -> None:  # noqa: N802
        if self.duration <= 0 or e.button() != Qt.LeftButton:
            return
        if e.position().y() < self._lane_top():
            return                                         # ruler / thumbnail strip: not a place for clips
        clip, _ = self.hit_test(e.position())
        if clip is None:                                   # exactly here - also below an existing clip (it gets its own lane)
            self.addClipAt.emit(max(0.0, min(self.x2t(e.position().x()), self.duration)))

    def wheelEvent(self, e: QWheelEvent) -> None:  # noqa: N802
        if self.duration <= 0:
            return
        dy = e.angleDelta().y() or e.angleDelta().x()
        if e.modifiers() & Qt.ControlModifier:
            self.zoom(1.25 if dy > 0 else 0.8, e.position().x())
        else:
            self.set_view_start(self.view_start - dy / 120 * self.visible_seconds() * 0.15)
            self.viewChanged.emit()
        e.accept()

    def keyPressEvent(self, e: QKeyEvent) -> None:  # noqa: N802
        if e.key() in (Qt.Key_Delete, Qt.Key_Backspace) and self.selected:
            self.clipActionRequested.emit("delete", self.selected)
        elif e.key() == Qt.Key_A and e.modifiers() & Qt.ControlModifier:
            self.select_all()
        elif e.key() == Qt.Key_Escape and self.selected_ids:
            self.select(None)
        else:
            super().keyPressEvent(e)

    def resizeEvent(self, e) -> None:  # noqa: N802
        if self.duration > 0:
            self.pps = max(self.pps, self.min_pps())
            self.clamp_view()
        self.viewChanged.emit()
        super().resizeEvent(e)


class TimelineWidget(QWidget):
    """Canvas + scroll bar + zoom buttons."""

    seekRequested = Signal(float)
    selectionChanged = Signal(object)
    clipActionRequested = Signal(str, str)
    addClipAt = Signal(float)
    addClipSpan = Signal(float, float)
    selectionSetChanged = Signal(list)
    handleDragged = Signal(float)
    dragFinished = Signal(str)

    def __init__(self, clips: ClipCollection, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.canvas = TimelineCanvas(clips)
        self.scroll = QScrollBar(Qt.Horizontal)
        self._updating = False
        bar = QHBoxLayout()
        self.btn_out = self._btn("zoom_out", "Zoom out (Ctrl+Wheel)")
        self.btn_in = self._btn("zoom_in", "Zoom in (Ctrl+Wheel)")
        self.btn_fit = self._btn("fit", "Fit whole video")
        self.controls = QWidget()                       # zoom buttons; the owner decides where to show them
        cl = QHBoxLayout(self.controls)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.setSpacing(4)
        self.zoom_label = QLabel("")
        self.zoom_label.setObjectName("muted")
        self.zoom_label.setMinimumWidth(70)
        self.zoom_label.setAlignment(Qt.AlignCenter)
        for b in (self.btn_out, self.zoom_label, self.btn_in, self.btn_fit):
            cl.addWidget(b)
        bar.addWidget(self.controls)
        bar.addStretch(1)
        self.hint = QToolButton()
        self.hint.setText("Drag edges to trim - drag body to move - double/right-click to add - drag on empty space to select several - Alt: no snap")
        self.hint.setEnabled(False)
        self.hint.setStyleSheet("QToolButton{border:none;background:transparent;}")
        bar.addWidget(self.hint)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)
        self.bar_widget = QWidget()
        self.bar_widget.setLayout(bar)
        lay.addWidget(self.bar_widget)
        lay.addWidget(self.canvas, 1)
        lay.addWidget(self.scroll)
        self.btn_in.clicked.connect(lambda: self.canvas.zoom(1.5))
        self.btn_out.clicked.connect(lambda: self.canvas.zoom(1 / 1.5))
        self.btn_fit.clicked.connect(self.canvas.fit)
        self.scroll.valueChanged.connect(self._on_scroll)
        self.canvas.viewChanged.connect(self._sync_scroll)
        for sig in ("seekRequested", "selectionChanged", "clipActionRequested", "addClipAt",
                    "handleDragged", "dragFinished", "addClipSpan", "selectionSetChanged"):
            getattr(self.canvas, sig).connect(getattr(self, sig))

    def move_controls_to(self, layout) -> None:
        """Place the zoom buttons in another layout and drop the now-empty bar (saves a row of height)."""
        layout.addWidget(self.controls)
        self.bar_widget.setVisible(self.hint.isVisible())

    def _btn(self, icon_name: str, tip: str) -> QToolButton:
        b = QToolButton()
        b.setIcon(theme.icon(icon_name, size=18))
        b.setToolTip(tip)
        b.setAccessibleName(tip)
        return b

    def refresh_icons(self) -> None:
        for b, n in ((self.btn_out, "zoom_out"), (self.btn_in, "zoom_in"), (self.btn_fit, "fit")):
            b.setIcon(theme.icon(n, size=18))

    def _on_scroll(self, v: int) -> None:
        if not self._updating:
            self.canvas.set_view_start(v / self.canvas.pps)

    def _sync_scroll(self) -> None:
        c = self.canvas
        self.zoom_label.setText(f"{c.pps:.1f} px/s" if c.duration > 0 else "")
        self._updating = True
        total_px = int(c.duration * c.pps)
        self.scroll.setRange(0, max(0, total_px - c.width()))
        self.scroll.setPageStep(c.width())
        self.scroll.setValue(int(c.view_start * c.pps))
        self._updating = False
        c.update()

    # convenience passthroughs
    def set_duration(self, d: float) -> None:
        self.canvas.set_duration(d)
        self._sync_scroll()

    def set_strip_height(self, h: int) -> None:
        self.canvas.strip_h = h
        self.canvas._on_clips_changed()

    def set_playhead(self, t: float, follow: bool = True) -> None:
        self.canvas.set_playhead(t)
        if follow and self.canvas._drag is None:
            self.canvas.ensure_visible(t)
            self._sync_scroll()

    def select(self, clip_id: Optional[str]) -> None:
        self.canvas.select(clip_id)

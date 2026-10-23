"""Export list: shows every clip, lets the user edit name/times and reorder."""

from __future__ import annotations

from typing import Dict, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QAbstractItemView, QHBoxLayout, QHeaderView, QPushButton, QTableWidget,
                               QTableWidgetItem, QVBoxLayout, QWidget)

from app import theme
from app.models.clip import ClipCollection, ClipError
from app.models.crop import ASPECT_PRESETS
from app.utils.validation import format_timestamp, parse_timestamp
from app.widgets.timeline import build_clip_menu

COLS = ["#", "Name", "File name", "Start", "End", "Length", "Crop", "Status"]


class ClipListWidget(QWidget):
    selectionChanged = Signal(object)
    clipActionRequested = Signal(str, str)
    editError = Signal(str)

    def __init__(self, clips: ClipCollection, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.clips = clips
        self.status: Dict[str, str] = {}
        self._rebuilding = False
        self.table = QTableWidget(0, len(COLS))
        self.table.setHorizontalHeaderLabels(COLS)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.setToolTip("Double-click Name, File name, Start or End to edit. Order here = export order.")
        for i, w in enumerate((40, 130, 170, 105, 105, 80, 100)):
            self.table.setColumnWidth(i, w)
        self.btn_up = QPushButton(theme.icon("up", size=16), "")
        self.btn_down = QPushButton(theme.icon("down", size=16), "")
        self.btn_up.setToolTip("Move selected clip earlier in the export order")
        self.btn_down.setToolTip("Move selected clip later in the export order")
        self.btn_up.setAccessibleName("Move clip up")
        self.btn_down.setAccessibleName("Move clip down")
        side = QVBoxLayout()
        side.addWidget(self.btn_up)
        side.addWidget(self.btn_down)
        side.addStretch(1)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.table, 1)
        lay.addLayout(side)
        clips.subscribe(self.rebuild)
        self.table.itemSelectionChanged.connect(self._sel)
        self.table.itemChanged.connect(self._edited)
        self.table.customContextMenuRequested.connect(self._menu)
        self.btn_up.clicked.connect(lambda: self._move(-1))
        self.btn_down.clicked.connect(lambda: self._move(1))

    def refresh_icons(self) -> None:
        self.btn_up.setIcon(theme.icon("up", size=16))
        self.btn_down.setIcon(theme.icon("down", size=16))

    def selected_id(self) -> Optional[str]:
        rows = self.table.selectionModel().selectedRows()
        return self.table.item(rows[0].row(), 0).data(Qt.UserRole) if rows else None

    def select(self, clip_id: Optional[str]) -> None:
        self._rebuilding = True
        self.table.clearSelection()
        if clip_id:
            for r in range(self.table.rowCount()):
                if self.table.item(r, 0).data(Qt.UserRole) == clip_id:
                    self.table.selectRow(r)
        self._rebuilding = False

    def set_status(self, clip_id: str, text: str) -> None:
        self.status[clip_id] = text
        for r in range(self.table.rowCount()):
            if self.table.item(r, 0).data(Qt.UserRole) == clip_id:
                self._set(r, 7, text, editable=False)

    def clear_status(self) -> None:
        self.status.clear()
        self.rebuild()

    def _set(self, row: int, col: int, text: str, editable: bool, color: Optional[str] = None) -> None:
        was = self._rebuilding
        self._rebuilding = True
        it = self.table.item(row, col)
        new = it is None
        if new:
            it = QTableWidgetItem()
        it.setText(text)
        flags = it.flags() | Qt.ItemIsSelectable | Qt.ItemIsEnabled
        it.setFlags(flags | Qt.ItemIsEditable if editable else flags & ~Qt.ItemIsEditable)
        if color:
            it.setForeground(QColor(color))
        if new:
            self.table.setItem(row, col, it)
        self._rebuilding = was

    def rebuild(self) -> None:
        if self._rebuilding:
            return
        keep = self.selected_id()
        self._rebuilding = True
        self.table.setRowCount(len(self.clips.clips))
        for r, c in enumerate(self.clips.clips):
            self._set(r, 0, f"{r + 1}", False, c.color)
            self.table.item(r, 0).setData(Qt.UserRole, c.id)
            self._set(r, 1, c.name, True)
            self._set(r, 2, c.filename, True)
            self.table.item(r, 2).setToolTip("Leave empty to use the filename template from Settings")
            self._set(r, 3, format_timestamp(c.start, True), True)
            self._set(r, 4, format_timestamp(c.end, True), True)
            self._set(r, 5, f"{c.duration:.2f}s", False)
            self._set(r, 6, ASPECT_PRESETS[c.crop.preset][0].split(" (")[0], False)
            self._set(r, 7, self.status.get(c.id, "Ready"), False)
        self._rebuilding = False
        if keep:
            self.select(keep)

    def _sel(self) -> None:
        if not self._rebuilding:
            self.selectionChanged.emit(self.selected_id())

    def _edited(self, item: QTableWidgetItem) -> None:
        if self._rebuilding:
            return
        cid = self.table.item(item.row(), 0).data(Qt.UserRole)
        try:
            c = self.clips.get(cid)
            text = item.text().strip()
            if item.column() == 1:
                self.clips.rename(cid, text)
            elif item.column() == 2:
                self.clips.set_filename(cid, text)
            elif item.column() == 3:
                self.clips.set_range(cid, parse_timestamp(text), c.end)
            elif item.column() == 4:
                self.clips.set_range(cid, c.start, parse_timestamp(text))
        except (ClipError, ValueError) as exc:
            self.editError.emit(str(exc))
            self.rebuild()

    def _move(self, delta: int) -> None:
        cid = self.selected_id()
        if not cid:
            return
        i = self.clips.index_of(cid)
        j = i + delta
        if 0 <= j < len(self.clips):
            self.clips.reorder(i, j)
            self.select(cid)

    def _menu(self, pos) -> None:
        cid = self.selected_id()
        if not cid:
            return
        menu, acts = build_clip_menu(self)
        chosen = menu.exec(self.table.viewport().mapToGlobal(pos))
        for k, a in acts.items():
            if a is chosen:
                self.clipActionRequested.emit(k, cid)

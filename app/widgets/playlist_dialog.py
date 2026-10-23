"""Dialog asking what to do with a playlist link."""

from __future__ import annotations

from typing import List, Optional

from PySide6.QtWidgets import (QAbstractItemView, QDialog, QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
                               QPushButton, QVBoxLayout, QWidget)

from app.services.ytdlp_service import PlaylistInfo
from app.utils.validation import format_timestamp


class PlaylistDialog(QDialog):
    """Result: ``choice`` in {"single", "open", "download_all"} and ``selected_url``."""

    def __init__(self, info: PlaylistInfo, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        post = getattr(info, "kind", "playlist") == "post"
        self.setWindowTitle("Choose the video" if post else "Playlist detected")
        self.setMinimumSize(560, 420)
        self.info = info
        self.choice = ""
        self.selected_url = ""
        self.list = QListWidget()
        self.list.setSelectionMode(QAbstractItemView.SingleSelection)
        for e in info.entries:
            it = QListWidgetItem(f"{e.title}   [{format_timestamp(e.duration) if e.duration else '?'}]")
            it.setData(256, e.url)
            self.list.addItem(it)
        self.list.setCurrentRow(0)
        self.btn_open = QPushButton("Open selected video in editor")
        self.btn_open.setObjectName("primary")
        self.btn_all = QPushButton(f"Download all {len(info.entries)} videos (full)")
        self.btn_single = QPushButton("Only the single video from the link")
        self.btn_single.setVisible(info.single_url_available)
        cancel = QPushButton("Cancel")
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel(
            f"<b>{info.title}</b><br>This page contains {len(info.entries)} videos (your post and maybe suggested ones). "
            "Select the video you want, then open it." if post else
            f"<b>{info.title}</b><br>This link points to a playlist. What would you like to do?"))
        if post:
            self.btn_all.setVisible(False)
        lay.addWidget(self.list, 1)
        row = QHBoxLayout()
        for b in (self.btn_single, self.btn_all, self.btn_open, cancel):
            row.addWidget(b)
        lay.addLayout(row)
        self.btn_open.clicked.connect(lambda: self._done("open"))
        self.btn_all.clicked.connect(lambda: self._done("download_all"))
        self.btn_single.clicked.connect(lambda: self._done("single"))
        cancel.clicked.connect(self.reject)
        self.list.itemDoubleClicked.connect(lambda _i: self._done("open"))

    def _done(self, choice: str) -> None:
        it = self.list.currentItem()
        self.choice = choice
        self.selected_url = it.data(256) if it else ""
        self.accept()

    def all_urls(self) -> List[str]:
        return [e.url for e in self.info.entries]

"""Collapsible section used by the Properties panel."""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QSize, Qt
from PySide6.QtWidgets import QToolButton, QVBoxLayout, QWidget


class CollapsibleSection(QWidget):
    """A header button that shows/hides *content* (keyboard accessible, state is remembered while the app runs)."""

    def __init__(self, title: str, content: QWidget, expanded: bool = True, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.title = title
        self.content = content
        self.header = QToolButton()
        self.header.setObjectName("sectionHeader")
        self.header.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.header.setIconSize(QSize(16, 16))
        self.header.setCheckable(True)
        self.header.setChecked(expanded)
        self.header.setSizePolicy(self.header.sizePolicy().horizontalPolicy(), self.header.sizePolicy().verticalPolicy())
        self.header.setAccessibleName(title)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addWidget(self.header)
        lay.addWidget(content)
        self.header.toggled.connect(self._toggle)
        self._toggle(expanded)
        from PySide6.QtWidgets import QSizePolicy
        self.header.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def _toggle(self, on: bool) -> None:
        self.header.setText(self.title.replace("&", "&&"))   # && = literal & on a button
        self.refresh_icons()
        self.content.setVisible(on)

    def refresh_icons(self) -> None:
        from app import theme
        self.header.setIcon(theme.icon("chev_down" if self.header.isChecked() else "chev_right", size=16))

    def is_expanded(self) -> bool:
        return self.header.isChecked()

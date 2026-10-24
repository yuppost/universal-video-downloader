"""Stop the mouse wheel from changing dropdowns / number boxes that were not clicked.

Scrolling a settings page with the wheel used to change every combo box the pointer passed over. With the guard, a combo
or spin box only reacts to the wheel while it has keyboard focus (the user clicked it); otherwise the wheel scrolls the
enclosing scroll area as the user expects.
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtWidgets import QAbstractScrollArea, QAbstractSpinBox, QComboBox, QWidget


class WheelGuard(QObject):
    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        if event.type() == QEvent.Type.Wheel and isinstance(obj, (QComboBox, QAbstractSpinBox)):
            if not obj.hasFocus():
                area = obj.parentWidget()
                while area is not None and not isinstance(area, QAbstractScrollArea):
                    area = area.parentWidget()
                if area is not None:
                    bar = area.verticalScrollBar()
                    dy = event.angleDelta().y()
                    if dy and bar.isVisible():
                        bar.setValue(bar.value() - int(dy / 120 * max(bar.singleStep(), 20) * 3))
                event.accept()
                return True                       # swallowed: the value stays as it was
        return False


_guard = WheelGuard()


def install(root: QWidget) -> None:
    """Guard every combo box and spin box below *root* (call after the widgets are created)."""
    for w in list(root.findChildren(QComboBox)) + list(root.findChildren(QAbstractSpinBox)):
        w.setFocusPolicy(Qt.StrongFocus)          # the wheel must not give focus either
        w.installEventFilter(_guard)

"""Window picker for the companion bind flow (Path B stage 3).

The thinnest possible picker: a modal QDialog with a QListWidget over the
serialized ``WindowCandidate`` dicts the ``galgame_window_candidates`` event
carries (window_id / title / process_name / app_id / pid / visible -- title +
process_name is all a human needs to spot the game window). Pure presentation:
no backend calls; returns the picked window_id (or None on cancel).
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QVBoxLayout,
    QWidget,
)


class WindowPickerDialog(QDialog):
    def __init__(self, candidates: list[dict[str, Any]], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("选择游戏窗口")
        self.setModal(True)

        self._list = QListWidget(self)
        for candidate in candidates:
            title = str(candidate.get("title") or "(无标题)")
            process = candidate.get("process_name")
            label = f"{title} — {process}" if process else title
            item = QListWidgetItem(label, self._list)
            item.setData(Qt.ItemDataRole.UserRole, str(candidate.get("window_id") or ""))
        if self._list.count():
            self._list.setCurrentRow(0)
        self._list.itemDoubleClicked.connect(lambda _item: self.accept())

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel, self
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("找到这些窗口，请选中正在玩的游戏：", self))
        layout.addWidget(self._list, 1)
        layout.addWidget(buttons)

    def selected_window_id(self) -> str | None:
        item = self._list.currentItem()
        if item is None:
            return None
        return str(item.data(Qt.ItemDataRole.UserRole) or "") or None

    @staticmethod
    def pick(candidates: list[dict[str, Any]], parent: QWidget | None = None) -> str | None:
        """Modal helper: show the picker, return the chosen window_id or None."""
        dialog = WindowPickerDialog(candidates, parent)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        return dialog.selected_window_id()

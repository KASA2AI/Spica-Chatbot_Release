"""Package selector with a separate remove target in each imported popup row."""

from PySide6.QtCore import QEvent, QRect, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QPainter, QPen
from PySide6.QtWidgets import QComboBox, QListView, QStyle, QStyledItemDelegate, QStyleOptionViewItem


REMOVABLE_ROLE = Qt.ItemDataRole.UserRole + 1


class _PackageItemDelegate(QStyledItemDelegate):
    @staticmethod
    def remove_rect(rect: QRect) -> QRect:
        width = max(28, rect.height())
        return QRect(rect.right() - width + 1, rect.top(), width, rect.height())

    def sizeHint(self, option, index):  # noqa: N802
        size = super().sizeHint(option, index)
        height = max(size.height(), option.fontMetrics.height() + 12)
        return QSize(size.width() + max(28, height), height)

    def paint(self, painter, option, index):
        if not index.data(REMOVABLE_ROLE):
            return super().paint(painter, option, index)
        label = QStyleOptionViewItem(option)
        self.initStyleOption(label, index)
        target = self.remove_rect(option.rect)
        # Elide before painting so long community names cannot run under ×.
        label.text = option.fontMetrics.elidedText(
            label.text, Qt.TextElideMode.ElideRight, max(0, option.rect.width() - target.width() - 12)
        )
        option.widget.style().drawControl(QStyle.ControlElement.CE_ItemViewItem, label, painter, option.widget)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        brush = (option.palette.highlightedText() if option.state & QStyle.StateFlag.State_Selected
                 else option.palette.text())
        painter.setPen(QPen(brush, 1.6))
        center = target.center()
        radius = max(4, option.fontMetrics.height() // 4)
        painter.drawLine(center.x() - radius, center.y() - radius, center.x() + radius, center.y() + radius)
        painter.drawLine(center.x() - radius, center.y() + radius, center.x() + radius, center.y() - radius)
        painter.restore()


class PackageComboBox(QComboBox):
    removal_requested = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        # Use the same Qt popup on Windows and Linux, not a native menu whose
        # mouse handling may bypass the remove target.
        self.setView(QListView(self))
        self.setItemDelegate(_PackageItemDelegate(self))
        self.view().viewport().installEventFilter(self)
        self._remove_pressed = None

    def add_package(self, label: str, path: str | None, *, removable: bool = False) -> None:
        self.addItem(label, path)
        self.setItemData(self.count() - 1, bool(removable and path), REMOVABLE_ROLE)

    def hidePopup(self):  # noqa: N802
        self._remove_pressed = None
        super().hidePopup()

    def eventFilter(self, watched, event):  # noqa: N802
        if watched is self.view().viewport() and event.type() in (
            QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonRelease,
            QEvent.Type.MouseButtonDblClick,
        ) and event.button() == Qt.MouseButton.LeftButton:
            index = self.view().indexAt(event.position().toPoint())
            hit = (self.isEnabled() and index.isValid() and index.data(REMOVABLE_ROLE)
                   and _PackageItemDelegate.remove_rect(self.view().visualRect(index)).contains(event.position().toPoint()))
            path = index.data(Qt.ItemDataRole.UserRole)
            if event.type() == QEvent.Type.MouseButtonRelease:
                pressed, self._remove_pressed = self._remove_pressed, None
                if pressed is not None:
                    if hit and path == pressed:
                        self.hidePopup()
                        QTimer.singleShot(0, lambda: self.removal_requested.emit(path))
                    return True
            elif hit:
                self._remove_pressed = path
                return True
        return super().eventFilter(watched, event)

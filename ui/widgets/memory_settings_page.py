"""Personal memory editor; all state changes go through the existing core surface."""
from __future__ import annotations

from datetime import datetime
import time

from PySide6.QtCore import Qt, Signal, Slot
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFrame, QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
    QMessageBox, QPushButton, QScrollArea, QSizePolicy, QTextEdit, QVBoxLayout, QWidget,
)

from ui.workers.character_package_worker import CharacterPackageWorker


class MemorySettingsPage(QWidget):
    busy_changed = Signal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.surface_provider = None
        self.worker = None
        self._external_busy = False
        self.rows = []
        self._has_more = False
        self._preferred_selection = None
        self._processing = {}
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.scroll_area = QScrollArea(self)
        self.scroll_area.setObjectName('settingsScroll')
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        body = QWidget()
        body.setObjectName('settingsBody')
        # Keep the editors' minimum sizes when the overlay has little height.
        # QScrollArea exposes the remaining controls instead of overlapping them.
        layout = QVBoxLayout(body)
        layout.setContentsMargins(0, 10, 0, 0)
        layout.setSpacing(10)
        self.scroll_area.setWidget(body)
        outer.addWidget(self.scroll_area)
        self.status = QLabel("查看当前角色的记忆，以及获准共享的本人事实。", self)
        self.status.setObjectName('settingsStatus')
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        processing_controls = QHBoxLayout()
        self.processing_status = QLabel('', self)
        self.processing_status.setObjectName('settingsHint')
        self.processing_status.setWordWrap(True)
        self.resume_processing_button = QPushButton('重试暂停的整理', self)
        self.resume_processing_button.setProperty('intent', 'quiet')
        self.resume_processing_button.setEnabled(False)
        processing_controls.addWidget(self.processing_status, 1)
        processing_controls.addWidget(self.resume_processing_button)
        layout.addLayout(processing_controls)
        controls = QHBoxLayout()
        self.history = QCheckBox("包含过去状态", self)
        self.refresh_button = QPushButton("刷新", self)
        self.more_button = QPushButton("加载更多", self)
        self.more_button.setEnabled(False)
        controls.addWidget(self.history, 1)
        controls.addWidget(self.refresh_button)
        controls.addWidget(self.more_button)
        layout.addLayout(controls)
        self.list = QListWidget(self)
        layout.addWidget(self.list, 1)
        self.editor = QTextEdit(self)
        self.editor.setAcceptRichText(False)
        self.editor.setPlaceholderText("选择记忆后修正，或输入要主动记住的本人事实。")
        layout.addWidget(self.editor, 1)
        self.sources = QTextEdit(self)
        self.sources.setReadOnly(True)
        self.sources.setPlaceholderText("来源、版本和修订原因")
        layout.addWidget(self.sources, 1)
        for editor in (self.list, self.editor, self.sources):
            # Share the available height before scrolling; their preferred
            # heights would otherwise force even a full-size panel to scroll.
            editor.setMinimumHeight(editor.minimumSizeHint().height())
            editor.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Ignored)
        self.reason = QComboBox(self)
        self.reason.addItem("原来记错了", "correction")
        self.reason.addItem("现在发生了变化", "change")
        reason_row = QHBoxLayout()
        label = QLabel('修订原因', self)
        label.setObjectName('settingsHint')
        reason_row.addWidget(label)
        reason_row.addWidget(self.reason, 1)
        layout.addLayout(reason_row)
        actions = QHBoxLayout()
        self.save_button = QPushButton("保存修订", self)
        self.save_button.setProperty('intent', 'primary')
        self.add_button = QPushButton("新增本人事实", self)
        self.delete_button = QPushButton("删除", self)
        self.delete_button.setProperty('intent', 'quiet')
        for widget in (self.save_button, self.add_button, self.delete_button):
            actions.addWidget(widget)
        layout.addLayout(actions)
        self.refresh_button.clicked.connect(self.refresh)
        self.more_button.clicked.connect(self._load_more)
        self.history.toggled.connect(self.refresh)
        self.list.currentItemChanged.connect(self._select)
        self.save_button.clicked.connect(self._revise)
        self.add_button.clicked.connect(self._remember)
        self.delete_button.clicked.connect(self._forget)
        self.resume_processing_button.clicked.connect(self._resume_processing)
        self._select()

    def showEvent(self, event):
        super().showEvent(event)
        self.refresh()

    def _surface(self):
        return self.surface_provider() if self.surface_provider is not None else None

    def _selected(self):
        item = self.list.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item is not None else None

    def set_external_busy(self, busy):
        self._external_busy = busy
        self.set_busy(self.worker is not None)

    def set_busy(self, busy):
        busy = busy or self._external_busy
        for widget in (self.list, self.editor, self.reason, self.refresh_button, self.history, self.add_button):
            widget.setEnabled(not busy)
        active = self._selected() and self._selected()["status"] == "active"
        self.save_button.setEnabled(not busy and self._selected() is not None)
        self.reason.setEnabled(not busy and bool(active))
        self.delete_button.setEnabled(not busy and self._selected() is not None)
        self.more_button.setEnabled(not busy and self._has_more)
        self.resume_processing_button.setEnabled(not busy and self._processing.get('auto_enabled', False)
            and bool(self._processing.get('held') or self._processing.get('provider_hold')))

    def refresh(self, *_):
        self._read_page()

    def _load_more(self):
        if self.rows and self._has_more:
            self._read_page(before_id=self.rows[-1]['id'])

    def _read_page(self, *, before_id=None):
        surface = self._surface()
        if surface is None:
            self.status.setText("核心启动后可查看和修正记忆。")
            return
        history = self.history.isChecked()
        self._start(lambda: {"rows": surface.list_memory(limit=501, include_history=history, before_id=before_id),
                            "processing": surface.memory_maintenance_status(),
                            "append": before_id is not None}, "正在读取记忆…")

    def _resume_processing(self):
        surface = self._surface()
        if surface is not None and self.worker is None and self.resume_processing_button.isEnabled():
            self._start(lambda: {'processing_resumed': surface.resume_memory_maintenance()}, '已请求有限重试…')

    def _select(self, *_):
        row = self._selected()
        self.save_button.setEnabled(row is not None)
        self.delete_button.setEnabled(row is not None)
        if row is None:
            self.editor.clear()
            self.sources.clear()
            return
        self.editor.setPlainText(row["content"])
        self.reason.setEnabled(row["status"] == "active")
        if row["status"] != "active":
            self.reason.setCurrentIndex(0)
        state = "当前有效" if row["status"] == "active" else "真实的过去状态"
        if row["status"] == "active":
            if row.get("valid_until") is not None and row["valid_until"] <= time.time():
                state = "已过有效期"
            elif row.get("valid_from") is not None and row["valid_from"] > time.time():
                state = "尚未生效"
        reason = {"change": "本人情况改变", "correction": "撤回原来的误记"}.get(row.get("revision_reason"), "首次记录")
        lines = [f"{state} · 版本 {row['revision']} · {reason}"]
        for key, label in (("valid_from", "生效时间"), ("valid_until", "有效期至")):
            if row.get(key) is not None:
                lines.append(label + "：" + datetime.fromtimestamp(row[key]).astimezone().isoformat(timespec="seconds"))
        for source in row["sources"]:
            if source.get("quote"):
                lines.append(f"原文 #{source['id']}：{source['quote']}")
            elif source.get("availability") == "outside_scope":
                lines.append(f"来源 #{source['id']}：其他角色或本人范围的只读引用，不提供原文。")
            elif source.get("origin_character"):
                lines.append(f"来自 {source['origin_character']} 允许共享的本人事实；不共享其原始对话。")
            else:
                lines.append(f"来源 #{source['id']}：原文已按留存政策到期，长期记忆单独保留。")
        self.sources.setPlainText("\n\n".join(lines))

    def _revise(self):
        row, text, surface = self._selected(), self.editor.toPlainText().strip(), self._surface()
        if row is None or not text or surface is None:
            return
        entry_id, reason = row["id"], self.reason.currentData()
        self._start(lambda: {"saved_id": surface.revise_memory(entry_id, text, reason=reason)}, "正在修订…")

    def _remember(self):
        text, surface = self.editor.toPlainText().strip(), self._surface()
        if text and surface is not None:
            self._start(lambda: {"saved_id": surface.remember(text)}, "正在保存本人事实…")

    def _forget(self):
        row, surface = self._selected(), self._surface()
        if row is None or surface is None or self.worker is not None:
            return
        dialog = QMessageBox(self)
        dialog.setWindowTitle("删除这条记忆")
        dialog.setText(row["content"])
        dialog.setInformativeText("同时清除相关派生记忆。默认删除支撑它的原文片段；共享事实的删除会在获准共享的角色间同步。")
        remove = dialog.addButton("删除记忆", QMessageBox.ButtonRole.DestructiveRole)
        cancel = dialog.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        dialog.setDefaultButton(cancel)
        dialog.exec()
        accepted = dialog.clickedButton() is remove
        dialog.deleteLater()
        if accepted:
            entry_id = row["id"]
            self._start(lambda: {"removed": surface.forget_memory(entry_id)}, "正在删除…")

    def _start(self, operation, message):
        if self.worker is not None or self._external_busy:
            return
        self.status.setText(message)
        self.set_busy(True)
        self.busy_changed.emit(True)
        self.worker = CharacterPackageWorker(operation, self)
        self.worker.completed.connect(self._complete)
        self.worker.start()

    @Slot(object)
    def _complete(self, event):
        self.worker.deleteLater()
        self.worker = None
        self.busy_changed.emit(False)
        self.set_busy(False)
        result = event.data
        if not result["ok"]:
            self.status.setText("操作未完成：" + result["error"])
            return
        if "rows" not in result:
            self._preferred_selection = result.get("saved_id")
            self.refresh()
            return
        self._has_more = len(result['rows']) > 500
        self._processing = result.get('processing', {})
        work = self._processing
        if work.get('auto_enabled'):
            description = f"待处理 {work.get('pending', 0)} · 暂停 {work.get('held', 0)}；手动重试每份最多 {work.get('max_attempts', 3)} 轮。"
            if work.get('provider_hold'):
                description += ' 请先检查后台模型配置。'
            elif 'input_budget' in work.get('held_reasons', []):
                description += ' 暂停材料超过当前处理预算，原文仍保留。'
            self.processing_status.setText(description)
        else:
            self.processing_status.setText('自动整理已关闭，原文照常保存。')
        self.rows = [*(self.rows if result.get('append') else []), *result['rows'][:500]]
        selected_id = self._preferred_selection or (self._selected()["id"] if self._selected() else None)
        self._preferred_selection = None
        self.list.clear()
        for row in self.rows:
            prefix = "过去 · " if row["status"] != "active" else ""
            item = QListWidgetItem(prefix + row["content"][:80], self.list)
            item.setData(Qt.ItemDataRole.UserRole, row)
            if row["id"] == selected_id:
                self.list.setCurrentItem(item)
        self.status.setText(f"显示最近 {len(self.rows)} 条记忆。原始聊天默认保留一年，长期事实与经历独立保存。")
        self.set_busy(False)

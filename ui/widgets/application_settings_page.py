"""Desktop settings widgets; configuration and network I/O live in the host."""

from __future__ import annotations

from copy import deepcopy

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFormLayout, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QScrollArea, QSpinBox, QVBoxLayout, QWidget,
)


class ApplicationSettingsPage(QWidget):
    save_requested = Signal()
    test_requested = Signal()
    secret_save_requested = Signal(str, str)
    open_config_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("applicationSettings")
        self._snapshot = None
        self.fields = {}
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 8, 0, 0)
        self.scroll_area = QScrollArea(self)
        self.scroll_area.setObjectName("settingsScroll")
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.body = QWidget()
        self.body.setObjectName("settingsBody")
        body_layout = QVBoxLayout(self.body)
        body_layout.setContentsMargins(0, 0, 8, 0)
        body_layout.setSpacing(8)
        self.scroll_area.setWidget(self.body)
        layout.addWidget(self.scroll_area, 1)
        self._hint(body_layout, "此页设置保存在本机，保存后重启生效。切换角色会保留这些设置。")

        form = self._section(body_layout, "模型与连接 · OpenAI 兼容")
        self._text(form, "API 地址", "llm.base_url", "留空使用 OpenAI 默认地址")
        self._text(form, "模型名称", "llm.model", "填写服务商提供的模型 ID")
        self._combo(form, "推理强度", "llm.reasoning_effort", [
            ("服务商默认", "default"), ("关闭思考", "none"),
            ("低", "low"), ("中", "medium"), ("高", "high"),
        ])
        self.api_key, self.api_key_status = self._secret(body_layout, "API Key", "openai_api_key")
        self.test_button = QPushButton("测试连接", self)
        self.test_button.setToolTip("使用当前填写的地址与密钥检查模型列表；不会发送聊天或保存设置。")
        self.test_button.clicked.connect(self.test_requested.emit)
        body_layout.addWidget(self.test_button)

        form = self._section(body_layout, "语音与功能")
        self._combo(form, "语音识别", "stt.backend", [("本地 Whisper", "faster_whisper"), ("Google 在线（中文）", "google")])
        self._combo(form, "识别语言", "stt.language", [("中文", "zh"), ("日语", "ja"), ("英语", "en")], editable=True)
        self._combo(form, "识别模型", "stt.model", [
            ("large-v3-turbo", "large-v3-turbo"), ("large-v3", "large-v3"),
            ("medium", "medium"), ("small", "small"), ("base", "base"),
        ], editable=True)
        self._hint(body_layout, "语言、模型、设备与预热用于本地 Whisper。Google 在线识别目前仅支持中文，需要网络。")
        for label, key in (
            ("播放角色语音", "tts.enabled"), ("允许屏幕理解", "screen.enabled"),
            ("允许唱歌（需角色包支持）", "song.enabled"), ("启用追番功能", "anime.enabled"),
        ):
            checkbox = QCheckBox(label, self)
            self._register(key, checkbox)
            body_layout.addWidget(checkbox)
        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        body_layout.addLayout(form)
        self._combo(form, "陪玩插话", "galgame.reaction_mode", [
            ("关闭", "off"), ("少量", "low"), ("正常", "normal"), ("活跃", "high"),
        ])
        self._hint(body_layout, "插话仅在启动游戏陪玩会话后生效。")

        self.advanced_button = QPushButton("▸ 高级选项", self)
        self.advanced_button.setCheckable(True)
        body_layout.addWidget(self.advanced_button)
        self.advanced = QWidget(self)
        advanced_layout = QVBoxLayout(self.advanced)
        advanced_layout.setContentsMargins(0, 0, 0, 0)
        form = self._section(advanced_layout, "本地识别设备")
        self._combo(form, "运行设备", "stt.device", [("NVIDIA GPU", "cuda"), ("CPU", "cpu"), ("自动", "auto")])
        self._combo(form, "计算精度", "stt.compute_type", [
            ("float16", "float16"), ("int8", "int8"), ("int8_float16", "int8_float16"),
            ("float32", "float32"), ("自动", "default"),
        ])
        self._hint(advanced_layout, "CPU 建议 int8；NVIDIA GPU 建议 float16。首次使用可能需要下载识别模型。")
        warmup = QCheckBox("启动时预热语音识别", self)
        self._register("stt.warmup_on_startup", warmup)
        advanced_layout.addWidget(warmup)
        self.open_config_button = QPushButton("打开配置文件夹", self)
        self.open_config_button.clicked.connect(self.open_config_requested.emit)
        advanced_layout.addWidget(self.open_config_button)
        self.restore_button = QPushButton("恢复此页默认值", self)
        self.restore_button.setToolTip("只修改此页表单，点击保存后才写入；保留密钥和角色设置。")
        self.restore_button.clicked.connect(self.restore_defaults)
        advanced_layout.addWidget(self.restore_button)
        body_layout.addWidget(self.advanced)
        self.advanced.hide()
        self.advanced_button.toggled.connect(self._toggle_advanced)
        body_layout.addStretch(1)

        self.status = QLabel("正在读取应用设置…", self)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.save_button = QPushButton("保存应用设置", self)
        self.save_button.setObjectName("saveApplicationButton")
        self.save_button.clicked.connect(self.save_requested.emit)
        actions = QHBoxLayout()
        self.discard_button = QPushButton("撤销修改", self)
        self.discard_button.setToolTip("撤销此页尚未保存的修改和密钥输入。")
        self.discard_button.clicked.connect(self.discard_changes)
        actions.addWidget(self.discard_button)
        actions.addWidget(self.save_button, 1)
        layout.addLayout(actions)

    def _section(self, layout, title):
        label = QLabel(title, self)
        label.setObjectName("settingsSection")
        layout.addWidget(label)
        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        form.setHorizontalSpacing(10)
        layout.addLayout(form)
        return form

    def _hint(self, layout, text):
        label = QLabel(text, self)
        label.setObjectName("settingsHint")
        label.setWordWrap(True)
        layout.addWidget(label)
        return label

    def _register(self, key, widget):
        self.fields[key] = widget
        widget.setObjectName(key.replace(".", "_"))
        if isinstance(widget, QCheckBox):
            widget.toggled.connect(self._edited)
        elif isinstance(widget, QComboBox):
            widget.currentTextChanged.connect(self._edited)
        elif isinstance(widget, QSpinBox):
            widget.valueChanged.connect(self._edited)
        else:
            widget.textChanged.connect(self._edited)

    def _text(self, form, label, key, placeholder=""):
        widget = QLineEdit(self)
        widget.setPlaceholderText(placeholder)
        widget.setMinimumWidth(0)
        self._register(key, widget)
        form.addRow(label, widget)
        return widget

    def _combo(self, form, label, key, items, *, editable=False):
        combo = QComboBox(self)
        combo.setEditable(editable)
        combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        combo.setMinimumContentsLength(7)
        for title, value in items:
            combo.addItem(title, value)
        self._register(key, combo)
        form.addRow(label, combo)

    def _secret(self, layout, label, slot):
        layout.addWidget(QLabel(label, self))
        row = QHBoxLayout()
        editor = QLineEdit(self)
        editor.setEchoMode(QLineEdit.EchoMode.Password)
        editor.setPlaceholderText("留空保留现有密钥")
        editor.setMinimumWidth(0)
        row.addWidget(editor, 1)
        button = QPushButton("保存密钥", self)
        button.clicked.connect(lambda: self.secret_save_requested.emit(slot, editor.text()))
        row.addWidget(button)
        layout.addLayout(row)
        status = self._hint(layout, "密钥只保存在本机，不随角色包分享。")
        return editor, status

    def _toggle_advanced(self, expanded):
        self.advanced.setVisible(expanded)
        self.advanced_button.setText("▾ 高级选项" if expanded else "▸ 高级选项")

    def load_snapshot(self, snapshot):
        self._snapshot = deepcopy(snapshot)
        overrides = snapshot["overrides"]
        for key, widget in self.fields.items():
            section, field = key.split(".")
            self._set_value(widget, snapshot["values"][section][field])
            widget.setEnabled(key not in overrides)
            widget.setToolTip(f"由 {overrides[key]} 覆盖，请先移除该配置来源。" if key in overrides else "")
        sources = {"inherited": "启动环境", "repo_dotenv": "本机配置", "parent_dotenv": "上级目录配置"}
        self.api_key_status.setText(
            f"已配置 · {sources.get(snapshot['api_key_source'], '本机')}；输入新值可替换，重启生效。"
            if snapshot["api_key_configured"] else "尚未配置 API Key；填写后点击“保存密钥”。"
        )
        if snapshot["api_key_source"] == "inherited":
            self.api_key_status.setText("API Key 由启动环境提供。请先移除该环境变量并重启，才能在此修改。")
        self.test_button.setEnabled(snapshot["connection_test_available"])
        self._update_stt_controls()
        self.status.setText("部分项目由旧配置覆盖，悬停可查看来源。" if overrides else "应用设置已读取，修改后保存并重启。")

    def _set_value(self, widget, value):
        widget.blockSignals(True)
        if isinstance(widget, QCheckBox):
            widget.setChecked(bool(value))
        elif isinstance(widget, QComboBox):
            index = widget.findData(value)
            if index < 0:
                widget.addItem(str(value or ""), value)
                index = widget.count() - 1
            widget.setCurrentIndex(index)
        elif isinstance(widget, QSpinBox):
            widget.setValue(int(value) if value is not None else widget.minimum())
        else:
            widget.setText(str(value or ""))
        widget.blockSignals(False)

    def values(self):
        values = {}
        for key, widget in self.fields.items():
            if isinstance(widget, QCheckBox):
                value = widget.isChecked()
            elif isinstance(widget, QComboBox):
                value = (widget.currentText().strip()
                         if widget.isEditable() and widget.currentText() != widget.itemText(widget.currentIndex())
                         else widget.currentData())
            elif isinstance(widget, QSpinBox):
                value = widget.value()
            else:
                value = widget.text().strip()
                if key in {"llm.base_url"}:
                    value = value or None
            section, field = key.split(".")
            values.setdefault(section, {})[field] = value
        return values

    def patch(self):
        if self._snapshot is None:
            return {}
        changes = {}
        for section, fields in self.values().items():
            for field, value in fields.items():
                key = f"{section}.{field}"
                if key not in self._snapshot["overrides"] and value != self._snapshot["values"][section][field]:
                    changes.setdefault(section, {})[field] = value
        return changes

    def restore_defaults(self):
        if self._snapshot is None:
            return
        for key, widget in self.fields.items():
            if key not in self._snapshot["overrides"]:
                section, field = key.split(".")
                self._set_value(widget, self._snapshot["defaults"][section][field])
        self._edited()

    def discard_changes(self):
        if self._snapshot is None:
            return
        self.api_key.clear()
        self.load_snapshot(self._snapshot)
        self.status.setText("已撤销未保存的修改。")

    def _update_stt_controls(self):
        if self._snapshot is None:
            return
        local = self.fields["stt.backend"].currentData() == "faster_whisper"
        for field in ("language", "model", "device", "compute_type", "warmup_on_startup"):
            key = f"stt.{field}"
            self.fields[key].setEnabled(local and key not in self._snapshot["overrides"])

    def _edited(self, *_):
        self._update_stt_controls()
        self.status.setText("有未保存的修改，保存后重启生效。")

    def set_busy(self, busy):
        self.body.setEnabled(not busy)
        self.save_button.setEnabled(not busy and self._snapshot is not None)
        self.discard_button.setEnabled(not busy and self._snapshot is not None)

    def has_unsaved_secret(self):
        return bool(self.api_key.text())

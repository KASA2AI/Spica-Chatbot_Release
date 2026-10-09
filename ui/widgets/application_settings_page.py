"""Desktop settings widgets; configuration and network I/O live in the host."""

from __future__ import annotations

from copy import deepcopy
from spica.config.application_settings import get_setting, set_setting, setting_leaves

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFormLayout, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QScrollArea, QSpinBox, QDoubleSpinBox, QVBoxLayout, QWidget,
)


class ApplicationSettingsPage(QWidget):
    save_requested = Signal()
    test_requested = Signal()
    secret_save_requested = Signal(str, str)
    open_config_requested = Signal()
    refresh_devices_requested = Signal()
    preview_requested = Signal()
    calibrate_requested = Signal()

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
        self._combo(form, "语音识别", "stt.backend", [("本地 Qwen3-ASR-1.7B", "qwen_asr"), ("百炼 Qwen 云端", "qwen_cloud")])
        self._combo(form, "识别语言", "stt.language", [("中文", "zh"), ("日语", "ja"), ("英语", "en")], editable=True)
        self._text(form, "本地模型目录", "stt.model", "models/stt/Qwen3-ASR-1.7B")
        self._text(form, "本地识别 Python", "stt.worker_python", "独立 Qwen 环境的 Python 路径")
        self._combo(form, "麦克风录音方式", "stt.mic_backend", [("普通麦克风（自动）", "auto"), ("普通麦克风", "generic"), ("ReSpeaker 硬件 VAD", "respeaker")])
        self._combo(form, "输入麦克风", "stt.input_device", [("系统默认", "")])
        self._combo(form, "输出音箱", "tts.output_device_id", [("系统默认", "")])
        self.refresh_devices_button = QPushButton("刷新音频设备", self)
        self.refresh_devices_button.clicked.connect(self.refresh_devices_requested.emit)
        body_layout.addWidget(self.refresh_devices_button)
        self._hint(body_layout, "可以指定麦克风和音箱；指定设备断开时会报错，不会自动换用其他设备。刷新列表不会改变已选设备。")
        self._combo(form, "云端地域", "stt.cloud_base_url", [
            ("百炼北京", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
            ("百炼新加坡", "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"),
        ], editable=True)
        self._text(form, "云端识别模型", "stt.cloud_model", "qwen3-asr-flash")
        self.asr_key, self.asr_key_status = self._secret(body_layout, "百炼语音识别 API Key", "dashscope_api_key")
        self._hint(body_layout, "本地模式不上传录音。云端模式会将有效录音发送至所选百炼服务，按服务商计费，不加载本地识别模型。先保存百炼密钥，再选择云端并保存设置；切换后重启生效。")
        for label, key in (
            ("启用语音引擎（含闹钟）", "tts.enabled"), ("播放日常对话语音", "tts.daily_enabled"), ("允许屏幕理解", "screen.enabled"),
            ("允许唱歌（需角色包支持）", "song.enabled"), ("启用追番功能", "anime.enabled"),
        ):
            checkbox = QCheckBox(label, self)
            self._register(key, checkbox)
            body_layout.addWidget(checkbox)
        self._hint(body_layout, "关闭日常对话语音后只显示文字，麦克风偏好不变，Home 闹钟仍可准备角色语音。关闭语音引擎则包括闹钟在内都不合成角色语音；文本角色包本身不提供声音。")
        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        body_layout.addLayout(form)
        self._combo(form, "陪玩插话", "galgame.reaction_mode", [
            ("关闭", "off"), ("少量", "low"), ("正常", "normal"), ("活跃", "high"),
        ])
        self._hint(body_layout, "插话仅在启动游戏陪玩会话后生效。")

        form = self._section(body_layout, "记忆整理")
        enabled = QCheckBox("按积累量自动整理记忆", self)
        self._register("memory.consolidation_enabled", enabled)
        form.addRow(enabled)
        self._text(form, "整理模型", "memory.consolidation_model", "同一 API 服务中的低成本模型 ID")
        for label, key, default in (("本人对话轮数", "consolidation_min_user_turns", 10),
                                    ("新材料 token 估算", "consolidation_min_tokens", 4000)):
            spin = QSpinBox(self)
            spin.setRange(1, 100000)
            spin.setValue(default)
            self._register("memory." + key, spin)
            form.addRow(label, spin)
        self._hint(body_layout, "原话立即保存在本机。达到任一门槛后等空闲整理，纠错可提前处理；少量材料最迟一天后整理。关闭时仍可在记忆页手动保存、修订和删除。整理模型沿用聊天服务地址与密钥，开启后可能产生费用。")

        self._add_home_settings(body_layout)

        self.advanced_button = QPushButton("▸ 高级选项", self)
        self.advanced_button.setCheckable(True)
        body_layout.addWidget(self.advanced_button)
        self.advanced = QWidget(self)
        advanced_layout = QVBoxLayout(self.advanced)
        advanced_layout.setContentsMargins(0, 0, 0, 0)
        form = self._section(advanced_layout, "本地识别设备")
        self._combo(form, "运行设备", "stt.device", [("NVIDIA GPU", "cuda"), ("CPU", "cpu"), ("自动", "auto")])
        self._combo(form, "计算精度", "stt.compute_type", [
            ("bfloat16", "bfloat16"), ("float16", "float16"), ("float32", "float32"),
        ])
        self._hint(advanced_layout, "CPU 使用 float32；较新 NVIDIA GPU 可选 bfloat16。请先安装独立 Qwen 环境并下载模型，程序不会在对话时下载。云端模式不需要这些本地资源。")
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
        elif isinstance(widget, (QSpinBox, QDoubleSpinBox)):
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
            self._set_value(widget, get_setting(snapshot["values"], key))
            widget.setEnabled(key not in overrides)
            widget.setToolTip(f"由 {overrides[key]} 覆盖，请先移除该配置来源。" if key in overrides else "")
        sources = {"inherited": "启动环境", "repo_dotenv": "本机配置", "parent_dotenv": "上级目录配置"}
        self.api_key_status.setText(
            f"已配置 · {sources.get(snapshot['api_key_source'], '本机')}；输入新值可替换，重启生效。"
            if snapshot["api_key_configured"] else "尚未配置 API Key；填写后点击“保存密钥”。"
        )
        if snapshot["api_key_source"] == "inherited":
            self.api_key_status.setText("API Key 由启动环境提供。请先移除该环境变量并重启，才能在此修改。")
        self.asr_key_status.setText(
            "百炼密钥由启动环境提供，请在启动环境中修改。" if snapshot.get("asr_key_source") == "inherited"
            else "百炼密钥已配置；更换后重启生效。" if snapshot.get("asr_key_configured")
            else "尚未配置百炼密钥。仅云端识别需要；留空不会清除原密钥。")
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
        elif isinstance(widget, (QSpinBox, QDoubleSpinBox)):
            widget.setValue(value * (widget.property("valueScale") or 1) if value is not None else widget.minimum())
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
            elif isinstance(widget, (QSpinBox, QDoubleSpinBox)):
                value = None if widget.property('nullable') and widget.value() < 0 else widget.value()
                if value is not None and widget.property('valueScale'):
                    value /= widget.property('valueScale')
                    if isinstance(widget, QSpinBox):
                        value = int(value)
            else:
                value = widget.text().strip()
                if key in {"llm.base_url", "stt.worker_python"}:
                    value = value or None
            set_setting(values, key, value)
        return values

    def patch(self):
        if self._snapshot is None:
            return {}
        changes = {}
        for key, value in setting_leaves(self.values()):
            if key not in self._snapshot['overrides'] and value != get_setting(self._snapshot['values'], key):
                set_setting(changes, key, value)
        return changes

    def update_device_choices(self, key, choices):
        widget = self.fields[key]
        value = get_setting(self.values(), key)
        widget.blockSignals(True)
        widget.clear()
        for label, identity in choices:
            widget.addItem(label, identity)
        index = widget.findData(value)
        if index < 0:
            widget.addItem("已选择的设备（当前不可用）", value)
            index = widget.count() - 1
        widget.setCurrentIndex(index)
        widget.blockSignals(False)

    def restore_defaults(self):
        if self._snapshot is None:
            return
        for key, widget in self.fields.items():
            if key not in self._snapshot["overrides"]:
                self._set_value(widget, get_setting(self._snapshot["defaults"], key))
        self._edited()

    def discard_changes(self):
        if self._snapshot is None:
            return
        self.api_key.clear()
        self.asr_key.clear()
        self.load_snapshot(self._snapshot)
        self.status.setText("已撤销未保存的修改。")

    def _update_stt_controls(self):
        if self._snapshot is None:
            return
        local = self.fields["stt.backend"].currentData() == "qwen_asr"
        for field in ("model", "worker_python", "device", "compute_type", "warmup_on_startup"):
            key = f"stt.{field}"
            self.fields[key].setEnabled(local and key not in self._snapshot["overrides"])
        for field in ("cloud_base_url", "cloud_model"):
            key = f"stt.{field}"
            self.fields[key].setEnabled(not local and key not in self._snapshot["overrides"])

    def _edited(self, *_):
        self._update_stt_controls()
        self.status.setText("有未保存的修改，保存后重启生效。")

    def set_busy(self, busy):
        self.body.setEnabled(not busy)
        self.save_button.setEnabled(not busy and self._snapshot is not None)
        self.discard_button.setEnabled(not busy and self._snapshot is not None)

    def has_unsaved_secret(self):
        return bool(self.api_key.text() or self.asr_key.text())

    def _checkbox(self, layout, label, key):
        widget = QCheckBox(label, self)
        self._register(key, widget)
        layout.addWidget(widget)

    def _number(self, form, label, key, minimum, maximum, *, decimals=None, nullable=False, scale=1):
        widget = QSpinBox(self) if decimals is None else QDoubleSpinBox(self)
        if decimals is not None:
            widget.setDecimals(decimals)
        widget.setRange(-1 if nullable else minimum, maximum)
        widget.setProperty("nullable", nullable)
        widget.setProperty("valueScale", scale)
        if nullable:
            widget.setSpecialValueText("未设置")
        if scale == 100:
            widget.setSuffix(" %")
        self._register(key, widget)
        form.addRow(label, widget)

    def _add_home_settings(self, parent_layout):
        from PySide6.QtWidgets import QGroupBox
        group = QGroupBox("Home · 可选家庭功能", self)
        parent_layout.addWidget(group)
        layout = QVBoxLayout(group)
        self._hint(layout, "Home 支持 Linux X11 和 Windows 的平台适配，人体检测需要 NVIDIA GPU。先保持停用，填写设备、准备模型并校准区域后再启用；不配置也能正常聊天。")
        self._checkbox(layout, "启用 Home", "home.enabled")
        self._checkbox(layout, "日常感知与亮屏", "home.daily_detection_enabled")
        form = self._section(layout, "网关与传感器")
        for label, key in (("MQTT 地址", "mqtt_host"), ("主题前缀", "mqtt_base_topic"),
                           ("人体存在设备名", "presence_device"), ("卧室门磁设备名", "door_device")):
            self._text(form, label, f"home.{key}")
        self._number(form, "MQTT 端口", "home.mqtt_port", 1, 65535)
        self._hint(layout, "设备名填写 Zigbee2MQTT 的 friendly_name。当前 MQTT 适配器不支持账号／TLS；请按指南部署在可信本机网络。门磁需仓库的 SNZB-04P 事件扩展。")
        form = self._section(layout, "相机与房间区域")
        self._combo(form, '相机', 'home.camera_device', [('请选择或填写稳定设备路径', '')], editable=True)
        for label, key, low, high in [('宽度', 'camera_width', 320, 3840), ('高度', 'camera_height', 240, 2160), ('帧率', 'camera_fps', 1, 30)]:
            self._number(form, label, 'home.'+key, low, high)
        self._combo(form, '叫醒／Home 音箱', 'home.output_device_id', [('使用日常输出', None), ('系统默认输出', '')])
        self._combo(form, 'Home 备用音箱', 'home.output_fallback_device_id', [('不启用备用音箱', '')])
        row = QHBoxLayout()
        for label, signal in [('刷新设备', self.refresh_devices_requested), ('预览相机', self.preview_requested), ('设置桌区／床区', self.calibrate_requested)]:
            button = QPushButton(label, self)
            button.clicked.connect(signal.emit)
            row.addWidget(button)
        layout.addLayout(row)
        self.camera_status = self._hint(layout, '保存相机设置后可拖框校准；不会自动拍照保存。')
        self._text(form, "模型目录", "home.model_directory")
        self._text(form, "区域／闹钟目录", "home.data_directory")
        self._hint(layout, "模型与区域目录相对项目根目录。此处提供相机预览和桌区／床区校准；更换相机或分辨率后重新校准。")
        form = self._section(layout, "目标显示器")
        for label, key, hint in (
            ("X11 DISPLAY", "display", "例如 :0；从本机桌面会话确认"),
            ("Xauthority 文件", "xauthority", "填写本机实际路径"),
            ("输出接口", "monitor_output", "例如 DP-1；用 xrandr --props 核对"),
            ("EDID 显示器名称", "monitor_name", "填写自己的显示器名称，不能照抄作者型号"),
            ("Windows 显示器 ID", "windows_monitor_id", "可选的 Windows 设备身份；Linux 输出接口单独保留"),
        ):
            self._text(form, label, f"home.{key}", hint)
        form = self._section(layout, "房间灯")
        self._text(form, "开灯手指地址", "home.lights.on_device", "0x 开头的 16 位小写 IEEE 地址")
        self._text(form, "关灯手指地址", "home.lights.off_device", "另一只手指的 IEEE 地址")
        self._number(form, "偏暗阈值（lux）", "home.lights.dark_lux", 0, 10000, decimals=1, nullable=True)
        self._hint(layout, "仅支持 TS0001_fingerbot 两只手指，两者都是全行程按下后回顶。保存设置不按压设备；安装行程与真实开关效果需本人验收。")
        self._checkbox(layout, "启用闹钟与叫醒", "home.wake.enabled")
        self._checkbox(layout, "晚安允许主机休眠（需先验证 RTC）", "home.wake.power_control_enabled")
        form = QFormLayout()
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        layout.addLayout(form)
        self._number(form, "休眠恢复准备时间（秒）", "home.wake.suspend_margin_seconds", 1, 300, decimals=0, nullable=True)
        self._number(form, "叫醒起始音量", "home.wake.initial_volume", 1, 100, decimals=0, scale=100)
        self._number(form, "叫醒最大音量", "home.wake.maximum_volume", 1, 100, decimals=0, scale=100)
        self._hint(layout, "先保持主机运行验证闹钟，再配置 deep／RTC。机箱、显卡、内存灯效有具体硬件兼容限制，本向导不自动开启或探测灯控。")

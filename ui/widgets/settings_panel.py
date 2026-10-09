from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import (
    QAbstractAnimation,
    QEvent,
    QEasingCurve,
    QPoint,
    QRectF,
    QSize,
    QVariantAnimation,
    Signal,
    Qt,
)
from PySide6.QtGui import QColor, QKeySequence, QLinearGradient, QPainter, QPainterPath, QPen, QShortcut
from PySide6.QtWidgets import (
    QAbstractSpinBox,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QSlider,
    QScrollArea,
    QSpinBox,
    QTabWidget,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from spica.conversation.character_loader import DEFAULT_INTERLOCUTOR_NAME
from ui.widgets.common import MAX_UI_SCALE, MIN_UI_SCALE, DEFAULT_DIALOGUE_OPACITY, scaled_px
from ui.widgets.icons import line_icon
from ui.widgets.package_combo_box import PackageComboBox
from ui.widgets.application_settings_page import ApplicationSettingsPage
from ui.widgets.memory_settings_page import MemorySettingsPage

# Shared motion language (2026-07-22): panel slides in from the right while
# fading, InOutCubic. Aesthetic constants, deliberately NOT configuration.
PANEL_OPEN_MS = 240
PANEL_CLOSE_MS = 200
PANEL_SLIDE_PX = 24


class SettingsPanel(QFrame):
    close_requested = Signal()
    exit_requested = Signal()
    restart_requested = Signal()
    opacity_changed = Signal(float)
    opacity_commit_requested = Signal()
    costume_changed = Signal(str)
    interlocutor_name_changed = Signal(str)
    scale_changed = Signal(float)
    overall_scale_changed = Signal(float)
    typing_speed_changed = Signal(float)
    voice_volume_changed = Signal(float)  # linear 0.0-1.0 (slider shows 0-100%)
    voice_volume_commit_requested = Signal()
    dialogue_visibility_changed = Signal(bool)  # persisted value: visible
    character_import_requested = Signal()
    character_remove_requested = Signal(str)
    dialogue_style_import_requested = Signal()
    dialogue_style_remove_requested = Signal(str)
    dialogue_style_changed = Signal(object)
    character_changed = Signal(str)
    character_export_requested = Signal(bool)
    voice_wake_enabled_changed = Signal(bool)
    voice_wake_words_changed = Signal(str)
    microphone_muted_changed = Signal(bool)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("settingsPanel")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAutoFillBackground(False)
        self._scale = 1.0
        self._character_busy = False
        self._application_busy = False
        self._memory_busy = False

        # The ONLY QGraphicsEffect in this subtree lives on the panel root
        # (children stay effect-free); it carries the open/close fade.
        self._opacity_effect = QGraphicsOpacityEffect(self)
        self._opacity_effect.setOpacity(1.0)
        self.setGraphicsEffect(self._opacity_effect)
        self._motion = QVariantAnimation(self)
        self._motion.setStartValue(0.0)
        self._motion.setEndValue(1.0)
        self._motion.setEasingCurve(QEasingCurve.Type.InOutCubic)
        self._motion.valueChanged.connect(self._on_motion_tick)
        self._motion.finished.connect(self._settle_motion_terminal)
        self._motion_direction: str | None = None
        self._motion_base_pos: QPoint | None = None
        self._motion_offset_from = 0.0
        self._motion_opacity_from = 1.0
        self._on_hidden: Callable[[], None] | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 16)
        layout.setSpacing(12)

        title = QLabel("设置", self)
        title.setObjectName("settingsTitle")
        header = QHBoxLayout()
        header.addWidget(title, 1)
        self.close_button = QPushButton(self)
        self.close_button.setObjectName("settingsCloseButton")
        self.close_button.setIcon(line_icon("close"))
        self.close_button.setToolTip("收起设置（Esc）")
        self.close_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.close_button.clicked.connect(self.close_requested.emit)
        header.addWidget(self.close_button)
        layout.addLayout(header)
        self._escape_shortcut = QShortcut(QKeySequence("Escape"), self)
        self._escape_shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
        self._escape_shortcut.activated.connect(self.close_requested.emit)

        self.scroll_area = QScrollArea(self)
        self.scroll_area.setObjectName("settingsScroll")
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        body = QWidget()
        body.setObjectName("settingsBody")
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(0, 0, 8, 0)
        body_layout.setSpacing(10)
        self.scroll_area.setWidget(body)
        self.tabs = QTabWidget(self)
        self.tabs.setObjectName("settingsTabs")
        self.tabs.addTab(self.scroll_area, "角色与外观")
        self.application_page = ApplicationSettingsPage(self)
        self.tabs.addTab(self.application_page, "应用设置")
        self.memory_page = MemorySettingsPage(self)
        self.memory_page.busy_changed.connect(self.set_memory_busy)
        self.tabs.addTab(self.memory_page, "记忆")
        from ui.widgets.home_status_page import HomeStatusPage
        self.home_status_page = HomeStatusPage(self)
        self.tabs.addTab(self.home_status_page, 'Home 状态')
        layout.addWidget(self.tabs, 1)

        self.microphone_muted_checkbox = QCheckBox("完全禁用麦克风（包括唤醒监听）", self)
        self.microphone_muted_checkbox.toggled.connect(self.microphone_muted_changed.emit)
        body_layout.addWidget(self.microphone_muted_checkbox)
        self.voice_wake_checkbox = QCheckBox("呼叫角色名字来唤醒", self)
        self.voice_wake_checkbox.toggled.connect(self.voice_wake_enabled_changed.emit)
        self.voice_wake_words_input = QLineEdit(self)
        self.voice_wake_words_input.setPlaceholderText("中文或英文读法；多个词用逗号分隔")
        self.voice_wake_words_input.editingFinished.connect(
            lambda: self.voice_wake_words_changed.emit(self.voice_wake_words_input.text()))
        body_layout.addWidget(self.voice_wake_checkbox)
        body_layout.addWidget(self.voice_wake_words_input)
        wake_hint = QLabel("需安装本地唤醒模型。回应结束后留 8 秒接话；没有说话就收麦，继续只监听角色名。也可说“结束对话”或“关闭麦克风”。", self)
        wake_hint.setWordWrap(True)
        wake_hint.setObjectName("settingsHint")
        body_layout.addWidget(wake_hint)

        self.character_box = PackageComboBox(self)
        self.character_box.removal_requested.connect(self.character_remove_requested.emit)
        self.character_box.activated.connect(lambda index: self.character_changed.emit(str(self.character_box.itemData(index))))
        self.character_import_button = QPushButton("导入角色文件夹", self)
        self.character_import_button.clicked.connect(self.character_import_requested.emit)
        self.character_share_button = QPushButton("导出分享包", self)
        self.character_share_button.clicked.connect(lambda: self.character_export_requested.emit(False))
        self.character_save_button = QPushButton("导出个人存档", self)
        self.character_save_button.clicked.connect(lambda: self.character_export_requested.emit(True))
        self.character_status = QLabel("选择角色后重启桌宠生效。", self)
        self.character_status.setWordWrap(True)
        body_layout.addWidget(QLabel("角色", self))
        body_layout.addWidget(self.character_box)
        body_layout.addWidget(self.character_import_button)
        export_row = QHBoxLayout()
        export_row.addWidget(self.character_share_button)
        export_row.addWidget(self.character_save_button)
        body_layout.addLayout(export_row)
        body_layout.addWidget(self.character_status)
        body_layout.addWidget(QLabel("对话框样式", self))
        self.dialogue_style_box = PackageComboBox(self)
        self.dialogue_style_box.removal_requested.connect(self.dialogue_style_remove_requested.emit)
        self.dialogue_style_box.activated.connect(
            lambda index: self.dialogue_style_changed.emit(self.dialogue_style_box.itemData(index))
        )
        self.dialogue_style_import_button = QPushButton("导入样式文件夹", self)
        self.dialogue_style_import_button.clicked.connect(self.dialogue_style_import_requested.emit)
        self.dialogue_style_status = QLabel("独立选择对话框；重启桌宠后生效。", self)
        self.dialogue_style_status.setWordWrap(True)
        body_layout.addWidget(self.dialogue_style_box)
        body_layout.addWidget(self.dialogue_style_import_button)
        body_layout.addWidget(self.dialogue_style_status)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
        form.setFormAlignment(Qt.AlignmentFlag.AlignTop)
        form.setHorizontalSpacing(10)
        form.setVerticalSpacing(11)
        self._form = form

        self.name_input = QLineEdit(self)
        self.name_input.setPlaceholderText(DEFAULT_INTERLOCUTOR_NAME)
        self.name_input.setToolTip("按回车或离开输入框时保存，重启桌宠后生效。")
        self.name_input.editingFinished.connect(self._emit_interlocutor_name)
        self.interlocutor_name_status = QLabel("修改称呼后需重启桌宠。", self)
        self.interlocutor_name_status.setWordWrap(True)

        self.costume_box = QComboBox(self)
        self.costume_box.activated.connect(self._select_costume_group)
        self.costume_variant_box = None
        self._costume_groups = {}
        self._costume_labels = {}
        self._costume_variants = {}
        self._selected_costume = None

        scale_row = QWidget(self)
        scale_row.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        scale_layout = QHBoxLayout(scale_row)
        scale_layout.setContentsMargins(0, 0, 0, 0)
        scale_layout.setSpacing(8)

        self.scale_slider = QSlider(Qt.Orientation.Horizontal, scale_row)
        self.scale_slider.setRange(50, 180)
        self.scale_slider.setSingleStep(5)
        self.scale_slider.setPageStep(10)

        self.scale_spin = QDoubleSpinBox(scale_row)
        self.scale_spin.setRange(0.5, 1.8)
        self.scale_spin.setSingleStep(0.05)
        self.scale_spin.setDecimals(2)

        self.scale_slider.valueChanged.connect(self._slider_changed)
        self.scale_spin.valueChanged.connect(self._spin_changed)

        scale_layout.addWidget(self.scale_slider, 1)
        scale_layout.addWidget(self.scale_spin)

        overall_row = QWidget(self)
        overall_row.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        overall_layout = QHBoxLayout(overall_row)
        overall_layout.setContentsMargins(0, 0, 0, 0)
        overall_layout.setSpacing(8)

        self.overall_slider = QSlider(Qt.Orientation.Horizontal, overall_row)
        self.overall_slider.setRange(round(MIN_UI_SCALE * 100), round(MAX_UI_SCALE * 100))
        self.overall_slider.setSingleStep(5)
        self.overall_slider.setPageStep(10)

        self.overall_spin = QDoubleSpinBox(overall_row)
        self.overall_spin.setRange(MIN_UI_SCALE, MAX_UI_SCALE)
        self.overall_spin.setSingleStep(0.05)
        self.overall_spin.setDecimals(2)

        self.overall_slider.valueChanged.connect(self._overall_slider_changed)
        self.overall_spin.valueChanged.connect(self._overall_spin_changed)

        overall_layout.addWidget(self.overall_slider, 1)
        overall_layout.addWidget(self.overall_spin)

        typing_row = QWidget(self)
        typing_row.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        typing_layout = QHBoxLayout(typing_row)
        typing_layout.setContentsMargins(0, 0, 0, 0)
        typing_layout.setSpacing(8)

        self.typing_speed_slider = QSlider(Qt.Orientation.Horizontal, typing_row)
        self.typing_speed_slider.setRange(50, 300)
        self.typing_speed_slider.setSingleStep(10)
        self.typing_speed_slider.setPageStep(25)

        self.typing_speed_spin = QDoubleSpinBox(typing_row)
        self.typing_speed_spin.setRange(0.5, 3.0)
        self.typing_speed_spin.setSingleStep(0.1)
        self.typing_speed_spin.setDecimals(2)
        self.typing_speed_spin.setSuffix("x")

        self.typing_speed_slider.valueChanged.connect(self._typing_speed_slider_changed)
        self.typing_speed_spin.valueChanged.connect(self._typing_speed_spin_changed)

        typing_layout.addWidget(self.typing_speed_slider, 1)
        typing_layout.addWidget(self.typing_speed_spin)

        volume_row = QWidget(self)
        volume_row.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        volume_layout = QHBoxLayout(volume_row)
        volume_layout.setContentsMargins(0, 0, 0, 0)
        volume_layout.setSpacing(8)

        # Slider + spin both in PERCENT (0-100%); the emitted signal is linear 0.0-1.0
        # (v1 linear mapping -- value/100). Default seeds to 86% == the historical
        # AudioController volume, so opening settings shows the current level.
        self.voice_volume_slider = QSlider(Qt.Orientation.Horizontal, volume_row)
        self.voice_volume_slider.setRange(0, 100)
        self.voice_volume_slider.setSingleStep(5)
        self.voice_volume_slider.setPageStep(10)

        self.voice_volume_spin = QDoubleSpinBox(volume_row)
        self.voice_volume_spin.setRange(0, 100)
        self.voice_volume_spin.setSingleStep(5)
        self.voice_volume_spin.setDecimals(0)
        self.voice_volume_spin.setSuffix("%")

        self.voice_volume_slider.valueChanged.connect(self._voice_volume_slider_changed)
        self.voice_volume_spin.valueChanged.connect(self._voice_volume_spin_changed)
        self.voice_volume_slider.sliderReleased.connect(
            self.voice_volume_commit_requested.emit
        )
        self.voice_volume_spin.editingFinished.connect(
            self.voice_volume_commit_requested.emit
        )

        volume_layout.addWidget(self.voice_volume_slider, 1)
        volume_layout.addWidget(self.voice_volume_spin)

        self.hide_dialogue_checkbox = QCheckBox("隐藏对话框", self)
        self.hide_dialogue_checkbox.toggled.connect(
            self._dialogue_hidden_changed
        )

        opacity_row = QWidget(self)
        opacity_layout = QHBoxLayout(opacity_row)
        opacity_layout.setContentsMargins(0, 0, 0, 0)
        opacity_layout.setSpacing(8)
        self.opacity_slider = QSlider(Qt.Orientation.Horizontal, opacity_row)
        self.opacity_slider.setRange(0, 80)
        self.opacity_spin = QDoubleSpinBox(opacity_row)
        self.opacity_spin.setRange(0, 80)
        self.opacity_spin.setDecimals(0)
        self.opacity_spin.setSuffix("%")
        self.opacity_slider.setToolTip("数值越大，框体越通透；文字和按钮保持清晰")
        self.opacity_slider.valueChanged.connect(self._opacity_slider_changed)
        self.opacity_spin.valueChanged.connect(self._opacity_spin_changed)
        self.opacity_slider.sliderReleased.connect(self.opacity_commit_requested.emit)
        self.opacity_spin.editingFinished.connect(self.opacity_commit_requested.emit)
        opacity_layout.addWidget(self.opacity_slider, 1)
        opacity_layout.addWidget(self.opacity_spin)
        self.set_opacity(DEFAULT_DIALOGUE_OPACITY)

        dialogue_form = QFormLayout()
        name_form = QFormLayout()
        self._forms = (form, dialogue_form, name_form)
        for heading, section in (("外观", form), ("对话与声音", dialogue_form), ("你的称呼", name_form)):
            label = QLabel(heading, self)
            label.setObjectName("settingsSection")
            body_layout.addWidget(label)
            body_layout.addLayout(section)
            section.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.addRow("服装", self.costume_box)
        form.addRow("立绘大小", scale_row)
        form.addRow("界面大小", overall_row)
        form.addRow("框体透明度", opacity_row)
        dialogue_form.addRow("文字出现速度", typing_row)
        dialogue_form.addRow("角色音量", volume_row)
        dialogue_form.addRow("台词显示", self.hide_dialogue_checkbox)
        name_form.addRow("如何称呼你", self.name_input)
        name_form.addRow(self.interlocutor_name_status)
        body_layout.addStretch(1)

        separator = QFrame(self)
        separator.setObjectName("settingsSeparator")
        separator.setFixedHeight(1)
        layout.addWidget(separator)
        actions = QHBoxLayout()
        actions.addStretch(1)
        self.restart_button = QPushButton("重启程序", self)
        self.restart_button.setObjectName("restartSpicaButton")
        self.restart_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.restart_button.setToolTip("重启桌面并载入已保存的角色、称呼和对话框样式")
        self.restart_button.clicked.connect(self.restart_requested.emit)
        actions.addWidget(self.restart_button)
        self.exit_button = QPushButton("退出 Spica", self)
        self.exit_button.setObjectName("exitSpicaButton")
        self.exit_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.exit_button.setToolTip("结束桌面端程序；仅需暂时隐藏时请使用最小化")
        self.exit_button.clicked.connect(self.exit_requested.emit)
        actions.addWidget(self.exit_button)
        layout.addLayout(actions)
        for editor in self.findChildren(QSlider) + self.findChildren(QDoubleSpinBox) + self.findChildren(QComboBox) + self.findChildren(QSpinBox):
            editor.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
            editor.installEventFilter(self)
        for spin in self.findChildren(QAbstractSpinBox):
            spin.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        self.apply_scale(1.0)

    # -- open/close motion ----------------------------------------------------

    def set_characters(self, characters: list[dict], selected: str | None) -> None:
        self.character_box.blockSignals(True)
        self.character_box.clear()
        for character in characters:
            label = character["name"] or character["character_id"]
            if character.get("version"):
                label += " · " + character["version"]
            self.character_box.add_package(label, character["dir"], removable=character.get("removable", False))
        index = self.character_box.findData(selected)
        self.character_box.setCurrentIndex(max(0, index))
        self.character_box.blockSignals(False)

    def set_voice_wake(self, enabled: bool, words: tuple[str, ...], muted: bool = False) -> None:
        self.microphone_muted_checkbox.blockSignals(True)
        self.microphone_muted_checkbox.setChecked(muted)
        self.microphone_muted_checkbox.blockSignals(False)
        self.voice_wake_checkbox.blockSignals(True)
        self.voice_wake_checkbox.setChecked(enabled)
        self.voice_wake_checkbox.blockSignals(False)
        if not self.voice_wake_words_input.hasFocus():
            self.voice_wake_words_input.setText("，".join(words))

    def set_character_busy(self, busy: bool) -> None:
        self._character_busy = busy
        self._update_settings_busy()

    def set_application_busy(self, busy: bool) -> None:
        self._application_busy = busy
        self._update_settings_busy()

    def set_memory_busy(self, busy: bool) -> None:
        self._memory_busy = busy
        self._update_settings_busy()

    @property
    def settings_busy(self) -> bool:
        return self._character_busy or self._application_busy or self._memory_busy

    def _update_settings_busy(self) -> None:
        busy = self.settings_busy
        self.application_page.set_busy(busy)
        self.memory_page.set_external_busy(self._character_busy or self._application_busy)
        self.name_input.setEnabled(not busy)
        self.microphone_muted_checkbox.setEnabled(not busy)
        self.voice_wake_checkbox.setEnabled(not busy)
        self.voice_wake_words_input.setEnabled(not busy)
        self.restart_button.setEnabled(not busy)
        self.exit_button.setEnabled(not busy)
        self.dialogue_style_box.setEnabled(not busy)
        self.dialogue_style_import_button.setEnabled(not busy)
        for widget in (self.character_box, self.character_import_button,
                       self.character_share_button, self.character_save_button):
            widget.setEnabled(not busy)

    def set_dialogue_styles(self, styles: list[dict], selected: str | None) -> None:
        self.dialogue_style_box.blockSignals(True)
        self.dialogue_style_box.clear()
        for style in styles:
            self.dialogue_style_box.add_package(style["name"], style["dir"], removable=style.get("removable", False))
        self.dialogue_style_box.setCurrentIndex(max(0, self.dialogue_style_box.findData(selected)))
        self.dialogue_style_box.blockSignals(False)

    @property
    def motion_animation(self) -> QVariantAnimation:
        return self._motion

    def play_open_motion(self) -> None:
        """Fade + slide in from the right of the already-laid-out geometry."""
        was_midflight = self._motion_base_pos is not None
        self._motion.stop()
        if not was_midflight:
            self._motion_base_pos = self.pos()
            self._opacity_effect.setOpacity(0.0)
            self.move(self._motion_base_pos + QPoint(PANEL_SLIDE_PX, 0))
        self._begin_motion("open", PANEL_OPEN_MS, None)

    def play_close_motion(self, on_hidden: Callable[[], None] | None = None) -> None:
        if self.isHidden():
            if on_hidden is not None:
                on_hidden()
            return
        self._motion.stop()
        if self._motion_base_pos is None:
            self._motion_base_pos = self.pos()
        self._begin_motion("close", PANEL_CLOSE_MS, on_hidden)

    def stop_motion_for_layout(self) -> None:
        """Layout wins: snap any in-flight open/close to its terminal state."""
        if self._motion.state() != QAbstractAnimation.State.Stopped:
            self._motion.stop()
            self._settle_motion_terminal()

    def _begin_motion(
        self,
        direction: str,
        duration_ms: int,
        on_hidden: Callable[[], None] | None,
    ) -> None:
        self._motion_direction = direction
        self._on_hidden = on_hidden
        base = self._motion_base_pos
        self._motion_offset_from = float(self.pos().x() - (base.x() if base else 0))
        self._motion_opacity_from = float(self._opacity_effect.opacity())
        self._motion.setDuration(max(1, int(duration_ms)))
        self._motion.start()

    def _on_motion_tick(self, value) -> None:
        base = self._motion_base_pos
        if base is None or self._motion_direction is None:
            return
        progress = max(0.0, min(1.0, float(value)))
        if self._motion_direction == "open":
            target_offset, target_opacity = 0.0, 1.0
        else:
            target_offset, target_opacity = float(PANEL_SLIDE_PX), 0.0
        offset = (
            self._motion_offset_from
            + (target_offset - self._motion_offset_from) * progress
        )
        opacity = (
            self._motion_opacity_from
            + (target_opacity - self._motion_opacity_from) * progress
        )
        self._opacity_effect.setOpacity(opacity)
        self.move(base.x() + round(offset), base.y())

    def _settle_motion_terminal(self) -> None:
        """Fail-open terminal: whatever interrupted us, land on a clean state."""
        direction = self._motion_direction
        base = self._motion_base_pos
        on_hidden = self._on_hidden
        self._motion_direction = None
        self._motion_base_pos = None
        self._on_hidden = None
        if base is not None:
            self.move(base)
        self._opacity_effect.setOpacity(1.0)
        if direction == "close":
            self.hide()
            if on_hidden is not None:
                on_hidden()

    # -- values ---------------------------------------------------------------

    def set_costume_enabled(self, enabled: bool) -> None:
        self.costume_box.setEnabled(enabled)
        self.costume_box.setToolTip("" if enabled else "请等本轮回复和语音结束后再切换服装。")
        if self.costume_variant_box is not None:
            self.costume_variant_box.setEnabled(enabled)
            self.costume_variant_box.setToolTip(self.costume_box.toolTip())


    def set_costumes(self, costumes: list[str], selected: str | None) -> None:
        self._selected_costume = selected
        self._costume_variants = {}
        self.costume_box.blockSignals(True)
        self.costume_box.clear()
        grouped = set()
        for label, members in self._costume_groups.items():
            variants = [key for key in members if key in costumes]
            if not variants:
                continue
            key = variants[0]
            self._costume_variants[key] = variants
            grouped.update(variants)
            self.costume_box.addItem(label, key)
        for costume in costumes:
            if costume not in grouped:
                self.costume_box.addItem(self._costume_labels.get(costume, costume), costume)
        key = next((key for key, variants in self._costume_variants.items() if selected in variants), selected)
        index = self.costume_box.findData(key)
        if index >= 0:
            self.costume_box.setCurrentIndex(index)
        key = self.costume_box.currentData()
        self._show_costume_variants(self._costume_variants.get(key, [key]) if key else [], selected)
        self.costume_box.blockSignals(False)


    def set_interlocutor_name(self, name: str) -> None:
        self.name_input.blockSignals(True)
        self.name_input.setText((name or DEFAULT_INTERLOCUTOR_NAME).strip() or DEFAULT_INTERLOCUTOR_NAME)
        self.name_input.blockSignals(False)

    def _emit_interlocutor_name(self) -> None:
        name = self.name_input.text().strip() or DEFAULT_INTERLOCUTOR_NAME
        self.name_input.setText(name)
        self.interlocutor_name_changed.emit(name)

    def set_scale(self, scale: float) -> None:
        value = max(0.5, min(1.8, float(scale)))
        self.scale_slider.blockSignals(True)
        self.scale_spin.blockSignals(True)
        self.scale_slider.setValue(round(value * 100))
        self.scale_spin.setValue(value)
        self.scale_slider.blockSignals(False)
        self.scale_spin.blockSignals(False)

    def _slider_changed(self, value: int) -> None:
        scale = value / 100
        self.scale_spin.blockSignals(True)
        self.scale_spin.setValue(scale)
        self.scale_spin.blockSignals(False)
        self.scale_changed.emit(scale)

    def _spin_changed(self, value: float) -> None:
        self.scale_slider.blockSignals(True)
        self.scale_slider.setValue(round(value * 100))
        self.scale_slider.blockSignals(False)
        self.scale_changed.emit(float(value))

    def set_overall_scale(self, scale: float) -> None:
        value = max(MIN_UI_SCALE, min(MAX_UI_SCALE, float(scale)))
        self.overall_slider.blockSignals(True)
        self.overall_spin.blockSignals(True)
        self.overall_slider.setValue(round(value * 100))
        self.overall_spin.setValue(value)
        self.overall_slider.blockSignals(False)
        self.overall_spin.blockSignals(False)

    def _overall_slider_changed(self, value: int) -> None:
        scale = value / 100
        self.overall_spin.blockSignals(True)
        self.overall_spin.setValue(scale)
        self.overall_spin.blockSignals(False)
        self.overall_scale_changed.emit(scale)

    def _overall_spin_changed(self, value: float) -> None:
        self.overall_slider.blockSignals(True)
        self.overall_slider.setValue(round(value * 100))
        self.overall_slider.blockSignals(False)
        self.overall_scale_changed.emit(float(value))

    def set_typing_speed(self, speed: float) -> None:
        value = max(0.5, min(3.0, float(speed)))
        self.typing_speed_slider.blockSignals(True)
        self.typing_speed_spin.blockSignals(True)
        self.typing_speed_slider.setValue(round(value * 100))
        self.typing_speed_spin.setValue(value)
        self.typing_speed_slider.blockSignals(False)
        self.typing_speed_spin.blockSignals(False)

    def _typing_speed_slider_changed(self, value: int) -> None:
        speed = value / 100
        self.typing_speed_spin.blockSignals(True)
        self.typing_speed_spin.setValue(speed)
        self.typing_speed_spin.blockSignals(False)
        self.typing_speed_changed.emit(speed)

    def _typing_speed_spin_changed(self, value: float) -> None:
        self.typing_speed_slider.blockSignals(True)
        self.typing_speed_slider.setValue(round(value * 100))
        self.typing_speed_slider.blockSignals(False)
        self.typing_speed_changed.emit(float(value))

    def set_voice_volume(self, volume: float) -> None:
        """Seed the slider/spin from a linear 0.0-1.0 volume (shown as 0-100%). No
        signal emission (blocked) -- this is initialisation, not a user edit."""
        percent = round(max(0.0, min(1.0, float(volume))) * 100)
        self.voice_volume_slider.blockSignals(True)
        self.voice_volume_spin.blockSignals(True)
        self.voice_volume_slider.setValue(percent)
        self.voice_volume_spin.setValue(percent)
        self.voice_volume_slider.blockSignals(False)
        self.voice_volume_spin.blockSignals(False)

    def _voice_volume_slider_changed(self, value: int) -> None:
        self.voice_volume_spin.blockSignals(True)
        self.voice_volume_spin.setValue(value)
        self.voice_volume_spin.blockSignals(False)
        self.voice_volume_changed.emit(value / 100)

    def _voice_volume_spin_changed(self, value: float) -> None:
        self.voice_volume_slider.blockSignals(True)
        self.voice_volume_slider.setValue(round(value))
        self.voice_volume_slider.blockSignals(False)
        self.voice_volume_changed.emit(value / 100)

    def set_dialogue_box_visible(self, visible: bool) -> None:
        """Seed from the visible key; the checkbox label expresses its inverse."""

        self.hide_dialogue_checkbox.blockSignals(True)
        self.hide_dialogue_checkbox.setChecked(not bool(visible))
        self.hide_dialogue_checkbox.blockSignals(False)

    def _dialogue_hidden_changed(self, hidden: bool) -> None:
        self.dialogue_visibility_changed.emit(not bool(hidden))

    def set_opacity(self, opacity: float) -> None:
        transparency = round((1 - float(opacity)) * 100)
        self.opacity_slider.blockSignals(True)
        self.opacity_spin.blockSignals(True)
        self.opacity_slider.setValue(transparency)
        self.opacity_spin.setValue(transparency)
        self.opacity_slider.blockSignals(False)
        self.opacity_spin.blockSignals(False)

    def _opacity_slider_changed(self, value: int) -> None:
        self.opacity_spin.blockSignals(True)
        self.opacity_spin.setValue(value)
        self.opacity_spin.blockSignals(False)
        self.opacity_changed.emit(1 - value / 100)

    def _opacity_spin_changed(self, value: float) -> None:
        self.opacity_slider.blockSignals(True)
        self.opacity_slider.setValue(round(value))
        self.opacity_slider.blockSignals(False)
        self.opacity_changed.emit(1 - value / 100)

    def eventFilter(self, watched, event) -> bool:  # noqa: N802
        if event.type() == QEvent.Type.Wheel:
            # Scrolling a settings page must not silently change clothes/volume.
            scroll = self.application_page.scroll_area if self.tabs.currentWidget() is self.application_page else self.scroll_area
            bar = scroll.verticalScrollBar()
            delta = event.pixelDelta().y()
            if not delta:
                delta = round(event.angleDelta().y() / 120 * bar.singleStep() * 3)
            bar.setValue(bar.value() - delta)
            event.accept()
            return True
        return super().eventFilter(watched, event)

    # -- appearance -----------------------------------------------------------

    def paintEvent(self, event) -> None:  # noqa: N802
        # Paint explicitly: translucent child frames under an opacity effect do
        # not reliably draw the stylesheet's panel background on every platform.
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        gradient = QLinearGradient(0, 0, 0, self.height())
        gradient.setColorAt(0, QColor(46, 70, 103, 248))
        gradient.setColorAt(1, QColor(37, 56, 83, 252))
        path = QPainterPath()
        radius = scaled_px(8, self._scale)
        path.addRoundedRect(QRectF(self.rect()), radius, radius)
        painter.fillPath(path, gradient)
        painter.setPen(QPen(QColor(220, 235, 250, 50), 0.7 * self._scale))
        inset = scaled_px(16, self._scale)
        painter.drawLine(inset, 1, self.width() - inset, 1)

    def _build_stylesheet(self, scale: float) -> str:
        font = scaled_px(13, scale)
        return f"""
            QFrame#settingsPanel, QWidget#settingsBody, QWidget#applicationSettings, QScrollArea#settingsScroll {{
                background: transparent; border: none;
            }}
            QTabWidget::pane {{ background: transparent; border: none; }}
            QTabBar::tab {{
                color: #AEC4DF; background: transparent;
                padding: {scaled_px(8, scale)}px {scaled_px(12, scale)}px;
                font-size: {font}px; border-bottom: 2px solid transparent;
            }}
            QTabBar::tab:selected {{ color: #F1F5FA; border-bottom-color: #F6A3C5; }}
            QLabel#settingsHint {{ color: #AEC4DF; font-size: {scaled_px(11, scale)}px; }}
            QLabel {{ background: transparent; color: #E4EDF7; font-size: {font}px; }}
            QLabel#settingsTitle {{ color: #F1F5FA; font-size: {scaled_px(17, scale)}px; }}
            QLabel#settingsSection {{
                color: #AEC4DF; font-size: {scaled_px(12, scale)}px;
                padding-top: {scaled_px(8, scale)}px;
                padding-bottom: {scaled_px(3, scale)}px;
            }}
            QComboBox, QLineEdit, QDoubleSpinBox, QSpinBox {{
                min-height: {scaled_px(28, scale)}px;
                border: 1px solid rgba(185, 211, 238, 65);
                border-radius: {scaled_px(5, scale)}px;
                background: rgba(16, 31, 51, 80);
                color: #E7EFF8;
                padding: 2px {scaled_px(6, scale)}px;
                font-size: {scaled_px(12, scale)}px;
                selection-background-color: #486A93;
                selection-color: #F4F8FC;
            }}
            QComboBox:focus, QLineEdit:focus, QDoubleSpinBox:focus {{
                border-color: #A4C4E7;
            }}
            QComboBox QAbstractItemView {{
                background: #2E4667; color: #E7EFF8;
                selection-background-color: #486A93;
                selection-color: #F4F8FC;
                border: 1px solid #91ABC9;
            }}
            QCheckBox {{ background: transparent; color: #E4EDF7; font-size: {font}px; spacing: 6px; }}
            QCheckBox::indicator {{
                width: {scaled_px(14, scale)}px; height: {scaled_px(14, scale)}px;
                border: 1px solid #91ABC9; border-radius: 3px;
                background: rgba(16, 31, 51, 80);
            }}
            QCheckBox::indicator:checked {{ background: #81ACDB; }}
            QCheckBox:focus {{ color: #F4F8FC; }}
            QSlider::groove:horizontal {{
                height: 3px; background: #4B6482; border-radius: 1px;
            }}
            QSlider::sub-page:horizontal {{ background: #92B9DF; }}
            QSlider::handle:horizontal {{
                width: {scaled_px(11, scale)}px; margin: -4px 0;
                border-radius: {scaled_px(6, scale)}px; background: #E8F2FC;
                border: 1px solid #A4C4E7;
            }}
            QPushButton {{
                min-height: {scaled_px(32, scale)}px; color: #E4EDF7;
                background: transparent; border: 1px solid transparent;
                border-radius: {scaled_px(8, scale)}px; padding: 0 8px; font-size: {font}px;
            }}
            QPushButton:hover, QPushButton:focus {{
                background: rgba(220, 236, 255, 28);
                border-color: rgba(220, 236, 255, 70);
            }}
            QPushButton:pressed {{ background: rgba(220, 236, 255, 50); }}
            QPushButton#restartSpicaButton {{ color: #F6A3C5; border-color: rgba(233, 78, 139, 85); }}
            QPushButton#restartSpicaButton:hover {{ background: rgba(233, 78, 139, 35); }}
            QPushButton#restartSpicaButton:disabled {{ color: #8E8591; border-color: transparent; }}
            QPushButton#exitSpicaButton {{ color: #F4D7DD; }}
            QPushButton#saveApplicationButton {{ color: #F6A3C5; border-color: rgba(233, 78, 139, 85); }}
            QPushButton#exitSpicaButton:hover {{ background: rgba(181, 127, 146, 36); }}
            QFrame#settingsSeparator {{ background: rgba(201, 219, 239, 35); border: none; }}
            QScrollBar:vertical {{
                width: {scaled_px(5, scale)}px; background: transparent; margin: 0;
            }}
            QScrollBar::handle:vertical {{ background: #7C97B7; min-height: 22px; border-radius: 2px; }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: transparent; }}
        """

    def apply_scale(self, scale: float) -> None:
        self._scale = scale
        self.setStyleSheet(self._build_stylesheet(scale))
        self.layout().setContentsMargins(scaled_px(16, scale), scaled_px(12, scale), scaled_px(16, scale), scaled_px(12, scale))
        self.layout().setSpacing(scaled_px(8, scale))
        self.close_button.setFixedSize(scaled_px(36, scale), scaled_px(36, scale))
        self.close_button.setIcon(line_icon("close", color="#DCE8F5"))
        self.close_button.setIconSize(QSize(scaled_px(18, scale), scaled_px(18, scale)))
        for form in self._forms:
            form.setHorizontalSpacing(scaled_px(12, scale))
            form.setVerticalSpacing(scaled_px(10, scale))
            for row in range(form.rowCount()):
                label = form.itemAt(row, QFormLayout.ItemRole.LabelRole)
                if label is not None:
                    label.widget().setFixedWidth(scaled_px(86, scale))
        for spin in self.findChildren(QDoubleSpinBox):
            spin.setFixedWidth(scaled_px(68, scale))

    def set_costume_groups(self, groups: dict[str, list[str]], labels: dict[str, str]) -> None:
        self._costume_groups = groups
        self._costume_labels = labels
        if groups and self.costume_variant_box is None:
            self.costume_variant_box = QComboBox(self)
            self.costume_variant_box.activated.connect(
                lambda _index: self.costume_changed.emit(self.costume_variant_box.currentData())
            )
            self.costume_variant_box.setEnabled(self.costume_box.isEnabled())
            self._form.insertRow(1, "造型", self.costume_variant_box)
            self.costume_variant_box.installEventFilter(self)


    def _select_costume_group(self, _index: int) -> None:
        key = self.costume_box.currentData()
        variants = self._costume_variants.get(key, [key])
        selected = self._selected_costume if self._selected_costume in variants else variants[0]
        self._show_costume_variants(variants, selected)
        if selected:
            self.costume_changed.emit(selected)


    def _show_costume_variants(self, variants, selected) -> None:
        box = self.costume_variant_box
        if box is None:
            return
        box.blockSignals(True)
        box.clear()
        for variant in variants:
            box.addItem(self._costume_labels.get(variant, variant), variant)
        box.setCurrentIndex(box.findData(selected))
        box.blockSignals(False)
        self._form.setRowVisible(box, len(variants) > 1)

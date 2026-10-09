"""Alarm presentation and editors; all scheduling decisions belong to Home."""
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from PySide6.QtCore import QDate, QTime, Qt, Signal
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import (
    QAbstractButton, QCheckBox, QComboBox, QDateEdit, QDialog, QFrame,
    QHBoxLayout, QLabel, QLineEdit, QPushButton, QScrollArea, QTimeEdit,
    QVBoxLayout, QWidget,
)

_DAYS = ('周一', '周二', '周三', '周四', '周五', '周六', '周日')
_ZONE = ZoneInfo('Asia/Shanghai')
_STYLE = """QDialog { background: #171e26; color: #edf3f8; font-size: 14px; }
QLabel { color: #edf3f8; background: transparent; }
QLabel[muted="true"] { color: #a8b5c3; }
QLabel#alarmTitle { font-size: 25px; font-weight: 600; }
QLabel#alarmNextTime { font-size: 44px; font-weight: 400; }
QLabel#alarmFixedTime { font-size: 32px; }
QLabel#alarmStatus { color: #a8cbbd; }
QFrame#alarmNext { background: #20342f; border: 1px solid #355a4c; border-radius: 16px; }
QFrame#alarmCard { background: #222c37; border: 1px solid #354252; border-radius: 13px; }
QPushButton { color: #dce9f6; background: #303e4e; border: 1px solid #46586b;
 border-radius: 8px; padding: 7px 12px; }
QPushButton:hover { background: #3b5064; }
QPushButton:disabled { color: #6c7a88; border-color: #34404c; background: #26313c; }
QPushButton#alarmPrimary { color: #10281e; background: #88d8b6; border: none; font-weight: 600; }
QPushButton#alarmDelete { color: #efadad; }
QLineEdit, QComboBox, QDateEdit, QTimeEdit { background: #222d39; color: #edf3f8;
 border: 1px solid #4b5e70; border-radius: 8px; padding: 8px; }
QTimeEdit { font-size: 36px; padding: 12px; }
QCheckBox { color: #dce9f6; padding: 4px; }
QScrollArea { border: none; background: transparent; }
QWidget#alarmRows { background: transparent; }
"""


def _date_text(epoch, now):
    local = datetime.fromtimestamp(epoch, _ZONE)
    today = datetime.fromtimestamp(now, _ZONE).date()
    delta = (local.date() - today).days
    day = '今天' if delta == 0 else '明天' if delta == 1 else local.strftime('%m月%d日')
    return f'{day} {_DAYS[local.weekday()]} {local:%H:%M}'


def _repeat_text(days):
    if days == [0, 1, 2, 3, 4]:
        return '周一至周五'
    if days == [5, 6]:
        return '周末'
    if days == list(range(7)):
        return '每天'
    return '、'.join(_DAYS[day] for day in days)


class AlarmSwitch(QAbstractButton):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setCheckable(True)
        self.setFixedSize(46, 26)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setOpacity(1 if self.isEnabled() else .4)
        painter.setBrush(QColor('#88d8b6' if self.isChecked() else '#465362'))
        painter.drawRoundedRect(self.rect(), 13, 13)
        painter.setBrush(QColor('#f4f8fb'))
        painter.drawEllipse(23 if self.isChecked() else 3, 3, 20, 20)
        if self.hasFocus():
            painter.setPen(QColor('#dce9f6'))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(self.rect().adjusted(1, 1, -1, -1), 12, 12)


class AlarmPanel(QDialog):
    fixed_requested = Signal()
    temporary_requested = Signal()
    operation_requested = Signal(str, object)
    edit_requested = Signal(object)
    hidden = Signal()

    def __init__(self, parent=None):
        super().__init__(parent, Qt.WindowType.Tool)
        self.setWindowTitle('闹钟')
        self.setStyleSheet(_STYLE)
        self.resize(450, 650)
        self.setMinimumSize(390, 420)
        self._snapshot = None
        self._rows_key = None
        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 18, 20, 18)
        outer.setSpacing(16)
        header = QHBoxLayout()
        title = QLabel('闹钟')
        title.setObjectName('alarmTitle')
        header.addWidget(title, 1)
        self.add_button = QPushButton('＋ 临时')
        self.add_button.setToolTip('新增一次临时叫醒')
        self.add_button.clicked.connect(self.temporary_requested.emit)
        header.addWidget(self.add_button)
        outer.addLayout(header)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        content = QWidget()
        content.setObjectName('alarmRows')
        body = QVBoxLayout(content)
        body.setContentsMargins(0, 0, 4, 0)
        body.setSpacing(14)
        self.scroll.setWidget(content)
        outer.addWidget(self.scroll, 1)
        next_card = QFrame()
        next_card.setObjectName('alarmNext')
        next_layout = QVBoxLayout(next_card)
        next_layout.setContentsMargins(18, 14, 18, 16)
        caption = QLabel('下一次叫醒')
        caption.setProperty('muted', True)
        self.next_time = QLabel('— — : — —')
        self.next_time.setObjectName('alarmNextTime')
        self.next_detail = QLabel('正在读取安排…')
        self.next_detail.setWordWrap(True)
        next_layout.addWidget(caption)
        next_layout.addWidget(self.next_time)
        next_layout.addWidget(self.next_detail)
        body.addWidget(next_card)
        fixed_card = QFrame()
        fixed_card.setObjectName('alarmCard')
        fixed_layout = QVBoxLayout(fixed_card)
        fixed_layout.setContentsMargins(16, 14, 16, 14)
        fixed_layout.addWidget(QLabel('固定作息'))
        row = QHBoxLayout()
        self.fixed_time = QLabel('未设置')
        self.fixed_time.setObjectName('alarmFixedTime')
        row.addWidget(self.fixed_time, 1)
        self.fixed_switch = AlarmSwitch()
        self.fixed_switch.setAccessibleName('固定作息开关')
        self.fixed_switch.clicked.connect(self._toggle_fixed)
        row.addWidget(self.fixed_switch)
        fixed_layout.addLayout(row)
        self.fixed_detail = QLabel('设置每天或每周的起床时间')
        self.fixed_detail.setWordWrap(True)
        self.fixed_detail.setProperty('muted', True)
        fixed_layout.addWidget(self.fixed_detail)
        controls = QHBoxLayout()
        self.fixed_edit_button = QPushButton('设置作息')
        self.fixed_edit_button.clicked.connect(self.fixed_requested.emit)
        self.move_button = QPushButton('改这次')
        self.move_button.clicked.connect(self._move_fixed)
        self.skip_button = QPushButton('跳过这次')
        self.skip_button.clicked.connect(self._skip_fixed)
        for button in (self.fixed_edit_button, self.move_button, self.skip_button):
            controls.addWidget(button)
        fixed_layout.addLayout(controls)
        self.adjustments = QVBoxLayout()
        fixed_layout.addLayout(self.adjustments)
        body.addWidget(fixed_card)
        body.addWidget(QLabel('临时叫醒'))
        self.temporary_rows = QVBoxLayout()
        self.temporary_rows.setSpacing(10)
        body.addLayout(self.temporary_rows)
        body.addStretch(1)
        self.active_status = QLabel()
        self.active_status.setWordWrap(True)
        outer.addWidget(self.active_status)
        self.status = QLabel('正在连接 Home…')
        self.status.setObjectName('alarmStatus')
        self.status.setWordWrap(True)
        outer.addWidget(self.status)
        self.set_busy(True)

    def hideEvent(self, event):
        self.hidden.emit()
        super().hideEvent(event)

    def set_busy(self, busy):
        ready = self._snapshot is not None and not busy
        fixed = self._snapshot.get('fixed') if self._snapshot else None
        self.add_button.setEnabled(ready)
        self.fixed_edit_button.setEnabled(ready and not self._snapshot.get('fixed_conflicts'))
        self.fixed_switch.setEnabled(ready and bool(fixed))
        self.move_button.setEnabled(ready and bool(fixed and fixed['next']))
        self.skip_button.setEnabled(ready and bool(fixed and fixed['next']))
        for layout in (self.temporary_rows, self.adjustments):
            for index in range(layout.count()):
                widget = layout.itemAt(index).widget()
                if widget:
                    widget.setEnabled(ready)

    def _toggle_fixed(self, checked):
        fixed = self._snapshot['fixed']
        self.fixed_switch.setChecked(fixed['enabled'])
        self.operation_requested.emit('set_enabled', {'schedule_id': fixed['id'], 'enabled': checked})

    def _move_fixed(self):
        self.edit_requested.emit(dict(self._snapshot['fixed']['next'], editor_kind='move'))

    def _skip_fixed(self):
        self.operation_requested.emit('skip', {'instance_id': self._snapshot['fixed']['next']['id']})

    @staticmethod
    def _clear(layout):
        while layout.count():
            item = layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

    def load_snapshot(self, value):
        self._snapshot = value
        now = value['now']
        upcoming, fixed = value['next'], value['fixed']
        self.next_time.setText(datetime.fromtimestamp(upcoming['due_at'], _ZONE).strftime('%H:%M') if upcoming else '暂无安排')
        self.next_detail.setText((_date_text(upcoming['due_at'], now) + ' · ' +
            ('固定作息的本次调整' if upcoming.get('adjustment') else '固定作息' if upcoming['kind'] == 'fixed' else '临时闹钟'))
            if upcoming else '可以设置固定作息，或添加一次临时叫醒')
        self.fixed_time.setText(fixed['local_time'][:5] if fixed else '未设置')
        self.fixed_switch.setChecked(bool(fixed and fixed['enabled']))
        self.fixed_edit_button.setText('编辑作息' if fixed else '设置作息')
        detail = _repeat_text(fixed['weekdays']) if fixed else '工作日、每天或自定义星期'
        if fixed:
            detail += (' · ' + fixed['label']) if fixed.get('label') else ''
            if not fixed['enabled']:
                detail += '\n固定作息已关闭，独立临时闹钟保留'
            elif fixed['next']:
                detail += '\n本次：' + _date_text(fixed['next']['due_at'], now)
        self.fixed_detail.setText(detail)
        if value['fixed_conflicts']:
            detail = '发现多个旧作息。选择保留一个后，其他旧作息停用，历史保留。'
            self.fixed_time.setText('选择作息')
            self.fixed_detail.setText(detail)
        rows_key = (value['revision'], datetime.fromtimestamp(now, _ZONE).date(),
                    tuple((v['id'], (v['next'] or {}).get('due_at'), v['enabled'],
                           v.get('can_enable'), (v.get('execution') or {}).get('state'),
                           bool(v.get('active'))) for v in value['temporary']),
                    tuple((v['id'], v['adjustment'], v['due_at'], v['effective']) for v in value['adjustments']))
        if rows_key != self._rows_key:
            self._rows_key = rows_key
            self._clear(self.temporary_rows)
            self._clear(self.adjustments)
            for item in value['fixed_conflicts']:
                choose = QPushButton('保留 ' + item['local_time'] + ' · ' + _repeat_text(item['weekdays']))
                choose.setToolTip('保留此作息，停用其他旧固定作息；历史保留')
                choose.clicked.connect(lambda _=False, identity=item['id']: self.operation_requested.emit('select_fixed', {'schedule_id': identity}))
                self.adjustments.addWidget(choose)
            for item in value['temporary']:
                self._temporary_row(item, now)
            if not value['temporary']:
                empty = QLabel('没有临时闹钟')
                empty.setProperty('muted', True)
                self.temporary_rows.addWidget(empty)
            for item in value['adjustments']:
                row = QWidget()
                layout = QHBoxLayout(row)
                layout.setContentsMargins(0, 4, 0, 0)
                label = QLabel(_date_text(item['due_at'], now) + (' · 已跳过' if item['adjustment'] == 'skip' else ' · 本次调整')
                               + ('' if item['effective'] else '（作息关闭／星期未选，当前不生效）'))
                label.setWordWrap(True)
                layout.addWidget(label, 1)
                restore = QPushButton('恢复固定时间')
                restore.clicked.connect(lambda _=False, identity=item['id']: self.operation_requested.emit('restore', {'instance_id': identity}))
                layout.addWidget(restore)
                self.adjustments.addWidget(row)
        active = value['active']
        notices = []
        if active:
            notices.append('正在叫醒；修改开关只影响之后的安排。本次持续离床或满十分钟结束。')
        elif value.get('attention'):
            notices.append('有未完成的叫醒已暂停，请向 Sana 查询原因；需要时可明确说“继续叫醒”。')
        bedtime = value.get('bedtime') or {}
        sync = bedtime.get('rtc_sync') or {}
        if sync.get('status') == 'pending':
            notices.append('闹钟已保存，正在同步晚安自动唤醒时间…')
        elif sync.get('status') == 'unconfirmed':
            notices.append('闹钟已保存，自动唤醒尚未确认：' + sync.get('reason', '请查询晚安状态'))
        elif sync.get('status') == 'verified':
            notices.append('晚安自动唤醒时间已核对；不代表已经挂起或保证实际叫醒。')
        elif sync.get('status') == 'not_required':
            notices.append(sync.get('reason', '本次保持开机'))
        self.active_status.setText('\n'.join(notices))
        self.set_busy(False)

    def _temporary_row(self, item, now):
        card = QFrame()
        card.setObjectName('alarmCard')
        layout = QVBoxLayout(card)
        top = QHBoxLayout()
        text = QLabel(item['local_time'][:5])
        text.setStyleSheet('font-size: 28px;')
        top.addWidget(text, 1)
        toggle = AlarmSwitch()
        toggle.setAccessibleName((item.get('label') or '临时闹钟') + '开关')
        enabled = bool(item['enabled'] and item['next'])
        toggle.setChecked(enabled or bool(item.get('active')))
        toggle.setEnabled(not item.get('active'))
        def toggle_item(checked):
            toggle.setChecked(enabled)
            action = 'rearm' if checked and not item.get('can_enable') else 'set_enabled'
            arguments = {'schedule_id': item['id']}
            if action == 'set_enabled':
                arguments['enabled'] = checked
            self.operation_requested.emit(action, arguments)
        toggle.clicked.connect(toggle_item)
        top.addWidget(toggle)
        layout.addLayout(top)
        execution = item.get('execution') or {}
        if item.get('active'):
            detail = '叫醒已暂停，计时未结束' if execution.get('state') == 'paused' else '正在叫醒'
        elif execution.get('state') == 'paused':
            detail = '叫醒已暂停，可查询原因或重新设置'
        elif execution:
            detail = '正在准备叫醒'
        elif item['next']:
            detail = _date_text(item['next']['due_at'], now)
        elif item.get('can_enable'):
            detail = '已关闭 · 原定 ' + _date_text(item['scheduled_for'], now)
        else:
            detail = '本次已结束 · 可重新设置'
        details = QLabel((item.get('label') or '临时叫醒') + ' · ' + detail)
        details.setProperty('muted', True)
        details.setWordWrap(True)
        layout.addWidget(details)
        controls = QHBoxLayout()
        edit = QPushButton('编辑' if item['next'] or item.get('can_enable') else '重新设置')
        edit.clicked.connect(lambda: self.edit_requested.emit(dict(item, editor_kind='temporary')))
        delete = QPushButton('删除')
        delete.setObjectName('alarmDelete')
        delete.clicked.connect(lambda: self.operation_requested.emit('delete', {'schedule_id': item['id']}))
        controls.addWidget(edit)
        controls.addStretch(1)
        controls.addWidget(delete)
        layout.addLayout(controls)
        self.temporary_rows.addWidget(card)


class AlarmEditor(QDialog):
    submitted = Signal(object)

    def __init__(self, parent, *, kind, now, value=None):
        super().__init__(parent)
        self.kind, self.value = kind, value or {}
        self.setWindowTitle('固定作息' if kind == 'fixed' else '修改这次' if kind == 'move' else '临时叫醒')
        self.setStyleSheet(_STYLE)
        self.setMinimumWidth(370)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 20)
        layout.setSpacing(14)
        self.time_edit = QTimeEdit()
        self.time_edit.setDisplayFormat('HH:mm')
        self.time_edit.setWrapping(True)
        raw_time = self.value.get('local_time', '08:25')
        due = None if kind == 'fixed' else (self.value.get('due_at') or (self.value.get('next') or {}).get('due_at')
                    or (self.value.get('scheduled_for') if self.value.get('can_enable') else None))
        self._original_due = due
        local = datetime.fromtimestamp(due or now, _ZONE)
        if kind != 'fixed' and not due:
            if self.value:
                local = datetime.combine(local.date(), time.fromisoformat(raw_time), _ZONE)
                if local.timestamp() <= now:
                    local += timedelta(days=1)
            else:
                local = datetime.fromtimestamp(now+300, _ZONE).replace(second=0, microsecond=0)
            raw_time = local.strftime('%H:%M')
        if due:
            raw_time = local.strftime('%H:%M')
        self.time_edit.setTime(QTime.fromString(raw_time[:5], 'HH:mm'))
        layout.addWidget(self.time_edit)
        self.date_edit = QDateEdit()
        self.date_edit.setCalendarPopup(True)
        self.date_edit.setDisplayFormat('yyyy年MM月dd日 ddd')
        self.date_edit.setDate(QDate(local.year, local.month, local.day))
        today = datetime.fromtimestamp(now, _ZONE)
        self.date_edit.setMinimumDate(QDate(today.year, today.month, today.day))
        if kind != 'fixed':
            layout.addWidget(QLabel('仅此一次 · 北京时间'))
            layout.addWidget(self.date_edit)
        self.label_edit = QLineEdit(self.value.get('label', ''))
        self.label_edit.setMaxLength(80)
        self.label_edit.setPlaceholderText('标签（可选）')
        if kind != 'move':
            layout.addWidget(self.label_edit)
        self.days = []
        if kind == 'fixed':
            layout.addWidget(QLabel('每周重复'))
            preset = QComboBox()
            preset.addItems(['周一至周五', '周末', '每天', '自定义'])
            layout.addWidget(preset)
            day_row = QHBoxLayout()
            selected = self.value.get('weekdays', [0, 1, 2, 3, 4])
            for index, name in enumerate(_DAYS):
                checkbox = QCheckBox(name[-1])
                checkbox.setToolTip(name)
                checkbox.setChecked(index in selected)
                self.days.append(checkbox)
                day_row.addWidget(checkbox)
            layout.addLayout(day_row)
            presets = ([0, 1, 2, 3, 4], [5, 6], list(range(7)))
            preset.setCurrentIndex(next((i for i, days in enumerate(presets) if days == selected), 3))
            def choose(index):
                if index < 3:
                    for day, checkbox in enumerate(self.days):
                        checkbox.setChecked(day in presets[index])
            preset.activated.connect(choose)
            for checkbox in self.days:
                checkbox.clicked.connect(lambda _: preset.setCurrentIndex(3))
        self.status = QLabel('修改长期时间会保留已明确的本次调整。' if kind == 'fixed' else '保存前请确认日期和时间。')
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        controls = QHBoxLayout()
        cancel = QPushButton('取消')
        cancel.clicked.connect(self.reject)
        self.save_button = QPushButton('保存')
        self.save_button.setObjectName('alarmPrimary')
        self.save_button.clicked.connect(self._submit)
        controls.addWidget(cancel)
        controls.addWidget(self.save_button)
        layout.addLayout(controls)

    def _submit(self):
        if self.kind == 'fixed':
            values = dict(local_time=self.time_edit.time().toString('HH:mm'),
                          weekdays=[i for i, item in enumerate(self.days) if item.isChecked()], label=self.label_edit.text())
            if not values['weekdays']:
                self.status.setText('至少选择一个星期；暂停作息请使用开关。')
                return
        else:
            day = self.date_edit.date().toPython()
            selected = self.time_edit.time().toPython()
            due = datetime.combine(day, selected, _ZONE).timestamp()
            if self._original_due is not None:
                original = datetime.fromtimestamp(self._original_due, _ZONE)
                if (day, selected.hour, selected.minute) == (original.date(), original.hour, original.minute):
                    due = self._original_due  # A label-only edit preserves relative-alarm seconds.
            values = dict(due_at=due, label=self.label_edit.text())
        self.submitted.emit(values)

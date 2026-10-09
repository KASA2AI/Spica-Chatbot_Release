"""Read-only presentation of Home's existing runtime and action receipts."""
from datetime import datetime
import time
from zoneinfo import ZoneInfo

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QScrollArea, QVBoxLayout, QWidget

from ui.widgets.settings_sections import section_form


_ZONE = ZoneInfo('Asia/Shanghai')
_LABELS = {
    'connected': '连接正常', 'starting': '正在连接', 'disconnected': '已断开',
    'unavailable': '当前不可用', 'bridge_offline': '网关离线', 'resumed': '恢复后等待新上报',
    'cache_filter_required': '传感器缓存过滤未配置', 'observed': '有效画面',
    'no_person': '画面中未检出人', 'no_observation': '尚无观察', 'no_fresh_frame': '没有有效新画面',
    'stale_frame': '画面已过期', 'too_dark': '画面过暗', 'calibration_required': '需要设置桌区／床区',
    'camera_preview': '相机用于预览／校准', 'camera_starting': '相机启动中',
    'camera_source_changed': '相机来源已变化', 'home_failed': 'Home 运行异常', 'home_stopped': 'Home 已停止',
    'scheduled': '等待到点', 'preparing': '准备叫醒', 'calling': '正在叫醒',
    'paused': '已暂停', 'completed': '已结束', 'cancelled': '已取消', 'snoozed': '已延后',
    'outside_occupied_at_start': '起播前已确认床外有人', 'bed_empty_with_person_outside': '床区无人且床外持续有人',
    'duration_limit': '已达到叫醒时长上限', 'generation_failed_retry': '生成失败，等待重试',
    'awaiting_region_confirmation': '等待区域连续确认', 'first_sound_deadline': '期限内未确认实际起播',
    'failed': '未完成', 'core_restarted': '核心重启', 'owner_cancelled': '本人取消',
    'already_on': '系统报告屏幕已亮', 'wake_requested': '已请求亮屏，尚无物理亮屏回执',
    'already_off': '系统报告屏幕已灭', 'blank_requested': '已请求息屏',
    'pressed': '按压已确认', 'unconfirmed': '结果尚未确认', 'not_ready': '设备尚未就绪',
    'manual': '本人操作', 'vacant': '持续无人', 'bedtime': '晚安', 'wake_alarm': '到点叫醒',
    'occupancy': '人体存在／画面有人', 'door_open': '新开门', 'settings_preview': '设置预览／校准',
    'arming': '正在准备晚安', 'waiting_reply': '等待晚安回复结束', 'awaiting_alarm': '等待确认起床安排',
    'retiming': '正在同步自动恢复时间', 'suspending': '准备休眠',
    'suspend_requested': '已请求休眠，实际状态待确认', 'suspend_unknown': '休眠结果未知',
    'night': '晚安模式', 'morning_preparation': '晨间准备', 'recovering': '恢复处理中',
    'cancelling': '正在退出晚安',
    'pending': '等待执行', 'skipped': '已跳过', 'busy': '当前有活动，跳过本次现场发言',
    'desktop_inactive': '电脑不是当前活动端', 'daily_suppressed': '日常场景暂不可执行',
    'presentation_unavailable': '现场播报不可用', 'started': '已收到实际起播回执',
    'delivered': '已确认送达／播放', 'stopped': '已停止', 'unknown': '结果未知',
    'ready': '准备就绪', 'claimed': '已接纳，等待结果', 'expired': '已过期',
    'requested': '已发出按压请求',
    'receipt_unavailable': '回执不可读，执行结果未知', 'audio_start_unconfirmed': '未收到实际起播回执',
}


def _reason(value):
    if not value:
        return ''
    if str(value).startswith('output_unconfirmed:'):
        return '播放未确认：' + _reason(str(value).split(':', 1)[1])
    return _LABELS.get(str(value), str(value))


def _when(value):
    if value is None:
        return '无时间记录'
    return datetime.fromtimestamp(value, _ZONE).strftime('%m-%d %H:%M:%S')


def _detected(value):
    return '检测到人' if value is True else '未检出人' if value is False else '未知'


class HomeStatusPage(QWidget):
    visibility_changed = Signal(bool)
    refresh_requested = Signal()
    alarms_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 10, 0, 0)
        toolbar = QHBoxLayout()
        hint = QLabel('当前运行状态 · 本页打开时每 2 秒刷新', self)
        hint.setObjectName('settingsHint')
        hint.setWordWrap(True)
        toolbar.addWidget(hint, 1)
        self.refresh_button = QPushButton('刷新', self)
        self.refresh_button.clicked.connect(self.refresh_requested.emit)
        toolbar.addWidget(self.refresh_button)
        outer.addLayout(toolbar)
        self.scroll_area = QScrollArea(self)
        self.scroll_area.setObjectName('settingsScroll')
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        body = QWidget()
        body.setObjectName('settingsBody')
        layout = QVBoxLayout(body)
        layout.setContentsMargins(0, 6, 6, 0)
        layout.setSpacing(12)
        self.values = {}
        for title, rows in (
            ('运行与叫醒', (('mode', '当前模式'), ('next', '下次叫醒'), ('output', 'Home 音箱'))),
            ('房间感知', (('connection', '网关'), ('presence', '存在传感器'), ('door', '卧室门'),
                       ('lux', '环境亮度'), ('camera', '相机'), ('regions', '区域判断'))),
            ('最近执行记录', (('display', '亮屏'), ('light', '房间灯'), ('wake', '叫醒'),
                          ('bedtime', '晚安'), ('welcome', '欢迎'), ('farewell', '告别'))),
        ):
            form = section_form(layout, title)
            for key, label in rows:
                value = QLabel('—')
                value.setTextFormat(Qt.TextFormat.PlainText)
                value.setWordWrap(True)
                value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
                form.addRow(label, value)
                self.values[key] = value
        self.values['welcome'].setToolTip('本次核心启动以来的最近一次欢迎触发；未触发不代表已经播报。')
        self.values['display'].setToolTip('本次核心启动以来的最近一次亮屏结果。')
        layout.addStretch(1)
        self.scroll_area.setWidget(body)
        outer.addWidget(self.scroll_area, 1)
        self.status = QLabel('正在读取 Home 状态…', self)
        self.status.setObjectName('settingsStatus')
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        outer.addWidget(self.status)
        alarms = QPushButton('查看闹钟安排', self)
        alarms.setProperty('intent', 'quiet')
        alarms.clicked.connect(self.alarms_requested.emit)
        outer.addWidget(alarms)

    def showEvent(self, event):
        super().showEvent(event)
        self.visibility_changed.emit(True)

    def hideEvent(self, event):
        self.visibility_changed.emit(False)
        super().hideEvent(event)

    def unavailable(self, message):
        for value in self.values.values():
            value.setText('—')
        self.values['mode'].setText(message)
        self.status.setText(message)

    def load_snapshot(self, value, audio, *, now=None):
        now = time.time() if now is None else now
        updated = value.get('updated_at')
        if updated is None:
            self.unavailable('核心尚未提供完整状态，请重新加载核心和桌面。')
            return
        stale = not 0 <= now - updated <= 5
        wake = value.get('wake') or {}
        bedtime = wake.get('bedtime') or {}
        instances = wake.get('instances') or []
        active = next((v for v in instances if v['state'] in {'calling', 'preparing'}), None)
        if not value.get('running'):
            mode = 'Home 未运行'
        elif wake.get('error') or value.get('resume_observation_error'):
            mode = '运行异常：' + _reason(wake.get('error') or value['resume_observation_error'])
        elif active:
            mode = _reason(active['state'])
        elif bedtime and bedtime['state'] not in {'cancelled', 'resumed', 'failed'}:
            mode = _reason(bedtime['state'])
        elif value.get('camera_preview'):
            mode = '相机预览／校准中'
        else:
            mode = '日常运行' if value.get('daily_detection_enabled') else '日常感知已关闭'
        self.values['mode'].setText(('上次状态：' if stale else '') + mode)
        alarm = value.get('next_alarm')
        next_text = (_when(alarm['due_at']) + ' · ' + ('固定作息' if alarm.get('kind') == 'fixed' else '临时闹钟')) if alarm else '暂无后续安排'
        if not wake.get('enabled'):
            next_text += '\n叫醒功能未启用'
        if value.get('alarm_conflicts'):
            next_text += '\n存在多个旧作息，请在闹钟页确认'
        self.values['next'].setText(next_text)
        self.values['output'].setText((audio['name'] + (' · 将使用备用' if audio.get('fallback') else
            ' · 跟随系统默认' if audio.get('system_default') else ' · 固定设备'))
            if audio.get('available') else audio.get('detail', '输出状态未知'))
        self.values['connection'].setText(('上次：' if stale else '') + _reason(value.get('connection', '未知')))
        for name, key, text in (
            ('presence', 'presence', _detected(value.get('room_occupied'))),
            ('door', 'door', '关闭' if value.get('door_contact') is True else '打开' if value.get('door_contact') is False else '未知'),
            ('illuminance', 'lux', f"{value['illuminance']:g} lux" if value.get('illuminance') is not None else '未知'),
        ):
            sensor = (value.get('sensors') or {}).get(name)
            if stale or not sensor or now > sensor['valid_until']:
                text = '未知（上报过期）' if sensor else '尚无有效上报'
            self.values[key].setText(text + ('\n最近上报 ' + _when(sensor['observed_at']) if sensor else ''))
        reasons = '、'.join(_reason(reason) for reason in value.get('camera_reasons', []))
        camera = '正在采集' if value.get('camera_running') else '未采集'
        self.values['camera'].setText(('上次：' if stale else '') + camera + (' · ' + reasons if reasons else '')
            + ('\n' + _reason(value['camera_error']) if value.get('camera_error') else ''))
        room = value.get('room') or {}
        self.values['regions'].setText('状态过期，等待新观察' if stale else
            '桌区：' + _detected(room.get('desk_occupied')) + ' · 床区：' + _detected(room.get('bed_occupied'))
            + '\n床外：' + _detected(room.get('outside_bed_occupied')) + ' · ' + _reason(room.get('reason')))
        display = value.get('display_result') or {}
        self.values['display'].setText(_reason(display.get('status')) +
            ('\n' + display['detail'] if display.get('detail') and display.get('status') == 'unavailable' else '') if display else '暂无亮屏结果')
        light = next((item for item in value.get('commands', []) if item.get('kind') == 'light'), None)
        self.values['light'].setText((_when(light.get('requested_at')) + ' · ' + ('开灯' if light['action'] == 'on' else '关灯')
            + ' · ' + _reason(light.get('source')) + '\n' + _reason(light.get('press_status')) + '；房间实际灯态未确认')
            if light else '暂无灯控记录')
        last = wake.get('last_execution')
        self.values['wake'].setText((_when(last['due_at']) + ' · ' + _reason(last['state'])
            + ('\n' + _reason(last['reason']) if last.get('reason') else '')
            + ('\n实际起播 ' + _when(last['first_sound_at']) if last.get('first_sound_at') is not None else '\n尚无实际起播回执'))
            if last else '暂无到点执行记录')
        self.values['bedtime'].setText((_reason(bedtime['state']) + ('\n' + _reason(bedtime['reason']) if bedtime.get('reason') else ''))
            if bedtime else '暂无晚安记录')
        greetings = value.get('greetings') or {}
        for key, label in (('welcome', '欢迎'), ('farewell', '告别')):
            receipt = greetings.get(key)
            text = '暂无' + label + '触发记录'
            if receipt:
                text = _when(receipt.get('at', receipt.get('trigger_at'))) + ' · ' + _reason(receipt.get('state'))
                if key == 'farewell':
                    text += ' · ' + ('QQ 文字' if receipt.get('channel') == 'qq' else '现场语音')
                if receipt.get('reason'):
                    text += '\n' + _reason(receipt['reason'])
                if receipt.get('actual_started_at') is not None:
                    text += '\n实际起播 ' + _when(receipt['actual_started_at'])
            self.values[key].setText(text)
        self.status.setText(('后台状态未更新，以上感知信息已过期。' if stale else '已更新 · ')
            + _when(updated) + ' · 音箱可用不代表现场已听到声音。')

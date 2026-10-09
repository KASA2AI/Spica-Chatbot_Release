"""Resolve owner dialogue time arguments; persistence and execution stay in Home."""
from datetime import date, datetime, time, timedelta
import math
import re
from zoneinfo import ZoneInfo

HOME_ZONE = ZoneInfo('Asia/Shanghai')


def spoken_day(text, requested_at, on_date=None):
    today = datetime.fromtimestamp(requested_at, HOME_ZONE).date()
    implied = None
    for words, offset in ((('后天',), 2), (('明天', '明早', '明晚'), 1), (('今天', '今晚', '今早'), 0)):
        if any(word in text for word in words):
            implied = today + timedelta(days=offset)
            break
    selected = date.fromisoformat(on_date) if on_date is not None else implied
    if implied is not None and selected != implied:
        raise ValueError('指定日期与本人说的日期不同，请确认')
    return selected


def spoken_due(*, text, requested_at, now, local_time=None, on_date=None, after_minutes=None):
    reference = datetime.fromtimestamp(requested_at, HOME_ZONE)
    if after_minutes is not None:
        if local_time is not None or on_date is not None:
            raise ValueError('请指定一个叫醒时刻或一段时长，不要混用')
        if type(after_minutes) not in (int, float) or not math.isfinite(after_minutes) or not 0 < after_minutes <= 1440:
            raise ValueError('语音只能设置未来 24 小时内的单次叫醒')
        due = requested_at + after_minutes * 60
    else:
        if not isinstance(local_time, str) or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d(?::[0-5]\d)?', local_time):
            raise ValueError('请明确上午／晚上及具体叫醒时间')
        selected = spoken_day(text, requested_at, on_date)
        day = selected or reference.date()
        due = datetime.combine(day, time.fromisoformat(local_time), HOME_ZONE).timestamp()
        if due <= requested_at and selected is None:
            due = datetime.combine(day + timedelta(days=1), time.fromisoformat(local_time), HOME_ZONE).timestamp()
    if due <= now:
        raise ValueError('这个叫醒时刻已经过去，未顺延或补响')
    if due > requested_at + 86400:
        raise ValueError('语音只能设置未来 24 小时内的单次叫醒；长期作息请在闹钟面板设置')
    return math.ceil(due)


def set_command(plan, *, due, text, latest, after_minutes=None, additional=False, label=''):
    day = datetime.fromtimestamp(due, HOME_ZONE).date().isoformat()
    separate = additional or after_minutes is not None or any(word in text for word in ('另外', '再加', '新增', '另加'))
    fixed = plan['fixed']
    if not separate and plan.get('fixed_conflicts'):
        raise ValueError('存在多个旧固定作息，请先在闹钟面板选择；明确另外新增临时闹钟仍可用')
    if not separate and fixed and fixed['enabled']:
        instance = next((value for value in plan['upcoming']
                         if value['schedule_id'] == fixed['id'] and value['day'] == day), None)
        if instance is not None:
            if instance['due_at'] > latest:
                raise ValueError('这次固定安排在 24 小时以外，请在闹钟面板修改；未新增临时闹钟')
            return 'move', dict(instance_id=instance['id'], due_at=due)
    return 'add_temporary', dict(due_at=due, label=label)


def adjustment_target(plan, *, action, instance_id, on_date, text, requested_at, now):
    selected_day = spoken_day(text, requested_at, on_date)
    known = {value['id']: value for value in plan['adjustments'] + plan['upcoming'] + plan['active']}
    if instance_id is not None:
        candidates = [known[instance_id]] if instance_id in known else []
    elif action == 'restore':
        candidates = plan['adjustments']
    elif selected_day is not None:
        candidates = plan['upcoming'] + plan['active']
    else:
        candidates = ([plan['next']] if plan['next'] else []) if any(
            word in text for word in ('下一次', '下一个', '最近', '这次')) else plan['upcoming'] + plan['active']
    if selected_day is not None:
        candidates = [v for v in candidates if datetime.fromtimestamp(v['due_at'], HOME_ZONE).date() == selected_day
                      or v['kind'] == 'fixed' and v['day'] == selected_day.isoformat()]
    if len(candidates) > 1:
        times = '、'.join(datetime.fromtimestamp(v['due_at'], HOME_ZONE).strftime('%H:%M') for v in candidates)
        raise ValueError('有多个叫醒安排（'+times+'），请明确要修改或跳过哪一个')
    if not candidates:
        raise ValueError('没有找到对应的叫醒安排，请先查询')
    target = candidates[0]
    if target.get('first_sound_at') is not None:
        raise ValueError('本次已经起播，仍按持续离床或十分钟结束')
    if target['due_at'] > requested_at + 86400:
        raise ValueError('语音只能修改未来 24 小时内的单次安排')
    if target['due_at'] <= now and action != 'restore':
        raise ValueError('这次叫醒时间已过，请另设一次')
    return target

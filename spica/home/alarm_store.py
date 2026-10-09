"""Durable Home schedules, occurrences and command receipts; no biometric storage."""
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta, timezone
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import uuid
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, model_validator


class AlarmSchedule(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)
    local_time: str = Field(pattern=r'^(?:[01]\d|2[0-3]):[0-5]\d(?::[0-5]\d)?$')
    timezone: str
    weekdays: tuple[int, ...] = Field(default=(), max_length=7)
    on_date: date | None = None
    wake_lead_minutes: int = Field(default=5, ge=2, le=120)
    label: str = Field(default='', max_length=80)
    deleted: bool = False

    @model_validator(mode='after')
    def valid(self):
        ZoneInfo(self.timezone)
        if bool(self.weekdays) == (self.on_date is not None):
            raise ValueError('choose a specific date or repeating weekdays')
        if len(set(self.weekdays)) != len(self.weekdays) or any(day not in range(7) for day in self.weekdays):
            raise ValueError('weekdays must be unique values from Monday=0 to Sunday=6')
        self.weekdays = tuple(sorted(self.weekdays))
        return self

    def due_on(self, day: date) -> float | None:
        if (self.on_date is not None and day != self.on_date
                or self.on_date is None and day.weekday() not in self.weekdays):
            return None
        zone = ZoneInfo(self.timezone)
        local = datetime.combine(day, time.fromisoformat(self.local_time), zone)
        # One instance per local date. A skipped DST wall time has no occurrence;
        # the repeated hour uses its first occurrence, never a second alarm.
        restored = datetime.fromtimestamp(local.timestamp(), zone)
        if restored.replace(tzinfo=None) != local.replace(tzinfo=None):
            return None
        return local.timestamp()


class AlarmStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS schedule (id TEXT PRIMARY KEY, request_key TEXT UNIQUE NOT NULL, created_at REAL NOT NULL, enabled INTEGER NOT NULL, spec TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS occurrence (id TEXT PRIMARY KEY, schedule_id TEXT NOT NULL, day TEXT NOT NULL, due_at REAL NOT NULL, state TEXT NOT NULL, data TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS bedtime (id INTEGER PRIMARY KEY CHECK(id=1), data TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS control (request_key TEXT PRIMARY KEY, result TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS alarm_plan_revision (id INTEGER PRIMARY KEY CHECK(id=1), revision INTEGER NOT NULL)')
            db.execute('INSERT OR IGNORE INTO alarm_plan_revision VALUES (1,0)')
            db.execute('CREATE TABLE IF NOT EXISTS alarm_override (schedule_id TEXT NOT NULL, day TEXT NOT NULL, data TEXT NOT NULL, PRIMARY KEY(schedule_id,day))')
            db.execute('CREATE TABLE IF NOT EXISTS room_command (request_key TEXT PRIMARY KEY, result TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS room_state (id INTEGER PRIMARY KEY CHECK(id=1), data TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS farewell (id TEXT PRIMARY KEY, day TEXT NOT NULL, state TEXT NOT NULL, claim_id TEXT UNIQUE, data TEXT NOT NULL)')
            db.execute("CREATE UNIQUE INDEX IF NOT EXISTS farewell_daily_claim ON farewell(day) WHERE state IN ('claimed','preparing','ready','sending','started','delivered','unknown')")
            for row in db.execute("SELECT data FROM farewell WHERE state IN ('claimed','preparing','ready','sending','started')").fetchall():
                value = json.loads(row['data'])
                value['state'] = 'expired' if (value['state'] in {'preparing', 'ready'}
                    or value['state'] == 'claimed' and value['channel'] == 'qq') else 'unknown'
                value['reason'] = 'core_restarted'
                db.execute('UPDATE farewell SET state=?, data=? WHERE id=?',
                           (value['state'], json.dumps(value), value['id']))

    def farewell_claim(self, *, day, channel, trigger_at, expires_at, character_id):
        value = dict(id='home-farewell:'+uuid.uuid4().hex, day=day, channel=channel,
            state='claimed' if channel == 'desktop' else 'preparing', trigger_at=trigger_at,
            expires_at=expires_at, character_id=character_id, attempt=uuid.uuid4().hex, text='', claim_id=None)
        try:
            with self.db() as db:
                db.execute('INSERT INTO farewell VALUES (?,?,?,?,?)',
                    (value['id'], day, value['state'], None, json.dumps(value)))
        except sqlite3.IntegrityError:
            return None
        return value

    def farewell_for_day(self, day):
        with self.db() as db:
            row = db.execute("SELECT data FROM farewell WHERE day=? AND state IN ('claimed','preparing','ready','sending','started','delivered','unknown')", (day,)).fetchone()
        return json.loads(row['data']) if row else None

    def farewell_for_claim(self, claim_id):
        with self.db() as db:
            row = db.execute('SELECT data FROM farewell WHERE claim_id=?', (claim_id,)).fetchone()
        return json.loads(row['data']) if row else None

    def farewell(self, identity):
        with self.db() as db:
            row = db.execute('SELECT data FROM farewell WHERE id=?', (identity,)).fetchone()
        return json.loads(row['data']) if row else None

    def save_farewell(self, value):
        with self.db() as db:
            db.execute('UPDATE farewell SET state=?, claim_id=?, data=? WHERE id=?',
                       (value['state'], value.get('claim_id'), json.dumps(value), value['id']))

    def pending_farewell_evidence(self):
        with self.db(timeout=0) as db:
            rows = db.execute("SELECT data FROM farewell WHERE state IN ('delivered','unknown') "
                "AND COALESCE(json_extract(data,'$.evidence_state'),'') != state").fetchall()
        return [json.loads(row['data']) for row in rows]

    def confirm_farewell_evidence(self, identity, attempt, state):
        # A late unknown-state receipt cannot overwrite a subsequently confirmed
        # delivery. Bookkeeping yields immediately when another writer is busy.
        with self.db(timeout=0) as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT data FROM farewell WHERE id=?', (identity,)).fetchone()
            if row is None:
                return False
            value = json.loads(row['data'])
            if value['attempt'] != attempt or value['state'] != state or state not in {'delivered', 'unknown'}:
                return False
            value['evidence_state'] = state
            db.execute('UPDATE farewell SET data=? WHERE id=?', (json.dumps(value), identity))
            return True

    def room_state(self):
        with self.db() as db:
            row = db.execute('SELECT data FROM room_state WHERE id=1').fetchone()
        return json.loads(row['data']) if row else {}

    def save_room_state(self, value):
        with self.db() as db:
            db.execute('INSERT INTO room_state VALUES (1,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data',
                       (json.dumps(value),))

    @contextmanager
    def db(self, *, timeout=5):
        connection = sqlite3.connect(self.path, timeout=timeout)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def create(self, spec: AlarmSchedule, *, request_id, origin, now):
        encoded = spec.model_dump_json()
        request_key = hashlib.sha256(json.dumps([request_id, origin, json.loads(encoded)], sort_keys=True).encode()).hexdigest()
        with self.db() as db:
            created = db.execute('INSERT OR IGNORE INTO schedule VALUES (?,?,?,?,?)',
                       (uuid.uuid4().hex, request_key, now, 1, encoded)).rowcount
            if created:
                db.execute('UPDATE alarm_plan_revision SET revision=revision+1 WHERE id=1')
            row = db.execute('SELECT * FROM schedule WHERE request_key=?', (request_key,)).fetchone()
        self.materialize(now)
        return self.schedule_view(row)

    @staticmethod
    def schedule_view(row):
        return {**json.loads(row['spec']), 'id': row['id'], 'enabled': bool(row['enabled']),
                'created_at': row['created_at'], 'wake_lead_minutes': 5}

    def schedules(self):
        with self.db() as db:
            return [self.schedule_view(row) for row in db.execute('SELECT * FROM schedule ORDER BY created_at')]

    def plan_snapshot(self, now):
        all_schedules = self.schedules()
        schedules = [value for value in all_schedules if not value.get('deleted')]
        by_id = {value['id']: value for value in all_schedules}
        with self.db() as db:
            revision = db.execute('SELECT revision FROM alarm_plan_revision WHERE id=1').fetchone()[0]
            values = [json.loads(row['data']) for row in db.execute('SELECT data FROM occurrence ORDER BY due_at')]
            overrides = [dict(row) for row in db.execute('SELECT * FROM alarm_override ORDER BY day')]
        upcoming, active = [], []
        for value in values:
            schedule = by_id.get(value['schedule_id'])
            if schedule is None:
                continue
            value = dict(value, kind='fixed' if schedule['weekdays'] else 'temporary',
                         label=schedule.get('label', ''),
                         due_local=datetime.fromtimestamp(value['due_at'], ZoneInfo(schedule['timezone'])).isoformat())
            if value['first_sound_at'] is not None and value['state'] not in {'completed', 'cancelled'}:
                active.append(value)
            elif not schedule.get('deleted') and schedule['enabled'] and value['state'] in {'scheduled', 'preparing'} and value['due_at'] > now:
                upcoming.append(value)
        for schedule in schedules:
            schedule['next'] = next((v for v in upcoming if v['schedule_id'] == schedule['id']), None)
            schedule['active'] = next((v for v in active if v['schedule_id'] == schedule['id']), None)
            schedule['execution'] = next((v for v in reversed(values) if v['schedule_id'] == schedule['id']
                and v['due_at'] <= now and v['state'] not in {'completed', 'cancelled'}), None)
            if not schedule['weekdays']:
                spec = AlarmSchedule(**{k: v for k, v in schedule.items() if k in AlarmSchedule.model_fields})
                schedule['scheduled_for'] = spec.due_on(spec.on_date)
                schedule['can_enable'] = any(v['schedule_id'] == schedule['id'] and v['due_at'] > now
                    and v['first_sound_at'] is None and (v['state'] in {'scheduled', 'preparing'}
                    or v['state'] == 'cancelled' and v['reason'] == 'schedule_disabled') for v in values)
        fixed = [value for value in schedules if value['weekdays']]
        known = {value['id']: value for value in values}
        adjustments = []
        for row in overrides:
            schedule = by_id.get(row['schedule_id'])
            if schedule is None or schedule.get('deleted'):
                continue
            day = date.fromisoformat(row['day'])
            base = datetime.combine(day, time.fromisoformat(schedule['local_time']), ZoneInfo(schedule['timezone'])).timestamp()
            adjustment = json.loads(row['data'])
            identity = row['schedule_id'] + ':' + row['day']
            due = adjustment['due_at'] if adjustment['action'] == 'move' else base
            if day < datetime.fromtimestamp(now, ZoneInfo(schedule['timezone'])).date() and due <= now:
                continue
            if known.get(identity, {}).get('first_sound_at') is not None or known.get(identity, {}).get('state') == 'completed':
                continue
            adjustments.append(dict(known.get(identity, {}), id=identity, schedule_id=row['schedule_id'],
                day=row['day'], kind='fixed', due_at=due, original_due_at=base, adjustment=adjustment['action'],
                due_local=datetime.fromtimestamp(due, ZoneInfo(schedule['timezone'])).isoformat(),
                effective=bool(schedule['enabled'] and day.weekday() in schedule['weekdays'])))
        return dict(revision=revision, fixed=fixed[0] if len(fixed) == 1 else None,
                    fixed_conflicts=fixed if len(fixed) > 1 else [],
                    temporary=[value for value in schedules if not value['weekdays']],
                    upcoming=upcoming, next=upcoming[0] if upcoming else None, active=active,
                    adjustments=adjustments, attention=[v for v in values if v['state'] == 'paused'][-5:])

    def edit_plan(self, *, action, arguments, request_key, expected_revision, now, before_commit=None):
        """Persist a management command and its receipt in the existing database."""
        if action not in {'save_fixed', 'select_fixed', 'add_temporary', 'move', 'skip', 'restore', 'set_enabled', 'rearm', 'edit_temporary', 'delete'}:
            raise ValueError('不支持的闹钟操作')
        allowed = {
            'save_fixed': {'local_time', 'weekdays', 'label'},
            'add_temporary': {'due_at', 'label'}, 'rearm': {'schedule_id', 'due_at', 'label'},
            'move': {'instance_id', 'due_at'}, 'skip': {'instance_id'}, 'restore': {'instance_id'},
            'set_enabled': {'schedule_id', 'enabled'}, 'edit_temporary': {'schedule_id', 'due_at', 'label'},
            'delete': {'schedule_id'},
            'select_fixed': {'schedule_id'},
        }
        if not isinstance(arguments, dict) or set(arguments) - allowed[action]:
            raise ValueError('闹钟操作参数不支持')
        if expected_revision is not None and (type(expected_revision) is not int or expected_revision < 0):
            raise ValueError('闹钟版本无效，请刷新')
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            previous = db.execute('SELECT result FROM control WHERE request_key=?', (request_key,)).fetchone()
            if previous:
                return json.loads(previous['result'])
            revision = db.execute('SELECT revision FROM alarm_plan_revision WHERE id=1').fetchone()[0]
            if expected_revision is not None and expected_revision != revision:
                raise ValueError('闹钟已被其他操作修改，请刷新后再保存')
            before = {row['id']: row['data'] for row in db.execute('SELECT id,data FROM occurrence')}
            if action == 'select_fixed':
                fixed = [row for row in db.execute('SELECT * FROM schedule')
                         if json.loads(row['spec']).get('weekdays') and not json.loads(row['spec']).get('deleted')]
                identity = arguments['schedule_id']
                if not any(row['id'] == identity for row in fixed):
                    raise ValueError('没有找到要保留的固定作息，请刷新')
                for row in fixed:
                    if row['id'] != identity:
                        spec = AlarmSchedule.model_validate_json(row['spec']).model_copy(update={'deleted': True})
                        db.execute('UPDATE schedule SET enabled=0,spec=? WHERE id=?', (spec.model_dump_json(), row['id']))
                        self._reconcile_schedule(db, row['id'], spec, now)
            elif action == 'save_fixed':
                fixed = [row for row in db.execute('SELECT * FROM schedule')
                         if json.loads(row['spec']).get('weekdays') and not json.loads(row['spec']).get('deleted')]
                if len(fixed) > 1:
                    raise ValueError('存在多个旧固定安排，请先明确保留哪一个')
                previous_spec = json.loads(fixed[0]['spec']) if fixed else {'timezone': 'Asia/Shanghai'}
                spec = AlarmSchedule(**dict(previous_spec, **arguments))
                if not spec.weekdays:
                    raise ValueError('固定闹钟至少选择一个星期')
                identity = fixed[0]['id'] if fixed else uuid.uuid4().hex
                if fixed:
                    db.execute('UPDATE schedule SET spec=? WHERE id=?', (spec.model_dump_json(), identity))
                    self._reconcile_schedule(db, identity, spec, now)
                else:
                    db.execute('INSERT INTO schedule VALUES (?,?,?,?,?)',
                               (identity, request_key, now, 1, spec.model_dump_json()))
            elif action in {'add_temporary', 'rearm'}:
                label = arguments.get('label', '')
                if action == 'rearm':
                    old = db.execute('SELECT * FROM schedule WHERE id=?', (arguments['schedule_id'],)).fetchone()
                    if old is None:
                        raise ValueError('没有找到这个临时闹钟')
                    old_spec = AlarmSchedule.model_validate_json(old['spec'])
                    if old_spec.weekdays or old_spec.deleted:
                        raise ValueError('只能重新设置保留中的临时闹钟')
                    local_now = datetime.fromtimestamp(now, ZoneInfo(old_spec.timezone))
                    local_due = datetime.combine(local_now.date(), time.fromisoformat(old_spec.local_time), local_now.tzinfo)
                    if local_due.timestamp() <= now:
                        local_due += timedelta(days=1)
                    due = self._future_time(arguments.get('due_at', local_due.timestamp()), now)
                    label = arguments.get('label', old_spec.label)
                    retired = old_spec.model_copy(update={'deleted': True})
                    db.execute('UPDATE schedule SET enabled=0,spec=? WHERE id=?', (retired.model_dump_json(), old['id']))
                    self._reconcile_schedule(db, old['id'], old_spec, now)
                else:
                    due = self._future_time(arguments['due_at'], now)
                local = datetime.fromtimestamp(due, ZoneInfo('Asia/Shanghai'))
                spec = AlarmSchedule(local_time=local.strftime('%H:%M:%S'), timezone='Asia/Shanghai',
                                     on_date=local.date(), label=label)
                identity = uuid.uuid4().hex
                db.execute('INSERT INTO schedule VALUES (?,?,?,?,?)',
                           (identity, request_key, now, 1, spec.model_dump_json()))
            elif action in {'set_enabled', 'edit_temporary', 'delete'}:
                identity = arguments['schedule_id']
                schedule = db.execute('SELECT * FROM schedule WHERE id=?', (identity,)).fetchone()
                if schedule is None:
                    raise ValueError('没有找到这个闹钟')
                spec = AlarmSchedule.model_validate_json(schedule['spec'])
                if spec.deleted:
                    raise ValueError('这个闹钟已删除')
                if action == 'edit_temporary':
                    if spec.weekdays:
                        raise ValueError('请使用固定作息编辑入口')
                    if any(json.loads(row['data'])['state'] in {'completed', 'paused'} for row in
                           db.execute('SELECT data FROM occurrence WHERE schedule_id=?', (identity,))):
                        raise ValueError('这次叫醒已经结束，请重新开启或另设一次')
                    if any(json.loads(row['data'])['first_sound_at'] is not None for row in
                           db.execute('SELECT data FROM occurrence WHERE schedule_id=?', (identity,))):
                        raise ValueError('已经起播的临时闹钟不能改期，请另设一次')
                    due = self._future_time(arguments['due_at'], now)
                    local = datetime.fromtimestamp(due, ZoneInfo(spec.timezone))
                    spec = AlarmSchedule(local_time=local.strftime('%H:%M:%S'), on_date=local.date(),
                                         timezone=spec.timezone, label=arguments.get('label', spec.label))
                    enabled = bool(schedule['enabled'])
                    db.execute('UPDATE schedule SET spec=? WHERE id=?', (spec.model_dump_json(), identity))
                elif action == 'delete':
                    if spec.weekdays:
                        raise ValueError('固定作息请使用开关，不能删除运行历史')
                    spec = spec.model_copy(update={'deleted': True})
                    enabled = False
                    db.execute('UPDATE schedule SET spec=? WHERE id=?', (spec.model_dump_json(), identity))
                else:
                    enabled = arguments['enabled']
                    if type(enabled) is not bool:
                        raise ValueError('闹钟开关必须为布尔值')
                db.execute('UPDATE schedule SET enabled=? WHERE id=?', (int(enabled), identity))
                self._reconcile_schedule(db, identity, spec, now)
            else:
                row = db.execute('SELECT data FROM occurrence WHERE id=?', (arguments['instance_id'],)).fetchone()
                if row is None:
                    raise ValueError('没有找到这次叫醒')
                value = json.loads(row['data'])
                if value['first_sound_at'] is not None or value['state'] == 'completed':
                    raise ValueError('已经开始或完成的叫醒不能改期')
                identity = value['schedule_id']
                schedule = db.execute('SELECT * FROM schedule WHERE id=?', (identity,)).fetchone()
                spec = AlarmSchedule.model_validate_json(schedule['spec'])
                if not spec.weekdays:
                    raise ValueError('临时叫醒请修改临时安排')
                if action == 'restore':
                    db.execute('DELETE FROM alarm_override WHERE schedule_id=? AND day=?', (identity, value['day']))
                else:
                    override = dict(action=action, due_at=self._future_time(arguments['due_at'], now) if action == 'move' else None)
                    db.execute('INSERT INTO alarm_override VALUES (?,?,?) ON CONFLICT(schedule_id,day) DO UPDATE SET data=excluded.data',
                               (identity, value['day'], json.dumps(override)))
                self._reconcile_schedule(db, identity, spec, now, force_ids={value['id']})
            self._materialize(db, now, skip_past_schedule=identity)
            if before_commit is not None:
                changed = [json.loads(row['data']) for row in db.execute('SELECT id,data FROM occurrence')
                           if before.get(row['id']) != row['data']]
                for started in before_commit(changed):
                    self._write_occurrence(db, started)
            result = dict(revision=revision+1, schedule_id=identity)
            db.execute('UPDATE alarm_plan_revision SET revision=? WHERE id=1', (revision+1,))
            db.execute('INSERT INTO control VALUES (?,?)', (request_key, json.dumps(result)))
        return result

    @staticmethod
    def _future_time(value, now):
        if type(value) not in (int, float) or not math.isfinite(value) or value <= now:
            raise ValueError('叫醒时间必须是有效的未来时刻')
        return math.ceil(value)

    @staticmethod
    def _override(db, identity, day):
        row = db.execute('SELECT data FROM alarm_override WHERE schedule_id=? AND day=?', (identity, day)).fetchone()
        return json.loads(row['data']) if row else None

    @staticmethod
    def _write_occurrence(db, value):
        db.execute('UPDATE occurrence SET due_at=?,state=?,data=? WHERE id=?',
                   (value['due_at'], value['state'], json.dumps(value), value['id']))

    def _reconcile_schedule(self, db, identity, spec, now, *, force_ids=()):
        enabled = bool(db.execute('SELECT enabled FROM schedule WHERE id=?', (identity,)).fetchone()[0])
        reversible = {'fixed_rule_changed', 'schedule_disabled', 'owner_skipped', 'plan_time_passed'}
        for row in db.execute('SELECT data FROM occurrence WHERE schedule_id=?', (identity,)).fetchall():
            value = json.loads(row['data'])
            if value['first_sound_at'] is not None or value['state'] == 'completed':
                continue
            if value['state'] in {'cancelled', 'paused'} and value['reason'] not in reversible and value['id'] not in force_ids:
                continue
            base = spec.due_on(date.fromisoformat(value['day']))
            override = self._override(db, identity, value['day'])
            due = override['due_at'] if base is not None and override else base
            reason = ('schedule_disabled' if not enabled else 'fixed_rule_changed' if base is None else
                      'owner_skipped' if override and override['action'] == 'skip' else '')
            if not reason and due <= now:
                if due == value['due_at'] and value['state'] in {'scheduled', 'preparing', 'calling'} and value['id'] not in force_ids:
                    continue
                reason = 'plan_time_passed'
            if reason:
                if value['state'] == 'cancelled' and value['reason'] == reason:
                    continue
                value.update(state='cancelled', reason=reason, revision=value['revision']+1)
            else:
                if due == value['due_at'] and value['state'] in {'scheduled', 'preparing', 'calling'} and value.get('adjustment') == (override['action'] if override else None):
                    continue
                value.update(due_at=due, scheduled_due_at=due, original_due_at=base,
                             adjustment=override['action'] if override else None,
                             wake_at=due-300, camera_at=due, next_speech_at=due, revision=value['revision']+1,
                             state='scheduled', reason='', level=0, attempt=0)
                for key in tuple(value):
                    if key.startswith(('light_', 'startup_', 'output_', 'covered_')) or key in {'preflight', 'joined_at', 'audible_coverage'}:
                        value.pop(key, None)
            self._write_occurrence(db, value)

    def materialize(self, now, *, skip_past_schedule=None):
        """Keep the full near-term date targets and the next scheduled date."""
        with self.db() as db:
            self._materialize(db, now, skip_past_schedule=skip_past_schedule)

    def _materialize(self, db, now, *, skip_past_schedule=None):
        for row in db.execute('SELECT * FROM schedule WHERE enabled=1').fetchall():
            spec = AlarmSchedule.model_validate_json(row['spec'])
            if spec.deleted:
                continue
            today = datetime.fromtimestamp(now, ZoneInfo(spec.timezone)).date()
            skips = db.execute('SELECT COUNT(*) FROM alarm_override WHERE schedule_id=?', (row['id'],)).fetchone()[0]
            days = (spec.on_date,) if spec.on_date is not None else (today + timedelta(days=offset) for offset in range(9+7*skips))
            for day in days:
                due = spec.due_on(day)
                override = self._override(db, row['id'], day.isoformat())
                base = due
                if due is not None and override:
                    if override['action'] == 'skip':
                        continue
                    due = override['due_at']
                if due is None or due < row['created_at']:
                    continue
                identity = row['id'] + ':' + day.isoformat()
                data = dict(id=identity, schedule_id=row['id'], day=day.isoformat(),
                    due_at=due, scheduled_due_at=due, wake_at=due-300,
                    camera_at=due, state='scheduled', reason='', level=0,
                    attempt=0, revision=0, first_sound_at=None, last_played_at=None,
                    next_speech_at=due, original_due_at=base,
                    adjustment=override['action'] if override else None)
                if row['id'] == skip_past_schedule and due <= now:
                    data.update(state='cancelled', reason='plan_time_passed')
                db.execute('INSERT OR IGNORE INTO occurrence VALUES (?,?,?,?,?,?)',
                           (identity, row['id'], day.isoformat(), due, data['state'], json.dumps(data)))
                existing = db.execute('SELECT state FROM occurrence WHERE id=?', (identity,)).fetchone()
                if due > now and day > today and not override and existing['state'] not in {'completed', 'cancelled'}:
                    break

    def active(self):
        with self.db() as db:
            return [json.loads(row['data']) for row in db.execute(
                "SELECT data FROM occurrence WHERE state NOT IN ('completed','cancelled') ORDER BY due_at")]

    def pending_outcomes(self):
        # Completion itself is the durable pending marker. A crash between the
        # business save and submitting a memory job therefore cannot lose it.
        with self.db(timeout=0) as db:
            return [json.loads(row['data']) for row in db.execute("""SELECT o.data FROM occurrence o
                WHERE o.state IN ('completed','cancelled') AND EXISTS (
                    SELECT 1 FROM json_each(o.data,'$.event_characters') c WHERE NOT EXISTS (
                        SELECT 1 FROM json_each(o.data,'$.outcome_evidence') a
                        WHERE a.key=c.value AND a.value=json_extract(o.data,'$.revision')))
                ORDER BY o.due_at""")]

    def confirm_outcome(self, identity, revision, character):
        # This bookkeeping runs after business decisions but must also yield
        # immediately to concurrent STOP/close when the database is busy.
        with self.db(timeout=0) as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT data FROM occurrence WHERE id=?', (identity,)).fetchone()
            if row is None:
                return False
            value = json.loads(row['data'])
            if (value['revision'] != revision or value['state'] not in {'completed', 'cancelled'}
                    or character not in value.get('event_characters', ())):
                return False
            value.setdefault('outcome_evidence', {})[character] = revision
            db.execute('UPDATE occurrence SET data=? WHERE id=?', (json.dumps(value), identity))
            return True

    def get(self, identity):
        with self.db() as db:
            row = db.execute('SELECT data FROM occurrence WHERE id=?', (identity,)).fetchone()
        if row is None:
            raise ValueError('没有找到这次叫醒，请先查询实例 ID')
        return json.loads(row['data'])

    def save(self, instance):
        with self.db() as db:
            db.execute('UPDATE occurrence SET due_at=?, state=?, data=? WHERE id=?',
                       (instance['due_at'], instance['state'], json.dumps(instance), instance['id']))

    def control_result(self, key):
        with self.db() as db:
            row = db.execute('SELECT result FROM control WHERE request_key=?', (key,)).fetchone()
        return json.loads(row['result']) if row else None

    def command_result(self, key):
        with self.db() as db:
            row = db.execute('SELECT result FROM room_command WHERE request_key=?', (key,)).fetchone()
        return json.loads(row['result']) if row else None

    def claim_command(self, key, receipt):
        # Keep the existing Home database; never recover a claimed press as work.
        with self.db() as db:
            return db.execute('INSERT OR IGNORE INTO room_command VALUES (?,?)',
                              (key, json.dumps(receipt))).rowcount == 1

    def finish_command(self, key, receipt):
        with self.db() as db:
            db.execute('UPDATE room_command SET result=? WHERE request_key=?', (json.dumps(receipt), key))

    def recent_commands(self):
        with self.db() as db:
            return [json.loads(row['result']) for row in db.execute(
                'SELECT result FROM room_command ORDER BY rowid DESC LIMIT 20')]

    def save_control(self, instance, key):
        with self.db() as db:
            db.execute('UPDATE occurrence SET due_at=?, state=?, data=? WHERE id=?',
                       (instance['due_at'], instance['state'], json.dumps(instance), instance['id']))
            db.execute('INSERT INTO control VALUES (?,?)', (key, json.dumps(instance)))
            db.execute('UPDATE alarm_plan_revision SET revision=revision+1 WHERE id=1')

    def set_schedule_enabled(self, identity, enabled):
        with self.db() as db:
            if db.execute('UPDATE schedule SET enabled=? WHERE id=?', (int(enabled), identity)).rowcount != 1:
                raise ValueError('没有找到这个周期安排')
            db.execute('UPDATE alarm_plan_revision SET revision=revision+1 WHERE id=1')

    def disabled_instances(self, schedule_id):
        with self.db() as db:
            values = [json.loads(row['data']) for row in db.execute(
                "SELECT data FROM occurrence WHERE schedule_id=? AND state='cancelled'", (schedule_id,))]
        return [value for value in values if value['reason'] == 'schedule_disabled']

    def query(self):
        with self.db() as db:
            rows = db.execute('SELECT data FROM occurrence ORDER BY due_at DESC LIMIT 30').fetchall()
        schedules = self.schedules()
        zones = {value['id']: ZoneInfo(value['timezone']) for value in schedules}
        instances = [json.loads(row['data']) for row in rows]
        for value in instances:
            value['due_local'] = datetime.fromtimestamp(value['due_at'], zones[value['schedule_id']]).isoformat(timespec='seconds')
        return dict(schedules=schedules, instances=instances, bedtime=self.bedtime())

    def bedtime(self):
        with self.db() as db:
            row = db.execute('SELECT data FROM bedtime WHERE id=1').fetchone()
        return json.loads(row['data']) if row else None

    def save_bedtime(self, value, *, preserve_receipt=False):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            if preserve_receipt:
                row = db.execute('SELECT data FROM bedtime WHERE id=1').fetchone()
                previous = json.loads(row['data']) if row else None
                if previous and previous['id'] == value['id']:
                    value = dict(value, receipt=previous.get('receipt'))
            db.execute('INSERT INTO bedtime VALUES (1,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data',
                       (json.dumps(value),))

    def save_power_receipt(self, intent_id, receipt):
        """Persist before a system write without overwriting a concurrent cancel."""
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT data FROM bedtime WHERE id=1').fetchone()
            value = json.loads(row['data']) if row else None
            if value is None or value['id'] != intent_id:
                raise RuntimeError('睡前操作已改变，未继续系统操作')
            value['receipt'] = receipt
            db.execute('UPDATE bedtime SET data=? WHERE id=1', (json.dumps(value),))

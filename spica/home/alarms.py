"""Home-owned wake instances; shared speech owns only each utterance's delivery."""
from contextlib import contextmanager
from contextvars import ContextVar
from collections import deque
import json
import logging
import math
import queue
import random
import re
import threading
import time

from spica.home.alarm_speech import wake_finished_directive, wake_speech, wake_event
from spica.home.alarm_store import AlarmSchedule, AlarmStore
from spica.home.models import RoomObservation
from spica.home.wake_run import WakeReaction, WakeRun

logger = logging.getLogger(__name__)


class WakeObservation:
    """One alarm's fresh region evidence; no persistent person selection."""
    def __init__(self, config):
        self.config = config
        self.last_at = self.since = self.outside_since = None
        self.frames = self.outside_frames = 0
        self.bed_since, self.bed_frames = None, 0
        self.last_report_at = float('-inf')
        self.bed_occupied = self.outside_occupied = None
        self.generation = None
        self.reason = 'awaiting_fresh_frames'

    def _unknown(self, reason):
        self.bed_occupied = self.outside_occupied = None
        self.since = self.outside_since = None
        self.frames = self.outside_frames = 0
        self.bed_since, self.bed_frames = None, 0
        self.reason = reason

    def reset_evidence(self):
        self.last_at = None
        self._unknown('awaiting_fresh_frames')

    def observe(self, room, captured_mono, generation, now):
        if generation != self.generation:
            self.reset_evidence()
            self.generation = generation
        if captured_mono is None:
            if room.reason not in {'observed', 'no_person'}:
                self._unknown(room.reason)
            elif self.last_at is None or now-self.last_at > self.config.frame_ttl_seconds:
                self._unknown('no_fresh_frame')
            return
        fresh = (0 <= now-captured_mono <= self.config.frame_ttl_seconds
                 and (self.last_at is None or captured_mono > self.last_at))
        if not fresh:
            self._unknown('stale_frame')
            return
        continuous = (self.last_at is not None
                      and captured_mono-self.last_at <= self.config.frame_ttl_seconds)
        self.last_at = captured_mono
        if room.reason not in {'observed', 'no_person'}:
            self._unknown(room.reason)
            return
        outside = room.outside_bed_occupied
        if outside is None:
            self.outside_since, self.outside_frames = None, 0
        else:
            if not continuous or outside != self.outside_occupied or self.outside_since is None:
                self.outside_since, self.outside_frames = captured_mono, 0
            self.outside_frames += 1
        if room.bed_occupied is False and outside is True:
            if not continuous or self.since is None:
                self.since, self.frames = captured_mono, 0
            self.frames += 1
        else:
            self.since, self.frames = None, 0
        if room.bed_occupied is True:
            if not continuous or self.bed_since is None:
                self.bed_since, self.bed_frames = captured_mono, 0
            self.bed_frames += 1
        else:
            self.bed_since, self.bed_frames = None, 0
        self.bed_occupied, self.outside_occupied = room.bed_occupied, outside
        self.reason = ('region_uncertain' if room.bed_occupied is None or outside is None else room.reason)

    def _confirmed(self, since, frames, seconds, minimum_frames):
        config = self.config.wake
        return (config.leave_bed_seconds is not None and since is not None
                and self.last_at-since >= seconds
                and frames >= minimum_frames)

    def outside_confirmed(self, occupied):
        config = self.config.wake
        return self.outside_occupied is occupied and self._confirmed(self.outside_since, self.outside_frames,
            config.start_confirm_seconds, config.start_confirm_min_frames)

    @property
    def confirmed(self):
        config = self.config.wake
        return self._confirmed(self.since, self.frames, config.leave_bed_seconds, config.leave_bed_min_frames)

    @property
    def bed_confirmed(self):
        config = self.config.wake
        return self._confirmed(self.bed_since, self.bed_frames, config.leave_bed_seconds, config.leave_bed_min_frames)


class HomeAlarms:
    # Leave seven seconds of the existing due+15 budget for generation/TTS/playback.
    VISUAL_START_BUDGET_SECONDS = 8.

    def __init__(self, config, store: AlarmStore, *, propose_speech, cancel_speech,
                 prepare_output, active_character, record_outcome=None, prepare_light=None,
                 close_response_scope=None, cancel_reply=None, check_readiness=None,
                 reaction_guard=None, retain_output=None, fallback_enabled=False,
                 clock=time.monotonic, wall_clock=time.time):
        self.config, self.store = config, store
        self.propose_speech, self.cancel_speech = propose_speech, cancel_speech
        self.prepare_output, self.active_character = prepare_output, active_character
        self.record_outcome = record_outcome
        self.prepare_light = prepare_light
        self.check_readiness = check_readiness
        self.close_response_scope, self.cancel_reply = close_response_scope, cancel_reply
        self.reaction_guard = reaction_guard
        self.retain_output = retain_output
        self._output_retained = False
        self.fallback_enabled = fallback_enabled
        self.clock, self.wall_clock = clock, wall_clock
        self._observations = {}
        self._origin = ContextVar('home_origin', default=None)
        self._lock = threading.RLock()
        self._closed = False
        self._outputs_stopped = False
        self._wake_state = ()
        self.error = ''
        self._runs = {}
        self._pending_stop_saves = set()
        self._next_stop_save = float('-inf')
        self._reaction = None
        self._reaction_observation = None
        self._outputs = {}
        self._output_retry_at = {}
        self._replies = {}
        self._reply_terminals = deque(maxlen=128)
        self._reply_events = queue.SimpleQueue()
        self._outcome_jobs, self._outcome_retry_at = {}, {}
        self._next_outcome_sync = float('-inf')
        self._last_materialized = float('-inf')
        self.bedtime = None
        self.output_generation = 0
        store.materialize(wall_clock())
        self.instances = {value['id']: value for value in store.active()}
        for value in self.instances.values():
            # Reconcile old, unstarted pre-camera/snooze instances to this policy.
            if value['first_sound_at'] is None:
                value['camera_at'] = value['due_at']
                value['wake_at'] = value['due_at']-300
                store.save(value)
            if value['state'] == 'calling':
                # An utterance might have played before the core stopped.
                value.update(state='paused', reason='core_restarted', revision=value['revision']+1)
                store.save(value)
        self._publish_wake_state()

    def _publish_wake_state(self):
        # Only the alarm owner publishes. Cross-owner guards never borrow the
        # mutable occurrence records or wait in command -> alarm lock order.
        self._wake_state = tuple((value['state'], value['first_sound_at'], value['due_at'])
            for value in self.instances.values() if value['state'] not in {'completed', 'cancelled'})
        bedtime_request = self.bedtime.intent.get('request_id') if self.bedtime and self.bedtime.intent else None
        self._audio_requests = frozenset(self._replies) | ({bedtime_request} if bedtime_request else set())
        needed = not self._closed and (self._reaction is not None or any(
            value['state'] not in {'completed', 'cancelled', 'paused'} and self.wall_clock() >= value['wake_at']
            for value in self.instances.values()))
        if needed != self._output_retained:
            self._output_retained = needed
            if self.retain_output is not None:
                self.retain_output(needed)

    def uses_home_audio(self, request_id):
        """A lock-free publication; audio delivery must not wait on lamp I/O."""
        return request_id in self._audio_requests

    def restore_output_demand(self):
        """Rebind the current wake demand when a disabled TTS engine is enabled."""
        with self._lock:
            if self.retain_output is not None:
                self.retain_output(self._output_retained and not self._closed)

    @contextmanager
    def _state_change(self):
        with self._lock:
            try:
                yield
            finally:
                self._publish_wake_state()

    @contextmanager
    def conversation(self, request_id, conversation_id, *, user_text='', character_id=None, want_audio=True,
                     embodied=False):
        token = self._origin.set((request_id, conversation_id, user_text, want_audio, self.wall_clock(),
                                 character_id or self.active_character()))
        event_binding = None
        try:
            if embodied or user_text.strip():
                with self._state_change():
                    self._cancel_reaction('user_conversation:'+conversation_id)
                    if embodied:
                        self._drain_reply_events()
                        value = self._interactive_instance()
                        if value is not None and request_id not in self._reply_terminals:
                            joined = self._bind_event_character(value, character_id or self.active_character())
                            event_binding = wake_event(value, 'interaction')
                            self._replies[request_id] = value['id']
                            self._withdraw(value['id'])
                            if joined:
                                self.store.save(value)
            yield event_binding
        finally:
            self._origin.reset(token)

    def _require_origin(self):
        origin = self._origin.get()
        if origin is None:
            raise PermissionError('Home 操作需要已认证的本人请求')
        return origin

    def _require_running(self):
        if self._closed or self.error:
            raise RuntimeError('Home 叫醒当前不可用：' + (self.error or '已停止'))

    def create(self, *, local_time, timezone, weekdays=(), on_date=None, wake_lead_minutes=5):
        origin = self._require_origin()
        if wake_lead_minutes != 5:
            raise ValueError('当前晚安策略固定提前五分钟恢复主机')
        spec = AlarmSchedule(local_time=local_time, timezone=timezone, weekdays=tuple(weekdays), on_date=on_date,
                             wake_lead_minutes=wake_lead_minutes)
        if spec.on_date is not None:
            due = spec.due_on(spec.on_date)
            if due is None or due <= self.wall_clock():
                raise ValueError('指定的起床日期时间已过或不是有效的当地时刻')
        with self._state_change():
            self._require_running()
            result = self.store.create(spec, request_id=origin[0], origin=origin[1], now=self.wall_clock())
            self._refresh()
            self._reconcile_bedtime()
            return dict(schedule=result, execution_enabled=self.config.wake.enabled,
                        vision_thresholds_configured=self.config.wake.leave_bed_seconds is not None)

    def query(self):
        self._require_origin()
        with self._lock:
            result = self.store.query()
            result.update(execution_enabled=self.config.wake.enabled, error=self.error,
                          regions=self._region_status(),
                          vision_thresholds_configured=self.config.wake.leave_bed_seconds is not None,
                          plan=self.plan())
            return result

    def plan(self):
        self._require_origin()
        with self._state_change():
            self._refresh()
            result = self.store.plan_snapshot(self.wall_clock())
            result.update(execution_enabled=self.config.wake.enabled, error=self.error,
                          now=self.wall_clock(),
                          bedtime=dict(self.bedtime.intent) if self.bedtime and self.bedtime.intent else None)
            return result

    def manage(self, *, action, arguments, expected_revision=None):
        if action == 'stop_current':
            return self._stop_current_wake(**arguments)
        return self._manage(action=action, arguments=arguments, expected_revision=expected_revision,
                            request_key=self._management_key())

    def _stop_current_wake(self, *, requested_at, requested_mono=None):
        """Explicit desktop STOP; ordinary mic/text interruptions keep the wake."""
        if self._require_origin()[1] != 'home-local-control':
            raise PermissionError('停止整轮叫醒需要本机明确控制')
        if isinstance(requested_at, bool) or not isinstance(requested_at, (int, float)) or not math.isfinite(requested_at):
            raise ValueError('停止请求时间无效')
        if requested_mono is not None and (isinstance(requested_mono, bool)
                or not isinstance(requested_mono, (int, float)) or not math.isfinite(requested_mono)):
            raise ValueError('停止请求单调时间无效')
        with self._state_change():
            cutoff = min(requested_at, self.wall_clock())
            mono_cutoff = min(requested_mono, self.clock()) if requested_mono is not None else None
            def selected(value):
                run = self._runs.get(value['id'])
                if mono_cutoff is not None and run is not None and run.due_mono is not None:
                    return run.due_mono <= mono_cutoff
                return value['due_at'] <= cutoff
            # Cancel the entire due group, including coalesced followers. Do
            # not disable recurring schedules or catch a later alarm if IPC lags.
            targets = [v for v in self.instances.values()
                       if v['state'] not in {'completed', 'cancelled'} and selected(v)]
            attempts = [(v, self._run_for(v).detach(), self._response_scope(v)) for v in targets]
            # Revoke every in-memory owner before cancellation callbacks or disk
            # I/O. A failed first save must not leave another member calling.
            for value in targets:
                value.update(state='cancelled', reason='owner_stopped',
                             revision=value['revision']+1, ended_at=self.wall_clock())
                self._observations.pop(value['id'], None)
                self._pending_stop_saves.add(value['id'])
            reaction = self._reaction
            binding = reaction.event_binding if reaction else None
            completed = self.instances.get(binding.event_id) if binding else None
            if completed is not None and selected(completed):
                self._cancel_reaction('owner_stop_button')
            for value, attempt, response_scope in attempts:
                self._end_interaction(value, response_scope=response_scope)
                if attempt is not None:
                    try:
                        self.cancel_speech(attempt.handle.proposal_id)
                    except Exception:
                        logger.exception('Home wake STOP transport unavailable: instance=%s', value['id'])
                    self._record_first_sound(value, attempt, persist=False)
            failed = self._flush_stop_saves(force=True)
            stopped = [v['id'] for v in targets]
            self._next_outcome_sync = float('-inf')
            logger.info('Home wake stopped by owner: instances=%s', stopped)
            return dict(stopped=stopped, persistence_failed=failed)

    def _flush_stop_saves(self, *, force=False):
        if self._pending_stop_saves and (force or self.clock() >= self._next_stop_save):
            self._next_stop_save = self.clock()+5
            for identity in [key for key in self.instances if key in self._pending_stop_saves]:
                try:
                    self.store.save(self.instances[identity])
                except Exception:
                    logger.exception('Home STOP persistence pending: instance=%s', identity)
                else:
                    self._pending_stop_saves.discard(identity)
        return sorted(self._pending_stop_saves)

    def _management_key(self, operation=None, arguments=None):
        origin = self._require_origin()
        parts = ['alarm_management', origin[0], origin[1]]
        if operation is not None:
            parts.extend((operation, arguments))
        return json.dumps(parts, sort_keys=True)

    def _manage(self, *, action, arguments, request_key, expected_revision=None, wait_for_reply=False):
        origin = self._require_origin()
        with self._state_change():
            self._require_running()
            if self._flush_stop_saves(force=True):
                raise RuntimeError('本轮叫醒已停止，但停止结果尚未保存，请稍后再修改闹钟')
            for value in self.instances.values():
                self._record_first_sound(value)
            started = []
            def withdraw_changed(values):
                # Validate the whole command before touching speech. Cancellation
                # can race an actual-start receipt; keep that fact even on rollback.
                for value in values:
                    current = self.instances.get(value['id'])
                    if current is None or current['first_sound_at'] is not None:
                        continue
                    self._withdraw(value['id'], persist=False)
                    if current['first_sound_at'] is not None:
                        started.append(current)
                        if action in {'move', 'skip', 'restore', 'edit_temporary'}:
                            raise ValueError('本次已经起播，不能改期或跳过')
                return started
            try:
                self.store.edit_plan(action=action, arguments=arguments, request_key=request_key,
                    expected_revision=expected_revision, now=self.wall_clock(), before_commit=withdraw_changed)
            finally:
                for value in started:
                    self.store.save(value)
            values = {value['id']: value for value in self.store.active()}
            values.update({identity: self.store.get(identity) for identity in self.instances})
            for value in values.values():
                current = self.instances.get(value['id'])
                if current is None:
                    self.instances[value['id']] = value
                elif current['first_sound_at'] is None and value['revision'] != current['revision']:
                    if value['due_at'] != current['due_at']:
                        run = self._runs.get(value['id'])
                        if run is not None:
                            run.due_mono = None
                    self.instances[value['id']] = value
                    self._observations.pop(value['id'], None)
                    self._outputs.pop(value['id'], None)
            self._release_changed_groups()
            self._reconcile_bedtime(reply_request=(origin[0], origin[3]) if wait_for_reply else None)
            return self.plan()

    def _reconcile_bedtime(self, *, reply_request=None):
        if self.bedtime is not None:
            plan = self.store.plan_snapshot(self.wall_clock())
            if plan['active']:
                return  # Editing tomorrow never starts sleep during this wake run.
            intent = self.bedtime.intent or {}
            if intent.get('state') == 'morning_preparation':
                identity = intent.get('instance_id')
                linked = self.instances.get(identity) if identity else None
                if linked and linked['state'] in {'completed', 'cancelled', 'paused'} and linked['due_at'] <= self.wall_clock():
                    return  # Let the existing morning lifecycle restore daily mode.
            candidates = [v for v in self.instances.values() if not v.get('covered_by')
                and v['state'] in {'scheduled', 'preparing', 'calling'} and v['first_sound_at'] is None
                and self.wall_clock() < v['due_at']+15 and v['due_at'] <= self.wall_clock()+86400]
            next_alarm = min(candidates, key=lambda v: v['due_at']) if candidates else None
            self.bedtime.reconcile_alarm(next_alarm, plan['revision'], reply_request=reply_request)

    def set_from_text(self, *, local_time=None, on_date=None, after_minutes=None, additional=False, label=''):
        from spica.home.alarm_language import set_command, spoken_due
        origin = self._require_origin()
        with self._state_change():
            key = self._management_key('set_from_text', dict(local_time=local_time, on_date=on_date,
                after_minutes=after_minutes, additional=additional, label=label))
            if self.store.control_result(key) is not None:
                return self.plan()
            if type(additional) is not bool:
                raise ValueError('additional 必须为布尔值')
            due = spoken_due(text=origin[2], requested_at=origin[4], now=self.wall_clock(),
                             local_time=local_time, on_date=on_date, after_minutes=after_minutes)
            action, arguments = set_command(self.plan(), due=due, text=origin[2], latest=origin[4]+86400,
                                            after_minutes=after_minutes, additional=additional, label=label)
            return self._manage(action=action, arguments=arguments, request_key=key, wait_for_reply=True)

    def adjust_from_text(self, *, action, instance_id=None, on_date=None, local_time=None):
        from spica.home.alarm_language import adjustment_target, spoken_due
        if action not in {'move', 'skip', 'restore'}:
            raise ValueError('语音只可改这次、跳过这次或恢复固定时间')
        origin = self._require_origin()
        with self._state_change():
            key = self._management_key('adjust_from_text', dict(action=action, instance_id=instance_id,
                on_date=on_date, local_time=local_time))
            if self.store.control_result(key) is not None:
                return self.plan()
            now = self.wall_clock()
            target = adjustment_target(self.plan(), action=action, instance_id=instance_id,
                on_date=on_date, text=origin[2], requested_at=origin[4], now=now)
            arguments = dict(instance_id=target['id'])
            if action == 'move':
                due = spoken_due(text=origin[2], requested_at=origin[4], now=now,
                                 local_time=local_time, on_date=on_date or target['due_local'][:10])
                arguments['due_at'] = due
                if target['kind'] == 'temporary':
                    action, arguments = 'edit_temporary', dict(schedule_id=target['schedule_id'], due_at=due)
            elif target['kind'] == 'temporary':
                if action == 'restore':
                    raise ValueError('临时闹钟没有固定作息可恢复')
                action, arguments = 'set_enabled', dict(schedule_id=target['schedule_id'], enabled=False)
            return self._manage(action=action, arguments=arguments, request_key=key, wait_for_reply=True)

    def _refresh(self):
        self.store.materialize(self.wall_clock())
        for value in self.store.active():
            self.instances.setdefault(value['id'], value)
        self._apply_bedtime_time_override()
        self._last_materialized = self.clock()

    def _apply_bedtime_time_override(self):
        for selected in tuple(self.instances.values()):
            replaced = selected.get('supersedes_instance_id')
            if replaced is None or selected['state'] in {'completed', 'cancelled'}:
                continue
            earlier = self.instances.get(replaced)
            if (earlier is not None and earlier['state'] in {'scheduled', 'preparing', 'snoozed', 'paused'}
                    and earlier['first_sound_at'] is None):
                self._withdraw(earlier['id'])
                if earlier['first_sound_at'] is None:
                    earlier.update(state='cancelled', reason='explicit_bedtime_time_override')
                    self.store.save(earlier)

    def _run_for(self, value):
        identity = value['id']
        run = self._runs.get(identity)
        return run if run is not None else self._runs.setdefault(identity, WakeRun(self.clock, self.wall_clock))

    def _withdraw(self, identity, *, persist=True):
        run = self._runs.get(identity)
        attempt = run.detach() if run is not None else None
        if attempt is not None:
            try:
                self.cancel_speech(attempt.handle.proposal_id)
            finally:
                self._record_first_sound(self.instances[identity], attempt, persist=persist)

    def _record_first_sound(self, value, attempt=None, *, persist=True):
        if self._run_for(value).capture_first_sound(value, attempt):
            observation = self._observations.get(value['id'])
            if observation is not None:
                observation.reset_evidence()
            if persist:
                self.store.save(value)

    def control(self, *, action, instance_id=None):
        origin = self._require_origin()
        if action not in {'cancel', 'resume'}:
            raise ValueError('叫醒不支持延后；只可取消未开始的实例或恢复故障暂停')
        with self._state_change():
            if action != 'cancel':
                self._require_running()
            if instance_id is None:
                active = [v for v in self.instances.values() if v['state'] in {'preparing', 'calling', 'paused', 'snoozed'}]
                current = [v for v in active if v['state'] != 'paused']
                if current:
                    active = current
                elif active:
                    # A previous date's paused result must not hide today's task.
                    latest = max(v['due_at'] for v in active)
                    active = [v for v in active if v['due_at'] == latest]
                if len(active) != 1:
                    raise ValueError('请先查询并指定这次叫醒的实例 ID')
                instance_id = active[0]['id']
            key = json.dumps([origin[0], origin[1], action, instance_id])
            previous = self.store.control_result(key)
            if previous is not None:
                return previous
            value = self.instances.get(instance_id) or self.store.get(instance_id)
            if action == 'resume' and value.get('covered_by'):
                return self.control(action=action, instance_id=value['covered_by'])
            if value['state'] in {'completed', 'cancelled'}:
                return value
            self._bind_event_character(value, origin[5])
            self._record_first_sound(value)
            if action == 'cancel' and value['first_sound_at'] is not None:
                raise ValueError('正在叫醒：持续确认离床或满十分钟才结束；请按当前角色继续互动')
            self._withdraw(instance_id)
            if action == 'cancel' and value['first_sound_at'] is not None:
                raise ValueError('本次已经起播，仍按持续离床或十分钟结束')
            observation = self._observations.get(instance_id)
            if action == 'cancel':
                self._observations.pop(instance_id, None)
            elif observation is not None:
                observation.reset_evidence()
            value['revision'] += 1
            value['level'] = 0
            value['unknown_since'] = None
            if action == 'cancel':
                value.update(state='cancelled', reason='owner_cancelled', ended_at=self.wall_clock())
            else:
                due = self.wall_clock()
                self.output_generation += 1
                lead = 300
                value.update(state='preparing', reason='owner_resumed',
                    due_at=due, next_speech_at=due, camera_at=due, wake_at=due-lead,
                    rescheduled=True, wake_lead_seconds=lead)
                self._run_for(value).resume()
            self.store.save_control(value, key)
            self.instances[instance_id] = value
            self._reconcile_bedtime()
            if action == 'cancel':
                self._sync_outcomes(force=True)
            return dict(value)

    def control_from_text(self, *, action, instance_id=None):
        if action == 'cancel':
            return self.adjust_from_text(action='skip', instance_id=instance_id)
        return self.control(action=action, instance_id=instance_id)

    def set_enabled(self, *, schedule_id, enabled):
        self.manage(action='set_enabled', arguments=dict(schedule_id=schedule_id, enabled=enabled))
        return dict(schedule_id=schedule_id, enabled=enabled)

    def camera_required(self):
        with self._lock:
            if self._closed or self.error or not self.config.wake.enabled:
                return False
            if self.bedtime is not None and self.bedtime.suppress_alarm:
                return False
            now = self.wall_clock()
            return any(v['state'] not in {'completed', 'cancelled'}
                       and (v['state'] != 'paused' or v['first_sound_at'] is not None)
                       and (v['first_sound_at'] is not None or now >= v['camera_at'])
                       for v in self.instances.values())

    def preparation_required(self):
        with self._lock:
            return (not self._closed and not self.error and self.config.wake.enabled
                and not (self.bedtime and self.bedtime.suppress_alarm)
                and any(v['state'] in {'scheduled', 'preparing'} and v['wake_at'] <= self.wall_clock() < v['due_at']
                        for v in self.instances.values()))

    def wake_active(self):
        # Time can advance without a mutation; evaluate the last complete
        # immutable publication using the current clock.
        if self._closed or self.error or not self.config.wake.enabled:
            return False
        # Reentrant owner checks (e.g. a just-finished wake's closing reaction)
        # need their current state. Another owner's lock is never waited on.
        if self._lock.acquire(blocking=False):
            try:
                self._publish_wake_state()
            finally:
                self._lock.release()
        now = self.wall_clock()
        return any(first_sound is not None or state in {'scheduled', 'preparing', 'calling'}
            and due <= now < due+15 for state, first_sound, due in self._wake_state)

    def prepare_environment(self):
        """Claim one light press durably before the adapter sends it."""
        with self._state_change():
            if (self._closed or self.error or not self.config.wake.enabled
                    or self.bedtime and self.bedtime.suppress_alarm or self.prepare_light is None):
                return
            leader = self._select_executor()
            for value in (leader,) if leader else ():
                if (value['state'] not in {'scheduled', 'preparing'}
                        or not value['due_at'] <= self.wall_clock() < value['due_at']+15
                        or value.get('light_requested_at') is not None
                        or value.get('light_checked_at') is not None):
                    continue
                # The one preparation budget starts at due time, including any
                # wait for another room command. Preserve seven seconds for voice.
                deadline = self.clock()+max(0., value['due_at']+self.VISUAL_START_BUDGET_SECONDS-self.wall_clock())
                def claim():
                    if (self._closed or self.error or not self.config.wake.enabled
                            or self.bedtime and self.bedtime.suppress_alarm
                            or value['state'] not in {'scheduled', 'preparing'}
                            or self.clock() >= deadline
                            or not value['due_at'] <= self.wall_clock() < value['due_at']+15):
                        return False
                    if value.get('light_requested_at') is None:
                        value.update(light_requested_at=self.wall_clock(), light_status='unconfirmed')
                        self.store.save(value)
                    return self.wall_clock() < value['due_at']+15
                try:
                    result = self.prepare_light(claim, deadline=deadline)
                except Exception:
                    logger.exception('Home alarm light preparation unconfirmed; voice remains available')
                    result = 'unconfirmed'
                value.update(light_status=result, light_checked_at=self.wall_clock())
                if result == 'requested':
                    value['light_ready_at'] = self.wall_clock()
                self.store.save(value)
                for follower in self.instances.values():
                    if follower.get('covered_by') == value['id']:
                        follower.update({k: v for k, v in value.items() if k.startswith('light_')})
                        self.store.save(follower)

    def _release_changed_groups(self):
        for value in self.instances.values():
            if not value.get('covered_by') or value['state'] in {'completed', 'cancelled'}:
                continue
            leader = self.instances.get(value['covered_by']) or self.store.get(value['covered_by'])
            if leader['first_sound_at'] is None and (leader['state'] == 'cancelled'
                    or leader['due_at'] != value.get('covered_due_at', leader['due_at'])):
                for key in ('covered_by', 'covered_due_at', 'joined_at', 'audible_coverage'):
                    value.pop(key, None)
                self.store.save(value)

    def _select_executor(self):
        """Associate due occurrences before any light or speech side effect."""
        self._release_changed_groups()
        now = self.wall_clock()
        candidates = []
        for value in self.instances.values():
            if value['state'] not in {'completed', 'cancelled'} and value['due_at'] <= now:
                self._run_for(value).observe_due(value['due_at'])
            if value.get('covered_by') or value['state'] in {'completed', 'cancelled', 'paused'}:
                continue
            self._record_first_sound(value)
            attempt = self._run_for(value).attempt
            if attempt is not None and attempt.handle.result.done():
                result = attempt.handle.result.result()
                if result.status != 'cancelled' and (result.status != 'completed' or result.audio_outcome != 'completed'):
                    continue  # A failure receipt already arrived, even if this tick has not consumed it.
            if value['first_sound_at'] is not None:
                if self._elapsed(value) < self.config.wake.maximum_duration_seconds:
                    candidates.append(value)
            elif value['state'] in {'scheduled', 'preparing', 'calling'} and value['wake_at'] <= now < value['due_at']+15:
                candidates.append(value)
        if not candidates:
            return None
        leader = min(candidates, key=lambda v: (v['first_sound_at'] is None, v['due_at'], v['id']))
        for value in candidates:
            if value is leader or value['first_sound_at'] is not None or value['due_at'] > now:
                continue
            self._withdraw(value['id'])
            if value['first_sound_at'] is not None:
                continue
            value.update(covered_by=leader['id'], covered_due_at=leader['due_at'], joined_at=now, audible_coverage=False)
            value.update({k: v for k, v in leader.items() if k.startswith('light_')})
            self._observations.pop(value['id'], None)
            self.store.save(value)
        return leader

    def _settle_joined(self):
        for value in self.instances.values():
            leader_id = value.get('covered_by')
            if not leader_id or value['state'] in {'completed', 'cancelled'}:
                continue
            leader = self.instances.get(leader_id) or self.store.get(leader_id)
            heard = self._run_for(leader).audible_since(leader, value['due_at'])
            changed = heard and not value.get('audible_coverage')
            if changed:
                value['audible_coverage'] = True
            terminal = leader['state'] in {'completed', 'cancelled', 'paused'}
            if terminal:
                if value['state'] == leader['state'] and value['reason'] == leader['reason']:
                    continue
                value.update(state=leader['state'], reason=leader['reason'],
                    revision=value['revision']+1, ended_at=self.wall_clock())
                changed = True
            if changed:
                self.store.save(value)

    def _elapsed(self, value):
        return self._run_for(value).elapsed(value)

    def _volume(self, value):
        config = self.config.wake
        fraction = min(1., self._elapsed(value)/config.volume_ramp_seconds)
        return config.initial_volume+(config.maximum_volume-config.initial_volume)*fraction

    def _schedule_response_gap(self, value, *, finished_wall=None, finished_mono=None):
        gap = value.get('response_window_seconds', self.config.wake.response_window_seconds)
        self._run_for(value).response_gap(value, gap, finished_wall=finished_wall, finished_mono=finished_mono)

    def _interactive_instance(self):
        if self._closed or self.error:
            return None
        values = [value for value in self.instances.values() if value['state'] == 'calling'
                  and value['first_sound_at'] is not None
                  and self._elapsed(value) < self.config.wake.maximum_duration_seconds]
        return min(values, key=lambda value: value['due_at']) if values else None

    def _finish_instance(self, value, reason, *, evidence_seconds=None, evidence_frames=None):
        completed_at = self.clock()
        left_bed = reason == 'bed_empty_with_person_outside'
        had_reply = value['id'] in self._replies.values()
        was_calling = value['state'] == 'calling'
        self._end_interaction(value, preserve_inflight=left_bed)
        self._withdraw(value['id'])
        value.update(state='completed', reason=reason, revision=value['revision']+1, ended_at=self.wall_clock())
        if evidence_seconds is not None:
            value.update(evidence_seconds=evidence_seconds, evidence_frames=evidence_frames)
        self.store.save(value)
        self._replies = {key: identity for key, identity in self._replies.items() if identity != value['id']}
        self._next_outcome_sync = float('-inf')
        run = self._run_for(value)
        if (left_bed and was_calling and run.has_audio_start and not had_reply
                and not value.get('covered_by') and self.reaction_guard is not None):
            try:
                guard = self.reaction_guard()
                self._cancel_reaction()
                self._reaction = WakeReaction(wake_finished_directive(run.elapsed(value)),
                    run.last_release, guard, self.clock, completed_at=completed_at,
                    event_binding=wake_event(value))
                self._reaction_observation = WakeObservation(self.config)
            except Exception:
                logger.exception('Home wake reaction unavailable; occurrence remains completed')

    @staticmethod
    def _response_scope(value):
        return f"home-wake:{value['id']}:{value['revision']}"

    def _end_interaction(self, value, *, preserve_inflight=False, response_scope=None):
        if self.close_response_scope is not None:
            try:
                self.close_response_scope(response_scope or self._response_scope(value),
                                          preserve_inflight=preserve_inflight)
            except Exception:
                logger.exception('Home response-window withdrawal unavailable')
        requests = [request for request, identity in self._replies.items() if identity == value['id']]
        for request in requests:
            self._replies.pop(request, None)
            if not preserve_inflight and self.cancel_reply is not None:
                try:
                    self.cancel_reply(request)
                except Exception:
                    logger.exception('Home interactive reply cancellation unavailable')

    def _cancel_reaction(self, reason='withdrawn'):
        reaction, self._reaction = self._reaction, None
        self._reaction_observation = None
        if reaction is not None:
            try:
                reaction.cancel(self.cancel_speech)
                logger.info('Home wake reaction ended: reason=%s started=%s proposal=%s',
                    reason, reaction.started, reaction.handle.proposal_id if reaction.handle else None)
            except Exception:
                logger.exception('Home wake reaction cancellation unavailable')

    def _step_reaction(self, room, captured_mono, generation):
        reaction = self._reaction
        if reaction is None:
            return
        try:
            # A confirmed exit must not be erased by one jittering bed box.
            # Reuse the same fresh-frame duration/count as the exit decision.
            observed = self._reaction_observation
            if captured_mono is not None and captured_mono <= reaction.created_at:
                captured_mono = None
            observed.observe(room, captured_mono, generation, self.clock())
            returned_to_bed = not reaction.started and observed.bed_confirmed
            if self.wake_active():
                self._cancel_reaction('new_wake')
            elif returned_to_bed:
                self._cancel_reaction('bed_confirmed_again')
            elif not reaction.step(self.propose_speech):
                self._cancel_reaction(reaction.end_reason)
        except Exception:
            self._cancel_reaction()
            logger.exception('Home wake reaction failed; occurrence remains completed')

    def _pause(self, value, reason):
        self._end_interaction(value)
        self._withdraw(value['id'])
        value.update(state='paused', reason=reason, level=0, revision=value['revision']+1)
        self.store.save(value)
        logger.warning('Home wake paused: instance=%s reason=%s', value['id'], reason)

    def _prepare(self, value):
        if not self._output_retained and self.retain_output is not None:
            self._output_retained = True
            self.retain_output(True)
        self._bind_event_character(value, self.active_character())
        identity = value['id']
        future = self._outputs.get(identity)
        if future is not None and future.done():
            try:
                status = future.result()
            except Exception as exc:
                status = 'audio_preparation_exception:'+type(exc).__name__
            if value.get('output_status') != status:
                value.update(output_status=status, output_checked_at=self.wall_clock())
                self.store.save(value)
            ready = status == 'ready'
            if ready:
                self._outputs.pop(identity, None)
                return True
            self._outputs.pop(identity, None)
            self._output_retry_at[identity] = self.clock()+1
        if future is None and self.clock() >= self._output_retry_at.get(identity, 0):
            try:
                self._outputs[identity] = self.prepare_output()
                if self._outputs[identity].done():
                    return self._prepare(value)
                if value.get('output_status') != 'preparing':
                    value.update(output_status='preparing', output_checked_at=self.wall_clock())
                    self.store.save(value)
            except Exception as exc:
                status = 'audio_preparation_exception:'+type(exc).__name__
                if value.get('output_status') != status:
                    value.update(output_status=status, output_checked_at=self.wall_clock())
                    self.store.save(value)
                self._output_retry_at[identity] = self.clock()+1
        return False

    def _readiness(self):
        try:
            result = dict(self.check_readiness()) if self.check_readiness is not None else {}
        except Exception as exc:
            result = dict(error='readiness_unavailable:'+type(exc).__name__)
        warnings = []
        light = result.get('light', {})
        status = light.get('status')
        if status and status != 'ready':
            warnings.append({'needs_reset':'开灯手指需要在执行时复位',
                             'stale':'开灯手指的状态上报已过期，尚未确认',
                             'not_configured':'开灯手指未配置',
                             'not_ready':'开灯手指未就绪'}.get(status, '开灯设备状态未确认：'+status))
        battery = light.get('settings', {}).get('battery')
        if type(battery) in (int, float) and 0 <= battery <= 5:
            warnings.append(f'开灯手指最近上报电量仅 {battery:g}%，请检查电池；该读数不取消叫醒')
        camera = result.get('camera', {})
        if camera.get('device_present') is False:
            warnings.append('相机设备未找到，可能需要按视觉未知叫醒')
        if camera.get('calibrated') is False:
            warnings.append('床区和床外区域尚未校准，无法自动确认离床')
        if camera.get('missing_models'):
            warnings.append('人体模型文件缺失：'+', '.join(camera['missing_models']))
        if camera.get('error'):
            warnings.append('相机／视觉当前异常：'+camera['error'])
        if result.get('audio_endpoint') == 'desktop_unavailable':
            warnings.append('房间桌面音频端当前离线，尚不能确认到点可出声')
        if result.get('error'):
            warnings.append('叫醒设备检查未完成：'+result['error'])
        return dict(result, checked_at=self.wall_clock(), warnings=warnings)

    def _check_preparation(self, value, now):
        if self.check_readiness is None or not value['wake_at'] <= now < value['due_at']+15:
            return
        previous = value.get('preflight') or {}
        phase = 'due' if now >= value['due_at'] else 'preparation'
        if previous.get('phase') == phase and now-previous['checked_at'] < 30:
            return
        value['preflight'] = dict(self._readiness(), phase=phase)
        self.store.save(value)

    def refresh_bedtime(self):
        """Observe power recovery before any daily sensor or output decision."""
        with self._state_change():
            if self._closed or self.bedtime is None:
                return False
            resume_generation = self.bedtime.resume_generation
            self.bedtime.step()
            resumed = resume_generation != self.bedtime.resume_generation
            if resumed:
                self._observations.clear()
            return resumed

    def step(self, room, captured_mono=None, generation=0, *, environment_ready=True):
        with self._state_change():
            self._step(room, captured_mono, generation, environment_ready=environment_ready)
            self._sync_outcomes()

    def _step(self, room, captured_mono, generation, *, environment_ready):
        self._drain_reply_events()
        if self._closed:
            return
        self._flush_stop_saves()
        if self.refresh_bedtime():
            room, captured_mono, environment_ready = RoomObservation(), None, False
        if self.error:
            return
        if self.clock()-self._last_materialized >= 30:
            self._refresh()
        if self.bedtime is not None:
            if self.bedtime.suppress_alarm:
                self._cancel_reaction()
                return
        if not self.config.wake.enabled:
            self._cancel_reaction()
            return
        leader = self._select_executor()
        for value in tuple(self.instances.values()):
            if value.get('covered_by') or (value['due_at'] > self.wall_clock() and value is not leader):
                continue
            self._step_instance(value, room, captured_mono, generation, environment_ready)
        self._settle_joined()
        self._step_reaction(room, captured_mono, generation)

    def _step_instance(self, value, room, captured_mono, generation, environment_ready):
        now, identity = self.wall_clock(), value['id']
        if value['state'] in {'completed', 'cancelled'}:
            self._observations.pop(identity, None)
            return
        if value['state'] in {'scheduled', 'preparing'}:
            self._check_preparation(value, now)
        run = self._run_for(value)
        self._record_first_sound(value)
        if value['state'] == 'paused' and value['first_sound_at'] is None:
            # Later daily frames cannot rewrite a missed alarm as a successful
            # visual skip. Only an explicit resume opens a new start window.
            self._observations.pop(identity, None)
            return
        elapsed = self._elapsed(value)  # Reconstruct the monotonic origin after restart, including paused runs.
        if (value['state'] != 'paused' and value['first_sound_at'] is not None
                and elapsed >= self.config.wake.maximum_duration_seconds):
            self._finish_instance(value, 'duration_limit')  # Time limit is not evidence of waking up.
            return
        started = value['first_sound_at'] is not None
        if not started and now < value['camera_at']:
            if now >= value['wake_at'] and value['state'] in {'scheduled', 'preparing'}:
                self._prepare(value)
            return
        observed = self._observations.get(identity)
        if observed is None:
            observed = self._observations[identity] = WakeObservation(self.config)
        visual_since = max(value['camera_at'], value.get('light_ready_at',
            value.get('light_checked_at', value.get('light_requested_at', value['camera_at']))))
        first_frame_ready = (environment_ready and (
            (captured_mono is None or captured_mono >= run.started_mono) if started else
            room.captured_at is not None and visual_since <= room.captured_at <= now))
        previous_since = observed.since
        if not first_frame_ready:
            observed.reset_evidence()
        else:
            observed.observe(room, captured_mono, generation, self.clock())
        confirmation_reset = previous_since is not None and observed.since != previous_since
        if started and (confirmation_reset or self.clock()-observed.last_report_at >= 5):
            observed.last_report_at = self.clock()
            seconds = observed.last_at-observed.since if observed.since is not None else 0.
            age = round(self.clock()-observed.last_at, 3) if observed.last_at is not None else None
            logger.info('Home wake vision: instance=%s bed=%s outside=%s reason=%s leave_seconds=%.2f frames=%s frame_age=%s reset=%s camera_generation=%s',
                identity, observed.bed_occupied, observed.outside_occupied, observed.reason,
                seconds, observed.frames, age, confirmation_reset, generation)
        if value['state'] in {'scheduled', 'snoozed'}:
            value['state'] = 'preparing'
            self.store.save(value)
        attempt = run.attempt
        light_failed = (value.get('light_checked_at') is not None
                        and value.get('light_status') != 'requested')
        voice_only = (not observed.outside_confirmed(False) and observed.outside_occupied is not True
                      and (light_failed or now >= value['due_at']+self.VISUAL_START_BUDGET_SECONDS))
        can_start = observed.outside_confirmed(False) or voice_only
        if attempt is not None and value['first_sound_at'] is None and not can_start:
            # Queue admission is not actual sound. A changed scene invalidates
            # the pending first utterance without extending its due+15 budget.
            self._withdraw(identity)
            attempt = None
            value.update(state='preparing', reason='awaiting_region_confirmation', next_speech_at=value['due_at'])
            self.store.save(value)
        output_ready = self._prepare(value) if value['state'] != 'paused' and attempt is None else False
        if not started and now < value['due_at']:
            return
        if value['first_sound_at'] is None and observed.outside_confirmed(True):
            self._finish_instance(value, 'outside_occupied_at_start',
                evidence_seconds=observed.last_at-observed.outside_since, evidence_frames=observed.outside_frames)
            return
        if value['first_sound_at'] is not None and observed.confirmed:
            self._finish_instance(value, 'bed_empty_with_person_outside',
                evidence_seconds=observed.last_at-observed.since, evidence_frames=observed.frames)
            return
        in_bed = observed.bed_occupied is True
        if value['state'] == 'paused':
            return
        if value['first_sound_at'] is None and now >= value['due_at']+15:
            self._pause(value, 'output_unconfirmed:first_sound_deadline')
            return
        if identity in self._replies.values():
            return  # A real user reply owns the shared turn until presentation ends.
        if attempt is not None:
            if attempt.character != self.active_character():
                self._withdraw(identity)
                value['level'] = 0
                if value['first_sound_at'] is not None:
                    self._schedule_response_gap(value)
                self.store.save(value)
                return
            if not attempt.handle.result.done():
                return
            result = attempt.handle.result.result()
            run.detach()
            if result.status != 'completed' or result.audio_outcome != 'completed':
                if (self.fallback_enabled and not attempt.fallback
                        and (result.status in {'failed', 'audio_timeout', 'expired'}
                             or result.status == 'completed' and result.audio_outcome != 'completed')):
                    value['fallback_pending'] = True
                    value['reason'] = 'role_voice_unavailable:local_ringtone'
                    self.store.save(value)
                else:
                    if started and result.status == 'failed' and result.generation_failed:
                        value['reason'] = 'generation_failed_retry'
                        run.retry_generation(value)
                        self.store.save(value)
                        logger.warning('Home wake generation retry: instance=%s failures=%s',
                                       identity, run.generation_failures)
                        return
                    if result.status == 'cancelled':
                        value['reason'] = 'utterance_interrupted'
                        if started:
                            self._schedule_response_gap(value)
                        else:
                            value['next_speech_at'] = value['due_at']
                        if value['first_sound_at'] is None and now >= value['due_at']+15:
                            self._pause(value, 'output_unconfirmed:first_sound_deadline')
                            return
                        self.store.save(value)
                        return
                    self._pause(value, 'output_unconfirmed:' + result.status)
                    return
            if result.status == 'completed' and result.audio_outcome == 'completed':
                value['fallback_pending'] = False
                run.generation_failures = 0
                value['last_played_at'] = now
                self._schedule_response_gap(value)
                self.store.save(value)
        speech_due = run.speech_due(value, now)
        if self.clock() < speech_due:
            return
        if self.clock() >= speech_due+15:
            self._pause(value, 'output_unconfirmed:first_sound_deadline')
            return
        if value['first_sound_at'] is None and not can_start:
            return
        output_failed = value.get('output_status') not in {None, 'preparing', 'ready'}
        fallback = self.fallback_enabled and (value.get('fallback_pending', False)
            or not output_ready and (output_failed or now >= value['due_at']+8))
        if not output_ready and not fallback:
            return
        if fallback and run.last_release is not None and not run.last_release.done():
            return  # A prior voice attempt still owns playback; never overlap.
        cfg = self.config.wake
        elapsed = self._elapsed(value)
        level = min(2, int(2*elapsed/cfg.volume_ramp_seconds))
        volume = self._volume(value)
        character = self.active_character()
        self._bind_event_character(value, character)
        valid = threading.Event()
        valid.set()
        directive = ('已到本人约定的叫醒时间。' + ('最新有效画面在床区检出了人；这不证明这个人就是本人，也不证明是否睡着。' if in_bed else
            '视觉暂时无法确认本人位置；盖被或未检出人不代表离床，不要声称看见本人仍在床上。') +
            f'本次已叫醒约 {int(elapsed)} 秒，最多 {int(cfg.maximum_duration_seconds)} 秒；当前力度第 {level+1} 级。这些进度只用于把握语气，不逐轮朗读秒数、级别或规则。'
            '结合当前角色与实际互动，用一两句口语继续这次叫醒，初次温和，之后可以更直接。'
            '先接住本人最新一句的具体意思，已回答过的不用重答；没有新回应时简短提醒即可，不强造新话题、奖励或逐轮升级亲密。'
            '熟悉与亲近按角色和当下交流自然流露，直接关心、轻轻调侃或平常地叫名字都可以。此前生成稿不是必须延续的句式，不用每次先否认自己的关心。'
            '对方的任何自然话语都按角色互动，不承诺延后闹钟，也不把口头回应当作已离床。'
            '本人休假、撒娇或想赖床时，先接住感受，再继续邀请清醒；不能许可继续睡、约定稍后再叫或撤销这次叫醒。'
            '只依据实际材料说话，不编造身体接触、看见睡姿、已经准备早餐或将代做现实动作；想象必须让措辞本身表明是想象。'
            '每句话后会留出回应时间。不输出 NO_COMMENT。')
        value.update(state='calling', reason='', level=level, attempt=value['attempt']+1, last_attempt_at=now,
                     response_window_seconds=random.uniform(cfg.response_window_min_seconds, cfg.response_window_seconds))
        if value['first_sound_at'] is None:
            value.update(startup_mode='voice_only' if voice_only else 'visual',
                         startup_reason=('light:'+value['light_status'] if light_failed else observed.reason)
                         if voice_only else '')
        self.store.save(value)  # Claim before any generation or playback.
        request = wake_speech(directive, instance_id=identity,
            due_at=speech_due, volume=volume,
            response_window_seconds=value['response_window_seconds'],
            response_window_deadline=run.response_deadline(cfg.maximum_duration_seconds),
            response_scope_id=self._response_scope(value),
            event_binding=wake_event(value, facts=(f'vision={"bed_person" if in_bed else "unknown"}',)),
            fallback=fallback, reserve_fallback=self.fallback_enabled,
            is_current=lambda: valid.is_set() and self.active_character() == character
                and self._elapsed(value) < cfg.maximum_duration_seconds)
        handle = self.propose_speech(request)
        if handle is None:
            valid.clear()
            self._pause(value, 'output_unconfirmed:unavailable')
        else:
            run.attach(handle, valid, character, fallback=fallback)

    def close(self):
        with self._state_change():
            self._closed = True
            if not self._outputs_stopped:
                self._cancel_reaction()
                self._observations.clear()
                for value in self.instances.values():
                    if value['state'] == 'calling':
                        self._end_interaction(value)
                self._replies.clear()
                self._cancel_all()
                self._outputs_stopped = True
            # Admission is already closed, but a pending bedtime worker still
            # needs its final receipt consumed on subsequent close attempts.
            if self.bedtime is not None:
                self.bedtime.close()

    def _cancel_all(self):
        attempts = [(identity, run.detach()) for identity, run in tuple(self._runs.items())
                    if run.attempt is not None]
        for _, attempt in attempts:
            try:
                self.cancel_speech(attempt.handle.proposal_id)
            except Exception:
                logger.exception('Home speech cancellation failed')
        # Stop every output before recording received first-sound facts;
        # a failed save must not prevent cancellation or remaining cleanup.
        for identity, attempt in attempts:
            try:
                self._record_first_sound(self.instances[identity], attempt)
            except Exception:
                logger.exception('Home first sound persistence failed')

    def _bind_event_character(self, value, character):
        participants = value.setdefault('event_characters', [])
        if character not in participants:
            participants.append(character)
            return True
        return False

    def _sync_outcomes(self, *, force=False):
        if self._closed or self.record_outcome is None or not force and self.clock() < self._next_outcome_sync:
            return
        self._next_outcome_sync = self.clock()+1
        try:
            pending = self.store.pending_outcomes()
            live = set()
            for value in pending:
                for character in value.get('event_characters', ()):
                    revision = value['revision']
                    if value.get('outcome_evidence', {}).get(character) == revision:
                        continue
                    key = (value['id'], revision, character)
                    live.add(key)
                    future = self._outcome_jobs.get(key)
                    try:
                        if future is None:
                            if self.clock() < self._outcome_retry_at.get(key, 0):
                                continue
                            payload = {k: v for k, v in value.items() if k != 'outcome_evidence'}
                            future = self.record_outcome({**payload, 'character_id': character}, value.get('ended_at'))
                            if future is not None:
                                self._outcome_jobs[key] = future
                        if future is not None:
                            if not future.done():
                                continue
                            future.result()  # None return above is a synchronous sink acknowledgement.
                        if self.store.confirm_outcome(value['id'], revision, character):
                            cached = self.instances.get(value['id'])
                            if cached is not None and cached['revision'] == revision:
                                cached.setdefault('outcome_evidence', {})[character] = revision
                        self._outcome_jobs.pop(key, None)
                        self._outcome_retry_at.pop(key, None)
                    except Exception as exc:
                        self._outcome_jobs.pop(key, None)
                        self._outcome_retry_at[key] = self.clock()+1
                        logger.warning('Home outcome evidence pending: %s', type(exc).__name__)
            self._outcome_jobs = {key: job for key, job in self._outcome_jobs.items() if key in live}
            self._outcome_retry_at = {key: at for key, at in self._outcome_retry_at.items() if key in live}
        except Exception as exc:
            # Memory bookkeeping is after the business STOP/release decisions.
            logger.warning('Home outcome synchronization unavailable: %s', type(exc).__name__)

    def fail(self, error):
        with self._state_change():
            self.error = type(error).__name__ + ': ' + str(error)
            self._cancel_reaction()
            for value in self.instances.values():
                if value['state'] == 'calling':
                    self._end_interaction(value)
            self._cancel_all()
            for value in self.instances.values():
                if value['state'] in {'calling', 'preparing'}:
                    value.update(state='paused', reason='home_failed', revision=value['revision']+1)
                    try:
                        self.store.save(value)
                    except Exception:
                        logger.exception('Home failed occurrence persistence unavailable')
            if self.bedtime is not None:
                try:
                    self.bedtime.cancel(reason='home_failed')
                except Exception:
                    logger.exception('Home bedtime cancellation persistence failed')

    def _region_status(self):
        return {identity: dict(bed_occupied=observation.bed_occupied,
                              outside_bed_occupied=observation.outside_occupied,
                              reason=observation.reason, exit_confirmed=observation.confirmed,
                              exit_frames=observation.frames, outside_frames=observation.outside_frames,
                              outside_confirmed=(observation.outside_confirmed(True)
                                                 or observation.outside_confirmed(False)),
                              camera_generation=observation.generation)
                for identity, observation in self._observations.items()
                if self.instances[identity]['state'] not in {'completed', 'cancelled'}}

    def snapshot(self):
        with self._lock:
            now = self.wall_clock()
            latest = max((value for value in self.instances.values()
                if value['due_at'] <= now and value['state'] != 'scheduled' and not value.get('covered_by')),
                key=lambda value: value['due_at'], default=None)
            return dict(enabled=self.config.wake.enabled, error=self.error,
                running=not self._closed and not self.error, regions=self._region_status(),
                vision_thresholds_configured=self.config.wake.leave_bed_seconds is not None,
                instances=[dict(value) for value in self.instances.values() if value['state'] not in {'completed', 'cancelled'}],
                last_execution=dict(latest) if latest is not None else None,
                bedtime=dict(self.bedtime.intent) if self.bedtime and self.bedtime.intent else None)

    def prepare_bedtime_from_text(self, *, wake_date=None, wake_time=None, timezone='Asia/Shanghai', no_wake=False):
        origin = self._require_origin()
        text = origin[2].strip()
        sleep_request = bool(re.search(
            r'(?:^|[，,。！!；;\n])\s*(?:(?:Sana|Spica|请|帮我|麻烦你)[\s，,。！!]*)*'
            r'(?:我(?:准备|要|去|先)?睡觉了|我先睡了|我要睡了|我去睡了|继续睡(?:觉)?(?:了)?|晚安)'
            r'(?=$|[\s，,。！!；;])', text, re.IGNORECASE))
        refusal = bool(re.search(
            r'(?:不要|不用|不需要|别|暂不)(?:进入)?(?:睡|挂起|休眠|晚安)|(?:如果|假如|假设|举例)'
            r'|(?:你|他|她)(?:刚才|之前|曾经)?(?:说|问)', text))
        no_wake_request = bool(re.search(r'(?:不用|不要|不需要|无需)(?:再)?(?:叫我|叫醒|闹钟)', text))
        pending_answer = bool(self.bedtime and self.bedtime.intent
            and self.bedtime.intent['state'] == 'awaiting_alarm'
            and (wake_date and wake_time or no_wake_request))
        if refusal or not (sleep_request or pending_answer):
            raise PermissionError('当前本人请求未明确进入晚安，未安排挂起')
        if no_wake and not no_wake_request:
            raise PermissionError('本人尚未明确表示本次不用叫醒')
        if (wake_date is None) != (wake_time is None) or no_wake and wake_time:
            raise ValueError('请明确起床日期和时间，或明确本次不用叫醒')
        if self.wake_active():
            raise ValueError('叫醒尚未结束，不能开始新的晚安')
        plan = None
        if wake_date is not None:
            if timezone != 'Asia/Shanghai':
                raise ValueError('请按家庭时区 Asia/Shanghai 指定叫醒时间')
            plan = self.set_from_text(local_time=wake_time, on_date=wake_date)
        result = self.prepare_bedtime(no_wake=no_wake)
        if plan is not None:
            result['plan'] = plan
        return result

    def restore_daily_from_text(self):
        from spica.home.tools import explicit_daily_restore_request
        if not explicit_daily_restore_request(self._require_origin()[2]):
            raise PermissionError('本人尚未明确结束晚安或跳过本次叫醒')
        return self.restore_daily()

    def prepare_bedtime(self, *, reply_required=True, instance_id=None, no_wake=False, stay_awake=None):
        origin = self._require_origin()
        self._require_running()
        if self.bedtime is None or not self.config.wake.enabled:
            raise RuntimeError('睡前与叫醒能力尚未启用')
        if stay_awake is not None and type(stay_awake) is not bool:
            raise ValueError('stay_awake 必须为布尔值')
        with self._state_change():
            if stay_awake is None:
                intent = self.bedtime.intent or {}
                stay_awake = (intent.get('power_mode') == 'awake'
                    and intent.get('state') in {'night', 'awaiting_alarm', 'morning_preparation'})
            self._refresh()
            for active in self.instances.values():
                self._record_first_sound(active)
                if active['first_sound_at'] is not None and active['state'] not in {'completed', 'cancelled'}:
                    raise ValueError('叫醒尚未结束，不能开始新的晚安；明确开关灯仍可用')
            if self.wake_active():
                raise ValueError('正在准备或执行叫醒，不能开始新的晚安')
            self._cancel_reaction()
            candidates = [value for value in self.instances.values()
                          if value['state'] in {'scheduled', 'preparing', 'snoozed'}
                          and self.wall_clock() < value['due_at']
                          and (value['id'] == instance_id or value['due_at'] <= self.wall_clock()+86400)]
            if instance_id is not None:
                chosen = next((value for value in candidates if value['id'] == instance_id), None)
                if chosen is None:
                    raise ValueError('指定的叫醒实例已失效或时间已过')
                recurring = {value['id'] for value in self.store.schedules() if value['weekdays']}
                earlier = [value for value in candidates if value['schedule_id'] in recurring
                           and value['day'] == chosen['day'] and value['due_at'] < chosen['due_at']]
                if len(earlier) > 1:
                    raise ValueError('有多个固定安排，请先在闹钟面板明确保留哪一个')
                if earlier:
                    chosen['supersedes_instance_id'] = earlier[0]['id']
                    self.store.save(chosen)
                    self._apply_bedtime_time_override()
                    candidates = [value for value in candidates if value['state'] != 'cancelled']
            value = min(candidates, key=lambda item: item['due_at']) if candidates else None
            if (value is None and not no_wake and self.bedtime.intent
                    and self.bedtime.intent['state'] == 'awaiting_alarm'):
                result = self.bedtime.continue_request(request_id=origin[0], require_audio=origin[3],
                    reply_required=reply_required)
                return dict(result, ask_wake_time=False)
            if no_wake and value is not None:
                if len(candidates) > 1:
                    raise ValueError('有多个叫醒安排，请先明确要跳过哪一个')
                self.control(action='cancel', instance_id=value['id'])
                value = None
            if value is not None:
                value['wake_at'] = value['due_at']-300
                self.store.save(value)
            intent = self.bedtime.intent
            if intent and intent['state'] not in {
                    'cancelled', 'resumed', 'failed', 'recovering', 'cancelling', 'night', 'morning_preparation'}:
                self._reconcile_bedtime()
                result = self.bedtime.continue_request(request_id=origin[0], require_audio=origin[3],
                    reply_required=reply_required, no_wake=no_wake)
            else:
                result = self.bedtime.begin(wake_at=value['wake_at'] if value else None,
                    instance_id=value['id'] if value else None, request_id=origin[0], require_audio=origin[3],
                    reply_required=reply_required, awaiting_alarm=value is None and not no_wake,
                    stay_awake=stay_awake, wake_readiness=self._readiness() if value is not None else None)
            if value is not None:
                from datetime import datetime
                from zoneinfo import ZoneInfo
                result['wake_datetime'] = datetime.fromtimestamp(value['due_at'], ZoneInfo('Asia/Shanghai')).isoformat()
            result['ask_wake_time'] = result['state'] == 'awaiting_alarm'
            return result

    def restore_daily(self):
        origin = self._require_origin()
        with self._state_change():
            key = json.dumps(['restore_daily', origin[0], origin[1]])
            previous = self.store.command_result(key)
            if previous is not None:
                return previous
            for value in self.instances.values():
                self._record_first_sound(value)
                if value['first_sound_at'] is not None and value['state'] not in {'completed', 'cancelled'}:
                    raise ValueError('已经起播，本次仍按持续离床或十分钟结束')
            intent = self.bedtime.intent if self.bedtime else None
            identity = (intent.get('instance_id') if intent
                and intent['state'] not in {'cancelled', 'resumed', 'failed'} else None)
            linked = self.instances.get(identity)
            if linked is None or linked['state'] in {'completed', 'cancelled'}:
                # Without a live bedtime binding, only the occurrence already
                # preparing/calling belongs to this wake. A future schedule is
                # another sleep, even when it happens to be less than 24h away.
                candidates = [value for value in self.instances.values()
                    if value['first_sound_at'] is None and value['state'] in {'preparing', 'calling'}]
                identity = min(candidates, key=lambda value:value['due_at'])['id'] if candidates else None
            receipt = dict(kind='restore_daily', request_id=origin[0], instance_id=identity,
                           state='unconfirmed', reason='operation_unconfirmed')
            if not self.store.claim_command(key, receipt):
                return self.store.command_result(key)
            if identity is not None:
                self.control(action='cancel', instance_id=identity)
            if self.bedtime and self.bedtime.intent:
                receipt.update(self.bedtime.cancel(reason='owner_restored_daily'))
            else:
                receipt.update(state='resumed', reason='current_alarm_skipped' if identity else 'already_daily')
            self.store.finish_command(key, receipt)
            return receipt

    def cancel_bedtime(self):
        self._require_origin()
        with self._state_change():
            return self.bedtime.cancel() if self.bedtime else dict(state='cancelled', reason='not_enabled')

    def observe_reply(self, event):
        if self.bedtime:
            self.bedtime.observe_reply(event)
        if not self._closed and getattr(event, 'kind', '') == 'desktop_presentation_terminal':
            # Driver callbacks can hold their presentation dispatch lock. Like
            # HomeBedtime, enqueue only: never wait for the Home business lock.
            self._reply_events.put((event.request_id, self.wall_clock(), self.clock()))

    def _drain_reply_events(self):
        while True:
            try:
                request_id, finished_at, finished_mono = self._reply_events.get_nowait()
            except queue.Empty:
                return
            self._reply_terminals.append(request_id)
            identity = self._replies.pop(request_id, None)
            value = self.instances.get(identity)
            if value is not None and value['state'] == 'calling':
                self._schedule_response_gap(value, finished_wall=finished_at, finished_mono=finished_mono)
                self.store.save(value)

    def observe_text_delivery(self, request_id, delivered):
        if self.bedtime:
            self.bedtime.observe_text_delivery(request_id, delivered)

    def control_text(self, text):
        """Closed-set controls before conversational busy admission; no LLM reply."""
        self._require_origin()
        from spica.home.tools import parse_home_control
        action = parse_home_control(text)
        if action == 'query':
            return dict(kind='query', result=self.query())
        if action == 'cancel_bedtime':
            if not self.bedtime or not self.bedtime.intent:
                return None
            return dict(kind='bedtime', result=self.cancel_bedtime())
        if action == 'restore_daily':
            # Spoken acknowledgement during an active wake stays ordinary
            # role dialogue; it never ends the wake occurrence.
            with self._state_change():
                if self._interactive_instance() is not None:
                    return None
            return dict(kind='bedtime', result=self.restore_daily())
        with self._state_change():
            if action is None:
                value = self._interactive_instance()
                if value is None:
                    return None
                cfg = self.config.wake
                value['response_window_seconds'] = random.uniform(cfg.response_window_min_seconds, cfg.response_window_seconds)
                self.store.save(value)
                return dict(kind='interaction', result=dict(volume=self._volume(value),
                    response_window_seconds=value['response_window_seconds']))
            return dict(kind='alarm', result=self.control(action=action))

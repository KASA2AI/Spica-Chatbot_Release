"""Cancellable bedtime intent; saving it or stopping the core is not sleep."""
from concurrent.futures import Future
from copy import deepcopy
import logging
import queue
import threading
import time
import uuid

from spica.runtime.jobs import ThreadJobRunner

logger = logging.getLogger(__name__)


class HomeBedtime:
    def __init__(self, config, store, power, *, clock=time.monotonic, wall_clock=time.time,
                 resources_ready=lambda: True, light_off=None, indicators_off=None, indicators_on=None,
                 indicators_settled=None, display_off=None):
        self.config, self.store, self.power = config, store, power
        self.clock, self.wall_clock = clock, wall_clock
        self.resources_ready = resources_ready
        self.light_off = light_off
        self.indicators_off = indicators_off
        self.indicators_on = indicators_on
        self.indicators_settled = indicators_settled
        self._indicator_job = None
        self._indicator_next_check = 0
        self._indicator_cancelled = threading.Event()
        self.display_off = display_off
        self.resume_generation = 0
        self.jobs = ThreadJobRunner()
        self.intent = store.bedtime()
        self._cancelled = threading.Event()
        self._replies = queue.SimpleQueue()
        self._future = None
        self._phase = None
        self._closed = False
        self._cleanup_complete = False
        self._reply_finished = None
        self._audio_units = {}
        self._presentation = None
        self._released = False
        self._boot_id = power.boot_id()
        self._resume_stamp = power.resume_stamp()
        if self.intent and self.intent['state'] not in {'cancelled', 'resumed', 'failed', 'morning_preparation', 'night', 'awaiting_alarm'}:
            changed = self.intent.get('boot_id') != self._boot_id
            receipt = self.intent.get('receipt') or {}
            if receipt.get('suspend_attempted') and not changed:
                self.intent.update(state='suspend_unknown', reason='core_restarted_with_suspend_request')
                self._save()
                return
            self.intent.update(state='recovering', reason='host_booted' if changed else 'core_restarted_without_boot',
                               system_boot_observed=changed)
            if self.intent.pop('pending_alarm', None):
                self.intent['rtc_sync'] = dict(status='unconfirmed', reason='核心重启中断改期，需重新确认睡前状态')
            self._save()
            self._start_cleanup()

    @property
    def suppress_daily(self):
        if self.intent and self.intent['state'] == 'cancelling':
            return bool((self.intent.get('receipt') or {}).get('suspend_attempted'))
        if self.intent and self.intent['state'] in {'suspend_unknown', 'suspend_requested', 'shutdown_unknown'}:
            return bool(self.intent.get('previous_night') or self.wall_clock() < self.intent.get('deadline', 0))
        return bool(self.intent and self.intent['state'] not in {'cancelled', 'resumed', 'failed'})

    @property
    def suppress_alarm(self):
        if self.intent and self.intent['state'] in {'suspend_requested', 'suspend_unknown'}:
            return self.wall_clock() < self.intent.get('deadline', 0)
        return bool(self.intent and (self.intent['state'] in {'arming', 'retiming', 'waiting_reply', 'suspending', 'suspend_requested', 'suspend_unknown', 'shutdown_scheduled'}
            or self.intent['state'] == 'recovering' and self.intent.get('system_boot_observed')))

    def _save(self):
        self.store.save_bedtime(self.intent, preserve_receipt=True)

    def begin(self, *, wake_at, instance_id, request_id, require_audio, reply_required=True,
              awaiting_alarm=False, stay_awake=False, wake_readiness=None):
        self._consume_indicators()
        if self.intent and self.intent['request_id'] == request_id:
            return deepcopy(self.intent)
        if self._closed or not self.jobs.is_idle or (self.intent and self.intent.get('indicator_cleanup_pending')) or (self.intent
                and self.intent['state'] not in {'cancelled', 'resumed', 'failed', 'night', 'awaiting_alarm', 'morning_preparation'}):
            raise RuntimeError('已有睡前操作正在处理，请先查询或取消')
        now = self.wall_clock()
        previous_night = bool(self.intent and self.intent['state'] in {'night', 'morning_preparation'})
        self.intent = dict(id=uuid.uuid4().hex, request_id=request_id, instance_id=instance_id,
            wake_at=wake_at, state='arming', reason='', boot_id=self._boot_id,
            power_mode='awake' if stay_awake else 'suspend',
            explicit_no_wake=instance_id is None and not awaiting_alarm,
            previous_daily_enabled=self.config.daily_detection_enabled,
            previous_night=previous_night,
            wake_readiness=deepcopy(wake_readiness),
            deadline=now+self.config.wake.bedtime_prepare_timeout_seconds,
            require_audio=require_audio, receipt=None)
        self._cancelled = threading.Event()
        self._reply_finished = True if not reply_required else None
        self._audio_units, self._presentation = {}, None
        self._released = False
        self.store.save_bedtime(self.intent)
        if self.light_off is not None:
            try:
                self.intent['light_off'] = self.light_off()
            except Exception:
                self.intent['light_off'] = dict(press_status='unconfirmed', light_state='unknown')
                logger.exception('Bedtime light-off result unavailable')
            self._save()
        if self.indicators_off is not None:
            self._start_indicators(False)
        if awaiting_alarm:
            self.intent.update(state='awaiting_alarm', reason='ask_wake_time_once', wake_time_question_issued=True)
            self._save()
        elif wake_at is not None and wake_at <= now:
            self.intent.update(state='morning_preparation', reason='wake_time_too_close')
            self._save()
        elif stay_awake:
            self.intent.update(state='waiting_reply', reason='owner_requested_awake_night')
            self._save()
        elif not self.config.wake.power_control_enabled:
            self.intent.update(state='night' if previous_night else 'failed', reason='power_control_disabled')
            self._save()
        else:
            self._job('arm', lambda record: self.power.prepare_suspend(wake_at, self._cancelled, record=record))
        return deepcopy(self.intent)

    def _start_indicators(self, enabled):
        # Both directions share this owner's finite job lifetime. The worker
        # receives an immutable operation/token pair and never updates intent.
        if enabled is None:
            pending, check = deepcopy(self.intent['indicator_cleanup_pending']), self.indicators_settled
            operation = lambda cancelled: check(pending)
        else:
            operation = self.indicators_on if enabled else self.indicators_off
        cancelled = self._indicator_cancelled if enabled else self._cancelled
        if enabled is not None:
            key = 'indicator_restore' if enabled else 'indicator_lights'
            self.intent[key] = {'status': 'pending'}
            self._save()
        future = Future()
        self._indicator_job = self.intent['id'], enabled, future

        def run():
            try:
                result = operation(cancelled)
            except Exception:
                logger.exception('Home indicator lights result unavailable')
                result = {'status': 'unconfirmed'}
            future.set_result(result)

        self.jobs.submit(run)

    def _consume_indicators(self):
        if self._indicator_job is None:
            return
        identity, enabled, future = self._indicator_job
        if future.done():
            pending = {}
            if self.intent and self.intent['id'] == identity:
                devices = future.result()
                if enabled is None:
                    if devices is True:
                        self.intent.pop('indicator_cleanup_pending', None)
                    self._indicator_next_check = self.clock()+5
                elif enabled:
                    interrupted = any(isinstance(value, dict) and value.get('status') in {'cancelled', 'cleanup_pending'}
                                      for value in devices.values())
                    self.intent['indicator_restore'] = dict(status='pending' if interrupted else 'finished', devices=devices)
                else:
                    self.intent['indicator_lights'] = devices
                if enabled is not None:
                    pending = {key: value for key, value in devices.items()
                               if isinstance(value, dict) and value.get('status') == 'cleanup_pending'}
                    if pending:
                        self.intent['indicator_cleanup_pending'] = pending
                        self._indicator_next_check = self.clock()+5
                        self._cancelled.set()
                self._save()
            self._indicator_job = None
            if pending and not self._closed:
                self.cancel(reason='indicator_cleanup_pending')

    def _check_indicator_cleanup(self, *, closing=False):
        if (self.intent and self.intent.get('indicator_cleanup_pending') and self.indicators_settled is not None
                and self._indicator_job is None and self.jobs.is_idle
                and (closing or self.clock() >= self._indicator_next_check)):
            self._start_indicators(None)

    def continue_request(self, *, request_id, require_audio, reply_required, no_wake=False):
        """A bedtime answer owns its own reply; it does not repeat room actions."""
        self._bind_reply(request_id, require_audio, reply_required=reply_required)
        self.intent['explicit_no_wake'] = no_wake
        self._save()
        if no_wake and self.intent['state'] == 'awaiting_alarm':
            self.reconcile_alarm(None, 0, force=True)
        return deepcopy(self.intent)

    def _bind_reply(self, request_id, require_audio, *, reply_required=True):
        if self.intent['request_id'] != request_id:
            self.intent.update(request_id=request_id, require_audio=require_audio,
                deadline=self.wall_clock()+self.config.wake.bedtime_prepare_timeout_seconds)
            self._reply_finished = None if reply_required else True
            self._audio_units, self._presentation, self._released = {}, None, False

    def reconcile_alarm(self, next_alarm, revision, *, force=False, reply_request=None):
        """Replace an owned RTC only while the original bedtime is revocable."""
        if not self.intent or self.intent['state'] in {'cancelled', 'resumed', 'failed', 'cancelling', 'recovering'}:
            return
        target = dict(instance_id=next_alarm['id'] if next_alarm else None,
                      wake_at=next_alarm['wake_at'] if next_alarm else None, revision=revision)
        if (target['instance_id'], target['wake_at']) == (self.intent.get('instance_id'), self.intent.get('wake_at')):
            if self.intent['state'] != 'retiming' and not force:
                return
        if next_alarm is not None:
            self.intent['explicit_no_wake'] = False
        saved = self.store.bedtime()
        receipt = (saved or {}).get('receipt') or self.intent.get('receipt') or {}
        if (self.intent['state'] in {'suspend_requested', 'suspend_unknown', 'shutdown_unknown', 'shutdown_scheduled'}
                or receipt.get('suspend_attempted') and not self.power.resumed_since(receipt)):
            self.intent['rtc_sync'] = dict(status='unconfirmed', target=target,
                reason='挂起已提交或结果未知；闹钟已保存，保留原 RTC，未确认新时间可自动唤醒')
            self._save()
            return
        if reply_request is not None:
            self._bind_reply(*reply_request)
        self.intent.update(pending_alarm=target, rtc_sync=dict(status='pending', target=target))
        self._cancelled.set()  # Old arm/suspend jobs must finish cleanup first.
        self.intent['state'] = 'retiming'
        self._save()
        if self._future is None:
            self.intent['receipt'] = deepcopy(receipt)
            self._start_cleanup()

    def _finish_retime(self):
        if self._indicator_job is not None or not self.jobs.is_idle or self.intent.get('indicator_cleanup_pending'):
            return
        target = self.intent.pop('pending_alarm')
        # All old writes are finished before changing the operation identity.
        self.intent.update(id=uuid.uuid4().hex, receipt=None, instance_id=target['instance_id'],
                           wake_at=target['wake_at'], state='arming', reason='alarm_plan_changed',
                           deadline=self.wall_clock()+self.config.wake.bedtime_prepare_timeout_seconds)
        self._cancelled = threading.Event()
        self.store.save_bedtime(self.intent)
        now, wake_at = self.wall_clock(), target['wake_at']
        margin = self.config.wake.suspend_margin_seconds
        if target['instance_id'] is None and not self.intent.get('explicit_no_wake'):
            self.intent.update(state='awaiting_alarm', reason='alarm_plan_empty')
            self.intent['rtc_sync'] = dict(status='not_required', reason='没有有效闹钟，保持运行')
        elif self._keeps_running():
            self.intent['state'] = self._resting_state() if self._reply_finished else 'waiting_reply'
            self.intent['rtc_sync'] = dict(status='not_required', reason='保持运行，等待新的叫醒时间')
        elif (wake_at is not None and (margin is None or wake_at <= now + margin)) or not self.config.wake.power_control_enabled:
            self.intent.update(state=self._resting_state(), reason='retimed_alarm_stay_awake')
            self.intent['rtc_sync'] = dict(status='not_required', reason='保持运行准备叫醒')
        else:
            self._job('arm', lambda record: self.power.prepare_suspend(wake_at, self._cancelled, record=record))
        self._save()

    def _restore_indicators(self):
        self._consume_indicators()
        if (self._closed or self.indicators_on is None or self._indicator_job is not None
                or not self.jobs.is_idle or not self.intent
                or self.intent.get('indicator_cleanup_pending')
                or self.intent['state'] not in {'resumed', 'cancelled', 'failed'}
                or 'indicator_lights' not in self.intent
                or (self.intent.get('indicator_restore') or {}).get('status') == 'finished'):
            return
        self._start_indicators(True)

    def _job(self, phase, operation):
        future = self._future = Future()
        self._phase = phase
        cancelled, intent = self._cancelled, deepcopy(self.intent)
        last_receipt = deepcopy(intent.get('receipt') or {})

        def record(receipt):
            nonlocal last_receipt
            last_receipt = deepcopy(receipt)
            self.store.save_power_receipt(intent['id'], last_receipt)

        def run():
            try:
                result = operation(record)
            except Exception as exc:
                result = dict(last_receipt, status='unknown', reason=str(exc))
            if cancelled.is_set() and phase != 'cleanup':
                try:
                    result = dict(self.power.cancel(result, closing=self._closed), cleanup_attempted=True)
                except Exception as exc:
                    result = dict(result, status='unknown', reason=str(exc), cleanup_attempted=True)
            try:
                record(result)
            except Exception as exc:
                logger.exception('Home power receipt persistence failed')
                result = dict(result, status='unknown', reason='power_receipt_not_saved: ' + str(exc))
            finally:
                # Receipt/storage failure cannot orphan the future or cleanup.
                future.set_result(result)

        self.jobs.submit(run)

    def observe_reply(self, event):
        # Core/driver callbacks only enqueue metadata; never wait on Home locks.
        if self.intent and getattr(event, 'request_id', None) == self.intent['request_id']:
            self._replies.put(event)

    def observe_text_delivery(self, request_id, delivered):
        if self.intent and request_id == self.intent['request_id']:
            self._replies.put(('text', request_id, delivered))

    def _consume_replies(self):
        while not self._replies.empty():
            event = self._replies.get_nowait()
            request_id = event[1] if isinstance(event, tuple) else event.request_id
            if request_id != self.intent['request_id']:
                continue
            if isinstance(event, tuple):
                self._reply_finished = event[2]
            elif event.kind == 'desktop_audio_playback':
                self._audio_units[event.unit_index] = event.outcome
            elif event.kind == 'desktop_presentation_terminal':
                self._presentation = event
            elif event.kind == 'desktop_turn_lifecycle_released':
                self._released = True
        if self._released and self._presentation is not None:
            audio_ok = (not self.intent['require_audio']
                        or bool(self._audio_units) and set(self._audio_units.values()) == {'completed'}
                        or self._presentation.awaited_audio_playback)
            self._reply_finished = self._presentation.outcome == 'completed' and audio_ok

    def _start_cleanup(self, *, closing=False):
        receipt = deepcopy(self.intent.get('receipt') or {})
        after_boot = bool(self.intent.get('system_boot_observed'))
        self._job('cleanup', lambda record: self.power.cancel(receipt, closing=closing, after_boot=after_boot))

    def cancel(self, *, reason='owner_cancelled'):
        if not self.intent:
            return dict(state='cancelled', reason='no_bedtime_intent')
        self._cancelled.set()  # Optional off may still be draining after a power failure.
        if self.intent['state'] in {'cancelled', 'resumed', 'failed'}:
            return deepcopy(self.intent)
        self.intent.pop('pending_alarm', None)
        if reason == 'owner_restored_daily':
            self.intent['previous_night'] = False
        self.intent.update(state='cancelling', reason=reason)
        try:
            self._save()
        finally:
            if self._future is None:
                self._start_cleanup()
        return deepcopy(self.intent)

    def _cleanup_state(self, result):
        if result['status'] == 'shutting_down':
            return 'shutdown_scheduled'
        if result['status'] != 'cancelled':
            return 'suspend_unknown' if result.get('suspend_attempted') else 'shutdown_unknown'
        if self.intent.get('resume_observed') and not self._cancelled.is_set():
            return self._resting_state()
        if self.intent.get('stay_awake') and not self._cancelled.is_set():
            return self._resting_state()
        if self.intent.get('system_boot_observed') and not self._cancelled.is_set():
            if self.intent.get('power_mode') == 'suspend':
                return self._resting_state()
            identity = self.intent.get('instance_id')
            value = self.store.get(identity) if identity else None
            if value and value['state'] not in {'completed', 'cancelled', 'paused'} and self.wall_clock() <= value['due_at']+15:
                return 'morning_preparation'
            return 'resumed'
        return 'night' if self.intent.get('previous_night') else 'cancelled'

    def _resting_state(self):
        wake_at = self.intent.get('wake_at')
        return 'morning_preparation' if wake_at is not None and self.wall_clock() >= wake_at else 'night'

    def _keeps_running(self):
        return (self.intent.get('power_mode') == 'awake' or self.intent.get('resume_observed')
                or self.intent.get('system_boot_observed'))

    def _validate_alarm(self):
        identity = self.intent.get('instance_id')
        if identity is None:
            return
        value = self.store.get(identity)
        if (value['state'] not in {'scheduled', 'preparing', 'snoozed'}
                or value['first_sound_at'] is not None
                or value['due_at']-300 != self.intent['wake_at']):
            raise RuntimeError('本次叫醒安排已改变，未继续挂起')
        next_alarm = self.store.plan_snapshot(self.wall_clock())['next']
        if next_alarm is not None and next_alarm['due_at'] < value['due_at']:
            raise RuntimeError('已有更早叫醒安排，未继续挂起')

    def _consume_power(self):
        if self._future is None or not self._future.done():
            return
        result, phase = self._future.result(), self._phase
        self._future = None
        self.intent['receipt'] = result
        if self.intent['state'] == 'retiming' and not self._closed:
            if phase == 'cleanup' or result.get('cleanup_attempted'):
                if result['status'] == 'cancelled':
                    self._save()
                    self._finish_retime()
                else:
                    self.intent.update(state=self._cleanup_state(result), rtc_sync=dict(
                        status='unconfirmed', reason=result.get('reason') or '旧 RTC 尚未确认清理，未写入新时间'))
                    self._save()
            else:
                self._start_cleanup()
            return
        if phase == 'cleanup' or result.get('cleanup_attempted'):
            self.intent.update(state=self._cleanup_state(result), reason=result.get('reason') or self.intent.get('reason', ''))
            if (self.intent.get('rtc_sync') or {}).get('status') == 'pending':
                self.intent['rtc_sync'] = dict(status='unconfirmed', reason=self.intent.get('reason') or '睡前改期未完成，保持原故障收口')
            elif result['status'] == 'cancelled' and (self.intent.get('rtc_sync') or {}).get('status') == 'verified':
                self.intent['rtc_sync'] = dict(status='not_required', reason='当前保持运行，已保存的闹钟仍然有效')
        elif self._cancelled.is_set():
            self.intent['state'] = 'cancelling'
            self._start_cleanup(closing=self._closed)
        elif phase == 'arm' and result['status'] == 'verified':
            self.intent['state'] = 'waiting_reply'
            self.intent['rtc_sync'] = dict(status='verified', wake_at=self.intent.get('wake_at'))
        elif phase == 'suspend' and result['status'] in {'suspend_requested', 'unknown'}:
            self.intent.update(state='suspend_requested' if result['status'] == 'suspend_requested' else 'suspend_unknown',
                               reason=result.get('reason', ''))
        elif phase == 'suspend' and result['status'] == 'stay_awake':
            self.intent.update(state='recovering', stay_awake=True, reason=result['reason'])
            self._start_cleanup()
        else:
            self.intent.update(state='recovering', reason=result.get('reason', 'power_operation_failed'))
            self.intent['rtc_sync'] = dict(status='unconfirmed', reason=self.intent['reason'])
            self._start_cleanup()
        self._save()

    def step(self):
        if self._closed:
            return
        stamp = self.power.resume_stamp()
        resumed_now = (stamp['boot_id'] == self._resume_stamp['boot_id']
                       and stamp['suspend_count'] > self._resume_stamp['suspend_count'])
        self._resume_stamp = stamp
        if resumed_now:
            self.resume_generation += 1
        if not self.intent:
            return
        self._consume_replies()
        now = self.wall_clock()
        self._consume_indicators()
        self._consume_power()
        if (self.intent['state'] == 'retiming' and self._future is None
                and (self.intent.get('receipt') or {}).get('status') == 'cancelled'):
            self._finish_retime()
        state = self.intent['state']
        if state in {'arming', 'waiting_reply'} and (
                now >= self.intent['deadline'] or self._reply_finished is False):
            self.cancel()
            return
        if (state == 'waiting_reply' and self._reply_finished is True
                and self._indicator_job is None and not self.intent.get('indicator_cleanup_pending')
                and self.resources_ready()):
            if self._keeps_running():
                if self.display_off is not None:
                    try:
                        self.intent['display_off'] = self.display_off()
                    except Exception:
                        self.intent['display_off'] = {'status': 'unconfirmed'}
                        logger.exception('Bedtime display-off result unavailable')
                self.intent['state'] = self._resting_state()
                self._save()
                return
            self.intent.update(state='suspending')
            self._save()
            receipt = deepcopy(self.intent['receipt'])
            self._job('suspend', lambda record: self.power.suspend(receipt, self._cancelled,
                validate=self._validate_alarm, margin_seconds=self.config.wake.suspend_margin_seconds, record=record))
        elif state in {'suspend_requested', 'suspend_unknown'} and self._future is None:
            if self.power.resumed_since(self.intent.get('receipt') or {}):
                if not resumed_now:
                    self.resume_generation += 1
                self.intent.update(state='recovering', resume_observed=True, reason='suspend_resume_observed')
                self._start_cleanup()
                self._save()
        elif state == 'night' and self.intent.get('wake_at') is not None and now >= self.intent['wake_at']:
            self.intent.update(state='morning_preparation')
            self._save()
        elif state == 'morning_preparation':
            value = self.store.get(self.intent['instance_id']) if self.intent.get('instance_id') else None
            if value and value['state'] in {'completed', 'cancelled', 'paused'}:
                self.intent.update(state='resumed', reason='morning_alarm_settled')
                self._save()
        self._restore_indicators()
        self._check_indicator_cleanup()

    def close(self):
        if self._cleanup_complete:
            return
        self._closed = True
        self._cancelled.set()
        self._indicator_cancelled.set()
        deadline = time.monotonic()+6
        try:
            if self._future is not None:
                self.jobs.drain(max(0, deadline-time.monotonic()))
                self._consume_power()
            elif self.intent and self.intent['state'] not in {'cancelled', 'resumed', 'failed', 'morning_preparation', 'night', 'awaiting_alarm'}:
                self._start_cleanup(closing=True)
        finally:
            self._check_indicator_cleanup(closing=True)
            self.jobs.drain(max(0, deadline-time.monotonic()))
            if self._future is not None and self._future.done():
                self._consume_power()
            self._consume_indicators()
        if (not self.jobs.is_idle or self._future is not None or self._indicator_job is not None
                or self.intent and self.intent.get('indicator_cleanup_pending')):
            raise RuntimeError('Home bedtime cleanup remains pending; inspect the saved bedtime intent')
        if self.intent is not None:
            self._save()
        self._cleanup_complete = True

"""Room greetings and one daily farewell, with local speech receipts."""
from concurrent.futures import Future
from datetime import datetime
import json
import logging
import time
from zoneinfo import ZoneInfo

from spica.core.proactive import ProactiveTurnRequest, PreparedProactiveTurnRequest
from spica.ports.memory import MemoryScope
from spica.ports.conversation import MaterialHint, BusinessEventBinding
from uuid import uuid4

logger = logging.getLogger(__name__)


def record_farewell_outcome(record_evidence, owner_id, value):
    """Submit Home's unchanged receipt identity to the shared core writer."""
    return record_evidence(
        MemoryScope(value['character_id'], owner_id, 'default'),
        event_id=f"{value['id']}:{value['attempt']}:{value['state']}",
        kind='tool', source='home.farewell', modality='tool',
        content=json.dumps({key: value.get(key) for key in
            ('id', 'attempt', 'channel', 'state', 'trigger_at', 'actual_started_at', 'message_id')}, ensure_ascii=False),
        metadata={'actor': 'business', 'turn_id': value['id']+':'+value['attempt']})


class HomeGreetings:
    def __init__(self, store, *, prepare, propose, cancel, local_state, allowed, character,
                 record_outcome=None, clock=time.monotonic, wall_clock=time.time):
        self.store, self.prepare, self.propose, self.cancel_speech = store, prepare, propose, cancel
        self.local_state, self.allowed, self.character = local_state, allowed, character
        self.clock, self.wall_clock = clock, wall_clock
        self.record_outcome, self._evidence_retry_at = record_outcome, 0.
        self._evidence_jobs = {}
        self._day = None
        self._claim = None
        self._draft = self._local = self._welcome = None
        self._welcome_status = None
        self._retry_at = 0.
        self._closed = False

    def _window(self, at=None):
        value = datetime.fromtimestamp(self.wall_clock() if at is None else at, ZoneInfo('Asia/Shanghai'))
        minute = value.hour*60+value.minute
        return value.date().isoformat(), value.weekday() < 5 and 515 <= minute < 540, value.replace(hour=9, minute=0, second=0, microsecond=0).timestamp()

    def _daily(self):
        return not self._closed and self.allowed()

    def _refresh_day(self):
        day = self._window()[0]
        if self._day != day:
            self._day, self._claim = day, self.store.farewell_for_day(day)

    def _save(self, **values):
        self._claim.update(values)
        self.store.save_farewell(self._claim)

    def _eligible_claim(self):
        self._refresh_day()
        return self._claim is None or self._claim['state'] in {'failed', 'expired'}

    def welcome(self):
        desktop, busy = self.local_state()
        daily = self._daily()
        self._welcome_status = dict(at=self.wall_clock(), state='pending', reason='')
        if not daily or not desktop or busy:
            self._welcome_status.update(state='skipped',
                reason='daily_suppressed' if not daily else 'desktop_inactive' if not desktop else 'busy')
            return
        self._welcome = self.propose(ProactiveTurnRequest(
            '房间连续无人至少三十分钟后，刚发生新开门，并取得开门后的新房内有人依据。按当前角色卡与允许使用的近期上下文，'
            '用一两句自然口语欢迎，保留既有关系的亲近与角色自己的反应。想念、轻轻逗弄或平常的招呼均可，不必每次邀请陪伴。'
            '称呼和亲密程度沿用当前角色与既有关系，回应这次相见，不堆砌甜话，'
            '不照搬固定模板，避免重复最近的开场。'
            '只能说回到房间，不能推断身份、刚下班或从外面回家。不需要提及传感器。',
            source='home.room_welcome', ttl_seconds=10,
            material_hint=MaterialHint('home.welcome', ''),
            event_binding=BusinessEventBinding('home.welcome', uuid4().hex, 0, 'room_presence', ('identity=unconfirmed',)),
            is_current=lambda: self._daily() and self.local_state()[0]))
        if self._welcome is None:
            self._welcome_status.update(state='failed', reason='presentation_unavailable')
        else:
            self._welcome_status['proposal_id'] = self._welcome.proposal_id

    def _welcome_receipt(self):
        result = dict(self._welcome_status) if self._welcome_status is not None else None
        handle = self._welcome
        if handle is None or result is None or result.get('proposal_id') != handle.proposal_id:
            return result
        try:
            if handle.audio_started.done() and handle.audio_started.result() is not None:
                result.update(state='started', actual_started_at=self.wall_clock()-(self.clock()-handle.audio_started.result()))
            if handle.result.done():
                outcome = handle.result.result()
                started = result.get('actual_started_at') is not None
                if (outcome.status == 'completed' and outcome.presentation_outcome == 'completed'
                        and outcome.audio_outcome is None):
                    result.update(state='delivered', reason='silent_text_completed')
                    return result
                result.update(state=('delivered' if started and getattr(outcome, 'audio_outcome', None) == 'completed'
                                     else 'stopped' if started else 'failed'),
                              reason='audio_start_unconfirmed' if not started and outcome.status == 'completed' else outcome.status)
        except Exception:
            # Displaying diagnostic receipts must never stop room automation.
            result.update(state='unknown', reason='receipt_unavailable')
        return result

    def snapshot(self):
        # Only presentation receipts, never generated text or pending work.
        return dict(welcome=self._welcome_receipt(),
            farewell={key: self._claim.get(key) for key in
                ('day', 'state', 'channel', 'reason', 'trigger_at', 'actual_started_at')} if self._claim else None)

    def departing(self, trigger_at=None):
        trigger_at = self.wall_clock() if trigger_at is None else trigger_at
        day, in_window, end = self._window(trigger_at)
        desktop, busy = self.local_state()
        if (not self._daily() or not desktop or not in_window or not self._window()[1]
                or day != self._window()[0] or not self._eligible_claim()):
            return
        if busy:
            return
        draft = self._draft
        if draft is None or not draft.ready or not draft.current(check_memory=True):
            return
        self._claim = self.store.farewell_claim(day=day, channel='desktop', trigger_at=trigger_at,
            expires_at=end, character_id=self.character())
        if self._claim is None:
            return
        self._save(text=draft.answer)
        handle = self.propose(PreparedProactiveTurnRequest(
            '日常感知刚出现离开当前活动区域的迹象，呈现已准备的本次角色告别；这不证明已经离家。', source='home.farewell',
            speech_id=draft.id, first_sound_deadline=self.clock()+end-self.wall_clock(),
            material_hint=MaterialHint('home.farewell', ''),
            event_binding=getattr(getattr(draft, 'request', None), 'event_binding', None),
            is_current=lambda: self._daily() and self.local_state()[0] and draft.current()))
        if handle is None:
            self._save(state='failed', reason='presentation_unavailable')
            return
        self._local = (handle, draft)
        handle.audio_started.add_done_callback(lambda future: setattr(draft, 'started', future.result() is not None))


    def _settle_local(self):
        if self._local is None:
            return
        handle, draft = self._local
        started = handle.audio_started.done() and handle.audio_started.result() is not None
        released = handle.result.done() and handle.released.done()
        result = handle.result.result() if released else None
        text_delivered = bool(result and result.status == 'completed'
            and result.presentation_outcome == 'completed' and result.audio_outcome is None)
        try:
            if started and self._claim['state'] == 'claimed':
                self._save(state='started', actual_started_at=self.wall_clock()-(self.clock()-handle.audio_started.result()))
            if not released:
                return
            definitely_unsent = not handle.admitted.result() or result.audio_outcome == 'not_started'
            state = 'delivered' if started or text_delivered else 'failed' if definitely_unsent else 'unknown'
            self._save(state=state, reason='silent_text_completed' if text_delivered else result.status)
        finally:
            # Persistence failure remains visible, but cannot keep files bound
            # after the real player has relinquished them.
            if released:
                try:
                    draft.settle(delivered=started or text_delivered, request_id=result.request_id,
                        audio_played=started,
                        outcome='completed' if result.presentation_outcome == 'completed' else 'stopped')
                finally:
                    self._local = self._draft = None

    def _new_draft(self):
        now = self.wall_clock()
        expiry = min(self._window()[2], now+120)
        valid = lambda: self._daily() and self.local_state()[0] and (self._window()[1]
            or self._local is not None and self._local[1].started)
        self._draft = self.prepare(
            '工作日早晨，将在感知出现离开当前活动区域的迹象时提前告别，也可能只是去洗漱或临时取物。'
            '按当前角色卡与允许使用的近期上下文，准备一句自然、简短的告别，'
            '保留既有关系的亲近，可以平常地关心或轻轻表达舍不得，不必每次撒娇或索取下次陪伴。'
            '称呼和亲密程度沿用当前角色与既有关系，选适合当下的一点表达，不堆砌甜话，'
            '不照搬固定模板，避免重复最近的开场；不能声称已经确认出门、上班或真正离家。'
            '这是待触发的候选，不代表已经说过。不输出 NO_COMMENT。',
            source='home.farewell', ttl_seconds=max(.01, expiry-now), want_audio=True, is_current=valid,
            material_hint=MaterialHint('home.farewell', ''),
            event_binding=BusinessEventBinding('home.farewell', uuid4().hex, 0,
                'prepared', ('departure=unconfirmed',)))
        self._retry_at = self.clock()+5

    def step(self):
        self._settle_local()
        self._sync_outcomes()
        self._refresh_day()
        if self._welcome is not None and self._welcome.result.done():
            self._welcome_status = self._welcome_receipt()
            self._welcome = None
        if not self._daily() or not self.local_state()[0]:
            self._withdraw()
            return
        if self._local is not None:
            if not self._local[1].current():
                self.cancel_speech(self._local[0].proposal_id)
            return
        if not self._window()[1] or not self._eligible_claim():
            self._discard_draft()
            return
        if self._draft is not None and self._draft.current() and not self._draft.error:
            return
        if not self.local_state()[1] and self.clock() >= self._retry_at:
            self._new_draft()

    def _discard_draft(self):
        if self._draft is not None:
            self._draft.cancel()
            self._draft = None

    def _withdraw(self):
        if self._welcome is not None:
            self.cancel_speech(self._welcome.proposal_id)
        if self._local is not None:
            self.cancel_speech(self._local[0].proposal_id)
        else:
            self._discard_draft()
    def _sync_outcomes(self, *, force=False):
        if self._closed or self.record_outcome is None or not force and self.clock() < self._evidence_retry_at:
            return
        self._evidence_retry_at = self.clock()+5
        try:
            pending = self.store.pending_farewell_evidence()
        except Exception:
            logger.warning('Home farewell evidence pending', exc_info=True)
            return
        live = set()
        for value in pending:
            key = value['id'], value['attempt'], value['state']
            live.add(key)
            future = self._evidence_jobs.get(key)
            try:
                if future is None:
                    future = self.record_outcome(value)
                    if future is None:  # A synchronous sink has already acknowledged.
                        future = Future()
                        future.set_result(None)
                    self._evidence_jobs[key] = future
                if not future.done():
                    continue
                future.result()
            except Exception:
                self._evidence_jobs.pop(key, None)
                logger.warning('Home farewell evidence pending', exc_info=True)
                continue
            try:
                if self.store.confirm_farewell_evidence(*key):
                    if self._claim and (self._claim['id'], self._claim['attempt'], self._claim['state']) == key:
                        self._claim['evidence_state'] = value['state']
                self._evidence_jobs.pop(key, None)
            except Exception:
                # Keep the completed receipt: a busy Home DB must not resubmit
                # memory writes or block cancellation/resource release.
                logger.warning('Home farewell evidence acknowledgement pending', exc_info=True)
        self._evidence_jobs = {key: job for key, job in self._evidence_jobs.items() if key in live}

    def close(self):
        self._closed = True
        try:
            self._withdraw()
            self._settle_local()
        finally:
            if self._local is not None:
                handle, draft = self._local
                try:
                    if self._claim['state'] == 'claimed':
                        self._save(state='unknown', reason='closing_before_playback_settled')
                finally:
                    # Register even when persistence failed; never release a
                    # player's files before its real release acknowledgement.
                    handle.released.add_done_callback(lambda _: draft.settle(
                        delivered=handle.audio_started.done() and handle.audio_started.result() is not None,
                        outcome='stopped'))
                    self._local = self._draft = None

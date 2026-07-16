from __future__ import annotations

import gc
import threading
import time
import weakref

import pytest

from spica.core.conversation_coordinator import ConversationCoordinator
from spica.core.events import DoneEvent, StatusEvent
from spica.ports.conversation import (
    AdmissionDecision,
    ConversationCancellation,
    ConversationRequest,
    ConversationTurnKind,
    PresentationTerminalOutcome,
)


class _NoopExecutor:
    def stream(self, request, *, cancelled):
        del request, cancelled
        return iter(())


class _LiteralEventExecutor:
    def __init__(self):
        self.calls = 0

    def stream(self, request, *, cancelled):
        del request, cancelled
        self.calls += 1
        yield StatusEvent(state="thinking", message="")
        yield DoneEvent(
            answer="答复",
            emotion="happy",
            emotion_label="喜",
            emotion_reason="回应用户",
            units_count=1,
            timing={"total_ms": 12},
        )


class _DoneBeforeTerminalExecutor:
    def __init__(self):
        self.release_terminal = threading.Event()

    def stream(self, request, *, cancelled):
        del request, cancelled
        yield DoneEvent(
            answer="已生成但 producer 尚未退出",
            emotion="neutral",
            emotion_label="中性",
            emotion_reason="测试",
            units_count=0,
            timing={},
        )
        assert self.release_terminal.wait(2.0)


class _CancelAwareExecutor:
    def __init__(self):
        self.started = threading.Event()
        self.observed_cancel = threading.Event()
        self.cancel_token = None

    def stream(self, request, *, cancelled):
        del request
        self.cancel_token = cancelled
        self.started.set()
        assert cancelled.wait(2.0)
        self.observed_cancel.set()
        if False:
            yield StatusEvent(state="unreachable", message="")


class _NoSideEffectExecutor:
    def __init__(self):
        self.calls = 0

    def stream(self, request, *, cancelled):
        del request, cancelled
        self.calls += 1
        if False:
            yield StatusEvent(state="unreachable", message="")


class _BeforeLinearizationCoordinator(ConversationCoordinator):
    def __init__(self, executor):
        super().__init__(executor)
        self.before_linearization = threading.Event()
        self.release_linearization = threading.Event()

    def _linearize_admission(self, cancellation, publish):
        self.before_linearization.set()
        assert self.release_linearization.wait(2.0)
        return super()._linearize_admission(cancellation, publish)


class _AfterLinearizationCoordinator(ConversationCoordinator):
    def __init__(self, executor):
        super().__init__(executor)
        self.after_linearization = threading.Event()
        self.release_submit = threading.Event()

    def _linearize_admission(self, cancellation, publish):
        admitted = super()._linearize_admission(cancellation, publish)
        self.after_linearization.set()
        assert self.release_submit.wait(2.0)
        return admitted


class _SharedPayload:
    def __deepcopy__(self, memo):
        del memo
        return self


def _wait_until(predicate, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


def test_only_one_foreground_turn_is_admitted_at_a_time():
    coordinator = ConversationCoordinator(_NoopExecutor())

    first = coordinator.submit(
        ConversationRequest(
            request_id="request-1",
            requested_conversation_id="default",
            kind=ConversationTurnKind.USER,
            content="第一条消息",
        )
    )
    second = coordinator.submit(
        ConversationRequest(
            request_id="request-2",
            requested_conversation_id="default",
            kind=ConversationTurnKind.SYSTEM,
            content="一条主动开口指令",
        )
    )

    assert first.decision is AdmissionDecision.ACCEPTED
    assert first.accepted_turn is not None
    assert second.decision is AdmissionDecision.BUSY
    assert second.accepted_turn is None
    assert coordinator.is_busy()


def test_same_request_and_payload_replays_the_original_admission():
    coordinator = ConversationCoordinator(_NoopExecutor())
    request = ConversationRequest(
        request_id="request-1",
        requested_conversation_id="default",
        kind=ConversationTurnKind.USER,
        content="同一条消息",
        screen_attachment={"image_bytes": b"png", "region": {"x": 10, "y": 20}},
    )

    original = coordinator.submit(request)
    replay = coordinator.submit(request)

    assert replay is original
    assert replay.decision is AdmissionDecision.ACCEPTED
    assert replay.turn_id == original.turn_id
    assert replay.accepted_turn is original.accepted_turn


def test_same_request_id_with_different_payload_is_an_idempotency_conflict():
    coordinator = ConversationCoordinator(_NoopExecutor())
    original = ConversationRequest(
        request_id="request-1",
        requested_conversation_id="default",
        kind=ConversationTurnKind.USER,
        content="原消息",
    )
    changed = ConversationRequest(
        request_id="request-1",
        requested_conversation_id="default",
        kind=ConversationTurnKind.USER,
        content="被替换的消息",
    )

    accepted = coordinator.submit(original)
    conflict = coordinator.submit(changed)

    assert accepted.decision is AdmissionDecision.ACCEPTED
    assert conflict.decision is AdmissionDecision.IDEMPOTENCY_CONFLICT
    assert conflict.turn_id == accepted.turn_id
    assert conflict.accepted_turn is None


def test_accepted_turn_wraps_runtime_events_with_stable_identity_and_sequence():
    executor = _LiteralEventExecutor()
    coordinator = ConversationCoordinator(executor)
    request = ConversationRequest(
        request_id="request-events",
        requested_conversation_id="desktop::default",
        kind=ConversationTurnKind.USER,
        content="你好",
    )
    admission = coordinator.submit(request)

    envelopes = list(admission.accepted_turn.events())
    replay = coordinator.submit(request)

    assert executor.calls == 1
    assert replay.accepted_turn is admission.accepted_turn
    with pytest.raises(RuntimeError, match="only be consumed once"):
        list(replay.accepted_turn.events())
    assert executor.calls == 1
    assert [envelope.requested_conversation_id for envelope in envelopes] == [
        "desktop::default",
        "desktop::default",
    ]
    assert [envelope.turn_id for envelope in envelopes] == [
        admission.turn_id,
        admission.turn_id,
    ]
    assert [envelope.sequence for envelope in envelopes] == [1, 2]
    assert [envelope.event.kind for envelope in envelopes] == ["status", "done"]


def test_active_slot_waits_for_real_producer_and_presentation_terminal_axes():
    executor = _DoneBeforeTerminalExecutor()
    coordinator = ConversationCoordinator(executor)
    admission = coordinator.submit(
        ConversationRequest(
            request_id="request-terminal",
            requested_conversation_id="default",
            kind=ConversationTurnKind.USER,
            content="测试 terminal",
        )
    )
    events = admission.accepted_turn.events()

    assert next(events).event.kind == "done"
    assert coordinator.report_presentation_terminal(
        admission.turn_id,
        PresentationTerminalOutcome.COMPLETED,
    )
    assert coordinator.is_busy()

    executor.release_terminal.set()
    assert list(events) == []
    assert _wait_until(lambda: not coordinator.is_busy())


def test_cancel_sets_the_same_token_read_by_the_executor():
    executor = _CancelAwareExecutor()
    coordinator = ConversationCoordinator(executor)
    admission = coordinator.submit(
        ConversationRequest(
            request_id="request-cancel",
            requested_conversation_id="default",
            kind=ConversationTurnKind.USER,
            content="停止这一轮",
        )
    )

    assert executor.started.wait(2.0)
    assert admission.accepted_turn.cancel("user_stop")
    assert admission.accepted_turn.cancel("duplicate_stop")
    assert executor.observed_cancel.wait(2.0)
    assert executor.cancel_token is not None
    assert executor.cancel_token.is_set()
    assert coordinator.is_busy()

    assert coordinator.report_presentation_terminal(
        admission.turn_id,
        PresentationTerminalOutcome.STOPPED,
    )
    assert list(admission.accepted_turn.events()) == []
    assert _wait_until(lambda: not coordinator.is_busy())


def test_cancel_wins_before_coordinator_linearization_without_starting_producer(
    monkeypatch,
):
    executor = _NoSideEffectExecutor()
    coordinator = _BeforeLinearizationCoordinator(executor)
    cancellation = ConversationCancellation()
    request = ConversationRequest(
        request_id="request-cancel-before-linearization",
        requested_conversation_id="default",
        kind=ConversationTurnKind.USER,
        content="在线性化前停止",
    )
    real_thread = threading.Thread
    real_start = real_thread.start
    producer_constructions = []

    def track_producer_construction(*args, **kwargs):
        name = str(kwargs.get("name") or "")
        if name.startswith("conversation-turn-"):
            producer_constructions.append(name)
        return real_thread(*args, **kwargs)

    admissions = []
    errors = []

    def submit_request():
        try:
            admissions.append(coordinator.submit(request, cancellation=cancellation))
        except Exception as exc:
            errors.append(exc)

    submitter = real_thread(target=submit_request, name="cancel-first-submit")
    monkeypatch.setattr(threading, "Thread", track_producer_construction)
    real_start(submitter)
    assert coordinator.before_linearization.wait(2.0)

    assert cancellation.cancel("desktop_stop")
    coordinator.release_linearization.set()
    submitter.join(2.0)

    assert not submitter.is_alive()
    assert errors == []
    assert len(admissions) == 1
    assert admissions[0].decision is AdmissionDecision.CANCELLED
    assert admissions[0].accepted_turn is None
    assert producer_constructions == []
    assert executor.calls == 0
    assert not coordinator.is_busy()


def test_admission_wins_then_same_token_immediately_cancels_the_accepted_turn():
    executor = _CancelAwareExecutor()
    coordinator = _AfterLinearizationCoordinator(executor)
    cancellation = ConversationCancellation()
    request = ConversationRequest(
        request_id="request-cancel-after-linearization",
        requested_conversation_id="default",
        kind=ConversationTurnKind.USER,
        content="在线性化后停止",
    )
    admissions = []
    errors = []

    def submit_request():
        try:
            admissions.append(coordinator.submit(request, cancellation=cancellation))
        except Exception as exc:
            errors.append(exc)

    submitter = threading.Thread(target=submit_request, name="admission-first-submit")
    submitter.start()
    assert coordinator.after_linearization.wait(2.0)

    assert cancellation.cancel("desktop_stop")
    assert cancellation.event.is_set()
    assert not executor.started.is_set(), (
        "producer started before the post-linearization test barrier was released"
    )
    coordinator.release_submit.set()
    assert executor.started.wait(2.0)
    assert executor.observed_cancel.wait(2.0)
    assert executor.cancel_token is cancellation.event
    submitter.join(2.0)

    assert not submitter.is_alive()
    assert errors == []
    assert len(admissions) == 1
    admission = admissions[0]
    assert admission.decision is AdmissionDecision.ACCEPTED
    assert admission.accepted_turn is not None
    assert coordinator.report_presentation_terminal(
        admission.turn_id,
        PresentationTerminalOutcome.STOPPED,
    )
    assert list(admission.accepted_turn.events()) == []
    assert _wait_until(lambda: not coordinator.is_busy())


def test_producer_thread_start_failure_rolls_back_admission_for_safe_retry(monkeypatch):
    coordinator = ConversationCoordinator(_NoopExecutor())
    cancellation = ConversationCancellation()
    request = ConversationRequest(
        request_id="request-thread-start",
        requested_conversation_id="default",
        kind=ConversationTurnKind.USER,
        content="线程启动失败后重试",
    )
    real_start = threading.Thread.start

    def fail_start(_thread):
        raise RuntimeError("thread unavailable")

    monkeypatch.setattr(threading.Thread, "start", fail_start)
    with pytest.raises(RuntimeError, match="thread unavailable"):
        coordinator.submit(request, cancellation=cancellation)

    assert not coordinator.is_busy()

    monkeypatch.setattr(threading.Thread, "start", real_start)
    retry = coordinator.submit(request, cancellation=cancellation)
    assert retry.decision is AdmissionDecision.ACCEPTED
    assert list(retry.accepted_turn.events()) == []
    assert coordinator.report_presentation_terminal(
        retry.turn_id,
        PresentationTerminalOutcome.COMPLETED,
    )
    assert _wait_until(lambda: not coordinator.is_busy())


def test_producer_thread_construction_failure_rolls_back_for_safe_retry(monkeypatch):
    coordinator = ConversationCoordinator(_NoopExecutor())
    cancellation = ConversationCancellation()
    request = ConversationRequest(
        request_id="request-thread-construction",
        requested_conversation_id="default",
        kind=ConversationTurnKind.USER,
        content="线程构造失败后重试",
    )
    real_thread = threading.Thread

    def fail_producer_construction(*args, **kwargs):
        if str(kwargs.get("name") or "").startswith("conversation-turn-"):
            raise RuntimeError("thread construction unavailable")
        return real_thread(*args, **kwargs)

    monkeypatch.setattr(threading, "Thread", fail_producer_construction)
    with pytest.raises(RuntimeError, match="thread construction unavailable"):
        coordinator.submit(request, cancellation=cancellation)

    assert not coordinator.is_busy()

    monkeypatch.setattr(threading, "Thread", real_thread)
    retry = coordinator.submit(request, cancellation=cancellation)
    assert retry.decision is AdmissionDecision.ACCEPTED
    assert list(retry.accepted_turn.events()) == []
    assert coordinator.report_presentation_terminal(
        retry.turn_id,
        PresentationTerminalOutcome.COMPLETED,
    )
    assert _wait_until(lambda: not coordinator.is_busy())


def test_cancel_during_blocked_start_survives_start_failure_without_orphan_ledger(
    monkeypatch,
):
    executor = _NoSideEffectExecutor()
    coordinator = ConversationCoordinator(executor)
    cancellation = ConversationCancellation()
    request = ConversationRequest(
        request_id="request-cancel-during-failed-start",
        requested_conversation_id="default",
        kind=ConversationTurnKind.USER,
        content="启动失败窗口内停止",
    )
    real_start = threading.Thread.start
    producer_start_entered = threading.Event()
    release_failed_start = threading.Event()

    def blocked_failed_start(thread):
        if thread.name.startswith("conversation-turn-"):
            producer_start_entered.set()
            assert release_failed_start.wait(2.0)
            raise RuntimeError("thread unavailable")
        return real_start(thread)

    monkeypatch.setattr(threading.Thread, "start", blocked_failed_start)
    errors = []

    def submit_request():
        try:
            coordinator.submit(request, cancellation=cancellation)
        except Exception as exc:
            errors.append(exc)

    submitter = threading.Thread(target=submit_request, name="failed-start-submit")
    real_start(submitter)
    assert producer_start_entered.wait(2.0)
    watchdog = threading.Timer(1.0, release_failed_start.set)
    watchdog.start()
    try:
        assert cancellation.cancel("desktop_stop")
        assert not release_failed_start.is_set(), (
            "cancel waited for the blocked producer.start call"
        )
    finally:
        release_failed_start.set()
        watchdog.cancel()
    submitter.join(2.0)

    assert not submitter.is_alive()
    assert len(errors) == 1
    assert isinstance(errors[0], RuntimeError)
    assert not coordinator.is_busy()
    assert executor.calls == 0

    monkeypatch.setattr(threading.Thread, "start", real_start)
    retry = coordinator.submit(request, cancellation=cancellation)
    assert retry.decision is AdmissionDecision.CANCELLED
    assert retry.accepted_turn is None
    assert not coordinator.is_busy()
    assert executor.calls == 0


def test_thread_start_failure_cannot_publish_an_orphan_concurrent_replay(monkeypatch):
    coordinator = ConversationCoordinator(_NoopExecutor())
    request = ConversationRequest(
        request_id="request-concurrent-thread-start",
        requested_conversation_id="default",
        kind=ConversationTurnKind.USER,
        content="并发线程启动失败",
    )
    real_start = threading.Thread.start
    producer_start_entered = threading.Event()
    release_failed_start = threading.Event()
    start_calls = 0
    start_calls_lock = threading.Lock()

    def controlled_start(thread):
        nonlocal start_calls
        if thread.name.startswith("conversation-turn-"):
            with start_calls_lock:
                start_calls += 1
                call_number = start_calls
            if call_number == 1:
                producer_start_entered.set()
                assert release_failed_start.wait(2.0)
                raise RuntimeError("thread unavailable")
        return real_start(thread)

    monkeypatch.setattr(threading.Thread, "start", controlled_start)
    original_errors = []
    replay_admissions = []
    replay_done = threading.Event()

    def submit_original():
        try:
            coordinator.submit(request)
        except Exception as exc:
            original_errors.append(exc)

    def submit_replay():
        replay_admissions.append(coordinator.submit(request))
        replay_done.set()

    original_thread = threading.Thread(target=submit_original, name="original-submit")
    replay_thread = threading.Thread(target=submit_replay, name="replay-submit")
    real_start(original_thread)
    assert producer_start_entered.wait(2.0)
    real_start(replay_thread)
    try:
        assert not replay_done.wait(0.05)
    finally:
        release_failed_start.set()
        original_thread.join(2.0)
        replay_thread.join(2.0)

    assert len(original_errors) == 1
    assert isinstance(original_errors[0], RuntimeError)
    assert replay_done.is_set()
    assert len(replay_admissions) == 1
    replay = replay_admissions[0]
    assert replay.decision is AdmissionDecision.ACCEPTED
    assert list(replay.accepted_turn.events()) == []
    assert coordinator.report_presentation_terminal(
        replay.turn_id,
        PresentationTerminalOutcome.COMPLETED,
    )
    assert _wait_until(lambda: not coordinator.is_busy())


def test_terminal_turn_releases_heavy_request_payload_while_retaining_idempotency():
    coordinator = ConversationCoordinator(_NoopExecutor())
    payload = _SharedPayload()
    payload_ref = weakref.ref(payload)
    request = ConversationRequest(
        request_id="request-heavy-payload",
        requested_conversation_id="default",
        kind=ConversationTurnKind.USER,
        content="分析截图",
        screen_attachment={"image_bytes": payload},
    )

    admission = coordinator.submit(request)
    assert list(admission.accepted_turn.events()) == []
    assert coordinator.report_presentation_terminal(
        admission.turn_id,
        PresentationTerminalOutcome.COMPLETED,
    )
    assert _wait_until(lambda: not coordinator.is_busy())
    replay = coordinator.submit(request)
    assert replay is admission

    del replay
    del request
    del payload
    gc.collect()

    assert payload_ref() is None

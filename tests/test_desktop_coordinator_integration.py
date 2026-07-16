from __future__ import annotations

import copy
import os
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from spica.adapters.conversation.legacy_run_turn import LegacyRunTurnAdapter
from spica.core.chat_engine import ChatEngine
from spica.core.conversation_coordinator import ConversationCoordinator
from spica.config.schema import AppConfig
from spica.host.app_host import AppHost
from spica.core.events import (
    DoneEvent,
    StatusEvent,
    UnitAudioReadyEvent,
    UnitTextReadyEvent,
    UnitVisualReadyEvent,
)
from spica.ports.conversation import (
    AdmissionDecision,
    ConversationCancellation,
    ConversationRequest,
    ConversationTurnKind,
    PresentationTerminalOutcome,
)


USER_RUNTIME_EVENTS = (
    StatusEvent(state="thinking", message=""),
    DoneEvent(
        answer="用户答复",
        emotion="happy",
        emotion_label="喜",
        emotion_reason="正常回应",
        units_count=1,
        timing={"total_ms": 10},
    ),
)

SYSTEM_LEGACY_EVENTS = (
    {"event": "status", "data": {"state": "thinking", "message": ""}},
    {
        "event": "done",
        "data": {
            "answer": "主动答复",
            "emotion": "neutral",
            "emotion_label": "中性",
            "emotion_reason": "主动开口",
            "units_count": 0,
            "timing": {"total_ms": 8},
        },
    },
)

DESKTOP_LEGACY_GOLDEN = (
    {"event": "status", "data": {"state": "thinking", "message": ""}},
    {
        "event": "unit_text_ready",
        "data": {
            "index": 0,
            "display_text": "こんにちは",
            "tts_text": "こんにちは",
            "emotion": "happy",
            "timing": {"text_ms": 1},
        },
    },
    {
        "event": "unit_visual_ready",
        "data": {
            "index": 0,
            "visual": {"dialog": {"speaker": "spica"}},
            "cue": {"image_path": "summer/happy.png"},
            "visual_error": None,
            "timing": {"visual_ms": 2},
        },
    },
    {
        "event": "unit_audio_ready",
        "data": {
            "index": 0,
            "audio_url": None,
            "audio_path": "/tmp/unit-0.wav",
            "audio_error": None,
            "timing": {"tts_ms": 3},
        },
    },
    {
        "event": "done",
        "data": {
            "answer": "こんにちは",
            "emotion": "happy",
            "emotion_label": "喜",
            "emotion_reason": "挨拶",
            "units_count": 1,
            "timing": {"total_ms": 10},
        },
    },
)

DESKTOP_RUNTIME_GOLDEN = (
    StatusEvent(state="thinking", message=""),
    UnitTextReadyEvent(
        index=0,
        display_text="こんにちは",
        tts_text="こんにちは",
        emotion="happy",
        timing={"text_ms": 1},
    ),
    UnitVisualReadyEvent(
        index=0,
        visual={"dialog": {"speaker": "spica"}},
        cue={"image_path": "summer/happy.png"},
        visual_error=None,
        timing={"visual_ms": 2},
    ),
    UnitAudioReadyEvent(
        index=0,
        audio_url=None,
        audio_path="/tmp/unit-0.wav",
        audio_error=None,
        timing={"tts_ms": 3},
    ),
    DoneEvent(
        answer="こんにちは",
        emotion="happy",
        emotion_label="喜",
        emotion_reason="挨拶",
        units_count=1,
        timing={"total_ms": 10},
    ),
)


class _RecordingChatEngine:
    def __init__(self):
        self.user_calls = []

    def stream_voice_runtime(self, content, **kwargs):
        self.user_calls.append((content, kwargs))
        yield from USER_RUNTIME_EVENTS

    def stream_system_turn(self, directive, **kwargs):
        del directive, kwargs
        raise AssertionError("user request must not enter stream_system_turn")


class _SystemRecordingChatEngine:
    def __init__(self):
        self.system_calls = []

    def stream_voice_runtime(self, content, **kwargs):
        del content, kwargs
        raise AssertionError("system request must not enter stream_voice_runtime directly")

    def stream_system_turn(self, directive, **kwargs):
        self.system_calls.append((directive, kwargs))
        yield from SYSTEM_LEGACY_EVENTS


class _PublicSystemTurnHarness:
    stream_system_turn = ChatEngine.stream_system_turn

    def __init__(self):
        self.stream_voice_calls = []

    def stream_voice(self, content, **kwargs):
        self.stream_voice_calls.append((content, kwargs))
        yield from SYSTEM_LEGACY_EVENTS


class _HostChatEngine:
    def __init__(self, services, config):
        self.services = services
        self.config = config
        self.game_binding_provider = None
        self.user_calls = []

    def set_game_binding_provider(self, provider):
        self.game_binding_provider = provider

    def stream_voice_runtime(self, content, **kwargs):
        self.user_calls.append((content, kwargs))
        yield DoneEvent(
            answer="host reply",
            emotion="neutral",
            emotion_label="中性",
            emotion_reason="host seam",
            units_count=0,
            timing={},
        )


class _LegacyDesktopAgent:
    def __init__(self):
        self.calls = []

    def stream_voice(self, content, **kwargs):
        self.calls.append((content, kwargs))
        yield from copy.deepcopy(DESKTOP_LEGACY_GOLDEN)


class _ForbiddenLegacyAgent:
    def stream_voice(self, content, **kwargs):
        del content, kwargs
        raise AssertionError("flag=true must not use the legacy ChatWorker path")


class _BarrierDesktopExecutor:
    def __init__(self):
        self.calls = 0
        self.first_event_produced = threading.Event()
        self.release_tail = threading.Event()

    def stream(self, request, *, cancelled):
        del request, cancelled
        self.calls += 1
        yield DESKTOP_RUNTIME_GOLDEN[0]
        self.first_event_produced.set()
        assert self.release_tail.wait(2.0)
        yield from DESKTOP_RUNTIME_GOLDEN[1:]


class _CancelDrainExecutor:
    def __init__(self):
        self.started = threading.Event()
        self.release_first = threading.Event()
        self.cancel_observed = threading.Event()
        self.producer_terminal = threading.Event()

    def stream(self, request, *, cancelled):
        del request
        self.started.set()
        assert self.release_first.wait(2.0)
        yield StatusEvent(state="thinking", message="")
        assert cancelled.wait(2.0)
        self.cancel_observed.set()
        yield UnitTextReadyEvent(
            index=0,
            display_text="ghost",
            tts_text="ghost",
            emotion="neutral",
            timing={},
        )
        yield DoneEvent(
            answer="ghost",
            emotion="neutral",
            emotion_label="中性",
            emotion_reason="cancelled",
            units_count=1,
            timing={},
        )
        self.producer_terminal.set()


class _DelayedSubmitCoordinator:
    def __init__(self, coordinator):
        self.coordinator = coordinator
        self.submit_started = threading.Event()
        self.release_submit = threading.Event()
        self.submit_calls = 0
        self.last_admission = None

    def submit(self, request, *, cancellation=None):
        self.submit_calls += 1
        self.submit_started.set()
        assert self.release_submit.wait(2.0)
        self.last_admission = self.coordinator.submit(
            request,
            cancellation=cancellation,
        )
        return self.last_admission

    def is_busy(self):
        return self.coordinator.is_busy()

    def report_presentation_terminal(self, turn_id, outcome):
        return self.coordinator.report_presentation_terminal(turn_id, outcome)


class _PreAdmissionProbeCoordinator:
    def __init__(self, coordinator):
        self.coordinator = coordinator
        self.busy_probe_started = threading.Event()
        self.release_busy_probe = threading.Event()
        self.submit_calls = 0

    def submit(self, request, *, cancellation=None):
        self.submit_calls += 1
        return self.coordinator.submit(request, cancellation=cancellation)

    def is_busy(self):
        self.busy_probe_started.set()
        assert self.release_busy_probe.wait(2.0)
        return self.coordinator.is_busy()

    def report_presentation_terminal(self, turn_id, outcome):
        return self.coordinator.report_presentation_terminal(turn_id, outcome)


class _InternalAdmissionBarrierCoordinator(ConversationCoordinator):
    def __init__(self, executor):
        super().__init__(executor)
        self.before_linearization = threading.Event()
        self.release_linearization = threading.Event()

    def _linearize_admission(self, cancellation, publish):
        self.before_linearization.set()
        assert self.release_linearization.wait(2.0)
        return super()._linearize_admission(cancellation, publish)


class _CountingCoordinator:
    def __init__(self, coordinator):
        self.coordinator = coordinator
        self.busy_checks = 0

    def submit(self, request, *, cancellation=None):
        return self.coordinator.submit(request, cancellation=cancellation)

    def is_busy(self):
        self.busy_checks += 1
        return self.coordinator.is_busy()

    def report_presentation_terminal(self, turn_id, outcome):
        return self.coordinator.report_presentation_terminal(turn_id, outcome)


class _VoiceRearmLifecycleExecutor:
    def __init__(self):
        self.calls = 0
        self.second_started = threading.Event()
        self.second_cancel_observed = threading.Event()
        self.release_second_terminal = threading.Event()

    def stream(self, request, *, cancelled):
        del request
        self.calls += 1
        if self.calls == 1:
            yield DoneEvent(
                answer="",
                emotion="neutral",
                emotion_label="中性",
                emotion_reason="first turn completed",
                units_count=0,
                timing={},
            )
            return
        self.second_started.set()
        assert cancelled.wait(2.0)
        self.second_cancel_observed.set()
        assert self.release_second_terminal.wait(2.0)
        if False:
            yield StatusEvent(state="unreachable", message="")


class _PreemptThenCompleteExecutor:
    def __init__(self):
        self.contents = []
        self.first_started = threading.Event()
        self.first_cancel_observed = threading.Event()
        self.release_first_terminal = threading.Event()
        self.second_started = threading.Event()

    def stream(self, request, *, cancelled):
        self.contents.append(request.content)
        if request.content == "旧消息":
            self.first_started.set()
            assert cancelled.wait(2.0)
            self.first_cancel_observed.set()
            assert self.release_first_terminal.wait(2.0)
            return
        self.second_started.set()
        yield DoneEvent(
            answer="",
            emotion="neutral",
            emotion_label="中性",
            emotion_reason="replacement",
            units_count=0,
            timing={},
        )


class _BlockingExecutor:
    def __init__(self):
        self.started = threading.Event()
        self.release = threading.Event()
        self.contents = []
        self.cancel_tokens = []

    def stream(self, request, *, cancelled):
        self.contents.append(request.content)
        self.cancel_tokens.append(cancelled)
        self.started.set()
        assert self.release.wait(2.0)
        if False:
            yield StatusEvent(state="unreachable", message="")


class _UncooperativeExecutor:
    def __init__(self):
        self.started = threading.Event()
        self.release = threading.Event()
        self.calls = 0

    def stream(self, request, *, cancelled):
        del request, cancelled
        self.calls += 1
        self.started.set()
        assert self.release.wait(5.0)
        if False:
            yield StatusEvent(state="unreachable", message="")


class _EmptyDoneExecutor:
    def stream(self, request, *, cancelled):
        del request, cancelled
        yield DoneEvent(
            answer="",
            emotion="neutral",
            emotion_label="中性",
            emotion_reason="silent",
            units_count=0,
            timing={},
        )


class _CapturingRequestExecutor:
    def __init__(self):
        self.request = None
        self.received = threading.Event()

    def stream(self, request, *, cancelled):
        del cancelled
        self.request = request
        self.received.set()
        yield DoneEvent(
            answer="",
            emotion="neutral",
            emotion_label="中性",
            emotion_reason="silent",
            units_count=0,
            timing={},
        )


class _NullAudioController:
    def release_chat_audio(self):
        return None

    def release_preloaded(self):
        return None


class _NullTypewriterController:
    def start(self, text, on_finished=None, interval_ms=None):
        del text, on_finished, interval_ms

    def stop(self):
        return None


@pytest.fixture(scope="module")
def qapp():
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


def _spin_qt_until(qapp, predicate, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        qapp.processEvents()
        if predicate():
            return True
        time.sleep(0.005)
    qapp.processEvents()
    return predicate()


def test_legacy_adapter_routes_user_turn_through_typed_runtime_entry():
    engine = _RecordingChatEngine()
    adapter = LegacyRunTurnAdapter(engine)
    cancelled = threading.Event()
    attachment = {"image_bytes": b"png", "target": "selected_region"}
    request = ConversationRequest(
        request_id="user-request",
        requested_conversation_id="desktop::default",
        kind=ConversationTurnKind.USER,
        content="请看这张截图",
        visual_overrides={"costume_mode": "fixed", "costume_set": "summer"},
        include_user_time_context=True,
        screen_attachment=attachment,
    )

    events = list(adapter.stream(request, cancelled=cancelled))

    assert events == list(USER_RUNTIME_EVENTS)
    assert engine.user_calls == [
        (
            "请看这张截图",
            {
                "conversation_id": "desktop::default",
                "visual_overrides": {
                    "costume_mode": "fixed",
                    "costume_set": "summer",
                },
                "include_user_time_context": True,
                "interaction_mode": "chat",
                "screen_attachment": attachment,
                "cancelled": cancelled,
            },
        )
    ]


def test_legacy_adapter_routes_raw_system_directive_and_restores_typed_events():
    engine = _SystemRecordingChatEngine()
    adapter = LegacyRunTurnAdapter(engine)
    cancelled = threading.Event()
    request = ConversationRequest(
        request_id="system-request",
        requested_conversation_id="desktop::default",
        kind=ConversationTurnKind.SYSTEM,
        content="刚刚唱完歌，请自然收尾。",
        source="song",
        visual_overrides={"costume_mode": "fixed", "costume_set": "summer"},
        include_user_time_context=False,
    )

    events = list(adapter.stream(request, cancelled=cancelled))

    assert [event.to_legacy_dict() for event in events] == list(SYSTEM_LEGACY_EVENTS)
    assert engine.system_calls == [
        (
            "刚刚唱完歌，请自然收尾。",
            {
                "conversation_id": "desktop::default",
                "source": "song",
                "cancelled": cancelled,
                "visual_overrides": {
                    "costume_mode": "fixed",
                    "costume_set": "summer",
                },
                "include_user_time_context": False,
            },
        )
    ]


def test_stream_system_turn_forwards_desktop_visual_and_time_context_options():
    harness = _PublicSystemTurnHarness()
    cancelled = threading.Event()

    events = list(
        harness.stream_system_turn(
            "刚刚唱完歌。",
            conversation_id="desktop::default",
            source="song",
            cancelled=cancelled,
            visual_overrides={"costume_mode": "fixed", "costume_set": "summer"},
            include_user_time_context=False,
        )
    )

    assert events == list(SYSTEM_LEGACY_EVENTS)
    assert harness.stream_voice_calls == [
        (
            "【系统事件，不是麦说的话】刚刚唱完歌。\n"
            "请以 Spica 的口吻自然地主动说一句，简短、口语化、适合直接朗读。"
            "不要提到系统、事件、指令这些词。",
            {
                "conversation_id": "desktop::default",
                "interaction_mode": "system",
                "cancelled": cancelled,
                "visual_overrides": {
                    "costume_mode": "fixed",
                    "costume_set": "summer",
                },
                "include_user_time_context": False,
            },
        )
    ]


def test_desktop_coordinator_flag_defaults_off_and_can_be_enabled_typed():
    assert AppConfig().conversation.coordinator_desktop_enabled is False
    enabled = AppConfig.model_validate(
        {"conversation": {"coordinator_desktop_enabled": True}}
    )
    assert enabled.conversation.coordinator_desktop_enabled is True


def test_app_host_keeps_chat_engine_surface_and_exposes_named_coordinator_surface():
    host = AppHost()

    assert host.conversation_surface is None
    assert host.conversation_coordinator is None

    engine = object()
    coordinator = object()
    host.chat_engine = engine
    host.conversation_coordinator = coordinator

    assert host.conversation_surface is engine
    assert host.conversation_coordinator is coordinator


@pytest.mark.parametrize("desktop_enabled", [False, True])
def test_app_host_always_assembles_one_typed_conversation_coordinator(
    desktop_enabled,
):
    host = AppHost()
    config = AppConfig.model_validate(
        {"conversation": {"coordinator_desktop_enabled": desktop_enabled}}
    )
    character_package = SimpleNamespace(
        skill_dir="/tmp/spica-skill",
        visual_config_path=None,
        tts_config_path=None,
    )
    services = SimpleNamespace(
        ocr_adapter=object(),
        llm_client=object(),
        recent_memory=object(),
        memory_store=object(),
        effective_platform="linux",
    )

    with (
        patch("spica.host.app_host.ConfigManager.load", return_value=config),
        patch("spica.host.app_host.load_secrets", return_value=SimpleNamespace()),
        patch.object(host.plugin_host, "load"),
        patch("spica.host.app_host.load_character_package", return_value=character_package),
        patch.object(host.registry, "resolve_visual", return_value=object()),
        patch("spica.host.app_host.load_tts_config", return_value={}),
        patch.object(
            host,
            "_resolve_tts_assembly",
            return_value=("fake", object(), object()),
        ),
        patch("spica.host.app_host.build_agent_services", return_value=services),
        patch("spica.host.app_host._install_ocr_runtime_provider"),
        patch.object(host, "_install_moondream_seam"),
        patch.object(host.registry, "resolve_llm", return_value=object()),
        patch.object(host.registry, "resolve_memory", return_value=object()),
        patch("spica.host.app_host.anime_assembly.install"),
        patch("spica.host.app_host.ChatEngine", _HostChatEngine),
        patch("spica.host.app_host.reaction_assembly.install"),
        patch.object(host, "_new_stt_adapter", return_value=object()),
    ):
        host.initialize()

    assert host.conversation_surface is host.chat_engine
    assert isinstance(host.conversation_coordinator, ConversationCoordinator)

    admission = host.conversation_coordinator.submit(
        ConversationRequest(
            request_id=f"host-{desktop_enabled}",
            requested_conversation_id="desktop::default",
            kind=ConversationTurnKind.USER,
            content="从 Host 发起",
        )
    )
    envelopes = list(admission.accepted_turn.events())

    assert [envelope.event.to_legacy_dict() for envelope in envelopes] == [
        {
            "event": "done",
            "data": {
                "answer": "host reply",
                "emotion": "neutral",
                "emotion_label": "中性",
                "emotion_reason": "host seam",
                "units_count": 0,
                "timing": {},
            },
        }
    ]
    assert len(host.chat_engine.user_calls) == 1
    assert host.conversation_coordinator.report_presentation_terminal(
        admission.turn_id,
        PresentationTerminalOutcome.COMPLETED,
    )


def test_flag_false_chat_worker_emits_the_literal_legacy_golden_unchanged():
    pytest.importorskip("PySide6")
    from ui.workers.chat_worker import ChatWorker

    agent = _LegacyDesktopAgent()
    emitted = []
    worker = ChatWorker(
        agent,
        "你好",
        "desktop::default",
        {"costume_mode": "fixed", "costume_set": "summer"},
        True,
        "chat",
        conversation_coordinator=None,
        request_id="legacy-request",
        screen_attachment={"image_bytes": b"png"},
    )
    worker.stream_event.connect(lambda name, data: emitted.append((name, data)))

    worker.run()

    assert emitted == [
        (event["event"], event["data"])
        for event in copy.deepcopy(DESKTOP_LEGACY_GOLDEN)
    ]
    assert len(agent.calls) == 1
    assert agent.calls[0][0] == "你好"
    assert agent.calls[0][1]["conversation_id"] == "desktop::default"
    assert agent.calls[0][1]["interaction_mode"] == "chat"


@pytest.mark.parametrize("desktop_enabled", [False, True])
def test_overlay_typed_flag_only_controls_desktop_coordinator_injection(
    qapp,
    desktop_enabled,
):
    del qapp
    from ui.qt_overlay import OverlayWindow

    coordinator = object()

    with patch.object(OverlayWindow, "_init_backend", lambda self: None):
        window = OverlayWindow()
    window.host = SimpleNamespace(
        config=AppConfig.model_validate(
            {"conversation": {"coordinator_desktop_enabled": desktop_enabled}}
        ),
        conversation_coordinator=coordinator,
    )
    window.agent = object()
    try:
        window._init_chat_stream_controller()

        controller = window.chat_stream_controller
        assert controller.agent is window.agent
        assert controller.conversation_coordinator is (
            coordinator if desktop_enabled else None
        )
        assert window.song_controller.chat_stream_controller is controller
        assert window.interaction_controller.chat_stream_controller is controller
    finally:
        window.close()


def test_overlay_busy_owner_switch_preserves_outer_song_and_vad_gates(qapp):
    del qapp
    from ui.qt_overlay import OverlayWindow

    class _BusySource:
        def __init__(self, busy):
            self.busy = busy

        def is_busy(self):
            return self.busy

        def shutdown(self, *args, **kwargs):
            del args, kwargs

    coordinator = _BusySource(True)
    legacy_controller = _BusySource(False)
    with patch.object(OverlayWindow, "_init_backend", lambda self: None):
        window = OverlayWindow()
    window.host = SimpleNamespace(
        config=AppConfig.model_validate(
            {"conversation": {"coordinator_desktop_enabled": True}}
        ),
        conversation_coordinator=coordinator,
    )
    window.chat_stream_controller = legacy_controller
    window._is_song_busy = lambda: False
    window._is_user_speaking = lambda: False
    try:
        assert window._is_conversation_busy()

        coordinator.busy = False
        legacy_controller.busy = True
        assert window._is_conversation_busy()

        window._is_song_busy = lambda: True
        assert window._is_conversation_busy()

        window._is_song_busy = lambda: False
        window._is_user_speaking = lambda: True
        assert window._is_proactive_busy()

        window.host.config = AppConfig()
        coordinator.busy = True
        legacy_controller.busy = False
        assert not window._is_conversation_busy()
        legacy_controller.busy = True
        assert window._is_conversation_busy()
    finally:
        window.close()


def test_voice_rearm_waits_for_coordinator_producer_tail(qapp):
    from ui.qt_overlay import OverlayWindow

    class _BusyCoordinator:
        def __init__(self):
            self.busy = True

        def is_busy(self):
            return self.busy

    class _VoiceInput:
        voice_mode_active = True
        voice_session_id = 7

        def __init__(self):
            self.scheduled = []
            self.started = []

        def schedule_next_recording(self, delay_ms):
            self.scheduled.append(delay_ms)

        def maybe_start_recording(self, session_id):
            self.started.append(session_id)

    coordinator = _BusyCoordinator()
    voice_input = _VoiceInput()
    with patch.object(OverlayWindow, "_init_backend", lambda self: None):
        window = OverlayWindow()
    original_voice_input = window.voice_input_controller
    window.host = SimpleNamespace(
        config=AppConfig.model_validate(
            {"conversation": {"coordinator_desktop_enabled": True}}
        ),
        conversation_coordinator=coordinator,
    )
    window.voice_input_controller = voice_input
    try:
        window._schedule_next_voice_recording(10)

        assert voice_input.scheduled == []
        assert not _spin_qt_until(qapp, lambda: bool(voice_input.started), timeout=0.05)

        coordinator.busy = False
        assert _spin_qt_until(qapp, lambda: voice_input.started == [7])
    finally:
        window.voice_input_controller = original_voice_input
        window.close()


def test_voice_rearm_survives_late_admission_until_both_terminals(qapp):
    from PySide6.QtCore import QTimer

    from ui.controllers.chat_stream_controller import ChatStreamController
    from ui.qt_overlay import OverlayWindow

    class _VoiceInput:
        voice_mode_active = True
        voice_session_id = 7

        def __init__(self):
            self.attempts = 0
            self.started = []

        def schedule_next_recording(self, delay_ms):
            session_id = self.voice_session_id
            QTimer.singleShot(
                delay_ms,
                lambda sid=session_id: self.maybe_start_recording(sid),
            )

        def maybe_start_recording(self, session_id):
            self.attempts += 1
            if (
                not self.voice_mode_active
                or session_id != self.voice_session_id
                or window._is_conversation_busy()
            ):
                return
            self.started.append(session_id)

    executor = _VoiceRearmLifecycleExecutor()
    coordinator = ConversationCoordinator(executor)
    counting_coordinator = _CountingCoordinator(coordinator)
    voice_input = _VoiceInput()
    first_done = threading.Event()
    with patch.object(OverlayWindow, "_init_backend", lambda self: None):
        window = OverlayWindow()
    original_voice_input = window.voice_input_controller
    original_chat_controller = window.chat_stream_controller
    window.host = SimpleNamespace(
        config=AppConfig.model_validate(
            {"conversation": {"coordinator_desktop_enabled": True}}
        ),
        conversation_coordinator=counting_coordinator,
    )
    window.voice_input_controller = voice_input
    window._is_song_busy = lambda: False

    def handle_chat_done():
        window._handle_chat_stream_done()
        first_done.set()

    controller = ChatStreamController(
        parent=qapp,
        agent=_ForbiddenLegacyAgent(),
        conversation_id_provider=lambda: "desktop::default",
        visual_overrides_provider=lambda: {"costume_mode": "random"},
        audio_controller=_NullAudioController(),
        typewriter_controller=_NullTypewriterController(),
        set_character_image=lambda image: None,
        set_busy=lambda busy: None,
        on_chat_done=handle_chat_done,
        on_error=lambda message: None,
        apply_visual=lambda visual: None,
        conversation_coordinator=counting_coordinator,
    )
    window.chat_stream_controller = controller
    try:
        controller.start_chat("安排下一次录音")
        assert _spin_qt_until(qapp, first_done.is_set)
        assert _spin_qt_until(qapp, lambda: not coordinator.is_busy())

        controller.start_chat("timer 到点前迟到 admission")
        assert executor.second_started.wait(2.0)
        controller.stop_current()
        assert executor.second_cancel_observed.wait(2.0)
        checks_after_stop = counting_coordinator.busy_checks
        attempts_after_stop = voice_input.attempts

        assert _spin_qt_until(
            qapp,
            lambda: (
                counting_coordinator.busy_checks > checks_after_stop
                or voice_input.attempts > attempts_after_stop
            ),
            timeout=0.6,
        )
        assert voice_input.started == []
        assert coordinator.is_busy()

        executor.release_second_terminal.set()
        assert _spin_qt_until(qapp, lambda: not coordinator.is_busy())
        assert _spin_qt_until(qapp, lambda: voice_input.started == [7])
    finally:
        executor.release_second_terminal.set()
        controller.shutdown()
        window.voice_input_controller = original_voice_input
        window.chat_stream_controller = original_chat_controller
        window.close()


def test_voice_rearm_discards_stale_session_timer(qapp):
    from PySide6.QtCore import QTimer

    from ui.controllers.chat_stream_controller import ChatStreamController
    from ui.qt_overlay import OverlayWindow

    class _VoiceInput:
        voice_mode_active = True
        voice_session_id = 7

        def __init__(self):
            self.started = []

        def schedule_next_recording(self, delay_ms):
            session_id = self.voice_session_id
            QTimer.singleShot(
                delay_ms,
                lambda sid=session_id: self.maybe_start_recording(sid),
            )

        def maybe_start_recording(self, session_id):
            if (
                self.voice_mode_active
                and session_id == self.voice_session_id
                and not window._is_conversation_busy()
            ):
                self.started.append(session_id)

    coordinator = ConversationCoordinator(_EmptyDoneExecutor())
    counting_coordinator = _CountingCoordinator(coordinator)
    voice_input = _VoiceInput()
    completed_turns = 0
    with patch.object(OverlayWindow, "_init_backend", lambda self: None):
        window = OverlayWindow()
    original_voice_input = window.voice_input_controller
    original_chat_controller = window.chat_stream_controller
    window.host = SimpleNamespace(
        config=AppConfig.model_validate(
            {"conversation": {"coordinator_desktop_enabled": True}}
        ),
        conversation_coordinator=counting_coordinator,
    )
    window.voice_input_controller = voice_input
    window._is_song_busy = lambda: False

    def handle_chat_done():
        nonlocal completed_turns
        window._handle_chat_stream_done()
        completed_turns += 1

    controller = ChatStreamController(
        parent=qapp,
        agent=_ForbiddenLegacyAgent(),
        conversation_id_provider=lambda: "desktop::default",
        visual_overrides_provider=lambda: {"costume_mode": "random"},
        audio_controller=_NullAudioController(),
        typewriter_controller=_NullTypewriterController(),
        set_character_image=lambda image: None,
        set_busy=lambda busy: None,
        on_chat_done=handle_chat_done,
        on_error=lambda message: None,
        apply_visual=lambda visual: None,
        conversation_coordinator=counting_coordinator,
    )
    window.chat_stream_controller = controller
    try:
        controller.start_chat("为旧 voice session 安排 timer")
        assert _spin_qt_until(qapp, lambda: completed_turns == 1)
        voice_input.voice_session_id = 8
        assert not _spin_qt_until(
            qapp,
            lambda: bool(voice_input.started),
            timeout=0.4,
        )

        controller.start_chat("为新 voice session 安排 timer")
        assert _spin_qt_until(qapp, lambda: completed_turns == 2)
        assert _spin_qt_until(qapp, lambda: voice_input.started == [8])
    finally:
        controller.shutdown()
        window.voice_input_controller = original_voice_input
        window.chat_stream_controller = original_chat_controller
        window.close()


def test_flag_true_chat_worker_unwraps_each_envelope_without_buffering(qapp):
    from ui.workers.chat_worker import ChatWorker

    executor = _BarrierDesktopExecutor()
    coordinator = ConversationCoordinator(executor)
    emitted = []
    worker = ChatWorker(
        _ForbiddenLegacyAgent(),
        "你好",
        "desktop::default",
        {"costume_mode": "fixed", "costume_set": "summer"},
        True,
        "chat",
        conversation_coordinator=coordinator,
        request_id="coordinator-request",
    )
    worker.stream_event.connect(lambda name, data: emitted.append((name, data)))

    worker.start()
    assert executor.first_event_produced.wait(2.0)
    assert _spin_qt_until(qapp, lambda: len(emitted) == 1)
    assert emitted == [("status", {"state": "thinking", "message": ""})]

    executor.release_tail.set()
    assert worker.wait(2000)
    assert _spin_qt_until(qapp, lambda: len(emitted) == len(DESKTOP_LEGACY_GOLDEN))
    assert emitted == [
        (event["event"], event["data"])
        for event in copy.deepcopy(DESKTOP_LEGACY_GOLDEN)
    ]
    assert executor.calls == 1
    assert coordinator.report_presentation_terminal(
        worker.accepted_turn.turn_id,
        PresentationTerminalOutcome.COMPLETED,
    )
    assert _spin_qt_until(qapp, lambda: not coordinator.is_busy())


def test_stop_current_silently_drains_coordinator_producer_to_terminal(qapp):
    from ui.controllers.chat_stream_controller import ChatStreamController

    executor = _CancelDrainExecutor()
    coordinator = ConversationCoordinator(executor)
    controller = ChatStreamController(
        parent=qapp,
        agent=_ForbiddenLegacyAgent(),
        conversation_id_provider=lambda: "desktop::default",
        visual_overrides_provider=lambda: {"costume_mode": "random"},
        audio_controller=_NullAudioController(),
        typewriter_controller=_NullTypewriterController(),
        set_character_image=lambda image: None,
        set_busy=lambda busy: None,
        on_chat_done=lambda: None,
        on_error=lambda message: None,
        apply_visual=lambda visual: None,
        conversation_coordinator=coordinator,
    )

    controller.start_chat("停止测试")
    worker = controller.chat_worker
    emitted = []
    worker.stream_event.connect(lambda name, data: emitted.append((name, data)))
    assert executor.started.wait(2.0)
    executor.release_first.set()
    assert _spin_qt_until(qapp, lambda: emitted == [
        ("status", {"state": "thinking", "message": ""})
    ])

    controller.stop_current()

    assert executor.cancel_observed.wait(2.0)
    assert executor.producer_terminal.wait(2.0)
    assert worker.wait(2000)
    qapp.processEvents()
    assert emitted == [("status", {"state": "thinking", "message": ""})]
    assert _spin_qt_until(qapp, lambda: not coordinator.is_busy())
    controller.shutdown()


def test_stop_before_admission_never_submits_or_starts_the_producer(qapp):
    from ui.controllers.chat_stream_controller import ChatStreamController

    engine = _RecordingChatEngine()
    coordinator = ConversationCoordinator(LegacyRunTurnAdapter(engine))
    pre_admission = _PreAdmissionProbeCoordinator(coordinator)
    controller = ChatStreamController(
        parent=qapp,
        agent=_ForbiddenLegacyAgent(),
        conversation_id_provider=lambda: "desktop::default",
        visual_overrides_provider=lambda: {"costume_mode": "random"},
        audio_controller=_NullAudioController(),
        typewriter_controller=_NullTypewriterController(),
        set_character_image=lambda image: None,
        set_busy=lambda busy: None,
        on_chat_done=lambda: None,
        on_error=lambda message: None,
        apply_visual=lambda visual: None,
        conversation_coordinator=pre_admission,
    )

    controller.start_chat("立即停止")
    worker = controller.chat_worker
    assert pre_admission.busy_probe_started.wait(2.0)

    controller.stop_current()
    pre_admission.release_busy_probe.set()

    try:
        assert worker.wait(2000)
        qapp.processEvents()
        assert pre_admission.submit_calls == 0
        assert engine.user_calls == []
        assert worker.accepted_turn is None
        assert _spin_qt_until(qapp, lambda: not coordinator.is_busy())
    finally:
        pre_admission.release_busy_probe.set()
        if worker.accepted_turn is not None:
            coordinator.report_presentation_terminal(
                worker.accepted_turn.turn_id,
                PresentationTerminalOutcome.STOPPED,
            )
        controller.shutdown()


def test_stop_at_coordinator_internal_pre_admission_point_returns_without_waiting(
    qapp,
):
    from ui.controllers.chat_stream_controller import ChatStreamController

    executor = _UncooperativeExecutor()
    coordinator = _InternalAdmissionBarrierCoordinator(executor)
    controller = ChatStreamController(
        parent=qapp,
        agent=_ForbiddenLegacyAgent(),
        conversation_id_provider=lambda: "desktop::default",
        visual_overrides_provider=lambda: {"costume_mode": "random"},
        audio_controller=_NullAudioController(),
        typewriter_controller=_NullTypewriterController(),
        set_character_image=lambda image: None,
        set_busy=lambda busy: None,
        on_chat_done=lambda: None,
        on_error=lambda message: None,
        apply_visual=lambda visual: None,
        conversation_coordinator=coordinator,
    )

    controller.start_chat("在线性化前立即停止")
    worker = controller.chat_worker
    assert coordinator.before_linearization.wait(2.0)
    watchdog = threading.Timer(1.0, coordinator.release_linearization.set)
    watchdog.start()
    try:
        controller.stop_current()
        assert not coordinator.release_linearization.is_set(), (
            "GUI stop waited for the worker's in-flight submit"
        )
    finally:
        coordinator.release_linearization.set()
        watchdog.cancel()

    assert worker.wait(2000)
    qapp.processEvents()
    assert worker.accepted_turn is None
    assert executor.calls == 0
    assert not coordinator.is_busy()
    assert controller.shutdown()


def test_shutdown_timeout_retains_running_qthread_without_delete_later(qapp):
    from ui.controllers.chat_stream_controller import ChatStreamController
    from ui.workers.chat_worker import ChatWorker

    class _ObservedChatWorker(ChatWorker):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.delete_later_running_states = []

        def deleteLater(self):  # noqa: N802 - Qt override
            running = self.isRunning()
            self.delete_later_running_states.append(running)
            if running:
                return None
            return super().deleteLater()

    executor = _UncooperativeExecutor()
    coordinator = ConversationCoordinator(executor)
    controller = ChatStreamController(
        parent=qapp,
        agent=_ForbiddenLegacyAgent(),
        conversation_id_provider=lambda: "desktop::default",
        visual_overrides_provider=lambda: {"costume_mode": "random"},
        audio_controller=_NullAudioController(),
        typewriter_controller=_NullTypewriterController(),
        set_character_image=lambda image: None,
        set_busy=lambda busy: None,
        on_chat_done=lambda: None,
        on_error=lambda message: None,
        apply_visual=lambda visual: None,
        conversation_coordinator=coordinator,
    )

    with patch(
        "ui.controllers.chat_stream_controller.ChatWorker",
        _ObservedChatWorker,
    ):
        controller.start_chat("关窗超时安全测试")
    worker = controller.chat_worker
    assert executor.started.wait(2.0)

    try:
        assert controller.shutdown(wait_ms=0) is False
        assert worker.isRunning()
        assert worker in controller.retired_chat_workers
        assert worker.parent() is controller
        assert worker.delete_later_running_states == []
    finally:
        executor.release.set()

    assert worker.wait(2000)
    assert controller.shutdown(wait_ms=100) is True
    assert worker.delete_later_running_states == [False]
    assert controller.retired_chat_workers == []
    qapp.processEvents()


def test_overlay_close_event_is_ignored_when_chat_qthread_shutdown_times_out(qapp):
    del qapp
    from PySide6.QtGui import QCloseEvent
    from ui.qt_overlay import OverlayWindow

    class _TimedOutChatController:
        def __init__(self):
            self.shutdown_calls = []

        def shutdown(self, wait_ms):
            self.shutdown_calls.append(wait_ms)
            return False

    with patch.object(OverlayWindow, "_init_backend", lambda self: None):
        window = OverlayWindow()
    timed_out = _TimedOutChatController()
    window.chat_stream_controller = timed_out
    event = QCloseEvent()
    try:
        window.closeEvent(event)

        assert timed_out.shutdown_calls == [1500]
        assert not event.isAccepted()
    finally:
        window.chat_stream_controller = None
        window.close()


def test_replacement_user_turn_waits_for_cancelled_producer_then_is_admitted(qapp):
    from ui.controllers.chat_stream_controller import ChatStreamController

    executor = _PreemptThenCompleteExecutor()
    coordinator = ConversationCoordinator(executor)
    errors = []
    chat_done = threading.Event()
    controller = ChatStreamController(
        parent=qapp,
        agent=_ForbiddenLegacyAgent(),
        conversation_id_provider=lambda: "desktop::default",
        visual_overrides_provider=lambda: {"costume_mode": "random"},
        audio_controller=_NullAudioController(),
        typewriter_controller=_NullTypewriterController(),
        set_character_image=lambda image: None,
        set_busy=lambda busy: None,
        on_chat_done=chat_done.set,
        on_error=errors.append,
        apply_visual=lambda visual: None,
        conversation_coordinator=coordinator,
    )

    try:
        controller.start_chat("旧消息")
        assert executor.first_started.wait(2.0)

        controller.start_chat("新消息")
        replacement_worker = controller.chat_worker
        assert executor.first_cancel_observed.wait(2.0)
        assert _spin_qt_until(
            qapp,
            lambda: bool(errors) or not replacement_worker.isRunning(),
            timeout=0.1,
        ) is False
        assert errors == []

        executor.release_first_terminal.set()
        assert executor.second_started.wait(2.0)
        assert _spin_qt_until(qapp, chat_done.is_set)
        assert _spin_qt_until(qapp, lambda: not coordinator.is_busy())
        assert executor.contents == ["旧消息", "新消息"]
        assert errors == []
    finally:
        executor.release_first_terminal.set()
        controller.shutdown()


def test_submit_time_system_busy_rejection_is_silent_and_restores_completion_gate(
    qapp,
):
    from ui.controllers.chat_stream_controller import ChatStreamController

    executor = _BlockingExecutor()
    coordinator = ConversationCoordinator(executor)
    delayed = _DelayedSubmitCoordinator(coordinator)

    errors = []
    completion_gate_restored = threading.Event()
    chat_done = threading.Event()
    controller = ChatStreamController(
        parent=qapp,
        agent=_ForbiddenLegacyAgent(),
        conversation_id_provider=lambda: "desktop::default",
        visual_overrides_provider=lambda: {"costume_mode": "random"},
        audio_controller=_NullAudioController(),
        typewriter_controller=_NullTypewriterController(),
        set_character_image=lambda image: None,
        set_busy=lambda busy: None,
        on_chat_done=chat_done.set,
        on_error=errors.append,
        apply_visual=lambda visual: None,
        conversation_coordinator=delayed,
    )

    competitor = None
    try:
        token = controller.start_system_turn(
            SimpleNamespace(
                directive="这条主动发言应被最终 admission 丢弃",
                conversation_id="desktop::default",
                source="galgame",
            )
        )
        assert token is not None
        assert delayed.submit_started.wait(2.0)

        competitor = coordinator.submit(
            ConversationRequest(
                request_id="submit-race-winner",
                requested_conversation_id="desktop::default",
                kind=ConversationTurnKind.USER,
                content="在最终 submit 前抢先获准的用户 turn",
            )
        )
        assert executor.started.wait(2.0)
        controller.notify_on_current_stream_done(completion_gate_restored.set)
        delayed.release_submit.set()

        assert _spin_qt_until(qapp, completion_gate_restored.is_set)
        assert _spin_qt_until(qapp, chat_done.is_set)
        assert delayed.submit_calls == 1
        assert delayed.last_admission.decision is AdmissionDecision.BUSY
        assert not controller.is_busy()
        assert errors == []
        assert executor.contents == ["在最终 submit 前抢先获准的用户 turn"]
        assert coordinator.is_busy()
    finally:
        delayed.release_submit.set()
        executor.release.set()
        if competitor is not None:
            coordinator.report_presentation_terminal(
                competitor.turn_id,
                PresentationTerminalOutcome.STOPPED,
            )
            list(competitor.accepted_turn.events())
        assert _spin_qt_until(qapp, lambda: not coordinator.is_busy())
        controller.shutdown()


def test_stale_system_start_cannot_cancel_controller_owned_user_turn(qapp):
    from ui.controllers.chat_stream_controller import ChatStreamController
    from ui.qt_overlay import OverlayWindow

    executor = _BlockingExecutor()
    coordinator = ConversationCoordinator(executor)
    controller = ChatStreamController(
        parent=qapp,
        agent=_ForbiddenLegacyAgent(),
        conversation_id_provider=lambda: "desktop::default",
        visual_overrides_provider=lambda: {"costume_mode": "random"},
        audio_controller=_NullAudioController(),
        typewriter_controller=_NullTypewriterController(),
        set_character_image=lambda image: None,
        set_busy=lambda busy: None,
        on_chat_done=lambda: None,
        on_error=lambda message: None,
        apply_visual=lambda visual: None,
        conversation_coordinator=coordinator,
    )

    controller.start_chat("不能被旧主动请求打断的用户 turn")
    assert executor.started.wait(2.0)
    user_worker = controller.chat_worker
    user_token = controller.active_stream_token
    proactive_finished = threading.Event()
    reaction_closed = threading.Event()
    overlay = SimpleNamespace(
        chat_stream_controller=controller,
        proactive_arbiter=SimpleNamespace(
            system_speech_finished=proactive_finished.set,
        ),
        _reaction_stream_closed=reaction_closed.set,
    )

    try:
        OverlayWindow._start_system_turn_gui(
            overlay,
            SimpleNamespace(
                directive="已经过期的主动请求",
                conversation_id="desktop::default",
                source="galgame",
            )
        )
        _spin_qt_until(qapp, lambda: not user_worker.isRunning(), timeout=0.1)

        assert controller.chat_worker is user_worker
        assert controller.active_stream_token == user_token
        assert not user_worker.cancel_event.is_set()
        assert not executor.cancel_tokens[0].is_set()
        assert executor.contents == ["不能被旧主动请求打断的用户 turn"]
        assert coordinator.is_busy()
        assert proactive_finished.is_set()
        assert reaction_closed.is_set()
    finally:
        controller.stop_current()
        executor.release.set()
        user_worker.wait(2000)
        assert _spin_qt_until(qapp, lambda: not coordinator.is_busy())
        controller.shutdown()


def test_normal_presentation_completion_releases_coordinator_after_producer(qapp):
    from ui.controllers.chat_stream_controller import ChatStreamController

    coordinator = ConversationCoordinator(_EmptyDoneExecutor())
    chat_done = threading.Event()
    controller = ChatStreamController(
        parent=qapp,
        agent=_ForbiddenLegacyAgent(),
        conversation_id_provider=lambda: "desktop::default",
        visual_overrides_provider=lambda: {"costume_mode": "random"},
        audio_controller=_NullAudioController(),
        typewriter_controller=_NullTypewriterController(),
        set_character_image=lambda image: None,
        set_busy=lambda busy: None,
        on_chat_done=chat_done.set,
        on_error=lambda message: None,
        apply_visual=lambda visual: None,
        conversation_coordinator=coordinator,
    )

    controller.start_chat("完成测试")

    assert _spin_qt_until(qapp, chat_done.is_set)
    assert _spin_qt_until(qapp, lambda: not coordinator.is_busy())
    controller.shutdown()


def test_system_turn_submits_raw_directive_once_with_desktop_snapshot(qapp):
    from ui.controllers.chat_stream_controller import ChatStreamController

    executor = _CapturingRequestExecutor()
    coordinator = ConversationCoordinator(executor)
    controller = ChatStreamController(
        parent=qapp,
        agent=_ForbiddenLegacyAgent(),
        conversation_id_provider=lambda: "desktop::default",
        visual_overrides_provider=lambda: {
            "costume_mode": "fixed",
            "costume_set": "summer",
        },
        audio_controller=_NullAudioController(),
        typewriter_controller=_NullTypewriterController(),
        set_character_image=lambda image: None,
        set_busy=lambda busy: None,
        on_chat_done=lambda: None,
        on_error=lambda message: None,
        apply_visual=lambda visual: None,
        conversation_coordinator=coordinator,
    )

    controller.start_system_turn(
        SimpleNamespace(
            directive="刚刚唱完歌，请自然收尾。",
            conversation_id="desktop::default",
            source="song",
        )
    )

    assert executor.received.wait(2.0)
    assert executor.request.content == "刚刚唱完歌，请自然收尾。"
    assert executor.request.kind is ConversationTurnKind.SYSTEM
    assert executor.request.source == "song"
    assert executor.request.include_user_time_context is False
    assert executor.request.visual_overrides == {
        "costume_mode": "fixed",
        "costume_set": "summer",
    }
    assert _spin_qt_until(qapp, lambda: not coordinator.is_busy())
    controller.shutdown()

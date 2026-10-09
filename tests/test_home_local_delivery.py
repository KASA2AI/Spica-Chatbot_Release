"""Real Qt worker/playback-controller boundary, with no model/device operations."""
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
pytest.importorskip('PySide6')
from PySide6.QtWidgets import QWidget
from PySide6.QtTest import QTest

from test_voice_wake import qapp
from test_chat_stream_controller_playback_advance import _FakeAudioController, _make_controller
from ui.controllers.typewriter_controller import TypewriterController
from ui.controllers.home_speech_controller import HomeSpeechController
from spica.core.proactive import ScheduledProactiveTurnRequest
from spica.config.schema import AppConfig


def until(predicate):
    for _ in range(150):
        QTest.qWait(10)
        if predicate():
            return
    assert predicate()


@pytest.fixture
def delivery(qapp, tmp_path):
    release = threading.Event()
    path = tmp_path / 'speech.wav'
    path.write_bytes(b'RIFF')
    options, observed = [], []
    def stream(*args, **kwargs):
        options.append(kwargs)
        yield {'event': 'unit_ready', 'data': {'index': 0, 'display_text': '起きたね。', 'audio_path': str(path)}}
        yield {'event': 'done', 'data': {'answer': '起きたね。', 'units_count': 1}}
        assert release.wait(2)
    engine = SimpleNamespace(config=AppConfig(), daily_voice_enabled=True, stream_voice=stream)
    class Audio(_FakeAudioController):
        def play_chat_audio(self, path, token, on_finished, *, on_playback=None, **kwargs):
            self.play_calls.append((path, token, on_finished))
            self.outcome, self.route = on_playback, kwargs
            return True
    audio = Audio()
    chat = _make_controller(audio)
    chat.agent = engine
    writer = TypewriterController(None, lambda _: None)
    chat.typewriter_controller = writer
    window = QWidget()
    window._restart_requested = False
    window._is_proactive_busy = chat.is_busy
    window.host = SimpleNamespace(chat_engine=engine,
        home_runtime=SimpleNamespace(alarms=SimpleNamespace(observe_reply=observed.append)))
    window.voice_input_controller = SimpleNamespace(suspended=False, interrupt_current_recording=Mock(),
        bind_business_reply=Mock(), end_response_scope=Mock())
    window.chat_stream_controller, window.audio_controller = chat, audio
    controller = HomeSpeechController(window)
    try:
        yield SimpleNamespace(controller=controller, chat=chat, audio=audio, window=window,
            release=release, options=options, observed=observed)
    finally:
        release.set()
        controller.shutdown()
        chat.shutdown(1500)
        QTest.qWait(60)
        assert controller.shutdown()
        writer.stop()
        chat.deleteLater()
        writer.deleteLater()
        window.deleteLater()


def request(**values):
    now = time.monotonic()
    return ScheduledProactiveTurnRequest('叫醒', source='home.wake:test', policy='queue_latest',
        due_at=values.get('due_at', now), yield_at=values.get('yield_at', now + .1),
        first_sound_deadline=values.get('deadline', now + 2), volume=.65,
        response_scope_id='wake:test', response_window_seconds=5)


def test_alarm_uses_actual_start_and_waits_for_producer_release(delivery):
    env = delivery
    handle = env.controller.submit(request())
    until(lambda: bool(env.audio.play_calls))
    assert handle.admitted.result() and not handle.audio_started.done()
    assert env.options[0]['want_audio'] and not env.options[0]['inherit_active_domain']
    assert env.audio.route == {'volume': .65, 'audio_route': 'home'}
    env.audio.outcome('started', time.monotonic())
    assert handle.audio_started.result() is not None
    env.audio.outcome('completed', time.monotonic())
    env.audio.play_calls[0][2]()
    env.chat.typewriter_controller.stop()
    env.chat._mark_text_finished()
    until(handle.result.done)
    assert handle.result.result().status == 'completed'
    assert handle.result.result().audio_outcome == 'completed'
    assert not handle.released.done()
    env.release.set()
    until(handle.released.done)
    assert any(event.kind == 'desktop_turn_lifecycle_released' for event in env.observed)
    env.window.voice_input_controller.bind_business_reply.assert_called_once()


def test_expired_pending_alarm_never_starts_or_interrupts(delivery):
    env = delivery
    env.window._is_proactive_busy = lambda: True
    now = time.monotonic()
    handle = env.controller.submit(request(due_at=now-6, yield_at=now-1, deadline=now-.1))
    until(handle.result.done)
    assert handle.result.result().status == 'expired'
    assert not handle.admitted.result() and handle.released.result()
    assert not env.options and not env.audio.play_calls
    env.window.voice_input_controller.interrupt_current_recording.assert_not_called()


def test_prepared_silent_memory_withdrawal_releases_delivery(delivery, tmp_path):
    from test_prepared_speech import prepare
    from spica.core.proactive import PreparedProactiveTurnRequest
    engine, draft, _ = prepare(tmp_path)
    checks = iter((True, False))  # Binding is valid; withdrawal races the first replay event.
    draft.context_current = lambda: next(checks, False)
    env = delivery
    env.controller.engine = env.chat.agent = engine
    env.controller._prepared = draft
    handle = env.controller.submit(PreparedProactiveTurnRequest('', source='home.departure',
        speech_id=draft.id, want_audio=False, first_sound_deadline=time.monotonic()+.1))
    until(handle.released.done)
    assert handle.admitted.result() and handle.result.result().status == 'failed'
    assert not env.chat.is_busy()
    draft.settle(delivered=False)
    assert draft.closed


def test_silent_hidden_bedtime_has_no_success_receipt(delivery):
    env = delivery
    env.chat._presentation_turn_id = 'goodnight'
    env.chat._text_presented = True
    env.chat._text_surface_visible = lambda: False
    env.chat._record_presentation('completed')
    assert env.observed[-1].outcome == 'failed'
    assert not env.observed[-1].awaited_audio_playback


def test_close_settles_proposals_queued_before_gui_dispatch(delivery):
    env = delivery
    handle = env.controller.submit(request())
    warm = env.controller.prepare_audio(lambda: True, revision=0)
    assert env.controller.shutdown()
    assert handle.result.result().status == 'cancelled' and handle.released.result()
    assert warm.result() == 'desktop_unavailable'
    QTest.qWait(60)
    assert not env.audio.play_calls

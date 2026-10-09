from __future__ import annotations


import json


from types import SimpleNamespace


import pytest


pytest.importorskip("PySide6")


from PySide6.QtCore import QObject


from PySide6.QtWidgets import QApplication


from spica.core.character import CharacterPackage


from ui.controllers.voice_input_controller import ReactionVoiceDuckGate, VoiceInputController


from ui.overlay_config import load_voice_wake_preferences, save_voice_wake_enabled, save_voice_wake_words


from ui.wake_words import default_wake_words


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def voice(qapp, monkeypatch):
    parent = QObject()
    calls = SimpleNamespace(wake=[], text=[], busy=[], active=[], dialogue=[], timers=[], starts=[])
    controller = VoiceInputController(
        parent, calls.active.append, calls.busy.append, lambda: False,
        calls.dialogue.append, calls.text.append, lambda: True,
    )
    controller.set_stt_port(object())
    controller.on_wake_word = calls.wake.append
    monkeypatch.setattr(controller, "schedule_next_recording", lambda delay_ms=320: calls.timers.append(delay_ms))
    monkeypatch.setattr(controller, "_start_speech_worker", lambda: calls.starts.append(True))
    controller.configure_wake(True, ("Spica", "斯皮卡"))
    yield controller, calls
    controller.speech_worker = None
    controller.shutdown()
    parent.deleteLater()


def test_passive_speech_is_private_until_current_role_is_addressed(voice):
    controller, calls = voice
    sid = controller.voice_session_id
    controller.handle_speech_status("等待说话", sid)
    controller.handle_recognized("今天天气不错", sid)
    controller.handle_error("没有检测到语音输入", sid)
    assert calls.text == calls.wake == calls.dialogue == []
    controller.handle_recognized("Spica", sid)  # ASR text can never wake the passive microphone.
    assert calls.wake == []
    controller.handle_wake_detected("斯皮卡", sid)
    assert calls.wake == ["斯皮卡"]
    assert not controller.voice_mode_active
    sid = controller.wait_for_wake_reply()
    controller.maybe_start_recording(sid)
    assert calls.starts == []
    controller.finish_wake_reply(sid, True)
    assert controller.daily_active and controller._response_preparing
    assert not controller.capture_ready and not controller.voice_mode_active
    sid = controller.voice_session_id
    import time
    controller.handle_capture_ready(time.monotonic(), sid)
    assert controller.capture_ready and calls.active[-1] is True
    controller.handle_recognized("接下来正常聊天", sid)
    assert calls.text == ["接下来正常聊天"]


def test_manual_wake_uses_existing_reply_and_bounded_listening_with_kws_disabled(voice, monkeypatch):
    from ui.qt_overlay import OverlayWindow
    controller, calls = voice
    requests, results, now = [], [], [100.]
    monkeypatch.setattr('ui.controllers.voice_input_controller.time.monotonic', lambda: now[0])
    controller.configure_wake(False, ())
    chat = SimpleNamespace(start_system_turn=lambda request: requests.append(request) or object(),
                           notify_on_current_reply_result=results.append, stream_session_id=7)
    window = SimpleNamespace(chat_stream_controller=chat, voice_input_controller=controller,
                             _schedule_next_voice_recording=lambda: None)
    controller.on_wake_word = lambda name: OverlayWindow._on_voice_wake_word(window, name)
    assert controller.request_manual_wake('Spica')
    assert len(requests) == len(results) == 1 and requests[0].source == 'desktop.voice_wake'
    assert 'Spica' in requests[0].directive
    assert not controller.request_manual_wake('Spica'), 'Do not duplicate an in-flight wake reply.'
    assert not controller._response_preparing and not controller.capture_ready
    from ui.controllers.chat_stream_controller import ReplyPresentationResult
    results[0](ReplyPresentationResult(True, True, True, True))
    assert controller._response_preparing and controller.daily_active
    assert not controller.wake_enabled and not controller.voice_mode_active
    controller.handle_capture_ready(now[0], controller.voice_session_id)
    assert controller._response_until == 108.
    assert not controller.request_manual_wake('Spica'), 'An existing response window owns the mic.'
    now[0] = 108.
    controller._expire_response_window()
    assert not controller.conversation_active and not controller.listening_enabled
    assert calls.wake == calls.text == [] and len(requests) == 1


def test_silent_wake_grants_only_one_bounded_response_window(voice, monkeypatch):
    from ui.controllers.chat_stream_controller import ReplyPresentationResult
    controller, _ = voice
    now, replies = [100.], []
    monkeypatch.setattr('ui.controllers.voice_input_controller.time.monotonic', lambda: now[0])
    controller.configure_wake(False, ())
    session = controller.wait_for_wake_reply()
    controller.bind_reply(SimpleNamespace(stream_session_id=1, notify_on_current_reply_result=replies.append),
                          wake_session=session)
    replies[0](ReplyPresentationResult(True, True, False, False))
    assert controller.preparing_response
    controller.handle_capture_ready(100, controller.voice_session_id)
    assert controller.capture_ready and controller._response_until == 108
    now[0] = 108.
    controller._expire_response_window()
    assert not controller.conversation_active and not controller.listening_enabled


def test_timed_response_accepts_arbitrary_speech_and_keeps_voice_preference(voice, monkeypatch):
    controller, calls = voice
    now = [100.]
    monkeypatch.setattr('ui.controllers.voice_input_controller.time.monotonic', lambda: now[0])
    controller.begin_response_window(8, deadline=115)
    assert not controller.voice_mode_active and controller._response_input_active()
    controller.handle_recognized('再让我抱一会', controller.voice_session_id)
    assert calls.text == ['再让我抱一会'] and controller._response_pending
    now[0] = 109
    controller.finish_response_reply()
    assert controller._response_until == 115 and not controller.voice_mode_active
    now[0] = 115
    controller._expire_response_window()
    controller.handle_recognized('过期的声音', controller.voice_session_id)
    assert calls.text == ['再让我抱一会']
    assert not controller._response_input_active() and controller.wake_enabled


def test_daily_deadline_starts_at_real_capture_and_noise_does_not_refresh_it(voice, monkeypatch):
    controller, calls = voice
    now = [100.]
    monkeypatch.setattr('ui.controllers.voice_input_controller.time.monotonic', lambda: now[0])
    controller.arm_response_scope('daily:test', kind='daily')
    controller.begin_response_window(8, scope_id='daily:test')
    sid = controller.voice_session_id
    controller.handle_recognized('not ready', sid)
    assert controller._response_until == 0 and calls.text == []
    now[0] = 102
    controller.handle_capture_ready(now[0], sid)
    assert controller._response_until == 110
    now[0] = 106
    controller.handle_error('没有检测到语音输入。', sid)
    controller.handle_recognized('', sid)
    controller.handle_capture_ready(now[0], sid)  # a retry is not a new window
    assert controller._response_until == 110
    now[0] = 110
    controller._expire_response_window()
    controller.handle_capture_ready(110, sid)
    controller.handle_recognized('too late', sid)
    assert not controller.daily_active and not controller.capture_ready and calls.text == []
    assert controller.wake_enabled


@pytest.mark.parametrize('text', ['一句说完了', ''])
def test_daily_inflight_utterance_can_finish_but_empty_result_cannot_renew(voice, monkeypatch, text):
    controller, calls = voice
    now = [100.]
    monkeypatch.setattr('ui.controllers.voice_input_controller.time.monotonic', lambda: now[0])
    controller.arm_response_scope('daily:test', kind='daily')
    controller.begin_response_window(8, scope_id='daily:test')
    sid = controller.voice_session_id
    controller.handle_capture_ready(100, sid)
    controller.speech_worker = SimpleNamespace(isRunning=lambda: True,
        is_capturing_user_speech=lambda: True, speech_started_at=107.9, requestInterruption=lambda: None)
    now[0] = 108
    controller._expire_response_window()
    assert controller._response_finishing and controller.voice_session_id == sid
    now[0] = 110
    controller.handle_recognized(text, sid)
    assert calls.text == ([text] if text else [])
    assert controller.daily_active is bool(text)
    if text:
        key = controller.response_reply_key
        controller.arm_response_scope('alarm:new')
        controller.begin_response_window(5, scope_id='alarm:new')
        controller.finish_response_reply(False, reply_key=key)
        assert controller._response_scope_id == 'alarm:new' and controller._response_until == 115
        assert not controller.end_daily_conversation()


@pytest.mark.parametrize('text, effect', [('结束对话。', 'end'), ('先聊到这里', 'end'),
    ('不要结束对话', 'normal'), ('他说“先聊到这里”是什么意思', 'normal'),
    ('停止', 'normal'), ('完全关闭麦克风！', 'mute')])
def test_daily_control_phrases_are_exact_and_local(voice, text, effect):
    import time
    controller, calls = voice
    controller.arm_response_scope('daily:test', kind='daily')
    controller.begin_response_window(8, scope_id='daily:test')
    controller.handle_capture_ready(time.monotonic(), controller.voice_session_id)
    controller.handle_recognized(text, controller.voice_session_id)
    assert calls.text == ([text] if effect == 'normal' else [])
    assert controller.manual_muted is (effect == 'mute')
    assert controller.daily_active is (effect == 'normal')


def test_daily_preparation_failure_and_failed_audio_close_the_scope(voice):
    controller, calls = voice
    controller.arm_response_scope('daily:prepare', kind='daily')
    controller.begin_response_window(8, scope_id='daily:prepare')
    controller._expire_response_window()
    assert not controller.daily_active and '准备超时' in calls.dialogue[-1]
    sid = controller.wait_for_wake_reply()
    controller.finish_wake_reply(sid, False)
    assert not controller.daily_active and not controller.capture_ready
    assert '未完整播放' in calls.dialogue[-1]


@pytest.mark.parametrize('timeout_first', [True, False])
def test_queued_completed_utterance_is_accepted_independently_of_timeout_order(voice, monkeypatch, timeout_first):
    controller, calls = voice
    now = [100.]
    monkeypatch.setattr('ui.controllers.voice_input_controller.time.monotonic', lambda: now[0])
    controller.arm_response_scope('daily:test', kind='daily')
    controller.begin_response_window(8, scope_id='daily:test')
    sid = controller.voice_session_id
    controller.handle_capture_ready(100, sid)
    now[0] = 107.8
    controller._expire_response_window()  # an early timer cannot close the grant
    assert controller.daily_active and controller._response_until == 108
    controller.speech_worker = SimpleNamespace(isRunning=lambda: False,
        speech_started_at=107.9, is_capturing_user_speech=lambda: True)
    now[0] = 109
    if timeout_first:
        controller._expire_response_window()
    controller.handle_recognized('截止前已开始的一句', sid)
    assert calls.text == ['截止前已开始的一句'] and controller._response_pending


@pytest.mark.parametrize('text', ['已经说完的一句', ''])
def test_switch_to_short_mode_keeps_completed_utterance_queued_for_current_session(voice, text):
    controller, calls = voice
    controller.start()
    sid = controller.voice_session_id
    controller._worker_session_id = sid
    controller.speech_worker = SimpleNamespace(isRunning=lambda: False, capture_released=True,
        speech_started_at=1., is_capturing_user_speech=lambda: True)
    controller._set_capture_ready(False)  # capture_finished was delivered before recognized.
    controller.use_short_conversation()
    controller.handle_recognized(text, sid)
    assert calls.text == ([text] if text else [])
    if text:
        assert controller.voice_session_id == sid and controller._response_pending
    else:
        assert not controller.daily_active and not controller._response_preparing


def test_switch_to_short_mode_does_not_revive_an_old_workers_utterance(voice):
    controller, calls = voice
    controller.start()
    sid = controller.voice_session_id
    controller._worker_session_id = sid - 1
    controller.speech_worker = SimpleNamespace(isRunning=lambda: False, capture_released=True,
        speech_started_at=1., is_capturing_user_speech=lambda: True)
    controller.use_short_conversation()
    controller.handle_recognized('已撤销的旧录音', sid - 1)
    assert calls.text == [] and controller._response_preparing
    assert not controller._response_finishing




def test_pending_reply_uses_the_shared_random_gap_and_finishes_existing_asr(voice, monkeypatch):
    controller, calls = voice
    now = [100.]
    monkeypatch.setattr('ui.controllers.voice_input_controller.time.monotonic', lambda: now[0])
    monkeypatch.setattr(controller, 'is_capturing_user_speech', lambda: True)
    controller.begin_response_window(3, deadline=120)
    sid = controller.voice_session_id
    now[0] = 103
    controller._expire_response_window()
    assert controller.voice_session_id == sid and controller._response_finishing
    now[0] = 104
    controller.handle_recognized('再让我抱一会', sid)
    controller.set_response_reply_window(7)
    now[0] = 106
    controller.finish_response_reply()
    assert calls.text == ['再让我抱一会'] and controller._response_until == 113
    assert not controller.voice_mode_active








def test_role_change_replaces_phrases_and_invalidates_old_transcripts(voice):
    controller, calls = voice
    old_sid = controller.voice_session_id
    character = CharacterPackage(
        character_id="megumin", char_name="めぐみん", name="惠惠 / Megumin",
        nicknames=("めぐみん", "Megumin", "惠惠"),
    )
    controller.configure_wake(True, default_wake_words(character))
    controller.handle_wake_detected("Spica", old_sid)
    controller.handle_wake_detected("Spica", controller.voice_session_id)
    controller.handle_wake_detected("惠惠", controller.voice_session_id)
    assert calls.wake == ["惠惠"]


@pytest.mark.parametrize("metadata, expected", [
    ({"character_id": "spica", "char_name": "スピカ"}, ("斯皮卡", "Spica")),
    ({"character_id": "megumin", "char_name": "めぐみん", "name": "惠惠 / Megumin"}, ("惠惠", "Megumin")),
    ({"character_id": "sana", "char_name": "紗凪", "nicknames": ("紗凪", "さな", "Sana", "纱凪")}, ("紗凪", "Sana", "纱凪")),
    ({"character_id": "spica-fan", "char_name": "小星"}, ("小星",)),
    ({"character_id": "legacy", "name": "小星"}, ("小星",)),
])
def test_old_cards_use_pronounceable_role_names(metadata, expected):
    assert default_wake_words(CharacterPackage(**metadata)) == expected


def test_authored_wake_words_and_local_overrides_have_separate_ownership(tmp_path):
    path = tmp_path / "overlay.json"
    role = CharacterPackage(character_id="megumin", char_name="めぐみん", wake_words=(" 惠惠 ", "小惠", "惠惠"))
    defaults = default_wake_words(role)
    assert defaults == ("惠惠", "小惠")
    assert save_voice_wake_words("megumin", ("惠惠同学",), path)
    assert load_voice_wake_preferences("megumin", defaults, path)[1] == ("惠惠同学",)
    assert load_voice_wake_preferences("sana", ("Sana",), path)[1] == ("Sana",)
    # Explicit unsupported spellings must reach the normal detector error;
    # only names inferred from older cards are filtered.
    assert default_wake_words(role.model_copy(update={"wake_words": ("めぐみん",)})) == ("めぐみん",)


def test_passive_mic_ducks_for_reply_and_waits_for_interrupted_worker(voice):
    controller, calls = voice
    running, interrupted = [True], []
    controller.speech_worker = SimpleNamespace(
        isRunning=lambda: running[0], requestInterruption=lambda: interrupted.append(True),
        deleteLater=lambda: None,
    )
    sid = controller.voice_session_id
    ReactionVoiceDuckGate(controller).before_system_speech()
    assert interrupted == [True]
    controller.handle_recognized("Spica", sid)
    assert calls.wake == []
    controller.maybe_start_recording()  # completed reply, but old recorder still exiting
    assert calls.starts == []
    running[0] = False
    controller.handle_finished(sid)
    controller.maybe_start_recording()
    assert calls.starts == [True]


@pytest.mark.parametrize('reason', ['恢复后旧音频已过期', 'Invalid input device during read'])
def test_stalled_keyword_capture_preserves_wake_preference_and_schedules_new_listener(voice, monkeypatch, reason):
    import ui.workers.wake_word_worker as module
    from hardware.audio_input.keyword_capture import KeywordCaptureInterrupted
    controller, calls = voice
    sid = controller.voice_session_id
    def interrupted(*args):
        raise KeywordCaptureInterrupted(reason)
    monkeypatch.setattr(module, 'microphone_frames', interrupted)
    worker = module.WakeWordWorker(detector=SimpleNamespace(create_stream=lambda words: object()),
        words=('Spica',), mic_backend='respeaker')
    worker.failed.connect(lambda message: controller.handle_error(message, sid))
    worker.run()
    controller.handle_finished(sid)
    assert controller.wake_enabled and controller.voice_session_id == sid
    assert calls.timers[-1] == 650 and not calls.wake
    worker.deleteLater()


def test_wake_engine_failure_disables_listening_with_a_visible_error(voice):
    controller, calls = voice
    controller.handle_error("唤醒监听不可用：缺少模型", controller.voice_session_id)
    assert not controller.listening_enabled
    assert calls.dialogue == ["唤醒监听不可用：缺少模型"]


def test_wake_preferences_preserve_other_roles_and_settings(tmp_path):
    path = tmp_path / "overlay.json"
    path.write_text(json.dumps({"voice_wake_enabled": "false", "dialogue_opacity": 0.7}), encoding="utf-8")
    assert load_voice_wake_preferences("spica", ("Spica",), path) == (False, ("Spica",))
    assert save_voice_wake_words("spica", ("Spica", "斯皮卡"), path)
    assert save_voice_wake_words("megumin", ("惠惠",), path)
    assert save_voice_wake_enabled(True, path)
    assert load_voice_wake_preferences("spica", (), path) == (True, ("Spica", "斯皮卡"))
    assert load_voice_wake_preferences("megumin", (), path) == (True, ("惠惠",))
    assert json.loads(path.read_text())["dialogue_opacity"] == 0.7
    path.write_text("{broken", encoding="utf-8")
    assert not save_voice_wake_enabled(False, path)
    assert path.read_text() == "{broken"

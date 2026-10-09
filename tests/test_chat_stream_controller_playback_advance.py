"""Regression: streaming playback must DEFER the next segment's play() out of the
EndOfMedia callback stack.

Root cause of the 2026-06-27 freeze: ``_maybe_advance_playback`` called
``_finish_playback(pump_immediately=True)``, which synchronously started the next
``QMediaPlayer.play()`` from *inside* the previous segment's
``mediaStatusChanged(EndOfMedia)`` callback. That re-entrant call deadlocked the
Qt audio backend -- two py-spy dumps taken minutes apart froze byte-for-byte at
``audio_controller.py:79`` (``QMediaPlayer.play()``). The fix makes that advance
defer via ``QTimer.singleShot(0, _pump_stream_playback)`` like every other
pump/visual path, so the next ``play()`` runs on a clean stack.

This whole playback-advance chain had ZERO direct test coverage (which is why the
bug lived so long). The test below locks in BOTH halves of the fix:
  * the next segment is NOT started synchronously inside the finished-callback, and
  * it IS started after one event-loop tick (playback continuity holds).
"""

from __future__ import annotations

import os
from typing import Any

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from ui.models.stream import StreamKind
from ui.controllers.chat_stream_controller import ChatStreamController  # noqa: E402
from ui.models.stream_unit import StreamUnitState  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


class _FakeAudioController:
    """Records every play_chat_audio call; never actually plays. The test drives
    the finished callbacks by hand so it controls the exact timing the production
    QMediaPlayer would drive via mediaStatusChanged."""

    def __init__(self) -> None:
        self.play_calls: list[tuple[Any, Any, Any]] = []

    def release_chat_audio(self) -> None:
        pass

    def release_preloaded(self) -> None:
        pass

    def preload_chat_audio(self, index: int, audio_path: Any) -> bool:
        return False

    def play_chat_audio(self, audio_path: Any, token: Any, on_finished: Any, *, on_playback=None) -> bool:
        self.play_calls.append((audio_path, token, on_finished))
        return True


class _FakeTypewriter:
    """start() is a no-op; the test calls _mark_text_finished() by hand."""

    def start(self, text: str, on_finished: Any = None) -> None:
        pass

    def stop(self) -> None:
        pass


def _make_controller(audio: _FakeAudioController) -> ChatStreamController:
    return ChatStreamController(
        parent=None,
        agent=None,
        conversation_id_provider=lambda: "test::convo",
        visual_overrides_provider=lambda: {},
        audio_controller=audio,
        typewriter_controller=_FakeTypewriter(),
        set_character_image=lambda *_: None,
        set_busy=lambda *_: None,
        on_chat_done=lambda: None,
        on_error=lambda *_: None,
        apply_visual=lambda *_: None,
    )


def test_advance_defers_next_segment_play_out_of_finished_callback(qapp, tmp_path) -> None:
    audio = _FakeAudioController()
    controller = _make_controller(audio)

    # _play_chunk_audio only calls play_chat_audio when the file EXISTS (otherwise
    # it takes the _mark_audio_finished bypass), so back the units with real files.
    wav0 = tmp_path / "seg0.wav"
    wav1 = tmp_path / "seg1.wav"
    wav0.write_bytes(b"RIFF")
    wav1.write_bytes(b"RIFF")

    unit0 = StreamUnitState(
        index=0, display_text="seg0", audio_path=str(wav0),
        text_ready=True, audio_ready=True, visual_ready=True,
    )
    unit1 = StreamUnitState(
        index=1, display_text="seg1", audio_path=str(wav1),
        text_ready=True, audio_ready=True, visual_ready=True,
    )

    # Enter streaming mode with two pending, ready segments.
    controller.streaming_mode = True
    controller.stream_done = True  # both segments already arrived; nothing more inbound
    controller.stream_pending_units = {0: unit0, 1: unit1}
    controller.next_stream_index = 0

    # Start playback of segment 0.
    controller._pump_stream_playback()
    assert len(audio.play_calls) == 1
    assert controller.next_stream_index == 1
    assert controller.playback_active is True
    assert controller.current_unit is unit0

    # Segment 0 finishes. Text first -> no advance yet (audio not done). Then AUDIO
    # last: in production this last leg runs inside QMediaPlayer's EndOfMedia
    # callback -- the deadlock path.
    controller._mark_text_finished()
    assert len(audio.play_calls) == 1  # text-only does not advance

    controller._handle_chat_audio_finished(0)

    # *** Regression core ***: advancing must NOT have synchronously started
    # segment 1. With the old pump_immediately=True this would already be 2 (and in
    # production a re-entrant play() inside the EndOfMedia callback -> deadlock).
    assert len(audio.play_calls) == 1, "next segment started synchronously (re-entrant play deadlock risk)"
    assert controller.playback_active is False  # _finish_playback ran; pump is deferred

    # One event-loop tick runs the deferred _pump_stream_playback.
    qapp.processEvents()

    # Now segment 1 plays -- deferred, on a clean stack. Continuity holds.
    assert len(audio.play_calls) == 2
    assert controller.next_stream_index == 2
    assert controller.current_unit is unit1


def test_visual_ready_as_last_lane_starts_playback_immediately(qapp, tmp_path) -> None:
    audio = _FakeAudioController()
    controller = _make_controller(audio)
    wav = tmp_path / "visual-last.wav"
    wav.write_bytes(b"RIFF")
    unit = StreamUnitState(
        index=0,
        display_text="visual last",
        audio_path=str(wav),
        text_ready=True,
        audio_ready=True,
        visual_ready=False,
    )
    controller.streaming_mode = True
    controller.stream_pending_units = {0: unit}
    controller.next_stream_index = 0

    controller._handle_stream_unit_visual_ready(
        {
            "index": 0,
            "visual": {"expression_id": "002"},
            "cue": {"image_path": "/tmp/face001_002.png"},
        }
    )

    assert unit.visual_ready is True
    assert len(audio.play_calls) == 1
    assert controller.current_unit is unit


@pytest.mark.parametrize("first_text,with_audio", [("第一句。", False), ("第一句！」", True)])
def test_each_sentence_tail_animates_before_the_next_ready_unit(
    qapp, tmp_path, isolated_runtime_config, first_text, with_audio,
):
    import time
    from unittest.mock import patch
    from PySide6.QtTest import QTest
    from ui.qt_overlay import OverlayWindow

    with patch.object(OverlayWindow, "_init_backend", lambda self: None):
        window = OverlayWindow()
    window.resize(1000, 800)
    window.show()
    qapp.processEvents()
    audio = _FakeAudioController()
    controller = _make_controller(audio)
    controller.typewriter_controller = window.typewriter_controller
    window.typewriter_controller.set_speed(3)
    tail = window.dialogue.tail
    tail.timer.setInterval(50)  # Sana's frame interval.
    ticks = []
    tail.timer.timeout.connect(lambda: ticks.append(window.dialogue.text_label.text()))
    wav = tmp_path / "short.wav"
    wav.write_bytes(b"RIFF")
    units = [StreamUnitState(
        index=index, display_text=text, audio_path=str(wav) if with_audio else None,
        text_ready=True, audio_ready=True, visual_ready=True,
    ) for index, text in enumerate((first_text, "第二句。"))]
    controller.streaming_mode = True
    controller.stream_done = True
    controller.stream_pending_units = dict(enumerate(units))
    controller.next_stream_index = 0
    try:
        controller._pump_stream_playback()
        if with_audio:
            controller._handle_chat_audio_finished(0)  # Audio ends before typing.
        deadline = time.monotonic() + 2
        while "第二句。" not in ticks and time.monotonic() < deadline:
            QTest.qWait(10)
        assert first_text in ticks, "first sentence disappeared before any animation tick"
        assert "第二句。" in ticks
    finally:
        window.typewriter_controller.stop()
        window.close()
        window.deleteLater()
        qapp.processEvents()


@pytest.mark.parametrize("outcome", ["completed", "cancelled", "failed"])
def test_local_presentation_has_one_terminal_receipt_and_shutdown_drains(qapp, outcome):
    from types import SimpleNamespace
    calls = []
    controller = _make_controller(_FakeAudioController())
    controller.agent = SimpleNamespace(record_presentation=lambda turn_id, **result: calls.append((turn_id, result)))
    controller._presentation_turn_id = "turn-1"
    controller._text_presented = True
    if outcome == "cancelled":
        controller.stop_current()
        # A late playback callback cannot relabel this stop as completion.
        controller._end_stream_playback()
    elif outcome == "failed":
        controller._presentation_failed = True
        controller._end_stream_playback()
    else:
        controller._end_stream_playback()
    assert controller.shutdown(wait_ms=1000)
    assert calls == [("turn-1", {"outcome": outcome})]


def test_audio_failure_is_not_reported_as_successful_presentation(qapp, tmp_path):
    class FailedAudio(_FakeAudioController):
        def play_chat_audio(self, path, token, on_finished, *, on_playback=None):
            on_playback("failed", 0.0)
            return False
    controller = _make_controller(FailedAudio())
    path = tmp_path / "unit.wav"
    path.write_bytes(b"RIFF")
    unit = StreamUnitState(index=0, display_text="本句", audio_path=str(path))
    controller.current_unit = unit
    controller._play_chunk_audio(unit)
    assert controller._presentation_failed


def test_silent_reading_pages_hold_before_advancing_and_authorizing_response(qapp, monkeypatch):
    from ui.controllers.typewriter_controller import TypewriterController
    shown, results, retained = [], [], []
    writer = TypewriterController(None, shown.append)
    controller = _make_controller(_FakeAudioController())
    controller.typewriter_controller = writer
    controller._next_stream_token(StreamKind.SYSTEM)
    controller._reset_playback_state(streaming=True)
    monkeypatch.setattr(controller, '_active_stream_signal_token', lambda: controller.active_stream_token)
    controller._reply_audio_enabled = False
    controller.notify_on_current_reply_result(results.append)
    controller.reply_text_complete.connect(lambda: retained.append(True))
    first = '第一段的说明需要慢慢阅读，不能马上被后面的内容盖住。'
    second = '第二段还有一些重要内容，全部看完以后才开始接话窗口。'
    try:
        controller._handle_stream_unit_ready(dict(index=0, display_text=first + second, audio_path=None))
        controller.stream_done = True  # Backend may finish before the reader.
        assert writer.typing_timer.interval() == 34
        for _ in first[1:]:
            writer.typing_timer.timeout.emit()
        assert shown[-1] == first
        assert writer.typing_timer.interval() == 1000
        assert not results and controller.is_busy()
        writer.typing_timer.timeout.emit()
        assert shown[-1] == second[0]
        for _ in second[1:]:
            writer.typing_timer.timeout.emit()
        assert shown[-1] == second and not results
        assert writer.typing_timer.interval() == 1000
        writer.typing_timer.timeout.emit()
        qapp.processEvents()
        assert len(results) == 1 and results[0].allows_response
        assert retained == [True]
        assert not controller.audio_controller.play_calls
    finally:
        controller.stop_current()
        writer.deleteLater()


@pytest.mark.parametrize('voiced', [False, True])
def test_reply_completion_keeps_last_caption_without_replaying_the_full_reply(qapp, monkeypatch, tmp_path, voiced):
    from ui.controllers.dialogue_visibility_controller import DialogueVisibilityController
    from ui.controllers.typewriter_controller import TypewriterController
    from ui.widgets.dialogue_box import TintedDialogueBox
    dialogue = TintedDialogueBox()
    visibility = DialogueVisibilityController(dialogue, user_hidden=False)
    writer = TypewriterController(dialogue, visibility.show_dialogue_line)
    controller = _make_controller(_FakeAudioController())
    controller.typewriter_controller = writer
    controller.reply_text_complete.connect(visibility.retain_reply)
    controller._next_stream_token(StreamKind.CHAT)
    controller._reset_playback_state(streaming=True)
    monkeypatch.setattr(controller, '_active_stream_signal_token', lambda: controller.active_stream_token)
    controller._reply_audio_enabled = voiced
    audio = tmp_path / 'voice.wav'
    audio.touch()
    captions = ['这句已经看完了。', '最后只留下这一句。']
    try:
        for index, caption in enumerate(captions):
            controller._handle_stream_unit_ready(dict(index=index, display_text=caption,
                audio_path=str(audio) if voiced else None))
        controller.stream_done = True
        for index, caption in enumerate(captions):
            if voiced:
                controller._handle_chat_audio_finished(index)
            for _ in caption:
                writer.typing_timer.timeout.emit()
            qapp.processEvents()
        assert not controller.is_busy()
        assert dialogue.text_label.text() == captions[-1]
        assert visibility._dialogue_line == captions[-1]
    finally:
        controller.stop_current()
        dialogue.deleteLater()
        visibility.deleteLater()


def test_reading_pause_and_old_timer_cannot_escape_stop_or_new_reply(qapp):
    from ui.controllers.typewriter_controller import TypewriterController
    shown, finished = [], []
    writer = TypewriterController(None, shown.append)
    try:
        writer.start('好。', reading=True, on_finished=lambda: finished.append('old'))
        old_timer = writer.typing_timer
        old_timer.timeout.emit()
        assert shown[-1] == '好。' and old_timer.interval() == 1000
        writer.stop()
        old_timer.timeout.emit()
        assert not writer.is_active() and not finished
        writer.start('新的回复。')
        assert shown[-1] == '新' and writer.typing_timer.interval() < 100
        old_timer.timeout.emit()
        assert shown[-1] == '新' and not finished
    finally:
        writer.stop()
        writer.deleteLater()


@pytest.mark.parametrize('text', ['一段没有标点的长回复' * 12, '\n'.join(f'第{i}项内容' for i in range(10))])
def test_reading_pages_keep_all_text_and_fit_short_paragraphs(qapp, text):
    from ui.controllers.typewriter_controller import TypewriterController
    shown, pages = [''], []
    def show(value):
        if len(value) < len(shown[-1]):
            pages.append(shown[-1])
        shown.append(value)
    writer = TypewriterController(None, show)
    try:
        writer.start(text, reading=True, on_finished=lambda: pages.append(shown[-1]))
        for _ in text:
            writer.typing_timer.timeout.emit()
        assert not writer.is_active()
        assert ''.join(pages) == text
        assert len(pages) > 1
        assert all(len(page) <= 48 and page.count('\n') <= 2 for page in pages)
    finally:
        writer.stop()
        writer.deleteLater()


def test_continuous_audio_changes_sentence_visuals_from_player_position_and_cancels(qapp, tmp_path):
    audio = _FakeAudioController()
    path = tmp_path / "whole.wav"
    path.write_bytes(b"RIFF")
    position = [0]
    audio.voice_playback_position = lambda: (path, position[0])
    controller = _make_controller(audio)
    shown, cues, callbacks = [], [], []
    controller.typewriter_controller.start = lambda text, **kwargs: (shown.append(text), callbacks.append(kwargs['on_finished']))
    controller.apply_visual = lambda visual: cues.append(visual['intent'])
    controller.streaming_mode = True
    controller._handle_stream_unit_ready({
        "index": 0, "display_text": "谢谢。爆裂魔法！我很担心。", "audio_path": str(path),
        "visual": {"intent": "thanks"},
        "speech_segments": [
            {"display_text": text, "visual": {"intent": intent}, "start_ms": start, "end_ms": start + 1000}
            for text, intent, start in [("谢谢。", "thanks", 0), ("爆裂魔法！", "explosion", 1000), ("我很担心。", "worry", 2000)]
        ],
    })
    assert shown == ["谢谢。"] and cues == ["thanks"]
    assert len(audio.play_calls) == 1
    position[0] = 1100
    controller._update_speech_presentation()
    assert shown[-1] == "爆裂魔法！" and cues[-1] == "explosion"
    callbacks[0]()  # A late previous-sentence completion must not finish this one.
    assert not controller.current_text_finished
    position[0] = 2100
    controller._update_speech_presentation()
    assert shown[-1] == "我很担心。" and cues[-1] == "worry"
    assert len(audio.play_calls) == 1  # No split, seek, or replay between sentences.
    controller.stop_current()
    position[0] = 3000
    controller._update_speech_presentation()
    callbacks[-1]()
    assert cues == ["thanks", "explosion", "worry"]
    assert not controller.playback_active


def test_failed_continuous_audio_still_presents_all_sentences(qapp):
    audio = _FakeAudioController()
    controller = _make_controller(audio)
    shown, callbacks = [], []
    controller.typewriter_controller.start = lambda text, **kwargs: (shown.append(text), callbacks.append(kwargs['on_finished']))
    controller.streaming_mode = True
    controller.stream_done = True
    controller._handle_stream_unit_ready({
        "index": 0, "display_text": "第一句。第二句。", "audio_error": "synthesis failed",
        "speech_segments": [{"display_text": text, "visual": {}, "start_ms": None, "end_ms": None}
                            for text in ["第一句。", "第二句。"]],
    })
    assert shown == ["第一句。"]
    callbacks[0]()
    qapp.processEvents()
    assert shown == ["第一句。", "第二句。"]
    callbacks[1]()
    qapp.processEvents()
    assert not controller.playback_active and not controller.streaming_mode
    assert audio.play_calls == []


def test_timed_long_subtitle_finishes_with_a_visible_tail_pause(qapp):
    import time
    from PySide6.QtTest import QTest
    from ui.controllers.typewriter_controller import TypewriterController

    shown, revealed, completed = [], [], []
    writer = TypewriterController(None, shown.append)
    writer.revealed.connect(lambda: revealed.append(time.monotonic()))
    writer.completed.connect(lambda: completed.append(time.monotonic()))
    text = "这句翻译比实际语音长，仍须完整显示。" * 5
    began = time.monotonic()
    try:
        writer.start(text, max_duration_ms=700)
        while not completed and time.monotonic() - began < 1:
            QTest.qWait(1)
        assert shown[-1] == text
        assert completed and completed[0] - began < 1
        assert completed[0] - revealed[0] >= .1
        # An ordinary next line regains the user's normal typing speed.
        shown.clear()
        writer.start("普通字幕。")
        QTest.qWait(10)
        assert shown == ["", "普"]
    finally:
        writer.stop()

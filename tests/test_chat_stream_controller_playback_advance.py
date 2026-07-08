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

    def play_chat_audio(self, audio_path: Any, token: Any, on_finished: Any) -> bool:
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
        text_ready=True, audio_ready=True, visual_ready=False,
    )
    unit1 = StreamUnitState(
        index=1, display_text="seg1", audio_path=str(wav1),
        text_ready=True, audio_ready=True, visual_ready=False,
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

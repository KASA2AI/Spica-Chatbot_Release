"""SongController (post-B2): playback lifecycle + the control fast path ONLY.

B2 (P2) tool-ised singing into the main LLM's ``sing_song`` function call. What
this controller no longer does: route intents (SongIntentRouter died), run a
confirmation FSM (INTENT_CONFIRMING died -- "which song?" is normal
conversation now), start a synthetic-prompt prelude turn (the tool turn's own
answer IS the acknowledgment), or speak canned lines through the chat bubble
(F14 -- conversational speech only ever comes from run_turn; control feedback
goes to a UI status chip via ``set_song_status``).

What remains:
- ``handle_song_request_event``: a SongRequestEvent (host sing_song closure ->
  bridge) starts the SongWorker; playback is gated on the turn's own
  acknowledgment speech finishing (``notify_on_current_stream_done``) -- the
  AudioOwner arbitration: a READY song never talks over her.
- ``try_handle_control_text``: pause/resume/cancel/restart verbs while a song
  flow is LIVE (a main-LLM round trip per "暂停" would be a UX regression).
  Everything else goes to normal chat.
- worker lifecycle, playback gate, AudioOwner token discipline -- unchanged.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QObject

from agent_tools.function_tools.song import (
    SongAction,
    SongRequest,
    SongState,
    parse_song_control_intent,
)
from spica.core.proactive import ProactiveTurnRequest
from ui.controllers.audio_controller import AudioController
from ui.controllers.chat_stream_controller import ChatStreamController
from ui.models.playback import AudioOwner, AudioToken
from ui.models.song_ui import SongPlaybackGate, SongUiState
from ui.workers.song_worker import SongWorker

logger = logging.getLogger(__name__)

_FAST_PATH_ACTIONS = {
    SongAction.PAUSE,
    SongAction.RESUME,
    SongAction.CANCEL,
    SongAction.RESTART,
}


class SongController(QObject):
    def __init__(
        self,
        parent: QObject,
        chat_stream_controller: ChatStreamController | None,
        audio_controller: AudioController,
        set_song_status: Callable[[str], None],
        set_busy: Callable[[bool], None],
        focus_input: Callable[[], None],
        stop_conversation_for_song: Callable[[], None],
        voice_mode_active_provider: Callable[[], bool],
        schedule_voice_recording: Callable[[int], None],
        request_proactive_turn: Callable[[ProactiveTurnRequest], Any] | None = None,
    ) -> None:
        super().__init__(parent)
        self.chat_stream_controller = chat_stream_controller
        self.audio_controller = audio_controller
        self.set_song_status = set_song_status
        self.set_busy = set_busy
        self.focus_input = focus_input
        self.stop_conversation_for_song = stop_conversation_for_song
        self.voice_mode_active_provider = voice_mode_active_provider
        self.schedule_voice_recording = schedule_voice_recording
        self.request_proactive_turn = request_proactive_turn

        self.ui_state = SongUiState()
        self.song_worker: SongWorker | None = None
        self.retired_song_workers: list[SongWorker] = []
        self.audio_session_id = 0

    # -- logging ---------------------------------------------------------------
    def _now_ms(self) -> float:
        return round(time.perf_counter() * 1000.0, 2)

    def _log_value(self, value: Any) -> str:
        if isinstance(value, str):
            return repr(value)
        return str(value)

    def _log_song_event(
        self,
        event: str,
        *,
        job_id: int | None = None,
        session_id: int | None = None,
        **fields: Any,
    ) -> None:
        if session_id is None:
            session_id = self.ui_state.session_id
        field_parts = " ".join(f"{key}={self._log_value(value)}" for key, value in fields.items())
        suffix = f" {field_parts}" if field_parts else ""
        logger.debug(
            "event=%s session_id=%s job_id=%s state=%s monotonic_ms=%s%s",
            event,
            session_id,
            job_id,
            self.ui_state.state.value,
            self._now_ms(),
            suffix,
        )

    # -- playback gate -----------------------------------------------------------
    def _reset_playback_gate_for_request(self, *, prelude_required: bool, reason: str) -> None:
        self.ui_state.playback_gate = SongPlaybackGate(
            prelude_done=not prelude_required,
            clear_throat_done=not prelude_required,
            song_ready=False,
            user_paused=False,
        )
        self._log_playback_gate(reason=reason)

    def _reset_playback_gate_to_idle(self, *, reason: str) -> None:
        self.ui_state.playback_gate = SongPlaybackGate()
        self._log_playback_gate(reason=reason)

    def _update_playback_gate(
        self,
        *,
        reason: str,
        prelude_done: bool | None = None,
        clear_throat_done: bool | None = None,
        song_ready: bool | None = None,
        user_paused: bool | None = None,
    ) -> None:
        gate = self.ui_state.playback_gate
        if prelude_done is not None:
            gate.prelude_done = prelude_done
        if clear_throat_done is not None:
            gate.clear_throat_done = clear_throat_done
        if song_ready is not None:
            gate.song_ready = song_ready
        if user_paused is not None:
            gate.user_paused = user_paused
        self._log_playback_gate(reason=reason)

    def _log_playback_gate(self, *, reason: str) -> None:
        gate = self.ui_state.playback_gate
        logger.debug(
            "event=song_playback_gate_snapshot session_id=%s state=%s reason=%s "
            "prelude_done=%s clear_throat_done=%s song_ready=%s user_paused=%s can_play=%s "
            "monotonic_ms=%s",
            self.ui_state.session_id,
            self.ui_state.state.value,
            reason,
            gate.prelude_done,
            gate.clear_throat_done,
            gate.song_ready,
            gate.user_paused,
            gate.can_play,
            self._now_ms(),
        )

    def _maybe_play_ready_song(self, *, reason: str) -> bool:
        gate = self.ui_state.playback_gate
        if self.ui_state.state not in {SongState.PREPARING, SongState.READY}:
            return False

        audio_path = self.ui_state.pending_audio_path or self.ui_state.context.pending_audio_path
        if not audio_path:
            return False

        if not gate.song_ready:
            self._update_playback_gate(reason=reason, song_ready=True)
            gate = self.ui_state.playback_gate

        if not gate.can_play:
            if self.ui_state.state == SongState.PREPARING:
                self._set_state(SongState.READY)
            if gate.user_paused:
                self.set_song_status("🎤 已就绪——说「继续」开始")
                self.focus_input()
            self._log_playback_gate(reason=f"{reason}_blocked")
            return False

        self._set_state(SongState.PLAYING)
        self.set_busy(False)
        self.set_song_status("🎵 唱歌中")
        self._play_song_audio(audio_path, self.ui_state.session_id)
        self._log_playback_gate(reason=f"{reason}_play")
        return True

    # -- wiring ------------------------------------------------------------------
    def set_chat_stream_controller(self, chat_stream_controller: ChatStreamController | None) -> None:
        self.chat_stream_controller = chat_stream_controller

    def set_stop_conversation_for_song(self, stop_conversation_for_song: Callable[[], None]) -> None:
        self.stop_conversation_for_song = stop_conversation_for_song

    def is_busy(self) -> bool:
        return self.ui_state.is_busy

    # -- B2 control fast path ------------------------------------------------------
    def try_handle_control_text(self, text: str) -> bool:
        """Control verbs while a song flow is LIVE -- the only pre-chat rule that
        survived B2. Returns True when the message was consumed. Anything below
        the 0.9 bar (or outside an active flow) falls through to normal chat."""
        if not self.is_busy():
            return False
        intent = parse_song_control_intent(text, self.ui_state.state)
        if intent.action not in _FAST_PATH_ACTIONS or intent.confidence < 0.9:
            return False
        self._log_song_event("song_control_fast_path", action=intent.action.value, text=text)
        if intent.action == SongAction.PAUSE:
            self.pause()
        elif intent.action == SongAction.RESUME:
            self.resume()
        elif intent.action == SongAction.CANCEL:
            self.cancel()
        elif intent.action == SongAction.RESTART:
            self.restart()
        return True

    # -- B2 song request (sing_song tool -> host closure -> event -> here) ---------
    def handle_song_request_event(self, query: str, title: str = "", artist: str = "") -> None:
        """Start preparing the requested song. Playback is gated on the CURRENT
        turn's acknowledgment speech finishing (the AudioOwner arbitration): the
        sing_song tool fired mid-turn, her spoken reply follows, and a READY song
        waits for it -- ``notify_on_current_stream_done`` flips the prelude gate.
        A stopped/aborted turn also counts as done, so the gate cannot deadlock."""
        if self.is_busy():
            self.cancel(clear_status=False)
        request = SongRequest(
            query=query, title=title or None, artist=artist or None, user_text=query
        )
        self.start_song_request(request, prelude_required=True)
        if self.chat_stream_controller is not None:
            self.chat_stream_controller.notify_on_current_stream_done(self._finish_song_prelude)
        else:
            self._finish_song_prelude()

    def start_song_request(self, request: SongRequest, *, prelude_required: bool = False) -> None:
        self._prune_retired_song_workers()
        self.ui_state.session_id += 1
        job_id = self.ui_state.session_id
        self._log_song_event(
            "song_request_start",
            job_id=job_id,
            prelude_required=prelude_required,
            query=request.search_keyword(),
        )
        self._set_state(SongState.PREPARING)
        self.ui_state.context.pending_request = request
        self.ui_state.context.pending_audio_path = None
        self.ui_state.pending_audio_path = None
        self._reset_playback_gate_for_request(
            prelude_required=prelude_required, reason="song_request_start"
        )
        self.set_busy(False)
        self.set_song_status("🎤 准备唱歌中…")
        self.song_worker = SongWorker(request, job_id, self)
        self.song_worker.progress.connect(self.handle_song_progress)
        self.song_worker.completed.connect(self.handle_song_ready)
        self.song_worker.failed.connect(self.handle_song_error)
        self.song_worker.finished.connect(lambda jid=job_id: self._handle_song_worker_finished(jid))
        self._log_song_event("song_worker_start", job_id=job_id, query=request.search_keyword())
        self.song_worker.start()

    def _finish_song_prelude(self) -> None:
        """The turn's acknowledgment finished speaking (or was aborted): open the
        prelude gate. The dead clear-throat canned line collapsed into this."""
        if self.ui_state.playback_gate.prelude_done:
            return
        self._update_playback_gate(
            reason="prelude_done", prelude_done=True, clear_throat_done=True
        )
        self._log_song_event("song_prelude_done")
        if self.ui_state.state not in {SongState.PREPARING, SongState.READY}:
            return
        if self.ui_state.playback_gate.user_paused:
            self.set_song_status("🎤 已就绪——说「继续」开始")
            return
        self._maybe_play_ready_song(reason="prelude_done")

    # -- control actions (UI status feedback only -- never fake speech, F14) -------
    def cancel(self, *, clear_status: bool = True) -> None:
        had_song = self.is_busy()
        self._log_song_event("song_cancel", had_song=had_song)
        self.ui_state.session_id += 1
        self._retire_song_worker(cancel=True)
        self.audio_controller.stop_song()
        self._set_state(SongState.IDLE)
        self.ui_state.context.pending_request = None
        self.ui_state.context.pending_audio_path = None
        self.ui_state.pending_audio_path = None
        self._reset_playback_gate_to_idle(reason="cancel")
        self.set_busy(False)
        if clear_status:
            self.set_song_status("")
        if had_song and self.voice_mode_active_provider():
            self.schedule_voice_recording(500)

    def pause(self) -> None:
        if self.ui_state.state == SongState.PLAYING:
            if not self.audio_controller.pause_song():
                return
            self._set_state(SongState.PAUSED)
            self.set_busy(False)
            self.set_song_status("⏸ 已暂停——说「继续」接着唱")
            self.focus_input()
            return
        if self.ui_state.state == SongState.PREPARING:
            self._update_playback_gate(reason="pause_preparing", user_paused=True)
            self.set_song_status("🎤 准备中（暂停待命）——说「继续」播放")
            self.focus_input()

    def resume(self) -> None:
        if self.ui_state.state == SongState.PAUSED:
            if not self.audio_controller.resume_song():
                return
            self._set_state(SongState.PLAYING)
            self.set_busy(False)
            self.set_song_status("🎵 唱歌中")
            return
        if self.ui_state.state == SongState.READY:
            audio_path = self.ui_state.pending_audio_path or self.ui_state.context.pending_audio_path
            if not audio_path:
                return
            self._update_playback_gate(
                reason="resume_ready",
                prelude_done=True,
                clear_throat_done=True,
                song_ready=True,
                user_paused=False,
            )
            self._maybe_play_ready_song(reason="resume_ready")

    def restart(self) -> None:
        request = self.ui_state.context.pending_request or self.ui_state.context.last_request
        if request is None:
            return
        self.cancel(clear_status=False)
        self.start_song_request(request)

    # -- worker callbacks ----------------------------------------------------------
    def handle_song_ready(self, job_id: int, payload: dict[str, Any]) -> None:
        if job_id != self.ui_state.session_id:
            return
        if not bool(payload.get("ok")):
            self.handle_song_error(job_id, str(payload.get("error") or "唱歌任务失败。"))
            return
        audio_path = payload.get("final_audio_path")
        if not audio_path:
            self.handle_song_error(job_id, "唱歌任务没有返回音频文件。")
            return
        self._log_song_event("song_ready", job_id=job_id, audio_path=audio_path)
        self.ui_state.pending_audio_path = str(audio_path)
        self.ui_state.context.pending_audio_path = str(audio_path)
        self._update_playback_gate(reason="song_ready", song_ready=True)
        self.set_busy(False)
        self._maybe_play_ready_song(reason="song_ready")

    def handle_song_progress(self, job_id: int, stage: str, payload: dict[str, Any]) -> None:
        if job_id != self.ui_state.session_id:
            return
        self._log_song_event("song_pipeline_progress", job_id=job_id, stage=stage, payload=payload)

    def handle_song_error(self, job_id: int, message: str) -> None:
        if job_id != self.ui_state.session_id:
            return
        logger.warning("song job failed: %s", message)
        self._set_state(SongState.ERROR)
        self.ui_state.context.pending_request = None
        self.ui_state.context.pending_audio_path = None
        self.ui_state.pending_audio_path = None
        self.audio_controller.stop_song()
        self._reset_playback_gate_to_idle(reason="song_error")
        self.set_busy(False)
        self.set_song_status("⚠ 唱歌失败")
        if self.voice_mode_active_provider():
            self.schedule_voice_recording(900)

    def finish_song_playback(self) -> None:
        self._log_song_event("song_play_end")
        self.audio_controller.stop_song()
        self.ui_state.context.last_request = self.ui_state.context.pending_request
        self.ui_state.context.last_audio_path = self.ui_state.context.pending_audio_path
        self.ui_state.context.pending_request = None
        self.ui_state.context.pending_audio_path = None
        self.ui_state.pending_audio_path = None
        self._set_state(SongState.IDLE)
        self._reset_playback_gate_to_idle(reason="song_finished")
        self.set_busy(False)
        self.set_song_status("")
        if self.voice_mode_active_provider():
            self.schedule_voice_recording(500)
        else:
            self.focus_input()
        # P3 first use case: the proactive finish report. The song domain only
        # AUTHORS the directive text -- timing/policy live in the mode-agnostic
        # arbiter (drop_if_busy: a report is disposable). run_turn speaks it in
        # character; no canned lines.
        last_request = self.ui_state.context.last_request
        if self.request_proactive_turn is not None and last_request is not None:
            title = last_request.title or last_request.query
            artist = f"（{last_request.artist}）" if last_request.artist else ""
            self.request_proactive_turn(ProactiveTurnRequest(
                source="song",
                directive=f"你刚刚唱完了《{title}》{artist}。",
                policy="drop_if_busy",
            ))

    def shutdown(self, wait_ms: int = 1500) -> None:
        self.cancel()
        workers = [worker for worker in self.retired_song_workers if worker is not None]
        if self.song_worker is not None:
            workers.append(self.song_worker)
            self.song_worker = None
        self.retired_song_workers = []
        for worker in workers:
            if worker.isRunning():
                worker.cancel()
                worker.wait(wait_ms)
            try:
                worker.deleteLater()
            except Exception:
                pass

    # -- audio plumbing --------------------------------------------------------------
    def _play_song_audio(self, audio_path: Any, job_id: int) -> None:
        token = self._next_audio_token()
        self._log_song_event("song_play_start", job_id=job_id, token_id=token.id, audio_path=audio_path)
        self.audio_controller.play_song(
            audio_path,
            token,
            on_finished=lambda jid=job_id: self._handle_song_audio_finished(jid),
            on_error=lambda message, jid=job_id: self._handle_song_audio_error(jid, message),
        )

    def _handle_song_audio_finished(self, job_id: int) -> None:
        if job_id != self.ui_state.session_id or not self.ui_state.is_playback_active:
            return
        self.finish_song_playback()

    def _handle_song_audio_error(self, job_id: int, message: str) -> None:
        if job_id != self.ui_state.session_id:
            return
        self.handle_song_error(job_id, message)

    def _handle_song_worker_finished(self, job_id: int) -> None:
        del job_id
        worker = self.sender()
        if worker is self.song_worker:
            self.song_worker = None
        if worker in self.retired_song_workers:
            self.retired_song_workers.remove(worker)
        if worker is not None:
            try:
                worker.deleteLater()
            except Exception:
                pass

    def _retire_song_worker(self, *, cancel: bool) -> SongWorker | None:
        worker = self.song_worker
        if worker is None:
            return None
        if cancel and worker.isRunning():
            worker.cancel()
        self.song_worker = None
        if worker.isRunning() and worker not in self.retired_song_workers:
            self.retired_song_workers.append(worker)
        elif not worker.isRunning():
            try:
                worker.deleteLater()
            except Exception:
                pass
        return worker

    def _prune_retired_song_workers(self) -> None:
        active_workers: list[SongWorker] = []
        for worker in self.retired_song_workers:
            if worker.isRunning():
                active_workers.append(worker)
                continue
            try:
                worker.deleteLater()
            except Exception:
                pass
        self.retired_song_workers = active_workers

    def _next_audio_token(self) -> AudioToken:
        self.audio_session_id += 1
        return AudioToken(id=self.audio_session_id, owner=AudioOwner.SONG)

    def _set_state(self, state: SongState) -> None:
        self.ui_state.state = state
        self.ui_state.context.state = state

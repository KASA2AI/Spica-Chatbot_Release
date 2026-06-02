from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QObject, QTimer

from agent_tools.function_tools.song import (
    SongAction,
    SongIntent,
    SongIntentRouter,
    SongRequest,
    SongState,
    build_song_request_from_intent,
    clear_pending_song_hint,
    update_pending_song_hint_from_intent,
)
from ui.controllers.audio_controller import AudioController
from ui.controllers.chat_stream_controller import ChatStreamController
from ui.controllers.typewriter_controller import TypewriterController
from ui.models.playback import AudioOwner, AudioToken
from ui.models.song_ui import SongUiState
from ui.workers.song_worker import SongWorker

logger = logging.getLogger(__name__)


class SongController(QObject):
    def __init__(
        self,
        parent: QObject,
        chat_stream_controller: ChatStreamController | None,
        audio_controller: AudioController,
        typewriter_controller: TypewriterController,
        visual_overrides_provider: Callable[[], dict[str, Any]],
        set_busy: Callable[[bool], None],
        focus_input: Callable[[], None],
        stop_conversation_for_song: Callable[[], None],
        voice_mode_active_provider: Callable[[], bool],
        schedule_voice_recording: Callable[[int], None],
    ) -> None:
        super().__init__(parent)
        self.chat_stream_controller = chat_stream_controller
        self.audio_controller = audio_controller
        self.typewriter_controller = typewriter_controller
        self.visual_overrides_provider = visual_overrides_provider
        self.set_busy = set_busy
        self.focus_input = focus_input
        self.stop_conversation_for_song = stop_conversation_for_song
        self.voice_mode_active_provider = voice_mode_active_provider
        self.schedule_voice_recording = schedule_voice_recording

        self.ui_state = SongUiState()
        self.router = SongIntentRouter()
        self.song_worker: SongWorker | None = None
        self.retired_song_workers: list[SongWorker] = []
        self.audio_session_id = 0

    def set_chat_stream_controller(self, chat_stream_controller: ChatStreamController | None) -> None:
        self.chat_stream_controller = chat_stream_controller

    def set_stop_conversation_for_song(self, stop_conversation_for_song: Callable[[], None]) -> None:
        self.stop_conversation_for_song = stop_conversation_for_song

    def route_text(self, text: str) -> SongIntent:
        return self.router.route(text, self.ui_state.state, self.ui_state.context)

    def is_actionable_intent(self, intent: SongIntent) -> bool:
        return intent.action not in {SongAction.NONE, SongAction.REJECT}

    def is_intent_confirming(self) -> bool:
        return self.ui_state.state == SongState.INTENT_CONFIRMING

    def exit_intent_confirmation(self) -> None:
        self._clear_pending_song_hint()
        self._set_state(SongState.IDLE)

    def is_busy(self) -> bool:
        return self.ui_state.is_busy

    def handle_intent(self, intent: SongIntent) -> bool:
        if intent.action == SongAction.SING:
            request = build_song_request_from_intent(intent)
            if request is None:
                self.typewriter_controller.start("想听哪一首？可以说歌名。", interval_ms=45)
                return True
            if self.ui_state.state in {SongState.PREPARING, SongState.READY, SongState.PLAYING, SongState.PAUSED}:
                self.cancel(show_message=False)
            self._clear_pending_song_hint()
            self.start_song_request_with_prelude(request)
            return True

        if intent.action == SongAction.SEARCH:
            self._set_state(SongState.INTENT_CONFIRMING)
            update_pending_song_hint_from_intent(self.ui_state.context, intent)
            self.typewriter_controller.start("想听哪一首？可以说歌名，或者说‘周杰伦的稻香’。", interval_ms=45)
            self.focus_input()
            return True

        if intent.action == SongAction.PAUSE:
            self.pause()
            return True

        if intent.action == SongAction.RESUME:
            self.resume()
            return True

        if intent.action == SongAction.CANCEL:
            self.cancel(show_message=True)
            return True

        if intent.action == SongAction.CHANGE:
            self.change(intent)
            return True

        if intent.action == SongAction.RESTART:
            self.restart()
            return True

        return False

    def start_song_request_with_prelude(self, request: SongRequest) -> None:
        if self.chat_stream_controller is None:
            self.start_song_request(request, auto_play=True, show_message=True)
            return

        self.stop_conversation_for_song()
        self.ui_state.prelude_active = True
        self.ui_state.user_paused_preparing = False
        self.start_song_request(request, auto_play=False, show_message=False, stop_conversation=False)
        self._start_song_prelude_chat(request)

    def start_song_request(
        self,
        request: SongRequest,
        auto_play: bool = True,
        show_message: bool = True,
        *,
        stop_conversation: bool = True,
    ) -> None:
        if stop_conversation:
            self.stop_conversation_for_song()
        self._prune_retired_song_workers()
        self.ui_state.session_id += 1
        job_id = self.ui_state.session_id
        logger.debug(
            "event=song_request_start job_id=%s auto_play=%s show_message=%s stop_conversation=%s query=%r",
            job_id,
            auto_play,
            show_message,
            stop_conversation,
            request.search_keyword(),
        )
        self._set_state(SongState.PREPARING)
        self._clear_pending_song_hint()
        self._set_auto_play(auto_play)
        self.ui_state.clear_throat_active = False
        self.ui_state.user_paused_preparing = False
        self.ui_state.context.pending_request = request
        self.ui_state.context.pending_audio_path = None
        self.ui_state.pending_audio_path = None
        self.set_busy(False)
        if show_message:
            self.typewriter_controller.start("Spica 正在清嗓", interval_ms=70)
        self.focus_input()

        self.song_worker = SongWorker(request, job_id, self)
        self.song_worker.progress.connect(self.handle_song_progress)
        self.song_worker.completed.connect(self.handle_song_ready)
        self.song_worker.failed.connect(self.handle_song_error)
        self.song_worker.finished.connect(lambda jid=job_id: self._handle_song_worker_finished(jid))
        logger.debug("event=song_worker_start job_id=%s query=%r", job_id, request.search_keyword())
        self.song_worker.start()

    def cancel(self, show_message: bool = True) -> None:
        had_song = self.is_busy()
        logger.debug(
            "event=song_cancel session_id=%s state=%s show_message=%s had_song=%s",
            self.ui_state.session_id,
            self.ui_state.state.value,
            show_message,
            had_song,
        )
        self.ui_state.session_id += 1
        if self.ui_state.prelude_active:
            self._stop_song_prelude()
        self._retire_song_worker(cancel=True)
        self.audio_controller.stop_song()
        self._set_state(SongState.IDLE)
        self._clear_pending_song_hint()
        self._set_auto_play(True)
        self.ui_state.clear_throat_active = False
        self.ui_state.user_paused_preparing = False
        self.ui_state.context.pending_request = None
        self.ui_state.context.pending_audio_path = None
        self.ui_state.pending_audio_path = None
        self.set_busy(False)
        if show_message and had_song:
            self.typewriter_controller.start("好，先不唱了。", interval_ms=45)
            if self.voice_mode_active_provider():
                self.schedule_voice_recording(500)

    def pause(self) -> None:
        if self.ui_state.state == SongState.PLAYING:
            if not self.audio_controller.pause_song():
                self.typewriter_controller.start("现在没有正在播放的歌曲。", interval_ms=45)
                return
            self._set_state(SongState.PAUSED)
            self.set_busy(False)
            self.typewriter_controller.start("先暂停。", interval_ms=45)
            self.focus_input()
            return

        if self.ui_state.state == SongState.PREPARING:
            self._set_auto_play(False)
            self.ui_state.user_paused_preparing = True
            self.typewriter_controller.start("好，准备好后先不播放。说继续我再唱。", interval_ms=45)
            self.focus_input()
            return

        self.typewriter_controller.start("现在没有正在播放的歌曲。", interval_ms=45)
        self.focus_input()

    def resume(self) -> None:
        if self.ui_state.state == SongState.PAUSED:
            if not self.audio_controller.resume_song():
                self.typewriter_controller.start("现在没有可以继续的歌曲。", interval_ms=45)
                return
            self._set_state(SongState.PLAYING)
            self.set_busy(False)
            self.typewriter_controller.start("继续唱。", interval_ms=45)
            return

        if self.ui_state.state == SongState.READY:
            if self.ui_state.prelude_active:
                self._stop_song_prelude()
            audio_path = self.ui_state.pending_audio_path or self.ui_state.context.pending_audio_path
            if not audio_path:
                self.typewriter_controller.start("现在没有可以继续的歌曲。", interval_ms=45)
                return
            self._set_auto_play(True)
            self.ui_state.user_paused_preparing = False
            self.ui_state.clear_throat_active = False
            self._set_state(SongState.PLAYING)
            self.set_busy(False)
            self.typewriter_controller.start("继续唱。", interval_ms=45)
            self._play_song_audio(audio_path, self.ui_state.session_id)
            return

        self.typewriter_controller.start("现在没有可以继续的歌曲。", interval_ms=45)
        self.focus_input()

    def restart(self) -> None:
        request = self.ui_state.context.pending_request or self.ui_state.context.last_request
        if request is not None:
            self.cancel(show_message=False)
            self.start_song_request_with_prelude(request)
            return
        self.typewriter_controller.start("还没有可以重唱的歌曲。", interval_ms=45)
        self.focus_input()

    def change(self, intent: SongIntent | None = None) -> None:
        if intent is not None and (intent.query or intent.title):
            request = build_song_request_from_intent(
                SongIntent(
                    action=SongAction.SING,
                    confidence=intent.confidence,
                    query=intent.query,
                    title=intent.title,
                    artist=intent.artist,
                    original_text=intent.original_text,
                    source=intent.source,
                    reason=intent.reason,
                )
            )
            if request is not None:
                self.cancel(show_message=False)
                self.start_song_request_with_prelude(request)
                return
        self.typewriter_controller.start("想换成哪首？", interval_ms=45)
        self.focus_input()

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
        logger.debug("event=song_ready job_id=%s audio_path=%s auto_play=%s", job_id, audio_path, self.ui_state.auto_play)
        self.ui_state.pending_audio_path = str(audio_path)
        self.ui_state.context.pending_audio_path = str(audio_path)
        self.set_busy(False)
        if not self.ui_state.auto_play:
            self._set_state(SongState.READY)
            if (self.ui_state.prelude_active or self.ui_state.clear_throat_active) and not self.ui_state.user_paused_preparing:
                return
            self.typewriter_controller.start("准备好了。说继续我再唱。", interval_ms=45)
            self.focus_input()
            return
        self._set_state(SongState.PLAYING)
        self.typewriter_controller.start("唱歌中", interval_ms=70)
        self._play_song_audio(audio_path, job_id)

    def handle_song_progress(self, job_id: int, stage: str, payload: dict[str, Any]) -> None:
        if job_id != self.ui_state.session_id:
            return
        logger.debug("event=song_pipeline_progress job_id=%s stage=%s payload=%s", job_id, stage, payload)

    def handle_song_error(self, job_id: int, message: str) -> None:
        if job_id != self.ui_state.session_id:
            return
        logger.debug("event=song_error job_id=%s message=%r", job_id, message)
        self._set_state(SongState.ERROR)
        self._clear_pending_song_hint()
        self._set_auto_play(True)
        self.ui_state.prelude_active = False
        self.ui_state.clear_throat_active = False
        self.ui_state.user_paused_preparing = False
        self.ui_state.context.pending_request = None
        self.ui_state.context.pending_audio_path = None
        self.ui_state.pending_audio_path = None
        self.audio_controller.stop_song()
        self.set_busy(False)
        self.typewriter_controller.start(f"唱歌失败：{message}", interval_ms=45)
        if self.voice_mode_active_provider():
            self.schedule_voice_recording(900)

    def finish_song_playback(self) -> None:
        logger.debug("event=song_play_end session_id=%s", self.ui_state.session_id)
        self.audio_controller.stop_song()
        self.ui_state.context.last_request = self.ui_state.context.pending_request
        self.ui_state.context.last_audio_path = self.ui_state.context.pending_audio_path
        self.ui_state.context.pending_request = None
        self.ui_state.context.pending_audio_path = None
        self.ui_state.pending_audio_path = None
        self._set_state(SongState.IDLE)
        self._clear_pending_song_hint()
        self._set_auto_play(True)
        self.ui_state.prelude_active = False
        self.ui_state.clear_throat_active = False
        self.ui_state.user_paused_preparing = False
        self.set_busy(False)
        self.typewriter_controller.start("唱完了。", interval_ms=45)
        if self.voice_mode_active_provider():
            self.schedule_voice_recording(500)
        else:
            self.focus_input()

    def shutdown(self, wait_ms: int = 1500) -> None:
        self.cancel(show_message=False)
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

    def _start_song_prelude_chat(self, request: SongRequest) -> None:
        if self.chat_stream_controller is None:
            self._finish_song_prelude()
            return
        prompt = self._song_prelude_prompt(request)
        logger.debug("event=song_prelude_start session_id=%s query=%r", self.ui_state.session_id, request.search_keyword())
        self.chat_stream_controller.start_song_prelude(
            prompt,
            self.visual_overrides_provider(),
            on_done=self._finish_song_prelude,
        )

    def _song_prelude_prompt(self, request: SongRequest) -> str:
        song_name = request.search_keyword()
        return (
            f"用户想听你唱《{song_name}》。"
            "请以 Spica 的口吻，用一句很短、自然、可直接朗读的话回应，表示你要准备唱这首歌了。"
            "不要解释流程，不要提到工具、模型、下载、生成、缓存或技术细节。"
        )

    def _finish_song_prelude(self) -> None:
        if not self.ui_state.prelude_active:
            return
        self.ui_state.prelude_active = False
        logger.debug("event=song_prelude_done session_id=%s state=%s", self.ui_state.session_id, self.ui_state.state.value)

        if self.ui_state.state not in {SongState.PREPARING, SongState.READY}:
            return
        if self.ui_state.user_paused_preparing:
            if self.ui_state.state == SongState.READY:
                self.typewriter_controller.start("准备好了。说继续我再唱。", interval_ms=45)
            else:
                self.typewriter_controller.start("好，准备好后先不播放。说继续我再唱。", interval_ms=45)
            self.focus_input()
            return

        self.ui_state.clear_throat_active = True
        logger.debug("event=clear_throat_start session_id=%s", self.ui_state.session_id)
        self._set_auto_play(False)
        job_id = self.ui_state.session_id
        self.typewriter_controller.start(
            "Spica 正在清嗓",
            interval_ms=70,
            on_finished=lambda jid=job_id: QTimer.singleShot(250, lambda: self._finish_song_clear_throat(jid)),
        )

    def _finish_song_clear_throat(self, job_id: int) -> None:
        if job_id != self.ui_state.session_id:
            return
        self.ui_state.clear_throat_active = False
        logger.debug("event=clear_throat_done job_id=%s state=%s", job_id, self.ui_state.state.value)
        if self.ui_state.user_paused_preparing:
            return
        self._set_auto_play(True)
        if self.ui_state.state == SongState.READY:
            self._play_ready_song_after_prelude()

    def _play_ready_song_after_prelude(self) -> None:
        if self.ui_state.state != SongState.READY:
            return
        audio_path = self.ui_state.pending_audio_path or self.ui_state.context.pending_audio_path
        if not audio_path:
            return
        self._set_state(SongState.PLAYING)
        self.set_busy(False)
        self.typewriter_controller.start("唱歌中", interval_ms=70)
        self._play_song_audio(audio_path, self.ui_state.session_id)

    def _stop_song_prelude(self) -> None:
        self.ui_state.prelude_active = False
        self.ui_state.clear_throat_active = False
        self.ui_state.user_paused_preparing = False
        if self.chat_stream_controller is not None:
            self.chat_stream_controller.stop_current()

    def _play_song_audio(self, audio_path: Any, job_id: int) -> None:
        token = self._next_audio_token()
        logger.debug("event=song_play_start job_id=%s token_id=%s audio_path=%s", job_id, token.id, audio_path)
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

    def _set_auto_play(self, auto_play: bool) -> None:
        self.ui_state.auto_play = auto_play
        self.ui_state.context.auto_play = auto_play

    def _clear_pending_song_hint(self) -> None:
        clear_pending_song_hint(self.ui_state.context)

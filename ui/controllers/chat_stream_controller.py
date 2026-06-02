from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, QTimer

from agent import SimpleAgent
from ui.controllers.audio_controller import AudioController
from ui.controllers.typewriter_controller import TypewriterController
from ui.models.playback import AudioOwner, AudioToken
from ui.models.stream import StreamKind, StreamToken
from ui.models.stream_unit import StreamUnitState
from ui.workers.chat_worker import ChatWorker

logger = logging.getLogger(__name__)


class ChatStreamController(QObject):
    def __init__(
        self,
        parent: QObject,
        agent: SimpleAgent,
        conversation_id_provider: Callable[[], str],
        visual_overrides_provider: Callable[[], dict[str, Any]],
        audio_controller: AudioController,
        typewriter_controller: TypewriterController,
        set_character_image: Callable[[Any], None],
        set_busy: Callable[[bool], None],
        on_chat_done: Callable[[], None],
        on_error: Callable[[str], None],
        apply_visual: Callable[[dict[str, Any]], None],
    ) -> None:
        super().__init__(parent)
        self.agent = agent
        self.conversation_id_provider = conversation_id_provider
        self.visual_overrides_provider = visual_overrides_provider
        self.audio_controller = audio_controller
        self.typewriter_controller = typewriter_controller
        self.set_character_image = set_character_image
        self.set_busy = set_busy
        self.on_chat_done = on_chat_done
        self.on_error = on_error
        self.apply_visual = apply_visual

        self.chat_worker: ChatWorker | None = None
        self.retired_chat_workers: list[ChatWorker] = []
        self.stream_session_id = 0
        self.active_stream_token: StreamToken | None = None
        self.current_stream_kind: StreamKind | None = None
        self.current_prelude_on_done: Callable[[], None] | None = None
        self.audio_session_id = 0

        self.streaming_mode = False
        self.stream_pending_units: dict[int, StreamUnitState] = {}
        self.next_stream_index = 0
        self.stream_done = False
        self.playback_items: list[StreamUnitState] = []
        self.playback_index = 0
        self.playback_active = False
        self.current_audio_finished = False
        self.current_text_finished = False

    def start_chat(self, message: str, visual_overrides: dict[str, Any] | None = None) -> StreamToken:
        return self._start_stream(
            kind=StreamKind.CHAT,
            message=message,
            visual_overrides=visual_overrides,
            on_done=None,
        )

    def start_song_prelude(
        self,
        prompt: str,
        visual_overrides: dict[str, Any] | None = None,
        on_done: Callable[[], None] | None = None,
    ) -> StreamToken:
        return self._start_stream(
            kind=StreamKind.SONG_PRELUDE,
            message=prompt,
            visual_overrides=visual_overrides,
            on_done=on_done,
        )

    def stop_current(self) -> None:
        self._retire_chat_worker(interrupt=True)
        self._invalidate_stream_token()
        self.current_stream_kind = None
        self.current_prelude_on_done = None
        self._reset_playback_state(streaming=False)
        self.typewriter_controller.stop()
        self.audio_controller.release_chat_audio()
        self.audio_controller.release_preloaded()
        self.set_busy(False)

    def is_busy(self) -> bool:
        return bool(
            (self.chat_worker and self.chat_worker.isRunning())
            or self.playback_active
            or self.streaming_mode
        )

    def shutdown(self, wait_ms: int = 1500) -> None:
        self.stop_current()
        workers = [worker for worker in self.retired_chat_workers if worker is not None]
        self.retired_chat_workers = []
        for worker in workers:
            if worker.isRunning():
                worker.requestInterruption()
                worker.wait(wait_ms)
            try:
                worker.deleteLater()
            except Exception:
                pass

    def _start_stream(
        self,
        *,
        kind: StreamKind,
        message: str,
        visual_overrides: dict[str, Any] | None,
        on_done: Callable[[], None] | None,
    ) -> StreamToken:
        self.stop_current()
        self._prune_retired_chat_workers()
        token = self._next_stream_token(kind)
        self.current_stream_kind = kind
        self.current_prelude_on_done = on_done if kind == StreamKind.SONG_PRELUDE else None
        logger.debug("event=stream_start stream_id=%s kind=%s message_len=%s", token.id, kind.value, len(message))

        self._reset_playback_state(streaming=True)
        self.set_busy(True)
        self.typewriter_controller.start("……", interval_ms=180)

        worker = ChatWorker(
            self.agent,
            message,
            self.conversation_id_provider(),
            visual_overrides if visual_overrides is not None else self.visual_overrides_provider(),
            self,
        )
        worker.token = token
        worker.stream_event.connect(self._handle_stream_event)
        worker.failed.connect(self._handle_chat_worker_error)
        worker.finished.connect(self._handle_chat_worker_finished)
        self.chat_worker = worker
        worker.start()
        return token

    def _next_stream_token(self, kind: StreamKind) -> StreamToken:
        self.stream_session_id += 1
        token = StreamToken(id=self.stream_session_id, kind=kind)
        self.active_stream_token = token
        return token

    def _invalidate_stream_token(self, token: StreamToken | None = None) -> None:
        if token is None or token == self.active_stream_token:
            self.active_stream_token = None

    def _next_audio_token(self) -> AudioToken:
        self.audio_session_id += 1
        return AudioToken(id=self.audio_session_id, owner=AudioOwner.CHAT)

    def _active_stream_signal_token(self) -> StreamToken | None:
        sender = self.sender()
        if not isinstance(sender, ChatWorker):
            return None
        token = sender.token
        if token is None or token != self.active_stream_token:
            return None
        return token

    def _disconnect_chat_worker_signals(self, worker: ChatWorker) -> None:
        for signal, handler in (
            (worker.stream_event, self._handle_stream_event),
            (worker.failed, self._handle_chat_worker_error),
            (worker.finished, self._handle_chat_worker_finished),
        ):
            try:
                signal.disconnect(handler)
            except Exception:
                pass

    def _retire_chat_worker(self, *, interrupt: bool = True) -> ChatWorker | None:
        worker = self.chat_worker
        if worker is None:
            return None

        self._disconnect_chat_worker_signals(worker)
        self._invalidate_stream_token(worker.token)
        worker.token = None
        if interrupt and worker.isRunning():
            worker.requestInterruption()
        self.chat_worker = None
        if worker.isRunning() and worker not in self.retired_chat_workers:
            self.retired_chat_workers.append(worker)
        elif not worker.isRunning():
            try:
                worker.deleteLater()
            except Exception:
                pass
        return worker

    def _prune_retired_chat_workers(self) -> None:
        active_workers: list[ChatWorker] = []
        for worker in self.retired_chat_workers:
            if worker.isRunning():
                active_workers.append(worker)
                continue
            try:
                worker.deleteLater()
            except Exception:
                pass
        self.retired_chat_workers = active_workers

    def _handle_chat_worker_finished(self) -> None:
        worker = self.sender()
        if worker is self.chat_worker:
            self.chat_worker = None
        if worker in self.retired_chat_workers:
            self.retired_chat_workers.remove(worker)
        if worker is not None:
            try:
                worker.deleteLater()
            except Exception:
                pass

    def _reset_playback_state(self, *, streaming: bool) -> None:
        self.audio_controller.release_chat_audio()
        self.audio_controller.release_preloaded()
        self.streaming_mode = streaming
        self.stream_pending_units = {}
        self.next_stream_index = 0
        self.stream_done = False
        self.playback_items = []
        self.playback_index = 0
        self.playback_active = False
        self.current_audio_finished = False
        self.current_text_finished = False

    def _handle_stream_event(self, event_name: str, data: dict[str, Any]) -> None:
        token = self._active_stream_signal_token()
        if token is None:
            logger.debug("event=stale_event_ignored source=stream_event name=%s", event_name)
            return
        if event_name == "status":
            self._handle_stream_status(data)
            return
        if event_name == "unit_ready":
            self._handle_stream_unit_ready(data)
            return
        if event_name == "done":
            self._handle_stream_done(data)
            self._invalidate_stream_token(token)
            return
        if event_name == "error":
            self._invalidate_stream_token(token)
            self._handle_stream_error(str(data.get("message") or "请求失败。"), token.kind)
            return

    def _handle_stream_status(self, data: dict[str, Any]) -> None:
        state = str(data.get("state") or "")
        if state == "tools" and not self.playback_active:
            self.typewriter_controller.start("正在处理工具...", interval_ms=55)

    def _handle_stream_unit_ready(self, data: dict[str, Any]) -> None:
        unit = self._stream_unit_from_ready_event(data)
        self.stream_pending_units[unit.index] = unit
        logger.debug(
            "event=unit_queued stream_id=%s kind=%s index=%s has_audio=%s pending=%s",
            self.active_stream_token.id if self.active_stream_token else None,
            self.current_stream_kind.value if self.current_stream_kind else None,
            unit.index,
            bool(unit.audio_path),
            sorted(self.stream_pending_units),
        )
        if self.playback_active:
            self._preload_next_pending_stream_unit()
        self._pump_stream_playback()

    def _handle_stream_done(self, data: dict[str, Any]) -> None:
        self.stream_done = True
        answer = str(data.get("answer") or "").strip()
        units_count = int(data.get("units_count") or 0)
        logger.debug(
            "event=stream_done stream_id=%s kind=%s units_count=%s answer_len=%s pending=%s",
            self.active_stream_token.id if self.active_stream_token else None,
            self.current_stream_kind.value if self.current_stream_kind else None,
            units_count,
            len(answer),
            sorted(self.stream_pending_units),
        )
        if units_count == 0 and answer and self.next_stream_index == 0:
            self.stream_pending_units[0] = StreamUnitState(index=0, display_text=answer)
        self._pump_stream_playback()

    def _handle_chat_worker_error(self, message: str) -> None:
        token = self._active_stream_signal_token()
        if token is None:
            logger.debug("event=stale_event_ignored source=worker_error message=%r", message)
            return
        self._invalidate_stream_token(token)
        self._handle_stream_error(message, token.kind)

    def _handle_stream_error(self, message: str, kind: StreamKind) -> None:
        self._reset_playback_state(streaming=False)
        self.typewriter_controller.stop()
        self.set_busy(False)
        if kind == StreamKind.SONG_PRELUDE:
            logger.warning("Song prelude chat failed: %s", message)
            self._call_prelude_done()
            return
        self.current_stream_kind = None
        self.on_error(message)

    def _stream_unit_from_ready_event(self, data: dict[str, Any]) -> StreamUnitState:
        visual = data.get("visual") if isinstance(data.get("visual"), dict) else {}
        self.apply_visual(visual)
        cues = visual.get("cues") if isinstance(visual.get("cues"), list) else []
        cue = cues[0] if cues and isinstance(cues[0], dict) else {}
        if not cue:
            maybe_cue = visual.get("cue") if isinstance(visual.get("cue"), dict) else {}
            cue = maybe_cue

        try:
            index = int(data.get("index") or 0)
        except (TypeError, ValueError):
            index = self.next_stream_index
        tts_text = str(data.get("tts_text")) if data.get("tts_text") is not None else None
        display_text = str(data.get("display_text") or tts_text or "……")
        audio_path = str(data.get("audio_path")) if data.get("audio_path") else None
        return StreamUnitState(
            index=index,
            display_text=display_text,
            tts_text=tts_text,
            audio_path=audio_path,
            visual=visual,
            cue=cue,
        )

    def _pump_stream_playback(self) -> None:
        if not self.streaming_mode or self.playback_active:
            return

        unit = self.stream_pending_units.pop(self.next_stream_index, None)
        if unit is not None:
            self.next_stream_index += 1
            unit.playback_started = True
            self.playback_items = [unit]
            self.playback_index = 0
            self.playback_active = True
            logger.debug(
                "event=playback_start stream_id=%s kind=%s item=%s next_stream_index=%s",
                self.active_stream_token.id if self.active_stream_token else None,
                self.current_stream_kind.value if self.current_stream_kind else None,
                unit.index,
                self.next_stream_index,
            )
            self._play_next_tts_item()
            return

        if self.stream_done:
            self._end_stream_playback()

    def _end_stream_playback(self) -> None:
        completed_kind = self.current_stream_kind
        logger.debug(
            "event=stream_finished stream_id=%s kind=%s",
            self.active_stream_token.id if self.active_stream_token else None,
            completed_kind.value if completed_kind else None,
        )
        self.streaming_mode = False
        self.stream_pending_units = {}
        self.playback_items = []
        self.playback_index = 0
        self.playback_active = False
        self.current_audio_finished = False
        self.current_text_finished = False
        self.audio_controller.release_preloaded()
        self.set_busy(False)
        if completed_kind == StreamKind.SONG_PRELUDE:
            self._call_prelude_done()
            return
        self.current_stream_kind = None
        self.on_chat_done()

    def _call_prelude_done(self) -> None:
        on_done = self.current_prelude_on_done
        self.current_prelude_on_done = None
        self.current_stream_kind = None
        if on_done is not None:
            on_done()

    def _play_next_tts_item(self) -> None:
        if not self.playback_active:
            self._finish_playback()
            return
        if self.playback_index >= len(self.playback_items):
            self._finish_playback()
            return

        unit = self.playback_items[self.playback_index]
        self.current_audio_finished = False
        self.current_text_finished = False
        cue = unit.cue
        image_path = cue.get("image_path")
        if image_path:
            self.set_character_image(image_path)

        self.typewriter_controller.start(str(unit.display_text or "……"), on_finished=self._mark_text_finished)
        self._play_chunk_audio(unit.audio_path)
        self._preload_next_playback_item()

    def _current_playback_item_index(self) -> Any:
        if 0 <= self.playback_index < len(self.playback_items):
            return self.playback_items[self.playback_index].index
        return self.playback_index

    def _audio_item_key(self, item_index: Any) -> int | None:
        try:
            return int(item_index)
        except (TypeError, ValueError):
            return None

    def _preload_audio_for_unit(self, unit: StreamUnitState) -> None:
        item_key = self._audio_item_key(unit.index)
        if item_key is None:
            return

        audio_path = unit.audio_path
        if not audio_path:
            return
        if self.audio_controller.preload_chat_audio(item_key, audio_path):
            logger.debug("Audio preloaded item=%s path=%s", item_key, Path(str(audio_path)))

    def _preload_next_playback_item(self) -> None:
        self._preload_next_pending_stream_unit()

    def _preload_next_pending_stream_unit(self) -> None:
        unit = self.stream_pending_units.get(self.next_stream_index)
        if unit is not None:
            self._preload_audio_for_unit(unit)

    def _play_chunk_audio(self, audio_path: Any) -> None:
        self.audio_controller.release_chat_audio()
        item_index = self._current_playback_item_index()
        if not audio_path:
            logger.debug("Audio fallback item=%s reason=missing_path_or_qt", item_index)
            self.current_audio_finished = True
            self._maybe_advance_playback()
            return

        path = Path(str(audio_path))
        if not path.exists():
            logger.debug("Audio fallback item=%s reason=missing_file path=%s", item_index, path)
            self.current_audio_finished = True
            self._maybe_advance_playback()
            return

        token = self._next_audio_token()
        logger.debug("event=audio_start stream_id=%s item=%s token_id=%s path=%s", self.stream_session_id, item_index, token.id, path)
        self.audio_controller.play_chat_audio(
            audio_path,
            token,
            on_finished=lambda index=item_index: self._handle_chat_audio_finished(index),
        )

    def _handle_chat_audio_finished(self, item_index: Any) -> None:
        logger.debug("event=audio_finished stream_id=%s item=%s", self.stream_session_id, item_index)
        self.current_audio_finished = True
        self._maybe_advance_playback()

    def _mark_text_finished(self) -> None:
        logger.debug("event=typewriter_finished stream_id=%s item=%s", self.stream_session_id, self._current_playback_item_index())
        self.current_text_finished = True
        self._maybe_advance_playback()

    def _maybe_advance_playback(self) -> None:
        if not self.playback_active:
            return
        if not self.current_audio_finished or not self.current_text_finished:
            return
        item_index = self._current_playback_item_index()
        if 0 <= self.playback_index < len(self.playback_items):
            self.playback_items[self.playback_index].playback_finished = True
        self.playback_index += 1
        self.current_audio_finished = False
        self.current_text_finished = False
        logger.debug(
            "event=playback_advance stream_id=%s item=%s playback_index=%s next_stream_index=%s",
            self.stream_session_id,
            item_index,
            self.playback_index,
            self.next_stream_index,
        )
        QTimer.singleShot(0, self._play_next_tts_item)

    def _finish_playback(self) -> None:
        self.playback_active = False
        self.playback_items = []
        self.playback_index = 0
        self.current_audio_finished = False
        self.current_text_finished = False
        if self.streaming_mode:
            QTimer.singleShot(0, self._pump_stream_playback)
            return
        self.audio_controller.release_preloaded()
        self.set_busy(False)
        if self.current_stream_kind == StreamKind.SONG_PRELUDE:
            self._call_prelude_done()
            return
        self.current_stream_kind = None
        self.on_chat_done()

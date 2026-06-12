from __future__ import annotations

import logging
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, QTimer

from spica.core.events import event_from_legacy
from spica.core.proactive import NO_COMMENT_SENTINEL, compose_system_directive_message
from spica.core.state_machine import ChatStateMachine
from ui.controllers.audio_controller import AudioController
from ui.controllers.typewriter_controller import TypewriterController
from ui.models.playback import AudioOwner, AudioToken
from ui.models.stream import StreamKind, StreamToken
from ui.models.stream_unit import (
    StreamUnitState,
    is_stream_unit_ready_for_playback,
    merge_stream_unit_state,
)
from ui.workers.chat_worker import ChatWorker

logger = logging.getLogger(__name__)


class ChatStreamController(QObject):
    def __init__(
        self,
        parent: QObject,
        agent: Any,
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
        # B2/P3: one-shot observers for "the CURRENT stream's playback ended".
        # SongController parks the prelude gate here; the proactive arbiter parks
        # its full-duplex restore point here. A LIST -- two waiters may coexist
        # (a sing_song event during a system turn), a single slot would drop one.
        self._on_current_stream_done: list[Callable[[], None]] = []
        # P5: optional report channel -- called with the final answer whenever a
        # SYSTEM stream reaches stream_done (the reaction engine's beat/refund hook).
        self.on_system_stream_done: Callable[[str], None] | None = None
        self.audio_session_id = 0

        # Phase 6E: ChatState is the single source of truth for UI busy-ness.
        # The booleans below remain as internal playback-loop coordination only.
        self.state_machine = ChatStateMachine()

        self.streaming_mode = False
        self.stream_pending_units: dict[int, StreamUnitState] = {}
        self.next_stream_index = 0
        self.stream_done = False
        self.playback_items: list[StreamUnitState] = []
        self.playback_index = 0
        self.current_unit: StreamUnitState | None = None
        self.playback_active = False
        self.current_audio_finished = False
        self.current_text_finished = False
        self._advance_wait_logged: set[tuple[int | None, bool, bool]] = set()
        self._pump_wait_logged: set[tuple[int, str, bool | None, bool | None]] = set()
        self._last_playback_advance_index: int | None = None
        self._last_playback_advance_at_ms: float | None = None

    def start_chat(
        self,
        message: str,
        visual_overrides: dict[str, Any] | None = None,
        screen_attachment: dict[str, Any] | None = None,
    ) -> StreamToken:
        return self._start_stream(
            kind=StreamKind.CHAT,
            message=message,
            visual_overrides=visual_overrides,
            screen_attachment=screen_attachment,
        )

    def start_system_turn(self, request: Any) -> StreamToken:
        """P3: launch a SYSTEM-initiated turn (a ProactiveTurnRequest). The framed
        directive rides the normal stream machinery -- ChatWorker / playback /
        typewriter / TTS / busy all behave exactly like a user turn, and a user
        message preempts it via the usual stop_current."""
        return self._start_stream(
            kind=StreamKind.SYSTEM,
            message=compose_system_directive_message(request.directive),
            visual_overrides=None,
            screen_attachment=None,
            conversation_id=request.conversation_id,
        )

    def notify_on_current_stream_done(self, callback: Callable[[], None]) -> None:
        """One-shot: run ``callback`` when the current stream's playback ends
        (or immediately when nothing is playing; a stopped/aborted stream also
        counts as done so a waiter can never deadlock)."""
        if not self.is_busy() and not self.playback_active:
            callback()
            return
        self._on_current_stream_done.append(callback)

    def _fire_current_stream_done(self) -> None:
        callbacks = self._on_current_stream_done
        self._on_current_stream_done = []
        for callback in callbacks:
            callback()

    def stop_current(self) -> None:
        self._retire_chat_worker(interrupt=True)
        self._invalidate_stream_token()
        self.current_stream_kind = None
        self._fire_current_stream_done()
        self._reset_playback_state(streaming=False)
        self.typewriter_controller.stop()
        self.audio_controller.release_chat_audio()
        self.audio_controller.release_preloaded()
        self.set_busy(False)

    def is_busy(self) -> bool:
        # Phase 6E: UI reads busy-ness from the state machine, not scattered bools.
        return bool(self.state_machine.is_busy or (self.chat_worker and self.chat_worker.isRunning()))

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
        screen_attachment: dict[str, Any] | None,
        conversation_id: str | None = None,
    ) -> StreamToken:
        self.stop_current()
        self._prune_retired_chat_workers()
        token = self._next_stream_token(kind)
        self.current_stream_kind = kind
        logger.debug(
            "event=stream_start stream_id=%s kind=%s message_len=%s next_stream_index=%s "
            "playback_active=%s pending_indexes=%s monotonic_ms=%s",
            token.id,
            kind.value,
            len(message),
            self.next_stream_index,
            self.playback_active,
            sorted(self.stream_pending_units),
            self._now_ms(),
        )

        self._reset_playback_state(streaming=True)
        self.set_busy(True)
        self.typewriter_controller.start("……", interval_ms=180)
        include_user_time_context = kind == StreamKind.CHAT
        interaction_mode = kind.value

        worker = ChatWorker(
            self.agent,
            message,
            conversation_id or self.conversation_id_provider(),
            visual_overrides if visual_overrides is not None else self.visual_overrides_provider(),
            include_user_time_context,
            interaction_mode,
            self,
            screen_attachment=screen_attachment if kind == StreamKind.CHAT else None,
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
        self.current_unit = None
        self.playback_active = False
        self.current_audio_finished = False
        self.current_text_finished = False
        self._advance_wait_logged = set()
        self._pump_wait_logged = set()
        self._last_playback_advance_index = None
        self._last_playback_advance_at_ms = None
        # Mirror the turn lifecycle into the state machine (Phase 6E).
        if streaming:
            self.state_machine.start_turn()
        else:
            self.state_machine.stop()

    def _handle_stream_event(self, event_name: str, data: dict[str, Any]) -> None:
        token = self._active_stream_signal_token()
        if token is None:
            self._log_stale_event_ignored("stream_event", name=event_name)
            return
        self.state_machine.on_runtime_event(event_from_legacy({"event": event_name, "data": data}))
        if event_name == "status":
            self._handle_stream_status(data)
            return
        if event_name == "unit_text_ready":
            self._handle_stream_unit_text_ready(data)
            return
        if event_name == "unit_audio_started":
            self._handle_stream_unit_audio_started(data)
            return
        if event_name == "unit_audio_ready":
            self._handle_stream_unit_audio_ready(data)
            return
        if event_name == "unit_visual_ready":
            self._handle_stream_unit_visual_ready(data)
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
        logger.debug(
            "event=stream_event_ignored_unknown stream_id=%s kind=%s name=%s next_stream_index=%s "
            "playback_active=%s pending_indexes=%s monotonic_ms=%s",
            token.id,
            token.kind.value,
            event_name,
            self.next_stream_index,
            self.playback_active,
            sorted(self.stream_pending_units),
            self._now_ms(),
        )

    def _handle_stream_status(self, data: dict[str, Any]) -> None:
        state = str(data.get("state") or "")
        message = str(data.get("message") or "")
        if state == "tools" and not self.playback_active:
            if message == "inspecting_screen":
                self.typewriter_controller.start("正在查看屏幕...", interval_ms=55)
                return
            if message == "tool:watch_game_screen":
                self.typewriter_controller.start("Spica正在尸检屏幕...", interval_ms=55)
                return
            self.typewriter_controller.start("正在处理工具...", interval_ms=55)

    def _handle_stream_unit_text_ready(self, data: dict[str, Any]) -> None:
        index = self._stream_unit_index_from_data(data)
        unit = self._unit_for_update(index, create=True)
        if unit is None:
            self._log_stale_event_ignored("unit_text_ready", index=index)
            return

        unit.display_text = str(data.get("display_text") or data.get("tts_text") or unit.display_text or "……")
        unit.tts_text = str(data.get("tts_text")) if data.get("tts_text") is not None else unit.tts_text
        unit.text_ready = True
        unit.timeline.text_ready_at_ms = self._now_ms()
        self._log_ui_playback_event(
            "unit_text_ready",
            unit=unit,
            text_ready_at_ms=unit.timeline.text_ready_at_ms,
            text_chars=len(unit.display_text or ""),
        )
        self._log_unit_queued(unit)
        self._pump_stream_playback()

    def _handle_stream_unit_audio_started(self, data: dict[str, Any]) -> None:
        index = self._stream_unit_index_from_data(data)
        unit = self._unit_for_update(index, create=True)
        if unit is None:
            self._log_stale_event_ignored("unit_audio_started", index=index)
            return

        unit.tts_text = str(data.get("tts_text")) if data.get("tts_text") is not None else unit.tts_text
        unit.timeline.audio_started_at_ms = self._now_ms()
        self._log_ui_playback_event(
            "unit_audio_started",
            unit=unit,
            audio_started_at_ms=unit.timeline.audio_started_at_ms,
        )

    def _handle_stream_unit_audio_ready(self, data: dict[str, Any]) -> None:
        index = self._stream_unit_index_from_data(data)
        unit = self._unit_for_update(index, create=True)
        if unit is None:
            self._log_stale_event_ignored("unit_audio_ready", index=index)
            return

        unit.audio_path = str(data.get("audio_path")) if data.get("audio_path") else unit.audio_path
        unit.audio_ready = True
        unit.timeline.audio_ready_at_ms = self._now_ms()
        unit.timeline.audio_error = str(data.get("audio_error")) if data.get("audio_error") else None
        self._log_ui_playback_event(
            "unit_audio_ready",
            unit=unit,
            audio_ready_at_ms=unit.timeline.audio_ready_at_ms,
            has_audio=bool(unit.audio_path),
            audio_error=unit.timeline.audio_error,
        )
        if self.playback_active:
            self._preload_next_pending_stream_unit()
        self._pump_stream_playback()

    def _handle_stream_unit_visual_ready(self, data: dict[str, Any]) -> None:
        index = self._stream_unit_index_from_data(data)
        unit = self._unit_for_update(index, create=True)
        if unit is None:
            self._log_stale_event_ignored("unit_visual_ready", index=index)
            return

        visual = data.get("visual") if isinstance(data.get("visual"), dict) else {}
        cue = data.get("cue") if isinstance(data.get("cue"), dict) else {}
        if not cue:
            cue = self._cue_from_visual_payload(visual)
        visual_error = data.get("visual_error")
        unit.visual = visual
        unit.cue = cue
        unit.visual_ready = True
        unit.timeline.visual_ready_at_ms = self._now_ms()
        unit.timeline.visual_error = str(visual_error) if visual_error else None
        image_path = cue.get("image_path") if isinstance(cue, dict) else None
        self._log_ui_playback_event(
            "unit_visual_ready",
            unit=unit,
            visual_ready_at_ms=unit.timeline.visual_ready_at_ms,
            has_visual=bool(visual),
            has_cue=bool(cue),
            has_image_path=bool(image_path),
            visual_error=unit.timeline.visual_error,
        )
        self._apply_unit_visual_if_current(unit, reason="unit_visual_ready")

    def _handle_stream_unit_ready(self, data: dict[str, Any]) -> None:
        index = self._stream_unit_index_from_data(data)
        target = self._unit_for_update(index, create=True)
        if target is None:
            self._log_stale_event_ignored("unit_ready", index=index)
            return
        visual_was_ready = target.visual_ready and target.timeline.visual_ready_at_ms is not None
        unit = self._stream_unit_from_ready_event(data)
        self._merge_stream_unit(target, unit)
        self._stamp_unit_stream(target)
        ready_at_ms = self._now_ms()
        if target.text_ready and target.timeline.text_ready_at_ms is None:
            target.timeline.text_ready_at_ms = ready_at_ms
        if target.audio_ready and target.timeline.audio_ready_at_ms is None:
            target.timeline.audio_ready_at_ms = ready_at_ms
        if target.visual and target.timeline.visual_ready_at_ms is None:
            target.timeline.visual_ready_at_ms = ready_at_ms
        if target.visual and not visual_was_ready:
            target.visual_ready = True
            self._apply_unit_visual_if_current(target, reason="unit_ready_compat")
        self._log_ui_playback_event(
            "unit_ready",
            unit=target,
            level=logging.DEBUG,
            has_audio=bool(target.audio_path),
            has_visual=bool(target.visual),
            has_cue=bool(target.cue),
            visual_ready_at_ms=target.timeline.visual_ready_at_ms,
            audio_error=target.timeline.audio_error,
            visual_error=target.timeline.visual_error,
        )
        self._log_unit_queued(target)
        if self.playback_active:
            self._preload_next_pending_stream_unit()
        self._pump_stream_playback()

    def _handle_stream_done(self, data: dict[str, Any]) -> None:
        self.stream_done = True
        answer = str(data.get("answer") or "").strip()
        units_count = int(data.get("units_count") or 0)
        if self.current_stream_kind == StreamKind.SYSTEM:
            # P5: report the system turn's outcome (reaction beat/refund hook)...
            if self.on_system_stream_done is not None:
                self.on_system_stream_done(answer)
            # ...and a swallowed NO_COMMENT turn must never reach the display --
            # without this, the no-units fallback below would SHOW the sentinel.
            if answer == NO_COMMENT_SENTINEL:
                answer = ""
        logger.debug(
            "event=stream_done stream_id=%s kind=%s units_count=%s answer_len=%s pending=%s",
            self.active_stream_token.id if self.active_stream_token else None,
            self.current_stream_kind.value if self.current_stream_kind else None,
            units_count,
            len(answer),
            sorted(self.stream_pending_units),
        )
        if units_count == 0 and answer and self.next_stream_index == 0:
            unit = StreamUnitState(index=0, display_text=answer)
            self._stamp_unit_stream(unit)
            ready_at_ms = self._now_ms()
            unit.timeline.text_ready_at_ms = ready_at_ms
            unit.timeline.audio_ready_at_ms = ready_at_ms
            self.stream_pending_units[0] = unit
        self._pump_stream_playback()

    def _handle_chat_worker_error(self, message: str) -> None:
        token = self._active_stream_signal_token()
        if token is None:
            self._log_stale_event_ignored("worker_error", message=message)
            return
        self._invalidate_stream_token(token)
        self._handle_stream_error(message, token.kind)

    def _handle_stream_error(self, message: str, kind: StreamKind) -> None:
        del kind
        self._reset_playback_state(streaming=False)
        self.typewriter_controller.stop()
        self.set_busy(False)
        self._fire_current_stream_done()  # an errored stream counts as done (B2 gate)
        self.current_stream_kind = None
        self.on_error(message)

    def _stream_unit_index_from_data(self, data: dict[str, Any]) -> int:
        try:
            return int(data.get("index") or 0)
        except (TypeError, ValueError):
            return self.next_stream_index

    def _now_ms(self) -> float:
        return round(time.perf_counter() * 1000.0, 2)

    def _active_stream_id(self) -> int | None:
        return self.active_stream_token.id if self.active_stream_token else None

    def _active_stream_kind_value(self) -> str | None:
        return self.current_stream_kind.value if self.current_stream_kind else None

    def _stamp_unit_stream(self, unit: StreamUnitState) -> None:
        unit.stream_id = self._active_stream_id()
        unit.stream_kind = self._active_stream_kind_value()

    def _unit_stream_id(self, unit: StreamUnitState | None) -> int | None:
        if unit is not None and unit.stream_id is not None:
            return unit.stream_id
        return self._active_stream_id()

    def _unit_stream_kind(self, unit: StreamUnitState | None) -> str | None:
        if unit is not None and unit.stream_kind is not None:
            return unit.stream_kind
        return self._active_stream_kind_value()

    def _gap_ms(self, later_ms: float | None, earlier_ms: float | None) -> float | None:
        if later_ms is None or earlier_ms is None:
            return None
        return round(later_ms - earlier_ms, 2)

    def _log_value(self, value: Any) -> str:
        if isinstance(value, str):
            return repr(value)
        return str(value)

    def _log_ui_playback_event(
        self,
        event: str,
        *,
        unit: StreamUnitState | None = None,
        index: Any | None = None,
        # Playback state-machine tracing is profiling material -> DEBUG by default.
        # BOTH wrapper layers must default to DEBUG: this one AND
        # _log_play_item_event, whose own INFO default kept re-imposing INFO on
        # every per-item event after this default was demoted (the second source
        # of the "chat_stream still spams INFO" mismatch). The WARNING callers
        # (slow alerts) pass their level explicitly.
        level: int = logging.DEBUG,
        **fields: Any,
    ) -> None:
        if index is None:
            index = unit.index if unit is not None else self._current_playback_item_index()
        field_parts = " ".join(f"{key}={self._log_value(value)}" for key, value in fields.items())
        suffix = f" {field_parts}" if field_parts else ""
        logger.log(
            level,
            "event=%s stream_id=%s kind=%s index=%s next_stream_index=%s text_ready=%s audio_ready=%s "
            "visual_ready=%s "
            "current_audio_finished=%s current_text_finished=%s playback_active=%s pending_indexes=%s "
            "monotonic_ms=%s%s",
            event,
            self._unit_stream_id(unit),
            self._unit_stream_kind(unit),
            index,
            self.next_stream_index,
            unit.text_ready if unit is not None else None,
            unit.audio_ready if unit is not None else None,
            unit.visual_ready if unit is not None else None,
            self.current_audio_finished,
            self.current_text_finished,
            self.playback_active,
            sorted(self.stream_pending_units),
            self._now_ms(),
            suffix,
        )

    def _log_stale_event_ignored(self, source: str, **fields: Any) -> None:
        field_parts = " ".join(f"{key}={self._log_value(value)}" for key, value in fields.items())
        suffix = f" {field_parts}" if field_parts else ""
        logger.debug(
            "event=stale_event_ignored stream_id=%s kind=%s source=%s next_stream_index=%s "
            "playback_active=%s pending_indexes=%s monotonic_ms=%s%s",
            self._active_stream_id(),
            self._active_stream_kind_value(),
            source,
            self.next_stream_index,
            self.playback_active,
            sorted(self.stream_pending_units),
            self._now_ms(),
            suffix,
        )

    def _duration_ms(self, started_at_ms: float) -> float:
        return round(self._now_ms() - started_at_ms, 2)

    def _path_exists(self, path: Any) -> bool:
        if not path:
            return False
        try:
            return Path(str(path)).exists()
        except (OSError, TypeError, ValueError):
            return False

    def _cue_from_visual_payload(self, visual: dict[str, Any]) -> dict[str, Any]:
        cue = visual.get("cue") if isinstance(visual.get("cue"), dict) else {}
        if cue:
            return cue
        cues = visual.get("cues") if isinstance(visual.get("cues"), list) else []
        if cues and isinstance(cues[0], dict):
            return cues[0]
        return {}

    def _apply_unit_visual_if_current(self, unit: StreamUnitState, *, reason: str) -> None:
        if not self.playback_active or self.current_unit is not unit:
            return

        if unit.visual:
            self.apply_visual(unit.visual)
        image_path = unit.cue.get("image_path") if isinstance(unit.cue, dict) else None
        if image_path:
            QTimer.singleShot(
                0,
                lambda target=unit, target_image=image_path: self._set_unit_image_after_playback_start(
                    target,
                    target_image,
                ),
            )
        self._log_ui_playback_event(
            "unit_visual_applied",
            unit=unit,
            reason=reason,
            has_visual=bool(unit.visual),
            has_image_path=bool(image_path),
        )

    def _play_item_log_fields(self, unit: StreamUnitState, image_path: Any) -> dict[str, Any]:
        return {
            "stream_kind": self._unit_stream_kind(unit),
            "has_image_path": bool(image_path),
            "image_path_exists": self._path_exists(image_path),
            "display_text_length": len(str(unit.display_text or "")),
            "has_audio_path": bool(unit.audio_path),
            "audio_path_exists": self._path_exists(unit.audio_path),
        }

    def _log_play_item_event(
        self,
        event: str,
        unit: StreamUnitState,
        image_path: Any,
        *,
        # DEBUG like _log_ui_playback_event -- an INFO default here silently
        # overrode the demotion there for every per-item playback event.
        level: int = logging.DEBUG,
        **fields: Any,
    ) -> None:
        self._log_ui_playback_event(
            event,
            unit=unit,
            level=level,
            **self._play_item_log_fields(unit, image_path),
            **fields,
        )

    def _unit_for_update(self, index: int, *, create: bool) -> StreamUnitState | None:
        unit = self.stream_pending_units.get(index)
        if unit is not None:
            self._stamp_unit_stream(unit)
            return unit

        for playback_unit in self.playback_items:
            if playback_unit.index == index:
                self._stamp_unit_stream(playback_unit)
                return playback_unit

        if index < self.next_stream_index:
            return None
        if not create:
            return None

        unit = StreamUnitState(
            index=index,
            text_ready=False,
            audio_ready=False,
            visual_ready=False,
        )
        self._stamp_unit_stream(unit)
        self.stream_pending_units[index] = unit
        return unit

    def _merge_stream_unit(self, target: StreamUnitState, source: StreamUnitState) -> None:
        merge_stream_unit_state(target, source)

    def _log_unit_queued(self, unit: StreamUnitState) -> None:
        logger.debug(
            "event=unit_queued stream_id=%s kind=%s index=%s has_audio=%s text_ready=%s "
            "audio_ready=%s visual_ready=%s pending=%s",
            self._unit_stream_id(unit),
            self._unit_stream_kind(unit),
            unit.index,
            bool(unit.audio_path),
            unit.text_ready,
            unit.audio_ready,
            unit.visual_ready,
            sorted(self.stream_pending_units),
        )

    def _unit_ready_for_playback(self, unit: StreamUnitState) -> bool:
        return is_stream_unit_ready_for_playback(unit)

    def _stream_unit_from_ready_event(self, data: dict[str, Any]) -> StreamUnitState:
        visual = data.get("visual") if isinstance(data.get("visual"), dict) else {}
        cue = self._cue_from_visual_payload(visual)

        try:
            index = int(data.get("index") or 0)
        except (TypeError, ValueError):
            index = self.next_stream_index
        tts_text = str(data.get("tts_text")) if data.get("tts_text") is not None else None
        display_text = str(data.get("display_text") or tts_text or "……")
        audio_path = str(data.get("audio_path")) if data.get("audio_path") else None
        unit = StreamUnitState(
            index=index,
            display_text=display_text,
            tts_text=tts_text,
            audio_path=audio_path,
            visual=visual,
            cue=cue,
        )
        unit.timeline.audio_error = str(data.get("audio_error")) if data.get("audio_error") else None
        visual_error = data.get("visual_error") or visual.get("selection_error")
        unit.timeline.visual_error = str(visual_error) if visual_error else None
        return unit

    def _log_pump_wait_not_ready(self, unit: StreamUnitState) -> None:
        key = (unit.index, "not_ready", unit.text_ready, unit.audio_ready)
        if key in self._pump_wait_logged:
            return
        self._pump_wait_logged.add(key)
        self._log_ui_playback_event(
            "pump_wait_not_ready",
            unit=unit,
            index=self.next_stream_index,
            next_index=self.next_stream_index,
        )

    def _log_pump_no_next_unit(self) -> None:
        key = (self.next_stream_index, "no_next", None, None)
        if key in self._pump_wait_logged:
            return
        self._pump_wait_logged.add(key)
        self._log_ui_playback_event(
            "pump_no_next_unit",
            index=self.next_stream_index,
            next_index=self.next_stream_index,
            stream_done=self.stream_done,
        )

    def _log_next_playback_gap(self, unit: StreamUnitState, playback_started_at_ms: float) -> None:
        if self._last_playback_advance_index is None or self._last_playback_advance_at_ms is None:
            return
        self._log_ui_playback_event(
            "next_playback_gap",
            unit=unit,
            previous_index=self._last_playback_advance_index,
            next_index=unit.index,
            gap_ms=self._gap_ms(playback_started_at_ms, self._last_playback_advance_at_ms),
        )
        self._last_playback_advance_index = None
        self._last_playback_advance_at_ms = None

    def _pump_stream_playback(self) -> None:
        if not self.streaming_mode or self.playback_active:
            return

        unit = self.stream_pending_units.get(self.next_stream_index)
        if unit is not None:
            if not self._unit_ready_for_playback(unit):
                self._log_pump_wait_not_ready(unit)
                return
            unit = self.stream_pending_units.pop(self.next_stream_index)
            self.next_stream_index += 1
            playback_started_at_ms = self._now_ms()
            unit.timeline.playback_started_at_ms = playback_started_at_ms
            unit.playback_started = True
            self.playback_items = [unit]
            self.playback_index = 0
            self.current_unit = unit
            self.playback_active = True
            self.state_machine.on_playback_started()
            self._log_ui_playback_event(
                "playback_start",
                unit=unit,
                playback_started_at_ms=unit.timeline.playback_started_at_ms,
                gap_from_audio_ready_ms=self._gap_ms(
                    unit.timeline.playback_started_at_ms,
                    unit.timeline.audio_ready_at_ms,
                ),
            )
            self._log_next_playback_gap(unit, playback_started_at_ms)
            self._play_next_tts_item()
            return

        if self.stream_done:
            self._end_stream_playback()
            return

        self._log_pump_no_next_unit()

    def _end_stream_playback(self) -> None:
        completed_kind = self.current_stream_kind
        logger.debug(
            "event=stream_finished stream_id=%s kind=%s next_stream_index=%s playback_active=%s "
            "pending_indexes=%s monotonic_ms=%s",
            self.active_stream_token.id if self.active_stream_token else None,
            completed_kind.value if completed_kind else None,
            self.next_stream_index,
            self.playback_active,
            sorted(self.stream_pending_units),
            self._now_ms(),
        )
        self.streaming_mode = False
        self.stream_pending_units = {}
        self.playback_items = []
        self.playback_index = 0
        self.current_unit = None
        self.playback_active = False
        self.current_audio_finished = False
        self.current_text_finished = False
        self.audio_controller.release_preloaded()
        self.set_busy(False)
        self.state_machine.stop()
        del completed_kind
        self._fire_current_stream_done()
        self.current_stream_kind = None
        self.on_chat_done()

    def _play_next_tts_item(self) -> None:
        if not self.playback_active:
            self._finish_playback()
            return
        if self.playback_index >= len(self.playback_items):
            self._finish_playback()
            return

        unit = self.playback_items[self.playback_index]
        self.current_unit = unit
        self.current_audio_finished = False
        self.current_text_finished = False
        if unit.visual_ready and unit.visual:
            if not unit.cue:
                unit.cue = self._cue_from_visual_payload(unit.visual)
            self.apply_visual(unit.visual)
            self._log_ui_playback_event(
                "unit_visual_applied",
                unit=unit,
                reason="playback_start_visual_ready",
                has_visual=bool(unit.visual),
                has_image_path=bool(unit.cue.get("image_path") if isinstance(unit.cue, dict) else None),
            )
        cue = unit.cue if isinstance(unit.cue, dict) else {}
        image_path = cue.get("image_path")
        self._log_play_item_event("play_item_enter", unit, image_path)

        typewriter_started_at_ms = self._now_ms()
        self._log_play_item_event("typewriter_start_begin", unit, image_path)
        self.typewriter_controller.start(str(unit.display_text or "……"), on_finished=self._mark_text_finished)
        typewriter_duration_ms = self._duration_ms(typewriter_started_at_ms)
        self._log_play_item_event(
            "typewriter_start_done",
            unit,
            image_path,
            duration_ms=typewriter_duration_ms,
        )
        if typewriter_duration_ms > 100:
            self._log_play_item_event(
                "typewriter_start_slow",
                unit,
                image_path,
                level=logging.WARNING,
                duration_ms=typewriter_duration_ms,
            )

        audio_started_at_ms = self._now_ms()
        self._log_play_item_event("play_chunk_audio_begin", unit, image_path)
        self._play_chunk_audio(unit)
        self._log_play_item_event(
            "play_chunk_audio_done",
            unit,
            image_path,
            duration_ms=self._duration_ms(audio_started_at_ms),
        )
        QTimer.singleShot(0, lambda target=unit, target_image=image_path: self._set_unit_image_after_playback_start(target, target_image))
        self._preload_next_playback_item()

    def _set_unit_image_after_playback_start(self, unit: StreamUnitState, image_path: Any) -> None:
        if not self.playback_active or self.current_unit is not unit:
            return

        image_started_at_ms = self._now_ms()
        self._log_play_item_event("set_character_image_start", unit, image_path)
        if image_path:
            self.set_character_image(image_path)
        image_duration_ms = self._duration_ms(image_started_at_ms)
        self._log_play_item_event(
            "set_character_image_done",
            unit,
            image_path,
            duration_ms=image_duration_ms,
        )
        if image_duration_ms > 100:
            self._log_play_item_event(
                "set_character_image_slow",
                unit,
                image_path,
                level=logging.WARNING,
                duration_ms=image_duration_ms,
            )

    def _current_playback_item_index(self) -> Any:
        if self.current_unit is not None:
            return self.current_unit.index
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

    def _play_chunk_audio(self, unit: StreamUnitState) -> None:
        self.audio_controller.release_chat_audio()
        audio_path = unit.audio_path
        item_index = unit.index
        if not audio_path:
            logger.debug("Audio fallback item=%s reason=missing_path_or_qt", item_index)
            self._mark_audio_finished(unit, item_index)
            return

        path = Path(str(audio_path))
        if not path.exists():
            logger.debug("Audio fallback item=%s reason=missing_file path=%s", item_index, path)
            self._mark_audio_finished(unit, item_index)
            return

        token = self._next_audio_token()
        self._log_ui_playback_event(
            "audio_start",
            unit=unit,
            index=item_index,
            token_id=token.id,
            path=str(path),
        )
        self.audio_controller.play_chat_audio(
            audio_path,
            token,
            on_finished=lambda index=item_index: self._handle_chat_audio_finished(index),
        )

    def _current_unit_for_finished_callback(self, item_index: Any) -> StreamUnitState | None:
        if self.current_unit is not None:
            if self.current_unit.index == item_index:
                return self.current_unit
            return None
        if 0 <= self.playback_index < len(self.playback_items):
            unit = self.playback_items[self.playback_index]
            if unit.index == item_index:
                return unit
        return None

    def _mark_audio_finished(self, unit: StreamUnitState | None, item_index: Any) -> None:
        audio_finished_at_ms = self._now_ms()
        if unit is not None:
            unit.timeline.audio_finished_at_ms = audio_finished_at_ms
        self.current_audio_finished = True
        self._log_ui_playback_event(
            "audio_finished",
            unit=unit,
            index=item_index,
            audio_finished_at_ms=audio_finished_at_ms,
        )
        self._maybe_advance_playback()

    def _handle_chat_audio_finished(self, item_index: Any) -> None:
        unit = self._current_unit_for_finished_callback(item_index)
        if unit is None:
            logger.debug(
                "event=audio_finished_stale stream_id=%s kind=%s index=%s current_index=%s",
                self._active_stream_id(),
                self._active_stream_kind_value(),
                item_index,
                self.current_unit.index if self.current_unit is not None else None,
            )
            return
        self._mark_audio_finished(unit, item_index)

    def _mark_text_finished(self) -> None:
        unit = self.current_unit
        text_finished_at_ms = self._now_ms()
        if unit is not None:
            unit.timeline.text_finished_at_ms = text_finished_at_ms
        self.current_text_finished = True
        self._log_ui_playback_event(
            "typewriter_finished",
            unit=unit,
            index=unit.index if unit is not None else self._current_playback_item_index(),
            text_finished_at_ms=text_finished_at_ms,
            gap_from_audio_finished_ms=self._gap_ms(
                text_finished_at_ms,
                unit.timeline.audio_finished_at_ms if unit is not None else None,
            ),
        )
        self._maybe_advance_playback()

    def _maybe_advance_playback(self) -> None:
        if not self.playback_active:
            return
        unit = self.current_unit
        if not self.current_audio_finished or not self.current_text_finished:
            wait_for_audio = not self.current_audio_finished
            wait_for_text = not self.current_text_finished
            if unit is not None:
                if wait_for_audio and wait_for_text:
                    unit.timeline.last_wait_reason = "audio_text"
                elif wait_for_audio:
                    unit.timeline.last_wait_reason = "audio"
                elif wait_for_text:
                    unit.timeline.last_wait_reason = "text"
            key = (unit.index if unit is not None else None, wait_for_audio, wait_for_text)
            if key not in self._advance_wait_logged:
                self._advance_wait_logged.add(key)
                self._log_ui_playback_event(
                    "advance_waiting",
                    unit=unit,
                    index=unit.index if unit is not None else self._current_playback_item_index(),
                    wait_for_audio=wait_for_audio,
                    wait_for_text=wait_for_text,
                )
            return
        item_index = unit.index if unit is not None else self._current_playback_item_index()
        playback_advance_at_ms = self._now_ms()
        if 0 <= self.playback_index < len(self.playback_items):
            finished_unit = self.playback_items[self.playback_index]
            finished_unit.playback_finished = True
            finished_unit.timeline.playback_advance_at_ms = playback_advance_at_ms
            finished_unit.timeline.playback_finished_at_ms = playback_advance_at_ms
            unit = finished_unit
        elif unit is not None:
            unit.playback_finished = True
            unit.timeline.playback_advance_at_ms = playback_advance_at_ms
            unit.timeline.playback_finished_at_ms = playback_advance_at_ms
        self.playback_index += 1
        self._last_playback_advance_index = int(item_index) if isinstance(item_index, int) else None
        self._last_playback_advance_at_ms = playback_advance_at_ms
        self._log_ui_playback_event(
            "playback_advance",
            unit=unit,
            index=item_index,
            playback_advance_at_ms=playback_advance_at_ms,
            gap_from_audio_finished_ms=self._gap_ms(
                playback_advance_at_ms,
                unit.timeline.audio_finished_at_ms if unit is not None else None,
            ),
            gap_from_text_finished_ms=self._gap_ms(
                playback_advance_at_ms,
                unit.timeline.text_finished_at_ms if unit is not None else None,
            ),
            playback_index=self.playback_index,
        )
        self.current_audio_finished = False
        self.current_text_finished = False
        self._finish_playback(pump_immediately=True)

    def _finish_playback(self, *, pump_immediately: bool = False) -> None:
        self.playback_active = False
        self.playback_items = []
        self.playback_index = 0
        self.current_unit = None
        self.current_audio_finished = False
        self.current_text_finished = False
        if self.streaming_mode:
            if pump_immediately:
                self._pump_stream_playback()
            else:
                QTimer.singleShot(0, self._pump_stream_playback)
            return
        self.audio_controller.release_preloaded()
        self.set_busy(False)
        self.state_machine.stop()
        self._fire_current_stream_done()
        self.current_stream_kind = None
        self.on_chat_done()

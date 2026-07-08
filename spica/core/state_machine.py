"""Conversation runtime state machine (Phase 6E).

A single source of truth for "what is the conversation doing right now", so the
UI can render from one ``ChatState`` instead of juggling scattered booleans
(streaming_mode / playback_active / stream_done / ...). Driven by the typed
``RuntimeEvent`` stream (Host -> UI boundary) plus a few playback callbacks the
audio layer raises (which are not part of RuntimeEvent).

INVARIANT (CLAUDE.md #1): Qt-free.
"""

from __future__ import annotations

from enum import Enum

from spica.core.events import (
    DoneEvent,
    ErrorEvent,
    RuntimeEvent,
    StatusEvent,
    UnitAudioReadyEvent,
    UnitAudioStartedEvent,
    UnitReadyEvent,
    UnitTextReadyEvent,
    UnitVisualReadyEvent,
)


class ChatState(str, Enum):
    IDLE = "idle"
    LISTENING = "listening"      # capturing voice input
    GENERATING = "generating"    # turn started, before any play unit (thinking/tools)
    STREAMING = "streaming"      # play units arriving / queued
    SPEAKING = "speaking"        # audio playing
    PAUSED = "paused"            # playback paused
    ERROR = "error"


_UNIT_EVENTS = (
    UnitTextReadyEvent,
    UnitVisualReadyEvent,
    UnitAudioStartedEvent,
    UnitAudioReadyEvent,
    UnitReadyEvent,
)


class ChatStateMachine:
    """Tracks ChatState from RuntimeEvents + playback callbacks.

    Generation (the RuntimeEvent stream) and playback (audio) are two axes: a
    turn can finish generating (``DoneEvent``) while audio is still playing, so
    ``done`` is recorded as a flag and the visible state only returns to IDLE
    once playback also finishes.
    """

    def __init__(self) -> None:
        self.state = ChatState.IDLE
        self._generation_done = False

    # -- explicit transitions -------------------------------------------------
    def start_turn(self) -> ChatState:
        self._generation_done = False
        self.state = ChatState.GENERATING
        return self.state

    def start_listening(self) -> ChatState:
        self.state = ChatState.LISTENING
        return self.state

    def stop(self) -> ChatState:
        self._generation_done = False
        self.state = ChatState.IDLE
        return self.state

    # -- RuntimeEvent-driven --------------------------------------------------
    def on_runtime_event(self, event: RuntimeEvent) -> ChatState:
        if isinstance(event, ErrorEvent):
            self.state = ChatState.ERROR
        elif isinstance(event, StatusEvent):
            if self.state in (ChatState.IDLE, ChatState.LISTENING):
                self.state = ChatState.GENERATING
        elif isinstance(event, _UNIT_EVENTS):
            # Units are arriving; stay in SPEAKING/PAUSED if already playing.
            if self.state not in (ChatState.SPEAKING, ChatState.PAUSED, ChatState.ERROR):
                self.state = ChatState.STREAMING
        elif isinstance(event, DoneEvent):
            self._generation_done = True
        return self.state

    # -- playback callbacks (raised by the audio layer) -----------------------
    def on_playback_started(self) -> ChatState:
        if self.state != ChatState.ERROR:
            self.state = ChatState.SPEAKING
        return self.state

    def on_playback_finished(self) -> ChatState:
        if self.state == ChatState.ERROR:
            return self.state
        # If generation is done and audio finished, the turn is over.
        self.state = ChatState.IDLE if self._generation_done else ChatState.STREAMING
        return self.state

    def pause(self) -> ChatState:
        if self.state == ChatState.SPEAKING:
            self.state = ChatState.PAUSED
        return self.state

    def resume(self) -> ChatState:
        if self.state == ChatState.PAUSED:
            self.state = ChatState.SPEAKING
        return self.state

    # -- queries --------------------------------------------------------------
    @property
    def is_busy(self) -> bool:
        return self.state not in (ChatState.IDLE, ChatState.ERROR)

    @property
    def generation_done(self) -> bool:
        return self._generation_done

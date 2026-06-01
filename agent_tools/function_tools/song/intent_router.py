from __future__ import annotations

import logging
import re
from typing import Any

from agent_tools.function_tools.song.config import load_song_config
from agent_tools.function_tools.song.intent import SongAction, SongContext, SongIntent, SongState
from agent_tools.function_tools.song.intent_llm import SongIntentLLMClassifier
from agent_tools.function_tools.song.intent_rules import (
    normalize_song_text,
    parse_song_command_intent,
    parse_song_control_intent,
    parse_song_followup_intent,
)


logger = logging.getLogger(__name__)


_CONTROL_ACTIONS = {
    SongAction.CANCEL,
    SongAction.PAUSE,
    SongAction.RESUME,
    SongAction.RESTART,
    SongAction.CHANGE,
}
_WEAK_SONG_SIGNAL_RE = re.compile(r"(想听|来点|唱|歌|音乐|cover|翻唱)", re.I)


class SongIntentRouter:
    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self.config = config or load_song_config()
        intent_config = self.config.get("intent") if isinstance(self.config.get("intent"), dict) else {}
        thresholds = intent_config.get("thresholds") if isinstance(intent_config.get("thresholds"), dict) else {}
        self.direct_execute = float(thresholds.get("direct_execute", 0.9))
        self.confirm = float(thresholds.get("confirm", 0.7))
        self.llm_fallback_min = float(thresholds.get("llm_fallback_min", 0.45))
        self.llm_fallback_max = float(thresholds.get("llm_fallback_max", 0.75))
        llm_config = intent_config.get("llm_fallback") if isinstance(intent_config.get("llm_fallback"), dict) else {}
        self.llm_classifier: SongIntentLLMClassifier | None = None
        if bool(intent_config.get("enabled", True)) and bool(llm_config.get("enabled", False)):
            try:
                self.llm_classifier = SongIntentLLMClassifier(llm_config)
            except Exception as exc:
                logger.warning("Song intent LLM fallback disabled: %s", exc)

    def route(
        self,
        text: str,
        state: SongState = SongState.IDLE,
        context: SongContext | None = None,
    ) -> SongIntent:
        current_state = _coerce_state(state)
        active_context = context or SongContext(state=current_state)
        active_context.state = current_state

        control_intent = parse_song_control_intent(text, current_state, active_context)
        if control_intent.action in _CONTROL_ACTIONS and control_intent.confidence >= self.direct_execute:
            return self._guard_direct_execute(control_intent, current_state)

        followup_intent = parse_song_followup_intent(text, current_state, active_context)
        if followup_intent.action == SongAction.CONFIRM and followup_intent.confidence >= self.confirm:
            return followup_intent

        command_intent = parse_song_command_intent(text)
        if command_intent.action == SongAction.REJECT and command_intent.confidence >= 0.9:
            return command_intent
        if self._is_direct_sing(command_intent):
            return command_intent
        if command_intent.action == SongAction.SEARCH and command_intent.confidence >= self.confirm:
            return command_intent

        if self._should_call_llm(command_intent, text):
            llm_intent = self._route_with_llm(text, current_state, active_context, command_intent)
            if llm_intent is not None:
                return self._guard_direct_execute(llm_intent, current_state)

        return command_intent

    def _route_with_llm(
        self,
        text: str,
        state: SongState,
        context: SongContext,
        rule_intent: SongIntent,
    ) -> SongIntent | None:
        if self.llm_classifier is None:
            return None
        try:
            return self.llm_classifier.classify(text, state, context, rule_intent)
        except Exception as exc:
            logger.warning("Song intent LLM fallback failed: %s", exc)
            return None

    def _should_call_llm(self, rule_intent: SongIntent, text: str) -> bool:
        if self.llm_classifier is None:
            return False
        if rule_intent.action in {SongAction.REJECT, SongAction.SING} and rule_intent.confidence >= self.direct_execute:
            return False
        if rule_intent.action in _CONTROL_ACTIONS and rule_intent.confidence >= self.direct_execute:
            return False
        if self.llm_fallback_min <= rule_intent.confidence <= self.llm_fallback_max:
            return True
        return rule_intent.action == SongAction.NONE and bool(_WEAK_SONG_SIGNAL_RE.search(normalize_song_text(text)))

    def _is_direct_sing(self, intent: SongIntent) -> bool:
        return intent.action == SongAction.SING and intent.confidence >= self.direct_execute and bool(intent.query or intent.title)

    def _guard_direct_execute(self, intent: SongIntent, state: SongState) -> SongIntent:
        if intent.action == SongAction.SING and not (intent.query or intent.title):
            return SongIntent(
                action=SongAction.NONE,
                confidence=0.0,
                reason="sing_without_song_object",
                source=intent.source,
                original_text=intent.original_text,
            )
        if intent.action == SongAction.SEARCH:
            intent.needs_confirmation = True
        if intent.action == SongAction.REJECT:
            return intent
        if intent.action == SongAction.RESUME and state not in {SongState.PAUSED, SongState.READY}:
            return SongIntent(action=SongAction.NONE, confidence=0.0, source=intent.source, original_text=intent.original_text)
        if intent.action == SongAction.PAUSE and state not in {SongState.PLAYING, SongState.PREPARING}:
            return SongIntent(action=SongAction.NONE, confidence=0.0, source=intent.source, original_text=intent.original_text)
        if intent.action == SongAction.CANCEL and state == SongState.IDLE:
            return SongIntent(action=SongAction.NONE, confidence=0.0, source=intent.source, original_text=intent.original_text)
        return intent


def _coerce_state(state: SongState) -> SongState:
    if isinstance(state, SongState):
        return state
    try:
        return SongState(str(state))
    except ValueError:
        return SongState.IDLE

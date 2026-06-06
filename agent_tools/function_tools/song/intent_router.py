from __future__ import annotations

import logging
import re
from typing import Any

from agent_tools.function_tools.song.config import load_song_config
from agent_tools.function_tools.song.intent import (
    SongAction,
    SongContext,
    SongIntent,
    SongState,
    clear_pending_song_hint,
    merge_pending_song_hint,
)
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
_GENERIC_LLM_SING_RE = re.compile(r"(随便|代表作|那首|很火|热门|伤感|适合|不要太|的歌|歌曲|音乐|风格)")


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
            intent = self._guard_direct_execute(control_intent, current_state)
            if intent.action == SongAction.CANCEL:
                self._clear_confirmation_context(active_context, current_state)
            return intent

        followup_intent = parse_song_followup_intent(text, current_state, active_context)
        if followup_intent.action == SongAction.CONFIRM and followup_intent.confidence >= self.confirm:
            return followup_intent
        if followup_intent.action == SongAction.REJECT and followup_intent.confidence >= 0.9:
            self._clear_confirmation_context(active_context, current_state)
            return followup_intent
        if followup_intent.action == SongAction.CANCEL and followup_intent.confidence >= self.direct_execute:
            self._clear_confirmation_context(active_context, current_state)
            return followup_intent
        if self._is_direct_sing(followup_intent):
            return self._guard_direct_execute(merge_pending_song_hint(followup_intent, active_context), current_state)

        command_intent = parse_song_command_intent(text)
        if command_intent.action == SongAction.REJECT and command_intent.confidence >= 0.9:
            self._clear_confirmation_context(active_context, current_state)
            return command_intent
        if self._is_direct_sing(command_intent):
            return self._guard_direct_execute(merge_pending_song_hint(command_intent, active_context), current_state)
        if command_intent.action == SongAction.SEARCH and command_intent.confidence >= self.confirm:
            return command_intent

        if self._should_call_llm(command_intent, text, current_state, active_context):
            llm_intent = self._route_with_llm(text, current_state, active_context, command_intent)
            if llm_intent is not None:
                guarded_intent = self._guard_direct_execute(
                    merge_pending_song_hint(llm_intent, active_context),
                    current_state,
                )
                if guarded_intent.action in {SongAction.NONE, SongAction.REJECT}:
                    self._clear_confirmation_context(active_context, current_state)
                return guarded_intent

        if command_intent.action in {SongAction.NONE, SongAction.REJECT}:
            self._clear_confirmation_context(active_context, current_state)
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

    def _should_call_llm(
        self,
        rule_intent: SongIntent,
        text: str,
        state: SongState,
        context: SongContext,
    ) -> bool:
        if self.llm_classifier is None:
            return False
        if rule_intent.action in {SongAction.REJECT, SongAction.SING} and rule_intent.confidence >= self.direct_execute:
            return False
        if rule_intent.action in _CONTROL_ACTIONS and rule_intent.confidence >= self.direct_execute:
            return False
        if state == SongState.INTENT_CONFIRMING and rule_intent.action == SongAction.NONE:
            return bool(
                context.pending_song_raw_query
                or context.pending_song_artist
                or context.pending_song_style
                or _WEAK_SONG_SIGNAL_RE.search(normalize_song_text(text))
            )
        if self.llm_fallback_min <= rule_intent.confidence <= self.llm_fallback_max:
            return True
        return rule_intent.action == SongAction.NONE and bool(_WEAK_SONG_SIGNAL_RE.search(normalize_song_text(text)))

    def _is_direct_sing(self, intent: SongIntent) -> bool:
        return intent.action == SongAction.SING and intent.confidence >= self.direct_execute and bool(intent.query or intent.title)

    def _guard_direct_execute(self, intent: SongIntent, state: SongState) -> SongIntent:
        if intent.action == SongAction.SING and not (intent.query or intent.title):
            if intent.source == "llm" and state == SongState.INTENT_CONFIRMING:
                return self._downgrade_llm_sing(intent, "llm_sing_without_song_object")
            return SongIntent(
                action=SongAction.NONE,
                confidence=0.0,
                reason="sing_without_song_object",
                source=intent.source,
                original_text=intent.original_text,
            )
        if intent.action == SongAction.SING and intent.source == "llm":
            if not self._llm_sing_has_explicit_song(intent):
                return self._downgrade_llm_sing(intent, "llm_sing_without_explicit_song")
            if intent.confidence < self.direct_execute:
                return self._downgrade_llm_sing(intent, "llm_sing_below_direct_execute")
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

    def _downgrade_llm_sing(self, intent: SongIntent, reason: str) -> SongIntent:
        if intent.query or intent.title:
            return SongIntent(
                action=SongAction.SEARCH,
                confidence=min(intent.confidence, self.confirm),
                query=intent.query or intent.title,
                title=intent.title,
                artist=intent.artist,
                reason=reason,
                needs_confirmation=True,
                source=intent.source,
                original_text=intent.original_text,
            )
        return SongIntent(
            action=SongAction.NONE,
            confidence=0.0,
            reason=reason,
            source=intent.source,
            original_text=intent.original_text,
        )

    def _llm_sing_has_explicit_song(self, intent: SongIntent) -> bool:
        if intent.title:
            return True
        query = normalize_song_text(intent.query or "")
        if not query:
            return False
        if _GENERIC_LLM_SING_RE.search(query):
            return False
        return True

    def _clear_confirmation_context(self, context: SongContext, state: SongState) -> None:
        if state != SongState.INTENT_CONFIRMING:
            return
        clear_pending_song_hint(context)
        context.state = SongState.IDLE


def _coerce_state(state: SongState) -> SongState:
    if isinstance(state, SongState):
        return state
    try:
        return SongState(str(state))
    except ValueError:
        return SongState.IDLE

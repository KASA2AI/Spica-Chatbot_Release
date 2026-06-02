from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

from agent_tools.function_tools.song.intent import SongAction, SongContext, SongIntent, SongState


_ALLOWED_ACTIONS = {action.value for action in SongAction}


class SongIntentLLMClassifier:
    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self.config = config or {}
        self.model = str(self.config.get("model") or "gpt-4.1-nano")
        self.timeout_sec = float(self.config.get("timeout_sec") or 3)
        self.max_tokens = int(self.config.get("max_tokens") or 180)
        self.temperature = float(self.config.get("temperature") or 0)

        api_key_env = str(self.config.get("api_key_env") or "SONG_INTENT_OPENAI_API_KEY")
        base_url_env = str(self.config.get("base_url_env") or "SONG_INTENT_OPENAI_BASE_URL")
        self.api_key = os.getenv(api_key_env) or os.getenv("OPENAI_API_KEY")
        self.base_url = os.getenv(base_url_env) or os.getenv("OPENAI_BASE_URL") or "https://api.openai.com/v1"
        self.base_url = self.base_url.rstrip("/")
        if not self.api_key:
            raise RuntimeError(f"missing song intent LLM API key: {api_key_env} or OPENAI_API_KEY")

    def classify(
        self,
        user_text: str,
        state: SongState,
        context: SongContext | None = None,
        rule_intent: SongIntent | None = None,
    ) -> SongIntent:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": self._system_prompt()},
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "user_text": user_text,
                            "current_state": _state_value(state),
                            "pending_song_hint": _context_payload(context),
                            "rule_intent": _intent_payload(rule_intent),
                        },
                        ensure_ascii=False,
                    ),
                },
            ],
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }
        response = self._post_chat_completions(payload)
        content = self._extract_content(response)
        data = self._parse_json_content(content)
        return self._to_intent(data, user_text, state)

    def _post_chat_completions(self, payload: dict[str, Any]) -> dict[str, Any]:
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_sec) as response:
                body = response.read().decode("utf-8")
        except urllib.error.URLError as exc:
            raise RuntimeError(f"song intent LLM request failed: {exc}") from exc
        return json.loads(body)

    def _extract_content(self, response: dict[str, Any]) -> str:
        choices = response.get("choices")
        if not isinstance(choices, list) or not choices:
            raise RuntimeError("song intent LLM returned no choices")
        message = choices[0].get("message") if isinstance(choices[0], dict) else None
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, str):
            raise RuntimeError("song intent LLM returned no message content")
        return content

    def _parse_json_content(self, content: str) -> dict[str, Any]:
        text = content.strip()
        if text.startswith("```"):
            lines = text.splitlines()
            if lines and lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip().startswith("```"):
                lines = lines[:-1]
            text = "\n".join(lines).strip()
        data = json.loads(text)
        if not isinstance(data, dict):
            raise RuntimeError("song intent LLM returned non-object JSON")
        return data

    def _to_intent(self, data: dict[str, Any], user_text: str, state: SongState) -> SongIntent:
        action_value = str(data.get("action") or "none").strip().lower()
        if action_value not in _ALLOWED_ACTIONS:
            action = SongAction.NONE
        else:
            action = SongAction(action_value)

        confidence = _clamp_float(data.get("confidence"), default=0.0)
        query = _clean_optional(data.get("query"))
        title = _clean_optional(data.get("title"))
        artist = _clean_optional(data.get("artist"))
        candidate_index = _int_or_none(data.get("candidate_index"))
        reason = str(data.get("reason") or "")
        needs_confirmation = bool(data.get("needs_confirmation"))
        current_state = _coerce_state(state)

        if action == SongAction.SING and not (query or title):
            action = SongAction.SEARCH if needs_confirmation else SongAction.NONE
            confidence = min(confidence, 0.69)
            needs_confirmation = action == SongAction.SEARCH
            reason = reason or "llm_sing_without_song_object"
        elif action == SongAction.RESUME and current_state not in {SongState.PAUSED, SongState.READY}:
            action = SongAction.NONE
            confidence = 0.0
            reason = reason or "resume_not_allowed_in_state"
        elif action == SongAction.PAUSE and current_state not in {SongState.PLAYING, SongState.PREPARING}:
            action = SongAction.NONE
            confidence = 0.0
            reason = reason or "pause_not_allowed_in_state"
        elif action == SongAction.CANCEL and current_state == SongState.IDLE:
            action = SongAction.NONE
            confidence = 0.0
            reason = reason or "cancel_not_allowed_in_idle"
        elif action == SongAction.CHANGE and current_state not in {SongState.PLAYING, SongState.PAUSED, SongState.PREPARING, SongState.READY}:
            action = SongAction.NONE
            confidence = 0.0
            reason = reason or "change_not_allowed_in_state"

        return SongIntent(
            action=action,
            confidence=confidence,
            query=query,
            title=title,
            artist=artist,
            candidate_index=candidate_index,
            reason=reason,
            needs_confirmation=needs_confirmation,
            source="llm",
            original_text=user_text,
        )

    def _system_prompt(self) -> str:
        return (
            "You are a singing-intent classifier, not the Spica character. "
            "Only output JSON. Do not answer the user. Do not start tools. "
            "Allowed action values are: none, sing, search, confirm, cancel, pause, resume, restart, change, help, reject. "
            "If the user only asks whether Spica can sing, asks how the singing feature works, asks for lyrics, "
            "or asks what a song means, use action=reject. "
            "If the user clearly asks Spica to sing, play, cover, or perform a specific song, use action=sing. "
            "If the user wants an artist/style/music but no specific song title is present, use action=search and needs_confirmation=true. "
            "When current_state is intent_confirming, the user may be answering a previous generic song request. "
            "Use pending_song_hint to interpret that follow-up. If the user gives a concrete song title, return action=sing. "
            "If the user only adds artist/style/filter words, return action=search with needs_confirmation=true. "
            "If the user cancels, return action=cancel. If the user is clearly just chatting, return action=reject or none. "
            "When current_state is playing, paused, preparing, or ready, classify pause/resume/cancel/change before song requests. "
            "Never invent song titles. Return this JSON schema exactly: "
            '{"action":"sing","confidence":0.0,"query":null,"title":null,"artist":null,'
            '"candidate_index":null,"needs_confirmation":false,"reason":""}'
        )


def _intent_payload(intent: SongIntent | None) -> dict[str, Any] | None:
    if intent is None:
        return None
    return {
        "action": intent.action.value,
        "confidence": intent.confidence,
        "query": intent.query,
        "title": intent.title,
        "artist": intent.artist,
        "candidate_index": intent.candidate_index,
        "needs_confirmation": intent.needs_confirmation,
        "reason": intent.reason,
        "source": intent.source,
    }


def _context_payload(context: SongContext | None) -> dict[str, Any]:
    if context is None:
        return {
            "pending_song_raw_query": None,
            "pending_song_artist": None,
            "pending_song_style": None,
        }
    return {
        "pending_song_raw_query": context.pending_song_raw_query,
        "pending_song_artist": context.pending_song_artist,
        "pending_song_style": context.pending_song_style,
    }


def _state_value(state: SongState) -> str:
    return _coerce_state(state).value


def _coerce_state(state: SongState) -> SongState:
    if isinstance(state, SongState):
        return state
    try:
        return SongState(str(state))
    except ValueError:
        return SongState.IDLE


def _clean_optional(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _clamp_float(value: Any, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return max(0.0, min(1.0, number))

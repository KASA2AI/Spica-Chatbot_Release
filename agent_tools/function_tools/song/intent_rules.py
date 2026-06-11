"""Song CONTROL fast path -- the only rule layer that survived B2.

B2 (P2, 2026-06) tool-ised singing into the main LLM's function call
(``sing_song``), deleting the pre-chat hijack stack (SongIntentRouter, the
second-LLM classifier, command/followup/search parsing -- ~1000 lines). What
remains is ONE deliberately thin rule: control verbs (pause / resume / cancel /
restart) while a song flow is LIVE, where a main-LLM round trip per "暂停"
would be a UX regression. Everything else -- naming a song, vague requests,
"换一首X" -- goes through normal chat and the sing_song tool.

The verb lists are intentionally narrow and only consulted while
``SongState != IDLE``: a miss falls through to normal chat (she answers), a
false positive is impossible outside an active song flow.
"""

from __future__ import annotations

import re

from agent_tools.function_tools.song.intent import SongAction, SongIntent, SongState


def normalize_song_text(text: str) -> str:
    text = (text or "").strip()
    text = text.replace("　", " ")
    text = re.sub(r"\s+", " ", text)
    return text


def parse_song_control_intent(text: str, state: SongState) -> SongIntent:
    """Control verbs for a LIVE song flow only. CHANGE was removed from the fast
    path on purpose (B2): "换一首X" is a new song request -- main LLM territory."""
    normalized = normalize_song_text(text)
    current_state = _coerce_state(state)
    if not normalized:
        return _intent(SongAction.NONE, 0.0, text)

    if current_state in {
        SongState.PREPARING,
        SongState.PLAYING,
        SongState.PAUSED,
        SongState.READY,
    }:
        if _matches_any(normalized, (r"^(别唱了|不要唱了|不听了|取消|算了|停掉|stop)$",)):
            return _intent(SongAction.CANCEL, 0.98, text, reason="control_cancel")

    if current_state in {SongState.PLAYING, SongState.PREPARING}:
        if _matches_any(normalized, (r"^(暂停|暂停一下|停一下|等一下|先停|pause)$",)):
            return _intent(SongAction.PAUSE, 0.98, text, reason="control_pause")
    if current_state == SongState.PLAYING:
        if _matches_any(normalized, (r"^(重来|重新唱|从头来|再唱一遍)$",)):
            return _intent(SongAction.RESTART, 0.94, text, reason="control_restart")

    if current_state in {SongState.PAUSED, SongState.READY}:
        if _matches_any(normalized, (r"^(继续|继续唱|接着唱|可以继续了|resume)$",)):
            return _intent(SongAction.RESUME, 0.98, text, reason="control_resume")

    return _intent(SongAction.NONE, 0.0, text)


def _coerce_state(state: SongState) -> SongState:
    if isinstance(state, SongState):
        return state
    try:
        return SongState(str(state))
    except ValueError:
        return SongState.IDLE


def _matches_any(text: str, patterns: tuple[str, ...]) -> bool:
    return any(re.search(pattern, text, flags=re.I) for pattern in patterns)


def _intent(
    action: SongAction,
    confidence: float,
    original_text: str,
    *,
    reason: str = "",
) -> SongIntent:
    return SongIntent(
        action=action,
        confidence=confidence,
        reason=reason,
        original_text=original_text,
    )

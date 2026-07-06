"""watch_anime: the anime-watch "act" tool (Phase 3) -- a pure forwarding shim.

Mirrors sing_song.py: the tool carries NO business logic. It parses/validates the
call, forwards to the injected host closure (which holds all authority: config,
sources, library, ports, event sink), and lets the closure's ``ScreenToolError``
envelope propagate on failure. effect="act", chainable=False, intent_gated=False
(state-supplied; supply is gated by the ``available`` predicate, not the router
wordlist -- no router change). Qt-free (CLAUDE.md #1).
"""

from __future__ import annotations

from typing import Any, Callable

from agent_tools.function_tools.screen.schema import ScreenToolError  # shared ToolError envelope

# closure: (query, episode, use_recent_unplayed) -> ack dict; raises
# ScreenToolError on failure.
RequestAnime = Callable[[str, "int | str | None", bool], dict[str, Any]]

_DESCRIPTION = (
    "帮用户找到并准备播放一部动漫的某一集。当用户明确说出想看的番名（可含第几季/第几集，"
    "如「无职转生第三季第一集」）时调用。只在用户给了具体片名时调用；如果用户只说「我想看动漫」"
    "这类没有具体片名的话，不要调用工具，先在对话里问清想看哪一部再调用。"
    "例外：用户明确要播放「刚下好的那集」（如「放吧」「现在看」「播吧」）而你给不出具体番名时，"
    "传 use_recent_unplayed=true（此时 query 可为空字符串）。"
)

WATCH_ANIME_SCHEMA: dict[str, Any] = {
    "type": "function",
    "name": "watch_anime",
    "strict": True,
    "description": _DESCRIPTION,
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "动漫名，可带季，如「无职转生 第三季」或「无职转生第三季第一集」。",
            },
            "episode": {
                # strict mode: optional -> nullable + still listed in required.
                "type": ["integer", "string", "null"],
                "description": "第几集（整数）；想看最新一集传字符串 \"latest\"；不确定就传 null。",
            },
            "use_recent_unplayed": {
                # 「放吧」escape (P1-11②): ONLY when the user asks to play the
                # just-finished download and no title can be reconstructed.
                "type": ["boolean", "null"],
                "description": "仅当用户要播放「刚下好的那集」（如「放吧」）且给不出片名时传 true；"
                               "其余情况一律传 null 或 false。",
            },
        },
        "required": ["query", "episode", "use_recent_unplayed"],
        "additionalProperties": False,
    },
}


class WatchAnimeTool:
    """``ToolPort`` for watch_anime (registered like the other tool shims)."""

    name = "watch_anime"

    def __init__(self, request_anime: RequestAnime) -> None:
        self._request_anime = request_anime

    def schema(self) -> dict[str, Any]:
        return WATCH_ANIME_SCHEMA

    def run(self, *, query: str = "", episode: "int | str | None" = None,
            use_recent_unplayed: "bool | None" = None) -> dict[str, Any]:
        query = (query or "").strip()
        recent = bool(use_recent_unplayed)
        # ANIME_QUERY_EMPTY contract unchanged for the plain path; only the
        # explicit 「放吧」flag exempts an empty query (P1-11②).
        if not query and not recent:
            raise ScreenToolError("ANIME_QUERY_EMPTY", "想看什么番呀？先告诉我名字～")
        # normalize an empty-string episode to None (LLM may send "")
        if isinstance(episode, str) and not episode.strip():
            episode = None
        return self._request_anime(query, episode, recent)

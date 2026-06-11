"""sing_song: the main LLM's singing entry (B2 / P2) -- the FIRST "act" tool.

Tool-ises what the pre-chat hijack stack used to do (FINDINGS #17): the user
asks for a song in natural language, the MAIN LLM decides and calls this tool;
the deleted SongIntentRouter / second-LLM classifier / canned typewriter lines
are all gone. Operation-tool discipline (P2 三铁则): the whitelisted action
surface is exactly "request one song by query"; execution authority lives in
the injected HOST closure (search + emit a SongRequestEvent for the UI to start
the worker -- this tool runs on the ChatWorker thread and must NEVER touch the
Qt SongController directly); failures come back as ToolError envelopes.

fire-and-acknowledge: the closure returns as soon as the song is resolved and
the job is handed to the UI -- the turn's streamed followup then speaks her
in-character acknowledgment (run_turn-generated, F14's canned lines healed).
Qt-free (CLAUDE.md #1); effect="act"; chainable=False (one call, one song).
"""

from __future__ import annotations

from typing import Any, Callable

from agent_tools.function_tools.screen.schema import ScreenToolError  # the shared ToolError type

# Host closure: query -> {"title": ..., "artist": ...}; raises ScreenToolError
# (SONG_NOT_FOUND / SONG_DISABLED) when it cannot start the song.
RequestSong = Callable[[str], dict[str, Any]]

_BASE_DESCRIPTION = (
    "你可以用自己的声音唱歌。当用户想让你唱一首具体的歌（点名歌名，或歌手+歌名，"
    "如「周杰伦的稻香」）时，调用此工具开始准备演唱。工具会立刻返回找到的歌曲，"
    "演唱需要准备一会儿，你应该自然地回应「去准备了」之类的话，不要解释技术细节。"
)

# 默认装载:模糊请求先确认(避免盲搜 30-60s 沉没成本)。真机调教时可换 DIRECT 版。
DESCRIPTION_CONFIRM_FIRST = _BASE_DESCRIPTION + (
    "只在用户给出明确歌名时调用；如果用户只说「唱首歌」「来点伤感的」这类没有具体"
    "歌名的请求，不要调用工具，先在对话里问清想听哪一首再调用。"
)

# 备选:模糊请求直接以描述词搜索(体验更顺,代价是搜错歌的沉没成本)。
DESCRIPTION_DIRECT_SEARCH = _BASE_DESCRIPTION + (
    "用户给出明确歌名时直接调用；用户只给风格/情绪（如「来点伤感的」）时,也可以把"
    "描述词作为 query 调用,由搜索挑选最接近的歌曲,并在回应里说出找到的是哪首。"
)

SING_SONG_SCHEMA: dict[str, Any] = {
    "type": "function",
    "name": "sing_song",
    "strict": True,
    "description": DESCRIPTION_CONFIRM_FIRST,
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "要唱的歌：歌名，或「歌手 歌名」（如「周杰伦 稻香」）。",
            },
        },
        "required": ["query"],
        "additionalProperties": False,
    },
}


class SingSongTool:
    """``ToolPort`` for ``sing_song`` (registered like the other tool shims)."""

    name = "sing_song"

    def __init__(self, request_song: RequestSong) -> None:
        self._request_song = request_song

    def schema(self) -> dict[str, Any]:
        return SING_SONG_SCHEMA

    def run(self, *, query: str = "") -> dict[str, Any]:
        query = (query or "").strip()
        if not query:
            raise ScreenToolError("SONG_QUERY_EMPTY", "要唱的歌名为空。")
        resolved = self._request_song(query)
        # Tiny envelope on purpose: it lands verbatim in the followup prompt --
        # just enough for her to NAME the song in the acknowledgment.
        return {
            "started": True,
            "title": resolved.get("title"),
            "artist": resolved.get("artist"),
        }

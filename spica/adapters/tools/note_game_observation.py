"""note_game_observation: store a dialogue-confirmed observation into the
companion's GAME memory (Phase 9 step 2 -- the write-back half of "看角色特征→加入记忆").

A pure WRITE shim -- no capture, no analysis, no ScreenAnalysisPort dependency.
The LLM passes the observation text already confirmed in dialogue (the user saw
her description and asked to record it); the tool never re-screenshots, so what
gets stored is the dialogue consensus, not a fresh unverified Moondream frame.
Granularity rules (one sentence, summary-level looks, no invented precision)
live in the schema description -- same description-driven discipline as watch.

Write authority stays with the HOST (CLAUDE.md #8): the injected ``record``
closure builds and persists the CompanionBeat against the game-memory adapter;
this adapter only forwards text. Companion awareness mirrors watch_game_screen:
an injected LAZY provider returns the live play's binding or ``None``; not
playing -> NO_ACTIVE_COMPANION tool error. Qt-free (CLAUDE.md #1).
"""

from __future__ import annotations

from typing import Any, Callable

from agent_tools.function_tools.screen.schema import ScreenToolError

# The published GameTurnBinding of the CURRENT companion play, or None.
NoteContextProvider = Callable[[], Any | None]
# Host closure: dialogue-confirmed observation text -> persisted beat_id.
RecordObservation = Callable[[str], str]

NOTE_GAME_OBSERVATION_SCHEMA: dict[str, Any] = {
    "type": "function",
    "name": "note_game_observation",
    "strict": True,
    "description": (
        "你正在陪用户玩 galgame。当用户明确表示要记住/记录刚才在游戏里看到的内容"
        "（角色特征、画面观察、共同的发现）时，把对话中已确认的、概括级的游戏观察"
        "存入你们这局游戏的共同记忆。一句话；角色外观存概括（如「穿校服的短发女孩」），"
        "不编造精确细节（如瞳色色值）；仅存对话中已确认过的内容，不在没看过画面的"
        "回合凭空记录。只在用户明确想记的时候调用，不要自动调用。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "observation": {
                "type": "string",
                "description": "要记录的观察内容：一句话，概括级，来自对话中已确认的信息。",
            },
        },
        "required": ["observation"],
        "additionalProperties": False,
    },
}


class NoteGameObservationTool:
    """``ToolPort`` for ``note_game_observation`` (registered like WatchGameScreenTool)."""

    name = "note_game_observation"

    def __init__(self, game_context: NoteContextProvider, record: RecordObservation) -> None:
        self._game_context = game_context
        self._record = record

    def schema(self) -> dict[str, Any]:
        return NOTE_GAME_OBSERVATION_SCHEMA

    def run(self, *, observation: str = "") -> dict[str, Any]:
        observation = (observation or "").strip()
        if self._game_context() is None:
            raise ScreenToolError(
                "NO_ACTIVE_COMPANION",
                "当前没有正在陪玩的游戏，无法记录游戏观察。要先开始陪玩。",
            )
        if not observation:
            raise ScreenToolError("NOTE_OBSERVATION_EMPTY", "要记录的观察内容为空。")
        beat_id = self._record(observation)
        # Deliberately tiny: this lands verbatim in the followup prompt, and a
        # note's job is done once persisted (特判二 stays un-expanded).
        return {"recorded": True, "beat_id": beat_id}

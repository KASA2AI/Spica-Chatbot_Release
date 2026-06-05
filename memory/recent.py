from __future__ import annotations

from collections import defaultdict, deque
from typing import Any


class RecentMemory:
    def __init__(self, max_turns: int = 3):
        self.max_turns = max(1, int(max_turns))
        self._turns: dict[str, deque[dict[str, Any]]] = defaultdict(lambda: deque(maxlen=self.max_turns))

    def get_recent(self, conversation_id: str, limit: int = 3) -> list[dict[str, Any]]:
        turns = list(self._turns.get(conversation_id, []))
        return turns[-max(1, int(limit)) :]

    def append_turn(
        self,
        conversation_id: str,
        user_text: str,
        assistant_text: str,
        user_local_time: str | None = None,
        interaction_mode: str = "chat",
    ) -> None:
        turn = {
            "user_text": user_text,
            "assistant_text": assistant_text,
            "interaction_mode": interaction_mode,
        }
        if user_local_time:
            turn["user_local_time"] = user_local_time
        self._turns[conversation_id].append(turn)

    def clear(self, conversation_id: str) -> None:
        self._turns.pop(conversation_id, None)

    def dump(self) -> dict[str, Any]:
        return {key: list(value) for key, value in self._turns.items()}

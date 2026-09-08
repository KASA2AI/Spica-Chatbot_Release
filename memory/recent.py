from __future__ import annotations

from collections import defaultdict, deque
import json
import logging
import threading
from pathlib import Path
from typing import Any


class RecentMemory:
    def __init__(self, max_turns: int = 3, *, path: Path | None = None, key_prefix: str = ""):
        self.max_turns = max(1, int(max_turns))
        self._turns: dict[str, deque[dict[str, Any]]] = defaultdict(lambda: deque(maxlen=self.max_turns))
        self._path = path
        self._key_prefix = key_prefix
        self._lock = threading.RLock()
        if path is not None and path.is_file():
            try:
                self.restore(json.loads(path.read_text(encoding="utf-8")), persist=False)
            except (OSError, ValueError, TypeError):
                logging.getLogger(__name__).warning("recent memory could not be restored")

    def get_recent(self, conversation_id: str, limit: int = 3) -> list[dict[str, Any]]:
        with self._lock:
            turns = list(self._turns.get(conversation_id, []))
        return turns[-max(1, int(limit)) :]

    def append_turn(
        self,
        conversation_id: str,
        user_text: str,
        assistant_text: str,
        user_local_time: str | None = None,
        interaction_mode: str = "chat",
        screen_observation_context: str | None = None,
    ) -> None:
        turn = {
            "user_text": user_text,
            "assistant_text": assistant_text,
            "interaction_mode": interaction_mode,
        }
        if user_local_time:
            turn["user_local_time"] = user_local_time
        if screen_observation_context:
            turn["screen_observation_context"] = screen_observation_context
        with self._lock:
            self._turns[conversation_id].append(turn)
            self._persist()

    def clear(self, conversation_id: str) -> None:
        with self._lock:
            self._turns.pop(conversation_id, None)
            self._persist()

    def dump(self) -> dict[str, Any]:
        with self._lock:
            return {key: [dict(turn) for turn in value] for key, value in self._turns.items()}

    def restore(self, data: dict[str, Any], *, persist: bool = True) -> None:
        if not isinstance(data, dict):
            raise ValueError("recent memory must be an object")
        restored = {}
        for key, turns in data.items():
            if not isinstance(key, str) or not key.startswith(self._key_prefix) or not isinstance(turns, list):
                raise ValueError("recent memory scope mismatch")
            if any(not isinstance(turn, dict) or not isinstance(turn.get("user_text"), str)
                   or not isinstance(turn.get("assistant_text"), str) for turn in turns):
                raise ValueError("invalid recent turn")
            restored[key] = deque((dict(turn) for turn in turns[-self.max_turns:]), maxlen=self.max_turns)
        with self._lock:
            self._turns.update(restored)
            if persist:
                self._persist()

    def _persist(self) -> None:
        if self._path is None:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self._path.with_suffix(".tmp")
            saved = {key: [turn for turn in turns if turn.get("interaction_mode", "chat") == "chat"]
                     for key, turns in self.dump().items()}
            temporary.write_text(json.dumps({key: turns for key, turns in saved.items() if turns}, ensure_ascii=False), encoding="utf-8")
            temporary.replace(self._path)
        except OSError:
            logging.getLogger(__name__).warning("recent memory could not be saved")

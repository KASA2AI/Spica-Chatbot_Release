from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv
from openai import OpenAI

from agent.character_loader import (
    DEFAULT_INTERLOCUTOR_NAME,
    DEFAULT_SPICA_SKILL_DIR,
    load_spica_character_profile,
    normalize_interlocutor_name,
    replace_mugi_references,
)
from memory.store import SQLiteMemoryStore
from agent.prompt_builder import DEFAULT_CHARACTER_PROFILE
from memory.recent import RecentMemory
from agent.reply_parser import EMOTION_LABELS, guess_emotion, normalize_emotion, parse_model_reply
from agent.runtime import run_voice_pipeline
from agent.state import AgentServices, AgentState
from agent.streaming_pipeline import stream_voice_events
from common.timing import log_timing
from agent_tools.function_tools import TOOL_SCHEMAS, default_tool_functions
from agent_tools.tts.adapters.gptsovits_current import CurrentGPTSoVITSAdapter
from agent_tools.tts.base import TTSAdapter


BASE_DIR = Path(__file__).resolve().parents[1]

load_dotenv(BASE_DIR / "xiaosan.env")
load_dotenv(BASE_DIR.parent / "xiaosan.env", override=False)


class SimpleAgent:
    """Thin facade around the explicit voice pipeline."""

    def __init__(
        self,
        tts_adapter: TTSAdapter | None = None,
        visual_tool: Any | None = None,
        tts_tool: Any | None = None,
    ):
        self.model = os.getenv("MODEL") or "gpt-4.1-mini"
        self.api_key = os.getenv("OPENAI_API_KEY")
        self.base_url = os.getenv("OPENAI_BASE_URL") or None
        self.tts_adapter = tts_adapter or (CurrentGPTSoVITSAdapter(tts_tool) if tts_tool is not None else None)
        self.visual_tool = visual_tool

        if not self.api_key:
            raise ValueError("没有读取到 OPENAI_API_KEY，请检查 xiaosan.env")

        http_client = httpx.Client(trust_env=False, timeout=15)
        self.client = OpenAI(
            api_key=self.api_key,
            base_url=self.base_url,
            http_client=http_client,
        )

        self.recent_memory = RecentMemory(max_turns=int(os.getenv("RECENT_MEMORY_TURNS") or 3))
        self.memory_store = SQLiteMemoryStore(BASE_DIR / "spica_data" / "memory.sqlite3")
        self.tool_functions = default_tool_functions()
        self.interlocutor_name = normalize_interlocutor_name(os.getenv("SPICA_USER_NAME") or DEFAULT_INTERLOCUTOR_NAME)
        self.character_profile = self._load_character_profile()
        self.services = self._build_services()

    def _load_character_profile(self) -> str:
        override = os.getenv("SPICA_CHARACTER_PROFILE")
        if override:
            return replace_mugi_references(override, self.interlocutor_name)

        configured_skill_dir = os.getenv("SPICA_SKILL_DIR")
        skill_dir = Path(configured_skill_dir) if configured_skill_dir else DEFAULT_SPICA_SKILL_DIR
        if not skill_dir.is_absolute():
            skill_dir = BASE_DIR / skill_dir
        loaded_profile = load_spica_character_profile(skill_dir, interlocutor_name=self.interlocutor_name)
        return loaded_profile or DEFAULT_CHARACTER_PROFILE

    def _build_services(self) -> AgentServices:
        return AgentServices(
            llm_client=self.client,
            tts_adapter=self.tts_adapter,
            visual_tool=self.visual_tool,
            memory_store=self.memory_store,
            recent_memory=self.recent_memory,
            config={
                "model": self.model,
                "character_profile": self.character_profile,
                "interlocutor_name": self.interlocutor_name,
                "recent_context_limit": int(os.getenv("RECENT_CONTEXT_LIMIT") or 3),
                "long_term_memory_limit": int(os.getenv("LONG_TERM_MEMORY_LIMIT") or 5),
                "long_term_memory_budget_chars": int(os.getenv("LONG_TERM_MEMORY_BUDGET_CHARS") or 1200),
                "recent_turn_char_limit": int(os.getenv("RECENT_TURN_CHAR_LIMIT") or 360),
                "max_long_term_memories": int(os.getenv("MAX_LONG_TERM_MEMORIES") or 200),
                "max_tool_rounds": int(os.getenv("MAX_TOOL_ROUNDS") or 3),
            },
            logger=log_timing,
            tool_functions=self.tool_functions,
            tool_schemas=TOOL_SCHEMAS,
        )

    def set_interlocutor_name(self, name: str) -> str:
        self.interlocutor_name = normalize_interlocutor_name(name)
        self.character_profile = self._load_character_profile()
        self.services.config["interlocutor_name"] = self.interlocutor_name
        self.services.config["character_profile"] = self.character_profile
        return self.interlocutor_name

    def set_visual_tool(self, visual_tool: Any | None) -> None:
        self.visual_tool = visual_tool
        self.services.visual_tool = visual_tool

    def run_voice(
        self,
        user_input: str,
        conversation_id: str = "default",
        emotion_override: str | None = None,
        tts_param_overrides: dict[str, Any] | None = None,
        visual_overrides: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        state = AgentState(
            conversation_id=conversation_id or "default",
            user_input=user_input or "",
            emotion_override=emotion_override,
            tts_param_overrides=tts_param_overrides,
            visual_overrides=visual_overrides or {},
        )
        state = run_voice_pipeline(state, self.services)
        return state.response_payload

    def run(self, user_input: str, conversation_id: str = "default") -> str:
        result = self.run_voice(user_input, conversation_id=conversation_id)
        return str(result.get("answer") or "")

    def stream_voice(
        self,
        user_input: str,
        conversation_id: str = "default",
        emotion_override: str | None = None,
        tts_param_overrides: dict[str, Any] | None = None,
        visual_overrides: dict[str, Any] | None = None,
    ):
        state = AgentState(
            conversation_id=conversation_id or "default",
            user_input=user_input or "",
            emotion_override=emotion_override,
            tts_param_overrides=tts_param_overrides,
            visual_overrides=visual_overrides or {},
        )
        return stream_voice_events(state, self.services)

    def clear_memory(self, conversation_id: str = "default", clear_long_term: bool = False) -> dict[str, Any]:
        self.recent_memory.clear(conversation_id)
        cleared = {"recent_memory": True, "long_term_memory": False}
        if clear_long_term:
            self.memory_store.clear_memories(conversation_id)
            cleared["long_term_memory"] = True
        return {"ok": True, "conversation_id": conversation_id, "cleared": cleared}

    def list_memory(self, conversation_id: str = "default", limit: int = 50) -> list[dict[str, Any]]:
        return self.memory_store.list_memories(conversation_id, limit=limit)

    def remember(
        self,
        content: str,
        conversation_id: str = "default",
        scope: str = "user",
        importance: float = 0.8,
    ) -> int:
        return self.memory_store.upsert_memory(
            conversation_id=conversation_id,
            scope=scope,
            content=content,
            importance=importance,
            source="manual",
        )

    def forget_memory(self, memory_id: int) -> None:
        self.memory_store.delete_memory(memory_id)

    def parse_model_reply(self, output_text: str) -> dict[str, str]:
        return parse_model_reply(output_text)

    def normalize_emotion(self, emotion: str | None) -> str:
        return normalize_emotion(emotion)

    def guess_emotion(self, text: str) -> str:
        return guess_emotion(text)


__all__ = ["SimpleAgent", "EMOTION_LABELS"]

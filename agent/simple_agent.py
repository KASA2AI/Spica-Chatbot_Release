from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
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
from spica.config.manager import ConfigManager
from spica.config.schema import AppConfig
from spica.config.secrets import Secrets, load_secrets
from spica.core.events import event_from_legacy
from agent_tools.function_tools import TOOL_SCHEMAS, default_tool_functions
from agent_tools.tts.adapters.gptsovits_current import CurrentGPTSoVITSAdapter
from agent_tools.tts.base import TTSAdapter


BASE_DIR = Path(__file__).resolve().parents[1]


class SimpleAgent:
    """Thin facade around the explicit voice pipeline."""

    def __init__(
        self,
        tts_adapter: TTSAdapter | None = None,
        visual_tool: Any | None = None,
        tts_tool: Any | None = None,
        config: AppConfig | None = None,
        secrets: Secrets | None = None,
    ):
        # Configuration now comes from the typed config layer instead of direct
        # env reads (Phase 3). Callers without a Host (e.g. examples/llm_demo)
        # pass nothing and get the default ConfigManager / secrets.
        self.config = config if config is not None else ConfigManager().load()
        self.secrets = secrets if secrets is not None else load_secrets()
        self.model = self.config.llm.model
        self.api_key = self.secrets.openai_api_key
        self.base_url = self.config.llm.base_url
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

        self.recent_memory = RecentMemory(max_turns=self.config.memory.recent_memory_turns)
        self.memory_store = SQLiteMemoryStore(BASE_DIR / "spica_data" / "memory.sqlite3")
        self.tool_functions = default_tool_functions()
        self.interlocutor_name = normalize_interlocutor_name(
            self.config.character.interlocutor_name or DEFAULT_INTERLOCUTOR_NAME
        )
        self.character_profile = self._load_character_profile()
        self.services = self._build_services()

    def _load_character_profile(self) -> str:
        override = self.config.character.profile_override
        if override:
            return replace_mugi_references(override, self.interlocutor_name)

        configured_skill_dir = self.config.character.skill_dir
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
                "recent_context_limit": self.config.memory.recent_context_limit,
                "long_term_memory_limit": self.config.memory.long_term_memory_limit,
                "long_term_memory_budget_chars": self.config.memory.long_term_memory_budget_chars,
                "recent_turn_char_limit": self.config.memory.recent_turn_char_limit,
                "max_long_term_memories": self.config.memory.max_long_term_memories,
                "max_tool_rounds": self.config.max_tool_rounds,
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
        include_user_time_context: bool = True,
        interaction_mode: str = "chat",
        screen_attachment: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        state = AgentState(
            conversation_id=conversation_id or "default",
            user_input=user_input or "",
            include_user_time_context=include_user_time_context,
            interaction_mode=interaction_mode,
            emotion_override=emotion_override,
            tts_param_overrides=tts_param_overrides,
            visual_overrides=visual_overrides or {},
            screen_attachment=screen_attachment,
        )
        state = run_voice_pipeline(state, self.services)
        return state.response_payload

    def run(self, user_input: str, conversation_id: str = "default") -> str:
        result = self.run_voice(user_input, conversation_id=conversation_id)
        return str(result.get("answer") or "")

    def stream_voice_runtime(
        self,
        user_input: str,
        conversation_id: str = "default",
        emotion_override: str | None = None,
        tts_param_overrides: dict[str, Any] | None = None,
        visual_overrides: dict[str, Any] | None = None,
        include_user_time_context: bool = True,
        interaction_mode: str = "chat",
        screen_attachment: dict[str, Any] | None = None,
    ):
        """Stream typed ``RuntimeEvent``s (Phase 6A) -- the same data as
        ``stream_voice``, adapted at the pipeline's output boundary."""
        state = AgentState(
            conversation_id=conversation_id or "default",
            user_input=user_input or "",
            include_user_time_context=include_user_time_context,
            interaction_mode=interaction_mode,
            emotion_override=emotion_override,
            tts_param_overrides=tts_param_overrides,
            visual_overrides=visual_overrides or {},
            screen_attachment=screen_attachment,
        )
        for event in stream_voice_events(state, self.services):
            yield event_from_legacy(event)

    def stream_voice(
        self,
        user_input: str,
        conversation_id: str = "default",
        emotion_override: str | None = None,
        tts_param_overrides: dict[str, Any] | None = None,
        visual_overrides: dict[str, Any] | None = None,
        include_user_time_context: bool = True,
        interaction_mode: str = "chat",
        screen_attachment: dict[str, Any] | None = None,
    ):
        """Stream legacy dict events for the current UI. This is the reverse
        adapter (Phase 6A): dicts flow out through ``RuntimeEvent`` and back, so
        the boundary is now the typed event while ``ChatStreamController`` keeps
        consuming dicts unchanged."""
        for event in self.stream_voice_runtime(
            user_input,
            conversation_id=conversation_id,
            emotion_override=emotion_override,
            tts_param_overrides=tts_param_overrides,
            visual_overrides=visual_overrides,
            include_user_time_context=include_user_time_context,
            interaction_mode=interaction_mode,
            screen_attachment=screen_attachment,
        ):
            yield event.to_legacy_dict()

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

"""Backend assembly (Phase 6D).

Builds the ``AgentServices`` bundle (LLM client, memory, character profile, tool
functions, config dict) that the conversation core runs on. This is the
assembly half of the dissolved ``SimpleAgent`` and belongs to the host
(composition root); the driving / management half is ``ChatEngine``.

INVARIANT (CLAUDE.md #1 + #4): Qt-free; secrets come from the secrets loader.
"""

from __future__ import annotations

import logging
from pathlib import Path

import httpx
from openai import OpenAI

from spica.conversation.character_loader import (
    DEFAULT_CHARACTER_NAME,
    DEFAULT_INTERLOCUTOR_NAME,
    build_character_profile,
    normalize_interlocutor_name,
)
from spica.runtime.services import AgentServices
from agent_tools.function_tools import TOOL_SCHEMAS, default_tool_functions
from common.timing import log_timing
from memory.recent import RecentMemory
from memory.store import SQLiteMemoryStore
from spica.adapters.game_memory import GameMemorySqliteAdapter
from spica.adapters.game_launcher import LinuxDesktopGameLauncher
from spica.adapters.window_locator import LinuxX11WindowLocator
from spica.adapters.screen_capture import MssScreenCapture
from spica.adapters.ocr import RapidOcrAdapter, RapidOcrOrtAdapter
from spica.config.schema import AppConfig
from spica.config.secrets import Secrets

_REPO_ROOT = Path(__file__).resolve().parents[2]
_LOGGER = logging.getLogger(__name__)


def build_ocr_adapter(provider: str = "rapidocr", fallback_provider: str | None = "rapidocr"):
    """Select the OCR ``OCRPort`` implementation by provider name (LOCAL_RUNTIME_PLAN
    §2.3 / §11). The SINGLE source of OCR provider selection -- the same returned
    adapter drives BOTH paths (galgame loop here via ``ocr_adapter``; inspect_screen
    via the path-B install hook in app_host), so they never fork (§2.2).

    Default ``rapidocr`` returns ``RapidOcrAdapter()`` -- byte-identical to before.
    Unknown / reserved-but-not-live names (e.g. ``rapidocr_trt_ep``, the step-2 TRT
    EP) fall back to ``fallback_provider`` with a warning, so a mis-set config
    degrades gracefully instead of crashing startup. The default is NOT switched
    away from ``rapidocr`` this cut -- that needs a parity report (§6.1)."""
    name = (provider or "rapidocr").strip()
    builders = {
        "rapidocr": RapidOcrAdapter,
        "rapidocr_ort": RapidOcrOrtAdapter,
        # "rapidocr_trt_ep": reserved for step 2 (ORT TensorRT EP). Not live yet;
        # selecting it falls back below until the engine-cache path is implemented.
    }
    builder = builders.get(name)
    if builder is None:
        if fallback_provider and fallback_provider != name:
            _LOGGER.warning(
                "unknown/unavailable OCR provider %r; falling back to %r", name, fallback_provider
            )
            return build_ocr_adapter(fallback_provider, fallback_provider=None)
        _LOGGER.warning("unknown OCR provider %r and no fallback; using rapidocr", name)
        return RapidOcrAdapter()
    return builder()


def build_llm_client(api_key: str, base_url: str | None, timeout: float = 15) -> OpenAI:
    """One OpenAI-compatible client. Shared by the main chat/summary client and the
    reaction judge's separate-key client (so the construction stays single-sourced).
    Construction is network-free -- a bad key fails on the first call, not here."""
    return OpenAI(
        api_key=api_key,
        base_url=base_url,
        http_client=httpx.Client(trust_env=False, timeout=timeout),
    )


def build_agent_services(
    config: AppConfig,
    secrets: Secrets,
    *,
    tts_adapter=None,
    visual_tool=None,
    character_package=None,
) -> AgentServices:
    api_key = secrets.openai_api_key
    if not api_key:
        raise ValueError("没有读取到 OPENAI_API_KEY，请检查 xiaosan.env")

    client = build_llm_client(api_key, config.llm.base_url)
    interlocutor_name = normalize_interlocutor_name(
        config.character.interlocutor_name or DEFAULT_INTERLOCUTOR_NAME
    )
    # Active character identity comes from the CharacterPackage (Phase 7);
    # falling back to Spica defaults when no package is supplied.
    if character_package is not None:
        character_id = character_package.character_id
        character_name = character_package.char_name
        skill_dir = character_package.skill_dir
    else:
        character_id = "spica"
        character_name = DEFAULT_CHARACTER_NAME
        skill_dir = config.character.skill_dir
    character_profile = build_character_profile(
        config.character.profile_override,
        skill_dir,
        interlocutor_name,
    )
    # Record the resolved character identity on the typed config so TurnDeps reads
    # the same normalized values the legacy services.config dict carries (C3b/C4).
    config.character.interlocutor_name = interlocutor_name
    config.character.character_id = character_id
    config.character.character_profile = character_profile
    config.character.character_name = character_name
    # Single data root, host-resolved (NOT cwd-relative). The galgame store shares
    # this root with the character memory store -- a separate file, never the same
    # DB (CLAUDE.md #1.8). No config knob this phase; mirrors the memory.sqlite3 path.
    data_dir = _REPO_ROOT / "spica_data"
    return AgentServices(
        llm_client=client,
        tts_adapter=tts_adapter,
        visual_tool=visual_tool,
        memory_store=SQLiteMemoryStore(data_dir / "memory.sqlite3"),
        recent_memory=RecentMemory(max_turns=config.memory.recent_memory_turns),
        game_memory_adapter=GameMemorySqliteAdapter(data_dir / "galgame.sqlite3"),
        # Phase 5: galgame launch + window-binding adapters (linux/Bottles path).
        game_launcher_adapter=LinuxDesktopGameLauncher(),
        window_locator_adapter=LinuxX11WindowLocator(),
        # Phase 6: galgame screen capture (mss) + OCR (RapidOCR bridge, shared engine).
        # cut 1: OCR provider selected by config via the factory (default rapidocr
        # = byte-identical). The SAME adapter is installed into the path-B hook in
        # app_host so galgame OCR and inspect_screen never fork (LOCAL_RUNTIME_PLAN §2.2).
        screen_capture_adapter=MssScreenCapture(),
        ocr_adapter=build_ocr_adapter(config.ocr.provider, config.ocr.fallback_provider),
        config={
            "model": config.llm.model,
            "character_profile": character_profile,
            "interlocutor_name": interlocutor_name,
            "recent_context_limit": config.memory.recent_context_limit,
            "long_term_memory_limit": config.memory.long_term_memory_limit,
            "long_term_memory_budget_chars": config.memory.long_term_memory_budget_chars,
            "recent_turn_char_limit": config.memory.recent_turn_char_limit,
            "max_long_term_memories": config.memory.max_long_term_memories,
            "max_tool_rounds": config.max_tool_rounds,
            "play_unit_min_chars": config.stream.play_unit_min_chars,
            "play_unit_max_chars": config.stream.play_unit_max_chars,
            "visual_stream_workers": config.stream.visual_stream_workers,
            "character_id": character_id,
            "character_name": character_name,
        },
        logger=log_timing,
        tool_functions=default_tool_functions(),
        tool_schemas=TOOL_SCHEMAS,
    )

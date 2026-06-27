"""P0b env-variable roster -- the single home of every env name the code touches.

Pure constants, ZERO behaviour (no ``os`` access, so ``test_no_getenv`` stays
green by construction). Three consumers import this same roster, which is what
keeps them from drifting apart:

- ``spica/config/manager.py`` -- the domain override functions (P0b step 1)
  build their env reads from the maps here;
- ``scripts/dump_resolved_config.py`` (Layer A snapshot) -- masks and
  attributes env provenance from the same maps;
- ``tests/test_resolved_config_equivalence.py`` (Layer B) -- masks the same
  names, and meta-checks that the config layer's source mentions no env name
  missing from this roster.

Map shape: ``{config_field_path: ENV_VAR_NAME}`` per domain. Field paths in
``APP_ENV_MAP`` are dotted into the ``AppConfig`` shape.
"""

from __future__ import annotations

# -- consumed: read as configuration ------------------------------------------

APP_ENV_MAP: dict[str, str] = {
    # mirrors ConfigManager._env_overrides (manager.py)
    "llm.model": "MODEL",
    "llm.base_url": "OPENAI_BASE_URL",
    "memory.recent_memory_turns": "RECENT_MEMORY_TURNS",
    "memory.recent_context_limit": "RECENT_CONTEXT_LIMIT",
    "memory.long_term_memory_limit": "LONG_TERM_MEMORY_LIMIT",
    "memory.long_term_memory_budget_chars": "LONG_TERM_MEMORY_BUDGET_CHARS",
    "memory.recent_turn_char_limit": "RECENT_TURN_CHAR_LIMIT",
    "memory.max_long_term_memories": "MAX_LONG_TERM_MEMORIES",
    "character.interlocutor_name": "SPICA_USER_NAME",
    "character.profile_override": "SPICA_CHARACTER_PROFILE",
    "character.skill_dir": "SPICA_SKILL_DIR",
    "stream.play_unit_min_chars": "PLAY_UNIT_MIN_CHARS",
    "stream.play_unit_max_chars": "PLAY_UNIT_MAX_CHARS",
    "stream.visual_stream_workers": "VISUAL_STREAM_WORKERS",
    "max_tool_rounds": "MAX_TOOL_ROUNDS",
    # Main-LLM reasoning/thinking control (deepseek thinking off / gpt effort).
    "llm.reasoning_effort": "REASONING_EFFORT",
    # Reaction-judge LLM endpoint (the ONLY galgame fields with env names -- judge
    # is a swappable LLM endpoint, unlike galgame's yaml-only tuning knobs). The
    # key half is the secret JUDGE_API_KEY (SECRETS_ENV_MAP); these are the
    # non-secret base_url + model + reasoning. All OpenAI-compatible (deepseek/...).
    "galgame.reaction_judge_base_url": "JUDGE_BASE_URL",
    "galgame.reaction_judge_model": "JUDGE_MODEL",
    "galgame.reaction_judge_reasoning_effort": "JUDGE_REASONING_EFFORT",
}

SECRETS_ENV_MAP: dict[str, str] = {
    "openai_api_key": "OPENAI_API_KEY",
    # Separate key for the reaction-judge LLM endpoint, so the judge's load never
    # saturates the main chat/summary endpoint (they share one key otherwise).
    # Vendor-neutral name -- the judge endpoint is any OpenAI-compatible provider
    # (deepseek/OpenAI/...; the base_url + model are JUDGE_BASE_URL / JUDGE_MODEL
    # below). Unset -> judge falls back to OPENAI_API_KEY (zero behaviour change).
    "judge_api_key": "JUDGE_API_KEY",
}

SCREEN_ENV_MAP: dict[str, str] = {
    # mirrors agent_tools/function_tools/screen/config.py field order
    "enabled": "SPICA_SCREEN_ENABLED",
    "provider": "SPICA_SCREEN_PROVIDER",
    "model_id": "SPICA_SCREEN_MODEL_ID",
    "revision": "SPICA_SCREEN_REVISION",
    "device": "SPICA_SCREEN_DEVICE",
    "dtype": "SPICA_SCREEN_DTYPE",
    "max_side": "SPICA_SCREEN_MAX_SIDE",
    "reasoning": "SPICA_SCREEN_REASONING",
    "preload": "SPICA_SCREEN_PRELOAD",
    "ocr_enabled": "SPICA_SCREEN_OCR_ENABLED",
    "ocr_engine": "SPICA_SCREEN_OCR_ENGINE",
    "capture_format": "SPICA_SCREEN_CAPTURE_FORMAT",
    "infer_timeout_sec": "SPICA_SCREEN_INFER_TIMEOUT_SEC",
    "log_timing": "SPICA_SCREEN_LOG_TIMING",
    "debug_save_images": "SPICA_SCREEN_DEBUG_SAVE",
}

RUNTIME_CACHE_ENV_MAP: dict[str, str] = {
    "cache_root": "SPICA_RUNTIME_CACHE_DIR",
}

RESPEAKER_ENV_MAP: dict[str, str] = {
    "tuning_path": "RESPEAKER_TUNING_PATH",
    "require_hardware_vad": "RESPEAKER_REQUIRE_HARDWARE_VAD",
    "input_device_index": "RESPEAKER_INPUT_DEVICE_INDEX",
    # Trailing-silence (seconds) the hardware-VAD loop waits before declaring the
    # utterance finished. Raise it if slow speech / mid-sentence pauses get cut off;
    # lower it if she feels slow to respond after you stop. Coerced in
    # hardware/respeaker/audio.py (default DEFAULT_END_SILENCE_SECONDS).
    "end_silence_seconds": "RESPEAKER_END_SILENCE_SECONDS",
}

# -- legacy: present in xiaosan.env history, consumed by NOTHING since B2
# deleted the second LLM classifier. load_secrets() warns when these are still
# set (P0b step 1); delete the lines from xiaosan.env.

LEGACY_ENV_VARS: tuple[str, ...] = ("DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL")

# -- written/stripped: process-env mutations for the vendored GPT-SoVITS/HF
# runtime (spica/config/runtime_env.py). Not configuration reads; listed so the
# roster meta-check can account for every env name the config layer mentions.

WRITTEN_ENV_VARS: tuple[str, ...] = (
    "HF_HOME",
    "NUMBA_CACHE_DIR",
    "MPLCONFIGDIR",
    "XDG_CACHE_HOME",
)
STRIPPED_ENV_VARS: tuple[str, ...] = (
    "ALL_PROXY",
    "HTTP_PROXY",
    "HTTPS_PROXY",
)


def consumed_env_names() -> frozenset[str]:
    """Every env name read as configuration (the Layer A/B mask list)."""
    names: set[str] = set()
    for mapping in (
        APP_ENV_MAP,
        SECRETS_ENV_MAP,
        SCREEN_ENV_MAP,
        RUNTIME_CACHE_ENV_MAP,
        RESPEAKER_ENV_MAP,
    ):
        names.update(mapping.values())
    return frozenset(names)

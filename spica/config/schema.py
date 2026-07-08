"""Typed application configuration (Phase 3).

Pydantic models for every tunable knob the conversation core reads. Defaults
match the historical ``os.getenv(...) or N`` fallbacks exactly, so building an
``AppConfig`` with no env and no file reproduces today's behaviour.

INVARIANT (CLAUDE.md #1 + #4): this layer is Qt-free and -- together with
``manager.py`` / ``secrets.py`` -- is the only place allowed to source
configuration. It must NOT import the ``agent`` package: agent-specific defaults
(interlocutor name, skill dir) are applied in ``agent`` so this layer stays
character-agnostic and there is no ``agent -> spica.config -> agent`` cycle.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator


class LLMConfig(BaseModel):
    provider: str = "openai_compatible"
    model: str = "gpt-4.1-mini"
    base_url: str | None = None
    # Reasoning/thinking control for the MAIN chat+summary LLM. env REASONING_EFFORT.
    # Values: default | none | low | medium | high.
    #   "default" -> send NO reasoning param (provider's own default; zero-diff).
    #   deepseek-* (thinking is BINARY): "none" disables thinking; low/medium/high
    #     all just leave it ON (deepseek has no gradient).
    #   gpt-*    : reasoning_effort = none/low/medium/high (a real gradient).
    # Disabling deepseek thinking is a big latency cut (~halves first-token).
    reasoning_effort: str = "default"


class MemoryConfig(BaseModel):
    provider: str = "sqlite"
    recent_memory_turns: int = 3
    recent_context_limit: int = 3
    long_term_memory_limit: int = 5
    long_term_memory_budget_chars: int = 1200
    recent_turn_char_limit: int = 360
    max_long_term_memories: int = 200


class CharacterConfig(BaseModel):
    # All optional. When unset, the agent layer applies DEFAULT_INTERLOCUTOR_NAME
    # / DEFAULT_SPICA_SKILL_DIR, keeping this layer character-agnostic.
    interlocutor_name: str | None = None
    profile_override: str | None = None
    skill_dir: str | None = None
    package_dir: str | None = None  # active CharacterPackage dir (Phase 7); None -> Spica
    # Resolved active character id (from the CharacterPackage); None -> "spica".
    # Set by the host after package load so the typed deps namespace memory by it.
    character_id: str | None = None
    # Resolved at assembly time (C4): the built persona text and display name the
    # prompt builder uses. Host writes these back so the turn reads them off
    # deps.config instead of the legacy services.config dict. None -> the prompt
    # builder's DEFAULT_CHARACTER_PROFILE / DEFAULT_CHARACTER_NAME fallback.
    character_profile: str | None = None
    character_name: str | None = None
    # Dialog-box DISPLAY language -- display only, NEVER the spoken language
    # (the voice always stays Japanese; TTS/memory/events keep the Japanese
    # side). "ja" (default): the dialog box shows the spoken Japanese line,
    # byte-identical to the pre-switch behaviour. "zh": the prompt asks for an
    # inline ⟦中文⟧ translation after every Japanese sentence and the dialog box
    # displays that translation instead. yaml-only knob: NO env name (铁律 #4 --
    # nothing added to env_roster). A typo fails loud at startup (Literal).
    dialog_display_language: Literal["ja", "zh"] = "ja"


class StreamConfig(BaseModel):
    play_unit_min_chars: int = 18
    play_unit_max_chars: int = 96
    visual_stream_workers: int = 2


class ReactionTierParams(BaseModel):
    """One reaction tier's gate values (P5 step 4-B). Mirrors the code-side
    ``reaction.ReactionModeParams`` 1:1; fields are REQUIRED (no defaults) so a
    half-filled tier in app.yaml fails loud rather than silently defaulting. Only
    materialized when ``GalgameConfig.reaction_table`` is uncommented (做法X)."""

    min_score: int
    max_per_window: int
    cooldown_seconds: float


class GalgameConfig(BaseModel):
    # Phase 8: galgame story summarization. ``summary_model`` is a dedicated config
    # slot for the summary LLM; None -> fall back to the dialogue model (config.llm),
    # so a future split onto a different model needs no code change. The same
    # endpoint/client is reused either way.
    summary_model: str | None = None
    summary_trigger_chars: int = 2000  # background summary fires ~every this many unsummarized chars
    # OCR sampling interval (seconds) the companion controller hands the OCR loop.
    # 0.3 (not 1.0) so fast page-turns are still sampled often enough to settle a line.
    ocr_interval_seconds: float = 0.3
    # P5 剧情反应系统 (step 4-A). Resolve-once, restart-effective (D-P5-4: the
    # host wraps the resolved tier in a holder lambda -- a future settings panel
    # swaps the holder value, not this field). "off" = engine never attached,
    # zero overhead on the OCR thread. yaml-only knob: NO env name (铁律 #4 --
    # nothing added to env_roster). A typo fails loud at startup (Literal).
    reaction_mode: Literal["off", "low", "normal", "high"] = "off"
    # -- P5 step 4-B real-machine tuning knobs. Every default == the prior
    # hardcoded value, so an un-set app.yaml resolves byte-identical (the code
    # constants stay the source of truth until a key is uncommented). yaml-only
    # (铁律 #4: no env names).
    #
    # reaction_table: per-tier gate table. None (default) -> the code
    # REACTION_MODE_TABLE is the source of truth (做法X: Layer A zero-diff). A
    # provided table replaces matching tiers; reaction_mode still selects which
    # tier is live. Factory: low=5/1/180  normal=4/3/90  high=3/6/45.
    reaction_table: dict[str, ReactionTierParams] | None = None
    # P5 v2 LLM reaction judge (offline-validated; replaces the blind lexicon gate
    # as the LIVE scorer when enabled). TWO fields by design (叉口①):
    # - reaction_judge_enabled: the on/off switch. False (default) -> the lexicon
    #   ``score_beat`` stays the scorer, byte-identical to pre-judge (zero behaviour
    #   diff). True -> selection routes through the LLM judge via the host
    #   ``_reaction_scorer`` seam (the reaction ENGINE is untouched either way).
    # - reaction_judge_model: which model the judge uses; None -> fall back to the
    #   dialogue model (``config.llm.model``), mirroring ``summary_model``. Use a
    #   small/fast tier (the offline report ran deepseek-v4-flash).
    # CALIBRATION (two scales, 叉口②-b): the judge scores on a 0-10 WORTH scale,
    # NOT the lexicon weight-sum scale. When enabling the judge, set
    # ``reaction_table`` to the worth scale (offline start: low=8/normal=7/high=6).
    # The code-side REACTION_MODE_TABLE (low=5/normal=4/high=3) stays the LEXICON
    # scale -- the host closure also uses it as the FALLBACK threshold when a judge
    # call fails, so it must keep the lexicon scale. yaml-only (铁律 #4: no env name).
    reaction_judge_enabled: bool = False
    # -- Reaction-judge LLM ENDPOINT (key + base_url + model). The ONLY galgame
    # fields with env names (JUDGE_MODEL / JUDGE_BASE_URL via APP_ENV_MAP; the key
    # is the secret JUDGE_API_KEY): the judge is a SWAPPABLE LLM endpoint, unlike
    # galgame's yaml-only tuning knobs -- giving it env knobs is the deliberate
    # exception. Run the judge on a SEPARATE endpoint/key so its load does not
    # saturate the main chat/summary endpoint (the deepseek-timeout-under-load root
    # cause). Each falls back to the main LLM independently:
    #   model    -> reaction_judge_model or config.llm.model
    #   base_url -> reaction_judge_base_url or config.llm.base_url
    #   key      -> secrets.judge_api_key, unset -> judge shares the main adapter
    # VENDOR SCOPE: any OpenAI-compatible provider (deepseek/OpenAI/... -- same
    # chat_completions branch, validated cross-vendor). Claude/Anthropic uses the
    # messages API (NOT OpenAI-compatible) -> would need a separate Anthropic
    # client adapter (H1 Anthropic branch); NOT supported by this endpoint yet.
    # RE-VALIDATE ON MODEL CHANGE: the offline selection quality was validated on
    # deepseek-v4-flash; switching JUDGE_MODEL means re-running
    # scripts/reaction_judge_report.py to confirm the new model's pick quality.
    reaction_judge_model: str | None = None
    reaction_judge_base_url: str | None = None
    # Reasoning/thinking control for the JUDGE endpoint, INDEPENDENT of the main
    # LLM (env JUDGE_REASONING_EFFORT). Same vocabulary as llm.reasoning_effort
    # (default | none | low | medium | high; deepseek none=thinking-off binary,
    # gpt = effort gradient). Set "none" to make the judge fast (it was the 18s
    # call under load); keep it on if judge pick quality needs the thinking.
    reaction_judge_reasoning_effort: str = "default"
    reaction_reply_char_limit: int = 40  # 吐槽回复字数上限 (compose_reaction_directive)
    reaction_budget_window_seconds: float = 600.0  # 吐槽频率统计滑窗(秒)
    reaction_excerpt_line_char_limit: int = 60  # 吐槽 directive 剧情摘录单行上限
    reaction_excerpt_total_char_limit: int = 300  # 剧情摘录总字数上限
    # 注入 prompt 的剧情(摘要/选项/陪玩 beat)各取最近几条 -- the former one
    # shared stages._GAME_CONTEXT_RECENT_LIMIT (summaries/choices/beats together).
    prompt_context_recent_limit: int = 5
    # [CURRENT_GAME_BUFFER] tail cap: keep only the last N unsummarized committed
    # lines in the live prompt (the OLDER ones are covered by [RECENT_GAME_SUMMARIES]
    # / [GAME_PROGRESS]). Without this the buffer = ALL unsummarized lines, so a
    # summary failure (or a fast player) lets it grow unbounded -> a 28k-char prompt
    # -> ~6s first-token. 0 (default) == NO cap == byte-identical to pre-cap (Layer
    # zero-diff when unset); app.yaml sets a real cap. Only caps the PROMPT view --
    # the summarizer (summarizer.py) still reads ALL unsummarized lines. yaml-only
    # (铁律 #4: no env name).
    game_buffer_tail_limit: int = 0
    # 履历卡硬字数上限 (history.CARD_MAX_CHARS). 注: prompt_builder._compact_text
    # 另在 220 截断,设高于 220 需同改那处才真正变长.
    play_history_card_max_chars: int = 220


# -- screen section coercion helpers (P0b step 2a) -----------------------------
# Moved VERBATIM from agent_tools/function_tools/screen/config.py so the typed
# section below is the ONE coercion implementation (the screen loader routes
# through ScreenConfig.model_validate; Layer B pins every branch).


def _normalize_dtype(value: str) -> str:
    normalized = (value or "").strip().lower()
    return normalized if normalized in {"bfloat16", "float16", "float32", "auto"} else "auto"


def _normalize_capture_format(value: str) -> str:
    normalized = (value or "").strip().lower()
    return normalized if normalized in {"png"} else "png"


def _positive_float(value: Any, *, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


_SCREEN_STRING_FIELDS = ("provider", "model_id", "revision", "device", "ocr_engine")
_SCREEN_BOOL_FIELDS = (
    "enabled", "reasoning", "preload", "ocr_enabled", "log_timing", "debug_save_images",
)


class ScreenConfig(BaseModel):
    """Typed screen-pipeline section (P0b step 2a).

    Defaults match the pre-2a ``_DEFAULTS`` dict verbatim. The before-validator
    replicates the legacy loader's FILE-side coercion exactly (env-side coercion
    -- the bool wordlist, the unparseable-int fallthrough -- happens in
    ``manager.screen_env_config_overrides`` BEFORE values reach this model, so
    the env/file asymmetries pinned by test_resolved_config_equivalence hold):

    - falsy strings -> field default (``raw.get(k) or DEFAULT`` semantics);
      values are NOT whitespace-stripped here (only env values were stripped);
    - bools -> plain ``bool()`` truthiness (json ``"no"`` -> True, as before --
      the 1/true/yes wordlist applied ONLY to env strings);
    - max_side -> ``int()`` then clamp to [128, 4096]; unparseable -> default;
    - infer_timeout_sec -> ``_positive_float`` (invalid/non-positive -> 30.0);
    - dtype / capture_format normalized through their whitelists.
    """

    enabled: bool = True
    provider: str = "moondream_local"
    model_id: str = "vikhyatk/moondream2"
    revision: str = "2025-06-21"
    device: str = "cuda"
    dtype: str = "bfloat16"
    max_side: int = 768
    reasoning: bool = False
    preload: bool = False
    ocr_enabled: bool = True
    ocr_engine: str = "rapidocr"
    capture_format: str = "png"
    infer_timeout_sec: float = 30.0
    log_timing: bool = True
    debug_save_images: bool = False

    @model_validator(mode="before")
    @classmethod
    def _coerce_legacy_semantics(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        out = dict(data)
        for key in _SCREEN_STRING_FIELDS:
            if key in out:
                if not out[key]:
                    out.pop(key)  # falsy file value -> default (`or` semantics)
                else:
                    out[key] = str(out[key])
        if "dtype" in out:
            if not out["dtype"]:
                out.pop("dtype")
            else:
                out["dtype"] = _normalize_dtype(str(out["dtype"]))
        if "capture_format" in out:
            if not out["capture_format"]:
                out.pop("capture_format")
            else:
                out["capture_format"] = _normalize_capture_format(str(out["capture_format"]))
        for key in _SCREEN_BOOL_FIELDS:
            if key in out:
                out[key] = bool(out[key])
        if "max_side" in out:
            try:
                out["max_side"] = max(128, min(4096, int(out["max_side"])))
            except (TypeError, ValueError):
                out.pop("max_side")  # unparseable file value -> default
        if "infer_timeout_sec" in out:
            out["infer_timeout_sec"] = _positive_float(out["infer_timeout_sec"], default=30.0)
        return out


class PluginEntryConfig(BaseModel):
    """One plugin manifest entry (P0b step 3). Mirrors plugins/manifest.py's
    PluginEntry semantics; the str shorthand ("name" == enabled entry) is
    normalized by AppConfig's plugins validator below."""

    name: str
    enabled: bool = True


class SttConfig(BaseModel):
    """Speech-to-text (Plan B): local faster-whisper replaces network Google STT.
    yaml-only (铁律 #4: no env names -- nothing added to env_roster). All fields
    restart-effective; the model is resolved once at startup and kept resident."""

    # "faster_whisper" -> local whisper (default; no network, cannot hang).
    # "google" -> legacy recognize_google fallback (STILL timeout-less / can hang;
    # kept only as an explicit opt-out, never auto-selected).
    backend: str = "faster_whisper"
    # W3 (A5): which microphone RECORDER feeds the voice loop. "auto" folds by
    # effective platform (linux -> respeaker hardware-VAD path, windows -> generic
    # PyAudio + webrtcvad software VAD); explicit values override (W3b: ReSpeaker
    # on Windows / debugging: force generic on Linux). Resolution is the pure
    # ``resolve_mic_backend`` in spica/host/app_host.py; illegal values die here.
    mic_backend: Literal["auto", "respeaker", "generic"] = "auto"
    model: str = "large-v3-turbo"  # repo id OR a local dir path (pre-downloaded)
    device: str = "cuda"  # cuda | cpu
    compute_type: str = "float16"  # float16 (gpu) | int8 | int8_float16 ...
    language: str = "zh"
    beam_size: int = 5
    vad_filter: bool = False
    # Warm the model at startup alongside TTS (predictable first-utterance latency).
    # False -> lazy load on the first transcribe (still loaded ONCE, never per call).
    warmup_on_startup: bool = True
    # None -> faster-whisper's default HF cache. Set to a dir to pin a pre-downloaded
    # model (China-friendly: avoids a blocking first-startup download).
    download_root: str | None = None


class TrtOcrConfig(BaseModel):
    """ORT TensorRT EP options for the ``rapidocr_trt_ep`` provider (cut 2).

    yaml-only / injected (铁律 #4 + §3.3: nothing here is read from env; the host
    resolves ``engine_cache_dir`` to an absolute path and passes it down)."""

    # D4: fp32 is the cut-2 default -- verify the TRT integration mechanism (engine
    # builds, cache hits, fallback, speedup, parity) with one variable before turning
    # on fp16. fp16 stays configurable as the step-2 follow-up.
    fp16: bool = False
    # Repo-relative; the host resolves to an absolute path. Gitignored (§7.3).
    engine_cache_dir: str = "artifacts/trt"
    timing_cache: bool = True
    # Explicit TRT min/opt/max shape profiles, filled ONLY if the real-machine shape
    # probe shows many shapes (D3). Empty -> rely on ORT's per-shape engine cache.
    # Shape: {"det": {"min": "x:1x3x32x32", "opt": ..., "max": ...}, "rec": {...}}.
    profiles: dict[str, dict[str, str]] = Field(default_factory=dict)
    device_id: int = 0


class OcrConfig(BaseModel):
    """OCR provider selection (LOCAL_RUNTIME_PLAN cut 1/2, §5).

    Governs BOTH OCR paths from one place so they never fork onto different
    engines (§2.2): path A (galgame loop, via ``services.ocr_adapter``) and path B
    (inspect_screen, via the path-B install hook) are wired from this one
    ``provider`` by ``build_ocr_adapter``.

    yaml-only (铁律 #4: no env names -- nothing added to env_roster).

    SCOPE NOTE -- distinct from ``screen.ocr_engine``:
      * ``screen.ocr_engine`` is a DESCRIPTIVE label recorded inside the
        screen-pipeline observation (which engine produced visible_text);
      * ``ocr.provider`` SELECTS the actual ``OCRPort`` implementation for both
        paths.
    ``screen.ocr_engine`` still defaults to the visible-text label
    ``"rapidocr"``. ``ocr.provider`` has two layers of defaulting: this schema
    built-in stays ``"rapidocr"`` for no-file / extreme rollback; the repo
    production default now comes from ``data/config/app.yaml`` and is
    ``"rapidocr_ort"``. Consolidating the label and provider knobs is registered
    P3 cleanup, NOT this cut.

    CUTOVER STATUS (§6.1): ``rapidocr_ort`` is the repo production default for
    the Path A+B provider-seam rehearsal. ``fallback_provider`` and the schema
    built-in default remain ``rapidocr``. ``rapidocr_trt_ep`` remains
    experimental until cache/prewarm + real galgame parity clear the cold-cache
    risk."""

    # "rapidocr" (schema default/fallback) | "rapidocr_ort" (repo default, cut 1)
    # | "rapidocr_trt_ep" (experimental cut 2 -- ORT TensorRT EP).
    provider: str = "rapidocr"
    fallback_provider: str = "rapidocr"
    trt: TrtOcrConfig = Field(default_factory=TrtOcrConfig)


class PlatformConfig(BaseModel):
    """Platform lane selection (Windows compat W1, WINDOWS_COMPAT_PLAN §3.1).

    yaml-only (铁律 #4): no env name -- nothing added to env_roster, so the
    roster meta-pin stays untouched. Literal makes a typo fail loud at startup
    (mirrors GalgameConfig.reaction_mode). ``auto`` is NEVER folded here or in
    ConfigManager.load() -- folding to the effective platform happens exactly
    once at assembly time (agent_assembly.fold_platform), keeping the
    resolved-config equivalence pins (load() == AppConfig()) green."""

    os: Literal["auto", "linux", "windows"] = "auto"


class AnimeConfig(BaseModel):
    """spica 看番装配配置 (Phase 3/4, yaml-only 无 env -- 铁律 #4; secrets 走 xiaosan.env)。

    Phase 4 补齐 UI worker / 完成行为 / 持久化的键 (auto_play_threshold_seconds /
    qbittorrent_poll_seconds / stall_timeout_minutes / ytdlp_format / cookies_file /
    library_file); disk_limit_gb 归 Phase 5 磁盘提醒。cookies_file / library_file
    的相对路径按仓库根解析 (装配层 anime.py 统一处理, 同 manager._REPO_ROOT 惯例)。
    """

    enabled: bool = False                 # Phase 4 真机端到端过后才翻 true
    # Repo-relative (anchored at the repo root by the anime assembly's
    # resolve_data_path) + gitignored via static/, next to generated_voice/song.
    # Downloads group per anime: <download_dir>/<anime_dirname(title)>/<episode>.
    download_dir: str = "static/generated_anime"
    player_command: str = "vlc"           # VLC handles AV1/HEVC/MKV more reliably
    bilibili_spaces: list[str] = Field(default_factory=lambda: ["3493112693394137"])
    mikan_base_urls: list[str] = Field(
        default_factory=lambda: ["https://mikanani.me"])
    quality: str = "1080p"
    subtitle_preference: list[str] = Field(
        default_factory=lambda: ["简繁", "简体"])
    source_timeout_seconds: float = 15.0
    resolve_budget_seconds: float = 45.0
    qbittorrent_url: str = "http://127.0.0.1:8080"
    qbittorrent_username: str = "admin"   # password 是 secret (QBITTORRENT_PASSWORD)
    # -- Phase 4 (UI worker + 完成行为 + 持久化) --
    auto_play_threshold_seconds: float = 300.0   # D5 智能阈值: 快下自动播
    qbittorrent_poll_seconds: float = 5.0        # worker 轮询间隔
    stall_timeout_minutes: float = 30.0          # 无进度判卡 (本轮只播报询问)
    ytdlp_format: str = "bv*[height<=1080]+ba/b[height<=1080]"
    cookies_file: str = "data/cookies.txt"       # yt-dlp --cookies; 文件可缺省(匿名降清晰度)
    library_file: str = "data/anime/library.json"  # host 唯一写点 (P1-6); pending.json 同目录


class AppConfig(BaseModel):
    llm: LLMConfig = Field(default_factory=LLMConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    character: CharacterConfig = Field(default_factory=CharacterConfig)
    stream: StreamConfig = Field(default_factory=StreamConfig)
    galgame: GalgameConfig = Field(default_factory=GalgameConfig)
    stt: SttConfig = Field(default_factory=SttConfig)
    screen: ScreenConfig = Field(default_factory=ScreenConfig)
    ocr: OcrConfig = Field(default_factory=OcrConfig)
    platform: PlatformConfig = Field(default_factory=PlatformConfig)
    anime: AnimeConfig = Field(default_factory=AnimeConfig)
    # P0b step 3 (D-3a): the song section is intentionally UNTYPED -- it is the
    # override dict layered over song/config.py's DEFAULT_CONFIG by the same
    # deep-merge engine the legacy json used (voices are an open name->config
    # map; pydantic-izing that engine in the highest-risk step was rejected).
    # Typed-ization is tracked as debt in GALGAME_FINDINGS.
    song: dict[str, Any] = Field(default_factory=dict)
    plugins: list[PluginEntryConfig] = Field(default_factory=list)
    max_tool_rounds: int = 3

    @field_validator("plugins", mode="before")
    @classmethod
    def _normalize_plugin_entries(cls, value: Any) -> Any:
        # Same tolerant semantics as plugins/manifest.py: str shorthand becomes
        # an enabled entry; blank/invalid items are dropped, not errors.
        if not isinstance(value, list):
            return []
        normalized: list[Any] = []
        for item in value:
            if isinstance(item, str) and item.strip():
                normalized.append({"name": item.strip()})
            elif isinstance(item, dict) and item.get("name"):
                normalized.append(item)
            elif isinstance(item, PluginEntryConfig):
                normalized.append(item)
        return normalized

"""Backend assembly (Phase 6D).

Builds the ``AgentServices`` bundle (LLM client, memory, character profile, tool
functions, config dict) that the conversation core runs on. This is the
assembly half of the dissolved ``SimpleAgent`` and belongs to the host
(composition root); the driving / management half is ``ChatEngine``.

INVARIANT (CLAUDE.md #1 + #4): Qt-free; secrets come from the secrets loader.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import httpx
from openai import OpenAI

from spica.conversation.character_loader import (
    DEFAULT_CHARACTER_NAME,
    DEFAULT_INTERLOCUTOR_NAME,
    build_character_profile,
    normalize_interlocutor_name,
)
from spica.runtime.scope import DEFAULT_CHARACTER_ID
from spica.runtime.services import AgentServices
from agent_tools.function_tools import TOOL_SCHEMAS, default_tool_functions
from common.timing import log_timing
from memory.recent import RecentMemory
from memory.store import SQLiteMemoryStore
from spica.adapters.game_memory import GameMemorySqliteAdapter
from spica.adapters.game_launcher import LinuxDesktopGameLauncher
from spica.adapters.window_locator import LinuxX11WindowLocator
from spica.adapters.screen_capture import MssScreenCapture
from spica.adapters.ocr import RapidOcrAdapter, RapidOcrOrtAdapter, RapidOcrTrtEpAdapter
from spica.local_runtime.vision import MoondreamHfProvider
from spica.config.schema import AppConfig, TrtOcrConfig
from spica.config.secrets import Secrets

_REPO_ROOT = Path(__file__).resolve().parents[2]
_LOGGER = logging.getLogger(__name__)


def _build_rapidocr_trt_ep(trt_config: TrtOcrConfig | None) -> RapidOcrTrtEpAdapter:
    """Construct the (lazy) TRT-EP adapter, resolving the engine-cache dir to an
    absolute path against the repo root (§3.3: no env / no cwd; pathlib, §13).

    Per-stage ``profiles`` are intentionally NOT wired yet (passed as None): they
    are the deferred sub-step that follows the real-machine shape probe (D3). The
    adapter is lazy, so this builds NO engine here."""
    cfg = trt_config or TrtOcrConfig()
    cache_dir = Path(cfg.engine_cache_dir)
    if not cache_dir.is_absolute():
        cache_dir = _REPO_ROOT / cache_dir
    if cfg.profiles:
        _LOGGER.info(
            "ocr.trt.profiles set but per-stage TRT profiles are a deferred sub-step "
            "(post-probe); relying on the ORT engine cache this cut."
        )
    return RapidOcrTrtEpAdapter(
        fp16=cfg.fp16,
        engine_cache_dir=str(cache_dir),
        timing_cache=cfg.timing_cache,
        profiles=None,
        device_id=cfg.device_id,
    )


def build_ocr_adapter(
    provider: str = "rapidocr",
    fallback_provider: str | None = "rapidocr",
    *,
    trt_config: TrtOcrConfig | None = None,
):
    """Select the OCR ``OCRPort`` implementation by provider name (LOCAL_RUNTIME_PLAN
    §2.3 / §11). The SINGLE source of OCR provider selection -- the same returned
    adapter drives BOTH paths (galgame loop here via ``ocr_adapter``; inspect_screen
    via the path-B install hook in app_host), so they never fork (§2.2).

    Schema/factory fallback ``rapidocr`` returns ``RapidOcrAdapter()`` and remains
    the no-file / extreme-rollback path. The repo production default is supplied
    by ``data/config/app.yaml`` and now selects ``rapidocr_ort`` for the Path A+B
    default-cutover rehearsal. ``rapidocr_trt_ep`` (cut 2, ORT TensorRT EP) remains
    experimental and LAZY (no engine built here); it needs cache/prewarm + real
    galgame parity before any default switch. Unknown names fall back to
    ``fallback_provider`` with a warning, so a mis-set config degrades gracefully
    instead of crashing startup."""
    name = (provider or "rapidocr").strip()
    if name == "rapidocr":
        return RapidOcrAdapter()
    if name == "rapidocr_ort":
        return RapidOcrOrtAdapter()
    if name == "rapidocr_trt_ep":
        return _build_rapidocr_trt_ep(trt_config)
    if fallback_provider and fallback_provider != name:
        _LOGGER.warning(
            "unknown/unavailable OCR provider %r; falling back to %r", name, fallback_provider
        )
        return build_ocr_adapter(fallback_provider, fallback_provider=None, trt_config=trt_config)
    _LOGGER.warning("unknown OCR provider %r and no fallback; using rapidocr", name)
    return RapidOcrAdapter()


def build_moondream_provider(
    provider: str = "moondream_local",
    fallback_provider: str | None = "moondream_local",
):
    """Select the Moondream screen-vision provider by name (LOCAL_RUNTIME_PLAN cut 4).

    The SINGLE source of Moondream provider selection. Unlike ``build_ocr_adapter``
    (which always returns an adapter), this returns a PROVIDER-or-None: the default
    ``moondream_local`` returns ``None`` so the host installs NOTHING and the manager
    seam (``load_moondream_backend``) calls the legacy ``MoondreamBackend.load``
    byte-for-byte (the zero-diff default, P0). ``moondream_hf`` returns the isolated
    ``MoondreamHfProvider`` (the relocated runtime). Unknown names fall back to
    ``fallback_provider`` with a warning -> ``None`` -> legacy, so a mis-set config
    degrades to the safe default instead of crashing startup.

    The default is NOT switched away from ``moondream_local`` this cut -- that needs
    a parity report (legacy vs hf)."""
    name = (provider or "moondream_local").strip()
    if name == "moondream_local":
        return None  # default: NOT installed -> manager seam calls legacy MoondreamBackend.load
    if name == "moondream_hf":
        return MoondreamHfProvider()
    if fallback_provider and fallback_provider != name:
        _LOGGER.warning(
            "unknown/unavailable Moondream provider %r; falling back to %r", name, fallback_provider
        )
        return build_moondream_provider(fallback_provider, fallback_provider=None)
    _LOGGER.warning(
        "unknown Moondream provider %r and no fallback; using legacy moondream_local", name
    )
    return None


def fold_platform(os_cfg: str, host_platform: str) -> str:
    """Fold the typed ``platform.os`` value into the effective platform (W1,
    WINDOWS_COMPAT_PLAN §3.2). Pure function -- no ``sys`` read here, so Layer B
    pins it with injected values; the ONE production ``sys.platform`` read is in
    ``build_agent_services``.

    - explicit "linux"/"windows" -> returned verbatim (never looks at the host;
      also the only escape hatch on unknown hosts);
    - "auto": host "linux" -> "linux", host "win32" -> "windows", anything else
      (darwin/cygwin/msys/...) RAISES -- fail loud, never a silent fold onto the
      wmctrl lane on a non-Linux host (P2-2);
    - an illegal os_cfg already fails loud at the schema Literal layer; the raise
      here only backstops non-config callers."""
    if os_cfg in ("linux", "windows"):
        return os_cfg
    if os_cfg == "auto":
        if host_platform == "linux":
            return "linux"
        if host_platform == "win32":
            return "windows"
        raise ValueError(
            f"platform.os=auto has no fold for host platform {host_platform!r}; "
            "set platform.os explicitly (linux|windows) in data/config/app.yaml"
        )
    raise ValueError(f"unknown platform.os value {os_cfg!r}")


def build_window_locator(effective_os: str):
    """Platform-lane factory for the ``WindowLocatorPort`` adapter (W1 §3.3,
    ``build_ocr_adapter`` precedent). UNLIKE build_ocr_adapter this FAILS LOUD on
    an unknown lane: a mis-selected platform must never silently fall back to the
    other platform's window probes. The windows class is imported lazily inside
    its branch so the module import stays platform-clean (§3.5)."""
    if effective_os == "linux":
        return LinuxX11WindowLocator()
    if effective_os == "windows":
        from spica.adapters.window_locator.windows_win32 import WindowsWin32WindowLocator

        return WindowsWin32WindowLocator()
    raise ValueError(f"no window_locator lane for effective platform {effective_os!r}")


def build_screen_capture(effective_os: str):
    """Platform-lane factory for the ``ScreenCapturePort`` adapter (W1 §3.3).
    mss is cross-platform (X11 on Linux, GDI on Windows), so BOTH lanes return
    ``MssScreenCapture`` -- the factory still validates the lane (fail loud)."""
    if effective_os in ("linux", "windows"):
        return MssScreenCapture()
    raise ValueError(f"no screen_capture lane for effective platform {effective_os!r}")


def build_game_launcher(effective_os: str):
    """Platform-lane factory for the ``GameLauncherPort`` adapter (W1 §3.3).
    Fail-loud on unknown lanes; windows class lazily imported (§3.5)."""
    if effective_os == "linux":
        return LinuxDesktopGameLauncher()
    if effective_os == "windows":
        from spica.adapters.game_launcher.windows_native import WindowsNativeGameLauncher

        return WindowsNativeGameLauncher()
    raise ValueError(f"no game_launcher lane for effective platform {effective_os!r}")


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
        character_id = DEFAULT_CHARACTER_ID
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
    # W1: fold the platform ONCE (resolve-once + inject, the screen/song precedent)
    # and select the three platform-adapter lanes via the factories. This is the
    # single production sys.platform read; every later consumer reads
    # services.effective_platform (§3.6), never sys.platform again.
    effective_platform = fold_platform(config.platform.os, sys.platform)
    window_locator = build_window_locator(effective_platform)
    screen_capture = build_screen_capture(effective_platform)
    game_launcher = build_game_launcher(effective_platform)
    _LOGGER.info(
        "platform resolved: os_cfg=%s host=%s effective=%s "
        "lanes=window_locator/%s screen_capture/%s game_launcher/%s",
        config.platform.os,
        sys.platform,
        effective_platform,
        getattr(window_locator, "name", type(window_locator).__name__),
        getattr(screen_capture, "name", type(screen_capture).__name__),
        getattr(game_launcher, "name", type(game_launcher).__name__),
    )
    return AgentServices(
        llm_client=client,
        tts_adapter=tts_adapter,
        visual_tool=visual_tool,
        memory_store=SQLiteMemoryStore(data_dir / "memory.sqlite3"),
        recent_memory=RecentMemory(max_turns=config.memory.recent_memory_turns),
        game_memory_adapter=GameMemorySqliteAdapter(data_dir / "galgame.sqlite3"),
        # Phase 5 / W1: galgame launch + window-binding adapters, now selected by
        # the platform-lane factories above (linux lane == the former hardcoded
        # constructions, byte-equivalent).
        game_launcher_adapter=game_launcher,
        window_locator_adapter=window_locator,
        # Phase 6: galgame screen capture (mss) + OCR. The OCR provider is selected
        # by resolved config via the factory: repo default rapidocr_ort, schema /
        # fallback default rapidocr. The SAME adapter is installed into the path-B
        # hook in app_host so galgame OCR and inspect_screen never fork
        # (LOCAL_RUNTIME_PLAN §2.2).
        screen_capture_adapter=screen_capture,
        ocr_adapter=build_ocr_adapter(
            config.ocr.provider, config.ocr.fallback_provider, trt_config=config.ocr.trt
        ),
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
        # W1 (§3.6/A8): the folded value's persistent home -- production always
        # writes the real fold result, never relying on the dataclass default.
        effective_platform=effective_platform,
    )

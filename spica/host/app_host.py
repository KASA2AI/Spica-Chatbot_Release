"""Application host: the composition root for the Spica platform.

``AppHost.initialize()`` constructs the backend services (LLM / TTS / Visual /
Memory adapters resolved by configured name from the ``CapabilityRegistry``, the
active character package, the built-in tools) and wires them into the conversation
core. The UI no longer ``new``s any service -- it calls ``AppHost().initialize()``
and reads the services back.

INVARIANT (CLAUDE.md #1): this module -- and everything under ``spica/`` -- must
never import PySide / Qt / any GUI library. The services constructed here are
all Qt-free, so the host stays framework-agnostic. That is what lets a future
Web/React front-end subscribe to the host without the core changing.

The host exposes two narrow surfaces rather than one fat object:

- ``conversation_surface`` (for the chat window) -- the ``ChatEngine`` that drives
  a turn (run / stream) and owns character / memory management.
- ``management_surface`` (for the settings centre) -- the ``ManagementSurface``
  that lists adapters / characters / plugins and reads / writes typed config.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Callable


from spica.config.manager import ConfigManager
from spica.config.schema import AppConfig
from spica.config.secrets import Secrets, load_secrets
from spica.core.chat_engine import ChatEngine
from spica.core.companion_events import CompanionEventSink, noop_companion_sink
from spica.conversation.character_loader import DEFAULT_SPICA_SKILL_DIR
from spica.core.character import load_character_package
from spica.adapters.memory.sqlite import scoped_conversation_id
from spica.galgame.binding import GameBinder
from spica.galgame.companion_controller import GalgameCompanionController
from spica.galgame.history import compose_play_history
from spica.core.proactive import ProactiveTurnRequest
from spica.galgame.models import CompanionBeat, utc_now_iso
from spica.galgame.ocr_calibration import GalgameOcrCalibrator
from spica.galgame.ocr_loop import OcrStreamRunner
from spica.galgame.reaction import (
    ReactionEngine,
    ReactionLexicon,
    ScoreResult,
    compose_reaction_directive,
)
from spica.galgame.reaction_judge import GalgameReactionJudge
from spica.galgame.reaction_scoring import ReactionScoringPolicy
from spica.galgame.session import GalgameCompanionSession
from spica.galgame.summarizer import GalgameSummarizer, recover_dangling_sessions
from spica.host.domain_router import ActiveDomainRouter
from spica.host.model_router import ModelRouter
from spica.runtime.context import GameTurnBinding
from spica.runtime.jobs import ThreadJobRunner
from spica.runtime.window import WatchContext, WindowTarget
from spica.runtime.scope import CharacterScope, character_scope_from_config
from spica.host.agent_assembly import (
    build_agent_services,
    build_moondream_provider,
)
from spica.host.assemblies import anime as anime_assembly
from spica.host.assemblies import reaction as reaction_assembly
from spica.host.builtins import register_builtin_adapters
from spica.host.management import ManagementSurface
from spica.host.warmup import run_warmup
from spica.plugins.host import PluginHost
from spica.plugins.registry import CapabilityRegistry
from spica.adapters.screen import LocalMoondreamScreenAnalysis
from spica.adapters.tools.note_game_observation import NoteGameObservationTool
from spica.adapters.tools.sing_song import SingSongTool
from spica.adapters.tools.watch_game_screen import WatchGameScreenTool
from spica.core.song_events import SongRequestEvent
from spica.galgame.models import CompanionBeat, utc_now_iso
from agent_tools.function_tools.screen.config import resolve_effective_screen_config
from agent_tools.function_tools.screen.schema import ScreenToolError
from agent_tools.function_tools.song.config import resolve_effective_song_config, song_enabled
from agent_tools.function_tools.song.models import SongRequest
from agent_tools.function_tools.song.netease import search_best_song
from spica.adapters.visual import build_spica_visual
from agent_tools.tts import CURRENT_GPTSOVITS_PROVIDERS, GPTSoVITSTool, load_tts_config

logger = logging.getLogger(__name__)

def _install_ocr_runtime_provider(config: AppConfig, services: Any) -> None:
    from agent_tools.function_tools.screen.backends.ocr_runtime import (
        reset_active_ocr_provider,
        set_active_ocr_provider,
    )

    if config.ocr.provider != "rapidocr":
        set_active_ocr_provider(services.ocr_adapter)
    else:
        reset_active_ocr_provider()


def resolve_mic_backend(mic_backend_cfg: str, effective_platform: str) -> str:
    """Fold the typed ``stt.mic_backend`` value into the effective mic recorder
    backend (W3 / A5). Pure function -- Layer B pins it with injected values,
    same discipline as ``fold_platform``.

    - explicit "respeaker"/"generic" -> returned verbatim (W3b: ReSpeaker on
      Windows; debugging: force generic on Linux);
    - "auto": platform "linux" -> "respeaker" (hardware-VAD path, unchanged),
      "windows" -> "generic" (PyAudio + webrtcvad software VAD), anything else
      RAISES -- fail loud, never a silent fold onto some mic path;
    - an illegal cfg value already dies at the schema Literal; the raise here
      only backstops non-config callers."""
    if mic_backend_cfg in ("respeaker", "generic"):
        return mic_backend_cfg
    if mic_backend_cfg == "auto":
        if effective_platform == "linux":
            return "respeaker"
        if effective_platform == "windows":
            return "generic"
        raise ValueError(
            f"stt.mic_backend=auto has no fold for effective platform {effective_platform!r}"
        )
    raise ValueError(f"unknown stt.mic_backend value {mic_backend_cfg!r}")


class AppHost:
    """Owns the backend services and wires them together at startup."""

    def __init__(self) -> None:
        self.config: AppConfig | None = None
        self.secrets: Secrets | None = None
        self.visual_tool: Any | None = None
        self.tts_tool: Any | None = None
        self.tts_adapter: Any | None = None
        self.stt_adapter: Any | None = None  # Plan B: local faster-whisper STT (resident singleton)
        # W3: resolved mic recorder backend STRING (resolve_mic_backend, set in
        # initialize()); the UI wires it into the voice loop like stt_adapter.
        # Default matches the pre-W3 Linux path for anything reading it early.
        self.effective_mic_backend: str = "respeaker"
        self.services: Any | None = None
        self.character_package: Any | None = None
        self.chat_engine: Any | None = None
        # galgame companion event sink (Phase 4). The PUBLIC sink is a stable
        # dispatcher (P5 tee, D-P5-0): it forwards to the UI bridge and -- when
        # the reaction engine is on -- enqueues into its worker queue. Default
        # UI side is no-op so the host runs headless; the UI injects a Qt-free
        # bridge via attach_companion_sink.
        self._ui_companion_sink: CompanionEventSink = noop_companion_sink
        self.companion_sink: CompanionEventSink = self._dispatch_companion_event
        # Anime-watch (Phase 3): dedicated Host->UI sink, attached in Phase 4 by
        # the UI. None = not wired -> watch_anime is not supplied (and its closure
        # returns ANIME_NOT_READY). Kept OFF the companion tee so anime events
        # never reach the reaction engine.
        self._anime_sink: Any = None
        # F8 busy seam: live in-flight download state ({"progress","title"} or
        # None), supplied by the UI controller via attach_anime_sink (Phase 4).
        self._anime_in_flight: Any = lambda: None
        # P5 reaction engine (built in initialize() when reaction_mode != off;
        # the arbiter handoff is UI-attached -- same shape as song's
        # request_proactive_turn injection).
        self.reaction_engine: ReactionEngine | None = None
        self._reaction_try_speak: Any | None = None
        # P5 v2 LLM judge: built by assemblies.reaction.install() when
        # reaction_judge_enabled (None -> the lexicon score_beat stays the
        # scorer, zero diff). The judge lives HERE (write-authority stays on the
        # host); cooldown state + lexicon caches moved into the policy (Phase 4).
        self._reaction_judge: GalgameReactionJudge | None = None
        # Phase 4: the scoring DECISION half. Providers are live-read lambdas on
        # purpose -- tests (and set_interlocutor_name-style runtime mutation)
        # replace host attributes after construction and the policy must see it;
        # capturing a bound method or value here would silently freeze them.
        self._reaction_scoring_policy = ReactionScoringPolicy(
            config_provider=lambda: self.config,
            game_scope_provider=lambda: self._reaction_game_scope(),
            game_memory_provider=lambda: (
                self.services.game_memory_adapter if self.services is not None else None
            ),
            character_scope_provider=lambda: self.character_scope,
            judge_provider=lambda: self._reaction_judge,
        )
        # Phase 6b: the ONE home for role/endpoint decisions (summary/judge
        # model fallbacks + the judge endpoint tree). Constructor is inert
        # (stores the host ref only); every decision resolves per call.
        self.model_router = ModelRouter(self)
        # Phase 8-c1: the ONE home for "which domain owns the current turn
        # binding". ChatEngine's single provider slot points at its current();
        # galgame publishes/retracts through the controller's binding sink.
        # galgame-only closures (_companion_game_binding / reaction scope /
        # note write-back) keep reading the CONTROLLER snapshot, never
        # router.current() (设计裁决 修正 1).
        self.domain_router = ActiveDomainRouter()
        # Path B stage 2: the process-wide companion controller singleton, built
        # lazily by companion_controller(); _companion_game_binding reads it.
        self._companion_controller: GalgameCompanionController | None = None
        # B2: the sing_song closure's search seam (tests inject a fake).
        self._song_search = search_best_song
        self.tts_provider: str = "gptsovits_current"
        # P0b 2a/3: resolve the screen pipeline config ONCE through the carrier
        # switch (legacy json present -> whole old chain + WARNING; absent ->
        # app.yaml chain) and inject the instance into every production
        # consumer (builtins inspect tool, watch tool, UI worker).
        self.screen_config = resolve_effective_screen_config()
        # P0b 2b/3: same resolve-once + carrier-switch pattern for song.
        # Injected into the UI SongWorker chain and read by _request_song.
        self.song_config = resolve_effective_song_config()
        self.registry = CapabilityRegistry()
        register_builtin_adapters(self.registry, screen_config=self.screen_config)
        # Phase 9: the companion watch tool. Registered HERE (not in builtins)
        # because it closes over this host -- the LAZY provider resolves the
        # companion controller + adapters at RUN time (adapters only exist after
        # initialize(); not playing -> None -> NO_ACTIVE_COMPANION tool error).
        # Trigger layer: offered by STATE (companion play active), not by wordlist
        # -- intent_gated=False, and the LLM decides the call via the description.
        watch_tool = WatchGameScreenTool(
            LocalMoondreamScreenAnalysis(),
            self._companion_watch_context,
            config=self.screen_config,
        )
        self.registry.register_tool(
            watch_tool.schema(),
            watch_tool.run,
            # screen.enabled joins the companion-state supply gate: with screen
            # vision off the tool is never offered (run() also hard-refuses
            # before capturing, since available is supply-side only).
            available=lambda: bool(self.screen_config.enabled)
            and self._companion_watch_context() is not None,
            intent_gated=False,
        )
        # Phase 9 step 2: write-back. note_game_observation stores a
        # dialogue-confirmed observation as a CompanionBeat in the GAME memory.
        # Same trigger shape as watch (state supply, the LLM decides the call);
        # the tool is a pure write shim -- beat construction and persistence
        # live in the host closure (_record_game_observation), so write
        # authority never leaves the host (CLAUDE.md #8).
        note_tool = NoteGameObservationTool(
            self._companion_game_binding, self._record_game_observation
        )
        self.registry.register_tool(
            note_tool.schema(),
            note_tool.run,
            available=lambda: self._companion_game_binding() is not None,
            intent_gated=False,
            effect="write",  # P2: writes own-domain (game) memory
        )
        # B2 (P2): sing_song -- the first "act" tool. Supply is wordlist-PRE-
        # FILTERED (intent_gated=True + the song terms in the router): the B1
        # lesson applied correctly -- the wordlist gates SUPPLY (miss = rephrase,
        # false hit = one wasted probe), it never hijacks/swallows the message.
        # The host closure (_request_song) holds all authority; the tool is a shim.
        sing_tool = SingSongTool(self._request_song)
        self.registry.register_tool(
            sing_tool.schema(),
            sing_tool.run,
            # song master switch: disabled -> not offered to the LLM at all.
            # Supply-side only -- the closure below re-checks, because tools.run
            # never re-evaluates ``available`` on a forced call.
            available=lambda: song_enabled(self.song_config),
            intent_gated=True,
            effect="act",
        )
        self.plugin_host = PluginHost(self.registry)
        self._management = ManagementSurface(
            registry=self.registry,
            config_manager=ConfigManager(),
            plugin_host=self.plugin_host,
            characters_root=DEFAULT_SPICA_SKILL_DIR.parent,
        )

    @property
    def character_scope(self) -> CharacterScope:
        """The current character identity, resolved LIVE from config on every
        access (Phase 2) -- the single replacement for the fourteen scattered
        bare-literal identity fallbacks this class used to carry. A
        property (not a cached value) so ``set_interlocutor_name``'s in-place
        config rename is reflected immediately. Requires config (post-initialize
        callers only, same as the sites it replaced)."""
        return character_scope_from_config(self.config)

    def initialize(self) -> None:
        """Construct the backend services (moved verbatim from the UI).

        Mechanical move of ``OverlayWindow._init_backend``'s construction logic,
        with zero behaviour change. On failure it salvages ``visual_tool`` (so
        the character can still render) and re-raises, leaving the UI to surface
        the error message and read back whatever was built.
        """
        try:
            self.config = ConfigManager().load()
            self.secrets = load_secrets()
            # Load external plugins so they can register adapters/tools into the
            # registry before capabilities are resolved by configured name (Phase 8).
            self.plugin_host.load()
            # Load the active character package first so its asset references
            # drive visual/tts construction (Phase 7b). Spica's package leaves the
            # paths unset -> engine defaults -> behaviour unchanged.
            self.character_package = load_character_package(
                self.config.character.package_dir or DEFAULT_SPICA_SKILL_DIR
            )
            # Keep skill_dir in sync so ChatEngine.set_interlocutor_name reloads
            # the active package's persona.
            self.config.character.skill_dir = self.character_package.skill_dir
            self.visual_tool = self.registry.resolve_visual(
                "spica_diff", config_path=self.character_package.visual_config_path
            )
            tts_config = (
                load_tts_config(self.character_package.tts_config_path)
                if self.character_package.tts_config_path
                else load_tts_config()
            )
            self.tts_provider, self.tts_tool, self.tts_adapter = (
                self._resolve_tts_assembly(tts_config)
            )
            self.services = build_agent_services(
                self.config,
                self.secrets,
                tts_adapter=self.tts_adapter,
                visual_tool=self.visual_tool,
                character_package=self.character_package,
            )
            # cut 1 (LOCAL_RUNTIME_PLAN §2.2): unify the two OCR paths. Path A
            # (galgame) already holds services.ocr_adapter; install that SAME object
            # into path B (inspect_screen analyzer) so a non-default provider covers
            # both. Default rapidocr -> NOT installed -> path B keeps the legacy
            # ocr_image (byte-identical), so the default chain is untouched.
            _install_ocr_runtime_provider(self.config, self.services)
            # cut 4 (LOCAL_RUNTIME_PLAN: Moondream): same install-hook shape for the
            # screen-vision backend. Default moondream_local -> factory returns None
            # -> NOT installed -> the manager seam calls the legacy
            # MoondreamBackend.load (byte-identical, the zero-diff default). A
            # non-default provider (moondream_hf) is built + installed once here.
            # screen_config is the SAME resolved instance inspect_screen / watch
            # query through, so the install decision matches what the manager sees.
            self._install_moondream_seam()
            # Resolve and inject the LLM / memory adapters by configured name.
            self.services.llm_adapter = self.registry.resolve_llm(
                self.config.llm.provider, client=self.services.llm_client,
                reasoning_effort=self.config.llm.reasoning_effort,
            )
            self.services.memory_adapter = self.registry.resolve_memory(
                self.config.memory.provider,
                store=self.services.memory_store,
                recent=self.services.recent_memory,
            )
            # Anime-watch (Phase 3): domain assembly. MUST be here -- config /
            # secrets / services already exist (adapters read config.anime +
            # secrets at build time), unlike the __init__-registered tools.
            anime_assembly.install(self)
            # C7: the turn resolves tools from the registry (inspect_screen ToolPort).
            self.services.tool_registry = self.registry
            # ChatEngine is the conversation core (Phase 6D: SimpleAgent dissolved
            # into ChatEngine + spica/host/agent_assembly).
            self.chat_engine = ChatEngine(self.services, self.config)
            # Stage 2: companion-play auto-injection. The provider is LAZY (reads
            # the controller singleton at call time), so wiring order is free and a
            # plain chat turn stays byte-identical while no companion play is active.
            # Phase 8-c1: the engine's single binding slot reads the domain
            # router (D6: the router is the ONE injector); galgame's binding
            # reaches it through the controller's sink at publish-LAST time.
            self.chat_engine.set_game_binding_provider(self.domain_router.current)
            # P5 / Phase 4: reaction domain wiring via the assembly (judge before
            # engine; install() builds THROUGH the thin delegates below -- the
            # facade is the only build path, pinned by patch-validity tests).
            reaction_assembly.install(self)
            # Plan B: build the STT adapter ONCE (resident singleton). Construction
            # is cheap (the WhisperModel loads lazily at warmup/first transcribe);
            # injected by reference into each SpeechWorker so worker churn never
            # reloads the model.
            self.stt_adapter = self._new_stt_adapter()
            # W3: resolve the mic recorder backend once (pure fold; selection
            # logged like the platform lanes so smoke logs show the choice).
            self.effective_mic_backend = resolve_mic_backend(
                self.config.stt.mic_backend, self.services.effective_platform
            )
            logger.info(
                "mic backend resolved: cfg=%s platform=%s effective=%s",
                self.config.stt.mic_backend,
                self.services.effective_platform,
                self.effective_mic_backend,
            )
        except Exception:
            if self.visual_tool is None:
                try:
                    self.visual_tool = build_spica_visual()
                except Exception:
                    self.visual_tool = None
            raise

    @property
    def conversation_surface(self) -> Any:
        """Entry point for the chat window: the ChatEngine (None before initialize)."""
        return self.chat_engine

    def attach_anime_sink(self, sink: Any, *, in_flight: Any = None) -> None:
        """Inject the anime-watch Host->UI event sink (Phase 4 seam). Until this
        is called, watch_anime is not supplied and its closure returns
        ANIME_NOT_READY. ``in_flight`` is the UI controller's live download-state
        provider for the F8 busy gate (None -> stays "no download")."""
        self._anime_sink = sink
        self._anime_in_flight = in_flight if in_flight is not None else (lambda: None)

    def attach_companion_sink(self, sink: CompanionEventSink) -> None:
        """Inject the galgame Host->UI event sink (Phase 4 seam).

        ``spica/`` is Qt-free, so the concrete sink (a Qt bridge that marshals onto
        the GUI thread) is created in ``ui/`` and injected down here. The public
        ``companion_sink`` stays the stable dispatcher (P5 tee), so attach order
        relative to session construction no longer matters.
        """
        self._ui_companion_sink = sink

    def _dispatch_companion_event(self, event: Any) -> None:
        """The P5 tee. May run on the OCR thread INSIDE the session lock, so the
        reaction leg is enqueue-only (put_nowait, D-P5-0 red line) -- all real
        work happens on the engine's own worker."""
        self._ui_companion_sink(event)
        engine = self.reaction_engine
        if engine is not None:
            engine.enqueue_event(event)

    # -- P5 reaction engine assembly (closures only -- the host stays thin) ------

    def attach_reaction_arbiter(self, try_speak: Any) -> None:
        """UI hands over ``ProactiveTurnArbiter.try_speak`` (same injection shape
        as the song controller's request_proactive_turn)."""
        self._reaction_try_speak = try_speak

    def _reaction_game_scope(self) -> tuple[str, str, GameTurnBinding] | None:
        """(game_id, playthrough_id, binding) of the LIVE play, or None."""
        binding = self._companion_game_binding()
        if binding is None:
            return None
        request = binding.game_context_request
        game_id = str(request.game_id or "")
        if not game_id:
            return None
        return game_id, str(request.playthrough_id or "default"), binding

    def _reaction_speak(self, beat: Any, score: int) -> bool:
        """Engine speak callback (worker thread): directive composition + arbiter
        handoff. False (= busy semantics, no budget charge) when the UI arbiter
        is not attached or the play has already ended."""
        del score  # telemetry lives in the engine's decision trail
        try_speak = self._reaction_try_speak
        scope = self._reaction_game_scope()
        if try_speak is None or scope is None:
            return False
        _, _, binding = scope
        request = ProactiveTurnRequest(
            directive=compose_reaction_directive(
                beat,
                reply_char_limit=self.config.galgame.reaction_reply_char_limit,
                line_char_cap=self.config.galgame.reaction_excerpt_line_char_limit,
                excerpt_char_cap=self.config.galgame.reaction_excerpt_total_char_limit,
            ),
            source="galgame",
            conversation_id=binding.conversation_id,
        )
        return bool(try_speak(request))

    def _reaction_lexicon_for(self, game_id: str | None) -> ReactionLexicon:
        """Thin delegate (Phase 4 facade; LONG-LIVED per D4 stop-clock, amendment
        521f882 -- deletion is not scheduled): the mtime cache lives on the
        scoring policy now."""
        return self._reaction_scoring_policy.lexicon_for(game_id)

    def _reaction_scorer(self, beat: Any) -> ScoreResult:
        """Thin delegate (Phase 4 facade; LONG-LIVED per D4 stop-clock, amendment
        521f882): the engine's
        ``(beat) -> ScoreResult`` seam now lands on ReactionScoringPolicy.score
        (judge call / cooldown / lexicon hot-reload / failure degradation all
        live there; write closures stay below on the host)."""
        return self._reaction_scoring_policy.score(beat)

    def _write_reaction_beat(self, content: str, meta: dict) -> None:
        """Engine beat_writer (worker thread): persist a reaction CompanionBeat
        under the live play's scope. Play already ended -> logged and dropped."""
        scope = self._reaction_game_scope()
        if scope is None or self.services is None:
            logger.warning("reaction beat dropped: no live play scope to record under")
            return
        game_id, playthrough_id, _ = scope
        character_id = self.character_scope.character_id
        user_id = self.character_scope.user_id
        self.services.game_memory_adapter.add_companion_beat(
            CompanionBeat(
                beat_id=uuid.uuid4().hex,
                game_id=game_id,
                playthrough_id=playthrough_id,
                type="reaction",
                content=content,
                source="spica",
                created_at=utc_now_iso(),
                scope={"character_id": character_id, "user_id": user_id, "game_id": game_id},
                meta=dict(meta),
            )
        )

    def _recent_reaction_beats(self, limit: int) -> list:
        """Engine recent_for_dedupe (worker thread): recent spica beats incl.
        silent ones (D-P5-6) for the similarity gate."""
        scope = self._reaction_game_scope()
        if scope is None or self.services is None:
            return []
        game_id = scope[0]
        character_id = self.character_scope.character_id
        user_id = self.character_scope.user_id
        return self.services.game_memory_adapter.recent_reaction_beats_for_dedupe(
            game_id, user_id, character_id, limit=limit
        )

    def _build_reaction_engine(self) -> ReactionEngine | None:
        """Thin delegate (Phase 4 facade; LONG-LIVED per D4 stop-clock, amendment
        521f882). The patch target cutover tests intercept; the assembly builds
        THROUGH it."""
        return reaction_assembly.build_reaction_engine(self)

    def _new_summarizer(self) -> GalgameSummarizer | None:
        # Summary LLM = the router's "summary" role (Phase 6b: summary_model or
        # the dialogue model, over the main resolved adapter -- decision now
        # lives in model_router). None when no LLM is wired (tests).
        if self.services is None or self.services.llm_adapter is None:
            return None
        return GalgameSummarizer(self.model_router.for_role("summary"))

    def _new_reaction_judge(self) -> GalgameReactionJudge | None:
        """Thin delegate (Phase 4 facade; LONG-LIVED per D4 stop-clock, amendment
        521f882)."""
        return reaction_assembly.new_reaction_judge(self)

    def _judge_llm_adapter(self) -> Any:
        """Thin delegate (Phase 4 facade; LONG-LIVED per D4 stop-clock, amendment
        521f882). The judge's BoundModel assembly takes the adapter THROUGH this
        method (patch-validity pin -- router.for_role("judge") calls back here),
        so tests patching it keep intercepting real construction; the endpoint
        fallback tree itself lives in model_router.judge_adapter (Phase 6b)."""
        return self.model_router.judge_adapter()

    def _new_stt_adapter(self) -> Any | None:
        """Plan B local STT. None unless backend == "faster_whisper" (the "google"
        backend keeps the legacy in-worker recognize_google fallback -- never built
        here). Resolve-once; the returned adapter holds the WhisperModel singleton
        and is injected by reference into every SpeechWorker (worker churn != model
        churn). Mirrors _new_summarizer/_new_reaction_judge (host stays thin)."""
        cfg = self.config.stt
        if str(cfg.backend) != "faster_whisper":
            logger.info("STT backend=%s -> no local adapter (legacy fallback in worker)", cfg.backend)
            return None
        from spica.adapters.stt.faster_whisper import FasterWhisperAdapter

        return FasterWhisperAdapter(
            model=cfg.model, device=cfg.device, compute_type=cfg.compute_type,
            language=cfg.language, beam_size=cfg.beam_size, vad_filter=cfg.vad_filter,
            download_root=cfg.download_root,
        )

    def new_companion_session(self) -> GalgameCompanionSession:
        """Build a galgame companion session wired to the game-memory adapter, the
        companion sink, a background ``ThreadJobRunner`` + the summarizer (Phase 8).
        Requires ``initialize()`` first (provides the adapters)."""
        return GalgameCompanionSession(
            self.services.game_memory_adapter,
            emit=self.companion_sink,
            character_id=self.character_scope.character_id,
            user_id=self.character_scope.user_id,
            jobs=ThreadJobRunner(),
            summarizer=self._new_summarizer(),
            summary_trigger_chars=self.config.galgame.summary_trigger_chars,
        )

    def companion_controller(self) -> GalgameCompanionController:
        """The process-wide companion controller (Path B stage 2) -- the ONE the
        main program uses, built lazily via ``new_companion_controller()`` and
        cached. The single-play guarantee = this singleton + ``start()``'s
        already-started rejection. Requires ``initialize()`` first."""
        if self._companion_controller is None:
            self._companion_controller = self.new_companion_controller()
        return self._companion_controller

    def _companion_game_binding(self) -> GameTurnBinding | None:
        """The provider wired into ChatEngine (stage 2): the active companion-turn
        binding, or ``None`` (no controller yet / not playing) -> plain chat."""
        controller = self._companion_controller
        return controller.current_game_context() if controller is not None else None

    def _companion_watch_context(self) -> WatchContext | None:
        """Lazy provider for the watch_game_screen tool (Phase 9; Phase 8-c2:
        named ``WatchContext`` replaces the bare 5-tuple): the live play's
        target + locator/capture handles + session_state, or ``None`` when not
        playing / before initialize(). Reads the singleton WITHOUT building it.
        session_state is read lock-free at call time (staleness <= one OCR cycle);
        the tool's privacy gate (CLAUDE.md §4) refuses capture on unsafe states."""
        controller = self._companion_controller
        if controller is None or self.services is None:
            # Supply-chain diagnostic, DEBUG by default. Open riddle: the predicate
            # is evaluated >=2x per turn (registry state filter + schemas_for_user_text)
            # yet the real machine logged it only once at startup -- to chase it,
            # logging.getLogger("spica.host.app_host").setLevel(logging.DEBUG).
            logger.debug(
                "watch context: None (controller built=%s, services ready=%s)",
                controller is not None, self.services is not None,
            )
            return None
        target = controller.current_watch_target()
        if target is None:
            logger.debug("watch context: None (controller built, no live play)")
            return None
        game_id, window_id = target
        session = controller.session
        if session is None:
            # stop() clears the watch target FIRST, so this is a narrow race;
            # treat it as not playing (NO_ACTIVE_COMPANION) rather than crash.
            logger.debug("watch context: None (target published but session gone)")
            return None
        state = session.state
        logger.debug(
            "watch context: game_id=%s window_id=%s state=%s",
            game_id, window_id, getattr(state, "value", state),
        )
        return WatchContext(
            target=WindowTarget(window_id=window_id, owner_domain="galgame", game_id=game_id),
            locator=self.services.window_locator_adapter,
            capture=self.services.screen_capture_adapter,
            state=state,
        )

    def new_companion_controller(self) -> GalgameCompanionController:
        """Build the galgame companion controller (Path B) -- the start/stop
        orchestration over session + OCR loop + summarizer, persisting to the REAL
        game-memory adapter (spica_data/galgame.sqlite3). Requires ``initialize()``.
        Builder used by demos/tests; the main program goes through the cached
        ``companion_controller()`` singleton (stage 2)."""
        return GalgameCompanionController(
            self.services.game_memory_adapter,
            self.services.screen_capture_adapter,
            self.services.window_locator_adapter,
            self.services.ocr_adapter,
            summarizer=self._new_summarizer(),
            emit=self.companion_sink,
            record_history=self._record_play_history,  # B 方案: host 持写权限
            character_id=self.character_scope.character_id,
            user_id=self.character_scope.user_id,
            summary_trigger_chars=self.config.galgame.summary_trigger_chars,
            interval_seconds=self.config.galgame.ocr_interval_seconds,
            play_history_card_max_chars=self.config.galgame.play_history_card_max_chars,
            binding_sink=self.domain_router,  # Phase 8-c1: publish-LAST/clear-FIRST 镜像
        )

    def _request_song(self, query: str) -> dict[str, Any]:
        """sing_song write closure (B2/P2, the first "act" tool). Resolve the song
        NOW (sub-second netease search, so the turn's acknowledgment can NAME it),
        hand the job to the UI via SongRequestEvent (RuntimeEvent sink -> bridge
        Qt signal -> SongController starts the SongWorker), and return -- by the
        time the followup streams, the song is already preparing in parallel.
        ``self._song_search`` is the injection seam (tests swap in a fake)."""
        if not song_enabled(self.song_config):
            # Hard gate IN the authority-holding closure (铁律 #9): ``available``
            # only filters schema supply and tools.run never re-checks it, so a
            # forced/hallucinated call must die HERE -- zero network search,
            # zero SongRequestEvent.
            raise ScreenToolError("SONG_DISABLED", "唱歌功能当前已在配置中关闭(song.enabled=false)。")
        request = SongRequest(query=query, title=None, artist=None, user_text=query)
        try:
            # P0b 2b: limit comes from the resolved song config (the pipeline
            # already read search.limit from config -- this closure used the
            # hard default 20; same value today, now a single source).
            limit = int(self.song_config.get("search", {}).get("limit", 20))
            song = self._song_search(request, limit=limit)
        except Exception as exc:  # noqa: BLE001 -- search failure = tool error envelope
            raise ScreenToolError(
                "SONG_NOT_FOUND", f"没有找到可以唱的歌：{query}"
            ) from exc
        self.companion_sink(
            SongRequestEvent(query=query, title=song.title, artist=song.artist_text)
        )
        return {"title": song.title, "artist": song.artist_text}

    # CompanionBeat content cap (Phase 9 step 2): a note is ONE summary-level
    # sentence -- the [COMPANION_CONTEXT] section carries the 5 most recent
    # beats, so an unclamped note could paste a whole story dump into prompts.
    _GAME_OBSERVATION_MAX_CHARS = 200

    def _record_game_observation(self, content: str) -> str:
        """note_game_observation write closure (Phase 9 step 2): persist a
        dialogue-confirmed observation as a CompanionBeat in the GAME memory
        (spica_data/galgame.sqlite3) -- NEVER the character's long-term store
        (CLAUDE.md #8; contrast _record_play_history below, which deliberately
        writes the character store). source="spica" = recorded by her in
        dialogue ("user" stays manual feed, "auto" reserved for future
        non-dialogue writers); session_id=None in v1 (beats are retrieved by
        game+user+character, never by session)."""
        binding = self._companion_game_binding()
        if binding is None:
            # The tool checks first; this guards the stop()-raced window between
            # the tool's check and this write (binding cleared FIRST on stop).
            raise ScreenToolError(
                "NO_ACTIVE_COMPANION", "当前没有正在陪玩的游戏，无法记录游戏观察。"
            )
        request = binding.game_context_request
        beat = CompanionBeat(
            beat_id=uuid.uuid4().hex,
            game_id=request.game_id,
            playthrough_id=request.playthrough_id or "default",
            session_id=None,
            type="shared_observation",
            content=content[: self._GAME_OBSERVATION_MAX_CHARS],
            source="spica",
            created_at=utc_now_iso(),
            scope={
                "character_id": self.character_scope.character_id,
                "user_id": self.character_scope.user_id,
                "game_id": request.game_id,
            },
        )
        return self.services.game_memory_adapter.add_companion_beat(beat)

    def _record_play_history(self, game_id: str, card: str) -> None:
        """Play-history bridge (B 方案, FINDINGS #15): upsert the card into the
        character's DEFAULT-scope long-term memory so plain-chat retrieval finds
        it. memory_key is the game -> one play of the same game OVERWRITES the
        previous card (store.upsert_memory's explicit-key UPDATE semantics);
        scope="relationship" renders as "スピカと麦" in the prompt."""
        character_id = self.character_scope.character_id
        self.services.memory_store.upsert_memory(
            conversation_id=scoped_conversation_id(character_id, "default"),
            scope="relationship",
            content=card,
            importance=0.85,  # high (survives pruning) but not pinned (no +2.0 retrieval floor)
            memory_key=f"galgame_history:{game_id}",
            memory_type="experience",
            source="galgame_companion",
        )

    def recover_dangling_companion_sessions(self) -> list[str]:
        """Crash recovery (Phase 8 / §12): 補總結 sessions left active/paused with no
        ended_at, then mark them ended. The "ask the user to resume" UI is deferred.

        B 方案 (FINDINGS #15): a recovered session never ran the normal stop() leg,
        so its play-history card is written HERE (deduped per game). Best-effort."""
        summarizer = self._new_summarizer()
        if summarizer is None:
            return []
        game_memory = self.services.game_memory_adapter
        dangling = {ps.session_id: ps for ps in game_memory.dangling_play_sessions()}
        recovered = recover_dangling_sessions(game_memory, summarizer)
        user_name = self.character_scope.user_id
        seen_games: set[str] = set()
        for session_id in recovered:
            play_session = dangling.get(session_id)
            if play_session is None or play_session.game_id in seen_games:
                continue
            seen_games.add(play_session.game_id)
            try:
                card = compose_play_history(
                    game_memory, play_session.game_id, play_session.playthrough_id,
                    user_name=user_name,
                    max_chars=self.config.galgame.play_history_card_max_chars,
                )
                if card:
                    self._record_play_history(play_session.game_id, card)
            except Exception as exc:  # noqa: BLE001 -- best-effort, never fail recovery
                logger.warning(
                    "play history record failed for recovered game %s: %s",
                    play_session.game_id, exc, exc_info=True,
                )
        return recovered

    def new_game_binder(self, session: GalgameCompanionSession | None = None) -> GameBinder:
        """Build a launch + window-binding coordinator (Phase 5) wired to the
        launcher / locator / game-memory adapters and the companion sink. The
        ``session`` is the one this binder will flip to ``game_launched``;
        ``None`` (stage 3) = selection/persistence-only mode -- the companion
        controller's own start() does the binding afterwards."""
        return GameBinder(
            self.services.game_launcher_adapter,
            self.services.window_locator_adapter,
            self.services.game_memory_adapter,
            session,
            emit=self.companion_sink,
        )

    def new_ocr_stream_runner(self, session: GalgameCompanionSession) -> OcrStreamRunner:
        """Build the background OCR text-stream runner (Phase 7) for an active play
        session, wired to the capture / locator / OCR adapters. Caller starts it with
        the resolved window id + calibrated region ratios."""
        return OcrStreamRunner(
            session,
            self.services.screen_capture_adapter,
            self.services.window_locator_adapter,
            self.services.ocr_adapter,
        )

    def new_ocr_calibrator(self) -> GalgameOcrCalibrator:
        """Build an OCR region calibration + test coordinator (Phase 6) wired to the
        capture / locator / OCR / game-memory adapters and the companion sink."""
        return GalgameOcrCalibrator(
            self.services.screen_capture_adapter,
            self.services.window_locator_adapter,
            self.services.ocr_adapter,
            self.services.game_memory_adapter,
            emit=self.companion_sink,
        )

    def _resolve_tts_assembly(self, tts_config: dict[str, Any]) -> tuple[str, Any | None, Any]:
        """TTS assembly gate (extracted as a test seam): resolves (provider,
        tool, adapter) from the character package's tts config + the tts.enabled
        switch. tts.enabled=false swaps in the no-model text_only adapter --
        NEVER a None adapter: qt_overlay skips the whole startup warmup worker
        (and the dangling-session recovery chained on its finished/failed)
        when tts_adapter is None."""
        provider = str(
            tts_config.get("provider")
            or tts_config.get("tts_provider")
            or "gptsovits_current"
        )
        if not self.config.tts.enabled:
            logger.info(
                "TTS disabled by config (tts.enabled=false) -> text_only adapter, GPT-SoVITS not assembled"
            )
            # DIRECT construction, deliberately NOT via the registry: a plugin
            # could re-register "text_only", and the disabled switch's no-model
            # guarantee must not be overridable by any registry state.
            from agent_tools.tts.adapters import TextOnlyTTSAdapter

            return "text_only", None, TextOnlyTTSAdapter()
        tool = GPTSoVITSTool() if provider in CURRENT_GPTSOVITS_PROVIDERS else None
        adapter = self.registry.resolve_tts(provider, config=tts_config, service=tool)
        return provider, tool, adapter

    def _install_moondream_seam(self) -> None:
        """Moondream seam gate (extracted as a test seam). Enabled: build the
        configured provider (moondream_hf; the default moondream_local factory
        returns None = legacy path) and install it. Disabled: never install --
        the weights must have no load path (tools are already unsupplied and
        hard-refuse before capture) -- AND clear process-level leftovers (seam
        + manager singleton) so a SAME-PROCESS re-initialize after flipping the
        switch releases an already-loaded model instead of keeping its VRAM."""
        if self.screen_config.enabled:
            moondream_provider = build_moondream_provider(self.screen_config.provider)
            from agent_tools.function_tools.screen.backends.moondream_runtime import (
                set_active_moondream_provider,
            )

            # ALWAYS install the decision -- None means the legacy
            # moondream_local path, and it must OVERWRITE a stale hf seam on a
            # same-process provider switch (hf -> local), or local configs keep
            # routing to the old provider and fail on the mismatch.
            set_active_moondream_provider(moondream_provider)
            return
        logger.info("screen disabled (screen.enabled=false) -> moondream provider not installed")
        try:
            from agent_tools.function_tools.screen.backends.moondream_runtime import (
                set_active_moondream_provider,
            )
            from agent_tools.function_tools.screen.model_manager import (
                clear_moondream_manager,
            )

            set_active_moondream_provider(None)
            clear_moondream_manager()
        except Exception:  # noqa: BLE001 -- best-effort cleanup, never blocks startup
            logger.debug("moondream process-level cleanup skipped", exc_info=True)

    def warmup(self, on_progress: Callable[[str, str], None]) -> None:
        """Run startup warmup (Phase 6E), reporting progress as
        ``on_progress(stage, message)`` where stage is
        ``"initializing" | "ready" | "error"``.

        Forwards to ``spica.host.warmup.run_warmup`` over the surfaces it uses.
        The UI runs this on a background thread and maps stages to its loading UI;
        keeping this method preserves that call site (``host.warmup(...)``).
        """
        run_warmup(
            self.conversation_surface,
            self.tts_adapter,
            on_progress,
            stt_adapter=self.stt_adapter,
            # config is None on a bare (pre-initialize) host -- keep the
            # historical warm-by-default there (test_warmup.py exercises it).
            stt_warmup_on_startup=(
                bool(self.config.stt.warmup_on_startup) if self.config is not None else True
            ),
        )

    @property
    def management_surface(self) -> Any:
        """Entry point for the settings centre (Phase 8)."""
        return self._management

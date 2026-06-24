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
import time
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
    REACTION_MODE_TABLE,
    ReactionEngine,
    ReactionLexicon,
    ReactionModeParams,
    ScoreResult,
    compose_reaction_directive,
    lexicon_source_mtime,
    load_reaction_lexicon,
    merge_mode_table,
    score_beat,
)
from spica.galgame.reaction_judge import GalgameReactionJudge, ReactionJudgeError
from spica.galgame.session import GalgameCompanionSession
from spica.galgame.summarizer import GalgameSummarizer, recover_dangling_sessions
from spica.runtime.context import GameTurnBinding
from spica.runtime.jobs import ThreadJobRunner
from spica.host.agent_assembly import build_agent_services
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
from agent_tools.function_tools.song.config import resolve_effective_song_config
from agent_tools.function_tools.song.models import SongRequest
from agent_tools.function_tools.song.netease import search_best_song
from spica.adapters.visual import build_spica_visual
from agent_tools.tts import CURRENT_GPTSOVITS_PROVIDERS, GPTSoVITSTool, load_tts_config

logger = logging.getLogger(__name__)

# -- P5 v2 reaction-judge host-wiring tunables --------------------------------
# judge-cooldown: minimum seconds between LLM judge calls on the engine worker
# thread (single-consumer, no lock). Caps the judge call rate even when it keeps
# declining (a decline stamps NO budget cooldown, so without this the closure
# would re-judge every non-cooldown beat). budget/cooldown still gate BEFORE the
# scorer (reaction.py L587), so this is an extra throttle, not the only one.
REACTION_JUDGE_COOLDOWN_SECONDS = 15.0
# scene window: tail N unsummarized committed lines the judge sees (the offline
# report's default). The cross-beat run-up the lexicon gate is blind to.
REACTION_JUDGE_WINDOW_LINES = 24
# judge-down fallback (叉口②-b): a pass score above any worth threshold, so the
# engine's worth-scale min_score can't silence a beat the LEXICON scale passed.
_LEXICON_FALLBACK_PASS_SCORE = 1000


class AppHost:
    """Owns the backend services and wires them together at startup."""

    def __init__(self) -> None:
        self.config: AppConfig | None = None
        self.secrets: Secrets | None = None
        self.visual_tool: Any | None = None
        self.tts_tool: Any | None = None
        self.tts_adapter: Any | None = None
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
        # P5 reaction engine (built in initialize() when reaction_mode != off;
        # the arbiter handoff is UI-attached -- same shape as song's
        # request_proactive_turn injection).
        self.reaction_engine: ReactionEngine | None = None
        self._reaction_try_speak: Any | None = None
        self._reaction_lexicons: dict[str | None, ReactionLexicon] = {}
        self._reaction_lexicon_mtimes: dict[str | None, float] = {}
        # P5 v2 LLM judge: built in initialize() when reaction_judge_enabled (None
        # -> the lexicon score_beat stays the scorer, zero diff). _last_at throttles
        # re-judging (worker-thread only, single-consumer -> no lock).
        self._reaction_judge: GalgameReactionJudge | None = None
        self._reaction_judge_last_at: float | None = None
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
            available=lambda: self._companion_watch_context() is not None,
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
            self.tts_provider = str(
                tts_config.get("provider")
                or tts_config.get("tts_provider")
                or "gptsovits_current"
            )
            self.tts_tool = GPTSoVITSTool() if self.tts_provider in CURRENT_GPTSOVITS_PROVIDERS else None
            self.tts_adapter = self.registry.resolve_tts(
                self.tts_provider, config=tts_config, service=self.tts_tool
            )
            self.services = build_agent_services(
                self.config,
                self.secrets,
                tts_adapter=self.tts_adapter,
                visual_tool=self.visual_tool,
                character_package=self.character_package,
            )
            # Resolve and inject the LLM / memory adapters by configured name.
            self.services.llm_adapter = self.registry.resolve_llm(
                self.config.llm.provider, client=self.services.llm_client
            )
            self.services.memory_adapter = self.registry.resolve_memory(
                self.config.memory.provider,
                store=self.services.memory_store,
                recent=self.services.recent_memory,
            )
            # C7: the turn resolves tools from the registry (inspect_screen ToolPort).
            self.services.tool_registry = self.registry
            # ChatEngine is the conversation core (Phase 6D: SimpleAgent dissolved
            # into ChatEngine + spica/host/agent_assembly).
            self.chat_engine = ChatEngine(self.services, self.config)
            # Stage 2: companion-play auto-injection. The provider is LAZY (reads
            # the controller singleton at call time), so wiring order is free and a
            # plain chat turn stays byte-identical while no companion play is active.
            self.chat_engine.set_game_binding_provider(self._companion_game_binding)
            # P5 v2: the reaction judge (None unless reaction_judge_enabled). Built
            # BEFORE the engine so the scorer closure sees it on the first beat.
            self._reaction_judge = self._new_reaction_judge()
            # P5: the reaction engine (None while reaction_mode is off -- the
            # tee then never enqueues, zero overhead on the OCR thread).
            self.reaction_engine = self._build_reaction_engine()
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
        """mtime-cached per-game lexicon (step 4-B hot-reload, mirrors
        VisualDiffService): a default.yaml / <game_id>.yaml edit is picked up on
        the next beat without a restart. Shared by the lexicon scorer (judge off)
        and the judge's failure fallback so both see the same hot-reloaded words."""
        mtime = lexicon_source_mtime(game_id)
        if (
            game_id not in self._reaction_lexicons
            or self._reaction_lexicon_mtimes.get(game_id) != mtime
        ):
            self._reaction_lexicons[game_id] = load_reaction_lexicon(game_id)
            self._reaction_lexicon_mtimes[game_id] = mtime
        return self._reaction_lexicons[game_id]

    def _reaction_scorer(self, beat: Any) -> ScoreResult:
        """The scorer behind the engine's ``self._scorer(beat)`` seam (reaction.py
        L592). ENGINE UNTOUCHED: the signature is ``(beat) -> ScoreResult`` and the
        L396 injection are both unchanged.

        - Judge OFF (default) -> lexicon ``score_beat`` (byte-identical to pre-judge,
          zero diff).
        - Judge ON -> LLM worth via the judge, reading a scene WINDOW + arc from
          game_memory (the same data ``_build_game_context_sections`` injects), so
          it no longer misses wordless drama.
        - judge-cooldown -> a worth-0 sentinel (drops below any worth threshold)
          without an LLM call, throttling the rate.
        - ANY judge failure DEGRADES HERE, not in the engine: the engine's worker
          loop swallows scorer exceptions into a silent drop, so catching here is
          the only place a failure becomes lexicon scoring rather than silence."""
        scope = self._reaction_game_scope()
        game_id = scope[0] if scope else None
        lexicon = self._reaction_lexicon_for(game_id)
        if self._reaction_judge is None:
            return score_beat(beat, lexicon)  # judge off: zero-diff lexicon path

        now = time.monotonic()
        if (
            self._reaction_judge_last_at is not None
            and now - self._reaction_judge_last_at < REACTION_JUDGE_COOLDOWN_SECONDS
        ):
            return ScoreResult(0, ("judge_cooldown",))
        if scope is None or self.services is None:
            return self._lexicon_fallback(beat, lexicon)  # no live scope -> lexicon scale

        game_id, playthrough_id, _ = scope
        gm = self.services.game_memory_adapter
        character_id = str(self.config.character.character_id or "spica")
        user_id = str(self.config.character.interlocutor_name or "麦")
        try:
            window = gm.unsummarized_committed_story_lines(game_id, playthrough_id)
            verdict = self._reaction_judge.judge(
                beat_lines=list(beat.lines),
                window_lines=window[-REACTION_JUDGE_WINDOW_LINES:],
                recent_summaries=gm.recent_summaries(game_id, playthrough_id, limit=2),
                progress=gm.get_progress_state(game_id, playthrough_id),
                recent_beats=gm.recent_companion_beats_for_prompt(
                    game_id, user_id, character_id,
                    limit=self.config.galgame.prompt_context_recent_limit,
                ),
            )
        except ReactionJudgeError:
            logger.warning("reaction judge failed -> lexicon fallback", exc_info=True)
            return self._lexicon_fallback(beat, lexicon)
        # Only an ACTUAL judge call stamps the cooldown (cooldown returns / fallback
        # do not), so the throttle measures spacing between real LLM calls.
        self._reaction_judge_last_at = now
        return ScoreResult(
            score=verdict.worth,
            reasons=(f"worth:{verdict.worth}", f"moment:{verdict.moment}", f"angle:{verdict.angle}"),
        )

    def _lexicon_fallback(self, beat: Any, lexicon: ReactionLexicon) -> ScoreResult:
        """叉口②-b: judge unavailable -> lexicon scoring on the LEXICON scale.
        Decide pass/fail against the CODE ``REACTION_MODE_TABLE`` (lexicon weight
        scale) -- NOT the worth-scale ``reaction_table`` the engine gates the judge
        with -- then return a pass/fail-encoded score so the engine's worth
        threshold can never silence a lexicon-passing beat (两套阈, 不沉默不崩)."""
        lex = score_beat(beat, lexicon)
        tier = REACTION_MODE_TABLE.get(self.config.galgame.reaction_mode)
        passed = tier is not None and lex.score >= tier.min_score
        return ScoreResult(
            score=(_LEXICON_FALLBACK_PASS_SCORE if passed else 0),
            reasons=("lexicon_fallback",) + lex.reasons,
        )

    def _write_reaction_beat(self, content: str, meta: dict) -> None:
        """Engine beat_writer (worker thread): persist a reaction CompanionBeat
        under the live play's scope. Play already ended -> logged and dropped."""
        scope = self._reaction_game_scope()
        if scope is None or self.services is None:
            logger.warning("reaction beat dropped: no live play scope to record under")
            return
        game_id, playthrough_id, _ = scope
        character_id = str(self.config.character.character_id or "spica")
        user_id = str(self.config.character.interlocutor_name or "麦")
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
        character_id = str(self.config.character.character_id or "spica")
        user_id = str(self.config.character.interlocutor_name or "麦")
        return self.services.game_memory_adapter.recent_reaction_beats_for_dedupe(
            game_id, user_id, character_id, limit=limit
        )

    def _build_reaction_engine(self) -> ReactionEngine | None:
        """Assemble + start the reaction engine, or None when off. The mode is
        the typed ``galgame.reaction_mode`` (step 4-A), resolve-once (D-P5-4:
        restart-effective; the params lambda is the holder seam a future
        settings panel swaps without touching this assembly)."""
        mode = self.config.galgame.reaction_mode
        override_raw = self.config.galgame.reaction_table
        override = (
            {
                name: ReactionModeParams(
                    min_score=tier.min_score,
                    max_per_window=tier.max_per_window,
                    cooldown_seconds=tier.cooldown_seconds,
                )
                for name, tier in override_raw.items()
            }
            if override_raw
            else None
        )
        params = merge_mode_table(override).get(mode)
        if params is None:
            # "off" by contract; anything else is schema-impossible (Literal),
            # kept as a defensive guard for hand-built configs in tests.
            if mode != "off":
                logger.warning("unknown galgame.reaction_mode %r -- reaction engine stays off", mode)
            return None
        engine = ReactionEngine(
            speak=self._reaction_speak,
            params_provider=lambda: params,
            scorer=self._reaction_scorer,
            beat_writer=self._write_reaction_beat,
            recent_for_dedupe=self._recent_reaction_beats,
            budget_window_seconds=self.config.galgame.reaction_budget_window_seconds,
        )
        engine.start()
        logger.info("reaction engine on (mode=%s)", mode)
        return engine

    def _new_summarizer(self) -> GalgameSummarizer | None:
        # Summary LLM = config.galgame.summary_model, else the dialogue model (Phase 8),
        # over the same resolved LLM adapter. None when no LLM is wired (tests).
        if self.services is None or self.services.llm_adapter is None:
            return None
        summary_model = self.config.galgame.summary_model or self.config.llm.model
        return GalgameSummarizer(self.services.llm_adapter, summary_model)

    def _new_reaction_judge(self) -> GalgameReactionJudge | None:
        """The P5 v2 reaction judge. None unless reaction_judge_enabled AND an LLM
        is wired (so a half-config or a test never builds it). Model =
        reaction_judge_model, else the dialogue model -- same resolved adapter as
        the summarizer (mirrors _new_summarizer)."""
        if not self.config.galgame.reaction_judge_enabled:
            return None
        if self.services is None or self.services.llm_adapter is None:
            return None
        model = self.config.galgame.reaction_judge_model or self.config.llm.model
        return GalgameReactionJudge(self.services.llm_adapter, model)

    def new_companion_session(self) -> GalgameCompanionSession:
        """Build a galgame companion session wired to the game-memory adapter, the
        companion sink, a background ``ThreadJobRunner`` + the summarizer (Phase 8).
        Requires ``initialize()`` first (provides the adapters)."""
        return GalgameCompanionSession(
            self.services.game_memory_adapter,
            emit=self.companion_sink,
            character_id=str(self.config.character.character_id or "spica"),
            user_id=str(self.config.character.interlocutor_name or "麦"),
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

    def _companion_watch_context(self) -> tuple[str, str, Any, Any, Any] | None:
        """Lazy provider for the watch_game_screen tool (Phase 9): the live play's
        (game_id, window_id, locator, capture, session_state), or ``None`` when not
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
        return (
            game_id,
            window_id,
            self.services.window_locator_adapter,
            self.services.screen_capture_adapter,
            state,
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
            character_id=str(self.config.character.character_id or "spica"),
            user_id=str(self.config.character.interlocutor_name or "麦"),
            summary_trigger_chars=self.config.galgame.summary_trigger_chars,
            interval_seconds=self.config.galgame.ocr_interval_seconds,
            play_history_card_max_chars=self.config.galgame.play_history_card_max_chars,
        )

    def _request_song(self, query: str) -> dict[str, Any]:
        """sing_song write closure (B2/P2, the first "act" tool). Resolve the song
        NOW (sub-second netease search, so the turn's acknowledgment can NAME it),
        hand the job to the UI via SongRequestEvent (RuntimeEvent sink -> bridge
        Qt signal -> SongController starts the SongWorker), and return -- by the
        time the followup streams, the song is already preparing in parallel.
        ``self._song_search`` is the injection seam (tests swap in a fake)."""
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
                "character_id": str(self.config.character.character_id or "spica"),
                "user_id": str(self.config.character.interlocutor_name or "麦"),
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
        character_id = str(self.config.character.character_id or "spica")
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
        user_name = str(self.config.character.interlocutor_name or "麦")
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

    def warmup(self, on_progress: Callable[[str, str], None]) -> None:
        """Run startup warmup (Phase 6E), reporting progress as
        ``on_progress(stage, message)`` where stage is
        ``"initializing" | "ready" | "error"``.

        Forwards to ``spica.host.warmup.run_warmup`` over the surfaces it uses.
        The UI runs this on a background thread and maps stages to its loading UI;
        keeping this method preserves that call site (``host.warmup(...)``).
        """
        run_warmup(self.conversation_surface, self.tts_adapter, on_progress)

    @property
    def management_surface(self) -> Any:
        """Entry point for the settings centre (Phase 8)."""
        return self._management

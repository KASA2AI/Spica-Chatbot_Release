"""Streaming orchestrator (Phase 6C).

Moved from agent/streaming_pipeline.py. Orchestrates a streaming turn: run the
sync prep nodes, optionally a tool round, stream LLM text, split into play units,
fan each unit out to the visual + TTS jobs, emit ordered events, and commit
memory. It only *coordinates* the runtime components / ports -- no business
logic (text cleanup, LLM branch, visual/TTS, extraction) lives here.

Tunables (model, play-unit sizes, visual workers) and the LLM port come from the
typed ``deps`` (C3b: ``deps.config`` / ``deps.llm``, never ``services.config``);
direct dict-config callers are bridged via ``TurnDeps.from_legacy_services``. The
turn runs on a typed ``TurnContext`` (C3c) rather than the ``AgentState``
blackboard. Qt-free (CLAUDE.md #1).
"""

from __future__ import annotations

import concurrent.futures
import functools
import queue
import threading
from dataclasses import replace
from typing import Any, Iterator

from spica.runtime.stages import (
    analyze_screen_attachment_node,
    build_prompt_node,
    contribute_context_node,
    load_recent_context_node,
    retrieve_long_term_memory_node,
    validate_input_node,
)
from spica.conversation.reply_parser import EMOTION_LABELS, guess_emotion, normalize_emotion, parse_model_reply
from spica.runtime.services import AgentServices
from spica.conversation.text_normalizer import build_tts_text, normalize_square_brackets_for_speech
from common.timing import now_ms
from spica.runtime.context import StreamedAnswer, TurnContext, is_turn_cancelled
from spica.runtime.deps import TurnDeps
from spica.runtime.exec_strategy import ExecStrategy, Threaded
from spica.runtime.jobs import ThreadJobRunner
from spica.runtime.observer import DefaultTurnObserver
from spica.core.proactive import (
    NO_COMMENT_SENTINEL,
    is_no_comment_answer,
    may_become_no_comment,
)
from spica.runtime.memory_commit import save_stream_memory
from spica.runtime.play_unit_splitter import JsonAnswerExtractor, PlayUnitSplitter
from spica.runtime.sequencer import Sequencer
from spica.runtime.tool_round import STREAM_RESET, prepare_prompt_for_streaming
from spica.runtime.tts_job import synthesize_unit_audio
from spica.runtime.visual_job import build_unit_visual_and_emit

_SENTINEL = object()
_FIRST_UNIT_WARNING_MS = 3000.0


def stream_voice_events(
    ctx: TurnContext,
    services: AgentServices,
    exec_strategy: ExecStrategy | None = None,
    deps: Any = None,
) -> Iterator[dict[str, Any]]:
    request_start_ms = now_ms()
    yield {"event": "status", "data": {"state": "thinking", "message": "thinking"}}

    output_queue: queue.Queue[Any] = queue.Queue()
    producer = threading.Thread(
        target=_produce_stream_events,
        args=(ctx, services, request_start_ms, output_queue, exec_strategy, deps),
        daemon=True,
    )
    producer.start()

    while True:
        item = output_queue.get()
        if item is _SENTINEL:
            break
        yield item
    producer.join(timeout=1)


def _produce_stream_events(
    ctx: TurnContext,
    services: AgentServices,
    request_start_ms: float,
    output_queue: queue.Queue[Any],
    exec_strategy: ExecStrategy | None = None,
    deps: Any = None,
) -> None:
    # Safe defaults so the except / finally below can never NameError if the setup
    # throws before they are built. A setup failure (deps bridge / Threaded pools /
    # observer / jobs construction) must still emit an `error` event and ALWAYS put
    # _SENTINEL, or the consumer (output_queue.get with no timeout) hangs forever.
    first_unit_timer: threading.Timer | None = None
    owns_exec = exec_strategy is None
    jobs: Any = None
    observer: Any = None

    def relative_ms() -> float:
        return round(now_ms() - request_start_ms, 2)

    try:
        unit_timings: list[dict[str, Any]] = []
        created_units: list[str] = []
        first_unit_event = threading.Event()

        # C3b: run on typed deps. Direct (dict-config) callers bridge here; the
        # hot path below reads only deps.config / deps.llm, never services.config.
        deps = deps or TurnDeps.from_legacy_services(services)

        # Concurrency + background jobs are injected per-turn (C2/C5/C6). Streaming
        # passes no exec_strategy and owns both the Threaded pools and a ThreadJobRunner
        # (drained in finally) so `done` is emitted without waiting on the long-term
        # memory commit; the sync path passes Inline and keeps the InlineJobRunner.
        # The three lanes (visual / serial tts / finalize) live in the strategy.
        if exec_strategy is None:
            exec_strategy = Threaded(visual_workers=max(1, deps.config.stream.visual_stream_workers))
        # C5: one per-turn observer wrapping ctx.timing (the sink) -- the single
        # timing/log write path; done.timing stays ctx.timing.
        observer = DefaultTurnObserver(ctx.timing, logger=services.logger)
        jobs = ThreadJobRunner() if owns_exec else deps.jobs
        deps = replace(deps, observer=observer, jobs=jobs)
        ready_futures: list[concurrent.futures.Future[Any]] = []
        # C1 ordered release: finalize workers push (index, payload) onto this
        # INTERNAL queue; only the producer thread drains it through the Sequencer
        # and emits unit_ready in order. completion_queue never leaves this function.
        completion_queue: queue.Queue[tuple[int, dict[str, Any]]] = queue.Queue()
        sequencer: Sequencer[dict[str, Any]] = Sequencer()

        def mark_first_unit_warning(reason: str) -> None:
            if first_unit_event.is_set():
                return
            if observer.snapshot().get("first_unit_warning_ms") is not None:
                return
            warning_ms = relative_ms()
            observer.mark_once("first_unit_warning_ms", warning_ms)
            observer.mark(
                "first_unit_warning",
                f"first playable unit was not created within {int(_FIRST_UNIT_WARNING_MS)}ms",
            )
            observer.mark("first_unit_warning_reason", reason)
            observer.event(
                "chat_stream_first_unit_warning",
                warning_ms,
                conversation_id=ctx.request.conversation_id,
                reason=reason,
            )

        def put_status(state_name: str, message: str) -> None:
            output_queue.put({"event": "status", "data": {"state": state_name, "message": message}})

        def put_unit_event(event_name: str, event_data: dict[str, Any]) -> None:
            output_queue.put({"event": event_name, "data": event_data})

        def drain_ready() -> None:
            # Single-consumer: only the producer thread feeds the Sequencer and emits
            # unit_ready, so ordering needs no lock. Finalize workers only enqueue.
            while True:
                try:
                    index, event_data = completion_queue.get_nowait()
                except queue.Empty:
                    break
                for ready_event in sequencer.complete(index, event_data):
                    output_queue.put({"event": "unit_ready", "data": ready_event})

        def submit_unit(display_text: str) -> None:
            display_text = normalize_square_brackets_for_speech((display_text or "").strip())
            if not display_text:
                return

            index = len(created_units)
            previous_units = list(created_units)
            full_answer_so_far = "".join(previous_units + [display_text])
            emotion = normalize_emotion(ctx.request.emotion_override or guess_emotion(full_answer_so_far))
            unit_timing: dict[str, Any] = {
                "unit_index": index,
                "unit_text_chars": len(display_text),
                "unit_created_ms": relative_ms(),
            }
            unit_timings.append(unit_timing)
            if index == 0:
                observer.mark_once("first_unit_created_ms", unit_timing["unit_created_ms"])
                observer.mark_once("first_sentence_ms", unit_timing["unit_created_ms"])
                if unit_timing["unit_created_ms"] > _FIRST_UNIT_WARNING_MS:
                    mark_first_unit_warning("first_unit_created_after_threshold")
                first_unit_event.set()

            tts_text = build_tts_text(display_text)
            unit = {
                "index": index,
                "display_text": display_text,
                "tts_text": tts_text,
                "emotion": emotion,
                "previous_units": previous_units,
                "full_answer_so_far": full_answer_so_far,
                "timing": unit_timing,
            }
            put_unit_event(
                "unit_text_ready",
                {
                    "index": index,
                    "display_text": display_text,
                    "tts_text": tts_text,
                    "emotion": emotion,
                    "timing": {
                        "unit_created_ms": unit_timing["unit_created_ms"],
                    },
                },
            )
            visual_future = exec_strategy.submit_visual(
                functools.partial(
                    build_unit_visual_and_emit,
                    services, ctx, unit, request_start_ms, observer, put_unit_event,
                )
            )
            tts_future = exec_strategy.submit_tts(
                functools.partial(
                    synthesize_unit_audio,
                    services, ctx, unit, request_start_ms, observer, put_unit_event,
                )
            )
            ready_futures.append(
                exec_strategy.submit_finalize(
                    functools.partial(
                        _finalize_unit,
                        unit, visual_future, tts_future, request_start_ms, observer, completion_queue,
                    )
                )
            )
            created_units.append(display_text)

        first_unit_timer = threading.Timer(
            _FIRST_UNIT_WARNING_MS / 1000.0,
            mark_first_unit_warning,
            args=("timer_threshold",),
        )
        first_unit_timer.daemon = True
        first_unit_timer.start()

        ctx = validate_input_node(ctx, services, deps)
        if ctx.error:
            output_queue.put({"event": "error", "data": {"message": ctx.error.message or "请求无效。"}})
            return

        ctx = load_recent_context_node(ctx, services, deps)
        ctx = retrieve_long_term_memory_node(ctx, services, deps)
        if ctx.request.screen_attachment:
            put_status("tools", "inspecting_screen")
        ctx = analyze_screen_attachment_node(ctx, services, deps)
        if ctx.error:
            output_queue.put({"event": "error", "data": {"message": ctx.error.message or "截图分析失败。"}})
            return
        ctx = build_prompt_node(ctx, services, deps)
        if ctx.error:
            output_queue.put({"event": "error", "data": {"message": ctx.error.message or "请求失败。"}})
            return
        # B3: gated galgame context injection, AFTER build_prompt, BEFORE the LLM.
        # `none` (every plain chat turn) is a byte-level no-op; best-effort, never
        # sets ctx.error, so no extra error gate is needed here.
        ctx = contribute_context_node(ctx, services, deps)

        visual_context = None
        if services.visual_tool is not None and hasattr(services.visual_tool, "prepare_stream_context"):
            visual_context = services.visual_tool.prepare_stream_context(
                requested_costume=ctx.request.visual_overrides.get("costume_set"),
                requested_mode=ctx.request.visual_overrides.get("costume_mode"),
            )
        ctx.metadata["stream_visual_context"] = visual_context

        model = deps.config.llm.model
        prompt_input = ctx.prompt.prompt_input if ctx.prompt else None
        observer.mark("agent_model", model)
        observer.mark("prompt_input_chars", len(str(prompt_input or "")))

        prompt_for_stream, prefetched = prepare_prompt_for_streaming(ctx, services, put_status, deps)
        splitter = PlayUnitSplitter(
            min_chars=deps.config.stream.play_unit_min_chars,
            max_chars=deps.config.stream.play_unit_max_chars,
        )
        extractor = JsonAnswerExtractor()
        raw_model_parts: list[str] = []
        # P5 NO_COMMENT gate (D-P5-5), SYSTEM turns only: while the streamed
        # answer is still sentinel-compatible, units are WITHHELD -- an explicit
        # hold, so swallowing never depends on play_unit_min_chars tuning.
        # Plain turns: hold is False from the start and every new branch below
        # short-circuits (byte-identical behaviour).
        hold_for_sentinel = ctx.request.interaction_mode == "system"
        # The generate phase begins: ctx.answer exists from here on (None during
        # the prep stages above, so they cannot read a not-yet-streamed answer).
        answer = StreamedAnswer()
        ctx.answer = answer

        def handle_raw_delta(delta: str) -> None:
            nonlocal hold_for_sentinel
            if not delta:
                return
            observer.mark_once("first_llm_delta_ms", relative_ms())
            observer.bump("llm_delta_events", 1)
            raw_model_parts.append(delta)
            answer.raw_model_output = "".join(raw_model_parts)
            answer_delta = extractor.feed(answer.raw_model_output)
            if hold_for_sentinel:
                if may_become_no_comment(extractor.answer):
                    return  # withhold: the answer may still be the sentinel
                hold_for_sentinel = False
                answer_delta = extractor.answer  # diverged: release everything withheld
            if not answer_delta:
                return
            previous_sentence_count = splitter.completed_sentence_count
            units = splitter.feed(answer_delta)
            if splitter.completed_sentence_count > previous_sentence_count:
                observer.mark_once("first_sentence_ms", relative_ms())
            for unit_text in units:
                submit_unit(unit_text)

        def reset_stream_state() -> None:
            # STREAM_RESET (tool turns): discard the plain, UNPLAYED tool preamble so
            # only the followup answer reaches raw/extractor/splitter -> final parse +
            # memory carry the real answer, never the preamble. Re-create extractor +
            # splitter (nothing was submitted from a plain preamble, so this is clean).
            nonlocal extractor, splitter
            raw_model_parts.clear()
            answer.raw_model_output = ""
            extractor = JsonAnswerExtractor()
            splitter = PlayUnitSplitter(
                min_chars=deps.config.stream.play_unit_min_chars,
                max_chars=deps.config.stream.play_unit_max_chars,
            )

        if isinstance(prefetched, str):
            # Non-streamed prefetch (Responses no-tool / dormant chain final): one
            # delta -> byte-identical to the pre-streaming behaviour.
            handle_raw_delta(prefetched)
            drain_ready()
        elif prefetched is not None:
            # Streaming chat-tool generator (DeepSeek): yields content deltas live and
            # may yield STREAM_RESET before the followup answer (tool turns).
            for item in prefetched:
                # #1 checkpoint ③b: stop consuming the moment cancel lands. Breaking
                # suspends the generator BEFORE it runs any (further) tool -> a turn
                # cancelled during the preamble never executes the tool at all.
                if is_turn_cancelled(ctx.request):
                    break
                if item is STREAM_RESET:
                    reset_stream_state()
                    continue
                handle_raw_delta(item)
                drain_ready()
        elif not is_turn_cancelled(ctx.request):
            # #1 checkpoint ③a: a turn cancelled during the tool round skips the LLM
            # stream entirely -- don't even open it. Deadline: cancelled None ->
            # `not False` -> identical to the original unconditional `else`.
            request = {"model": model, "input": prompt_for_stream}
            for delta in deps.llm.iter_response_text(request, ctx):
                # #1 checkpoint ③b: stop consuming deltas the moment cancel lands
                # mid-stream (saves tokens + halts further submit_unit -> TTS).
                # Breaking the for-loop suspends the generator; its connection is
                # closed on GC -- we stop CONSUMING, we do not force-abort the HTTP.
                if is_turn_cancelled(ctx.request):
                    break
                handle_raw_delta(delta)
                drain_ready()

        for unit_text in splitter.flush():
            submit_unit(unit_text)
        drain_ready()

        answer.raw_model_output = "".join(raw_model_parts)
        answer.parsed_reply = parse_model_reply(answer.raw_model_output or "")
        answer.answer = normalize_square_brackets_for_speech(answer.parsed_reply["answer"])
        answer.parsed_reply["answer"] = answer.answer
        answer.emotion = normalize_emotion(ctx.request.emotion_override or answer.parsed_reply["emotion"])
        # P5 (D-P5-5): a system turn that answered the NO_COMMENT sentinel is
        # swallowed. The hold above already kept units (and so TTS) at zero;
        # here the no-units fallback is skipped, recent memory is skipped, and
        # the done event carries the CANONICAL sentinel so the UI display
        # suppression and the reaction engine's refund hook recognize it.
        system_silent = hold_for_sentinel and is_no_comment_answer(answer.answer)
        if system_silent:
            answer.answer = NO_COMMENT_SENTINEL
            answer.parsed_reply["answer"] = NO_COMMENT_SENTINEL
            ctx.metadata["system_turn_silent"] = True
        if not system_silent and not created_units and answer.answer:
            fallback_splitter = PlayUnitSplitter(
                min_chars=splitter.min_chars,
                max_chars=splitter.max_chars,
            )
            for unit_text in fallback_splitter.feed(answer.answer) + fallback_splitter.flush():
                submit_unit(unit_text)

        if not system_silent and not is_turn_cancelled(ctx.request):
            # #1 checkpoint ②: a cancelled turn writes no ghost memory -- neither the
            # synchronous recent append nor the backgrounded long-term commit. Deadline:
            # cancelled None -> `not False` True -> equals the original `if not system_silent`.
            save_stream_memory(ctx, services, deps)

        for future in ready_futures:
            future.result()
            drain_ready()
        drain_ready()

        done_ms = relative_ms()
        observer.mark("done_ms", done_ms)
        observer.mark("units_count", len(created_units))
        observer.mark("units", unit_timings)
        snap = observer.snapshot()
        # Telemetry enrichment (behaviour-neutral, log-only): a single parseable
        # per-turn line carrying turn type + prompt size + the FULL-answer TTS sum
        # + the fallback REASON (not just the bool -- deepseek's benign chat path
        # always sets fallback_used=True, so the reason is what flags a real break).
        tts_total_ms = round(
            sum(float((t or {}).get("tts_duration_ms") or 0) for t in unit_timings), 2
        )
        observer.event(
            "chat_stream_done",
            done_ms,
            conversation_id=ctx.request.conversation_id,
            interaction_mode=ctx.request.interaction_mode,
            prompt_input_chars=snap.get("prompt_input_chars"),
            units_count=len(created_units),
            tts_total_ms=tts_total_ms,
            agent_rounds=snap.get("agent_rounds"),
            agent_tool_local_ms=snap.get("agent_tool_local_ms"),
            first_llm_delta_ms=snap.get("first_llm_delta_ms"),
            first_sentence_ms=snap.get("first_sentence_ms"),
            first_unit_created_ms=snap.get("first_unit_created_ms"),
            first_tts_start_ms=snap.get("first_tts_start_ms"),
            first_tts_done_ms=snap.get("first_tts_done_ms"),
            first_unit_ready_ms=snap.get("first_unit_ready_ms"),
            first_visual_ready_ms=snap.get("first_visual_ready_ms"),
            first_audio_ready_ms=snap.get("first_audio_ready_ms"),
            llm_stream_create_ms=snap.get("llm_stream_create_ms"),
            llm_stream_fallback_used=snap.get("llm_stream_fallback_used"),
            llm_stream_fallback_reason=snap.get("llm_stream_fallback_reason"),
        )
        output_queue.put(
            {
                "event": "done",
                "data": {
                    "answer": answer.answer or "",
                    "emotion": answer.emotion or "happy",
                    "emotion_label": EMOTION_LABELS[normalize_emotion(answer.emotion or "happy")],
                    "emotion_reason": answer.parsed_reply.get("emotion_reason") if answer.parsed_reply else "",
                    "units_count": len(created_units),
                    "timing": ctx.timing,
                },
            }
        )
    except Exception as exc:
        output_queue.put({"event": "error", "data": {"message": str(exc)}})
        if observer is not None:
            try:
                observer.event(
                    "chat_stream_error",
                    relative_ms(),
                    conversation_id=ctx.request.conversation_id,
                    error=str(exc),
                )
            except Exception:
                pass
    finally:
        # Always put _SENTINEL last, even if cleanup raises -- the consumer's
        # output_queue.get() has no timeout, so a missing sentinel hangs it forever.
        try:
            if first_unit_timer is not None:
                first_unit_timer.cancel()
            if owns_exec:
                # `done` is already queued; drain the backgrounded long-term commit so
                # no commit thread outlives the turn, then shut the exec pools down.
                if jobs is not None:
                    jobs.drain()
                if exec_strategy is not None:
                    exec_strategy.shutdown()
        finally:
            output_queue.put(_SENTINEL)


def _finalize_unit(
    unit: dict[str, Any],
    visual_future: concurrent.futures.Future[dict[str, Any]],
    tts_future: concurrent.futures.Future[dict[str, Any]],
    request_start_ms: float,
    observer: Any,
    completion_queue: "queue.Queue[tuple[int, dict[str, Any]]]",
) -> None:
    visual = visual_future.result()
    audio = tts_future.result()
    unit_timing = unit["timing"]
    unit_ready_ms = round(now_ms() - request_start_ms, 2)
    unit_timing["unit_ready_ms"] = unit_ready_ms
    if int(unit["index"]) == 0:
        observer.mark_once("first_unit_ready_ms", unit_ready_ms)
    observer.event(
        "chat_stream_unit_ready",
        unit_ready_ms,
        unit_index=unit["index"],
        visual_ms=unit_timing.get("visual_classifier_duration_ms"),
        tts_ms=unit_timing.get("tts_duration_ms"),
        audio_error=audio.get("audio_error"),
    )
    data = {
        "index": unit["index"],
        "display_text": unit["display_text"],
        "tts_text": unit["tts_text"],
        "emotion": unit["emotion"],
        "visual": visual,
        "audio_url": audio.get("audio_url"),
        "audio_path": audio.get("audio_path"),
        "timing": {
            "visual_ms": unit_timing.get("visual_classifier_duration_ms"),
            "tts_ms": unit_timing.get("tts_duration_ms"),
            "unit_ready_ms": unit_ready_ms,
        },
    }
    if audio.get("audio_error"):
        data["audio_error"] = audio["audio_error"]
    completion_queue.put((int(unit["index"]), data))

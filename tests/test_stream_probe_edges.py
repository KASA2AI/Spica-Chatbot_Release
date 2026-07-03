"""Phase 7-c0 characterization: stream/probe edge behavior, GREEN under v1.

Pins the three edges the Phase 7 flips (7-c1 orchestrator stream -> deps.model.
stream; 7-c2 tool_round probe family -> ToolCallingModel) must preserve but no
existing test measures. All assertions were taken from MEASURED v1 behavior on
the REAL chain (client-level fakes -> real OpenAICompatibleAdapter ->
ChatEngine.stream_voice -> orchestrator -> tool_round), never from the plan's
wording -- the Phase 0 #3 discipline: a port/adapter-level fake would stop
measuring anything real the moment the flip lands.

1. MID-STREAM ERROR (Responses path): the stream dies after 2 deltas and the
   non-stream fallback dies too. Measured envelope: the adapter's fallback tree
   SWALLOWS the mid-stream exception and retries non-streaming, so the surfaced
   error is the FALLBACK's message; events are exactly [status, error] -- an
   ``error`` event, NO ``done``, no units; the pre-error deltas are dropped
   (nothing played, nothing in recent memory); the client sees exactly two
   create calls (stream=True, then the non-stream fallback).
2. FOLLOWUP CANCEL (chat tool path): cancel lands DURING the followup stream,
   after STREAM_RESET. The tool executed exactly once, recent memory got ZERO
   appends (no ghost memory), and the followup stream stops being consumed at
   the cancel checkpoint (tool_round's followup delta loop).
3. STREAM_RESET semantics: the plain tool preamble reaches neither the final
   answer NOR recent memory -- only the followup answer survives the reset into
   the final parse + memory.

Every pin carries a non-cancelled/healthy control so it can't pass vacuously
(the test_cancellation discipline).
"""

import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

from agent_tools.function_tools import TOOL_SCHEMAS, default_tool_functions
from agent_tools.tts.schemas import TTSRequest, TTSResult
from memory.recent import RecentMemory
from memory.store import SQLiteMemoryStore
from spica.adapters.game_memory.sqlite import GameMemorySqliteAdapter
from spica.adapters.memory.sqlite import scoped_conversation_id
from spica.config.schema import AppConfig
from spica.core.chat_engine import ChatEngine
from spica.plugins.registry import CapabilityRegistry
from spica.runtime.services import AgentServices

RAW_ANSWER = json.dumps(
    {"answer": "画面上是个女孩。", "emotion": "happy", "emotion_reason": "x"},
    ensure_ascii=False,
)
PREAMBLE = "好的，我看看屏幕。"
QUESTION = "现在画面有什么"

EDGE_TOOL_SCHEMA = {
    "type": "function",
    "name": "edge_probe_tool",
    "description": "phase 7-c0 edge probe",
    "parameters": {"type": "object", "properties": {}},
}


class _RecordingHandler:
    def __init__(self):
        self.calls = 0

    def run(self, **kwargs):
        self.calls += 1
        return json.dumps({"ok": True, "data": {"seen": True}}, ensure_ascii=False)


# ---- shared assembly (the verify_watch_chain / test_chat_tool_round shape) -- #

class _FakeTTS:
    name = "fake_tts"

    def synthesize(self, request):
        assert isinstance(request, TTSRequest)
        return TTSResult(ok=True, provider=self.name, audio_url="/x.wav", audio_path="/tmp/x.wav",
                         chunks=[{"index": 0, "text": request.text, "audio_url": "/x.wav", "audio_path": "/tmp/x.wav"}],
                         timing={"tts_total_ms": 1.0}, duration_ms=1.0)


class _FakeVisual:
    def prepare_stream_context(self, requested_costume=None, requested_mode=None):
        return {"costume": "school", "costume_mode": "fixed", "dialog": {}, "character": {}, "classifier_version": "x"}

    def build_unit_visual_payload(self, **kwargs):
        return {"costume": "school", "costume_mode": "fixed", "classifier_version": "x",
                "selection_source": "x", "selection_error": None,
                "classifier": {"duration_ms": 1.0, "confidence": 0.9, "signals": []},
                "dialog": {}, "character": {},
                "cue": {"index": kwargs["unit_index"], "text": kwargs["current_unit_text"],
                        "expression_id": "002", "hand_pose": "normal", "image_url": "/x.png", "reason": "x"}}


def _build_engine(client, tmp, *, with_edge_tool=False):
    registry = CapabilityRegistry()
    handler = _RecordingHandler()
    if with_edge_tool:
        registry.register_tool(
            EDGE_TOOL_SCHEMA, handler.run, available=lambda: True, intent_gated=False
        )
    services = AgentServices(
        llm_client=client, tts_adapter=_FakeTTS(), visual_tool=_FakeVisual(),
        memory_store=SQLiteMemoryStore(Path(tmp) / "m.sqlite3"),
        recent_memory=RecentMemory(max_turns=3),
        game_memory_adapter=GameMemorySqliteAdapter(Path(tmp) / "g.sqlite3"),
        config={"model": "test-model", "character_profile": "p", "recent_context_limit": 3,
                "long_term_memory_limit": 5, "max_tool_rounds": 3, "character_id": "spica",
                "interlocutor_name": "麦"},
        logger=lambda *a, **k: None,
        tool_functions=default_tool_functions(), tool_schemas=TOOL_SCHEMAS,
    )
    services.tool_registry = registry
    return ChatEngine(services, AppConfig()), handler


def _recent(engine):
    return engine.services.recent_memory.get_recent(scoped_conversation_id("spica", "default"))


def _done_answer(events):
    done = next((e for e in events if e.get("event") == "done"), None)
    return (done or {}).get("data", {}).get("answer", "")


# ---- 1. mid-stream error (Responses path) ----------------------------------- #

class _DyingResponsesAPI:
    """stream=True yields 2 deltas then dies; the non-stream fallback dies too."""

    def __init__(self):
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs.get("stream"):
            def events():
                yield SimpleNamespace(type="response.output_text.delta", delta='{"answer":"你好')
                yield SimpleNamespace(type="response.output_text.delta", delta="呀")
                raise RuntimeError("stream died mid-flight")
            return events()
        raise RuntimeError("fallback endpoint down too")


class _HealthyResponsesAPI:
    def __init__(self):
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        def events():
            yield SimpleNamespace(type="response.output_text.delta", delta=RAW_ANSWER)
            yield SimpleNamespace(
                type="response.completed",
                response=SimpleNamespace(id="done", output_text=RAW_ANSWER, usage=None),
            )
        return events()


def _openai_client(api):
    return SimpleNamespace(base_url="https://api.openai.com/v1", responses=api)


class MidStreamErrorTest(unittest.TestCase):
    """c0-1: measured v1 envelope for 'stream dies mid-flight AND fallback dies'."""

    def test_error_event_no_done_and_no_ghost_state(self):
        api = _DyingResponsesAPI()
        with tempfile.TemporaryDirectory() as tmp:
            engine, _ = _build_engine(_openai_client(api), tmp)
            events = list(engine.stream_voice("你好"))
            recent = _recent(engine)

        # Measured envelope: exactly [status(thinking), error] -- nothing else.
        self.assertEqual([e.get("event") for e in events], ["status", "error"])
        # The SURFACED message is the FALLBACK's error: the adapter's fallback
        # tree swallows the mid-stream exception and retries non-streaming
        # first (the v1 semantics the 7-c1 flip must keep via v2's v1 reuse).
        self.assertEqual(events[-1]["data"]["message"], "fallback endpoint down too")
        self.assertNotIn("done", [e.get("event") for e in events])
        # The two pre-error deltas are DROPPED: no memory write, no ghost turn.
        self.assertEqual(recent, [])
        # The fallback attempt really happened: stream create, then non-stream.
        self.assertEqual(len(api.calls), 2)
        self.assertTrue(api.calls[0].get("stream"))
        self.assertNotIn("stream", api.calls[1])

    def test_control_healthy_stream_reaches_done(self):
        api = _HealthyResponsesAPI()
        with tempfile.TemporaryDirectory() as tmp:
            engine, _ = _build_engine(_openai_client(api), tmp)
            events = list(engine.stream_voice("你好"))
            recent = _recent(engine)

        self.assertEqual(_done_answer(events), "画面上是个女孩。")
        self.assertEqual(len(recent), 1)  # the harness really writes when healthy


# ---- 2 + 3. chat tool path: followup cancel / STREAM_RESET memory ----------- #

class _ChatToolAPI:
    """deepseek-shape chat API: the streaming tool probe returns a tool_call for
    edge_probe_tool (optionally after a plain preamble); the followup request
    (its prompt carries [TOOL_RESULTS]) streams ``followup_pieces`` one delta at
    a time, optionally setting ``cancel_event`` just before yielding the piece
    at ``cancel_before_index``. Records every followup piece actually yielded."""

    def __init__(self, followup_pieces, *, preamble="", cancel_event=None,
                 cancel_before_index=None):
        self.completions = self
        self.followup_pieces = followup_pieces
        self.preamble = preamble
        self.cancel_event = cancel_event
        self.cancel_before_index = cancel_before_index
        self.followup_yielded = []
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        assert kwargs.get("stream"), "this harness only exercises the streaming chain"
        is_followup = "[TOOL_RESULTS]" in kwargs["messages"][0]["content"]
        if not is_followup and kwargs.get("tools"):
            def probe_chunks():
                if self.preamble:
                    yield SimpleNamespace(choices=[SimpleNamespace(
                        delta=SimpleNamespace(content=self.preamble))])
                yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(
                    content=None,
                    tool_calls=[SimpleNamespace(index=0, id="call_1", type="function",
                        function=SimpleNamespace(name="edge_probe_tool", arguments="{}"))]))])
            return probe_chunks()

        def followup_chunks():
            for i, piece in enumerate(self.followup_pieces):
                if self.cancel_event is not None and i == self.cancel_before_index:
                    self.cancel_event.set()
                self.followup_yielded.append(piece)
                yield SimpleNamespace(choices=[SimpleNamespace(
                    delta=SimpleNamespace(content=piece))])
        return followup_chunks()


def _deepseek_client(api):
    return SimpleNamespace(base_url="https://api.deepseek.com/v1", chat=api)


def _split_answer(n):
    """RAW_ANSWER cut into n pieces (all non-empty) so the joined stream parses."""
    step = max(1, len(RAW_ANSWER) // n)
    pieces = [RAW_ANSWER[i:i + step] for i in range(0, len(RAW_ANSWER), step)]
    return pieces


class FollowupCancelTest(unittest.TestCase):
    """c0-2: cancel DURING the followup stream (after STREAM_RESET) -- the tool
    ran exactly once, no ghost memory, the stream stops at the checkpoint."""

    def test_cancel_mid_followup_tool_once_no_memory_stream_stops(self):
        cancel = threading.Event()  # unset during the probe; set mid-followup
        pieces = _split_answer(6)
        api = _ChatToolAPI(pieces, cancel_event=cancel, cancel_before_index=1)
        with tempfile.TemporaryDirectory() as tmp:
            engine, handler = _build_engine(_deepseek_client(api), tmp, with_edge_tool=True)
            events = list(engine.stream_voice(QUESTION, cancelled=cancel))
            recent = _recent(engine)

        # The tool executed EXACTLY once (before the cancel landed) -- the
        # cancel must not re-run it and must not have blocked the first run.
        self.assertEqual(handler.calls, 1)
        # No ghost memory: the cancelled turn skips save_stream_memory whole.
        self.assertEqual(recent, [])
        # The followup stream stopped being consumed at the checkpoint: the
        # piece that set the flag was the last one pulled (tool_round's
        # followup delta loop breaks before requesting the next).
        self.assertEqual(api.followup_yielded, pieces[:2])
        self.assertLess(len(api.followup_yielded), len(pieces))
        # And no done-with-answer was fabricated from the truncated stream.
        self.assertNotIn("画面上是个女孩。", _done_answer(events))

    def test_control_uncancelled_followup_completes(self):
        pieces = _split_answer(6)
        api = _ChatToolAPI(pieces)  # no cancel event
        with tempfile.TemporaryDirectory() as tmp:
            engine, handler = _build_engine(_deepseek_client(api), tmp, with_edge_tool=True)
            events = list(engine.stream_voice(QUESTION))
            recent = _recent(engine)

        self.assertEqual(handler.calls, 1)
        self.assertEqual(api.followup_yielded, pieces)  # fully consumed
        self.assertEqual(_done_answer(events), "画面上是个女孩。")
        self.assertEqual(len(recent), 1)


class StreamResetMemoryTest(unittest.TestCase):
    """c0-3: STREAM_RESET drops the plain tool preamble from the final answer
    AND from recent memory -- only the followup answer survives the reset."""

    def test_preamble_reaches_neither_answer_nor_memory(self):
        api = _ChatToolAPI([RAW_ANSWER], preamble=PREAMBLE)
        with tempfile.TemporaryDirectory() as tmp:
            engine, handler = _build_engine(_deepseek_client(api), tmp, with_edge_tool=True)
            events = list(engine.stream_voice(QUESTION))
            recent = _recent(engine)

        answer = _done_answer(events)
        self.assertEqual(answer, "画面上是个女孩。")  # followup answer only
        self.assertNotIn(PREAMBLE, answer)
        self.assertEqual(handler.calls, 1)  # the tool really ran
        # Memory carries exactly the followup answer; the preamble is nowhere in
        # the persisted turn (raw was reset before the followup streamed).
        self.assertEqual(len(recent), 1)
        self.assertEqual(recent[0]["assistant_text"], "画面上是个女孩。")
        self.assertNotIn(PREAMBLE, json.dumps(recent[0], ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()

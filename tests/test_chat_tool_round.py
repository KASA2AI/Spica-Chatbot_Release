"""Chat Completions tool round (FINDINGS #18 fix) -- pinned on the REAL chains.

Root cause being guarded: ``prepare_prompt_for_streaming`` used to skip the tool
probe entirely for chat-completions-preferring clients (DeepSeek), so tools
passed ``schemas_for_user_text`` but never reached the request body. The old
triage tests asserted on the sync path while the real machine streamed -- so
every test here drives the FULL entry the production code uses:

* streaming = ``ChatEngine.stream_voice`` -> orchestrator -> tool_round (the
  ChatWorker entry), assembly lifted from ``scripts/verify_watch_chain.py``.
  NOTE ``run_voice`` drives the SAME orchestrator (Inline strategy) -- there is
  no separate production entry over call_llm_node;
* the compat sync chain (golden-covered) = ``run_voice_pipeline`` ->
  call_llm_node, driven directly since nothing in production reaches it.

Contracts:
1. deepseek-shape client + watch supply -> the chat probe carries tools (nested
   ``{"type": "function", "function": {...}}``), the returned tool_call executes
   the real WatchGameScreenTool (capturing log), the followup prompt carries
   [TOOL_RESULTS], and the final answer parses normally.
2. deepseek-shape client + NO tool supply -> the request body is byte-identical
   to the pre-fix one: exactly {"model", "messages", "stream"}, no tools key.
3. ``to_chat_completions_tools`` flat->nested conversion (pure function).
4. Responses-shape client zero regression: probe still sends the FLAT schemas.
"""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from PIL import Image

from agent_tools.function_tools import TOOL_SCHEMAS, default_tool_functions
from agent_tools.tts.schemas import TTSRequest, TTSResult
from memory.recent import RecentMemory
from memory.store import SQLiteMemoryStore
from spica.adapters.game_memory.sqlite import GameMemorySqliteAdapter
from spica.adapters.llm.openai_compatible import to_chat_completions_tools
from spica.adapters.tools.watch_game_screen import WatchGameScreenTool
from spica.config.schema import AppConfig
from spica.core.chat_engine import ChatEngine
from spica.galgame.models import game_conversation_id
from spica.galgame.session import GalgameState
from spica.plugins.registry import CapabilityRegistry
from spica.ports.screen_capture import CaptureImage
from spica.ports.window_locator import WindowGeometry
from spica.runtime.context import GameContextRequest, GameTurnBinding, TurnContext, TurnRequest
from spica.runtime.services import AgentServices
from spica.runtime.sync_chain import run_voice_pipeline

RAW_ANSWER = json.dumps(
    {"answer": "画面上是个女孩。", "emotion": "happy", "emotion_reason": "x"},
    ensure_ascii=False,
)
QUESTION = "现在画面有什么"
PLAIN_QUESTION = "今天过得怎么样"


# ---- fake LLM clients (record every call) --------------------------------- #

class _ResponsesAPI:
    def __init__(self, calls):
        self._calls = calls

    def create(self, **kwargs):
        self._calls.append(("responses.create", kwargs))
        if kwargs.get("stream"):
            def events():
                yield SimpleNamespace(type="response.output_text.delta", delta=RAW_ANSWER)
                yield SimpleNamespace(
                    type="response.completed",
                    response=SimpleNamespace(id="done", output_text=RAW_ANSWER, usage=None),
                )
            return events()
        if kwargs.get("tools"):
            return SimpleNamespace(
                id="probe",
                output=[SimpleNamespace(
                    type="function_call", name="watch_game_screen",
                    arguments=json.dumps({"question": QUESTION}, ensure_ascii=False),
                )],
                output_text="", usage=None,
            )
        return SimpleNamespace(id="oneshot", output=[], output_text=RAW_ANSWER, usage=None)


class _ChatCompletionsAPI:
    def __init__(self, calls, decline_tools=False, preamble=""):
        self._calls = calls
        self._decline_tools = decline_tools
        self._preamble = preamble  # optional plain-text preamble before the tool_call
        self.completions = self

    def create(self, **kwargs):
        self._calls.append(("chat.completions.create", kwargs))
        is_followup = "[TOOL_RESULTS]" in kwargs["messages"][0]["content"]
        want_tool = bool(kwargs.get("tools")) and not self._decline_tools and not is_followup
        if kwargs.get("stream"):
            if want_tool:
                # Streaming tool probe -> an optional plain-text preamble (the shape
                # deepseek really emits), then tool_call deltas SPLIT across chunks
                # (name then arguments), exercising tc.index accumulation.
                preamble = self._preamble

                def chunks():
                    if preamble:
                        yield SimpleNamespace(choices=[SimpleNamespace(
                            delta=SimpleNamespace(content=preamble))])
                    yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(
                        content=None,
                        tool_calls=[SimpleNamespace(index=0, id="call_1", type="function",
                            function=SimpleNamespace(name="watch_game_screen", arguments=""))]))])
                    yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(
                        content=None,
                        tool_calls=[SimpleNamespace(index=0, function=SimpleNamespace(
                            name=None,
                            arguments=json.dumps({"question": QUESTION}, ensure_ascii=False)))]))])
                return chunks()

            def chunks():  # streamed content (no-tool answer / declined probe / followup)
                yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=RAW_ANSWER))])
            return chunks()
        if want_tool:
            # NON-streaming probe (the frozen sync chain via call_llm_node).
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
                content="",
                tool_calls=[SimpleNamespace(id="call_1", type="function",
                    function=SimpleNamespace(
                        name="watch_game_screen",
                        arguments=json.dumps({"question": QUESTION}, ensure_ascii=False)))],
            ))], usage=None)
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=RAW_ANSWER))], usage=None)


def _deepseek_client(calls, decline_tools=False):
    return SimpleNamespace(base_url="https://api.deepseek.com/v1",
                           responses=_ResponsesAPI(calls),
                           chat=_ChatCompletionsAPI(calls, decline_tools=decline_tools))


def _openai_client(calls):
    return SimpleNamespace(base_url="https://api.openai.com/v1",
                           responses=_ResponsesAPI(calls))


# ---- the rest of the verify_watch_chain.py assembly ------------------------ #

class _FakeTTS:
    name = "fake_tts"

    def synthesize(self, request):
        assert isinstance(request, TTSRequest)
        return TTSResult(ok=True, provider=self.name, audio_url="/x.wav", audio_path="/tmp/x.wav",
                         chunks=[{"index": 0, "text": request.text, "audio_url": "/x.wav", "audio_path": "/tmp/x.wav"}],
                         timing={"tts_total_ms": 1.0}, duration_ms=1.0)


class _FakeVisual:
    def build_visual_payload(self, answer, emotion, requested_costume=None, requested_mode=None):
        return {"costume": "school", "classifier_version": "x", "cues": [{"index": 0, "text": answer}]}

    def prepare_stream_context(self, requested_costume=None, requested_mode=None):
        return {"costume": "school", "costume_mode": "fixed", "dialog": {}, "character": {}, "classifier_version": "x"}

    def build_unit_visual_payload(self, **kwargs):
        return {"costume": "school", "costume_mode": "fixed", "classifier_version": "x",
                "selection_source": "x", "selection_error": None,
                "classifier": {"duration_ms": 1.0, "confidence": 0.9, "signals": []},
                "dialog": {}, "character": {},
                "cue": {"index": kwargs["unit_index"], "text": kwargs["current_unit_text"],
                        "expression_id": "002", "hand_pose": "normal", "image_url": "/x.png", "reason": "x"}}


class _WatchLocator:
    def get_window_geometry(self, window_id):
        return WindowGeometry(0, 0, 320, 200)


class _WatchCapture:
    def capture_rect(self, left, top, width, height):
        return CaptureImage(image=Image.new("RGB", (width, height), (30, 30, 30)),
                            width=width, height=height)


class _WatchAnalysis:
    def __init__(self):
        self.calls = []

    def analyze_image(self, image, mode, prompt=None, **kwargs):
        self.calls.append((mode, prompt))
        return {"schema_version": "screen_observation.v1", "request": {"target": mode},
                "capture": {"source": "automatic_screenshot"},
                "followup": {"context_for_next_turn": "画面上是一个女孩。"}}


def _build_engine(client, tmp, *, with_watch):
    analysis = _WatchAnalysis()
    registry = CapabilityRegistry()
    if with_watch:
        watch = WatchGameScreenTool(
            analysis,
            # privacy gate (review #1): the provider now carries the session state
            lambda: ("limelight", "0x07e00005", _WatchLocator(), _WatchCapture(),
                     GalgameState.PLAYING),
        )
        registry.register_tool(watch.schema(), watch.run, available=lambda: True, intent_gated=False)
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
    engine = ChatEngine(services, AppConfig())
    if with_watch:
        engine.set_game_binding_provider(lambda: GameTurnBinding(
            conversation_id=game_conversation_id("limelight"),
            game_context_request=GameContextRequest(mode="active", game_id="limelight"),
        ))
    return engine, analysis


def _stream(engine, question):
    events = list(engine.stream_voice(question))
    done = next((e for e in events if e.get("event") == "done"), None)
    answer = (done or {}).get("data", {}).get("answer", "")
    statuses = [e.get("data", {}) for e in events if e.get("event") == "status"]
    return answer, statuses


def _stream_answer(engine, question):
    return _stream(engine, question)[0]


def _nested_names(tools):
    return [(t.get("function") or {}).get("name") for t in tools]


class StreamChatToolRoundTest(unittest.TestCase):
    """Contract 1: the real streaming chain executes tools on the chat path."""

    def test_deepseek_stream_probe_carries_tools_and_executes(self):
        calls = []
        with tempfile.TemporaryDirectory() as tmp:
            engine, analysis = _build_engine(_deepseek_client(calls), tmp, with_watch=True)
            with self.assertLogs("spica.adapters.tools.watch_game_screen", level="INFO") as logs:
                answer, statuses = _stream(engine, QUESTION)

        self.assertEqual(answer, "画面上是个女孩。")
        # Status contract: nothing during the probe; per-tool status when the
        # tool ACTUALLY runs; "processing_tools" never appears on this chain.
        messages = [s.get("message") for s in statuses]
        self.assertIn("tool:watch_game_screen", messages)
        self.assertNotIn("processing_tools", messages)
        # Probe: chat.completions, now STREAMING (Plan: streaming tool probe), tools
        # in the NESTED chat format. The followup also streams -> two chat calls.
        methods = [m for m, _ in calls]
        self.assertEqual(methods, ["chat.completions.create", "chat.completions.create"])
        probe = calls[0][1]
        self.assertTrue(probe["stream"])  # streaming probe (was non-streaming pre-change)
        self.assertEqual(_nested_names(probe["tools"]), ["watch_game_screen"])
        for tool in probe["tools"]:
            self.assertEqual(tool["type"], "function")
            self.assertIn("parameters", tool["function"])
        # The tool really ran (Moondream port called with the user question).
        self.assertEqual(analysis.calls, [("game_window", QUESTION)])
        self.assertTrue(any("capturing" in line for line in logs.output))
        # Followup: streaming, NO tools, prompt carries the tool results.
        followup = calls[1][1]
        self.assertTrue(followup.get("stream"))
        self.assertNotIn("tools", followup)
        followup_text = followup["messages"][0]["content"]
        self.assertIn("[TOOL_RESULTS]", followup_text)
        self.assertIn("watch_game_screen", followup_text)


class StreamPreambleDroppedTest(unittest.TestCase):
    """edge 1 + RESET: deepseek often streams a PLAIN preamble ("好的我看看屏幕")
    before the tool_call. The JsonAnswerExtractor drops it (no "answer" field) so
    nothing is spoken, and STREAM_RESET clears it from raw before the followup -- so
    the final answer is the followup ONLY, never the preamble."""

    def test_plain_preamble_before_tool_not_played_and_raw_reset(self):
        calls = []
        with tempfile.TemporaryDirectory() as tmp:
            client = _deepseek_client(calls)
            client.chat._preamble = "好的，我看看屏幕。"  # plain preamble before the tool_call
            engine, analysis = _build_engine(client, tmp, with_watch=True)
            answer, _ = _stream(engine, QUESTION)

        # The followup answer is spoken; the preamble is NOT part of it.
        self.assertEqual(answer, "画面上是个女孩。")
        self.assertNotIn("好的", answer)
        self.assertNotIn("看屏幕", answer)
        self.assertEqual(analysis.calls, [("game_window", QUESTION)])  # the tool still ran


class StreamChatNoToolsByteIdenticalTest(unittest.TestCase):
    """Contract 2: with no tool supply the request body cannot change by a byte."""

    def test_deepseek_stream_plain_chat_request_shape(self):
        calls = []
        with tempfile.TemporaryDirectory() as tmp:
            engine, _ = _build_engine(_deepseek_client(calls), tmp, with_watch=False)
            answer = _stream_answer(engine, PLAIN_QUESTION)

        self.assertEqual(answer, "画面上是个女孩。")
        self.assertEqual(len(calls), 1)
        method, kwargs = calls[0]
        self.assertEqual(method, "chat.completions.create")
        # The exact pre-fix request body: model + messages + stream, nothing else.
        self.assertEqual(set(kwargs), {"model", "messages", "stream"})
        self.assertTrue(kwargs["stream"])

    def test_deepseek_run_voice_plain_chat_request_shape(self):
        # run_voice drives the same orchestrator (Inline) -- pin its entry too.
        calls = []
        with tempfile.TemporaryDirectory() as tmp:
            engine, _ = _build_engine(_deepseek_client(calls), tmp, with_watch=False)
            result = engine.run_voice(PLAIN_QUESTION)

        self.assertEqual(result.get("answer"), "画面上是个女孩。")
        self.assertEqual(len(calls), 1)
        method, kwargs = calls[0]
        self.assertEqual(method, "chat.completions.create")
        self.assertEqual(set(kwargs), {"model", "messages", "stream"})

    def test_deepseek_sync_chain_plain_chat_request_shape(self):
        # The compat sync chain's no-tools chat branch (complete_chat) untouched:
        # exactly model + messages, no stream, no tools.
        calls = []
        with tempfile.TemporaryDirectory() as tmp:
            engine, _ = _build_engine(_deepseek_client(calls), tmp, with_watch=False)
            ctx = run_voice_pipeline(
                TurnContext(TurnRequest(conversation_id="c1", user_input=PLAIN_QUESTION)),
                engine.services,
            )

        self.assertIsNone(ctx.error)
        self.assertEqual(ctx.answer.answer, "画面上是个女孩。")
        self.assertEqual(len(calls), 1)
        method, kwargs = calls[0]
        self.assertEqual(method, "chat.completions.create")
        self.assertEqual(set(kwargs), {"model", "messages"})


class SyncChainChatToolRoundTest(unittest.TestCase):
    """Same lesion in call_llm_node (compat sync chain, golden-covered), fixed
    together -- driven via run_voice_pipeline since production never reaches it."""

    def test_deepseek_sync_chain_probe_carries_tools_and_executes(self):
        calls = []
        with tempfile.TemporaryDirectory() as tmp:
            engine, analysis = _build_engine(_deepseek_client(calls), tmp, with_watch=True)
            ctx = run_voice_pipeline(
                TurnContext(TurnRequest(conversation_id="c1", user_input=QUESTION)),
                engine.services,
            )

        self.assertIsNone(ctx.error)
        self.assertEqual(ctx.answer.answer, "画面上是个女孩。")
        chat_calls = [k for m, k in calls if m == "chat.completions.create"]
        self.assertEqual(len(chat_calls), 2)
        self.assertEqual(_nested_names(chat_calls[0]["tools"]), ["watch_game_screen"])
        # The sync loop re-offers tools every round (mirror of the Responses
        # loop); the followup prompt carries the tool results.
        self.assertIn("tools", chat_calls[1])
        self.assertIn("[TOOL_RESULTS]", chat_calls[1]["messages"][0]["content"])
        self.assertEqual(analysis.calls, [("game_window", QUESTION)])


class StreamResponsesRegressionTest(unittest.TestCase):
    """Contract 4: the Responses path is untouched -- probe sends FLAT schemas."""

    def test_openai_stream_probe_unchanged(self):
        calls = []
        with tempfile.TemporaryDirectory() as tmp:
            engine, analysis = _build_engine(_openai_client(calls), tmp, with_watch=True)
            answer, statuses = _stream(engine, QUESTION)

        self.assertEqual(answer, "画面上是个女孩。")
        messages = [s.get("message") for s in statuses]
        self.assertIn("tool:watch_game_screen", messages)
        self.assertNotIn("processing_tools", messages)
        methods = [m for m, _ in calls]
        self.assertEqual(methods, ["responses.create", "responses.create"])
        probe = calls[0][1]
        # Flat Responses schemas pass through UNconverted on this path.
        self.assertEqual([t.get("name") for t in probe["tools"]], ["watch_game_screen"])
        self.assertTrue(all("function" not in t for t in probe["tools"]))
        self.assertEqual(analysis.calls, [("game_window", QUESTION)])
        followup = calls[1][1]
        self.assertTrue(followup.get("stream"))
        self.assertNotIn("tools", followup)

    def test_openai_stream_plain_chat_request_shape(self):
        calls = []
        with tempfile.TemporaryDirectory() as tmp:
            engine, _ = _build_engine(_openai_client(calls), tmp, with_watch=False)
            answer = _stream_answer(engine, PLAIN_QUESTION)

        self.assertEqual(answer, "画面上是个女孩。")
        self.assertEqual(len(calls), 1)
        method, kwargs = calls[0]
        self.assertEqual(method, "responses.create")
        self.assertEqual(set(kwargs), {"model", "input", "stream"})


class ProbeStatusSilentTest(unittest.TestCase):
    """The probe itself emits NO status: when the model declines the tools the
    user keeps seeing the plain pending dots, never '正在处理工具'."""

    def test_probe_declined_emits_no_tools_status(self):
        calls = []
        with tempfile.TemporaryDirectory() as tmp:
            engine, analysis = _build_engine(
                _deepseek_client(calls, decline_tools=True), tmp, with_watch=True)
            answer, statuses = _stream(engine, QUESTION)

        self.assertEqual(answer, "画面上是个女孩。")
        # The probe still happened (tools were offered)...
        self.assertEqual(_nested_names(calls[0][1]["tools"]), ["watch_game_screen"])
        # ...but no tool ran and no "tools" status reached the UI.
        self.assertEqual(analysis.calls, [])
        self.assertEqual([s for s in statuses if s.get("state") == "tools"], [])


class SchemaConversionTest(unittest.TestCase):
    """Contract 3: pure flat -> nested conversion."""

    def test_flat_schema_converts_to_nested(self):
        flat = {"type": "function", "name": "watch_game_screen",
                "description": "看画面", "parameters": {"type": "object", "properties": {}}}
        self.assertEqual(
            to_chat_completions_tools([flat]),
            [{"type": "function",
              "function": {"name": "watch_game_screen", "description": "看画面",
                           "parameters": {"type": "object", "properties": {}}}}],
        )

    def test_strict_is_preserved_extras_dropped(self):
        flat = {"type": "function", "name": "t", "parameters": {}, "strict": True, "junk": 1}
        converted = to_chat_completions_tools([flat])[0]
        self.assertEqual(converted["function"], {"name": "t", "parameters": {}, "strict": True})

    def test_nested_schema_passes_through_unchanged(self):
        nested = {"type": "function", "function": {"name": "t", "parameters": {}}}
        result = to_chat_completions_tools([nested])
        self.assertIs(result[0], nested)

    def test_empty_and_order(self):
        self.assertEqual(to_chat_completions_tools([]), [])
        flats = [{"name": "a"}, {"name": "b"}]
        self.assertEqual([t["function"]["name"] for t in to_chat_completions_tools(flats)],
                         ["a", "b"])


if __name__ == "__main__":
    unittest.main()

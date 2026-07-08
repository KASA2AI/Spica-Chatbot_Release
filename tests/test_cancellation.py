"""#1 ghost-producer cancellation -- acceptance pins for the three producer
side-effect checkpoints.

A turn carries a ``cancelled`` ``threading.Event`` (``TurnRequest.cancelled``).
The UI sets it via ``ChatWorker.cancel`` -- fired from the controller's
``_retire_chat_worker`` on user-cancel OR proactive/P5 preemption (both go through
the one ``stop_current``). The backend producer thread short-circuits at three
points so a RETIRED turn cannot ghost-execute tools, write ghost memory, or burn
LLM tokens after the consumer (ChatWorker) stopped reading its queue:

  ① tool execution    -- ``_run_tool_calls`` breaks BEFORE ``tools.run``, so a
                         cancelled turn never executes ``sing_song`` (whose
                         ``SongRequestEvent`` rides the ``companion_sink`` bridge,
                         bypassing the stream token -- it would otherwise start
                         singing on past the cancel). The ONLY audible ghost;
                         highest value.
  ② save_stream_memory -- skipped whole, so neither the synchronous recent append
                         nor the backgrounded long-term commit runs.
  ③ LLM delta loop     -- ③a skips the stream entirely if already cancelled, ③b
                         breaks mid-stream the moment cancel lands.

Deadline (``cancelled`` None/unset -> byte-identical) is pinned by the UNTOUCHED
golden tests ``test_chat_tool_round`` + ``test_proactive_turn``; every pin here
also carries a NON-cancelled control so the assertion is never vacuously true.
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
from spica.runtime.context import TurnContext, TurnRequest
from spica.runtime.observer import DefaultTurnObserver
from spica.runtime.services import AgentServices
from spica.runtime.tool_round import _run_tool_calls

RAW_ANSWER = json.dumps(
    {"answer": "好呀。", "emotion": "happy", "emotion_reason": "x"}, ensure_ascii=False
)


# ---- Pin ①: direct _run_tool_calls (the sing_song ghost killer) ------------ #

class _RecordingToolSet:
    """Records every ``tools.run``. A run of ``sing_song`` is precisely what would
    emit a ``SongRequestEvent`` on the companion_sink bridge, so 'never ran' ==
    'no ghost song'."""

    def __init__(self):
        self.ran = []

    def run(self, name, arguments):
        self.ran.append((name, arguments))
        return json.dumps({"ok": True, "data": {}}, ensure_ascii=False)


def _tool_ctx(cancelled):
    return TurnContext(TurnRequest(user_input="唱首歌", cancelled=cancelled))


class CheckpointOneToolExecutionTest(unittest.TestCase):
    def test_cancelled_turn_never_executes_sing_song(self):
        event = threading.Event()
        event.set()  # cancelled BEFORE the tool round
        ctx = _tool_ctx(event)
        tools = _RecordingToolSet()
        _run_tool_calls(
            ctx, DefaultTurnObserver(ctx.timing), tools, lambda *a: None,
            [{"name": "sing_song", "arguments": "{}"}],
        )
        self.assertEqual(tools.ran, [])  # no run -> no SongRequestEvent -> no ghost song

    def test_control_uncancelled_turn_does_execute(self):
        ctx = _tool_ctx(None)  # no cancel Event -> is_turn_cancelled False
        tools = _RecordingToolSet()
        _run_tool_calls(
            ctx, DefaultTurnObserver(ctx.timing), tools, lambda *a: None,
            [{"name": "sing_song", "arguments": "{}"}],
        )
        self.assertEqual([n for n, _ in tools.ran], ["sing_song"])  # harness really runs it

    def test_cancel_midlist_skips_remaining_tools(self):
        # Cancel lands DURING the first tool -> the loop breaks before the second.
        event = threading.Event()
        ctx = _tool_ctx(event)

        class _SetOnFirst:
            def __init__(self):
                self.ran = []

            def run(self, name, arguments):
                self.ran.append(name)
                event.set()  # cancel arrives while the first tool is executing
                return json.dumps({"ok": True, "data": {}}, ensure_ascii=False)

        tools = _SetOnFirst()
        _run_tool_calls(
            ctx, DefaultTurnObserver(ctx.timing), tools, lambda *a: None,
            [{"name": "inspect_screen", "arguments": "{}"},
             {"name": "sing_song", "arguments": "{}"}],
        )
        self.assertEqual(tools.ran, ["inspect_screen"])  # sing_song (2nd) skipped by ①


# ---- Pins ② / ③: the full producer via ChatEngine.stream_voice ------------- #

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


def _build_engine(chat_api, tmp):
    """A plain-chat engine (empty registry -> no tool probe) over a deepseek-shape
    client, mirroring the verify_watch_chain assembly the golden tests use."""
    client = SimpleNamespace(base_url="https://api.deepseek.com/v1", chat=chat_api)
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
    services.tool_registry = CapabilityRegistry()  # empty -> plain chat, no probe
    return ChatEngine(services, AppConfig())


class _OneChunkChatAPI:
    """Streams the whole answer in one chunk; records whether create() ran."""

    def __init__(self):
        self.completions = self
        self.created = 0

    def create(self, **kwargs):
        self.created += 1
        if kwargs.get("stream"):
            def chunks():
                yield SimpleNamespace(choices=[SimpleNamespace(
                    delta=SimpleNamespace(content=RAW_ANSWER))])
            return chunks()
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=RAW_ANSWER))], usage=None)


class CheckpointTwoMemoryTest(unittest.TestCase):
    def test_cancelled_turn_writes_no_memory(self):
        event = threading.Event()
        event.set()  # cancelled before the turn runs
        api = _OneChunkChatAPI()
        with tempfile.TemporaryDirectory() as tmp:
            engine = _build_engine(api, tmp)
            list(engine.stream_voice("你好", cancelled=event))
            recent = engine.services.recent_memory.get_recent(scoped_conversation_id("spica", "default"))
        self.assertEqual(recent, [])  # save_stream_memory skipped -> no ghost recent append
        # ② guards the WHOLE save_stream_memory call, so the backgrounded long-term
        # commit (jobs.submit inside it) is skipped by the same gate.
        self.assertEqual(api.created, 0)  # ③a too: the LLM stream was never even opened

    def test_control_uncancelled_turn_writes_memory(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = _build_engine(_OneChunkChatAPI(), tmp)
            list(engine.stream_voice("你好"))  # no cancel
            recent = engine.services.recent_memory.get_recent(scoped_conversation_id("spica", "default"))
        self.assertEqual(len(recent), 1)  # the harness really writes when not cancelled
        self.assertIn("你好", recent[0]["user_text"])


class _MultiDeltaChatAPI:
    """Streams ``pieces`` one delta at a time; sets ``cancel_event`` just before
    yielding the piece at ``cancel_before_index`` (so cancel lands mid-stream).
    Records each piece it actually reached the yield point for."""

    def __init__(self, pieces, cancel_event, cancel_before_index):
        self.completions = self
        self.pieces = pieces
        self.cancel_event = cancel_event
        self.cancel_before_index = cancel_before_index
        self.yielded = []

    def create(self, **kwargs):
        if kwargs.get("stream"):
            def chunks():
                for i, piece in enumerate(self.pieces):
                    if i == self.cancel_before_index:
                        self.cancel_event.set()
                    self.yielded.append(piece)
                    yield SimpleNamespace(choices=[SimpleNamespace(
                        delta=SimpleNamespace(content=piece))])
            return chunks()
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content="".join(self.pieces)))], usage=None)


class CheckpointThreeDeltaLoopTest(unittest.TestCase):
    def test_cancel_mid_stream_breaks_delta_loop(self):
        event = threading.Event()  # UNSET at start so ③a enters the stream
        pieces = ["a", "b", "c", "d", "e"]
        api = _MultiDeltaChatAPI(pieces, event, cancel_before_index=2)
        with tempfile.TemporaryDirectory() as tmp:
            engine = _build_engine(api, tmp)
            list(engine.stream_voice("你好", cancelled=event))
        # Consumer pulled a, b, then c (which set the flag) and broke -> d, e were
        # never produced. The generator is abandoned mid-stream (③b).
        self.assertEqual(api.yielded, ["a", "b", "c"])
        self.assertLess(len(api.yielded), len(pieces))

    def test_control_uncancelled_consumes_all_deltas(self):
        pieces = ["a", "b", "c", "d", "e"]
        api = _MultiDeltaChatAPI(pieces, threading.Event(), cancel_before_index=999)  # never fires
        with tempfile.TemporaryDirectory() as tmp:
            engine = _build_engine(api, tmp)
            list(engine.stream_voice("你好"))  # no cancel
        self.assertEqual(api.yielded, pieces)  # all deltas consumed when not cancelled


if __name__ == "__main__":
    unittest.main()

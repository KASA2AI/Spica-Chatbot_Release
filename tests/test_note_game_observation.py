"""note_game_observation (Phase 9 step 2) -- pinned on the REAL chains.

Assembly lifted from test_chat_tool_round (= verify_watch_chain scene B): real
CapabilityRegistry + real NoteGameObservationTool + real ChatEngine + real
GameMemorySqliteAdapter; fake LLM clients record every request. The write
closure in the chain tests is the REAL ``AppHost._record_game_observation``
(light host, HistoryBridgeTest precedent) -- no test-side beat construction.

Contracts:
1. companion-active stream turn -> note offered to the probe, the tool_call
   executes, and the CompanionBeat REALLY lands in game memory
   (type/source/session_id/scope fields + the 200-char clamp).
2. not playing -> NO_ACTIVE_COMPANION (tool raise + toolset error envelope).
3. write -> read-back closed loop (the linchpin): after a note turn, the NEXT
   companion turn's prompt carries [COMPANION_CONTEXT] with the observation --
   via the EXISTING stages active-branch injection, no new read path.
4. registry supply compat: note alongside watch -- both offered while playing,
   note filtered out by its ``available`` predicate when not, watch untouched.
"""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from agent_tools.function_tools import TOOL_SCHEMAS, default_tool_functions, tool_error
from agent_tools.function_tools.screen.schema import ScreenToolError
from agent_tools.tts.schemas import TTSRequest, TTSResult
from memory.recent import RecentMemory
from memory.store import SQLiteMemoryStore
from spica.adapters.game_memory.sqlite import GameMemorySqliteAdapter
from spica.adapters.tools.note_game_observation import NoteGameObservationTool
from spica.config.schema import AppConfig
from spica.core.chat_engine import ChatEngine
from spica.galgame.models import game_conversation_id
from spica.galgame.session import GalgameState
from spica.host.app_host import AppHost
from spica.plugins.registry import CapabilityRegistry
from spica.runtime.context import GameContextRequest, GameTurnBinding
from spica.runtime.services import AgentServices
from spica.runtime.tools import RegistryToolSet

RAW_ANSWER = json.dumps(
    {"answer": "记下啦。", "emotion": "happy", "emotion_reason": "x"},
    ensure_ascii=False,
)
NOTE_REQUEST = "把这个记下来"
FOLLOWUP_QUESTION = "她是谁来着"
OBSERVATION = "穿校服的短发女孩，名字叫小满。"

GAME_BINDING = GameTurnBinding(
    conversation_id=game_conversation_id("limelight"),
    game_context_request=GameContextRequest(mode="active", game_id="limelight"),
)


# ---- fakes (test_chat_tool_round shapes) ----------------------------------- #

class _ChatCompletionsAPI:
    """Records every request; issues the note tool_call ONCE (first probe with
    tools and no [TOOL_RESULTS]), then answers with content like a real model --
    so a second turn's probe declines and the turn stays a plain answer."""

    def __init__(self, calls, observation=OBSERVATION):
        self._calls = calls
        self._observation = observation
        self._issued = False
        self.completions = self

    def create(self, **kwargs):
        self._calls.append(("chat.completions.create", kwargs))
        is_followup = "[TOOL_RESULTS]" in kwargs["messages"][0]["content"]
        want_tool = bool(kwargs.get("tools")) and not self._issued and not is_followup
        args = json.dumps({"observation": self._observation}, ensure_ascii=False)
        if kwargs.get("stream"):
            if want_tool:
                self._issued = True
                def chunks():  # streaming probe -> note tool_call delta
                    yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(
                        content=None,
                        tool_calls=[SimpleNamespace(index=0, id="call_1", type="function",
                            function=SimpleNamespace(name="note_game_observation", arguments=args))]))])
                return chunks()

            def chunks():
                yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=RAW_ANSWER))])
            return chunks()
        if want_tool:  # NON-streaming (sync chain)
            self._issued = True
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
                content="",
                tool_calls=[SimpleNamespace(id="call_1", type="function",
                    function=SimpleNamespace(name="note_game_observation", arguments=args))],
            ))], usage=None)
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=RAW_ANSWER))], usage=None)


def _deepseek_client(calls, observation=OBSERVATION):
    return SimpleNamespace(base_url="https://api.deepseek.com/v1",
                           chat=_ChatCompletionsAPI(calls, observation=observation))


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


def _light_host(adapter, binding=GAME_BINDING):
    """A light AppHost (HistoryBridgeTest precedent) so the chain tests run the
    REAL _record_game_observation closure against the REAL adapter."""
    host = AppHost()
    host.config = AppConfig()
    host.services = SimpleNamespace(game_memory_adapter=adapter)
    host._companion_controller = SimpleNamespace(
        current_game_context=lambda: binding,
        current_watch_target=lambda: None,
    )
    return host


def _build_engine(client, tmp, *, binding=GAME_BINDING):
    adapter = GameMemorySqliteAdapter(Path(tmp) / "g.sqlite3")
    host = _light_host(adapter, binding=binding)
    registry = CapabilityRegistry()
    note = NoteGameObservationTool(host._companion_game_binding, host._record_game_observation)
    registry.register_tool(note.schema(), note.run,
                           available=lambda: host._companion_game_binding() is not None,
                           intent_gated=False)
    services = AgentServices(
        llm_client=client, tts_adapter=_FakeTTS(), visual_tool=_FakeVisual(),
        memory_store=SQLiteMemoryStore(Path(tmp) / "m.sqlite3"),
        recent_memory=RecentMemory(max_turns=3),
        game_memory_adapter=adapter,
        config={"model": "test-model", "character_profile": "p", "recent_context_limit": 3,
                "long_term_memory_limit": 5, "max_tool_rounds": 3, "character_id": "spica",
                "interlocutor_name": "麦"},
        logger=lambda *a, **k: None,
        tool_functions=default_tool_functions(), tool_schemas=TOOL_SCHEMAS,
    )
    services.tool_registry = registry
    engine = ChatEngine(services, AppConfig())
    engine.set_game_binding_provider(lambda: binding)
    return engine, adapter


def _stream(engine, question):
    events = list(engine.stream_voice(question))
    done = next((e for e in events if e.get("event") == "done"), None)
    answer = (done or {}).get("data", {}).get("answer", "")
    statuses = [e.get("data", {}) for e in events if e.get("event") == "status"]
    return answer, statuses


def _nested_names(tools):
    return [(t.get("function") or {}).get("name") for t in tools]


class NoteWriteChainTest(unittest.TestCase):
    """Contract 1: the real streaming chain writes the beat through the host closure."""

    def test_note_turn_persists_beat_with_approved_fields(self):
        calls = []
        with tempfile.TemporaryDirectory() as tmp:
            engine, adapter = _build_engine(_deepseek_client(calls), tmp)
            answer, statuses = _stream(engine, NOTE_REQUEST)

            self.assertEqual(answer, "记下啦。")
            # Probe carried the note tool; the followup carried the result.
            probe = calls[0][1]
            self.assertEqual(_nested_names(probe["tools"]), ["note_game_observation"])
            followup = calls[1][1]
            self.assertIn("[TOOL_RESULTS]", followup["messages"][0]["content"])
            self.assertIn("note_game_observation", followup["messages"][0]["content"])
            # Per-tool status fired (generic fallback text in the UI).
            self.assertIn("tool:note_game_observation",
                          [s.get("message") for s in statuses])
            # The beat REALLY landed, with the approved field decisions.
            beats = adapter.companion_beats("limelight", "麦", "spica", limit=10)
            self.assertEqual(len(beats), 1)
            beat = beats[0]
            self.assertEqual(beat.content, OBSERVATION)
            self.assertEqual(beat.type, "shared_observation")
            self.assertEqual(beat.source, "spica")
            self.assertIsNone(beat.session_id)
            self.assertEqual(beat.game_id, "limelight")
            self.assertEqual(beat.playthrough_id, "default")
            self.assertEqual(beat.scope, {"character_id": "spica", "user_id": "麦",
                                          "game_id": "limelight"})

    def test_observation_clamped_to_200_chars(self):
        calls = []
        dump = "剧" * 300
        with tempfile.TemporaryDirectory() as tmp:
            engine, adapter = _build_engine(_deepseek_client(calls, observation=dump), tmp)
            _stream(engine, NOTE_REQUEST)
            beats = adapter.companion_beats("limelight", "麦", "spica", limit=10)
        self.assertEqual(len(beats), 1)
        self.assertEqual(len(beats[0].content), 200)
        self.assertEqual(beats[0].content, dump[:200])


class NoActiveCompanionTest(unittest.TestCase):
    """Contract 2: not playing -> NO_ACTIVE_COMPANION, nothing written."""

    def test_tool_raises_and_record_never_called(self):
        recorded = []
        tool = NoteGameObservationTool(lambda: None, recorded.append)
        with self.assertRaises(ScreenToolError) as caught:
            tool.run(observation="x")
        self.assertEqual(caught.exception.code, "NO_ACTIVE_COMPANION")
        self.assertEqual(recorded, [])

    def test_toolset_run_returns_error_envelope(self):
        registry = CapabilityRegistry()
        tool = NoteGameObservationTool(lambda: None, lambda content: "never")
        registry.register_tool(tool.schema(), tool.run, available=lambda: True,
                               intent_gated=False)
        result = json.loads(RegistryToolSet(registry).run(
            "note_game_observation", json.dumps({"observation": "x"})))
        self.assertFalse(result.get("ok"))
        self.assertEqual(result["error"]["code"], "NO_ACTIVE_COMPANION")

    def test_empty_observation_rejected_before_write(self):
        recorded = []
        tool = NoteGameObservationTool(lambda: GAME_BINDING, recorded.append)
        with self.assertRaises(ScreenToolError) as caught:
            tool.run(observation="   ")
        self.assertEqual(caught.exception.code, "NOTE_OBSERVATION_EMPTY")
        self.assertEqual(recorded, [])


class NoteReadbackLoopTest(unittest.TestCase):
    """Contract 3 (the linchpin): the stored observation reaches the NEXT
    companion turn's prompt through the EXISTING [COMPANION_CONTEXT] injection."""

    def test_next_companion_turn_prompt_carries_observation(self):
        calls = []
        with tempfile.TemporaryDirectory() as tmp:
            engine, adapter = _build_engine(_deepseek_client(calls), tmp)

            # Turn 1: note turn. Its OWN prompt has no companion section yet.
            _stream(engine, NOTE_REQUEST)
            turn1_probe_text = calls[0][1]["messages"][0]["content"]
            self.assertNotIn("[COMPANION_CONTEXT]", turn1_probe_text)
            self.assertEqual(len(adapter.companion_beats("limelight", "麦", "spica")), 1)

            # Turn 2: plain companion question (fake declines: tool already issued).
            answer, _ = _stream(engine, FOLLOWUP_QUESTION)
            self.assertEqual(answer, "记下啦。")

        turn2_first_call = calls[2][1]
        prompt_text = turn2_first_call["messages"][0]["content"]
        self.assertIn("[COMPANION_CONTEXT]", prompt_text)
        self.assertIn(OBSERVATION, prompt_text)
        self.assertIn("shared_observation", prompt_text)


class RegistrySupplyCompatTest(unittest.TestCase):
    """Contract 4: note rides the same supply rules and does not disturb watch."""

    def test_note_alongside_watch_and_state_filtering(self):
        playing = {"on": True}
        registry = CapabilityRegistry()
        watch_schema = {"type": "function", "name": "watch_game_screen",
                        "parameters": {"type": "object", "properties": {}}}
        registry.register_tool(watch_schema, lambda **k: {"ok": True},
                               available=lambda: True, intent_gated=False)
        note = NoteGameObservationTool(lambda: GAME_BINDING, lambda content: "b1")
        registry.register_tool(note.schema(), note.run,
                               available=lambda: playing["on"], intent_gated=False)

        toolset = RegistryToolSet(registry)
        offered = [s.get("name") for s in toolset.schemas_for_user_text(NOTE_REQUEST)]
        self.assertEqual(offered, ["watch_game_screen", "note_game_observation"])

        playing["on"] = False  # companion stops -> note filtered, watch untouched
        offered = [s.get("name") for s in toolset.schemas_for_user_text(NOTE_REQUEST)]
        self.assertEqual(offered, ["watch_game_screen"])


class NoteHostRegistrationTest(unittest.TestCase):
    """The PRODUCTION wiring in AppHost.__init__: note registered next to watch,
    available only while a companion play publishes its binding."""

    def test_host_registry_offers_note_only_while_playing(self):
        host = AppHost()
        names = [s.get("name") for s in host.registry.tool_schemas()]
        self.assertNotIn("note_game_observation", names)  # no controller yet
        self.assertNotIn("watch_game_screen", names)

        host._companion_controller = SimpleNamespace(
            current_game_context=lambda: GAME_BINDING,
            current_watch_target=lambda: ("limelight", "0x1"),
            # privacy gate (review #1): the watch provider reads session.state
            session=SimpleNamespace(state=GalgameState.PLAYING),
        )
        host.services = SimpleNamespace(
            window_locator_adapter=object(), screen_capture_adapter=object(),
        )
        names = [s.get("name") for s in host.registry.tool_schemas()]
        self.assertIn("note_game_observation", names)
        self.assertIn("watch_game_screen", names)
        self.assertFalse(host.registry.tool_intent_gated("note_game_observation"))

    def test_host_closure_guards_stop_race(self):
        host = AppHost()
        host.config = AppConfig()
        host._companion_controller = None  # stopped between tool check and write
        with self.assertRaises(ScreenToolError) as caught:
            host._record_game_observation("x")
        self.assertEqual(caught.exception.code, "NO_ACTIVE_COMPANION")


if __name__ == "__main__":
    unittest.main()

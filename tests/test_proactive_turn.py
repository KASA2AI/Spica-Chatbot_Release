"""P3: proactive turn initiation -- pinned on the REAL chain + the policy unit.

Contracts:
1. MODE-AGNOSTIC: the request carries no domain field (name check), and a fake
   "video" domain drives the arbiter -> start_turn chain exactly like song does.
2. ONE dialogue path: ``stream_system_turn`` drives the real orchestrator --
   the framed directive lands in the prompt, ``interaction_mode="system"``
   reaches recent memory, and the request body carries NO tools even though the
   directive contains 唱 (the self-excitation hard-off; supply wordlist would
   otherwise offer sing_song right back to her).
3. Busy arbitration v1 (drop_if_busy) + the full-duplex gate seam.
4. First use case: finish_song_playback submits a song-named directive.
"""

import dataclasses
import json
import tempfile
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
from spica.core.proactive import (
    NullInputGate,
    ProactiveTurnArbiter,
    ProactiveTurnRequest,
    compose_system_directive_message,
)
from spica.host.app_host import AppHost
from spica.runtime.services import AgentServices

RAW_ANSWER = json.dumps(
    {"answer": "唱完啦，怎么样？", "emotion": "happy", "emotion_reason": "x"},
    ensure_ascii=False,
)


class _ChatCompletionsAPI:
    def __init__(self, calls):
        self._calls = calls
        self.completions = self

    def create(self, **kwargs):
        self._calls.append(("chat.completions.create", kwargs))
        if kwargs.get("stream"):
            def chunks():
                yield SimpleNamespace(choices=[SimpleNamespace(
                    delta=SimpleNamespace(content=RAW_ANSWER))])
            return chunks()
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=RAW_ANSWER))], usage=None)


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


def _build_engine(client, tmp):
    """Real AppHost registry (sing_song registered!) so the no-tools assertion
    is against the REAL supply -- the directive contains 唱 and would hit the
    wordlist without the system gate."""
    host = AppHost()
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
    services.tool_registry = host.registry
    return ChatEngine(services, AppConfig())


class ModeAgnosticTest(unittest.TestCase):
    """Contract 1: zero domain assumptions in the initiator."""

    def test_request_fields_carry_no_domain_names(self):
        field_names = {f.name for f in dataclasses.fields(ProactiveTurnRequest)}
        self.assertEqual(
            field_names,
            {"directive", "source", "conversation_id", "policy", "ttl_seconds"},
        )
        for name in field_names:
            for domain in ("game", "song", "video", "galgame"):
                self.assertNotIn(domain, name)

    def test_fake_video_domain_drives_the_same_chain(self):
        started = []
        arbiter = ProactiveTurnArbiter(is_busy=lambda: False, start_turn=started.append)
        request = ProactiveTurnRequest(
            source="video", directive="视频播到了名场面。", conversation_id="video::bv1")
        self.assertTrue(arbiter.try_speak(request))
        self.assertEqual(started, [request])  # untouched, no song assumptions


class SystemTurnSinglePathTest(unittest.TestCase):
    """Contract 2: the system turn rides run_turn -- and gets NO tools."""

    def test_system_turn_prompt_framing_and_tool_hard_off(self):
        calls = []
        with tempfile.TemporaryDirectory() as tmp:
            engine = _build_engine(
                SimpleNamespace(base_url="https://api.deepseek.com/v1",
                                chat=_ChatCompletionsAPI(calls)), tmp)
            events = list(engine.stream_system_turn(
                "你刚唱完了《稻香》（周杰伦）。", source="song"))
            done = next(e for e in events if e.get("event") == "done")
            recent = engine.services.recent_memory.get_recent(scoped_conversation_id("spica", "default"))

        self.assertEqual(done["data"]["answer"], "唱完啦，怎么样？")
        # Exactly ONE streamed call -- NO probe, NO tools field, although the
        # directive contains 唱 (sing_song is registered and wordlist-gated).
        self.assertEqual(len(calls), 1)
        method, kwargs = calls[0]
        self.assertEqual(set(kwargs), {"model", "messages", "stream"})
        prompt = kwargs["messages"][0]["content"]
        self.assertIn("【系统事件，不是麦说的话】", prompt)
        self.assertIn("稻香", prompt)
        # interaction_mode landed in recent memory; the stored user side is the
        # framed directive (self-identifying, never impersonates the user).
        self.assertEqual(len(recent), 1)
        self.assertEqual(recent[0]["interaction_mode"], "system")
        self.assertIn("【系统事件", recent[0]["user_text"])


class ArbiterPolicyTest(unittest.TestCase):
    """Contract 3: drop_if_busy + the full-duplex gate seam."""

    def test_busy_drops_with_debug_log(self):
        started = []
        arbiter = ProactiveTurnArbiter(is_busy=lambda: True, start_turn=started.append)
        with self.assertLogs("spica.core.proactive", level="DEBUG") as logs:
            spoke = arbiter.try_speak(ProactiveTurnRequest(source="song", directive="x"))
        self.assertFalse(spoke)
        self.assertEqual(started, [])
        self.assertTrue(any("dropped (busy)" in line for line in logs.output))

    def test_idle_starts_and_gate_hooks_fire(self):
        events = []

        class _Gate:
            def before_system_speech(self):
                events.append("before")

            def after_system_speech(self):
                events.append("after")

        started = []
        arbiter = ProactiveTurnArbiter(
            is_busy=lambda: False, start_turn=started.append, input_gate=_Gate())
        request = ProactiveTurnRequest(source="song", directive="x")
        self.assertTrue(arbiter.try_speak(request))
        self.assertEqual(started, [request])
        self.assertEqual(events, ["before"])  # before fires exactly once, pre-start
        arbiter.system_speech_finished()
        self.assertEqual(events, ["before", "after"])

    def test_null_gate_is_default_and_silent(self):
        arbiter = ProactiveTurnArbiter(is_busy=lambda: False, start_turn=lambda r: None)
        self.assertIsInstance(arbiter._input_gate, NullInputGate)
        arbiter.system_speech_finished()  # no-op, must not raise


class ComposeFramingTest(unittest.TestCase):
    def test_framing_marks_system_and_keeps_directive(self):
        message = compose_system_directive_message("你刚唱完了《稻香》。")
        self.assertTrue(message.startswith("【系统事件，不是麦说的话】你刚唱完了《稻香》。"))
        self.assertIn("Spica", message)


if __name__ == "__main__":
    unittest.main()

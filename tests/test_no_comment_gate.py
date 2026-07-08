"""P5 step 3: the NO_COMMENT gate on the REAL streaming chain (D-P5-5).

Four explicit interceptions for a system turn whose whole answer is the
sentinel: no units (=> no TTS), no display payload (canonical sentinel on done,
units_count 0 -- the UI suppression keys on it), no recent memory, and a silent
completion (done still fires). Pinned to be INDEPENDENT of play_unit_min_chars
(the min_chars=1 pin), and mode-gated: a plain CHAT turn answering the literal
sentinel is NOT swallowed.

Plus the step-3 full chain with a fake LLM: trigger -> ReactionEngine ->
host closures -> arbiter -> stream_system_turn -> notify -> CompanionBeat in
the REAL galgame DB / budget refund, both branches (spoken / swallowed).
"""

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
from spica.config.schema import AppConfig, StreamConfig
from spica.core.chat_engine import ChatEngine
from spica.core.companion_events import (
    GalgameStableLineCommittedEvent,
    GalgameStatusChangedEvent,
)
from spica.core.proactive import (
    NO_COMMENT_SENTINEL,
    ProactiveTurnArbiter,
    is_no_comment_answer,
    may_become_no_comment,
)
from spica.galgame.reaction import (
    ReactionEngine,
    ReactionTurnFinished,
    ScoreResult,
)
from spica.galgame.session import GalgameState
from spica.host.app_host import AppHost
from spica.runtime.context import GameContextRequest, GameTurnBinding
from spica.runtime.services import AgentServices


def _raw(answer):
    return json.dumps(
        {"answer": answer, "emotion": "happy", "emotion_reason": "x"}, ensure_ascii=False
    )


class _ChatCompletionsAPI:
    def __init__(self, calls, raw):
        self._calls = calls
        self._raw = raw
        self.completions = self

    def create(self, **kwargs):
        self._calls.append(("chat.completions.create", kwargs))
        if kwargs.get("stream"):
            def chunks():
                yield SimpleNamespace(choices=[SimpleNamespace(
                    delta=SimpleNamespace(content=self._raw))])
            return chunks()
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=self._raw))], usage=None)


class _CountingTTS:
    name = "fake_tts"

    def __init__(self):
        self.calls = 0

    def synthesize(self, request):
        assert isinstance(request, TTSRequest)
        self.calls += 1
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


def _build_engine(answer, tmp, *, min_chars=None):
    calls = []
    client = SimpleNamespace(
        base_url="https://api.deepseek.com/v1", chat=_ChatCompletionsAPI(calls, _raw(answer))
    )
    tts = _CountingTTS()
    host = AppHost()
    services = AgentServices(
        llm_client=client, tts_adapter=tts, visual_tool=_FakeVisual(),
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
    config = AppConfig()
    if min_chars is not None:
        config = AppConfig(stream=StreamConfig(play_unit_min_chars=min_chars))
    return ChatEngine(services, config), calls, tts


def _events_of(engine, gen):
    events = list(gen)
    done = next(e for e in events if e.get("event") == "done")
    units = [e for e in events if "unit" in str(e.get("event", ""))]
    return events, done, units


class SentinelHelpersTest(unittest.TestCase):
    def test_is_no_comment_tolerates_noise(self):
        for text in ("NO_COMMENT", "no_comment", " NO_COMMENT。", '"NO_COMMENT"', "NO_COMMENT！"):
            self.assertTrue(is_no_comment_answer(text), text)
        for text in ("", "好耶", "NO_COMMENTS PLEASE", "NO COMMENT but actually..."):
            self.assertFalse(is_no_comment_answer(text), text)

    def test_prefix_compatibility(self):
        for partial in ("", "N", "NO_COM", "NO_COMMENT", "NO_COMMENT。"):
            self.assertTrue(may_become_no_comment(partial), partial)
        for partial in ("好", "NO_COMMENTS", "N0"):
            self.assertFalse(may_become_no_comment(partial), partial)


class SwallowedSystemTurnTest(unittest.TestCase):
    def _assert_swallowed(self, answer_text, *, min_chars=None):
        with tempfile.TemporaryDirectory() as tmp:
            engine, _, tts = _build_engine(answer_text, tmp, min_chars=min_chars)
            _, done, units = _events_of(
                engine, engine.stream_system_turn("陪玩剧情片段。", source="galgame")
            )
            recent = engine.services.recent_memory.get_recent(scoped_conversation_id("spica", "default"))
        self.assertEqual(units, [])                       # 不TTS的前提:零unit事件
        self.assertEqual(tts.calls, 0)                    # 不TTS(直接证据)
        self.assertEqual(done["data"]["answer"], NO_COMMENT_SENTINEL)  # done带canonical sentinel
        self.assertEqual(done["data"]["units_count"], 0)  # UI不上屏的判据
        self.assertEqual(recent, [])                      # 不进recent memory

    def test_sentinel_system_turn_is_swallowed(self):
        self._assert_swallowed(NO_COMMENT_SENTINEL)

    def test_sentinel_with_trailing_punct_is_swallowed_canonically(self):
        self._assert_swallowed("NO_COMMENT。")

    def test_min_chars_one_still_zero_leak(self):
        # D-P5-5: the swallow must NOT depend on play_unit_min_chars tuning --
        # at min_chars=1 a single sentinel char would otherwise become a unit.
        self._assert_swallowed(NO_COMMENT_SENTINEL, min_chars=1)

    def test_normal_system_turn_still_streams_and_remembers(self):
        # the hold releases on divergence: a real answer behaves like before
        with tempfile.TemporaryDirectory() as tmp:
            engine, _, tts = _build_engine("唱完啦，怎么样？", tmp)
            _, done, _ = _events_of(
                engine, engine.stream_system_turn("你刚唱完了歌。", source="song")
            )
            recent = engine.services.recent_memory.get_recent(scoped_conversation_id("spica", "default"))
        self.assertEqual(done["data"]["answer"], "唱完啦，怎么样？")
        self.assertGreater(done["data"]["units_count"], 0)
        self.assertGreater(tts.calls, 0)
        self.assertEqual(len(recent), 1)

    def test_chat_turn_answering_the_sentinel_is_not_swallowed(self):
        # mode-gated: a PLAIN turn whose answer happens to be the literal
        # sentinel keeps today's behaviour byte for byte (gate is system-only).
        with tempfile.TemporaryDirectory() as tmp:
            engine, _, tts = _build_engine(NO_COMMENT_SENTINEL, tmp)
            _, done, _ = _events_of(engine, engine.stream_voice("随便聊聊"))
            recent = engine.services.recent_memory.get_recent(scoped_conversation_id("spica", "default"))
        self.assertEqual(done["data"]["answer"], NO_COMMENT_SENTINEL)
        self.assertGreater(done["data"]["units_count"], 0)  # fallback unit spoken
        self.assertGreater(tts.calls, 0)
        self.assertEqual(len(recent), 1)


class FullChainTest(unittest.TestCase):
    """假 LLM 全链: ReactionEngine -> host closures -> arbiter ->
    stream_system_turn -> notify -> real-DB CompanionBeat / refund."""

    def _wire(self, tmp, answer_text):
        chat_engine, calls, tts = _build_engine(answer_text, tmp)
        host = AppHost()
        host.config = AppConfig()
        adapter = GameMemorySqliteAdapter(Path(tmp) / "beats.sqlite3")
        host.services = SimpleNamespace(game_memory_adapter=adapter)
        binding = GameTurnBinding(
            conversation_id="galgame::limelight::default",
            game_context_request=GameContextRequest(mode="active", game_id="limelight"),
        )
        host._companion_controller = SimpleNamespace(current_game_context=lambda: binding)
        outcome = {}

        def start_turn(request):
            outcome["request"] = request
            _, done, _ = _events_of(
                chat_engine,
                chat_engine.stream_system_turn(
                    request.directive, conversation_id=request.conversation_id,
                    source=request.source,
                ),
            )
            outcome["answer"] = done["data"]["answer"]

        arbiter = ProactiveTurnArbiter(is_busy=lambda: False, start_turn=start_turn)
        host.attach_reaction_arbiter(arbiter.try_speak)
        engine = ReactionEngine(
            speak=host._reaction_speak,
            scorer=lambda beat: ScoreResult(10, ("test",)),
            beat_writer=host._write_reaction_beat,
            recent_for_dedupe=host._recent_reaction_beats,
        )
        host.reaction_engine = engine
        return host, engine, adapter, outcome, calls

    def _feed_hot_beat(self, engine, base_t, lines):
        for i, (speaker, text) in enumerate(lines):
            engine.handle_event(
                GalgameStableLineCommittedEvent(line_id=f"f{base_t}-{i}", speaker=speaker, text=text),
                now=base_t + i,
            )

    def test_spoken_branch_records_the_beat(self):
        with tempfile.TemporaryDirectory() as tmp:
            host, engine, adapter, outcome, calls = self._wire(tmp, "这展开也太突然了吧！")
            engine.handle_event(
                GalgameStatusChangedEvent(state=GalgameState.PLAYING.value), now=0.0
            )
            self._feed_hot_beat(engine, 0, [
                ("月岛", "其实我一直骗着你。"), ("雪鹰", "诶。"), ("月岛", "对不起！")])
            self.assertEqual(engine.decisions[-1].kind, "spoke")
            # the directive reached the REAL prompt: excerpt + constraints + escape
            prompt = calls[0][1]["messages"][0]["content"]
            self.assertIn("【系统事件，不是麦说的话】", prompt)
            self.assertIn("月岛：其实我一直骗着你。", prompt)
            self.assertIn("不超过40个字", prompt)
            self.assertIn(NO_COMMENT_SENTINEL, prompt)
            self.assertEqual(outcome["request"].conversation_id, "galgame::limelight::default")
            # UI report -> beat lands in the REAL DB with full meta
            engine.handle_event(ReactionTurnFinished(outcome["answer"], silent=False), now=3.0)
            beats = adapter.recent_reaction_beats_for_dedupe("limelight", "麦", "spica")
            self.assertEqual(len(beats), 1)
            self.assertEqual(beats[0].content, "这展开也太突然了吧！")
            self.assertEqual(beats[0].type, "reaction")
            self.assertEqual(beats[0].source, "spica")
            meta = beats[0].meta
            self.assertEqual(meta["silent"], False)
            self.assertEqual(meta["reason"], "spoke")
            self.assertEqual(meta["score"], 10)
            self.assertEqual(len(meta["source_line_ids"]), 3)
            self.assertTrue(meta["dedupe_hash"])
            self.assertIn("骗着你", meta["trigger_text"])
            # spoken (non-silent) beats ARE prompt-visible
            visible = adapter.recent_companion_beats_for_prompt("limelight", "麦", "spica")
            self.assertEqual([b.content for b in visible], ["这展开也太突然了吧！"])

    def test_swallowed_branch_refunds_and_records_silent_beat(self):
        with tempfile.TemporaryDirectory() as tmp:
            host, engine, adapter, outcome, _ = self._wire(tmp, NO_COMMENT_SENTINEL)
            engine.handle_event(
                GalgameStatusChangedEvent(state=GalgameState.PLAYING.value), now=0.0
            )
            self._feed_hot_beat(engine, 0, [
                ("月岛", "其实我一直骗着你。"), ("雪鹰", "诶。"), ("月岛", "对不起！")])
            self.assertEqual(engine.decisions[-1].kind, "spoke")
            self.assertEqual(outcome["answer"], NO_COMMENT_SENTINEL)  # orchestrator swallowed
            engine.handle_event(
                ReactionTurnFinished(outcome["answer"], silent=True), now=4.0
            )
            self.assertEqual(engine.decisions[-1].kind, "silent_refund")
            # silent beat persisted (dedupe history) but NOT prompt-visible
            dedupe = adapter.recent_reaction_beats_for_dedupe("limelight", "麦", "spica")
            self.assertEqual(len(dedupe), 1)
            self.assertEqual(dedupe[0].content, "")
            self.assertEqual(dedupe[0].meta["reason"], "no_comment")
            self.assertEqual(
                adapter.recent_companion_beats_for_prompt("limelight", "麦", "spica"), []
            )
            # refund proof: a DISSIMILAR hot beat right after (well inside the
            # normal 90s cooldown of the refunded stamp) speaks again
            self._feed_hot_beat(engine, 10, [
                ("莉莉子", "今天的Live要开始了哦。"), ("雪鹰", "紧张起来了。"), ("莉莉子", "上台吧！")])
            self.assertEqual(engine.decisions[-1].kind, "spoke")


if __name__ == "__main__":
    unittest.main()

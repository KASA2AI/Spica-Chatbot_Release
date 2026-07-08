"""B2 (P2): sing_song -- the first "act" tool, pinned on the REAL chains.

Assembly mirrors test_tool_chain_rounds, but the registry is the REAL AppHost
registry (its __init__ registers sing_song wired to the REAL _request_song
closure); the netease search seam (host._song_search) takes a fake so no test
touches the network. Contracts:

1. wordlist SUPPLY pre-filter: song-ish text offers sing_song to the probe;
   plain chat carries NO tools (zero probe cost for normal turns -- the B1
   lesson as supply, not verdict).
2. fire-and-acknowledge: the tool resolves the song, emits SongRequestEvent
   through the host sink, returns the tiny started envelope; the followup
   streams her acknowledgment.
3. SONG_NOT_FOUND: search failure -> ToolError envelope in the followup, no
   event emitted, the turn still answers.
4. effect metadata (P2 risk tiers): sing_song=act, note=write, watch/inspect=read.
"""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from agent_tools.function_tools import TOOL_SCHEMAS, default_tool_functions
from agent_tools.function_tools.screen.schema import ScreenToolError
from agent_tools.tts.schemas import TTSRequest, TTSResult
from memory.recent import RecentMemory
from memory.store import SQLiteMemoryStore
from spica.adapters.game_memory.sqlite import GameMemorySqliteAdapter
from spica.adapters.tools.sing_song import SingSongTool
from spica.config.schema import AppConfig
from spica.core.chat_engine import ChatEngine
from spica.host.app_host import AppHost
from spica.runtime.services import AgentServices

RAW_ANSWER = json.dumps(
    {"answer": "找到了《稻香》，我去清嗓～", "emotion": "happy", "emotion_reason": "x"},
    ensure_ascii=False,
)
SONG_QUESTION = "给我唱一首稻香"
PLAIN_QUESTION = "今天过得怎么样"


class _ChatCompletionsAPI:
    def __init__(self, calls, issue_tool_call=True):
        self._calls = calls
        self._issue = issue_tool_call
        self.completions = self

    def create(self, **kwargs):
        self._calls.append(("chat.completions.create", kwargs))
        is_followup = "[TOOL_RESULTS]" in kwargs["messages"][0]["content"]
        want_tool = bool(kwargs.get("tools")) and self._issue and not is_followup
        args = json.dumps({"query": "稻香"}, ensure_ascii=False)
        if kwargs.get("stream"):
            if want_tool:
                def chunks():  # streaming probe -> sing_song tool_call delta
                    yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(
                        content=None,
                        tool_calls=[SimpleNamespace(index=0, id="call_1", type="function",
                            function=SimpleNamespace(name="sing_song", arguments=args))]))])
                return chunks()

            def chunks():
                yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=RAW_ANSWER))])
            return chunks()
        if want_tool:  # NON-streaming (sync chain)
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
                content="",
                tool_calls=[SimpleNamespace(id="call_1", type="function",
                    function=SimpleNamespace(name="sing_song", arguments=args))],
            ))], usage=None)
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=RAW_ANSWER))], usage=None)


def _deepseek_client(calls, issue_tool_call=True):
    return SimpleNamespace(base_url="https://api.deepseek.com/v1",
                           chat=_ChatCompletionsAPI(calls, issue_tool_call))


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


def _fake_search(request, limit=20):
    # P0b 2b: the closure now passes limit from the resolved song config.
    if "没有这首" in request.search_keyword():
        raise RuntimeError("netease: no result")
    return SimpleNamespace(title="稻香", artists=["周杰伦"], artist_text="周杰伦")


def _build_engine(client, tmp):
    """REAL AppHost registry + REAL _request_song closure; fake search + sink."""
    host = AppHost()
    host._song_search = _fake_search
    events = []
    host.attach_companion_sink(events.append)
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
    engine = ChatEngine(services, AppConfig())
    return engine, events, host


def _stream(engine, question):
    events = list(engine.stream_voice(question))
    done = next((e for e in events if e.get("event") == "done"), None)
    answer = (done or {}).get("data", {}).get("answer", "")
    statuses = [e.get("data", {}) for e in events if e.get("event") == "status"]
    return answer, statuses


def _nested_names(tools):
    return [(t.get("function") or {}).get("name") for t in tools]


class SingSongChainTest(unittest.TestCase):
    """Contract 2: fire-and-acknowledge through the real streaming chain."""

    def test_song_request_emits_event_and_acknowledges(self):
        calls = []
        with tempfile.TemporaryDirectory() as tmp:
            engine, events, _host = _build_engine(_deepseek_client(calls), tmp)
            answer, statuses = _stream(engine, SONG_QUESTION)

        self.assertEqual(answer, "找到了《稻香》，我去清嗓～")
        # Supply: the probe carried sing_song (wordlist pre-filter hit on 唱/听).
        probe = calls[0][1]
        self.assertIn("sing_song", _nested_names(probe["tools"]))
        # The host closure REALLY emitted the song request for the UI.
        song_events = [e for e in events if getattr(e, "kind", "") == "song_request"]
        self.assertEqual(len(song_events), 1)
        self.assertEqual(song_events[0].query, "稻香")
        self.assertEqual(song_events[0].title, "稻香")
        self.assertEqual(song_events[0].artist, "周杰伦")
        # fire-and-acknowledge envelope reached the followup; final answer streams.
        followup = calls[1][1]
        self.assertTrue(followup.get("stream"))
        followup_text = followup["messages"][0]["content"]
        self.assertIn("[TOOL_RESULTS]", followup_text)
        self.assertIn('\\"started\\": true', followup_text)
        self.assertIn("稻香", followup_text)
        self.assertIn("tool:sing_song", [s.get("message") for s in statuses])

    def test_song_not_found_returns_error_envelope_no_event(self):
        calls = []
        with tempfile.TemporaryDirectory() as tmp:
            engine, events, host = _build_engine(_deepseek_client(calls), tmp)

            def _failing_probe(**kwargs):
                pass

            # Make the LLM ask for an unfindable song.
            client = engine.services.llm_client
            client.chat._issue = True
            original_create = client.chat.create

            missing = json.dumps({"query": "没有这首"}, ensure_ascii=False)

            def create_with_missing_song(**kwargs):
                response = original_create(**kwargs)
                if kwargs.get("stream"):
                    # Streaming probe: rewrite the tool_call args inside the chunks.
                    def rewritten():
                        for chunk in response:
                            for choice in (chunk.choices or []):
                                for tc in (getattr(choice.delta, "tool_calls", None) or []):
                                    if tc.function and tc.function.name == "sing_song":
                                        tc.function.arguments = missing
                            yield chunk
                    return rewritten()
                message = getattr(response.choices[0], "message", None)
                if message is not None and getattr(message, "tool_calls", None):
                    message.tool_calls[0].function.arguments = missing
                return response

            client.chat.create = create_with_missing_song
            answer, _ = _stream(engine, SONG_QUESTION)

        self.assertEqual(answer, "找到了《稻香》，我去清嗓～")  # she still answers
        self.assertEqual([e for e in events if getattr(e, "kind", "") == "song_request"], [])
        followup_text = calls[1][1]["messages"][0]["content"]
        self.assertIn("SONG_NOT_FOUND", followup_text)


class SupplyWordlistTest(unittest.TestCase):
    """Contract 1: plain chat pays ZERO probe cost; song-ish text gets the tool."""

    def test_plain_chat_carries_no_tools(self):
        calls = []
        with tempfile.TemporaryDirectory() as tmp:
            engine, _events, _host = _build_engine(_deepseek_client(calls), tmp)
            answer, _ = _stream(engine, PLAIN_QUESTION)

        self.assertEqual(answer, "找到了《稻香》，我去清嗓～")
        self.assertEqual(len(calls), 1)  # no probe at all
        method, kwargs = calls[0]
        self.assertEqual(set(kwargs), {"model", "messages", "stream"})

    def test_songish_text_offers_sing_song(self):
        from agent_tools.function_tools.router import is_song_intent_possible

        self.assertTrue(is_song_intent_possible("给我唱一首稻香"))
        self.assertTrue(is_song_intent_possible("来点音乐"))
        self.assertFalse(is_song_intent_possible("今天过得怎么样"))


class EffectMetadataTest(unittest.TestCase):
    """Contract 4 (P2 risk tiers): the three existing tools + the first act tool."""

    def test_registered_effects(self):
        host = AppHost()
        self.assertEqual(host.registry.tool_effect("sing_song"), "act")
        self.assertEqual(host.registry.tool_effect("note_game_observation"), "write")
        self.assertEqual(host.registry.tool_effect("watch_game_screen"), "read")
        self.assertEqual(host.registry.tool_effect("inspect_screen"), "read")
        self.assertEqual(host.registry.tool_effect("unknown"), "read")

    def test_invalid_effect_rejected(self):
        host = AppHost()
        with self.assertRaises(ValueError):
            host.registry.register_tool(
                {"type": "function", "name": "bad", "parameters": {}},
                lambda **k: {},
                effect="dangerous",
            )


class SingSongToolUnitTest(unittest.TestCase):
    def test_empty_query_rejected_before_closure(self):
        recorded = []
        tool = SingSongTool(recorded.append)
        with self.assertRaises(ScreenToolError) as caught:
            tool.run(query="   ")
        self.assertEqual(caught.exception.code, "SONG_QUERY_EMPTY")
        self.assertEqual(recorded, [])


if __name__ == "__main__":
    unittest.main()

"""Phase 0 characterization (golden) test for the streaming voice pipeline.

Records the CURRENT semantic behaviour of ``stream_voice_events`` so that later
phases which restructure the pipeline cannot silently change behaviour --
especially Phase 6A (replaces the raw ``{"event": ..., "data": ...}`` dicts with
``RuntimeEvent`` dataclasses) and Phase 6C (decomposes the pipeline into
``spica/runtime/`` components).

Design rule (REFACTOR_PLAN Phase 0): assertions are FORMAT-AGNOSTIC. They check
event *semantics and order* (which kind, which index, what text, what emotion,
what ordering) -- never "is this a dict". Every event-shape access goes through
the ``_event_kind`` / ``_field`` seam below. When Phase 6A introduces
RuntimeEvent, only that seam learns the dataclass form and this test then runs
unchanged against both the old dict path and the new RuntimeEvent path.

Fakes are intentionally self-contained (no shared conftest) per the Phase 0
decision, so this golden does not move when other test files are refactored.
"""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from memory.recent import RecentMemory
from memory.store import SQLiteMemoryStore
from spica.runtime.services import AgentServices
from spica.runtime.context import TurnContext, TurnRequest
from spica.runtime.orchestrator import stream_voice_events
from agent_tools.function_tools import TOOL_SCHEMAS, default_tool_functions
from agent_tools.tts.schemas import TTSRequest, TTSResult
from spica.core.events import event_from_legacy


# --- format-agnostic seam -------------------------------------------------
# Phase 0: events are dicts ``{"event": kind, "data": {...}}``.
# Phase 6A: events become ``RuntimeEvent`` dataclasses. When that lands, teach
# these two helpers the dataclass form (a ``kind`` attribute + field attributes)
# and every assertion below keeps working on both the old and new paths.

def _event_kind(event):
    if isinstance(event, dict):
        return event["event"]
    return getattr(event, "kind", type(event).__name__)


def _field(event, name, default=None):
    if isinstance(event, dict):
        return event.get("data", {}).get(name, default)
    return getattr(event, name, default)


# --- self-contained fakes -------------------------------------------------

class _FakeResponse:
    def __init__(self, text):
        self.id = "golden-stream-response"
        self.output_text = text
        self.output = []
        self.usage = SimpleNamespace(input_tokens=10, output_tokens=5, total_tokens=15)


class _FakeResponses:
    def __init__(self, text):
        self.text = text
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs.get("stream"):
            chunks = [self.text[i:i + 9] for i in range(0, len(self.text), 9)]
            events = [
                SimpleNamespace(type="response.output_text.delta", delta=chunk)
                for chunk in chunks
            ]
            events.append(
                SimpleNamespace(type="response.completed", response=_FakeResponse(self.text))
            )
            return iter(events)
        return _FakeResponse(self.text)


class _FakeLLMClient:
    def __init__(self, text):
        self.responses = _FakeResponses(text)


class _FakeVisual:
    def prepare_stream_context(self, requested_costume=None, requested_mode=None):
        return {
            "costume": requested_costume or "school",
            "costume_mode": requested_mode or "fixed",
            "dialog": {},
            "character": {},
            "classifier_version": "golden-local",
        }

    def build_unit_visual_payload(self, **kwargs):
        unit_index = kwargs["unit_index"]
        return {
            "costume": "school",
            "costume_mode": "fixed",
            "classifier_version": "golden-local",
            "selection_source": "local_vote_classifier",
            "selection_error": None,
            "classifier": {"duration_ms": 3.0, "confidence": 0.9, "signals": ["explain"]},
            "dialog": {},
            "character": {},
            "cue": {
                "index": unit_index,
                "text": kwargs["current_unit_text"],
                "expression_id": "002",
                "hand_pose": "normal",
                "image_url": "/visual/file/golden.png",
                "reason": "golden",
            },
        }


class _FakeTTS:
    name = "golden_tts"

    def __init__(self):
        self.calls = 0

    def synthesize(self, request):
        assert isinstance(request, TTSRequest)
        index = self.calls
        self.calls += 1
        return TTSResult(
            ok=True,
            provider=self.name,
            audio_url=f"/static/generated_voice/unit_{index}.wav",
            audio_path="/tmp/golden.wav",
            timing={"tts_total_ms": 2.0},
            duration_ms=2.0,
        )


def _make_services(tmpdir, answer_text):
    raw = json.dumps(
        {"answer": answer_text, "emotion": "happy", "emotion_reason": "説明口調。"},
        ensure_ascii=False,
    )
    return AgentServices(
        llm_client=_FakeLLMClient(raw),
        tts_adapter=_FakeTTS(),
        visual_tool=_FakeVisual(),
        memory_store=SQLiteMemoryStore(Path(tmpdir) / "memory.sqlite3"),
        recent_memory=RecentMemory(max_turns=3),
        config={
            "model": "golden-model",
            "character_profile": "profile",
            "recent_context_limit": 3,
            "long_term_memory_limit": 5,
            "max_tool_rounds": 2,
        },
        logger=lambda *a, **k: None,
        tool_functions=default_tool_functions(),
        tool_schemas=TOOL_SCHEMAS,
    )


# Two-sentence answer that deterministically yields exactly two play units: a
# short opener that merges into sentence 1, plus a trailing short sentence.
GOLDEN_ANSWER = "もちろん。フーリエ変換は信号を分解します。必要なら具体例も出しますよ。"


class StreamingGoldenTest(unittest.TestCase):
    def _run(self, answer, user_input="説明して"):
        with tempfile.TemporaryDirectory() as tmpdir:
            services = _make_services(tmpdir, answer)
            return list(
                stream_voice_events(
                    TurnContext(TurnRequest(conversation_id="c1", user_input=user_input)), services
                )
            )

    def test_happy_path_event_semantics_and_order(self):
        dict_events = self._run(GOLDEN_ANSWER)
        runtime_events = [event_from_legacy(d) for d in dict_events]
        # Phase 6A: dict <-> RuntimeEvent is lossless across the boundary.
        self.assertEqual([e.to_legacy_dict() for e in runtime_events], dict_events)

        def _check(events):
            kinds = [_event_kind(e) for e in events]

            # 1. status(thinking) opens the stream; done closes it.
            self.assertEqual(kinds[0], "status")
            self.assertEqual(_field(events[0], "state"), "thinking")
            self.assertEqual(kinds[-1], "done")

            # 2. No raw token leakage across the Host->UI boundary.
            self.assertTrue(all("token" not in kind for kind in kinds))

            # 3. Exactly two ordered, contiguous play units whose display texts
            #    together reconstruct the full answer.
            ready = [e for e in events if _event_kind(e) == "unit_ready"]
            self.assertEqual([_field(e, "index") for e in ready], [0, 1])
            self.assertEqual("".join(_field(e, "display_text") for e in ready), GOLDEN_ANSWER)
            for e in ready:
                self.assertTrue(_field(e, "emotion"))  # every unit carries an emotion

            # 4. Per-index ordering: a unit's text is announced before it is ready.
            for index in (0, 1):
                text_pos = next(
                    i for i, e in enumerate(events)
                    if _event_kind(e) == "unit_text_ready" and _field(e, "index") == index
                )
                ready_pos = next(
                    i for i, e in enumerate(events)
                    if _event_kind(e) == "unit_ready" and _field(e, "index") == index
                )
                self.assertLess(text_pos, ready_pos)

            # 5. done carries the assembled answer, unit count and parsed emotion.
            done = events[-1]
            self.assertEqual(_field(done, "answer"), GOLDEN_ANSWER)
            self.assertEqual(_field(done, "units_count"), 2)
            self.assertEqual(_field(done, "emotion"), "happy")

        # Same assertions hold whether events are legacy dicts or RuntimeEvents.
        for label, events in (("dict", dict_events), ("runtime", runtime_events)):
            with self.subTest(path=label):
                _check(events)

    def test_error_path_emits_error_and_no_done(self):
        dict_events = self._run(GOLDEN_ANSWER, user_input="")
        runtime_events = [event_from_legacy(d) for d in dict_events]
        self.assertEqual([e.to_legacy_dict() for e in runtime_events], dict_events)

        def _check(events):
            kinds = [_event_kind(e) for e in events]
            self.assertEqual(kinds[0], "status")
            self.assertEqual(_field(events[0], "state"), "thinking")
            self.assertIn("error", kinds)
            self.assertNotIn("done", kinds)
            self.assertNotIn("unit_ready", kinds)
            error = next(e for e in events if _event_kind(e) == "error")
            self.assertTrue(_field(error, "message"))

        for label, events in (("dict", dict_events), ("runtime", runtime_events)):
            with self.subTest(path=label):
                _check(events)


if __name__ == "__main__":
    unittest.main()

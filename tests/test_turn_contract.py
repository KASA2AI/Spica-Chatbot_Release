"""C0 -- turn event contract welded shut (REFACTOR_PLAN_CORE, stage C0).

Characterises the CURRENT semantic behaviour of ``stream_voice_events`` across
the 7 turn shapes that later stages (C1+) must not silently break, using ONLY
the two reusable matchers from ``support.event_asserts``:

  * ``assert_ordered_axis``  -- precise on the ordered main axis
    (unit_ready index order / done after them / error terminates);
  * ``assert_telemetry_present`` -- presence-only on telemetry
    (status / unit_text_ready / ...), never order / count / timing.

Deliberately NOT asserted (REFACTOR_PLAN_CORE §0): timing numbers, the relative
order of ``unit_visual_ready`` vs ``unit_audio_*``, how many times ``status``
appears, or any thread-scheduling-dependent telemetry order. Those are noise the
hardening stages are free to change.

Fakes are self-contained (no shared conftest), per the Phase 0 decision, so this
contract does not move when other test files are refactored.
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

# Make ``support`` importable regardless of pytest's import mode.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from support.event_asserts import (  # noqa: E402
    assert_ordered_axis,
    assert_telemetry_present,
    event_field,
    event_kind,
    unit_ready_events,
)

from memory.recent import RecentMemory  # noqa: E402
from memory.store import SQLiteMemoryStore  # noqa: E402
from agent.state import AgentServices  # noqa: E402
from spica.runtime.context import TurnContext, TurnRequest  # noqa: E402
from agent.streaming_pipeline import stream_voice_events  # noqa: E402
from agent_tools.function_tools import TOOL_SCHEMAS, default_tool_functions  # noqa: E402
from agent_tools.tts.schemas import TTSRequest, TTSResult  # noqa: E402
from spica.core.events import event_from_legacy  # noqa: E402


# --- self-contained fakes -------------------------------------------------- #

class _FakeResponse:
    def __init__(self, text):
        self.id = "contract-response"
        self.output_text = text
        self.output = []
        self.usage = SimpleNamespace(input_tokens=10, output_tokens=5, total_tokens=15)


class _StreamingResponses:
    """OpenAI-Responses style: streams ``text`` as output_text deltas."""

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


class _StreamingLLMClient:
    def __init__(self, text):
        self.responses = _StreamingResponses(text)


class _RaisingResponses:
    """Every call raises -- streaming creation AND the non-stream fallback, so the
    failure propagates out of the adapter into the orchestrator's except branch."""

    def __init__(self):
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        raise RuntimeError("llm exploded mid-turn")


class _RaisingLLMClient:
    def __init__(self):
        self.responses = _RaisingResponses()


class _ToolThenAnswerResponses:
    """Probe (non-stream, has tools) -> one inspect_screen function_call;
    final (stream=True) -> the JSON answer."""

    def __init__(self, answer_json):
        self.answer_json = answer_json
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs.get("stream"):
            text = self.answer_json
            chunks = [text[i:i + 9] for i in range(0, len(text), 9)]
            events = [
                SimpleNamespace(type="response.output_text.delta", delta=chunk)
                for chunk in chunks
            ]
            events.append(
                SimpleNamespace(type="response.completed", response=_FakeResponse(text))
            )
            return iter(events)
        # Non-stream probe with tools -> request a screen inspection.
        return SimpleNamespace(
            id="contract-tool-call",
            output_text="",
            output=[
                SimpleNamespace(
                    type="function_call",
                    name="inspect_screen",
                    arguments=json.dumps(
                        {"target": "full_screen", "question": "看看屏幕"},
                        ensure_ascii=False,
                    ),
                )
            ],
            usage=SimpleNamespace(input_tokens=10, output_tokens=5, total_tokens=15),
        )


class _ToolThenAnswerLLMClient:
    def __init__(self, answer_json):
        self.responses = _ToolThenAnswerResponses(answer_json)


class _FakeVisual:
    def prepare_stream_context(self, requested_costume=None, requested_mode=None):
        return {
            "costume": requested_costume or "school",
            "costume_mode": requested_mode or "fixed",
            "dialog": {},
            "character": {},
            "classifier_version": "contract-local",
        }

    def build_unit_visual_payload(self, **kwargs):
        unit_index = kwargs["unit_index"]
        return {
            "costume": "school",
            "costume_mode": "fixed",
            "classifier_version": "contract-local",
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
                "image_url": "/visual/file/contract.png",
                "reason": "contract",
            },
        }


class _FakeTTS:
    name = "contract_tts"

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
            audio_path="/tmp/contract.wav",
            timing={"tts_total_ms": 2.0},
            duration_ms=2.0,
        )


def _json_reply(answer, emotion="happy", reason="説明口調。"):
    return json.dumps(
        {"answer": answer, "emotion": emotion, "emotion_reason": reason},
        ensure_ascii=False,
    )


def _make_services(tmpdir, llm):
    return AgentServices(
        llm_client=llm,
        tts_adapter=_FakeTTS(),
        visual_tool=_FakeVisual(),
        memory_store=SQLiteMemoryStore(Path(tmpdir) / "memory.sqlite3"),
        recent_memory=RecentMemory(max_turns=3),
        config={
            "model": "contract-model",
            "character_profile": "profile",
            "recent_context_limit": 3,
            "long_term_memory_limit": 5,
            "max_tool_rounds": 2,
        },
        logger=lambda *a, **k: None,
        tool_functions=default_tool_functions(),
        tool_schemas=TOOL_SCHEMAS,
    )


def _make_screen_attachment():
    return {
        "kind": "screen_capture",
        "target": "selected_region",
        "mode": "region",
        "source": "manual_region_selection",
        "created_at": "2026-06-06T00:00:00+00:00",
        "captured_at": "2026-06-06T00:00:00+00:00",
        "image_bytes": b"png-bytes",
        "mime_type": "image/png",
        "width": 100,
        "height": 80,
        "original_resolution": {"width": 100, "height": 80},
        "sent_resolution": {"width": 100, "height": 80},
        "downscaled": False,
        "format": "png",
        "quality": None,
        "region": {
            "screen_name": "primary",
            "logical": {"x": 0, "y": 0, "width": 100, "height": 80},
            "physical": {"x": 0, "y": 0, "width": 100, "height": 80},
            "device_pixel_ratio": 1.0,
        },
    }


def _fake_screen_observation(question):
    return {
        "schema_version": "screen_observation.v1",
        "type": "screen_observation",
        "request": {
            "user_question": question,
            "question_type": "general_observation",
            "target": "region",
        },
        "capture": {"captured_scope": "region", "source": "manual_region_selection"},
        "answer": {"direct_answer": "ブラウザが見えます。", "confidence": 0.8},
        "followup": {
            "context_for_next_turn": "region shows a browser",
            "needs_followup_capture": False,
            "suggested_capture": None,
        },
        "limitations": ["single screenshot only"],
    }


def _screen_tool_result(target, question):
    return json.dumps(
        {
            "ok": True,
            "data": {
                "schema_version": "screen_observation.v1",
                "type": "screen_observation",
                "request": {
                    "user_question": question,
                    "question_type": "general_observation",
                    "target": target,
                },
                "capture": {"captured_scope": "full_screen", "source": "automatic_screenshot"},
                "answer": {"direct_answer": "画面にエラーは見えません。", "confidence": 0.9},
                "followup": {
                    "context_for_next_turn": "No visible error.",
                    "needs_followup_capture": False,
                    "suggested_capture": None,
                },
            },
            "error": None,
        },
        ensure_ascii=False,
    )


# Two-sentence answer that deterministically yields exactly two play units.
GOLDEN_ANSWER = "もちろん。フーリエ変換は信号を分解します。必要なら具体例も出しますよ。"


class TurnContractTest(unittest.TestCase):
    def _run(self, state, services):
        return list(stream_voice_events(state, services))

    # --- scenario 1: empty input ------------------------------------------- #
    def test_empty_input_errors_with_no_units_or_done(self):
        with tempfile.TemporaryDirectory() as tmp:
            services = _make_services(tmp, _StreamingLLMClient(_json_reply("x")))
            events = self._run(TurnContext(TurnRequest(conversation_id="c1", user_input="")), services)

        # A leading status is allowed; the axis must end in error with no units.
        assert_ordered_axis(events, expected_units=0, terminal="error")
        error = next(e for e in events if event_kind(e) == "error")
        self.assertTrue(event_field(error, "message"))

    # --- scenario 2: normal multi-sentence reply --------------------------- #
    def test_normal_reply_emits_ordered_units_then_done(self):
        with tempfile.TemporaryDirectory() as tmp:
            services = _make_services(tmp, _StreamingLLMClient(_json_reply(GOLDEN_ANSWER)))
            events = self._run(
                TurnContext(TurnRequest(conversation_id="c1", user_input="説明して")), services
            )

        assert_ordered_axis(events, expected_units=2, terminal="done")
        self.assertEqual(
            "".join(event_field(e, "display_text") for e in unit_ready_events(events)),
            GOLDEN_ANSWER,
        )
        assert_telemetry_present(events, ["unit_text_ready"])
        done = events[-1]
        self.assertEqual(event_field(done, "answer"), GOLDEN_ANSWER)

    # --- scenario 3: tool round (inspect_screen intent hit) ---------------- #
    def test_tool_round_triggers_then_emits_units(self):
        answer = "画面にはエラーは見えません。"
        with tempfile.TemporaryDirectory() as tmp:
            services = _make_services(tmp, _ToolThenAnswerLLMClient(_json_reply(answer)))
            tool_calls = []

            def fake_inspect_screen(target, question):
                tool_calls.append({"target": target, "question": question})
                return _screen_tool_result(target, question)

            services.tool_functions = {"inspect_screen": fake_inspect_screen}
            # "看看" (action) + "屏幕" (target) -> is_screen_intent_explicit True.
            events = self._run(
                TurnContext(TurnRequest(conversation_id="c1", user_input="帮我看看屏幕上有没有报错")),
                services,
            )

        assert_ordered_axis(events, terminal="done")
        self.assertGreaterEqual(len(unit_ready_events(events)), 1)
        # Tool path was actually exercised (presence only; not how many times).
        assert_telemetry_present(events, ["status:tools"])
        self.assertEqual(len(tool_calls), 1)
        self.assertEqual(event_field(events[-1], "answer"), answer)

    # --- scenario 4: manual screenshot attachment -------------------------- #
    def test_manual_attachment_injects_observation_without_other_tools(self):
        answer = "スクリーンショットにはブラウザが見えます。"
        with tempfile.TemporaryDirectory() as tmp:
            services = _make_services(tmp, _StreamingLLMClient(_json_reply(answer)))
            tool_calls = []
            services.tool_functions = {
                "inspect_screen": lambda **kw: tool_calls.append(kw) or "{}"
            }
            state = TurnContext(TurnRequest(
                conversation_id="c1",
                user_input="これは何？",
                screen_attachment=_make_screen_attachment(),
            ))
            with patch(
                "agent.nodes.analyze_screen_attachment",
                lambda *, attachment, user_question: _fake_screen_observation(user_question),
            ):
                events = self._run(state, services)

        assert_ordered_axis(events, terminal="done")
        self.assertGreaterEqual(len(unit_ready_events(events)), 1)
        # The attachment turn announces inspecting_screen but fires NO model tool.
        assert_telemetry_present(events, ["status:tools"])
        self.assertEqual(tool_calls, [])
        self.assertTrue(
            all("tools" not in call for call in services.llm_client.responses.calls)
        )
        # Observation was injected into the turn state (used downstream in prompt).
        self.assertIsNotNone(state.screen_observation)

    # --- scenario 5: non-JSON reply -> text heuristic fallback ------------- #
    def test_non_json_reply_falls_back_to_heuristic_and_still_emits_units(self):
        plain = "今日はとても良い天気ですね。散歩でもしませんか。"
        with tempfile.TemporaryDirectory() as tmp:
            services = _make_services(tmp, _StreamingLLMClient(plain))
            events = self._run(
                TurnContext(TurnRequest(conversation_id="c1", user_input="雑談しよう")), services
            )

        assert_ordered_axis(events, terminal="done")
        self.assertGreaterEqual(len(unit_ready_events(events)), 1)
        # Heuristic parse recovered the plain text as the answer.
        self.assertEqual(event_field(events[-1], "answer"), plain)

    # --- scenario 6: single-unit fallback (empty answer -> apology) -------- #
    def test_empty_answer_uses_single_unit_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            services = _make_services(tmp, _StreamingLLMClient(_json_reply("")))
            events = self._run(
                TurnContext(TurnRequest(conversation_id="c1", user_input="……")), services
            )

        # No streamed units (empty answer), but the fallback still yields one.
        assert_ordered_axis(events, terminal="done")
        self.assertGreaterEqual(len(unit_ready_events(events)), 1)
        self.assertTrue(event_field(events[-1], "answer"))

    # --- scenario 7: mid-stream exception ---------------------------------- #
    def test_mid_stream_exception_emits_error_and_no_done(self):
        with tempfile.TemporaryDirectory() as tmp:
            services = _make_services(tmp, _RaisingLLMClient())
            events = self._run(
                TurnContext(TurnRequest(conversation_id="c1", user_input="説明して")), services
            )

        assert_ordered_axis(events, expected_units=0, terminal="error")
        error = next(e for e in events if event_kind(e) == "error")
        self.assertTrue(event_field(error, "message"))

    # --- dict <-> RuntimeEvent main-axis equivalence ----------------------- #
    def test_dict_runtime_axis_equivalence(self):
        with tempfile.TemporaryDirectory() as tmp:
            services = _make_services(tmp, _StreamingLLMClient(_json_reply(GOLDEN_ANSWER)))
            dict_events = self._run(
                TurnContext(TurnRequest(conversation_id="c1", user_input="説明して")), services
            )
        runtime_events = [event_from_legacy(d) for d in dict_events]

        # Round-trip is lossless on the boundary the UI/tests share...
        self.assertEqual([e.to_legacy_dict() for e in runtime_events], dict_events)
        # ...and both representations satisfy the same axis + telemetry contract.
        for events in (dict_events, runtime_events):
            assert_ordered_axis(events, expected_units=2, terminal="done")
            assert_telemetry_present(events, ["status", "unit_text_ready"])


if __name__ == "__main__":
    unittest.main()

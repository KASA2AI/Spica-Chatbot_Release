"""C3c unit tests for the typed turn context (spica/runtime/context.py).

Locks the shape that replaces the ``AgentState`` blackboard: per-stage sub-objects
are ``None`` until their stage runs, mutable accumulators are per-instance, and a
``TurnError`` serializes to the legacy dict only through the one serializer.
"""

import unittest

from spica.runtime.context import (
    PromptBundle,
    RetrievedContext,
    StreamedAnswer,
    TurnContext,
    TurnError,
    TurnRequest,
    turn_error_to_legacy_dict,
)


class TurnContextTest(unittest.TestCase):
    def test_stage_outputs_are_none_until_their_stage(self):
        ctx = TurnContext(TurnRequest(user_input="hi"))
        # The whole point: a prep stage cannot read a later stage's output.
        self.assertIsNone(ctx.recent)
        self.assertIsNone(ctx.screen_observation)
        self.assertIsNone(ctx.prompt)
        self.assertIsNone(ctx.answer)
        self.assertIsNone(ctx.error)

    def test_accumulators_default_empty(self):
        ctx = TurnContext(TurnRequest(user_input="hi"))
        self.assertEqual(ctx.timing, {})
        self.assertEqual(ctx.metadata, {})
        self.assertEqual(ctx.tools, [])
        self.assertEqual(ctx.response_payload, {})
        self.assertEqual(ctx.user_local_time, {})

    def test_response_id_is_flat_and_defaults_none(self):
        # The LLM port writes ctx.response_id (and reads it back via ``or``).
        ctx = TurnContext(TurnRequest(user_input="hi"))
        self.assertIsNone(ctx.response_id)

    def test_mutable_defaults_are_per_instance(self):
        a = TurnContext(TurnRequest(user_input="a"))
        b = TurnContext(TurnRequest(user_input="b"))
        a.timing["x"] = 1
        a.tools.append({"name": "t"})
        a.metadata["k"] = "v"
        a.response_payload["answer"] = "z"
        self.assertEqual(b.timing, {})
        self.assertEqual(b.tools, [])
        self.assertEqual(b.metadata, {})
        self.assertEqual(b.response_payload, {})

    def test_user_input_defaults_to_request_input(self):
        ctx = TurnContext(TurnRequest(user_input="  hi  "))
        # __post_init__ seeds the working input from the request (raw; validate
        # strips it later). It must never silently default to "".
        self.assertEqual(ctx.user_input, "  hi  ")

    def test_explicit_user_input_is_kept(self):
        ctx = TurnContext(TurnRequest(user_input="raw"), user_input="normalized")
        self.assertEqual(ctx.user_input, "normalized")

    def test_request_is_frozen(self):
        req = TurnRequest(user_input="hi")
        with self.assertRaises(Exception):
            req.user_input = "changed"  # type: ignore[misc]


class SubObjectDefaultsTest(unittest.TestCase):
    def test_retrieved_context_defaults(self):
        r = RetrievedContext()
        self.assertEqual(r.recent_context, [])
        self.assertEqual(r.long_term_memories, [])

    def test_prompt_bundle_default(self):
        self.assertIsNone(PromptBundle().prompt_input)

    def test_streamed_answer_defaults(self):
        a = StreamedAnswer()
        self.assertIsNone(a.raw_model_output)
        self.assertIsNone(a.parsed_reply)
        self.assertIsNone(a.answer)
        self.assertIsNone(a.emotion)
        self.assertIsNone(a.visual)
        self.assertIsNone(a.tts_result)
        # response_id is flat on TurnContext (port-written telemetry), not here.
        self.assertFalse(hasattr(a, "response_id"))


class TurnErrorTest(unittest.TestCase):
    def test_is_frozen(self):
        err = TurnError(code="EMPTY_MESSAGE", message="message 不能为空。")
        with self.assertRaises(Exception):
            err.code = "OTHER"  # type: ignore[misc]

    def test_single_serializer_returns_legacy_shape(self):
        err = TurnError(code="TTS_FAILED", message="boom")
        self.assertEqual(
            turn_error_to_legacy_dict(err),
            {"code": "TTS_FAILED", "message": "boom"},
        )


if __name__ == "__main__":
    unittest.main()

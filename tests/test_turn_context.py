"""C3c unit tests for the typed turn context (spica/runtime/context.py).

Locks the shape that replaces the ``AgentState`` blackboard: per-stage sub-objects
are ``None`` until their stage runs, mutable accumulators are per-instance, and a
``TurnError`` serializes to the legacy dict only through the one serializer.
"""

import unittest

from spica.runtime.context import (
    GameContextRequest,
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


class TurnRequestGalgameFieldsTest(unittest.TestCase):
    """Phase 1: typed galgame turn-input fields + the §27① fallback. These are
    fields-and-defaults only; no stage consumes them until Phase 3."""

    def test_new_fields_default_to_none(self):
        req = TurnRequest(user_input="hi")
        self.assertIsNone(req.memory_conversation_id)
        self.assertIsNone(req.command_intent)
        self.assertIsNone(req.game_context_request)

    def test_existing_defaults_unchanged(self):
        # A plain chat turn must look exactly like before (zero behaviour change).
        req = TurnRequest(user_input="hi")
        self.assertEqual(req.conversation_id, "default")
        self.assertEqual(req.interaction_mode, "chat")

    def test_memory_conversation_id_falls_back_to_conversation_id(self):
        req = TurnRequest(user_input="hi", conversation_id="default")
        self.assertEqual(req.effective_memory_conversation_id, "default")

        # A galgame turn keeps recent-memory continuity on the galgame id while
        # reading long-term character memory from the "default" namespace.
        galgame = TurnRequest(
            user_input="刚才发生什么了",
            conversation_id="galgame::ABC::playthrough::default",
            memory_conversation_id="default",
        )
        self.assertEqual(
            galgame.conversation_id, "galgame::ABC::playthrough::default"
        )
        self.assertEqual(galgame.effective_memory_conversation_id, "default")

    def test_explicit_memory_conversation_id_is_used(self):
        req = TurnRequest(
            user_input="hi", conversation_id="c1", memory_conversation_id="m1"
        )
        self.assertEqual(req.effective_memory_conversation_id, "m1")

    def test_game_context_request_is_typed_and_frozen(self):
        gcr = GameContextRequest(mode="active", game_id="ABC")
        self.assertEqual(gcr.mode, "active")
        self.assertEqual(gcr.game_id, "ABC")
        self.assertEqual(gcr.playthrough_id, "default")
        with self.assertRaises(Exception):
            gcr.mode = "offline"  # type: ignore[misc]

    def test_game_context_request_default_mode_is_none(self):
        self.assertEqual(GameContextRequest().mode, "none")


class GameTurnBindingTest(unittest.TestCase):
    """Stage 2: the frozen binding a companion controller publishes for turn
    auto-fill. Two fields only -- memory_conversation_id is deliberately absent
    (derived at _request time from the caller's conversation, manifest ①)."""

    def test_frozen_with_two_fields(self):
        import dataclasses

        from spica.runtime.context import GameTurnBinding

        binding = GameTurnBinding(
            conversation_id="galgame::g::playthrough::default",
            game_context_request=GameContextRequest(mode="active", game_id="g"),
        )
        self.assertEqual(
            {f.name for f in dataclasses.fields(GameTurnBinding)},
            {"conversation_id", "game_context_request"},
        )
        with self.assertRaises(dataclasses.FrozenInstanceError):
            binding.conversation_id = "x"  # type: ignore[misc]

    def test_prefix_constant_matches_conversation_id_helper(self):
        # The value anchor across the three deliberate literals (context.py /
        # stages.py / models.game_conversation_id) -- see GALGAME_FINDINGS.md #9.
        from spica.galgame.models import game_conversation_id
        from spica.runtime.context import GALGAME_CONVERSATION_PREFIX

        self.assertEqual(GALGAME_CONVERSATION_PREFIX, "galgame::")
        self.assertTrue(game_conversation_id("g").startswith(GALGAME_CONVERSATION_PREFIX))


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

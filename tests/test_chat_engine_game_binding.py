"""Path B stage 2: ChatEngine._request auto-fills galgame turn fields from the
host-injected GameTurnBinding provider.

Core guarantee under test: provider unset / returning None -> the built
TurnRequest is FIELD-FOR-FIELD identical to today's (frozen dataclass equality),
so a plain chat turn cannot change by a byte. With a binding active, exactly
three fields change (conversation_id / memory_conversation_id /
game_context_request) and memory_conversation_id preserves the CALLER's original
conversation (§27①, manifest correction ①).
"""

import dataclasses
import unittest

from spica.config.schema import AppConfig
from spica.core.chat_engine import ChatEngine
from spica.runtime.context import GameContextRequest, GameTurnBinding, TurnRequest
from spica.runtime.services import AgentServices


def _engine() -> ChatEngine:
    services = AgentServices(
        llm_client=None,
        tts_adapter=None,
        visual_tool=None,
        memory_store=None,
        recent_memory=None,
        config={},
        llm_adapter=object(),  # short-circuit port resolution (no real client)
        memory_adapter=object(),  # short-circuit store wrapping
    )
    return ChatEngine(services, AppConfig())


def _build(engine: ChatEngine, conversation_id: str = "default") -> TurnRequest:
    # The exact positional shape run_voice / stream_voice_runtime hand _request.
    return engine._request("你好", conversation_id, None, None, None, True, "chat", None)


_BINDING = GameTurnBinding(
    conversation_id="galgame::limelight::playthrough::default",
    game_context_request=GameContextRequest(mode="active", game_id="limelight"),
)


class NoProviderTest(unittest.TestCase):
    def test_no_provider_request_field_identical(self):
        # Field-for-field equality against the EXPLICIT expected request (every
        # galgame field None) -- the plain-chat byte-identity contract.
        self.assertEqual(
            _build(_engine()),
            TurnRequest(
                user_input="你好",
                conversation_id="default",
                emotion_override=None,
                interaction_mode="chat",
                include_user_time_context=True,
                screen_attachment=None,
                tts_param_overrides=None,
                visual_overrides={},
                memory_conversation_id=None,
                command_intent=None,
                game_context_request=None,
            ),
        )

    def test_provider_returns_none_identical(self):
        engine = _engine()
        baseline = _build(engine)
        engine.set_game_binding_provider(lambda: None)  # set, but not playing
        self.assertEqual(_build(engine), baseline)


class BindingActiveTest(unittest.TestCase):
    def test_binding_fills_three_fields_only(self):
        engine = _engine()
        baseline = _build(engine)
        engine.set_game_binding_provider(lambda: _BINDING)
        req = _build(engine)
        self.assertEqual(req.conversation_id, "galgame::limelight::playthrough::default")
        self.assertEqual(req.memory_conversation_id, "default")  # caller's original
        self.assertIs(req.game_context_request, _BINDING.game_context_request)
        changed = {"conversation_id", "memory_conversation_id", "game_context_request"}
        for f in dataclasses.fields(TurnRequest):
            if f.name in changed:
                continue
            with self.subTest(field=f.name):
                self.assertEqual(getattr(req, f.name), getattr(baseline, f.name))

    def test_caller_galgame_cid_not_rewritten(self):
        # Double-wrap guard: a caller already addressing a galgame conversation
        # (manual/debug path) is taken as-is even while a binding is active.
        engine = _engine()
        engine.set_game_binding_provider(lambda: _BINDING)
        manual_cid = "galgame::other::playthrough::ng+"
        req = _build(engine, conversation_id=manual_cid)
        self.assertEqual(req.conversation_id, manual_cid)
        self.assertIsNone(req.memory_conversation_id)
        self.assertIsNone(req.game_context_request)

    def test_set_provider_none_resets(self):
        engine = _engine()
        baseline = _build(engine)
        engine.set_game_binding_provider(lambda: _BINDING)
        engine.set_game_binding_provider(None)
        self.assertEqual(_build(engine), baseline)

    def test_memory_conversation_id_preserves_caller_conversation(self):
        # NOT hardcoded "default": whatever conversation the caller was in is the
        # one long-term memory stays continuous with (§27①).
        engine = _engine()
        engine.set_game_binding_provider(lambda: _BINDING)
        req = _build(engine, conversation_id="side_chat")
        self.assertEqual(req.memory_conversation_id, "side_chat")
        self.assertEqual(req.conversation_id, _BINDING.conversation_id)


if __name__ == "__main__":
    unittest.main()

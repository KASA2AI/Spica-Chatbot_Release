"""Phase 7-c1 contract: ``TurnDeps.model`` auto-fill + the orchestrator's final
no-tool stream running through it.

Four pins:
1. ``from_services`` auto-fills ``deps.model`` as ``BoundModel(deps.llm,
   config.llm.model)`` -- every bridge and direct construction gets the v2
   handle for free (the ~25 direct-construction tests stay untouched).
2. An explicitly passed ``model`` is respected, never clobbered by the
   auto-fill.
3. ``dataclasses.replace(deps, ...)`` copies a filled model VERBATIM (the
   orchestrator's per-turn ``replace(deps, observer=..., jobs=...)`` must not
   mint a new binding).
4. The orchestrator's final no-tool stream calls ``deps.model.stream(prompt,
   ctx)`` -- proven by injecting a recording BoundModel-shaped object into a
   REAL ``ChatEngine`` turn (never an LLMPort-level fake). The byte-level
   client request shape ({"model","input","stream"}) stays pinned by
   test_chat_tool_round.test_openai_stream_plain_chat_request_shape and
   test_text_model_contract Group A -- both client-level, both untouched.
"""

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from agent_tools.function_tools import TOOL_SCHEMAS, default_tool_functions
from agent_tools.tts.schemas import TTSRequest, TTSResult
from memory.recent import RecentMemory
from memory.store import SQLiteMemoryStore
from spica.adapters.game_memory.sqlite import GameMemorySqliteAdapter
from spica.config.schema import AppConfig, LLMConfig
from spica.core.chat_engine import ChatEngine
from spica.plugins.registry import CapabilityRegistry
from spica.ports.model import BoundModel
from spica.runtime.deps import TurnDeps
from spica.runtime.services import AgentServices

RAW_ANSWER = json.dumps(
    {"answer": "好呀。", "emotion": "happy", "emotion_reason": "x"}, ensure_ascii=False
)


def _services(client=None):
    return SimpleNamespace(
        llm_adapter=None,
        llm_client=client if client is not None else SimpleNamespace(),
        tts_adapter=None,
        visual_tool=None,
        memory_adapter=None,
        memory_store=SimpleNamespace(),
        recent_memory=SimpleNamespace(),
        tool_registry=None,
        tool_schemas=[],
        tool_functions={},
    )


class AutoFillTest(unittest.TestCase):
    def test_from_services_autofills_bound_model(self):
        config = AppConfig(llm=LLMConfig(model="m-c1"))
        deps = TurnDeps.from_services(_services(), config)
        self.assertIsInstance(deps.model, BoundModel)
        self.assertEqual(deps.model.model, "m-c1")  # bound to the config model
        self.assertIs(deps.model.adapter, deps.llm)  # over the SAME resolved port

    def test_explicit_model_is_respected(self):
        sentinel = BoundModel(SimpleNamespace(), "explicit-m")
        deps = TurnDeps(
            config=AppConfig(), llm=SimpleNamespace(), tts=None, visual=None,
            memory=None, tools=SimpleNamespace(), model=sentinel,
        )
        self.assertIs(deps.model, sentinel)  # auto-fill never clobbers

    def test_no_llm_means_no_model(self):
        # llm=None direct constructions (golden #2 shape) stay model-less; they
        # never reach the stream. NOT a readiness contract -- readiness is
        # llm_ready's job (7-c2), never deps.model's.
        deps = TurnDeps(
            config=AppConfig(), llm=None, tts=None, visual=None,
            memory=None, tools=SimpleNamespace(),
        )
        self.assertIsNone(deps.model)

    def test_replace_preserves_model_identity(self):
        deps = TurnDeps.from_services(_services(), AppConfig())
        replaced = replace(deps, observer=SimpleNamespace(), jobs=SimpleNamespace())
        # The orchestrator does replace(deps, observer=..., jobs=...) per turn:
        # the copied model must be the SAME binding, not a re-minted one.
        self.assertIs(replaced.model, deps.model)

    def test_replace_with_new_llm_keeps_old_binding_by_design(self):
        # BUG-4 ledger (characterization, NOT an endorsement): replace() copies
        # a filled model verbatim. Correct for the observer/jobs path above,
        # but it means ``replace(deps, llm=...)`` / ``replace(deps, config=...)``
        # silently keeps the OLD binding. Today nothing does that; any future
        # model-switching (6b router / settings) must pass a new BoundModel
        # explicitly or rebuild deps -- adjudicated before 6b construction.
        deps = TurnDeps.from_services(_services(), AppConfig())
        new_llm = SimpleNamespace()
        swapped = replace(deps, llm=new_llm)
        self.assertIs(swapped.llm, new_llm)
        self.assertIs(swapped.model, deps.model)          # stale binding kept
        self.assertIsNot(swapped.model.adapter, new_llm)  # the ledgered trap


class LlmReadyTerminalSemanticsTest(unittest.TestCase):
    """Phase 7-c2: readiness = ANY LLM capability (adapter OR raw client).

    Adapter-only is the natural shape of a non-OpenAI provider -- it MUST be
    ready, or "a second provider = just write an adapter" cannot hold. Only
    adapter-less AND client-less is not ready (the 5-c0 error path,
    reinterpreted as "no LLM capability at all"; its message is unchanged).
    """

    def test_adapter_only_services_are_ready(self):
        services = _services(client=None)
        services.llm_client = None
        services.llm_adapter = object()  # a resolved v2-capable adapter, no raw client
        deps = TurnDeps.from_services(services, AppConfig())
        self.assertTrue(deps.llm_ready)

    def test_no_adapter_and_no_client_is_not_ready(self):
        services = _services(client=None)
        services.llm_client = None  # llm_adapter already None in the helper
        deps = TurnDeps.from_services(services, AppConfig())
        self.assertFalse(deps.llm_ready)

    def test_falsey_adapter_only_bundle_binds_the_adapter(self):
        # BUG-3 regression: llm selection must use the SAME ``is not None`` test
        # as llm_ready. A falsy-but-present adapter (e.g. a mock whose __bool__
        # is False) must be BOUND -- under the old ``or`` it was silently
        # swapped for OpenAICompatibleAdapter(None) while llm_ready said True,
        # and the first stream() call crashed on the None client.
        class _FalseyV2Adapter:
            def __bool__(self):
                return False

            def complete(self, prompt, *, model):
                return "ok"

            def stream(self, prompt, *, model, state):
                yield "答"

        adapter = _FalseyV2Adapter()
        services = _services(client=None)
        services.llm_client = None
        services.llm_adapter = adapter
        deps = TurnDeps.from_services(services, AppConfig())
        self.assertTrue(deps.llm_ready)
        self.assertIs(deps.llm, adapter)            # bound, not swapped
        self.assertIs(deps.model.adapter, adapter)  # BoundModel over the SAME adapter
        # The stream really runs on the fake -- never OpenAICompatibleAdapter(None).
        self.assertEqual(list(deps.model.stream("p", SimpleNamespace(timing={}))), ["答"])


# ---- pin 4: the real orchestrator streams through deps.model ---------------- #

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


class _RecordingBoundModel:
    """BoundModel-shaped recorder (stream only -- the final no-tool branch)."""

    def __init__(self):
        self.stream_calls = []

    def stream(self, prompt, state):
        self.stream_calls.append((prompt, state))
        yield RAW_ANSWER


class OrchestratorStreamsThroughModelTest(unittest.TestCase):
    def test_final_no_tool_stream_uses_deps_model(self):
        recorder = _RecordingBoundModel()
        with tempfile.TemporaryDirectory() as tmp:
            services = AgentServices(
                llm_client=SimpleNamespace(), tts_adapter=_FakeTTS(), visual_tool=_FakeVisual(),
                memory_store=SQLiteMemoryStore(Path(tmp) / "m.sqlite3"),
                recent_memory=RecentMemory(max_turns=3),
                game_memory_adapter=GameMemorySqliteAdapter(Path(tmp) / "g.sqlite3"),
                config={"model": "test-model", "character_profile": "p", "recent_context_limit": 3,
                        "long_term_memory_limit": 5, "max_tool_rounds": 3, "character_id": "spica",
                        "interlocutor_name": "麦"},
                logger=lambda *a, **k: None,
                tool_functions=default_tool_functions(), tool_schemas=TOOL_SCHEMAS,
            )
            services.tool_registry = CapabilityRegistry()  # empty -> no tool probe
            engine = ChatEngine(services, AppConfig())
            engine.deps = replace(engine.deps, model=recorder)
            events = list(engine.stream_voice("你好"))

        done = next((e for e in events if e.get("event") == "done"), None)
        self.assertEqual((done or {}).get("data", {}).get("answer"), "好呀。")
        # Exactly one final stream, called (prompt, ctx) -- no request dict at
        # the call site anymore (assembly moved inside the adapter, 7-c1).
        self.assertEqual(len(recorder.stream_calls), 1)
        prompt, state = recorder.stream_calls[0]
        self.assertIsInstance(prompt, str)
        self.assertIn("你好", prompt)  # the built prompt, not a request dict
        self.assertTrue(hasattr(state, "timing"))  # the TurnContext rides through


if __name__ == "__main__":
    unittest.main()

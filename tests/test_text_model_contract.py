"""TextModel / BoundModel v2 contract suite (OO migration Phase 6a).

TWO groups with DIFFERENT (deliberate) fake layers:

- Group A -- OpenAICompatibleAdapter.complete/stream provider-I/O contract.
  Fakes sit at the OpenAI CLIENT layer (recording ``responses.create`` /
  ``chat.completions.create`` kwargs, the ``test_phase5_adapters`` /
  ``test_turn_contract`` idiom) and drive the REAL adapter. A port/summarizer
  level fake here would stop measuring anything real the moment a later phase
  rewires the internals -- the Phase 0 #3 lesson.

- Group B -- BoundModel binding/forwarding contract. BoundModel does no
  provider I/O, only binds ``(adapter, model)``; a MINIMAL recording TextModel
  fake isolates exactly that. B2 is the structural pin for the Phase 6a
  no-compat rule: the production v2 path must never fall back to
  ``complete_text`` -- a v1-only fake must break loudly, not quietly work.

Group A is parameterized over adapter factories so a second provider adapter
can join the same contract without a new suite.
"""

import inspect
import unittest
from types import SimpleNamespace

from spica.adapters.llm import OpenAICompatibleAdapter
from spica.ports.model import BoundModel


# --- Group A fakes: OpenAI client layer (test_phase5_adapters idiom) --------

class _FakeResp:
    def __init__(self, text):
        self.id = "r"
        self.output_text = text
        self.output = []
        self.usage = SimpleNamespace(input_tokens=1, output_tokens=1, total_tokens=2)


class _FakeResponses:
    def __init__(self, text):
        self.text = text
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs.get("stream"):
            chunks = [self.text[i:i + 5] for i in range(0, len(self.text), 5)]
            events = [SimpleNamespace(type="response.output_text.delta", delta=c) for c in chunks]
            events.append(SimpleNamespace(type="response.completed", response=_FakeResp(self.text)))
            return iter(events)
        return _FakeResp(self.text)


class _FakeOpenAI:
    def __init__(self, text="hello world"):
        self.responses = _FakeResponses(text)


class _FakeChatCompletions:
    def __init__(self, text):
        self.text = text
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=self.text))],
            usage=SimpleNamespace(input_tokens=1, output_tokens=1, total_tokens=2),
        )


class _FakeDeepSeek:
    def __init__(self, text="やあ"):
        self.base_url = "https://api.deepseek.com/v1"
        self.chat = SimpleNamespace(completions=_FakeChatCompletions(text))


def _state():
    return SimpleNamespace(timing={}, response_id=None)


# One entry per v2-capable adapter; a future provider adapter registers a
# second (name, factory) pair and inherits the whole Group A contract.
ADAPTER_FACTORIES = [
    ("openai_compatible", OpenAICompatibleAdapter),
]


class AdapterTextModelContractTest(unittest.TestCase):
    """Group A: the REAL adapter over client-layer recording fakes."""

    def test_complete_responses_path_shape(self):
        for name, factory in ADAPTER_FACTORIES:
            with self.subTest(adapter=name):
                client = _FakeOpenAI("回答文本")
                out = factory(client).complete("你好", model="m-A")
                self.assertEqual(out, "回答文本")
                (call,) = client.responses.calls
                self.assertEqual(call["model"], "m-A")
                self.assertEqual(call["input"], "你好")
                self.assertNotIn("tools", call)
                self.assertNotIn("stream", call)  # one-shot, not streamed

    def test_complete_chat_completions_path_shape(self):
        for name, factory in ADAPTER_FACTORIES:
            with self.subTest(adapter=name):
                client = _FakeDeepSeek("こんにちは")
                out = factory(client).complete("hi", model="m-B")
                self.assertEqual(out, "こんにちは")
                (call,) = client.chat.completions.calls
                self.assertEqual(call["model"], "m-B")
                self.assertEqual(call["messages"], [{"role": "user", "content": "hi"}])
                self.assertNotIn("tools", call)

    def test_stream_request_shape_no_tools(self):
        for name, factory in ADAPTER_FACTORIES:
            with self.subTest(adapter=name):
                client = _FakeOpenAI("hello world")
                out = "".join(factory(client).stream("hi", model="m-C", state=_state()))
                self.assertEqual(out, "hello world")
                (call,) = client.responses.calls
                self.assertEqual(call["model"], "m-C")
                self.assertEqual(call["input"], "hi")
                self.assertIs(call["stream"], True)
                self.assertNotIn("tools", call)


# --- Group B fakes: minimal TextModel adapter layer -------------------------

class _RecordingTextModel:
    """v2-only adapter fake: has complete/stream, NO complete_text."""

    def __init__(self, text="ok"):
        self.text = text
        self.complete_calls = []
        self.stream_calls = []

    def complete(self, prompt, *, model):
        self.complete_calls.append((prompt, model))
        return self.text

    def stream(self, prompt, *, model, state):
        self.stream_calls.append((prompt, model, state))
        yield self.text


class _V1OnlyFake:
    """v1-only shape: complete_text but NO v2 complete -- must break loudly."""

    def complete_text(self, prompt, *, model):
        return "v1"


class BoundModelContractTest(unittest.TestCase):
    """Group B: BoundModel binds and forwards; it never routes or shims."""

    def test_complete_injects_the_bound_model_value(self):
        fake = _RecordingTextModel("答")
        self.assertEqual(BoundModel(fake, "m-X").complete("hi"), "答")
        self.assertEqual(fake.complete_calls, [("hi", "m-X")])

    def test_stream_injects_model_and_passes_state_through(self):
        fake = _RecordingTextModel("流")
        state = object()
        out = list(BoundModel(fake, "m-Y").stream("p", state))
        self.assertEqual(out, ["流"])
        self.assertEqual(fake.stream_calls, [("p", "m-Y", state)])

    def test_callers_cannot_repick_the_model(self):
        # The bound value is the ONLY model source: no ``model`` parameter on
        # either consumer-facing method.
        self.assertNotIn("model", inspect.signature(BoundModel.complete).parameters)
        self.assertNotIn("model", inspect.signature(BoundModel.stream).parameters)

    def test_no_complete_text_compat_shim(self):
        # Phase 6a no-compat rule, both directions:
        # a v2-only adapter fully works through BoundModel...
        self.assertEqual(BoundModel(_RecordingTextModel("v2"), "m").complete("p"), "v2")
        # ...and a v1-only fake breaks LOUDLY (no duck-typed fallback to
        # complete_text hiding in the production v2 path).
        with self.assertRaises(AttributeError):
            BoundModel(_V1OnlyFake(), "m").complete("p")


if __name__ == "__main__":
    unittest.main()

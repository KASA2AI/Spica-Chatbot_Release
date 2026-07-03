"""ToolCallingModel / ToolProbeStream v2 contract suite (Phase 7-c2).

Same two-group discipline as tests/test_text_model_contract.py:

- Group A -- the REAL ``OpenAICompatibleAdapter`` over CLIENT-layer recording
  fakes: family internalization (probe_stream Optional-return), laziness (zero
  client I/O at construction), tool_call accumulation shape, and the
  usage no-double-accounting ruling (Responses -> result.usage carries the
  provider usage for the runtime observer; chat -> adapter-internal
  ``_record_usage(state, ...)`` and ``usage=None``).
- Group B -- ``ToolProbeStream``'s own contract over a minimal iterator (no
  provider I/O): ``.calls`` readable ONLY after normal exhaustion; early read /
  abandoned (cancel) / mid-exception reads raise RuntimeError. Plus BoundModel's
  bound-model injection into probe/probe_stream.
"""

import gc
import unittest
from types import SimpleNamespace

from spica.adapters.llm import OpenAICompatibleAdapter
from spica.ports.model import BoundModel, ToolProbeResult, ToolProbeStream

TOOLS = [{"type": "function", "name": "t1", "description": "d",
          "parameters": {"type": "object", "properties": {}}}]


def _state():
    return SimpleNamespace(timing={}, response_id=None)


# --- Group A fakes: client layer --------------------------------------------- #

class _StreamingToolChatAPI:
    """chat.completions returning a streaming tool probe: one content delta,
    then a tool_call SPLIT across chunks (name, then arguments halves)."""

    def __init__(self, die_mid_stream=False):
        self.completions = self
        self.create_calls = 0
        self.die_mid_stream = die_mid_stream

    def create(self, **kwargs):
        self.create_calls += 1
        assert kwargs.get("stream"), "the streaming probe must request stream=True"

        def chunks():
            yield SimpleNamespace(choices=[SimpleNamespace(
                delta=SimpleNamespace(content="前导"))])
            if self.die_mid_stream:
                raise RuntimeError("probe stream died")
            yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(
                content=None,
                tool_calls=[SimpleNamespace(index=0, id="c1", type="function",
                    function=SimpleNamespace(name="t1", arguments='{"a"'))]))])
            yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(
                content=None,
                tool_calls=[SimpleNamespace(index=0, function=SimpleNamespace(
                    name=None, arguments=':1}'))]))])
        return chunks()


class _NonStreamToolChatAPI:
    def __init__(self):
        self.completions = self
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(
                content="答",
                tool_calls=[SimpleNamespace(id="c1", type="function",
                    function=SimpleNamespace(name="t1", arguments='{"a":1}'))],
            ))],
            usage=SimpleNamespace(input_tokens=3, output_tokens=4, total_tokens=7),
        )


def _deepseek(api):
    return SimpleNamespace(base_url="https://api.deepseek.com/v1", chat=api)


class _ToolResponsesAPI:
    def __init__(self, with_calls=True):
        self.calls = []
        self.with_calls = with_calls

    def create(self, **kwargs):
        self.calls.append(kwargs)
        output = (
            [SimpleNamespace(type="function_call", name="t1", arguments='{"a":1}')]
            if self.with_calls else []
        )
        return SimpleNamespace(
            id="resp-1", output=output, output_text="probe文本",
            usage=SimpleNamespace(input_tokens=1, output_tokens=2, total_tokens=3),
        )


def _openai(api):
    return SimpleNamespace(base_url="https://api.openai.com/v1", responses=api)


class AdapterProbeStreamContractTest(unittest.TestCase):
    """Group A: probe_stream over the real adapter."""

    def test_construction_is_lazy_zero_client_io(self):
        api = _StreamingToolChatAPI()
        handle = OpenAICompatibleAdapter(_deepseek(api)).probe_stream(
            "p", TOOLS, model="m", state=_state())
        self.assertIsNotNone(handle)
        self.assertEqual(api.create_calls, 0)  # NO I/O yet (lazy contract)
        deltas = list(handle.deltas)  # first iteration opens the client stream
        self.assertEqual(api.create_calls, 1)
        self.assertEqual(deltas, ["前导"])
        # Normal exhaustion -> calls readable, accumulated across split chunks.
        self.assertEqual(handle.calls, [{"name": "t1", "arguments": '{"a":1}'}])

    def test_responses_family_probe_stream_is_none(self):
        adapter = OpenAICompatibleAdapter(_openai(_ToolResponsesAPI()))
        self.assertIsNone(adapter.probe_stream("p", TOOLS, model="m", state=_state()))

    def test_calls_early_read_raises(self):
        handle = OpenAICompatibleAdapter(_deepseek(_StreamingToolChatAPI())).probe_stream(
            "p", TOOLS, model="m", state=_state())
        with self.assertRaises(RuntimeError):
            _ = handle.calls  # nothing consumed yet

    def test_calls_after_mid_stream_exception_raises(self):
        handle = OpenAICompatibleAdapter(
            _deepseek(_StreamingToolChatAPI(die_mid_stream=True))
        ).probe_stream("p", TOOLS, model="m", state=_state())
        with self.assertRaises(RuntimeError):
            list(handle.deltas)  # the underlying stream dies
        with self.assertRaises(RuntimeError):
            _ = handle.calls  # never normally exhausted -> locked

    def test_calls_after_abandoned_stream_raises(self):
        # The cancel shape: the consumer walks away mid-stream (no exception).
        handle = OpenAICompatibleAdapter(_deepseek(_StreamingToolChatAPI())).probe_stream(
            "p", TOOLS, model="m", state=_state())
        next(handle.deltas)  # consume ONE delta, then abandon
        with self.assertRaises(RuntimeError):
            _ = handle.calls


class AdapterProbeContractTest(unittest.TestCase):
    """Group A: non-streaming probe -- family internalization + usage ruling."""

    def test_responses_probe_normalizes_and_carries_usage(self):
        api = _ToolResponsesAPI()
        state = _state()
        result = OpenAICompatibleAdapter(_openai(api)).probe("p", TOOLS, model="m", state=state)
        self.assertIsInstance(result, ToolProbeResult)
        self.assertEqual(result.calls, [{"name": "t1", "arguments": '{"a":1}'}])
        self.assertEqual(result.text, "probe文本")
        self.assertEqual(result.response_id, "resp-1")
        self.assertIsNotNone(result.usage)  # the RUNTIME records this one (obs)
        (call,) = api.calls
        self.assertEqual((call["model"], call["input"], call["tools"]), ("m", "p", TOOLS))

    def test_responses_probe_no_calls_keeps_response_id(self):
        result = OpenAICompatibleAdapter(_openai(_ToolResponsesAPI(with_calls=False))).probe(
            "p", TOOLS, model="m", state=_state())
        self.assertEqual(result.calls, [])
        self.assertEqual(result.response_id, "resp-1")

    def test_chat_probe_records_usage_internally_and_carries_none(self):
        state = _state()
        result = OpenAICompatibleAdapter(_deepseek(_NonStreamToolChatAPI())).probe(
            "p", TOOLS, model="m", state=state)
        self.assertEqual(result.calls, [{"name": "t1", "arguments": '{"a":1}'}])
        self.assertEqual(result.text, "答")
        # No-double-accounting: usage went into state via the adapter-internal
        # _record_usage; the result deliberately carries None.
        self.assertIsNone(result.usage)
        self.assertEqual(state.timing["input_tokens"], 3)
        self.assertEqual(state.timing["total_tokens"], 7)


# --- Group B: ToolProbeStream own contract + BoundModel injection ------------ #

class ToolProbeStreamContractTest(unittest.TestCase):
    def test_normal_exhaustion_unlocks_calls(self):
        def opener(sink):
            yield "a"
            yield "b"
            sink.append({"name": "t", "arguments": "{}"})
        stream = ToolProbeStream(opener)
        self.assertEqual(list(stream.deltas), ["a", "b"])
        self.assertEqual(stream.calls, [{"name": "t", "arguments": "{}"}])

    def test_opener_not_called_until_iteration(self):
        opened = []

        def opener(sink):
            opened.append(True)
            yield "a"
        stream = ToolProbeStream(opener)
        self.assertEqual(opened, [])  # construction ran NO opener code
        next(stream.deltas)
        self.assertEqual(opened, [True])

    def test_abandoned_stream_closes_on_refcount_without_gc(self):
        # BUG-2 regression (self-cycle): an abandoned (cancelled) probe stream
        # must release its underlying generator via REFCOUNT the moment the
        # handle drops -- never wait for cyclic GC. This is the v1 bare-nested-
        # generator cancel semantics the Phase 7 lazy contract promised to
        # preserve; a bound-method pump would re-create the handle<->generator
        # cycle and this test would go red (finally deferred to gc.collect).
        closed = []

        def opener(sink):
            try:
                yield "a"
                yield "b"
            finally:
                closed.append(True)

        gc_was_enabled = gc.isenabled()
        gc.disable()  # isolate refcount behaviour from the cyclic collector
        try:
            stream = ToolProbeStream(opener)
            next(stream.deltas)  # consume ONE delta, then abandon (cancel shape)
            del stream
            # finally ran BEFORE any gc.collect -> released by refcount alone.
            self.assertEqual(closed, [True])
        finally:
            if gc_was_enabled:
                gc.enable()


class _RecordingToolAdapter:
    def __init__(self):
        self.probe_kwargs = None
        self.probe_stream_kwargs = None

    def probe(self, prompt, tools, *, model, state):
        self.probe_kwargs = (prompt, tools, model, state)
        return ToolProbeResult(calls=[], text="ok")

    def probe_stream(self, prompt, tools, *, model, state):
        self.probe_stream_kwargs = (prompt, tools, model, state)
        return None


class BoundModelProbeInjectionTest(unittest.TestCase):
    def test_probe_and_probe_stream_inject_the_bound_model(self):
        fake = _RecordingToolAdapter()
        bound = BoundModel(fake, "m-X")
        state = object()
        result = bound.probe("p", TOOLS, state)
        self.assertEqual(result.text, "ok")
        self.assertEqual(fake.probe_kwargs, ("p", TOOLS, "m-X", state))
        self.assertIsNone(bound.probe_stream("p", TOOLS, state))
        self.assertEqual(fake.probe_stream_kwargs, ("p", TOOLS, "m-X", state))


if __name__ == "__main__":
    unittest.main()

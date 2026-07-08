"""moondream_hf provider: code-equivalence vs legacy + provider-gate behaviour.

cut 4 is a MOVE, not a rewrite. The load-bearing parity for this cut is that
``MoondreamHfBackend``'s load/query bodies are byte-equal to the legacy
``MoondreamBackend`` (modulo the accepted provider name) -- so the relocated
runtime cannot silently drift from the ``from_pretrained`` path it replaced. A
fake torch/transformers keeps the behavioural tests off any real model / GPU.
"""

import ast
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agent_tools.function_tools.screen.backends.moondream import MoondreamResult
from agent_tools.function_tools.screen.config import ScreenPipelineConfig
from agent_tools.function_tools.screen.schema import ScreenToolError
from spica.local_runtime.vision import MoondreamHfBackend, MoondreamHfProvider

_REPO_ROOT = Path(__file__).resolve().parents[1]
_LEGACY = _REPO_ROOT / "agent_tools/function_tools/screen/backends/moondream.py"
_HF = _REPO_ROOT / "spica/local_runtime/vision/moondream_hf.py"


def make_config(**overrides) -> ScreenPipelineConfig:
    values = {
        "enabled": True, "provider": "moondream_hf", "model_id": "vikhyatk/moondream2",
        "revision": "2025-06-21", "device": "cuda", "dtype": "bfloat16", "max_side": 32,
        "reasoning": False, "preload": False, "ocr_enabled": True, "ocr_engine": "rapidocr",
        "capture_format": "png", "infer_timeout_sec": 30.0, "log_timing": True,
        "debug_save_images": False,
    }
    values.update(overrides)
    return ScreenPipelineConfig(**values)


def _method_dump(source: str, class_name: str, method_name: str) -> str:
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for item in node.body:
                if isinstance(item, ast.FunctionDef) and item.name == method_name:
                    return ast.dump(item)
    raise AssertionError(f"{class_name}.{method_name} not found in source")


def _func_dump(source: str, func_name: str) -> str:
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == func_name:
            return ast.dump(node)
    raise AssertionError(f"{func_name} not found in source")


class CodeEquivalenceTest(unittest.TestCase):
    """The relocated bodies == legacy bodies after normalizing the provider name."""

    def setUp(self):
        self.legacy_src = _LEGACY.read_text(encoding="utf-8")
        # Normalize the TWO intended differences so what remains is pure logic:
        #   1. the accepted provider literal (+ its echo in the error message),
        #   2. the backend class name (which shows up in load's return annotation).
        # After both rewrites the hf class is named "MoondreamBackend" and its
        # bodies must be byte-equal to legacy.
        self.hf_src = (
            _HF.read_text(encoding="utf-8")
            .replace("moondream_hf", "moondream_local")
            .replace("MoondreamHfBackend", "MoondreamBackend")
        )

    def test_load_body_equivalent(self):
        self.assertEqual(
            _method_dump(self.legacy_src, "MoondreamBackend", "load"),
            _method_dump(self.hf_src, "MoondreamBackend", "load"),
            "MoondreamHfBackend.load drifted from legacy MoondreamBackend.load",
        )

    def test_query_body_equivalent(self):
        self.assertEqual(
            _method_dump(self.legacy_src, "MoondreamBackend", "query"),
            _method_dump(self.hf_src, "MoondreamBackend", "query"),
        )

    def test_query_model_body_equivalent(self):
        self.assertEqual(
            _method_dump(self.legacy_src, "MoondreamBackend", "_query_model"),
            _method_dump(self.hf_src, "MoondreamBackend", "_query_model"),
        )

    def test_helpers_equivalent(self):
        for fn in ("_torch_dtype", "_result_to_text"):
            self.assertEqual(
                _func_dump(self.legacy_src, fn),
                _func_dump(self.hf_src, fn),
                f"helper {fn} drifted from legacy",
            )


def _fake_runtime(cuda_available=True):
    calls = {"model": [], "tokenizer": [], "queries": []}

    class FakeModel:
        def eval(self):
            calls["eval"] = True

        def query(self, image, question):
            calls["queries"].append({"image": image, "question": question})
            return {"answer": "hf screen summary"}

    class FakeAutoModelForCausalLM:
        @staticmethod
        def from_pretrained(model_id, **kwargs):
            calls["model"].append({"model_id": model_id, "kwargs": kwargs})
            return FakeModel()

    class FakeAutoTokenizer:
        @staticmethod
        def from_pretrained(model_id, **kwargs):
            calls["tokenizer"].append({"model_id": model_id, "kwargs": kwargs})
            return object()

    fake_torch = SimpleNamespace(
        bfloat16="bf16", float16="fp16", float32="fp32",
        cuda=SimpleNamespace(is_available=lambda: cuda_available),
    )
    fake_transformers = SimpleNamespace(
        AutoModelForCausalLM=FakeAutoModelForCausalLM, AutoTokenizer=FakeAutoTokenizer,
    )
    return calls, {"torch": fake_torch, "transformers": fake_transformers}


class HfBackendBehaviourTest(unittest.TestCase):
    def test_rejects_non_hf_provider(self):
        # the new backend's own provider gate accepts only moondream_hf (Decision 3).
        with self.assertRaises(ScreenToolError) as raised:
            MoondreamHfBackend.load(make_config(provider="moondream_local"))
        self.assertEqual(raised.exception.code, "SCREEN_CONFIG_INVALID")
        self.assertIn("moondream_hf", raised.exception.message)

    def test_rejects_non_cuda_device(self):
        # device gate runs before any torch import, so no fakes needed.
        with self.assertRaises(ScreenToolError) as raised:
            MoondreamHfBackend.load(make_config(device="cpu"))
        self.assertEqual(raised.exception.code, "SCREEN_CONFIG_INVALID")

    def test_loads_with_pinned_from_pretrained_kwargs(self):
        calls, fakes = _fake_runtime()
        with patch.dict(sys.modules, fakes):
            backend = MoondreamHfBackend.load(make_config())
        self.assertEqual(len(calls["model"]), 1)
        self.assertEqual(calls["model"][0]["model_id"], "vikhyatk/moondream2")
        self.assertEqual(calls["model"][0]["kwargs"]["revision"], "2025-06-21")
        self.assertIs(calls["model"][0]["kwargs"]["trust_remote_code"], True)
        self.assertEqual(calls["model"][0]["kwargs"]["device_map"], {"": "cuda"})
        self.assertEqual(calls["model"][0]["kwargs"]["torch_dtype"], "bf16")
        result = backend.query(object(), "what is this?")
        self.assertIsInstance(result, MoondreamResult)
        self.assertEqual(result.text, "hf screen summary")

    def test_cuda_unavailable_raises(self):
        _calls, fakes = _fake_runtime(cuda_available=False)
        with patch.dict(sys.modules, fakes):
            with self.assertRaises(ScreenToolError) as raised:
                MoondreamHfBackend.load(make_config())
        self.assertEqual(raised.exception.code, "SCREEN_CUDA_UNAVAILABLE")

    def test_provider_delegates_to_backend(self):
        calls, fakes = _fake_runtime()
        with patch.dict(sys.modules, fakes):
            backend = MoondreamHfProvider().load(make_config())
        self.assertIsInstance(backend, MoondreamHfBackend)
        self.assertEqual(len(calls["model"]), 1)


if __name__ == "__main__":
    unittest.main()

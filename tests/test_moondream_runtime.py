"""Moondream backend-load seam (LOCAL_RUNTIME_PLAN cut 4).

Proves the ``MoondreamModelManager`` now routes the backend load through the shared
``load_moondream_backend`` seam instead of calling ``MoondreamBackend.load``
directly -- so the host-installed provider governs which backend loads. Also pins
the zero-diff default: with NO provider installed, ``load_moondream_backend`` calls
the legacy ``MoondreamBackend.load(config)`` EXACTLY (same arg), byte-for-byte.

Mirror of ``test_ocr_path_b_unified`` for the screen-vision seam.
"""

import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from agent_tools.function_tools.screen import model_manager
from agent_tools.function_tools.screen.backends import moondream_runtime


class _SentinelBackend:
    def query(self, image, question):
        return SimpleNamespace(text="sentinel")


class _SentinelProvider:
    name = "moondream_sentinel"

    def __init__(self):
        self.calls = []

    def load(self, config):
        self.calls.append(config)
        return _SentinelBackend()


class MoondreamRuntimeSeamTest(unittest.TestCase):
    def tearDown(self):
        moondream_runtime.reset_active_moondream_provider()  # never leak global state

    def test_manager_load_is_the_shared_seam(self):
        # the manager imports the SAME seam object, so a host install governs it.
        self.assertIs(model_manager.load_moondream_backend, moondream_runtime.load_moondream_backend)

    def test_no_provider_falls_back_to_legacy_backend_load(self):
        moondream_runtime.reset_active_moondream_provider()
        config = object()
        sentinel_backend = object()
        with patch.object(moondream_runtime.MoondreamBackend, "load", return_value=sentinel_backend) as legacy:
            result = moondream_runtime.load_moondream_backend(config)
        legacy.assert_called_once_with(config)  # EXACT: same arg -> byte-identical default
        self.assertIs(result, sentinel_backend)

    def test_installed_provider_routes_and_skips_legacy(self):
        sentinel = _SentinelProvider()
        moondream_runtime.set_active_moondream_provider(sentinel)
        config = object()
        with patch.object(moondream_runtime.MoondreamBackend, "load") as legacy:
            backend = moondream_runtime.load_moondream_backend(config)
        legacy.assert_not_called()  # provider installed -> legacy untouched
        self.assertEqual(sentinel.calls, [config])
        self.assertEqual(backend.query(None, "q").text, "sentinel")

    def test_get_and_reset_active_provider(self):
        self.assertIsNone(moondream_runtime.get_active_moondream_provider())
        sentinel = _SentinelProvider()
        moondream_runtime.set_active_moondream_provider(sentinel)
        self.assertIs(moondream_runtime.get_active_moondream_provider(), sentinel)
        moondream_runtime.reset_active_moondream_provider()
        self.assertIsNone(moondream_runtime.get_active_moondream_provider())


class ManagerThroughSeamIntegrationTest(unittest.TestCase):
    """End-to-end: manager(provider=moondream_hf) + installed hf provider + fake
    runtime -> the real MoondreamHfBackend.load runs (fake from_pretrained) and the
    manager returns its query text. Ties manager -> seam -> hf backend together."""

    def tearDown(self):
        moondream_runtime.reset_active_moondream_provider()
        model_manager.clear_moondream_manager()

    def _config(self, **over):
        from agent_tools.function_tools.screen.config import ScreenPipelineConfig

        values = {
            "enabled": True, "provider": "moondream_hf", "model_id": "vikhyatk/moondream2",
            "revision": "2025-06-21", "device": "cuda", "dtype": "bfloat16", "max_side": 32,
            "reasoning": False, "preload": False, "ocr_enabled": True, "ocr_engine": "rapidocr",
            "capture_format": "png", "infer_timeout_sec": 30.0, "log_timing": True,
            "debug_save_images": False,
        }
        values.update(over)
        return ScreenPipelineConfig(**values)

    def _fakes(self):
        class FakeModel:
            def eval(self):
                pass

            def query(self, image, question):
                return {"answer": "hf seam summary"}

        fake_torch = SimpleNamespace(
            bfloat16="bf16", float16="fp16", float32="fp32",
            cuda=SimpleNamespace(is_available=lambda: True),
        )
        fake_transformers = SimpleNamespace(
            AutoModelForCausalLM=SimpleNamespace(from_pretrained=staticmethod(lambda *a, **k: FakeModel())),
            AutoTokenizer=SimpleNamespace(from_pretrained=staticmethod(lambda *a, **k: object())),
        )
        return {"torch": fake_torch, "transformers": fake_transformers}

    def test_manager_hf_provider_loads_isolated_backend(self):
        from PIL import Image

        from spica.local_runtime.vision import MoondreamHfBackend, MoondreamHfProvider

        model_manager.clear_moondream_manager()
        moondream_runtime.set_active_moondream_provider(MoondreamHfProvider())
        with patch.dict(sys.modules, self._fakes()):
            manager = model_manager.get_moondream_manager(self._config())
            text = manager.query(Image.new("RGB", (16, 16), (10, 20, 30)), "what is this?")
        self.assertEqual(text, "hf seam summary")
        self.assertIsInstance(manager._backend, MoondreamHfBackend)  # routed to the isolated runtime


if __name__ == "__main__":
    unittest.main()

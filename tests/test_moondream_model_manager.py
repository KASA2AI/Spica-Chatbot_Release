import sys
import threading
from types import SimpleNamespace

import pytest
from PIL import Image

from agent_tools.function_tools.screen.config import ScreenPipelineConfig
from agent_tools.function_tools.screen.model_manager import (
    DEFAULT_SCREEN_PROMPT,
    MoondreamModelManager,
    clear_moondream_manager,
    get_moondream_manager,
)
from agent_tools.function_tools.screen.schema import ScreenToolError


def make_config(**overrides) -> ScreenPipelineConfig:
    values = {
        "enabled": True,
        "provider": "moondream_local",
        "model_id": "vikhyatk/moondream2",
        "revision": "2025-06-21",
        "device": "cuda",
        "dtype": "bfloat16",
        "max_side": 32,
        "reasoning": False,
        "preload": False,
        "ocr_enabled": True,
        "ocr_engine": "rapidocr",
        "capture_format": "png",
        "infer_timeout_sec": 30.0,
        "log_timing": True,
        "debug_save_images": False,
    }
    values.update(overrides)
    return ScreenPipelineConfig(**values)


def install_fake_runtime(monkeypatch, *, cuda_available=True, block_load=None):
    calls = {"model": [], "tokenizer": [], "queries": []}

    class FakeModel:
        def eval(self):
            calls["eval"] = True

        def query(self, image, question):
            calls["queries"].append({"image": image, "question": question})
            return {"answer": "local screen summary"}

    class FakeAutoModelForCausalLM:
        @staticmethod
        def from_pretrained(model_id, **kwargs):
            calls["model"].append({"model_id": model_id, "kwargs": kwargs})
            if block_load is not None:
                block_load["started"].set()
                block_load["release"].wait(timeout=5)
            return FakeModel()

    class FakeAutoTokenizer:
        @staticmethod
        def from_pretrained(model_id, **kwargs):
            calls["tokenizer"].append({"model_id": model_id, "kwargs": kwargs})
            return object()

    fake_torch = SimpleNamespace(
        bfloat16="bf16",
        float16="fp16",
        float32="fp32",
        cuda=SimpleNamespace(is_available=lambda: cuda_available),
    )
    fake_transformers = SimpleNamespace(
        AutoModelForCausalLM=FakeAutoModelForCausalLM,
        AutoTokenizer=FakeAutoTokenizer,
    )
    monkeypatch.setitem(sys.modules, "torch", fake_torch)
    monkeypatch.setitem(sys.modules, "transformers", fake_transformers)
    return calls


def test_manager_init_does_not_load_model(monkeypatch):
    calls = install_fake_runtime(monkeypatch)
    manager = MoondreamModelManager(make_config())

    assert manager.get_status() == "unloaded"
    assert calls["model"] == []
    assert calls["tokenizer"] == []


def test_first_query_loads_once_and_prepares_pil_image(monkeypatch):
    calls = install_fake_runtime(monkeypatch)
    manager = MoondreamModelManager(make_config(max_side=32))
    source = Image.new("L", (128, 64), 255)

    result = manager.query(source, "What is visible?")
    second = manager.query(source, "Describe again.")

    assert result == "local screen summary"
    assert second == "local screen summary"
    assert manager.is_ready() is True
    assert manager.get_status() == "ready"
    assert len(calls["model"]) == 1
    assert calls["model"][0]["model_id"] == "vikhyatk/moondream2"
    assert calls["model"][0]["kwargs"]["revision"] == "2025-06-21"
    assert calls["model"][0]["kwargs"]["trust_remote_code"] is True
    assert calls["model"][0]["kwargs"]["device_map"] == {"": "cuda"}
    assert calls["model"][0]["kwargs"]["torch_dtype"] == "bf16"
    assert len(calls["queries"]) == 2
    prepared = calls["queries"][0]["image"]
    assert prepared.mode == "RGB"
    assert prepared.size == (32, 16)
    assert DEFAULT_SCREEN_PROMPT in calls["queries"][0]["question"]
    assert "What is visible?" in calls["queries"][0]["question"]


def test_preload_async_is_non_blocking_and_loads_once(monkeypatch):
    block_load = {"started": threading.Event(), "release": threading.Event()}
    calls = install_fake_runtime(monkeypatch, block_load=block_load)
    manager = MoondreamModelManager(make_config())

    future = manager.preload_async()
    assert block_load["started"].wait(timeout=1)
    assert manager.get_status() == "loading"
    assert not future.done()

    block_load["release"].set()
    assert future.result(timeout=2) is manager
    assert manager.get_status() == "ready"
    assert len(calls["model"]) == 1


def test_cuda_unavailable_is_clear_error_without_fallback(monkeypatch):
    install_fake_runtime(monkeypatch, cuda_available=False)
    manager = MoondreamModelManager(make_config())

    with pytest.raises(ScreenToolError) as raised:
        manager.load()

    assert raised.value.code == "SCREEN_CUDA_UNAVAILABLE"
    assert "CUDA 不可用" in raised.value.message
    assert manager.get_status() == "error"
    details = manager.get_status_details()
    assert details["state"] == "error"
    assert details["error_type"] == "ScreenToolError"
    assert "CUDA 不可用" in details["error_message"]


def test_global_manager_is_singleton_per_config_signature(monkeypatch):
    clear_moondream_manager()
    install_fake_runtime(monkeypatch)
    config = make_config(max_side=32)
    same_config = make_config(max_side=32)
    different_config = make_config(max_side=64)

    first = get_moondream_manager(config)
    second = get_moondream_manager(same_config)
    third = get_moondream_manager(different_config)

    assert first is second
    assert third is not first
    clear_moondream_manager()


# cut 4: the manager seam serves BOTH the legacy backend and the isolated
# moondream_hf provider, so _validate_config accepts either provider name (the
# narrow per-backend gate still pins which backend each value routes to).
def test_validate_config_allows_moondream_local(monkeypatch):
    install_fake_runtime(monkeypatch)
    MoondreamModelManager(make_config(provider="moondream_local"))._validate_config()  # no raise


def test_validate_config_allows_moondream_hf(monkeypatch):
    install_fake_runtime(monkeypatch)
    MoondreamModelManager(make_config(provider="moondream_hf"))._validate_config()  # no raise


def test_validate_config_rejects_unknown_provider(monkeypatch):
    install_fake_runtime(monkeypatch)
    manager = MoondreamModelManager(make_config(provider="remote_api"))
    with pytest.raises(ScreenToolError) as raised:
        manager._validate_config()
    assert raised.value.code == "SCREEN_CONFIG_INVALID"
    assert "moondream_hf" in raised.value.message  # message names both accepted providers

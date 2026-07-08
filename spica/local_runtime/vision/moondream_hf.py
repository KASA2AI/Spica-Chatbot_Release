"""Moondream HF provider -- the isolated screen-vision inference runtime.

LOCAL_RUNTIME_PLAN cut 4. This is the EXISTING ``MoondreamBackend`` load+query
logic (``agent_tools...backends.moondream``) relocated into the local-runtime
layer and renamed ``MoondreamHfBackend``. The ``transformers``
``AutoModelForCausalLM.from_pretrained(trust_remote_code=True)`` load path is
moved VERBATIM -- the only intentional difference from the legacy backend is the
provider name it accepts (``moondream_hf`` instead of ``moondream_local``). A
``code-equivalence`` test (``tests/test_moondream_hf_backend.py``) pins the two
load/query bodies equal so this copy cannot silently drift.

``spica -> agent_tools`` is the allowed layer direction (the OCR / RVC runtimes
already rely on it). This module reuses ``ScreenPipelineConfig`` /
``ScreenToolError`` / ``MoondreamResult`` from ``agent_tools`` rather than forking
new types, so the manager seam consumes either backend interchangeably.

ENV-FREE (CLAUDE.md #4 / §3.3): reads no environment; the device / dtype / model
id / revision all arrive on the injected ``ScreenPipelineConfig``.

WIRING: installed by the host via ``build_moondream_provider`` ->
``set_active_moondream_provider`` only when ``screen.provider == "moondream_hf"``.
Default ``moondream_local`` does NOT install this -- the manager seam then calls
the legacy ``MoondreamBackend.load`` byte-for-byte (zero-diff default, P0).
"""

from __future__ import annotations

from typing import Any

from agent_tools.function_tools.screen.backends.moondream import MoondreamResult
from agent_tools.function_tools.screen.config import ScreenPipelineConfig
from agent_tools.function_tools.screen.schema import ScreenToolError


class MoondreamHfBackend:
    def __init__(self, *, model: Any, tokenizer: Any, config: ScreenPipelineConfig) -> None:
        self.model = model
        self.tokenizer = tokenizer
        self.config = config

    @classmethod
    def load(cls, config: ScreenPipelineConfig) -> "MoondreamHfBackend":
        if config.provider != "moondream_hf":
            raise ScreenToolError(
                "SCREEN_CONFIG_INVALID",
                f"screen provider 必须是 moondream_hf，当前是 {config.provider!r}。",
            )
        if config.device != "cuda":
            raise ScreenToolError(
                "SCREEN_CONFIG_INVALID",
                f"Moondream 本地 screen pipeline 只允许 device='cuda'，当前是 {config.device!r}。",
            )
        try:
            import torch  # type: ignore[import-not-found]
        except ImportError as exc:
            raise ScreenToolError(
                "SCREEN_ANALYSIS_DEPENDENCY_MISSING",
                "缺少 torch，无法运行本地 Moondream。请在 gptsovits 环境安装 CUDA 版 torch。",
            ) from exc
        if not torch.cuda.is_available():
            raise ScreenToolError(
                "SCREEN_CUDA_UNAVAILABLE",
                "CUDA 不可用，无法在本地运行 Moondream；不会 fallback 到远端视觉 API。",
            )
        try:
            from transformers import AutoModelForCausalLM, AutoTokenizer  # type: ignore[import-not-found]
        except ImportError as exc:
            raise ScreenToolError(
                "SCREEN_ANALYSIS_DEPENDENCY_MISSING",
                "缺少 transformers，无法加载本地 Moondream。请安装 transformers。",
            ) from exc

        load_kwargs: dict[str, Any] = {
            "revision": config.revision,
            "trust_remote_code": True,
            "device_map": {"": "cuda"},
        }
        dtype = _torch_dtype(torch, config.dtype)
        if dtype is not None:
            load_kwargs["torch_dtype"] = dtype

        try:
            model = AutoModelForCausalLM.from_pretrained(config.model_id, **load_kwargs)
            tokenizer = AutoTokenizer.from_pretrained(
                config.model_id,
                revision=config.revision,
                trust_remote_code=True,
            )
            if hasattr(model, "eval"):
                model.eval()
            return cls(model=model, tokenizer=tokenizer, config=config)
        except ScreenToolError:
            raise
        except Exception as exc:
            raise ScreenToolError(
                "SCREEN_MOONDREAM_LOAD_FAILED",
                f"Moondream 加载失败：{type(exc).__name__}: {exc}",
            ) from exc

    def query(self, image: Any, question: str) -> MoondreamResult:
        try:
            raw = self._query_model(image, question)
            text = _result_to_text(raw)
            if not text:
                raise ScreenToolError("SCREEN_MOONDREAM_EMPTY_RESULT", "Moondream 推理结果为空。")
            return MoondreamResult(text=text, raw=raw)
        except ScreenToolError:
            raise
        except Exception as exc:
            raise ScreenToolError(
                "SCREEN_MOONDREAM_INFERENCE_FAILED",
                f"Moondream 推理失败：{type(exc).__name__}: {exc}",
            ) from exc

    def _query_model(self, image: Any, question: str) -> Any:
        if hasattr(self.model, "query"):
            return self.model.query(image, question)
        if hasattr(self.model, "answer_question"):
            if hasattr(self.model, "encode_image"):
                encoded = self.model.encode_image(image)
                return self.model.answer_question(encoded, question, self.tokenizer)
            return self.model.answer_question(image, question, self.tokenizer)
        if hasattr(self.model, "caption"):
            return self.model.caption(image)
        raise ScreenToolError(
            "SCREEN_MOONDREAM_API_UNSUPPORTED",
            "当前 Moondream 模型对象不支持 query/answer_question/caption 接口。",
        )


class MoondreamHfProvider:
    """Host-installed provider seam: ``.load(config) -> MoondreamHfBackend``.

    The factory returns ONE of these (only when ``screen.provider ==
    "moondream_hf"``); the host installs it via ``set_active_moondream_provider``
    and the manager seam (``load_moondream_backend``) calls ``.load(config)``.
    Mirrors the OCR ``OCRPort`` install-hook shape -- a small object, not a class,
    so it is installable / resettable like the OCR provider."""

    name = "moondream_hf"

    def load(self, config: ScreenPipelineConfig) -> MoondreamHfBackend:
        return MoondreamHfBackend.load(config)


def _torch_dtype(torch: Any, dtype_name: str) -> Any:
    normalized = (dtype_name or "auto").strip().lower()
    if normalized == "auto":
        return "auto"
    mapping = {
        "bfloat16": getattr(torch, "bfloat16", None),
        "float16": getattr(torch, "float16", None),
        "float32": getattr(torch, "float32", None),
    }
    return mapping.get(normalized)


def _result_to_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        for key in ("answer", "caption", "text", "summary"):
            if key in value:
                return str(value.get(key) or "").strip()
        return str(value).strip()
    if hasattr(value, "answer"):
        return str(getattr(value, "answer") or "").strip()
    return str(value or "").strip()

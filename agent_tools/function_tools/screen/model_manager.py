from __future__ import annotations

import logging
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from threading import RLock
from typing import Any

from agent_tools.function_tools.screen.backends.moondream import MoondreamBackend, MoondreamResult
from agent_tools.function_tools.screen.backends.moondream_runtime import load_moondream_backend
from agent_tools.function_tools.screen.config import ScreenPipelineConfig, load_screen_config
from agent_tools.function_tools.screen.schema import ScreenToolError


DEFAULT_SCREEN_PROMPT = (
    "Describe the visible computer screen. Focus on:\n"
    "- active application or window\n"
    "- visible UI elements\n"
    "- dialogs, errors, warnings\n"
    "- buttons or input fields\n"
    "- browser or editor context\n"
    "- actionable state\n"
    "Be concise and factual. Do not invent hidden content."
)

STATUS_UNLOADED = "unloaded"
STATUS_LOADING = "loading"
STATUS_READY = "ready"
STATUS_ERROR = "error"

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class _ManagerSignature:
    provider: str
    model_id: str
    revision: str
    device: str
    dtype: str
    max_side: int


class MoondreamModelManager:
    """Lazy singleton-friendly manager for local Moondream inference."""

    def __init__(self, config: ScreenPipelineConfig | None = None) -> None:
        self._config = config or load_screen_config()
        self._backend: MoondreamBackend | None = None
        self._status = STATUS_UNLOADED
        self._error_type: str | None = None
        self._error_message: str | None = None
        self._loaded_at: str | None = None
        self._loading_started_at: str | None = None
        self._preload_future: Future[Any] | None = None
        self._executor: ThreadPoolExecutor | None = None
        self._state_lock = RLock()
        self._load_lock = RLock()
        self._infer_lock = RLock()
        # retired flag (2026-07 review): a manager cleared via
        # clear_moondream_manager must never re-acquire the model -- an
        # in-flight load could otherwise repopulate _backend after reset.
        self._closed = False
        self._generation = 0  # reset 递增; 迟到的旧代 load 结果一律丢弃(第十轮)

    @property
    def config(self) -> ScreenPipelineConfig:
        return self._config

    def preload_async(self) -> Future[Any]:
        """Start model loading in the background without taking a screenshot."""

        with self._state_lock:
            if self._closed:
                # retired manager: refuse up-front instead of creating an
                # executor / flipping status to loading before the future fails.
                failed: Future[Any] = Future()
                failed.set_exception(ScreenToolError(
                    "SCREEN_MOONDREAM_CLEARED",
                    "Moondream manager 已被清除（screen.enabled=false 重装配）——"
                    "需经 get_moondream_manager 重新获取。",
                ))
                return failed
            if self._backend is not None and self._status == STATUS_READY:
                future: Future[Any] = Future()
                future.set_result(self)
                return future
            if self._preload_future is not None and not self._preload_future.done():
                return self._preload_future
            if self._executor is None:
                self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="spica-moondream")
            self._mark_loading_locked()
            self._preload_future = self._executor.submit(self.load)
            return self._preload_future

    def load(self) -> "MoondreamModelManager":
        """Load Moondream once. CUDA is mandatory; there is no CPU or remote fallback."""

        with self._load_lock:
            with self._state_lock:
                generation = self._generation
                if self._closed:
                    raise ScreenToolError(
                        "SCREEN_MOONDREAM_CLEARED",
                        "Moondream manager 已被清除（screen.enabled=false 重装配）——"
                        "需经 get_moondream_manager 重新获取。",
                    )
                if self._backend is not None and self._status == STATUS_READY:
                    return self
                self._mark_loading_locked()

            try:
                self._validate_config()
                self._assert_cuda_available()
                # LOCAL_RUNTIME_PLAN cut 4: route the backend load through the
                # provider seam. No provider installed (default moondream_local)
                # -> calls MoondreamBackend.load(self._config) EXACTLY (zero-diff);
                # moondream_hf installed -> the isolated MoondreamHfBackend.
                backend = load_moondream_backend(self._config)
            except ScreenToolError as exc:
                self._mark_error(exc, stage="load")
                raise
            except Exception as exc:
                wrapped = ScreenToolError(
                    "SCREEN_MOONDREAM_LOAD_FAILED",
                    f"Moondream 加载失败：{type(exc).__name__}: {exc}",
                )
                self._mark_error(wrapped, stage="load", original=exc)
                raise wrapped from exc

            with self._state_lock:
                if self._closed or self._generation != generation:
                    # 慢 load 期间 reset(含 close=False)已返回(第九/十轮): 写回
                    # 会复活已卸载的 manager -- 按代号丢弃本次结果。del 切断
                    # 异常 traceback 对 backend/显存的保活(第十轮 P2)。
                    del backend
                    raise ScreenToolError(
                        "SCREEN_MOONDREAM_CLEARED",
                        "Moondream manager 在加载期间被 reset/清除——丢弃本次加载结果。",
                    )
                self._backend = backend
                self._status = STATUS_READY
                self._error_type = None
                self._error_message = None
                self._loaded_at = _utc_now()
            return self

    def is_ready(self) -> bool:
        with self._state_lock:
            return self._backend is not None and self._status == STATUS_READY

    def get_status(self) -> str:
        with self._state_lock:
            return self._status

    def get_status_details(self) -> dict[str, Any]:
        with self._state_lock:
            return {
                "closed": self._closed,
                "state": self._status,
                "model_id": self._config.model_id,
                "revision": self._config.revision,
                "device": self._config.device,
                "dtype": self._config.dtype,
                "max_side": self._config.max_side,
                "error_type": self._error_type,
                "error_message": self._error_message,
                "loading_started_at": self._loading_started_at,
                "loaded_at": self._loaded_at,
            }

    def query(self, image: Any, question: str = "", reasoning: bool = False) -> str:
        """Run a single local screen query on a PIL image."""

        backend = self.load()._require_backend()
        prepared = self._prepare_image(image)
        prompt = self._build_prompt(question, reasoning)

        with self._infer_lock:
            # re-validate INSIDE the lock (2026-07 review): this thread may have
            # grabbed ``backend`` before the lock and paused while reset(close)
            # completed -- a retired/replaced backend must never infer again.
            with self._state_lock:
                if self._closed or self._backend is not backend:
                    raise ScreenToolError(
                        "SCREEN_MOONDREAM_CLEARED",
                        "Moondream manager 已被清除——放弃这次已排队的推理。",
                    )
            try:
                return backend.query(prepared, prompt).text
            except ScreenToolError as exc:
                self._mark_error(exc, stage="inference")
                raise
            except Exception as exc:
                wrapped = ScreenToolError(
                    "SCREEN_MOONDREAM_INFERENCE_FAILED",
                    f"Moondream 推理失败：{type(exc).__name__}: {exc}",
                )
                self._mark_error(wrapped, stage="inference", original=exc)
                raise wrapped from exc

    def reset(self, *, close: bool = False) -> None:
        """Unload the backend. Takes ``_load_lock`` FIRST so an in-flight
        ``load()`` finishes and its result is then discarded here -- without
        that ordering the old load repopulates ``_backend`` after the reset
        (2026-07 review race). ``_infer_lock`` comes next so an in-flight
        inference finishes before the backend is dropped (and a queued
        inference re-checks ``_closed`` under the same lock and refuses).
        ``close=True`` (clear_moondream_manager) additionally retires this
        instance: any later ``load()`` on a stale reference raises instead of
        silently re-acquiring the model/VRAM. Lock order everywhere is
        load -> infer -> state, so no inversion."""
        if close:
            # 第七轮 review P2 钉序: closing 必须在开始等锁**之前**就对排队推理
            # 可见 -- 已排队未进 backend 的 query 在自己的锁内复核时立即拒绝,
            # 不依赖 RLock 获取顺序; 已开始的推理(持 infer 锁)完整收尾。
            with self._state_lock:
                self._closed = True
        # 有界等待(第八轮 review P2): 挂死的 load/推理不得让 reset 无限阻塞
        # (enabled->disabled 重装配挂死)。超时则强制清态并如实告警 -- 挂死线程
        # 本就不可回收, closed 标志已挡住任何新的/排队的使用。
        import math as _math

        try:
            raw_timeout = float(getattr(self._config, "infer_timeout_sec", 30) or 30)
        except (TypeError, ValueError):
            raw_timeout = 30.0
        if not _math.isfinite(raw_timeout) or raw_timeout <= 0:
            raw_timeout = 30.0  # inf/1e308 会让 RLock.acquire OverflowError(第九轮 P2)
        wait_s = max(1.0, min(300.0, raw_timeout))
        got_load = self._load_lock.acquire(timeout=wait_s)
        try:
            got_infer = self._infer_lock.acquire(timeout=wait_s)
            try:
                with self._state_lock:
                    self._generation += 1  # 任何 reset 都换代: 迟到 load 不得写回
                    self._backend = None
                    self._status = STATUS_UNLOADED
                    self._error_type = None
                    self._error_message = None
                    self._loaded_at = None
                    self._loading_started_at = None
                    self._preload_future = None
                    if close:
                        self._closed = True
            finally:
                if got_infer:
                    self._infer_lock.release()
        finally:
            if got_load:
                self._load_lock.release()
        if not (got_load and got_infer):
            _LOGGER.warning(
                "Moondream reset: 等待进行中的 load/推理超时(%.1fs)——已强制清态; "
                "挂死线程无法回收, closed=%s 已挡住后续使用", wait_s, self._closed,
            )

    def _require_backend(self) -> MoondreamBackend:
        with self._state_lock:
            if self._backend is None:
                raise ScreenToolError("SCREEN_MOONDREAM_NOT_READY", "Moondream backend 未完成加载。")
            return self._backend

    def _validate_config(self) -> None:
        # LOCAL_RUNTIME_PLAN cut 4: the manager seam serves BOTH the legacy
        # moondream_local backend AND the isolated moondream_hf provider, so it
        # accepts either provider name. The narrow per-backend provider check
        # (legacy MoondreamBackend.load / MoondreamHfBackend.load) still pins which
        # backend each value routes to -- this only widens the manager gate.
        if self._config.provider not in ("moondream_local", "moondream_hf"):
            raise ScreenToolError(
                "SCREEN_CONFIG_INVALID",
                f"screen provider 必须是 moondream_local 或 moondream_hf，当前是 {self._config.provider!r}。",
            )
        if self._config.device != "cuda":
            raise ScreenToolError(
                "SCREEN_CONFIG_INVALID",
                f"Moondream 本地 screen pipeline 只允许 device='cuda'，当前是 {self._config.device!r}。",
            )

    def _assert_cuda_available(self) -> None:
        try:
            import torch  # type: ignore[import-not-found]
        except ImportError as exc:
            raise ScreenToolError(
                "SCREEN_ANALYSIS_DEPENDENCY_MISSING",
                "缺少 torch，无法运行本地 Moondream。请在 gptsovits 环境安装 CUDA 版 torch。",
            ) from exc

        cuda = getattr(torch, "cuda", None)
        is_available = getattr(cuda, "is_available", None)
        if not callable(is_available) or not bool(is_available()):
            raise ScreenToolError(
                "SCREEN_CUDA_UNAVAILABLE",
                "CUDA 不可用，无法在本地运行 Moondream；不会 fallback 到远端视觉 API。",
            )

    def _prepare_image(self, image: Any) -> Any:
        try:
            from PIL import Image  # type: ignore[import-not-found]
        except ImportError as exc:
            raise ScreenToolError(
                "SCREEN_CAPTURE_DEPENDENCY_MISSING",
                "缺少图片处理依赖 Pillow，请安装 Pillow。",
            ) from exc

        if not isinstance(image, Image.Image):
            raise ScreenToolError("SCREEN_ANALYSIS_FAILED", "Moondream screen query 要求 PIL.Image.Image。")

        processed = image.convert("RGB") if image.mode != "RGB" else image
        longest = max(processed.width, processed.height)
        max_side = max(1, int(self._config.max_side))
        if longest <= max_side:
            return processed

        scale = max_side / float(longest)
        size = (max(1, round(processed.width * scale)), max(1, round(processed.height * scale)))
        resampling = getattr(Image, "Resampling", None)
        filter_value = resampling.LANCZOS if resampling is not None else getattr(Image, "LANCZOS", 1)
        return processed.resize(size, filter_value)

    def _build_prompt(self, question: str, reasoning: bool) -> str:
        question = (question or "").strip()
        prompt = DEFAULT_SCREEN_PROMPT if not question else f"{DEFAULT_SCREEN_PROMPT}\n\n{question}"
        if reasoning:
            prompt += "\nBriefly reason only from visible screen evidence."
        return prompt

    def _mark_loading_locked(self) -> None:
        self._status = STATUS_LOADING
        self._error_type = None
        self._error_message = None
        self._loading_started_at = _utc_now()

    def _mark_error(self, exc: BaseException, *, stage: str, original: BaseException | None = None) -> None:
        error = original or exc
        with self._state_lock:
            if not self._closed:
                # a retired manager's state stays UNLOADED -- a late failure
                # from a pre-close code path must not rewrite it to error.
                self._status = STATUS_ERROR
                self._error_type = type(error).__name__
                self._error_message = str(error)
        _LOGGER.error(
            "Moondream %s failed: %s: %s",
            stage,
            type(error).__name__,
            error,
            exc_info=True,
        )


class _MoondreamBackendAdapter:
    """Compatibility adapter for older callers expecting .query(...).text."""

    def __init__(self, manager: MoondreamModelManager) -> None:
        self.manager = manager

    def query(self, image: Any, question: str) -> MoondreamResult:
        return MoondreamResult(
            text=self.manager.query(image, question, reasoning=bool(self.manager.config.reasoning)),
            raw=None,
        )


_MANAGER_LOCK = RLock()
_MANAGER: MoondreamModelManager | None = None
_SIGNATURE: _ManagerSignature | None = None


def get_moondream_manager(config: ScreenPipelineConfig | None = None) -> MoondreamModelManager:
    global _MANAGER, _SIGNATURE
    resolved = config or load_screen_config()
    signature = _signature(resolved)
    with _MANAGER_LOCK:
        if _MANAGER is None or _SIGNATURE != signature:
            _MANAGER = MoondreamModelManager(resolved)
            _SIGNATURE = signature
        return _MANAGER


def get_moondream_backend(config: ScreenPipelineConfig) -> _MoondreamBackendAdapter:
    return _MoondreamBackendAdapter(get_moondream_manager(config))


def clear_moondream_manager() -> None:
    global _MANAGER, _SIGNATURE
    with _MANAGER_LOCK:
        if _MANAGER is not None:
            # close=True: waits out an in-flight load (load lock), discards its
            # backend and retires the instance -- stale references can never
            # bring the model (and its VRAM) back.
            _MANAGER.reset(close=True)
        _MANAGER = None
        _SIGNATURE = None


def clear_moondream_backend() -> None:
    clear_moondream_manager()


def preload_async(config: ScreenPipelineConfig | None = None) -> Future[Any]:
    return get_moondream_manager(config).preload_async()


def load(config: ScreenPipelineConfig | None = None) -> MoondreamModelManager:
    return get_moondream_manager(config).load()


def is_ready(config: ScreenPipelineConfig | None = None) -> bool:
    return get_moondream_manager(config).is_ready()


def get_status(config: ScreenPipelineConfig | None = None) -> str:
    return get_moondream_manager(config).get_status()


def query(
    image: Any,
    question: str = "",
    *,
    config: ScreenPipelineConfig | None = None,
    reasoning: bool = False,
) -> str:
    return get_moondream_manager(config).query(image, question, reasoning=reasoning)


def _signature(config: ScreenPipelineConfig) -> _ManagerSignature:
    return _ManagerSignature(
        provider=config.provider,
        model_id=config.model_id,
        revision=config.revision,
        device=config.device,
        dtype=config.dtype,
        max_side=config.max_side,
    )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()

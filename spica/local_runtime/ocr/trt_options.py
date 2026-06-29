"""ONNX Runtime TensorRT EP options + fallback orchestration (LOCAL_RUNTIME_PLAN cut 2).

PURE core: NO onnxruntime / rapidocr imports, so the CI tests of this module need
no GPU / TRT / model (§6.5). The real ORT wiring lives in ``rapidocr_trt_runtime``
and is a thin shell over these pure helpers.

We use ORT's TensorRT *Execution Provider* (engine build + cache handled by ORT) --
NOT a hand-written TensorRT builder. fp32 is the cut-2 default (verify the
integration mechanism with one variable; fp16 is a configurable step-2 follow-up).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

TRT_EP = "TensorrtExecutionProvider"
CUDA_EP = "CUDAExecutionProvider"
CPU_EP = "CPUExecutionProvider"


def build_trt_provider_options(
    *,
    fp16: bool,
    engine_cache_dir: str,
    timing_cache: bool,
    profiles: dict[str, str] | None = None,
    device_id: int = 0,
) -> dict[str, Any]:
    """ORT TensorRT EP provider options.

    Engine cache is always on (so a built engine survives restarts -- session init
    drops from minutes to seconds). ``fp16`` is explicit (cut-2 default False =
    fp32). ``profiles`` (one session's min/opt/max shape strings, ORT format
    ``"name:1x3xHxW"``) is emitted ONLY when supplied -- deferred until the
    real-machine shape probe says explicit profiles are needed (D3); until then ORT
    builds a per-shape engine into the cache."""
    opts: dict[str, Any] = {
        "device_id": device_id,
        "trt_fp16_enable": bool(fp16),
        "trt_engine_cache_enable": True,
        "trt_engine_cache_path": engine_cache_dir,
        "trt_timing_cache_enable": bool(timing_cache),
        "trt_timing_cache_path": engine_cache_dir,
    }
    if profiles:
        # Only the keys present are emitted; ORT requires min+opt+max together when
        # any is set, but validation of completeness is the caller's job (the runtime
        # builds all three from the probe).
        if profiles.get("min"):
            opts["trt_profile_min_shapes"] = profiles["min"]
        if profiles.get("opt"):
            opts["trt_profile_opt_shapes"] = profiles["opt"]
        if profiles.get("max"):
            opts["trt_profile_max_shapes"] = profiles["max"]
    return opts


def default_cuda_options(device_id: int = 0) -> dict[str, Any]:
    """CUDA EP options mirroring rapidocr_onnxruntime's own defaults (the fallback EP)."""
    return {
        "device_id": device_id,
        "arena_extend_strategy": "kNextPowerOfTwo",
        "cudnn_conv_algo_search": "EXHAUSTIVE",
        "do_copy_in_default_stream": True,
    }


def default_cpu_options() -> dict[str, Any]:
    return {"arena_extend_strategy": "kSameAsRequested"}


def build_ep_list(
    trt_options: dict[str, Any],
    *,
    cuda_options: dict[str, Any] | None = None,
    cpu_options: dict[str, Any] | None = None,
    device_id: int = 0,
) -> list[tuple[str, dict[str, Any]]]:
    """Provider list in priority order: TRT -> CUDA (fallback) -> CPU (last resort).

    ORT tries each in order; if TRT EP can't initialize, it shifts to CUDA. This
    list is what a TRT-capable ``OrtInferSession`` hands to ``InferenceSession``."""
    return [
        (TRT_EP, dict(trt_options)),
        (CUDA_EP, dict(cuda_options if cuda_options is not None else default_cuda_options(device_id))),
        (CPU_EP, dict(cpu_options if cpu_options is not None else default_cpu_options())),
    ]


# ---- per-stage EP routing (cut 2.1) -------------------------------------------
# Diagnosis: det/rec build clean TRT engines; cls (ch_ppocr_mobile_v2.0_cls) fails
# TRT engine build even on a single static shape ("different constant values /
# contradictory kMIN/kMAX" -- a cls GRAPH issue), so cls is routed to CUDA. Only a
# POSITIVELY identified det/rec stage gets TRT; anything unrecognized stays on CUDA
# (conservative -- a renamed rapidocr model never silently lands on TRT).

# The stage each provider list runs on when TRT genuinely loaded (the verification
# target). cls is intentionally CUDA, NOT a failure.
EXPECTED_STAGE_PROVIDER = {"det": "trt", "rec": "trt", "cls": "cuda"}


def classify_stage(model_path: str) -> str:
    """Map a RapidOCR model path to ``"det" | "cls" | "rec" | "unknown"`` by file name.

    Basename-only (a parent dir must never trigger a match), lowercased. Unknown is
    the safe default -> CUDA (never TRT) for anything we don't positively recognize."""
    name = str(model_path or "").replace("\\", "/").rsplit("/", 1)[-1].lower()
    if "det" in name:
        return "det"
    if "cls" in name:
        return "cls"
    if "rec" in name:
        return "rec"
    return "unknown"


def ep_list_for_stage(
    model_path: str,
    *,
    trt_options: dict[str, Any],
    cuda_options: dict[str, Any],
    cpu_options: dict[str, Any],
) -> list[tuple[str, dict[str, Any]]]:
    """Per-stage provider list (cut 2.1): det/rec -> [TRT, CUDA, CPU]; cls/unknown ->
    [CUDA, CPU] (no TRT). The CPU floor is ORT's implicit last resort, not a success
    path (the verification flags any stage that lands on CPU)."""
    cuda = (CUDA_EP, dict(cuda_options))
    cpu = (CPU_EP, dict(cpu_options))
    if classify_stage(model_path) in ("det", "rec"):
        return [(TRT_EP, dict(trt_options)), cuda, cpu]
    return [cuda, cpu]


def classify_load_status(stage_providers: dict[str, str]) -> tuple[bool, str | None]:
    """Verify TRT genuinely loaded -- NOT just that it was requested (CLAUDE.md: no
    silent CUDA masquerading as TRT success). ``stage_providers`` is the ACTUAL EP
    each session runs on (``{"det": "trt"|"cuda"|..., "rec": ..., "cls": ...}``).

    OK iff det AND rec are on ``trt`` (cls=cuda is EXPECTED, never a failure). When a
    TRT-expected stage fell back, returns a clear diagnostic distinguishing an
    env/load problem from the expected cls routing."""
    bad = [
        f"{stage}={stage_providers.get(stage)!r}"
        for stage in ("det", "rec")
        if stage_providers.get(stage) != "trt"
    ]
    if bad:
        return False, (
            f"TensorRT EP not active for {', '.join(bad)} (expected 'trt') -- TRT EP failed to "
            f"load or build and fell back. Check libnvinfer.so.10 / tensorrt_libs are installed "
            f"and preloaded (or on LD_LIBRARY_PATH). NOTE: cls=cuda is expected (cls is routed to "
            f"CUDA by design)."
        )
    return True, None


@dataclass
class EngineBuildResult:
    engine: Any
    used: str  # "trt" | "cuda"
    error: BaseException | None = None


def build_engine_with_fallback(
    primary_factory: Callable[[], Any],
    fallback_factory: Callable[[], Any],
    *,
    warmup: Callable[[Any], None] | None = None,
    on_fallback: Callable[[BaseException], None] | None = None,
) -> EngineBuildResult:
    """Build the TRT engine, falling back to CUDA on ANY failure (CLAUDE.md hard
    constraint: TRT init failure must degrade to CUDA, never crash).

    The ``warmup`` (run one inference) is run on the primary so a TRT *engine build*
    failure -- which surfaces at first inference, not construction -- is caught HERE
    (at startup), not mid-turn. If primary construction OR its warmup raises, build
    the fallback (CUDA) and warm that instead. ``primary_factory`` /
    ``fallback_factory`` are injected -> the CI test drives this with fakes, no ORT."""
    try:
        engine = primary_factory()
        if warmup is not None:
            warmup(engine)
        return EngineBuildResult(engine=engine, used="trt", error=None)
    except Exception as exc:  # noqa: BLE001 -- any TRT failure -> CUDA fallback
        if on_fallback is not None:
            on_fallback(exc)
        engine = fallback_factory()
        if warmup is not None:
            warmup(engine)
        return EngineBuildResult(engine=engine, used="cuda", error=exc)

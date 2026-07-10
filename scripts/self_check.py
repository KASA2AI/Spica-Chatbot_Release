#!/usr/bin/env python
"""Spica 模型自检：检查各子系统模型能否正常运行、跑在 CPU 还是 CUDA 上。

用法（在仓库根、生产 env 中运行；建议关闭 Spica 应用后再跑 --full）:

  python scripts/self_check.py                    # 轻量档: 零模型加载、零显存
  python scripts/self_check.py --full             # 真加载档: 逐子系统独立子进程真跑
  python scripts/self_check.py --full --only stt,ocr
  python scripts/self_check.py --json             # 机器可读输出
  python scripts/self_check.py --full --llm       # 附带线上 LLM 连通性检查(会发真实请求)
  python scripts/self_check.py --full --all       # 连被开关关掉的子系统也检
  python scripts/self_check.py --full --allow-model-downloads   # 放开 HF 下载(默认离线)

状态语义:
  PASS              真跑通过(仅 --full 会给出), 或轻量档事实核查完全通过(config/gpu/secrets)
  DEGRADED          能跑但不在期望环境(如配置 cuda 实际落 CPU / 文件缺失)
  FAIL              跑不通(含超时; 进程树已清理)
  SKIPPED_DISABLED  被 enabled 开关关掉(--all 强制检查)
  UNVERIFIED        轻量档下无法不加载模型验证的项(只报事实, 不算失败)

exit code: 0=无 FAIL/DEGRADED; 1=有 DEGRADED; 2=有 FAIL; 3=自检自身错误/前置拒绝。

环境变量申明(doctor.py 纪律: scripts/ 在 no-getenv 扫描域之外, 但读了什么要在文件头写明):
  - 读: 各 secrets env 名的**在位与否**(值绝不打印/绝不进报告), 见 env_roster.SECRETS_ENV_MAP;
        OPENAI_BASE_URL / JUDGE_* 端点信息(--llm 时用于连通性检查)。
  - 写(仅子进程 env): HF_HUB_OFFLINE=1 / TRANSFORMERS_OFFLINE=1 /
        SPICA_SELF_CHECK_NO_DOWNLOAD=1(默认禁下载; HF 变量只管 Hugging Face,
        audio-separator 的 GitHub 下载路径靠第三个变量在 worker 内预检),
        HF_HUB_DISABLE_TELEMETRY=1。--allow-model-downloads 时三者从子进程 env 移除
        (含父环境里预先存在的)。

设计(2026-07 review 采纳): 每个真跑检查在独立子进程执行(退出即释放显存, 互不争 GPU),
逐项 timeout + 进程树 SIGKILL 清理; 显存为按 PID/后代 nvidia-smi 采样的**近似**峰值
(torch.cuda.memory_allocated 看不到 CTranslate2/ONNX Runtime/RVC 子进程)。
"""

from __future__ import annotations

import argparse
import json
import math
import os
import signal
import struct
import subprocess
import sys
import threading
import time
import wave
from pathlib import Path
from typing import Any, Callable

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from spica.config.secrets import load_secrets  # noqa: E402  (铁律 #10: main 首句灌注)

RESULT_MARKER = "SELF_CHECK_RESULT_JSON:"

STATUS_PASS = "PASS"
STATUS_DEGRADED = "DEGRADED"
STATUS_FAIL = "FAIL"
STATUS_SKIPPED = "SKIPPED_DISABLED"
STATUS_UNVERIFIED = "UNVERIFIED"

VALID_STATUSES = frozenset(
    {STATUS_PASS, STATUS_DEGRADED, STATUS_FAIL, STATUS_SKIPPED, STATUS_UNVERIFIED}
)

# 正式入口是 webui_qt.py(main -> ui.qt_overlay.main); 直接跑 qt_overlay 也要挡。
APP_PROCESS_PATTERNS = ("webui_qt", "qt_overlay")

HEAVY_CHECKS = ("tts", "stt", "moondream", "ocr", "song_uvr", "song_rvc", "llm")

DEFAULT_TIMEOUTS_S: dict[str, float] = {
    "tts": 300.0,
    "stt": 240.0,
    "moondream": 300.0,
    "ocr": 240.0,
    "song_uvr": 300.0,
    "song_rvc": 480.0,
    "llm": 60.0,
}


# --------------------------------------------------------------------------
# small pure helpers (unit-tested)
# --------------------------------------------------------------------------

def exit_code_for(results: list[dict[str, Any]]) -> int:
    statuses = {str(r.get("status")) for r in results}
    if any(s not in VALID_STATUSES for s in statuses):
        return 2  # unknown status = a broken worker/report, never silently OK
    if STATUS_FAIL in statuses:
        return 2
    if STATUS_DEGRADED in statuses:
        return 1
    return 0


def parse_worker_stdout(stdout: str) -> dict[str, Any] | None:
    """Last RESULT_MARKER line wins -- heavy libraries print freely before it."""
    payload = None
    for line in stdout.splitlines():
        if line.startswith(RESULT_MARKER):
            try:
                payload = json.loads(line[len(RESULT_MARKER):])
            except json.JSONDecodeError:
                payload = None
    return payload


def render_report(mode: str, results: list[dict[str, Any]], as_json: bool) -> str:
    if as_json:
        return json.dumps(
            {"mode": mode, "results": results, "exit_code": exit_code_for(results)},
            ensure_ascii=False, indent=2,
        )
    lines = [f"[self-check] mode={mode}"]
    width = max((len(str(r.get('name'))) for r in results), default=8) + 2
    for r in results:
        detail = r.get("detail") or {}
        facts = " ".join(f"{k}={v}" for k, v in detail.items())
        reason = f"  [{r['reason']}]" if r.get("reason") else ""
        lines.append(f"  {str(r.get('name')).ljust(width)}{str(r.get('status')).ljust(18)}{facts}{reason}")
    lines.append(f"exit_code={exit_code_for(results)}")
    return "\n".join(lines)


def secrets_presence(environ: dict[str, str]) -> dict[str, bool]:
    """Presence booleans ONLY -- secret values must never reach the report."""
    from spica.config.env_roster import SECRETS_ENV_MAP

    return {env_name: bool(environ.get(env_name)) for env_name in SECRETS_ENV_MAP.values()}


def write_sine_wav(path: Path, seconds: float = 2.0, freq: float = 440.0, rate: int = 44100) -> Path:
    """Deterministic local fixture (never depends on a song cache): amplitude-
    modulated sine so the separator has non-trivial content to chew on."""
    path.parent.mkdir(parents=True, exist_ok=True)
    frames = int(seconds * rate)
    with wave.open(str(path), "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(rate)
        chunk = bytearray()
        for i in range(frames):
            t = i / rate
            amp = 0.5 + 0.45 * math.sin(2 * math.pi * 2.0 * t)  # 2 Hz tremolo
            sample = int(32767 * 0.6 * amp * math.sin(2 * math.pi * freq * t))
            chunk += struct.pack("<h", sample)
        f.writeframes(bytes(chunk))
    return path


# --------------------------------------------------------------------------
# subprocess runner (timeout + process-tree kill + approx VRAM sampling)
# --------------------------------------------------------------------------

def _descendant_pids(root_pid: int) -> set[int]:
    pids = {root_pid}
    try:  # psutil first (works on Windows too); optional dependency
        import psutil  # noqa: PLC0415

        proc = psutil.Process(root_pid)
        return pids | {p.pid for p in proc.children(recursive=True)}
    except Exception:
        pass
    if os.name == "nt":
        return pids  # no psutil on Windows -> root only (sampling still works)
    try:
        out = subprocess.run(
            ["ps", "-eo", "pid,ppid"], capture_output=True, text=True, timeout=5
        ).stdout
    except Exception:
        return pids
    children: dict[int, list[int]] = {}
    for line in out.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
            children.setdefault(int(parts[1]), []).append(int(parts[0]))
    stack = [root_pid]
    while stack:
        for child in children.get(stack.pop(), []):
            if child not in pids:
                pids.add(child)
                stack.append(child)
    return pids


def _vram_sampler(root_pid: int, stop: threading.Event, out: dict[str, Any]) -> None:
    peak = 0
    while not stop.is_set():
        try:
            txt = subprocess.run(
                ["nvidia-smi", "--query-compute-apps=pid,used_memory",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=5,
            ).stdout
        except Exception:
            return  # no nvidia-smi -> no sampling, non-fatal
        pids = _descendant_pids(root_pid)
        total = 0
        for line in txt.splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 2 and parts[0].isdigit() and int(parts[0]) in pids:
                try:
                    total += int(parts[1])
                except ValueError:
                    pass
        peak = max(peak, total)
        out["approx_vram_peak_mb"] = peak
        stop.wait(0.5)


def _kill_process_tree(proc: subprocess.Popen) -> None:
    """Platform-correct tree kill: POSIX kills the session group; Windows uses
    taskkill /T (killpg/ps/pgrep would raise there and leak model processes)."""
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
            capture_output=True, timeout=15,
        )
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    proc.wait()


def run_subprocess_check(
    cmd: list[str],
    timeout_s: float,
    env: dict[str, str] | None = None,
    sample_vram: bool = True,
) -> dict[str, Any]:
    """Run one worker command; returns a result dict (status/detail/reason).

    Trust rules (2026-07 review): a nonzero return code is ALWAYS a FAIL, even
    after a PASS report (crash-after-report); an unknown status string is a
    FAIL, never a silent exit-0."""
    started = time.time()
    popen_kwargs: dict[str, Any] = (
        {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt"
        else {"start_new_session": True}
    )
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        cwd=str(REPO_ROOT), env=env, **popen_kwargs,
    )
    vram: dict[str, Any] = {}
    stop = threading.Event()
    sampler = None
    if sample_vram:
        sampler = threading.Thread(
            target=_vram_sampler, args=(proc.pid, stop, vram), daemon=True
        )
        sampler.start()
    try:
        stdout, stderr = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        _kill_process_tree(proc)
        return {
            "status": STATUS_FAIL,
            "reason": f"timeout after {timeout_s:.0f}s (process tree killed)",
            "detail": dict(vram),
        }
    except BaseException:
        # Ctrl-C / bad-timeout / any parent failure: workers run in their OWN
        # session (start_new_session) so the terminal SIGINT never reaches
        # them -- without this kill an interrupted --full leaves GPU workers
        # alive and holding VRAM (review P1).
        _kill_process_tree(proc)
        raise
    finally:
        stop.set()
        if sampler is not None:
            sampler.join(timeout=2)
    duration = round(time.time() - started, 1)
    payload = parse_worker_stdout(stdout or "")
    if payload is None:
        tail = (stderr or "").strip().splitlines()[-3:]
        return {
            "status": STATUS_FAIL,
            "reason": f"worker exited rc={proc.returncode} without a result "
                      f"(stderr tail: {' | '.join(tail) if tail else 'empty'})",
            "detail": {"duration_s": duration, **vram},
        }
    if not isinstance(payload, dict):
        # a legal-but-non-object JSON (array/string) must become a FAIL, not an
        # AttributeError that crashes the CLI with an unspecified exit code.
        return {
            "status": STATUS_FAIL,
            "reason": f"worker result is not a JSON object: {type(payload).__name__}",
            "detail": {"duration_s": duration, **vram},
        }
    if proc.returncode != 0:
        return {
            "status": STATUS_FAIL,
            "reason": f"worker exited rc={proc.returncode} AFTER reporting "
                      f"{payload.get('status')!r} -- crash-after-report is a FAIL",
            "detail": {"duration_s": duration, **vram},
        }
    status = str(payload.get("status") or "")
    if status not in VALID_STATUSES:
        return {
            "status": STATUS_FAIL,
            "reason": f"worker returned invalid status {payload.get('status')!r} "
                      f"(expected one of {sorted(VALID_STATUSES)})",
            "detail": {"duration_s": duration, **vram},
        }
    raw_detail = payload.get("detail")
    if raw_detail is not None and not isinstance(raw_detail, dict):
        # nested-shape violation ({"detail": "oops"}) must be a FAIL, not a
        # ValueError that crashes the CLI with an unspecified exit code.
        return {
            "status": STATUS_FAIL,
            "reason": f"worker detail is not a JSON object: {type(raw_detail).__name__}",
            "detail": {"duration_s": duration, **vram},
        }
    detail = dict(raw_detail or {})
    detail.setdefault("duration_s", payload.get("duration_s", duration))
    detail.update(vram)
    return {"status": status, "reason": str(payload.get("reason") or ""), "detail": detail}


def _worker_command(name: str, allow_downloads: bool) -> list[str]:
    cmd = [sys.executable, str(Path(__file__).resolve()), "--worker", name]
    if allow_downloads:
        cmd.append("--allow-model-downloads")
    return cmd


def _worker_env(allow_downloads: bool, force_disabled: bool = False) -> dict[str, str]:
    env = dict(os.environ)
    env["HF_HUB_DISABLE_TELEMETRY"] = "1"
    env["PYTHONUNBUFFERED"] = "1"
    if force_disabled:
        # --all: workers that apply enabled switches inside their production
        # assembly seams (tts) must be told to check the engine anyway.
        env["SPICA_SELF_CHECK_FORCE_DISABLED"] = "1"
    else:
        env.pop("SPICA_SELF_CHECK_FORCE_DISABLED", None)
    if allow_downloads:
        # honour the flag even when the PARENT env already forces offline.
        env.pop("HF_HUB_OFFLINE", None)
        env.pop("TRANSFORMERS_OFFLINE", None)
        env.pop("SPICA_SELF_CHECK_NO_DOWNLOAD", None)
    else:
        # default no-download: a missing model must FAIL with a clear hint, not
        # silently pull gigabytes mid-check. HF flags only cover Hugging Face;
        # workers with non-HF download paths (audio-separator pulls UVR weights
        # from GitHub) pre-check via SPICA_SELF_CHECK_NO_DOWNLOAD.
        env["HF_HUB_OFFLINE"] = "1"
        env["TRANSFORMERS_OFFLINE"] = "1"
        env["SPICA_SELF_CHECK_NO_DOWNLOAD"] = "1"
    return env


def spica_appears_running() -> bool:
    """True when the real app looks alive. The production entry is webui_qt.py
    (its main imports ui.qt_overlay), so matching qt_overlay alone misses a
    normally-started Spica."""
    own = os.getpid()
    try:  # psutil first: cross-platform cmdline scan (optional dependency)
        import psutil  # noqa: PLC0415

        for proc in psutil.process_iter(["pid", "cmdline"]):
            if proc.info["pid"] == own:
                continue
            cmdline = " ".join(proc.info.get("cmdline") or [])
            if any(pattern in cmdline for pattern in APP_PROCESS_PATTERNS):
                return True
        return False
    except Exception:
        pass
    if os.name == "nt":
        return False  # no psutil on Windows -> detection unavailable (best-effort)
    try:
        out = subprocess.run(
            ["pgrep", "-af", "|".join(APP_PROCESS_PATTERNS)],
            capture_output=True, text=True, timeout=5,
        ).stdout
    except Exception:
        return False
    return any(
        line.split() and line.split()[0] != str(own) for line in out.splitlines()
    )


# --------------------------------------------------------------------------
# light checks (in-process; NO model loads, no VRAM)
# --------------------------------------------------------------------------

def _load_configs() -> tuple[Any, Any, dict[str, Any], dict[str, Any]]:
    from spica.config.manager import ConfigManager
    from agent_tools.function_tools.screen.config import resolve_effective_screen_config
    from agent_tools.function_tools.song.config import resolve_effective_song_config
    from agent_tools.tts.manager import load_tts_config

    app = ConfigManager().load()
    return app, resolve_effective_screen_config(), resolve_effective_song_config(), load_tts_config()


def check_config_light(app: Any, screen_cfg: Any, song_cfg: dict[str, Any]) -> dict[str, Any]:
    from agent_tools.function_tools.song.config import song_enabled

    return {
        "name": "config",
        "status": STATUS_PASS,
        "detail": {
            "tts_enabled": app.tts.enabled,
            "stt_backend": app.stt.backend,
            "stt_device": app.stt.device,
            "stt_warmup_on_startup": app.stt.warmup_on_startup,
            "screen_enabled": bool(screen_cfg.enabled),
            "screen_device": screen_cfg.device,
            "song_enabled": song_enabled(song_cfg),
            "anime_enabled": app.anime.enabled,
            "ocr_provider": app.ocr.provider,
        },
    }


def check_gpu_light() -> dict[str, Any]:
    from spica.local_runtime.device import probe_device

    info = probe_device().to_dict()
    detail: dict[str, Any] = {
        "onnx_cuda_ep": info["cuda_ep"],
        "onnx_tensorrt_ep": info["tensorrt_ep"],
        "nvidia_driver": info["nvidia_driver"],
    }
    try:
        txt = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total,memory.used",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10,
        ).stdout.strip()
        if txt:
            name, total, used = [p.strip() for p in txt.splitlines()[0].split(",")]
            detail.update({"gpu": name, "vram_total_mb": int(total), "vram_used_mb": int(used)})
    except Exception:
        pass
    status = STATUS_PASS if info["nvidia_driver"] else STATUS_DEGRADED
    reason = "" if info["nvidia_driver"] else "无 NVIDIA 驱动/GPU——所有 cuda 配置将失败"
    return {"name": "gpu", "status": status, "detail": detail, "reason": reason}


def check_secrets_light() -> dict[str, Any]:
    presence = secrets_presence(dict(os.environ))
    missing_core = [k for k in ("OPENAI_API_KEY",) if not presence.get(k)]
    return {
        "name": "secrets",
        "status": STATUS_DEGRADED if missing_core else STATUS_PASS,
        "detail": presence,  # booleans ONLY -- never values
        "reason": f"缺少 {','.join(missing_core)}" if missing_core else "",
    }


def _tts_paths(tts_cfg: dict[str, Any]) -> dict[str, bool]:
    base = Path(str(tts_cfg.get("_config_path") or REPO_ROOT / "data/config/tts.yaml")).parent
    out: dict[str, bool] = {}
    for key in ("gptsovits_root", "gpt_model_path", "sovits_model_path"):
        raw = tts_cfg.get(key)
        if raw:
            out[key] = (base / str(raw)).resolve().exists()
    return out


def check_tts_light(app: Any, tts_cfg: dict[str, Any]) -> dict[str, Any]:
    if not app.tts.enabled:
        return {"name": "tts", "status": STATUS_SKIPPED, "detail": {"enabled": False},
                "reason": "tts.enabled=false(纯文本模式)"}
    paths = _tts_paths(tts_cfg)
    missing = [k for k, ok in paths.items() if not ok]
    status = STATUS_DEGRADED if missing else STATUS_UNVERIFIED
    return {
        "name": "tts", "status": status,
        "detail": {"provider": tts_cfg.get("provider"), **paths},
        "reason": f"文件缺失: {','.join(missing)}" if missing else "轻量档不加载模型(--full 真跑)",
    }


def check_stt_light(app: Any) -> dict[str, Any]:
    cfg = app.stt
    if cfg.backend != "faster_whisper":
        return {"name": "stt", "status": STATUS_SKIPPED,
                "detail": {"backend": cfg.backend},
                "reason": "本地 STT 关闭(backend=google 线上回退)"}
    model_path = Path(cfg.model)
    if not model_path.is_absolute():
        model_path = REPO_ROOT / cfg.model
    model_is_dir = model_path.is_dir()
    detail = {"model": cfg.model, "device": cfg.device, "compute_type": cfg.compute_type,
              "local_model_dir": model_is_dir}
    if "/" not in cfg.model and not model_is_dir:
        detail["local_model_dir"] = "n/a(HF repo id)"
        return {"name": "stt", "status": STATUS_UNVERIFIED, "detail": detail,
                "reason": "轻量档不加载模型(--full 真跑)"}
    status = STATUS_UNVERIFIED if model_is_dir else STATUS_DEGRADED
    return {"name": "stt", "status": status, "detail": detail,
            "reason": "轻量档不加载模型(--full 真跑)" if model_is_dir
            else f"本地模型目录不存在: {model_path}"}


def check_moondream_light(screen_cfg: Any) -> dict[str, Any]:
    if not screen_cfg.enabled:
        return {"name": "moondream", "status": STATUS_SKIPPED,
                "detail": {"enabled": False}, "reason": "screen.enabled=false"}
    cache = Path.home() / ".cache/huggingface/hub" / (
        "models--" + str(screen_cfg.model_id).replace("/", "--"))
    return {
        "name": "moondream", "status": STATUS_UNVERIFIED,
        "detail": {"provider": screen_cfg.provider, "device": screen_cfg.device,
                   "dtype": screen_cfg.dtype, "hf_cache_present": cache.exists()},
        "reason": "轻量档不加载模型(--full 真跑)",
    }


def check_ocr_light(app: Any) -> dict[str, Any]:
    from spica.local_runtime.device import probe_device

    info = probe_device()
    wants_gpu = app.ocr.provider in ("rapidocr_ort", "rapidocr_trt_ep")
    degraded = wants_gpu and not info.cuda_ep
    return {
        "name": "ocr",
        "status": STATUS_DEGRADED if degraded else STATUS_UNVERIFIED,
        "detail": {"provider": app.ocr.provider, "fallback": app.ocr.fallback_provider,
                   "onnx_cuda_ep": info.cuda_ep, "onnx_tensorrt_ep": info.tensorrt_ep},
        "reason": "配置 GPU EP 但 onnxruntime 无 CUDA EP" if degraded
        else "轻量档不跑推理(--full 报逐 session 实际 provider)",
    }


def check_song_light(song_cfg: dict[str, Any]) -> list[dict[str, Any]]:
    from agent_tools.function_tools.song.config import song_enabled

    if not song_enabled(song_cfg):
        skip = {"detail": {"enabled": False}, "reason": "song.enabled=false"}
        return [{"name": "song_uvr", "status": STATUS_SKIPPED, **skip},
                {"name": "song_rvc", "status": STATUS_SKIPPED, **skip}]
    rvc = song_cfg.get("rvc", {})
    voice_name = str(rvc.get("voice_model") or "spica")
    voice = (rvc.get("voices") or {}).get(voice_name) or {}
    model_path = str(voice.get("model_path") or "")
    index_path = str(voice.get("index_path") or "")
    model_ok = bool(model_path) and Path(model_path).exists()
    # 显式配置了 index 却指向不存在的文件 = 配置错误(生产会静默丢弃 index 降质量);
    # 未配置 index 则不算缺失。
    index_ok = (not index_path) or Path(index_path).exists()
    sep = song_cfg.get("separator", {})
    uvr = {
        "name": "song_uvr", "status": STATUS_UNVERIFIED,
        "detail": {"model_filename": sep.get("model_filename")},
        "reason": "轻量档不跑分离(--full 真分离正弦 fixture)",
    }
    rvc_missing = [k for k, ok in (("model_path", model_ok), ("index_path", index_ok)) if not ok]
    rvc_check = {
        "name": "song_rvc",
        # 任何缺失都必须体现在 status 上(review: reason 报缺失、status 却
        # UNVERIFIED/exit 0 是自相矛盾的假绿)。
        "status": STATUS_DEGRADED if rvc_missing else STATUS_UNVERIFIED,
        "detail": {"voice": voice_name, "device": voice.get("device"),
                   "execution_mode": rvc.get("execution_mode") or "subprocess",
                   "model_path_exists": model_ok,
                   "index_path_exists": (Path(index_path).exists() if index_path
                                         else "not_configured")},
        "reason": f"文件缺失: {','.join(rvc_missing)}" if rvc_missing
        else "轻量档不跑推理(--full 真推理)",
    }
    return [uvr, rvc_check]


def check_llm_light(app: Any) -> dict[str, Any]:
    # mirrors ModelRouter.role_model / judge_adapter's decisions (names only,
    # no network): summary/judge fall back to the dialogue model; the judge
    # endpoint is distinct iff JUDGE_API_KEY is set (router reads secrets).
    summary_model = app.galgame.summary_model or app.llm.model
    judge_model = app.galgame.reaction_judge_model or app.llm.model
    return {
        "name": "llm", "status": STATUS_UNVERIFIED,
        "detail": {"main_model": app.llm.model, "summary_model": summary_model,
                   "judge_model": judge_model,
                   "judge_endpoint_distinct": bool(os.environ.get("JUDGE_API_KEY"))},
        "reason": "不默认联网; --full --llm 时经生产 ModelRouter 对各角色发极小真实请求",
    }


# --------------------------------------------------------------------------
# heavy workers (each runs INSIDE its own subprocess via --worker <name>)
# --------------------------------------------------------------------------

def _torch_cuda_evidence() -> dict[str, Any]:
    try:
        import torch

        ev: dict[str, Any] = {"torch_cuda_available": bool(torch.cuda.is_available())}
        if torch.cuda.is_available():
            ev["torch_device_name"] = torch.cuda.get_device_name(0)
        return ev
    except Exception:
        return {"torch_cuda_available": "unknown"}


def _worker_tts() -> dict[str, Any]:
    """Warm the TTS PRODUCTION would assemble -- via the host's OWN assembly
    seam (``AppHost._resolve_tts_assembly``: character-package tts.yaml +
    registry provider + the tts.enabled switch), WITHOUT full initialize():
    a missing LLM key or an unrelated subsystem's assembly failure must not
    read as a TTS FAIL (review P2 -- per-subsystem checks stay independent).
    --all sets SPICA_SELF_CHECK_FORCE_DISABLED so a disabled tts.enabled still
    checks the underlying engine (review P2 -- the parent alone can't force it,
    the switch is applied inside the assembly seam)."""
    from agent_tools.tts.manager import load_tts_config
    from spica.config.manager import ConfigManager
    from spica.host.app_host import (
        DEFAULT_SPICA_SKILL_DIR,
        AppHost,
        load_character_package,
    )
    from spica.host.builtins import register_tts_providers
    from spica.plugins.registry import CapabilityRegistry

    # __new__ 而非 AppHost(): __init__ 会构造 screen 工具/PluginHost 等无关件,
    # 它们的异常会在进入 TTS 装配缝之前把结果记成 TTS FAIL(review P2)。缝只读
    # config 和 registry 两个属性; registry 只注册 builtins 的 TTS 切片(单一来源)。
    host = AppHost.__new__(AppHost)
    host.config = ConfigManager().load()
    host.registry = CapabilityRegistry()
    register_tts_providers(host.registry)
    package = load_character_package(
        host.config.character.package_dir or DEFAULT_SPICA_SKILL_DIR
    )
    tts_config = (
        load_tts_config(package.tts_config_path)
        if package.tts_config_path else load_tts_config()
    )
    forced = False
    if (not host.config.tts.enabled
            and os.environ.get("SPICA_SELF_CHECK_FORCE_DISABLED") == "1"):
        host.config.tts.enabled = True
        forced = True
    provider, _tool, adapter = host._resolve_tts_assembly(tts_config)
    public_config = getattr(adapter, "public_config", None)
    warmup = getattr(adapter, "warmup", None)
    if adapter is None or public_config is None or warmup is None:
        return {"status": STATUS_UNVERIFIED,
                "reason": f"生产装配的 provider={provider} 无预热面(如 text_only/dummy)——无模型可检",
                "detail": {"provider": provider, "forced_despite_disabled": forced}}
    emotion = str((public_config() or {}).get("warmup_emotion") or "happy")
    result = warmup(emotion=emotion, synthesize=True)
    # Device EVIDENCE (not just availability): GPT-SoVITS runs torch in THIS
    # process, so resident weights show up in torch.cuda.memory_allocated().
    allocated_mb: Any = None
    try:
        import torch  # noqa: PLC0415

        if torch.cuda.is_available():
            allocated_mb = round(torch.cuda.memory_allocated() / (1024 * 1024), 1)
    except Exception:
        pass
    detail = {"provider": provider, "forced_despite_disabled": forced,
              "warmup_duration_ms": result.get("duration_ms"),
              "torch_cuda_allocated_mb": allocated_mb, **_torch_cuda_evidence()}
    if not result.get("ok"):
        return {"status": STATUS_FAIL, "reason": str(result.get("error") or "warmup failed"),
                "detail": detail}
    if allocated_mb is None:
        return {"status": STATUS_DEGRADED,
                "reason": "无 CUDA 显存占用证据(torch 无 CUDA)——合成成功但疑似跑在 CPU",
                "detail": detail}
    if allocated_mb <= 0:
        return {"status": STATUS_DEGRADED,
                "reason": "合成成功但 torch CUDA 分配为 0——疑似跑在 CPU", "detail": detail}
    detail["device_evidence"] = "cuda"
    return {"status": STATUS_PASS, "detail": detail}


def _worker_stt() -> dict[str, Any]:
    from spica.config.manager import ConfigManager
    from spica.adapters.stt.faster_whisper import FasterWhisperAdapter

    cfg = ConfigManager().load().stt
    adapter = FasterWhisperAdapter(
        model=cfg.model, device=cfg.device, compute_type=cfg.compute_type,
        language=cfg.language, beam_size=cfg.beam_size, vad_filter=cfg.vad_filter,
        download_root=cfg.download_root,
    )
    result = adapter.warmup()  # drains the segments generator = real decode
    # ACTUAL device evidence from the loaded CTranslate2 model (configured
    # device is just an echo; the review requires runtime proof or DEGRADED).
    actual_device: Any = None
    try:
        ct2_model = getattr(getattr(adapter, "_model", None), "model", None)
        actual_device = getattr(ct2_model, "device", None)
    except Exception:
        pass
    detail = {"model": cfg.model, "configured_device": cfg.device,
              "actual_device": actual_device, "compute_type": cfg.compute_type,
              "warmup_duration_ms": result.get("duration_ms")}
    if not result.get("ok"):
        return {"status": STATUS_FAIL, "reason": str(result.get("error")), "detail": detail}
    if actual_device is None:
        return {"status": STATUS_DEGRADED,
                "reason": "取不到 CTranslate2 实际 device 证据(API 变动?)", "detail": detail}
    if str(actual_device) != str(cfg.device):
        return {"status": STATUS_DEGRADED,
                "reason": f"配置 device={cfg.device} 实际={actual_device}", "detail": detail}
    return {"status": STATUS_PASS, "detail": detail}


def _worker_moondream() -> dict[str, Any]:
    from PIL import Image

    from agent_tools.function_tools.screen.config import resolve_effective_screen_config
    from agent_tools.function_tools.screen.model_manager import get_moondream_manager
    from spica.host.agent_assembly import build_moondream_provider

    cfg = resolve_effective_screen_config()
    provider = build_moondream_provider(cfg.provider)  # same seam install as AppHost
    if provider is not None:
        from agent_tools.function_tools.screen.backends.moondream_runtime import (
            set_active_moondream_provider,
        )

        set_active_moondream_provider(provider)
    image = Image.new("RGB", (64, 64), (200, 120, 40))
    manager = get_moondream_manager(cfg)
    answer = manager.query(image, "What is the dominant color?")
    status_details = manager.get_status_details()
    detail = {"provider": cfg.provider,
              "device": status_details.get("device"),
              "dtype": status_details.get("dtype"),
              "state": status_details.get("state"),
              # loader evidence: _validate_config 强制 device==cuda 且
              # _assert_cuda_available 断言 torch.cuda -- 加载成功本身就证明在 CUDA。
              "cuda_enforced_by_loader": True,
              "answer_chars": len(str(answer or ""))}
    if not str(answer or "").strip():
        return {"status": STATUS_DEGRADED, "reason": "加载成功但空回答", "detail": detail}
    return {"status": STATUS_PASS, "detail": detail}


def _ocr_session_providers(provider: str) -> dict[str, Any]:
    """Per-session in-use providers. For providers that delegate to the SHARED
    legacy engine singleton (rapidocr / rapidocr_ort) we reflect its exact
    attribute layout (diag_ocr_providers.py pins it for rapidocr_onnxruntime
    1.4.x). For other providers (rapidocr_trt_ep) touching ``_get_engine()``
    would LOAD a second engine, so we go straight to the gc sweep -- it
    enumerates every live onnxruntime.InferenceSession in the process."""
    out: dict[str, Any] = {}
    if provider in ("rapidocr", "rapidocr_ort"):
        try:
            from agent_tools.function_tools.screen.backends import rapidocr as ocr_backend

            engine = ocr_backend._get_engine()
            paths = {
                "det": ("text_det", "infer"),
                "cls": ("text_cls", "infer"),
                "rec": ("text_rec", "session"),  # rec names its wrapper differently
            }
            for name, (stage_attr, wrapper_attr) in paths.items():
                wrapper = getattr(getattr(engine, stage_attr, None), wrapper_attr, None)
                session = getattr(wrapper, "session", None)
                if session is not None and hasattr(session, "get_providers"):
                    out[name] = list(session.get_providers())
        except Exception:
            out = {}
    if not out:
        try:  # gc sweep (diag's fallback): provider-agnostic, no extra loads
            import gc  # noqa: PLC0415

            import onnxruntime  # noqa: PLC0415

            sessions = [
                obj for obj in gc.get_objects()
                if isinstance(obj, onnxruntime.InferenceSession)
            ]
            if sessions:
                out["gc-sweep"] = sorted(
                    {p for s in sessions for p in s.get_providers()}
                )
        except Exception:
            pass
    return out


def _synth_ocr_image() -> Any:
    """Dialog-strip-like synthetic image (diag_ocr_providers 的做法): real-sized
    text lines so det+cls+rec all genuinely run."""
    from PIL import Image, ImageDraw, ImageFont

    image = Image.new("RGB", (1280, 250), (24, 24, 48))
    draw = ImageDraw.Draw(image)
    font = None
    for candidate in ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                      "C:/Windows/Fonts/arial.ttf"):  # Windows fixture 字体
        try:
            font = ImageFont.truetype(candidate, 34)
            break
        except OSError:
            continue
    tiny_fallback = False
    if font is None:
        try:
            font = ImageFont.load_default(size=34)  # Pillow >= 10.1
        except TypeError:
            font = ImageFont.load_default()  # ~11px bitmap -- upscale below
            tiny_fallback = True
    lines = [
        "Spica self check: the quick brown fox jumps over the lazy dog.",
        "0123456789 ABCDEFGHIJKLMNOPQRSTUVWXYZ",
    ]
    for i, line in enumerate(lines):
        draw.text((30, 40 + i * 80), line, fill=(240, 240, 240), font=font)
    if tiny_fallback:
        # keep glyphs at a det-friendly size instead of fake-DEGRADING on
        # platforms without the truetype candidates (review P3).
        image = image.resize((image.width * 3, image.height * 3), Image.NEAREST)
    return image


def _worker_ocr() -> dict[str, Any]:
    from spica.config.manager import ConfigManager
    from spica.host.agent_assembly import build_ocr_adapter

    app = ConfigManager().load()
    adapter = build_ocr_adapter(
        app.ocr.provider, app.ocr.fallback_provider, trt_config=app.ocr.trt
    ) if hasattr(app.ocr, "trt") else build_ocr_adapter(app.ocr.provider, app.ocr.fallback_provider)
    image = _synth_ocr_image()
    started = time.time()
    result = adapter.recognize(image)
    duration_ms = round((time.time() - started) * 1000, 1)
    text = str(getattr(result, "text", "") or "")
    error = getattr(result, "error", None)
    providers = _ocr_session_providers(app.ocr.provider)
    import onnxruntime  # noqa: PLC0415

    detail = {"provider": app.ocr.provider, "recognize_ms": duration_ms,
              "text_chars": len(text), "text_sample": text[:40],
              "session_providers": providers or "unavailable",
              "ort_available": onnxruntime.get_available_providers()}
    if error:
        return {"status": STATUS_FAIL, "reason": f"OCR error payload: {error}", "detail": detail}
    wants_gpu = app.ocr.provider in ("rapidocr_ort", "rapidocr_trt_ep")
    if wants_gpu and not providers:
        # evidence missing must never read as PASS (review P1-5)
        return {"status": STATUS_DEGRADED,
                "reason": "配置 GPU EP 但拿不到任何 session provider 证据", "detail": detail}
    on_cpu_only = bool(providers) and all(
        set(p) == {"CPUExecutionProvider"} for p in providers.values())
    if wants_gpu and on_cpu_only:
        return {"status": STATUS_DEGRADED, "reason": "配置 GPU EP 但全部 session 落在 CPU",
                "detail": detail}
    if not text.strip():
        return {"status": STATUS_DEGRADED, "reason": "推理成功但没识别出文字", "detail": detail}
    return {"status": STATUS_PASS, "detail": detail}


def _worker_song_uvr() -> dict[str, Any]:
    import soundfile

    from agent_tools.function_tools.song.config import resolve_effective_song_config
    from agent_tools.function_tools.song.separator import separate_vocals

    cfg = resolve_effective_song_config()
    sep = cfg.get("separator", {})
    model_filename = str(sep.get("model_filename") or "")
    if os.environ.get("SPICA_SELF_CHECK_NO_DOWNLOAD") == "1":
        # audio-separator pulls missing UVR weights from GitHub (HF_HUB_OFFLINE
        # does NOT cover it) -- pre-check the model file so default mode never
        # silently downloads.
        model_dir = None
        try:
            from audio_separator.separator import Separator  # noqa: PLC0415

            probe = Separator(output_dir=str(REPO_ROOT / "static/generated_song/tmp"))
            model_dir = getattr(probe, "model_file_dir", None)
        except Exception:
            pass
        if model_dir is not None and not (Path(str(model_dir)) / model_filename).exists():
            return {"status": STATUS_FAIL,
                    "reason": f"UVR 模型文件缺失且默认禁下载: {model_dir}/{model_filename} "
                              "(--allow-model-downloads 放开)",
                    "detail": {"model_filename": model_filename}}
    work = REPO_ROOT / "static/generated_song/tmp/self_check_uvr"
    try:
        fixture = write_sine_wav(work / "fixture.wav")
        primary, secondary = separate_vocals(
            fixture, work / "out", str(sep.get("model_filename")),
            output_format=str(sep.get("output_format") or "WAV"),
            extra_kwargs=sep.get("extra_kwargs") or {},
        )
        frames = {Path(p).name: soundfile.info(str(p)).frames for p in (primary, secondary)}
    finally:
        import shutil

        shutil.rmtree(work, ignore_errors=True)
    detail = {"model_filename": sep.get("model_filename"), "output_frames": frames}
    if any(v <= 0 for v in frames.values()):
        return {"status": STATUS_FAIL, "reason": "分离输出不可解码/为空", "detail": detail}
    return {"status": STATUS_PASS, "detail": detail}


# vendored rvc/lib/utils.py::load_embedding 的目录映射(逐名镜像, 绝不做启发式
# dash/underscore 转换 -- spin-v2 保留连字符而 hubert 三个转下划线): 目录不对、
# 或 bin/config.json 缺任意一个, 它都会直接 wget 下载。
_RVC_EMBEDDER_DIRS = {
    "contentvec": "contentvec",
    "spin": "spin",
    "spin-v2": "spin-v2",
    "chinese-hubert-base": "chinese_hubert_base",
    "japanese-hubert-base": "japanese_hubert_base",
    "korean-hubert-base": "korean_hubert_base",
}


def rvc_embedder_missing_files(applio_root: Path, embedder: str) -> list[str] | None:
    """The files vendored ``load_embedding`` would wget if absent -- BOTH
    pytorch_model.bin and config.json have独立下载分支, so both are checked in
    the EXACT directory the vendored map uses. Returns None for an unknown
    embedder name (outside the map; production would KeyError -- config error)."""
    if embedder == "custom":
        return []  # custom 分支不下载(缺失时回落本地 contentvec 目录路径)
    dir_name = _RVC_EMBEDDER_DIRS.get(embedder)
    if dir_name is None:
        return None
    base = applio_root / "rvc" / "models" / "embedders" / dir_name
    return [
        str(base / name)
        for name in ("pytorch_model.bin", "config.json")
        if not (base / name).exists()
    ]


_RVC_PARAM_KEYS = (
    "f0_method", "transpose", "index_rate", "protect", "device", "volume_envelope",
    "split_audio", "f0_autotune", "f0_autotune_strength", "proposed_pitch",
    "proposed_pitch_threshold", "clean_audio", "clean_strength", "export_format",
    "embedder_model", "embedder_model_custom", "sid",
)


def _worker_song_rvc() -> dict[str, Any]:
    import soundfile

    from agent_tools.function_tools.song.config import resolve_effective_song_config
    from spica.local_runtime.rvc.driver import run_rvc

    cfg = resolve_effective_song_config()
    rvc = cfg.get("rvc", {})
    voice_name = str(rvc.get("voice_model") or "spica")
    voice = (rvc.get("voices") or {}).get(voice_name)
    if not isinstance(voice, dict):
        return {"status": STATUS_FAIL, "reason": f"找不到声线配置 {voice_name}", "detail": {}}
    applio_root = Path(str(cfg.get("applio_root") or ""))
    if os.environ.get("SPICA_SELF_CHECK_NO_DOWNLOAD") == "1":
        # vendored RVC 的 load_embedding 在 embedder 权重缺失时直接 wget 下载
        # ~361MiB contentvec(rvc/lib/utils.py, 不理会 HF_HUB_OFFLINE / 本脚本的
        # 离线变量; contentvec 又被 .gitignore 排除)——默认禁下载必须在跑之前
        # 把这条 GitHub/HF 之外的下载链也拦住(review P1)。
        embedder = str(voice.get("embedder_model") or "contentvec")
        missing = rvc_embedder_missing_files(applio_root, embedder)
        if missing is None:
            return {"status": STATUS_FAIL,
                    "reason": f"未知 embedder_model: {embedder}"
                              "(不在 vendored load_embedding 的映射表里, 生产会 KeyError)",
                    "detail": {"voice": voice_name, "embedder": embedder}}
        if missing:
            return {"status": STATUS_FAIL,
                    "reason": f"embedder({embedder}) 文件缺失且默认禁下载: "
                              f"{'; '.join(missing)} "
                              "(--allow-model-downloads 放开, 否则 vendored RVC 会 wget 补齐)",
                    "detail": {"voice": voice_name, "embedder": embedder}}
        f0_method = str(voice.get("f0_method") or "rmvpe")
        predictor = {"rmvpe": "rmvpe.pt", "fcpe": "fcpe.pt"}.get(f0_method)
        if predictor and not (applio_root / "rvc/models/predictors" / predictor).exists():
            return {"status": STATUS_FAIL,
                    "reason": f"f0 predictor 缺失: rvc/models/predictors/{predictor}",
                    "detail": {"voice": voice_name, "f0_method": f0_method}}
    params = {k: voice[k] for k in _RVC_PARAM_KEYS if k in voice}
    work = REPO_ROOT / "static/generated_song/tmp/self_check_rvc"
    try:
        fixture = write_sine_wav(work / "vocal_fixture.wav", seconds=2.0, freq=220.0)
        output = work / "rvc_out.wav"
        run_rvc(
            input_vocal_path=str(fixture),
            output_vocal_path=str(output),
            model_path=str(voice.get("model_path")),
            index_path=str(voice.get("index_path") or "") or None,
            applio_root=str(cfg.get("applio_root")),
            execution_mode=str(rvc.get("execution_mode") or "subprocess"),
            worker_python=rvc.get("worker_python"),
            **params,
        )
        frames = soundfile.info(str(output)).frames
    finally:
        import shutil

        shutil.rmtree(work, ignore_errors=True)
    detail = {"voice": voice_name, "configured_device": voice.get("device"),
              "execution_mode": rvc.get("execution_mode") or "subprocess",
              "output_frames": frames}
    if frames <= 0:
        return {"status": STATUS_FAIL, "reason": "RVC 输出不可解码/为空", "detail": detail}
    return {"status": STATUS_PASS, "detail": detail}


def llm_reply_missing(outcome: dict[str, Any]) -> bool:
    """no-exception but empty reply (format drift / content filter): reachable
    != working, must never read as PASS (review P2)."""
    return bool(outcome.get("ok")) and not outcome.get("reply_chars")


def _worker_llm() -> dict[str, Any]:
    """Ping every LLM role THROUGH the production assembly: AppHost.initialize()
    resolves the real adapters (Responses vs chat.completions is the adapter's
    decision) and ModelRouter is the ONE role/endpoint decision point (Phase
    6b) -- no env re-reading, no re-implemented judge fallback tree."""
    from spica.host.app_host import AppHost

    host = AppHost()
    host.initialize()
    roles = ["dialogue", "summary"]
    if host.config.galgame.reaction_judge_enabled:
        roles.append("judge")
    detail: dict[str, Any] = {}
    failures: list[str] = []
    suspicious: list[str] = []
    pinged: dict[tuple, dict[str, Any]] = {}
    for role in roles:
        bound = host.model_router.for_role(role)
        entry: dict[str, Any] = {"model": bound.model}
        if role == "judge":
            entry["endpoint_distinct"] = bool(
                host.secrets.judge_api_key if host.secrets else None
            )
        key = (id(bound.adapter), bound.model)
        if key in pinged:  # same adapter+model already proven -- don't re-bill
            entry.update(pinged[key])
            entry["deduped_with_prior_role"] = True
            detail[role] = entry
            if llm_reply_missing(entry):
                suspicious.append(role)
            continue
        started = time.time()
        try:
            reply = bound.complete("请只回复一个字：好")
            outcome = {"ok": True,
                       "latency_ms": round((time.time() - started) * 1000, 1),
                       "reply_chars": len(str(reply or "").strip())}
        except Exception as exc:
            failures.append(f"{role}: {type(exc).__name__}: {exc}")
            outcome = {"ok": False}
        pinged[key] = outcome
        entry.update(outcome)
        detail[role] = entry
        if llm_reply_missing(outcome):
            suspicious.append(role)
    if failures:
        return {"status": STATUS_FAIL, "reason": "; ".join(failures)[:400], "detail": detail}
    if suspicious:
        return {"status": STATUS_DEGRADED,
                "reason": f"请求成功但回复为空: {','.join(suspicious)}", "detail": detail}
    return {"status": STATUS_PASS, "detail": detail}


WORKERS: dict[str, Callable[[], dict[str, Any]]] = {
    "tts": _worker_tts,
    "stt": _worker_stt,
    "moondream": _worker_moondream,
    "ocr": _worker_ocr,
    "song_uvr": _worker_song_uvr,
    "song_rvc": _worker_song_rvc,
    "llm": _worker_llm,
}


def run_worker_and_print(name: str) -> int:
    started = time.time()
    try:
        payload = WORKERS[name]()
    except Exception as exc:  # noqa: BLE001 -- everything becomes a typed FAIL result
        payload = {"status": STATUS_FAIL,
                   "reason": f"{type(exc).__name__}: {exc}"[:500], "detail": {}}
    payload["duration_s"] = round(time.time() - started, 1)
    print(RESULT_MARKER + json.dumps(payload, ensure_ascii=False), flush=True)
    return 0


# --------------------------------------------------------------------------
# orchestration
# --------------------------------------------------------------------------

def _light_results(app: Any, screen_cfg: Any, song_cfg: dict[str, Any],
                   tts_cfg: dict[str, Any]) -> list[dict[str, Any]]:
    results = [
        check_config_light(app, screen_cfg, song_cfg),
        check_gpu_light(),
        check_secrets_light(),
        check_tts_light(app, tts_cfg),
        check_stt_light(app),
        check_moondream_light(screen_cfg),
        check_ocr_light(app),
        *check_song_light(song_cfg),
        check_llm_light(app),
    ]
    return results


def _enabled_map(app: Any, screen_cfg: Any, song_cfg: dict[str, Any]) -> dict[str, bool]:
    from agent_tools.function_tools.song.config import song_enabled

    song_on = song_enabled(song_cfg)
    return {
        "tts": bool(app.tts.enabled),
        "stt": app.stt.backend == "faster_whisper",
        "moondream": bool(screen_cfg.enabled),
        "ocr": True,  # galgame OCR 独立于 screen.enabled, 无开关
        "song_uvr": song_on,
        "song_rvc": song_on,
        "llm": True,
    }


def run_full_checks(
    names: list[str],
    enabled: dict[str, bool],
    args: argparse.Namespace,
    runner: Callable[..., dict[str, Any]] = run_subprocess_check,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for name in names:
        if name == "llm" and not args.llm:
            results.append({"name": name, "status": STATUS_UNVERIFIED, "detail": {},
                            "reason": "未启用 --llm(不默认联网)"})
            continue
        if not enabled.get(name, True) and not args.all:
            results.append({"name": name, "status": STATUS_SKIPPED, "detail": {},
                            "reason": "已被开关关闭(--all 强制检查)"})
            continue
        timeout = DEFAULT_TIMEOUTS_S[name] * args.timeout_scale
        print(f"[self-check] running {name} (timeout {timeout:.0f}s)...",
              file=sys.stderr, flush=True)
        result = runner(
            _worker_command(name, args.allow_model_downloads),
            timeout,
            env=_worker_env(args.allow_model_downloads, force_disabled=args.all),
        )
        results.append(_apply_gpu_evidence_rule(name, {"name": name, **result}))
    return results


def _apply_gpu_evidence_rule(name: str, result: dict[str, Any]) -> dict[str, Any]:
    """GPU evidence backstop for the song workers: their GPU work happens in
    onnxruntime / an RVC CHILD process, invisible to worker-side torch probes --
    the parent's per-PID-tree nvidia-smi sampling is the only proof. A PASS with
    zero sampled VRAM (or no sampling at all) cannot claim CUDA, so it degrades
    (review P1-5: evidence missing is never a silent PASS). tts/stt/moondream/ocr
    carry their own in-worker evidence and are not touched here."""
    if name not in ("song_uvr", "song_rvc") or result.get("status") != STATUS_PASS:
        return result
    detail = result.get("detail") or {}
    if str(detail.get("configured_device", "cuda")) != "cuda":
        return result  # deliberately configured off-GPU -- nothing to prove
    peak = detail.get("approx_vram_peak_mb")
    if peak is None:
        result["status"] = STATUS_DEGRADED
        result["reason"] = "跑通但无显存采样证据(nvidia-smi 不可用)——无法确认 GPU 生效"
    elif peak <= 0:
        result["status"] = STATUS_DEGRADED
        result["reason"] = "跑通但进程树显存峰值为 0——疑似跑在 CPU"
    return result


def main(argv: list[str] | None = None) -> int:
    load_secrets()  # 铁律 #10: 进程入口在构造任何对象之前先灌注环境(AST 测试钉住)
    try:
        return _main(argv)
    except KeyboardInterrupt:
        # 覆盖所有阶段(轻量检查/配置加载/守卫/重检查循环), 统一约定 exit 3;
        # 进行中的 worker 进程树已由 run_subprocess_check 的 BaseException 分支清理。
        print("[self-check] 已中断(Ctrl-C)——如有进行中的 worker 其进程树已清理。",
              file=sys.stderr)
        return 3


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--full", action="store_true", help="真加载逐子系统检查(独立子进程)")
    parser.add_argument("--only", default="", help="逗号分隔的子系统子集(如 stt,ocr)")
    parser.add_argument("--json", action="store_true", help="JSON 输出")
    parser.add_argument("--llm", action="store_true", help="附带线上 LLM 连通性检查(发真实请求)")
    parser.add_argument("--all", action="store_true", help="连被开关关掉的子系统也检")
    parser.add_argument("--allow-model-downloads", action="store_true",
                        help="放开 HF 下载(默认 HF_HUB_OFFLINE=1)")
    parser.add_argument("--timeout-scale", type=float, default=1.0)
    parser.add_argument("--force", action="store_true", help="Spica 运行中也强行 --full")
    parser.add_argument("--worker", default="", help=argparse.SUPPRESS)
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        # argparse 参数错误默认 exit 2, 与本脚本「2=有模型 FAIL」的约定冲突 --
        # 参数错误属自检自身错误, 统一 3; --help 的 SystemExit(0) 保持 0。
        return 0 if exc.code in (0, None) else 3

    if args.worker:
        return run_worker_and_print(args.worker)

    if not math.isfinite(args.timeout_scale) or args.timeout_scale <= 0:
        # nan/inf/<=0 would otherwise blow up AFTER spawning a worker and leak it
        print(f"[self-check] FATAL: --timeout-scale 非法: {args.timeout_scale}",
              file=sys.stderr)
        return 3

    try:
        app, screen_cfg, song_cfg, tts_cfg = _load_configs()
    except Exception as exc:  # noqa: BLE001 -- config broken = self-check cannot proceed
        print(f"[self-check] FATAL: 配置加载失败: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 3

    only = [s.strip() for s in args.only.split(",") if s.strip()]
    unknown = [s for s in only if s not in HEAVY_CHECKS]
    if unknown:
        print(f"[self-check] FATAL: --only 未知子系统 {unknown} (可选: {list(HEAVY_CHECKS)})",
              file=sys.stderr)
        return 3

    if not args.full:
        results = _light_results(app, screen_cfg, song_cfg, tts_cfg)
        if only:
            keep = set(only) | {"config", "gpu", "secrets"}
            results = [r for r in results if r["name"] in keep]
        print(render_report("light", results, args.json))
        return exit_code_for(results)

    if spica_appears_running() and not args.force:
        print("[self-check] FATAL: 检测到 Spica(qt_overlay) 正在运行。--full 会真加载模型并与"
              "应用争 GPU/显存——请先关闭应用，或用 --force 强行继续。", file=sys.stderr)
        return 3

    names = only or [n for n in HEAVY_CHECKS]
    enabled = _enabled_map(app, screen_cfg, song_cfg)
    results = run_full_checks(names, enabled, args)  # KI -> main() 统一转 exit 3
    print(render_report("full", results, args.json))
    return exit_code_for(results)


if __name__ == "__main__":
    sys.exit(main())

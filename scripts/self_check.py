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
  FAIL              跑不通(含超时; 进程树清理为 best-effort, 未确认时结果降级并注明)
  SKIPPED_DISABLED  被 enabled 开关关掉(--all 强制检查)
  UNVERIFIED        轻量档下无法不加载模型验证的项(只报事实, 不算失败)

exit code: 0=无 FAIL/DEGRADED; 1=有 DEGRADED; 2=有 FAIL; 3=自检自身错误/前置拒绝。

环境变量申明(doctor.py 纪律: scripts/ 在 no-getenv 扫描域之外, 但读了什么要在文件头写明):
  - 读: 各 secrets env 名的**在位与否**(值绝不打印/绝不进报告), 见 env_roster.SECRETS_ENV_MAP;
        OPENAI_BASE_URL / JUDGE_* 端点信息(--llm 时用于连通性检查);
        AUDIO_SEPARATOR_MODEL_DIR(镜像 audio-separator 的目录优先级做禁下载预检)。
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
import re
import secrets as _stdlib_secrets
from collections import deque
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


def parse_worker_stdout(stdout: str, nonce: str | None = None) -> dict[str, Any] | None:
    """Last RESULT_MARKER wins -- heavy libraries print freely before it.
    The marker is matched ANYWHERE in a line, not just at column 0: a library's
    final write without a trailing newline glues the worker's print onto the
    same line ("noiseSELF_CHECK_RESULT_JSON:{...}"). ``nonce`` 由父进程按次生成
    并经 env 传给 worker: 提供时只认带 nonce 的 marker -- 后置输出的公开 marker
    (插件 atexit 等)不能覆盖真实结果(第八轮 review P2)。"""
    marker = RESULT_MARKER + (nonce + ":" if nonce else "")
    payload = None
    for line in stdout.split("\n"):  # 只认真实 \n: U+0085/U+2028/U+2029 是合法
        # JSON 内容, Unicode-aware splitlines 会把 payload 拆坏(第十一轮 P2-5)
        # 行内**左起**逐个 marker 尝试整段解析(第七轮 review P2): payload 字符串
        # 里合法出现的 marker 位于 JSON 内部, 从真 marker 起整段 loads 天然正确;
        # rfind 会从字符串内的假 marker 起把 JSON 拦腰截断。解析失败绝不覆盖
        # 此前的合法结果; 多个真 marker 时最后一个合法者获胜。
        start = 0
        while True:
            index = line.find(marker, start)
            if index == -1:
                break
            try:
                payload = json.loads(line[index + len(marker):])
                break
            except json.JSONDecodeError:
                start = index + 1
    return payload


def _secret_value_pairs(environ: dict[str, str]) -> list[tuple[str, str]]:
    from spica.config.env_roster import SECRETS_ENV_MAP

    pairs = [(name, environ.get(name) or "") for name in SECRETS_ENV_MAP.values()]
    # 任何非空值都处理(短密码同样是密码, 第七轮 review P1); 长值先替换,
    # 防止短值恰是长值子串时先替换把长值拆碎。
    return sorted(((n, v) for n, v in pairs if v), key=lambda p: len(p[1]), reverse=True)


def redact_secrets(text: str, environ: dict[str, str] | None = None) -> str:
    """把已知 secret 的 VALUE 从文本里抹掉(值→«REDACTED:ENV名», 定长占位,
    不泄漏长度或片段)。在**序列化之前**的原始字符串上做——序列化后的 replace
    对含引号/反斜杠的值失效(dumps 转义后搜不到原值)。"""
    env = environ if environ is not None else os.environ
    for env_name, value in _secret_value_pairs(env):
        token = f"«REDACTED:{env_name}»"
        variants = {value}
        try:  # repr/unicode_escape 与 JSON 转义形式同样能还原 secret(第十轮 P1)
            variants.add(value.encode("unicode_escape").decode("ascii"))
            variants.add(json.dumps(value)[1:-1])
        except Exception:  # noqa: BLE001
            pass
        for variant in sorted(variants, key=len, reverse=True):
            if variant and variant in text:
                text = text.replace(variant, token)
    return text


# 固定协议键(第九轮 P2): secret 恰为 "status"/"reason" 这类词时不得改写 schema
# (值照常清洗)。
_PROTOCOL_KEYS = frozenset({
    "mode", "results", "exit_code", "name", "status", "reason", "detail",
    "duration_s",
})


def redact_obj(value: Any, environ: dict[str, str] | None = None,
               _key: str | None = None) -> Any:
    """递归结构化脱敏: 报告/worker payload 的每个字符串**键与值**在进入
    json.dumps 之前清洗(第七/八轮 review P1: 序列化后 replace 对转义值失效;
    secret 作为 dict 键同样泄漏)。唯一例外是协议字段: ``status`` 的合法枚举值
    原样保留 -- 否则 QBITTORRENT_PASSWORD=PASS 这类常见词密码会把合法状态替换
    成占位符, 击穿五态/exit-code 契约(第八轮 P2; 此时 reason/detail 里的同词
    仍会被替换)。"""
    if isinstance(value, str):
        if _key == "status" and value in VALID_STATUSES:
            return value
        return redact_secrets(value, environ)
    if isinstance(value, dict):
        return {
            (key if (isinstance(key, str) and key in _PROTOCOL_KEYS)
             else redact_secrets(key, environ) if isinstance(key, str) else key):
            redact_obj(item, environ, _key=key if isinstance(key, str) else None)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact_obj(item, environ) for item in value]
    return value


def render_report(mode: str, results: list[dict[str, Any]], as_json: bool) -> str:
    results = redact_obj(results)  # 序列化前结构化清洗(两种格式共用)
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


class SelfCheckInternalError(RuntimeError):
    """自检自身的基础设施错误(如无法 spawn worker) -- 统一 exit 3, 不与
    「1=DEGRADED / 2=FAIL」的模型结论码混淆。"""


MAX_CHECK_TIMEOUT_S = 86_400.0  # 单项超时上限: 乘积必须有限且可执行(P2-4)

_STDOUT_TAIL_LINES = 200
_STDERR_TAIL_LINES = 50
_LINE_CAP_CHARS = 1_000_000

# POSIX 生命周期容器(第七轮 review P1): killpg 只覆盖同 PGID, setsid 脱组的
# 孙进程杀不到 -- 进程组 != 进程树。本 supervisor 以 PR_SET_CHILD_SUBREAPER
# 作为后代孤儿的归养点: worker 无论正常退出还是崩溃, 其(任意深度、任意会话的)
# 遗孤都会在各自父进程死亡时归养到 supervisor, 由它逐层清扫后再以 worker 的
# rc 退出。正常路径(supervisor 存活)下确定性; supervisor 自身被 SIGKILL 属
# best-effort waiver(父端快照补杀 + 如实报清理未确认)。绝不向 stdout 写任何东西(marker 通道)。
_POSIX_SUPERVISOR = r"""
import ctypes, os, signal, subprocess, sys, time
try:
    # 父进程 spawn 窗口的 SIG_BLOCK 掩码会被继承(信号掩码跨 fork/exec 保留):
    # 不解除的话 supervisor 永远收不到 SIGTERM, 超时清理只能走 SIGKILL 满额等待。
    signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGTERM, signal.SIGINT})
except (ValueError, OSError):
    pass
EXPECTED_PARENT = int(sys.argv[1])
try:
    libc = ctypes.CDLL(None, use_errno=True)
    rc_sub = libc.prctl(36, 1, 0, 0, 0)   # PR_SET_CHILD_SUBREAPER
    rc_pd = libc.prctl(1, signal.SIGTERM, 0, 0, 0)  # PR_SET_PDEATHSIG
except Exception as exc:  # libc/prctl 不存在也不崩: 降级继续检查(第十一轮)
    rc_sub = rc_pd = -1
if rc_sub != 0 or rc_pd != 0:
    # 哨兵供父进程识别 -> 结果强制 <=DEGRADED(containment 降级, 检查照做)
    print("SELF_CHECK_SUPERVISOR_PRCTL_DEGRADED: sub=%s pd=%s" % (rc_sub, rc_pd),
          file=sys.stderr, flush=True)
worker = None
TERMINATED = False
def _on_term(_s, _f):
    global TERMINATED
    TERMINATED = True
    if worker is not None:
        worker.kill()
signal.signal(signal.SIGTERM, _on_term)
if os.getppid() != EXPECTED_PARENT:
    # PDEATHSIG 装载与父死亡之间的竞态: arm 后复核, 父已死则不再拉起 worker
    sys.exit(96)
try:
    worker = subprocess.Popen(sys.argv[2:])
except OSError as exc:
    print("SELF_CHECK_SUPERVISOR_SPAWN_ERROR: %s" % exc, file=sys.stderr)
    sys.exit(97)
if TERMINATED:
    worker.kill()  # SIGTERM 落在 spawn 赋值窗口内: flag 兜底
rc = worker.wait()
ME = os.getpid()
try:
    MY_PG = os.getpgid(0)
except Exception:
    MY_PG = None
def _reap():
    while True:
        try:
            pid, _ = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return
        if pid == 0:
            return
def _children():
    kids = []
    try:  # /proc 优先: 不依赖 PATH/ps 二进制, 不把枚举进程自己算进来, 跳过僵尸
        entries = os.listdir("/proc")
        for entry in entries:
            if not entry.isdigit():
                continue
            try:
                with open("/proc/%s/stat" % entry, "rb") as fh:
                    stat = fh.read().decode("ascii", "replace")
                after = stat.rsplit(")", 1)[1].split()
                state, ppid = after[0], int(after[1])
            except Exception:
                continue
            if ppid == ME and state != "Z":
                kids.append(int(entry))
        return kids
    except Exception:
        pass
    try:  # 非 Linux 回退: ps, 排除 ps 进程自身(否则每轮都误判有孩子空转到 deadline)
        proc = subprocess.Popen(["ps", "-eo", "pid,ppid"], stdout=subprocess.PIPE, text=True)
        out = proc.communicate(timeout=5)[0]
        for line in out.splitlines()[1:]:
            parts = line.split()
            if (len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit()
                    and int(parts[1]) == ME and int(parts[0]) != proc.pid):
                kids.append(int(parts[0]))
    except Exception:
        pass
    return kids
deadline = time.time() + 10
while time.time() < deadline:
    _reap()
    kids = _children()
    if not kids:
        break
    for kid in kids:
        try:
            pg = os.getpgid(kid)
        except Exception:
            pg = None
        try:
            if pg is not None and MY_PG is not None and pg != MY_PG:
                os.killpg(pg, signal.SIGKILL)  # 脱组后代: 连它的组一起清
            else:
                os.kill(kid, signal.SIGKILL)   # 同组 helper: 绝不 killpg(会把自己杀掉)
        except Exception:
            pass
    time.sleep(0.05)
sys.exit(rc if isinstance(rc, int) else 1)
"""


def _windows_job_container(pid: int) -> tuple[Any, bool]:
    """(job_handle|None, guaranteed): Job Object + KILL_ON_JOB_CLOSE(MS: Job
    Objects) -- Windows 上结束父进程不会自动结束子进程, 只有 Job 能按组终止。
    任一 API 失败返回 (None, False), 调用方必须如实报告「无法保证」。"""
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.windll.kernel32
        # 显式 argtypes/restype(第八轮 review): 64 位 HANDLE 缺声明会被默认
        # c_int 截断。见 Python ctypes 文档与 CreateJobObjectW API。
        kernel32.CreateJobObjectW.restype = ctypes.c_void_p
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.SetInformationJobObject.argtypes = [
            ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        kernel32.TerminateJobObject.restype = wintypes.BOOL
        kernel32.TerminateJobObject.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return None, False

        class _IoCounters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

        class _BasicLimits(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64),
                        ("PerJobUserTimeLimit", ctypes.c_int64),
                        ("LimitFlags", wintypes.DWORD),
                        ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t),
                        ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_void_p),
                        ("PriorityClass", wintypes.DWORD),
                        ("SchedulingClass", wintypes.DWORD)]

        class _ExtendedLimits(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", _BasicLimits),
                        ("IoInfo", _IoCounters),
                        ("ProcessMemoryLimit", ctypes.c_size_t),
                        ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t),
                        ("PeakJobMemoryUsed", ctypes.c_size_t)]

        info = _ExtendedLimits()
        info.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(job, 9, ctypes.byref(info),
                                                ctypes.sizeof(info)):
            kernel32.CloseHandle(job)
            return None, False
        handle = kernel32.OpenProcess(0x0101, False, pid)  # SET_QUOTA|TERMINATE
        if not handle:
            kernel32.CloseHandle(job)
            return None, False
        assigned = kernel32.AssignProcessToJobObject(job, handle)
        kernel32.CloseHandle(handle)
        if not assigned:
            kernel32.CloseHandle(job)
            return None, False
        return job, True
    except Exception:
        return None, False


def _pid_starttime(pid: int) -> str | None:
    """/proc/<pid>/stat 第 22 字段(starttime)作为 PID 身份指纹: 补杀前校验,
    PID 被复用则不杀并报清理未确认(第十轮: 不误杀无关进程组)。非 Linux 返回 None。"""
    try:
        with open(f"/proc/{pid}/stat", "rb") as fh:
            stat = fh.read().decode("ascii", "replace")
        return stat.rsplit(")", 1)[1].split()[19]  # 括号后第 20 个字段 = starttime
    except Exception:  # noqa: BLE001
        return None


def _win_kernel32() -> Any:
    """kernel32 获取点(测试注入缝)。Linux 上返回 None。"""
    try:
        import ctypes

        return ctypes.windll.kernel32  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        return None


def _cleanup_tree(proc: subprocess.Popen, job: Any,
                  tracked_pids: set | None = None) -> tuple[bool, str]:
    """尽力清掉整棵 worker 进程树; 返回 (确认清理, 说明)。失败必须让调用方
    如实上报, 不允许静默声称 process tree killed(P2-4)。"""
    if os.name == "nt":
        if job is None:
            try:  # 兜底本身的异常不得越过 cleanup 契约(第十轮 P2)
                subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                               capture_output=True, timeout=15)
            except Exception as exc:  # noqa: BLE001
                return False, f"Windows 无 Job Object 且 taskkill 兜底失败: {exc}"
            return False, "Windows 无 Job Object -- 已 taskkill /T 兜底, 完整性无法保证"
        try:
            kernel32 = _win_kernel32()
            if kernel32 is None:
                return False, "kernel32 不可用 -- Job 清理未确认"
            import ctypes
            from ctypes import wintypes

            kernel32.TerminateJobObject.restype = wintypes.BOOL
            kernel32.TerminateJobObject.argtypes = [ctypes.c_void_p, ctypes.c_uint]
            kernel32.CloseHandle.restype = wintypes.BOOL
            kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
            terminated = bool(kernel32.TerminateJobObject(job, 1))
            closed = bool(kernel32.CloseHandle(job))  # KILL_ON_JOB_CLOSE 双保险
            proc.wait(timeout=15)
            if terminated and closed:
                return True, "job object terminated"
            # 返回值失败必须如实上报(第八轮 review), 不冒充已清理
            return False, (f"Job API 返回失败(terminate={terminated}, close={closed})"
                           " -- 进程树清理未确认")
        except Exception as exc:  # noqa: BLE001
            return False, f"TerminateJobObject 失败: {exc}"
    try:
        os.kill(proc.pid, signal.SIGTERM)  # supervisor: 杀 worker + 清扫归养孤儿
    except ProcessLookupError:
        # supervisor 已死: 正常退出=它已清扫; 但若是被 SIGKILL/OOM 干掉则没有
        # (第九轮 P1)。按父进程追踪到的后代快照补杀, 结果如实。
        leftovers = []
        identity_mismatch = 0
        for pid, expected_start in sorted((tracked_pids or {}).items()):
            if pid == proc.pid:
                continue
            try:
                os.kill(pid, 0)
            except (ProcessLookupError, PermissionError):
                continue
            current_start = _pid_starttime(pid)
            if (expected_start is None or current_start is None
                    or current_start != expected_start):
                # 身份未知或不符都不杀(第十一轮 P2-3): 宁漏杀不误杀, 如实上报
                identity_mismatch += 1
                continue
            leftovers.append(pid)
            try:
                os.killpg(os.getpgid(pid), signal.SIGKILL)
            except Exception:  # noqa: BLE001
                try:
                    os.kill(pid, signal.SIGKILL)
                except Exception:  # noqa: BLE001
                    pass
        if leftovers or identity_mismatch:
            return False, (f"supervisor 异常死亡: 按快照补杀 {len(leftovers)} 个残留, "
                           f"{identity_mismatch} 个因身份未知/不符跳过 -- 清理未确认"
                           "(快照窗口外的后代无法覆盖, best-effort)")
        rc = proc.poll()
        if rc is not None and rc < 0:
            # 被信号杀死(SIGKILL 等)的 supervisor 没机会清扫; 快照又为空 --
            # 不得返回"swept"(第十一轮 P2-3), 如实报清理未确认(waiver: best-effort)。
            return False, (f"supervisor 被信号终止(rc={rc})且快照无可补杀对象 -- "
                           "清理未确认(best-effort waiver)")
        return True, "supervisor already exited (swept on its way out)"
    except Exception as exc:  # noqa: BLE001
        return False, f"SIGTERM supervisor 失败: {exc}"
    try:
        proc.wait(timeout=15)
        return True, "supervisor swept and exited"
    except subprocess.TimeoutExpired:
        pass
    except Exception as exc:  # noqa: BLE001
        return False, f"等待 supervisor 失败: {exc}"
    try:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait(timeout=5)
        return False, "supervisor 未按时清扫, 已 SIGKILL 其进程组(脱组后代可能残留)"
    except Exception as exc:  # noqa: BLE001
        return False, f"强杀 supervisor 组失败: {exc}"


def _drain_stream(stream: Any, tail: deque, marker_lines: list | None,
                  marker: str = RESULT_MARKER) -> None:
    """有界流式读取(P3): 只保留尾部 ring buffer 和 marker 行, 大量输出不撑爆
    内存也不死锁(readers 持续排空管道)。流层**不做**脱敏(会打碎 marker/JSON/
    哨兵); 结构化 payload 走 redact_obj, crash 路径不携带 stderr 原文(第十一轮
    止战方案)。marker 候选按**带 nonce 的**
    marker 过滤(第九轮 P2: 明文 marker 洪水不得把真结果挤出候选窗)。"""
    try:
        for raw in iter(stream.readline, ""):
            line = raw.rstrip("\n")[:_LINE_CAP_CHARS]  # 流层不脱敏(会打碎 marker
            # JSON/哨兵, 第十轮 P2); 脱敏在结构化层与 stderr 重组点做
            tail.append(line)
            if marker_lines is not None and marker in line:
                marker_lines.append(line)
                del marker_lines[:-4]  # 只留最后几条 marker 候选
    except Exception:  # noqa: BLE001 -- reader 永不抛到主线程
        pass
    finally:
        try:
            stream.close()
        except Exception:  # noqa: BLE001
            pass


def run_subprocess_check(
    cmd: list[str],
    timeout_s: float,
    env: dict[str, str] | None = None,
    sample_vram: bool = True,
) -> dict[str, Any]:
    """Run one worker command; returns a result dict (status/detail/reason).

    Trust rules (2026-07 review): a nonzero return code is ALWAYS a FAIL, even
    after a PASS report (crash-after-report); an unknown status string is a
    FAIL, never a silent exit-0. POSIX worker 经 subreaper supervisor 包裹
    (进程树保证); Windows 经 Job Object(不可用时如实报告无法保证)。"""
    if not (isinstance(timeout_s, (int, float)) and math.isfinite(timeout_s)
            and 0 < timeout_s <= MAX_CHECK_TIMEOUT_S):
        raise SelfCheckInternalError(
            f"非法 worker 超时: {timeout_s!r} (须有限且 0<t<={MAX_CHECK_TIMEOUT_S:.0f}s)")
    started = time.time()
    if os.name == "nt":
        popen_kwargs: dict[str, Any] = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
        full_cmd = list(cmd)
    else:
        popen_kwargs = {"start_new_session": True}
        full_cmd = [sys.executable, "-c", _POSIX_SUPERVISOR, str(os.getpid()), *cmd]
    # 本次运行的 marker nonce(第八轮 review P2): 后置输出的公开 marker(插件
    # atexit 等)无法伪造带 nonce 的真实结果通道。同进程恶意代码仍可读 env --
    # 防的是意外/静态输出欺骗, 不是进程内对抗(固有边界)。
    nonce = _stdlib_secrets.token_hex(8)
    env = dict(env if env is not None else os.environ)
    env["SPICA_SELF_CHECK_MARKER_NONCE"] = nonce
    # spawn/赋值窗口对 SIGTERM/SIGINT 原子化(第十轮 P1): handler 在 proc 赋值前
    # 触发会让已启动的 supervisor 无引用可清理。仅 POSIX 主线程可 mask。
    original_mask = None
    if os.name != "nt":
        try:  # 保存原掩码, 出口 SIG_SETMASK 精确恢复(第十一轮 P2: 不覆写调用方语义)
            original_mask = signal.pthread_sigmask(
                signal.SIG_BLOCK, {signal.SIGTERM, signal.SIGINT})
        except (ValueError, OSError):
            original_mask = None
    try:
        proc = subprocess.Popen(
            full_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            encoding="utf-8", errors="replace",  # 非法字节不再杀死 reader(P2)
            cwd=str(REPO_ROOT), env=env, **popen_kwargs,
        )
    except OSError as exc:
        if original_mask is not None:
            signal.pthread_sigmask(signal.SIG_SETMASK, original_mask)
        raise SelfCheckInternalError(f"无法启动 worker 子进程: {exc}") from exc
    job: Any = None
    tracked: dict = {}
    stdout_tail: deque = deque(maxlen=_STDOUT_TAIL_LINES)
    stderr_tail: deque = deque(maxlen=_STDERR_TAIL_LINES)
    marker_lines: list = []
    vram: dict[str, Any] = {}
    stop = threading.Event()
    sampler = None
    try:
        # spawn 之后的所有基础设施初始化(job/readers/sampler)都在清理保护内:
        # Thread.start 的资源错误曾逃逸为原生 exit 1 且不清理 worker(第八轮 P1)。
        if os.name == "nt":
            job, _job_ok = _windows_job_container(proc.pid)
        nonced_marker = RESULT_MARKER + nonce + ":"
        readers = [
            threading.Thread(target=_drain_stream,
                             args=(proc.stdout, stdout_tail, marker_lines, nonced_marker),
                             daemon=True),
            threading.Thread(target=_drain_stream,
                             args=(proc.stderr, stderr_tail, None), daemon=True),
        ]
        for reader in readers:
            reader.start()
        # 后代快照追踪(第九轮 P1): supervisor 被 SIGKILL/OOM 干掉时, 父进程凭
        # 此快照补杀残留(快照窗口外的如实报告)。
        def _snapshot() -> None:
            for pid in _descendant_pids(proc.pid):
                if pid not in tracked:
                    tracked[pid] = _pid_starttime(pid)  # 记录身份, 防 PID 复用误杀

        def _track() -> None:
            while not stop.is_set():
                try:
                    _snapshot()
                except Exception:  # noqa: BLE001
                    pass
                stop.wait(0.3)

        try:
            _snapshot()  # 立即首采样: 把 supervisor 秒死的窗口压到毫秒级
        except Exception:  # noqa: BLE001
            pass
        tracker = threading.Thread(target=_track, daemon=True)
        tracker.start()
        if sample_vram:
            sampler = threading.Thread(
                target=_vram_sampler, args=(proc.pid, stop, vram), daemon=True
            )
            sampler.start()
        if original_mask is not None:
            # 解除掩码放在 cleanup 保护域内(第十一轮 P2-1): pending SIGTERM 在
            # 此抛 KI -> 下方 BaseException 先清树再传播, 嵌入式调用不遗留。
            signal.pthread_sigmask(signal.SIG_SETMASK, original_mask)
            original_mask = None
    except BaseException as exc:
        _cleanup_tree(proc, job, tracked)
        if isinstance(exc, Exception):
            raise SelfCheckInternalError(f"worker 基础设施初始化失败: {exc}") from exc
        raise
    finally:
        if original_mask is not None:  # 兜底恢复(幂等)
            try:
                signal.pthread_sigmask(signal.SIG_SETMASK, original_mask)
            except (ValueError, OSError):
                pass
    cleaned: tuple[bool, str] | None = None
    try:
        try:
            proc.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            cleaned = _cleanup_tree(proc, job, tracked)
            note = ("process tree killed" if cleaned[0]
                    else f"清理未确认: {cleaned[1]}")
            return {
                "status": STATUS_FAIL,
                "reason": f"timeout after {timeout_s:.0f}s ({note})",
                "detail": dict(vram),
            }
        except BaseException:
            # Ctrl-C / any parent failure: worker 在独立会话里收不到终端 SIGINT
            cleaned = _cleanup_tree(proc, job, tracked)
            raise
    finally:
        stop.set()
        if sampler is not None:
            sampler.join(timeout=2)
        if cleaned is None:
            # 正常退出/崩溃路径的树清理: supervisor 正常时早已清扫(此处 no-op
            # 确认); 失败必须体现在结果里, 不静默(P2-4)。
            cleaned = _cleanup_tree(proc, job, tracked)
        for reader in readers:
            reader.join(timeout=10)
    cleanup_ok, cleanup_note = cleaned
    duration = round(time.time() - started, 1)

    def _infra_detail() -> dict[str, Any]:
        infra: dict[str, Any] = {"duration_s": duration, **vram}
        if not cleanup_ok:
            infra["tree_cleanup"] = cleanup_note
        return infra

    if proc.returncode == 97 and any(
            "SELF_CHECK_SUPERVISOR_SPAWN_ERROR" in line for line in stderr_tail):
        # 内层 worker spawn 失败 = 自检基础设施错误(第九轮 P2), 不是模型 FAIL
        raise SelfCheckInternalError(
            "无法启动内层 worker 子进程: "
            + " | ".join(l for l in stderr_tail if "SPAWN_ERROR" in l)[:300])
    stdout_text = "\n".join(marker_lines if marker_lines else list(stdout_tail))
    payload = parse_worker_stdout(stdout_text, nonce=nonce)
    if payload is None:
        # 止战方案(第十一轮 P1): crash/no-marker 路径不携带任何 raw stderr 原文
        # (repr/CR 归一化/超窗多行 secret 的变体军备赛就此终结), 只报固定元数据。
        # 结构化 worker payload 照走 redact_obj。
        stderr_lines = sum(1 for line in stderr_tail if line.strip())
        detail = _infra_detail()
        detail["stderr_lines_captured"] = stderr_lines
        return {
            "status": STATUS_FAIL,
            "reason": f"worker exited rc={proc.returncode} without a result "
                      f"(stderr: {stderr_lines} 行已捕获, 原文不进报告)",
            "detail": detail,
        }
    if not isinstance(payload, dict):
        # a legal-but-non-object JSON (array/string) must become a FAIL, not an
        # AttributeError that crashes the CLI with an unspecified exit code.
        return {
            "status": STATUS_FAIL,
            "reason": f"worker result is not a JSON object: {type(payload).__name__}",
            "detail": _infra_detail(),
        }
    if proc.returncode != 0:
        return {
            "status": STATUS_FAIL,
            "reason": f"worker exited rc={proc.returncode} AFTER reporting "
                      f"{payload.get('status')!r} -- crash-after-report is a FAIL",
            "detail": _infra_detail(),
        }
    status = str(payload.get("status") or "")
    if status not in VALID_STATUSES:
        return {
            "status": STATUS_FAIL,
            "reason": f"worker returned invalid status {payload.get('status')!r} "
                      f"(expected one of {sorted(VALID_STATUSES)})",
            "detail": _infra_detail(),
        }
    raw_detail = payload.get("detail")
    if raw_detail is not None and not isinstance(raw_detail, dict):
        # nested-shape violation ({"detail": "oops"}) must be a FAIL, not a
        # ValueError that crashes the CLI with an unspecified exit code.
        return {
            "status": STATUS_FAIL,
            "reason": f"worker detail is not a JSON object: {type(raw_detail).__name__}",
            "detail": _infra_detail(),
        }
    detail = dict(raw_detail or {})
    detail.setdefault("duration_s", payload.get("duration_s", duration))
    detail.update(vram)
    reason = str(payload.get("reason") or "")
    if any("SELF_CHECK_SUPERVISOR_PRCTL_DEGRADED" in line for line in stderr_tail):
        # containment 降级(prctl 不可用): 检查结论保留, 但绿色状态不可信
        detail["containment"] = "prctl 不可用 -- 进程树清理保证降级(best-effort)"
        if status in (STATUS_PASS, STATUS_UNVERIFIED, STATUS_SKIPPED):
            status = STATUS_DEGRADED
            reason = (reason + "; " if reason else "") + "containment 降级(prctl 不可用)"
    if not cleanup_ok:
        detail["tree_cleanup"] = cleanup_note  # 明确报告, 不静默声称已清理
        if status in (STATUS_PASS, STATUS_UNVERIFIED, STATUS_SKIPPED):
            # 清理未确认时任何"绿色"结论都不可信(第十轮 P1): 统一降 DEGRADED;
            # 原本已是 DEGRADED/FAIL 的不降低。
            status = STATUS_DEGRADED
            reason = (reason + "; " if reason else "") + f"进程树清理未确认: {cleanup_note}"
    return {"status": status, "reason": reason, "detail": detail}


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
    try:  # 缺包 = 本地 STT 任何形态都跑不了(第十轮 P2: 不只裸 size 分支)
        import faster_whisper  # noqa: F401,PLC0415
    except ImportError:
        return {"name": "stt", "status": STATUS_DEGRADED,
                "detail": {"model": cfg.model, "device": cfg.device,
                           "compute_type": cfg.compute_type},
                "reason": "faster-whisper 未安装, 本地 STT 无法运行"}
    model_path = Path(cfg.model)
    if not model_path.is_absolute():
        model_path = REPO_ROOT / cfg.model
    model_is_dir = model_path.is_dir()
    detail = {"model": cfg.model, "device": cfg.device, "compute_type": cfg.compute_type,
              "local_model_dir": model_is_dir}
    if model_is_dir:
        if not (model_path / "model.bin").is_file():
            # 目录在但缺 CTranslate2 布局的 model.bin: 真加载立即失败(第九轮 P2)
            return {"name": "stt", "status": STATUS_DEGRADED, "detail": detail,
                    "reason": f"本地模型目录缺 model.bin: {model_path}"}
        return {"name": "stt", "status": STATUS_UNVERIFIED, "detail": detail,
                "reason": "轻量档不加载模型(--full 真跑)"}
    # faster-whisper 的 model 可以是尺寸名或 org/name Hub ID -- 这类值目录不存
    # 在是正常的(走 HF 缓存)。但: ①显式本地路径(./ ../ 绝对/反斜杠)不能误判成
    # Hub ID; ②裸名必须在 faster-whisper 的官方 size 表里(未知裸名离线即
    # ValueError, 第八轮 P2); ③HF 段不得以 . 或 - 开头(-bad/model、org/.bad
    # 必然非法)。
    looks_explicit_path = (
        Path(cfg.model).is_absolute()
        or cfg.model.startswith(("./", "../", ".\\", "..\\"))
        or "\\" in cfg.model
    )
    if not looks_explicit_path:
        if "/" not in cfg.model:
            # 裸名必须在 faster-whisper 官方 size 表(未知裸名离线即 ValueError);
            # faster-whisper 本身装不上 = 本地 STT 根本跑不了(第九轮 P2)。
            try:
                from faster_whisper.utils import _MODELS  # noqa: PLC0415
            except ImportError:
                return {"name": "stt", "status": STATUS_DEGRADED, "detail": detail,
                        "reason": "faster-whisper 未安装, 本地 STT 无法运行"}
            if cfg.model not in _MODELS:
                return {"name": "stt", "status": STATUS_DEGRADED, "detail": detail,
                        "reason": f"不是合法的 faster-whisper size 名: {cfg.model}"
                                  "(加载时会直接 ValueError)"}
            detail["local_model_dir"] = "n/a(size 名)"
            return {"name": "stt", "status": STATUS_UNVERIFIED, "detail": detail,
                    "reason": "轻量档不加载模型(--full 真跑)"}
        # org/name: 用官方 validator(第九轮 P2: 自造正则对 _org/name 假红、对
        # org/name./org/na--me/.git 假绿), 官方不可用时如实标注无法判定。
        try:
            from huggingface_hub.utils import validate_repo_id  # noqa: PLC0415

            validate_repo_id(cfg.model)
        except ImportError:
            detail["local_model_dir"] = "n/a(HF hub id, 未经官方校验)"
            return {"name": "stt", "status": STATUS_UNVERIFIED, "detail": detail,
                    "reason": "huggingface_hub 不可用, ID 合法性未校验"}
        except Exception as exc:  # noqa: BLE001 -- HFValidationError 家族
            return {"name": "stt", "status": STATUS_DEGRADED, "detail": detail,
                    "reason": f"非法 HF repo id: {cfg.model} ({exc})"[:200]}
        detail["local_model_dir"] = "n/a(HF hub id)"
        return {"name": "stt", "status": STATUS_UNVERIFIED, "detail": detail,
                "reason": "轻量档不加载模型(--full 真跑)"}
    return {"name": "stt", "status": STATUS_DEGRADED, "detail": detail,
            "reason": f"本地模型目录不存在或模型名非法: {model_path}"}


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


def _register_host_tool_stubs(registry: Any, errors: dict[str, str]) -> None:
    """生产在插件加载前还注册了三个 host 闭包工具(watch/note/sing_song, 权限在
    AppHost)。自检没有 host 实例, 注册**同 schema 的拒绝执行 stub**(第九轮 P2):
    插件对「工具是否存在」的依赖与生产等价; 若插件真去调用, 得到明确拒绝。"""
    try:
        from agent_tools.function_tools.screen.schema import ScreenToolError
        from spica.adapters.tools.note_game_observation import NoteGameObservationTool
        from spica.adapters.tools.sing_song import SingSongTool
        from spica.adapters.tools.watch_game_screen import WATCH_GAME_SCREEN_SCHEMA

        def _refuse(**_kwargs: Any) -> dict[str, Any]:
            raise ScreenToolError(
                "SELF_CHECK_STUB", "自检环境无 AppHost, host 闭包工具不可执行。")

        registry.register_tool(WATCH_GAME_SCREEN_SCHEMA, _refuse,
                               available=lambda: False, intent_gated=False)
        registry.register_tool(
            NoteGameObservationTool(lambda: None, lambda *a, **k: None).schema(),
            _refuse, available=lambda: False, intent_gated=False, effect="write")
        def _song_enabled_like_production() -> bool:
            try:
                from agent_tools.function_tools.song.config import (
                    resolve_effective_song_config,
                    song_enabled,
                )

                return song_enabled(resolve_effective_song_config())
            except Exception:  # noqa: BLE001
                return False

        # available 镜像生产谓词(第十轮 P2: 插件经公共 tool_schemas() 依赖
        # sing_song 的 offered 状态时, 自检必须与生产一致)
        registry.register_tool(SingSongTool(lambda q: None).schema(), _refuse,
                               available=_song_enabled_like_production,
                               intent_gated=True, effect="act")
    except Exception as exc:  # noqa: BLE001 -- stub 失败降级并记录
        errors.setdefault("<host-tool-stubs>", str(exc))


def _tts_check_registry(
    plugins_root: Any = None, manifest_path: Any = None, screen_config: Any = None
) -> tuple[Any, dict[str, str]]:
    """(registry, plugin_errors): 生产 capability catalogue 的轻量切片
    (register_core_capability_catalogue, 单一来源, 零对象构造) + 生产同款插件
    注册 -- 插件的 register() 可以像生产一样依赖 LLM 等 builtin(第七轮 review
    P2)。插件错误如实返回(PluginHost.errors()), 由调用方在 provider 解析失败时
    带进 FAIL 结论; 单个坏插件不摧毁检查。"""
    from spica.host.builtins import (
        register_builtin_adapters,
        register_core_capability_catalogue,
    )
    from spica.plugins.registry import CapabilityRegistry

    registry = CapabilityRegistry()
    errors: dict[str, str] = {}
    try:
        # 生产同款完整内建目录(含 inspect_screen 工具, 第八轮 review P2: 插件
        # 可以依赖它)。host 闭包工具(watch/note/sing_song)以同 schema 拒执行 stub
        # 提供(见 _register_host_tool_stubs), offered 状态与生产对齐。
        register_builtin_adapters(registry, screen_config=screen_config)
    except Exception as exc:  # noqa: BLE001 -- 无关内建失败不殃及 TTS 结论
        errors["<builtins>"] = str(exc)
        registry = CapabilityRegistry()
        register_core_capability_catalogue(registry)
    _register_host_tool_stubs(registry, errors)
    try:
        from spica.plugins.host import PluginHost  # noqa: PLC0415

        kwargs: dict[str, Any] = {}
        if plugins_root is not None:
            kwargs["plugins_root"] = plugins_root
        if manifest_path is not None:
            kwargs["manifest_path"] = manifest_path
        plugin_host = PluginHost(registry, **kwargs)
        plugin_host.load()
        errors.update(plugin_host.errors())  # merge, 不覆盖 <builtins> 记录(第九轮 P3)
    except Exception as exc:  # noqa: BLE001 -- degrade, never fake a TTS FAIL
        errors.setdefault("<plugin-host>", str(exc))  # merge, 不覆盖 <builtins>
        print(redact_secrets(f"[self-check] 插件注册失败(继续用内建目录): {exc}"),
              file=sys.stderr)
    return registry, errors


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
    # __new__ 而非 AppHost(): __init__ 会构造 screen 工具等无关件, 它们的异常
    # 会在进入 TTS 装配缝之前把结果记成 TTS FAIL(review P2)。缝只读 config 和
    # registry 两个属性; registry = builtins TTS 切片 + 插件注册(生产对齐)。
    host = AppHost.__new__(AppHost)
    host.config = ConfigManager().load()
    from agent_tools.function_tools.screen.config import resolve_effective_screen_config

    host.registry, plugin_errors = _tts_check_registry(
        screen_config=resolve_effective_screen_config())  # 生产同源(第十一轮 P2-6)
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
    try:
        provider, _tool, adapter = host._resolve_tts_assembly(tts_config)
    except (KeyError, ValueError) as exc:
        # 配置的 provider 解析失败: 若配置依赖的插件恰好注册失败, 把准确原因
        # 带出来(经 worker 出口结构化脱敏), 不静默继续到 KeyError 才裸崩。
        reason = f"配置的 TTS provider 解析失败: {exc}"
        if plugin_errors:
            reason += f"; 插件错误: {plugin_errors}"
        return {"status": STATUS_FAIL, "reason": reason,
                "detail": {"plugin_errors": plugin_errors}}
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


def uvr_effective_model_dir(
    sep_config: dict[str, Any], environ: dict[str, str] | None = None
) -> str | None:
    """audio-separator 实际读模型的目录, 优先级严格镜像 0.44.2 的 Separator
    (separator.py:167): ①AUDIO_SEPARATOR_MODEL_DIR env **优先于** kwarg;
    ②extra_kwargs.model_file_dir(生产 separate_vocals 透传); ③探测默认目录
    (此路径才构造 Separator; ①②时绝不探测)。None = 无法确定(预检放行,
    由真跑自己失败)。"""
    env = environ if environ is not None else os.environ
    env_dir = env.get("AUDIO_SEPARATOR_MODEL_DIR")
    if env_dir:
        return str(env_dir)
    extra = sep_config.get("extra_kwargs") or {}
    override = extra.get("model_file_dir")
    if override:
        return str(override)
    try:
        from audio_separator.separator import Separator  # noqa: PLC0415

        probe = Separator(output_dir=str(REPO_ROOT / "static/generated_song/tmp"))
        model_dir = getattr(probe, "model_file_dir", None)
        return str(model_dir) if model_dir else None
    except Exception:
        return None


def uvr_missing_prerequisites(model_dir: str | Path, model_filename: str) -> list[str]:
    """``load_model`` 缺失时会从 GitHub 下载的**全部**前置文件。通用前置(0.44.2
    源码核对): 主模型文件、download_checks.json(list_supported_model_files 无条件
    fetch)、非 yaml 模型的 vr_model_data.json + mdx_model_data.json
    (load_model_data_using_hash)。此外每个模型有自己的 ``download_files``
    (yaml 模型的 .th 权重、Roformer ckpt 的配套 yaml 等, 第八轮 review P1)——
    download_checks.json 在场时用**真实 Separator 的解析器**离线枚举(文件已在,
    list_supported_model_files 的 download_file_if_not_exists 全部跳过, 零网络)。
    返回缺失路径列表; 空 = 禁下载可安全放行。"""
    base = Path(str(model_dir))
    required = {model_filename, "download_checks.json"}
    has_companion_yaml = False
    if (base / "download_checks.json").is_file():
        try:
            from audio_separator.separator import Separator  # noqa: PLC0415

            saved_env = os.environ.pop("AUDIO_SEPARATOR_MODEL_DIR", None)
            try:  # 解析必须用传入目录, 不被 ambient env 偷换(第十一轮 hermetic)
                kwargs = {"output_dir": str(REPO_ROOT / "static/generated_song/tmp"),
                          "model_file_dir": str(base)}
                try:
                    probe = Separator(info_only=True, **kwargs)  # 避免 Torch/ORT 探测
                except TypeError:
                    probe = Separator(**kwargs)
                groups = probe.list_supported_model_files()
            finally:
                if saved_env is not None:
                    os.environ["AUDIO_SEPARATOR_MODEL_DIR"] = saved_env
            for models in groups.values():
                for info in models.values():
                    files = list(info.get("download_files") or [])
                    if info.get("filename") == model_filename or model_filename in files:
                        for item in files:
                            name = (str(item).split("/")[-1]
                                    if str(item).startswith("http") else str(item))
                            required.add(name)
                            if name.lower().endswith(".yaml"):
                                has_companion_yaml = True
        except Exception:
            # 枚举失败(schema 变动等): 退回通用前置 -- 宁可漏报也不误报,
            # 但通用前置仍拦住最大的下载面。
            pass
    if (not model_filename.lower().endswith(".yaml")) and not has_companion_yaml:
        # 只有真正走 hash 路径(无 companion yaml 的非 yaml 模型)才需要这两个
        # json(第九轮 P2: MDXC/Roformer ckpt+yaml 不读它们, 强加会假 FAIL)。
        required |= {"vr_model_data.json", "mdx_model_data.json"}
    # is_file 而非 exists(第九轮 P1): 同名目录能骗过 exists, 真实 0.44.2 用
    # os.path.isfile 判断后仍会下载。
    return sorted(str(base / name) for name in required if not (base / name).is_file())


def _worker_song_uvr() -> dict[str, Any]:
    import soundfile

    from agent_tools.function_tools.song.config import resolve_effective_song_config
    from agent_tools.function_tools.song.separator import separate_vocals

    cfg = resolve_effective_song_config()
    sep = cfg.get("separator", {})
    model_filename = str(sep.get("model_filename") or "")
    if os.environ.get("SPICA_SELF_CHECK_NO_DOWNLOAD") == "1":
        # audio-separator pulls missing files from GitHub (HF_HUB_OFFLINE does
        # NOT cover it) -- pre-check ALL of load_model's download-able
        # prerequisites (model + metadata jsons) in the EFFECTIVE dir
        # (env > extra_kwargs > default, 镜像 0.44.2 Separator 的优先级)。
        model_dir = uvr_effective_model_dir(sep)
        if model_dir is not None:
            missing = uvr_missing_prerequisites(model_dir, model_filename)
            if missing:
                return {"status": STATUS_FAIL,
                        "reason": f"UVR 前置文件缺失且默认禁下载: {'; '.join(missing)} "
                                  "(--allow-model-downloads 放开)",
                        "detail": {"model_filename": model_filename,
                                   "model_dir": str(model_dir)}}
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
        return {"status": STATUS_FAIL,
                "reason": redact_secrets("; ".join(failures))[:400], "detail": detail}
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
        payload = {"status": STATUS_FAIL,  # 脱敏先于截断(第九轮 P1: 截断可把
                   # secret 切成脱敏器匹配不到的片段)
                   "reason": redact_secrets(f"{type(exc).__name__}: {exc}")[:500],
                   "detail": {}}
    payload["duration_s"] = round(time.time() - started, 1)
    # 结构化脱敏(第七轮 review P1): 隐藏 --worker 输出不经 render_report,
    # str(exc)/detail 可能携带 secret -- 在 dumps 之前清洗。
    payload = redact_obj(payload)
    # 前置换行是第二重保险: 即使某个库最后一次写 stdout 没换行, marker 也独占一行
    nonce = os.environ.get("SPICA_SELF_CHECK_MARKER_NONCE", "")
    marker = RESULT_MARKER + (nonce + ":" if nonce else "")
    print("\n" + marker + json.dumps(payload, ensure_ascii=False), flush=True)
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


def _raise_keyboard_interrupt(_signum: int, _frame: Any) -> None:
    raise KeyboardInterrupt


def main(argv: list[str] | None = None) -> int:
    load_secrets()  # 铁律 #10: 进程入口在构造任何对象之前先灌注环境(AST 测试钉住)
    previous_handler: Any = None
    handler_installed = False
    try:
        # 第九轮 P1: CI cancel/systemd stop 发 SIGTERM -- 转成 KI 走同一条
        # 「清理 worker 树再 exit 3」的路径, 否则独立 session 里的整棵树遗留。
        previous_handler = signal.getsignal(signal.SIGTERM)
        signal.signal(signal.SIGTERM, _raise_keyboard_interrupt)
        handler_installed = True
    except (ValueError, OSError):
        pass  # 非主线程/受限环境: 装不上 handler 就保持原语义
    try:
        return _main(argv)
    except KeyboardInterrupt:
        # 覆盖所有阶段(轻量检查/配置加载/守卫/重检查循环), 统一约定 exit 3;
        # 进行中的 worker 进程树已由 run_subprocess_check 的 BaseException 分支清理。
        print("[self-check] 已中断——已尝试清理进行中的 worker 进程树"
              "(结果 best-effort, 见上方日志)。", file=sys.stderr)
        return 3
    except SelfCheckInternalError as exc:
        print(redact_secrets(f"[self-check] FATAL: {exc}"), file=sys.stderr)
        return 3
    finally:
        if handler_installed:
            try:  # 不污染调用进程(第十轮 P2): main 返回后恢复原 handler
                signal.signal(signal.SIGTERM, previous_handler)
            except (ValueError, OSError, TypeError):
                pass


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
        print(redact_secrets(f"[self-check] FATAL: 配置加载失败: {type(exc).__name__}: {exc}"),
              file=sys.stderr)
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
    for name in names:
        # P2(第七轮): scale 本身有限不够, 乘积也必须有限且可执行 -- 1e308×480=inf
        # 曾在 spawn 之后才以 OverflowError 原生 exit 1 崩掉并泄漏 worker。
        product = DEFAULT_TIMEOUTS_S[name] * args.timeout_scale
        if not (math.isfinite(product) and 0 < product <= MAX_CHECK_TIMEOUT_S):
            print(f"[self-check] FATAL: --timeout-scale={args.timeout_scale} 使 {name} "
                  f"的超时非法: {product!r} (须有限且 <={MAX_CHECK_TIMEOUT_S:.0f}s)",
                  file=sys.stderr)
            return 3
    enabled = _enabled_map(app, screen_cfg, song_cfg)
    results = run_full_checks(names, enabled, args)  # KI -> main() 统一转 exit 3
    print(render_report("full", results, args.json))
    return exit_code_for(results)


if __name__ == "__main__":
    sys.exit(main())

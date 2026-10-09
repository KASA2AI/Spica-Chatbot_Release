"""Resident Qwen ASR behind the existing STT port, with isolated dependencies.

The child owns Transformers, CUDA and Silero. The host owns the child: shutdown,
timeout or a broken reply closes it before another request may start a new one.
No listener, microphone, model download or dialogue/tool execution is involved.
"""
from __future__ import annotations

import base64
import json
import logging
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time

from common.timing import log_timing, now_ms
from spica.ports.stt import STT_REQUEST_TIMEOUT_SECONDS

logger = logging.getLogger(__name__)
WORKER = Path(__file__).resolve().parents[2] / "local_runtime" / "stt" / "qwen_worker.py"
MAX_PCM_BYTES = 16000 * 2 * 60


class QwenAsrAdapter:
    name = "qwen_asr"

    def __init__(self, *, model: str, worker_python: str | None = None,
                 device: str = "cuda", compute_type: str = "bfloat16",
                 language: str = "zh"):
        self._model_path = str(Path(model).resolve())
        self._python = worker_python or sys.executable
        self._device, self._compute_type, self._language = device, compute_type, language
        self._request_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._process: subprocess.Popen | None = None
        self._closed = False
        self._startup_timeout = 90.0
        self._inference_timeout = 20.0
        self._request_timeout = STT_REQUEST_TIMEOUT_SECONDS

    def _start(self):
        with self._state_lock:
            if self._closed:
                raise RuntimeError("本地语音识别已关闭")
            if self._process is not None:
                return self._process, False
            if not Path(self._model_path).is_dir():
                raise RuntimeError("Qwen 需要已下载的本地模型目录，不会在对话时下载")
            process = subprocess.Popen(
                [self._python, "-X", "utf8", "-s", "-u", str(WORKER),
                 "--model", self._model_path, "--device", self._device,
                 "--compute-type", self._compute_type, "--language", self._language],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                # Runtime diagnostics go to the service log, never the JSON pipe.
                stderr=None,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            self._process = process
            return process, True

    def _discard(self, process):
        with self._state_lock:
            # Keep ownership until reaping is confirmed. A failed kill/wait must
            # not permit another resident model to be spawned beside this one.
            if process.poll() is None:
                try:
                    process.terminate()
                except ProcessLookupError:
                    pass
                try:
                    process.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                    process.wait(timeout=1.0)
            else:
                process.wait()
            for stream in (process.stdin, process.stdout):
                if stream is not None:
                    stream.close()
            if self._process is process:
                self._process = None

    def _request(self, request):
        deadline = time.monotonic() + self._request_timeout
        # Waiting behind warmup/another caller belongs to this request's budget.
        # Expiry here must not terminate the worker owned by that other caller.
        while True:
            with self._state_lock:
                if self._closed:
                    raise RuntimeError("本地语音识别已关闭")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError("Qwen 识别排队超时，请稍后重试")
            if self._request_lock.acquire(timeout=min(.1, remaining)):
                break
        try:
            if time.monotonic() >= deadline:
                raise RuntimeError("Qwen 识别排队超时，请稍后重试")
            process, fresh = self._start()
            completed = queue.Queue(maxsize=1)

            def exchange():
                try:
                    process.stdin.write(json.dumps(request).encode("utf-8") + b"\n")
                    process.stdin.flush()
                    line = process.stdout.readline(65537)
                    if not line.endswith(b"\n") or len(line) > 65536:
                        raise RuntimeError("Qwen 识别进程已断开或返回不完整")
                    reply = json.loads(line)
                    if not isinstance(reply, dict) or reply.get("ok") is not True:
                        detail = reply.get("error", "无有效结果") if isinstance(reply, dict) else "无有效结果"
                        raise RuntimeError(f"Qwen 识别失败：{detail}")
                    completed.put((reply["result"], None))
                except Exception as exc:
                    completed.put((None, exc))

            io = threading.Thread(target=exchange, name="qwen-asr-request", daemon=True)
            io.start()
            try:
                timeout = min(deadline - time.monotonic(),
                              self._startup_timeout if fresh else self._inference_timeout)
                result, error = completed.get(timeout=max(0, timeout))
                if error is not None:
                    raise error
                with self._state_lock:
                    if self._closed:
                        raise RuntimeError("本地语音识别已关闭")
                return result
            except queue.Empty as exc:
                self._discard(process)
                raise RuntimeError("Qwen 识别超时，已回收推理进程，请重试") from exc
            except Exception:
                self._discard(process)
                raise
            finally:
                io.join(timeout=1.0)
        finally:
            self._request_lock.release()

    def warmup(self):
        start = now_ms()
        try:
            result = self._request({"op": "warmup"})
            result.update(ok=True, duration_ms=now_ms()-start)
            logger.info("qwen-asr warmup ok: %s", result)
            return result
        except Exception as exc:
            logger.error("qwen-asr warmup failed: %s", exc)
            return {"ok": False, "duration_ms": now_ms()-start, "error": str(exc)}

    def transcribe(self, pcm: bytes, *, sample_rate: int = 16000) -> str:
        if sample_rate != 16000 or len(pcm) % 2 or len(pcm) > MAX_PCM_BYTES:
            raise ValueError("Qwen 识别需要不超过 60 秒的 16 kHz 单声道 int16 PCM")
        if not pcm:
            return ""
        start = now_ms()
        result = self._request({"op": "transcribe", "pcm": base64.b64encode(pcm).decode("ascii")})
        text = result.get("text")
        if not isinstance(text, str):
            raise RuntimeError("Qwen 识别结果格式错误")
        log_timing("stt_transcribe", now_ms()-start, backend=self.name,
                   chars=len(text), audio_ms=round(len(pcm)/32), text=text[:80])
        return text

    def close(self):
        with self._state_lock:
            self._closed = True
            process = self._process
        if process is not None:
            self._discard(process)

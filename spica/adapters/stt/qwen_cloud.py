"""Explicit opt-in Bailian ASR. No local model, retries, or provider fallback.

The host owns this adapter. Each synchronous transcription owns one bounded
async HTTP request; closing the adapter cancels that request and rejects reuse.
Audio stays in memory and only the final transcript crosses the existing port.
"""
from __future__ import annotations

import asyncio
import base64
import io
import json
import threading
import wave

import httpx

from common.timing import log_timing, now_ms


class QwenCloudAsrAdapter:
    name = "qwen_cloud"
    requires_model_warmup = False

    def __init__(self, *, api_key: str | None, base_url: str, model: str,
                 language: str = "zh", timeout_seconds: float = 20):
        # Also validate direct construction: the credential cannot be redirected
        # to an arbitrary endpoint by a caller bypassing settings validation.
        from spica.config.schema import SttConfig
        config = SttConfig(cloud_base_url=base_url, cloud_model=model,
                           cloud_timeout_seconds=timeout_seconds)
        if not api_key or not api_key.isascii() or any(char.isspace() for char in api_key):
            raise ValueError("请在设置中保存有效的百炼语音识别 API Key。")
        self._key = api_key
        self._url = config.cloud_base_url + "/chat/completions"
        self._model = config.cloud_model
        self._language = language
        self._timeout = config.cloud_timeout_seconds
        self._lock = threading.Lock()
        self._request_lock = threading.Lock()
        self._closed = False
        self._loop = None
        self._task = None

    def warmup(self) -> dict:
        # No paid probe or microphone upload at startup. Configured != tested.
        with self._lock:
            return {"ok": not self._closed, "remote": True, "verified": False,
                    "duration_ms": 0, **({"error": "云端识别已关闭"} if self._closed else {})}

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._key = ""
            if self._loop is not None and self._task is not None:
                self._loop.call_soon_threadsafe(self._task.cancel)

    @staticmethod
    def _has_speech(pcm: bytes) -> bool:
        # Same small CPU-only VAD dependency as the ordinary microphone. No
        # torch/transformers/Silero environment is required for cloud users.
        import webrtcvad
        vad = webrtcvad.Vad(2)
        frame_bytes = 960  # 30 ms, 16 kHz, signed 16-bit mono
        return any(vad.is_speech(pcm[start:start + frame_bytes].ljust(frame_bytes, b"\0"), 16000)
                   for start in range(0, len(pcm), frame_bytes))

    def transcribe(self, pcm: bytes, *, sample_rate: int = 16000) -> str:
        if sample_rate != 16000 or len(pcm) % 2 or len(pcm) > 16000 * 2 * 60:
            raise ValueError("语音识别需要不超过 60 秒的 16 kHz 单声道 int16 PCM。")
        if not self._request_lock.acquire(blocking=False):
            raise RuntimeError("上一段语音仍在识别，请稍后再说。")
        start = now_ms()
        try:
            with self._lock:
                if self._closed:
                    raise RuntimeError("云端语音识别已关闭。")
            if not pcm or not self._has_speech(pcm):
                return ""
            audio = io.BytesIO()
            with wave.open(audio, "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(sample_rate)
                wav.writeframes(pcm)
            data = "data:audio/wav;base64," + base64.b64encode(audio.getvalue()).decode("ascii")
            try:
                text = asyncio.run(self._transcribe(data))
                log_timing("stt_transcribe", now_ms() - start, backend=self.name,
                           audio_seconds=len(pcm) / 32000, text_chars=len(text))
                return text
            except (asyncio.TimeoutError, httpx.TimeoutException):
                raise RuntimeError("云端语音识别超时，请检查网络后重试。") from None
            except asyncio.CancelledError:
                raise RuntimeError("云端语音识别已取消。") from None
            except httpx.HTTPError:
                raise RuntimeError("无法连接百炼语音识别，请检查网络。") from None
        finally:
            self._request_lock.release()

    async def _transcribe(self, data: str) -> str:
        with self._lock:
            if self._closed:
                raise RuntimeError("云端语音识别已关闭。")
            self._loop = asyncio.get_running_loop()
            self._task = asyncio.current_task()
            key = self._key
        try:
            # Total deadline includes connect, upload and full response, not just
            # idle time between received chunks. There is no automatic retry.
            result = await asyncio.wait_for(self._request(data, key), timeout=self._timeout)
            with self._lock:
                if self._closed:
                    raise RuntimeError("云端语音识别已关闭。")
            return result
        finally:
            with self._lock:
                self._loop = self._task = None

    async def _request(self, data: str, key: str) -> str:
        payload = {
            "model": self._model,
            "messages": [{"role": "user", "content": [
                {"type": "input_audio", "input_audio": {"data": data}}]}],
            "stream": False,
            "asr_options": {"enable_itn": False, **({"language": self._language} if self._language else {})},
        }
        async with httpx.AsyncClient(timeout=self._timeout, follow_redirects=False, trust_env=False) as client:
            async with client.stream("POST", self._url, json=payload,
                                     headers={"Authorization": "Bearer " + key}) as response:
                if response.status_code != 200:
                    messages = {
                        401: "百炼语音识别密钥无效，请核对密钥与接口地域。",
                        402: "百炼语音识别账户余额不足。",
                        403: "百炼语音识别未获授权，请检查服务开通、地域和账户额度。",
                        429: "百炼语音识别请求受限，请检查额度或稍后重试。",
                    }
                    # Never expose response bodies: they can echo audio or keys.
                    raise RuntimeError(messages.get(response.status_code, "百炼语音识别请求失败，请核对服务与模型配置。"))
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    content.extend(chunk)
                    if len(content) > 65536:
                        raise RuntimeError("百炼语音识别返回内容过长。")
        try:
            choice = json.loads(content)["choices"][0]
            text = choice["message"]["content"]
            if choice.get("finish_reason") != "stop" or not isinstance(text, str):
                raise ValueError
            return text.strip()
        except (ValueError, KeyError, IndexError, TypeError):
            raise RuntimeError("百炼语音识别返回格式不完整，请重试。") from None

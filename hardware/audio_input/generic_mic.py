"""Generic-mic recorder with webrtcvad software segmentation (W3, §4.2/§4.3).

Behavior contract (W3-a): same shape as ``record_respeaker_channel0_hardware_vad``
-- ``(should_stop, on_speech_start, end_silence_seconds, ...) -> bytes`` of one
VAD-segmented 16 kHz mono int16 utterance (pre-roll included) -- so the
``SpeechWorker`` call site consumes either backend unchanged. The endpointing
state machine is a 1:1 port of the hardware-VAD loop (pre-roll deque / started /
speech_elapsed / silence_elapsed / start_timeout / max_seconds); the only
substitution is ``webrtcvad.Vad.is_speech(frame, 16000)`` for the ReSpeaker's
``control.is_voice()``, on 20 ms frames (320 samples -- the same chunk size the
hardware loop derives from ``vad_poll_seconds=0.02``).

IMPORT DISCIPLINE (W3 ruling): ``webrtcvad`` MAY be imported at module level
(requirements-stt.txt ships the -wheels fork on both platforms); **PyAudio must
stay lazy** -- it is only touched inside the real open path, so importing this
module never requires an audio stack.

Error envelopes (P2-3 fatal-marker contract):
- missing PyAudio            -> "缺少 PyAudio..."        (existing FATAL marker)
- open failure (no device /  -> "无法打开麦克风：..."     (FATAL marker, added
  permission denied / bad     with W3 to FATAL_SPEECH_ERROR_MARKERS)
  params / unknown factory
  blowup)
- read/VAD failure mid-take  -> "麦克风读取异常/软件 VAD 判定失败" (TRANSIENT: this
  recording fails readably, the loop schedules the next one; a dead device then
  fails the next OPEN, which IS fatal -- retry unit is the whole recording,
  never per-frame)
- overflow                   -> tolerated (``exception_on_overflow=False``), not an error
- no speech / cancelled      -> ``ReSpeakerNoSpeechError`` / ``ReSpeakerRecordingCancelled``
  pass through unwrapped (P3-7: the ``ReSpeaker*`` exception family is knowingly
  reused so the SpeechWorker call face stays unchanged).

Qt-free (CLAUDE.md #1).
"""

from __future__ import annotations

import logging
import math
from collections import deque
from typing import Any, Callable

import webrtcvad

from hardware.respeaker.audio import (
    DEFAULT_END_SILENCE_SECONDS,
    ReSpeakerAudioError,
    ReSpeakerNoSpeechError,
    ReSpeakerRecordingCancelled,
)

logger = logging.getLogger(__name__)

SAMPLE_RATE = 16000
SAMPLE_WIDTH = 2
FRAME_MS = 20  # webrtcvad accepts 10/20/30 ms; 20 ms == the hardware loop's chunk
FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS // 1000  # 320
FRAME_BYTES = FRAME_SAMPLES * SAMPLE_WIDTH  # 640
# 0-3; 2 is the W3-a default, tuned on the real machine during W3 acceptance.
DEFAULT_VAD_AGGRESSIVENESS = 2


def _load_pyaudio():
    try:
        import pyaudio
    except Exception as exc:
        raise ReSpeakerAudioError(
            "缺少 PyAudio，无法从麦克风录音。请在当前 Python 环境安装 PyAudio。"
        ) from exc
    return pyaudio


class _DefaultMicStream:
    """Owns the PyAudio instance + stream so ``close()`` tears both down."""

    def __init__(self, audio: Any, stream: Any) -> None:
        self._audio = audio
        self._stream = stream

    def read(self, frames_per_buffer: int, exception_on_overflow: bool = False) -> bytes:
        return self._stream.read(frames_per_buffer, exception_on_overflow=exception_on_overflow)

    def close(self) -> None:
        try:
            self._stream.stop_stream()
        except Exception:  # noqa: BLE001
            pass
        try:
            self._stream.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            self._audio.terminate()
        except Exception:  # noqa: BLE001
            pass


def _open_default_mic_stream(frames_per_buffer: int) -> _DefaultMicStream:
    """Open the system default input device at 1ch/16k/int16. Any failure --
    no device, privacy/permission denial, unsupported rate -- raises the FATAL
    "无法打开麦克风" envelope with the backend's original message preserved."""
    pyaudio = _load_pyaudio()
    audio = pyaudio.PyAudio()
    try:
        stream = audio.open(
            format=pyaudio.paInt16,
            channels=1,
            rate=SAMPLE_RATE,
            input=True,
            frames_per_buffer=frames_per_buffer,
        )
    except Exception as exc:
        try:
            audio.terminate()
        except Exception:  # noqa: BLE001
            pass
        raise ReSpeakerAudioError(
            f"无法打开麦克风（默认输入设备，1ch/16000Hz/s16le）：{exc}"
        ) from exc
    return _DefaultMicStream(audio, stream)


def record_generic_mic_software_vad(
    max_seconds: float = 8.0,
    start_timeout: float = 4.0,
    end_silence_seconds: float = DEFAULT_END_SILENCE_SECONDS,
    min_speech_seconds: float = 0.20,
    pre_roll_seconds: float = 0.25,
    should_stop: Callable[[], bool] | None = None,
    on_speech_start: Callable[[], None] | None = None,
    *,
    vad: Any | None = None,
    vad_aggressiveness: int = DEFAULT_VAD_AGGRESSIVENESS,
    stream_factory: Callable[[int], Any] | None = None,
) -> bytes:
    """Record the default mic until the software VAD sees the utterance end.

    ``vad`` / ``stream_factory`` are behavior seams (W3 TDD ruling): tests drive
    the full segmentation contract with fake VAD frames / a fake stream; in
    production both default to the real webrtcvad + PyAudio open path.
    """
    _validate_args(
        max_seconds=max_seconds,
        start_timeout=start_timeout,
        end_silence_seconds=end_silence_seconds,
        min_speech_seconds=min_speech_seconds,
        pre_roll_seconds=pre_roll_seconds,
    )
    if vad is None:
        vad = webrtcvad.Vad(vad_aggressiveness)

    factory = stream_factory or _open_default_mic_stream
    try:
        stream = factory(FRAME_SAMPLES)
    except ReSpeakerAudioError:
        raise  # already an enveloped (fatal) open failure
    except Exception as exc:
        raise ReSpeakerAudioError(f"无法打开麦克风：{exc}") from exc

    chunk_seconds = FRAME_MS / 1000.0
    pre_roll: deque[bytes] = deque(maxlen=max(1, math.ceil(pre_roll_seconds / chunk_seconds)))
    recorded: list[bytes] = []
    started = False
    session_seconds = 0.0
    speech_elapsed = 0.0
    silence_elapsed = 0.0

    try:
        while True:
            if should_stop is not None and should_stop():
                raise ReSpeakerRecordingCancelled("麦克风录音已取消。")

            try:
                frame = stream.read(FRAME_SAMPLES, exception_on_overflow=False)
            except Exception as exc:
                # TRANSIENT: fail this take readably; the next OPEN decides fatality.
                raise ReSpeakerAudioError(f"麦克风读取异常：{exc}") from exc
            if len(frame) != FRAME_BYTES:
                raise ReSpeakerAudioError(
                    f"麦克风读取异常：期望 {FRAME_BYTES} bytes/帧，实际 {len(frame)}。"
                )
            session_seconds += chunk_seconds

            try:
                voice = bool(vad.is_speech(frame, SAMPLE_RATE))
            except Exception as exc:
                raise ReSpeakerAudioError(f"软件 VAD 判定失败：{exc}") from exc

            if voice:
                if not started:
                    started = True
                    recorded.extend(pre_roll)
                    pre_roll.clear()
                    logger.info("generic mic software VAD started recording")
                    if on_speech_start is not None:
                        try:
                            on_speech_start()
                        except Exception:  # noqa: BLE001 -- a hint cb must never kill recording
                            logger.warning("on_speech_start hint failed", exc_info=True)
                recorded.append(frame)
                speech_elapsed += chunk_seconds
                silence_elapsed = 0.0
            elif started:
                recorded.append(frame)
                speech_elapsed += chunk_seconds
                silence_elapsed += chunk_seconds
                if silence_elapsed >= end_silence_seconds and speech_elapsed >= min_speech_seconds:
                    logger.info(
                        "generic mic software VAD ended recording after %.2fs speech and %.2fs trailing silence",
                        speech_elapsed,
                        silence_elapsed,
                    )
                    return b"".join(recorded)
            else:
                pre_roll.append(frame)
                if session_seconds >= start_timeout:
                    raise ReSpeakerNoSpeechError("没有检测到语音输入。")

            if started and session_seconds >= max_seconds:
                logger.info("generic mic software VAD reached max_seconds %.2fs", max_seconds)
                return b"".join(recorded)
            if not started and session_seconds >= max_seconds:
                raise ReSpeakerNoSpeechError("没有检测到语音输入。")
    finally:
        try:
            stream.close()
        except Exception:  # noqa: BLE001
            pass


def _validate_args(**values: float) -> None:
    negative = [name for name, value in values.items() if value < 0]
    if negative:
        raise ReSpeakerAudioError(f"录音参数不能为负数：{', '.join(sorted(negative))}")
    if values["max_seconds"] <= 0:
        raise ReSpeakerAudioError("max_seconds 必须大于 0。")

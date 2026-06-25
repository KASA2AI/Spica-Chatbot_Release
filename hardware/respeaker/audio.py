from __future__ import annotations

import logging
import math
from collections import deque
from typing import Callable

from spica.config.manager import respeaker_env_overrides

from .control import ReSpeakerControl, ReSpeakerControlError


SAMPLE_RATE = 16000
CHANNELS = 6
SAMPLE_WIDTH = 2
DEFAULT_CHUNK_FRAMES = 320
RESPEAKER_DEVICE_KEYWORDS = ("respeaker", "seeed", "2886")

logger = logging.getLogger(__name__)


class ReSpeakerAudioError(RuntimeError):
    pass


class ReSpeakerNoSpeechError(ReSpeakerAudioError):
    pass


class ReSpeakerRecordingCancelled(ReSpeakerAudioError):
    pass


def record_respeaker_channel0(seconds: float = 8.0) -> bytes:
    """Record fixed-duration ReSpeaker audio and return channel 0 as 16kHz s16le PCM."""
    if seconds <= 0:
        return b""

    pyaudio = _load_pyaudio()
    audio = pyaudio.PyAudio()
    stream = None
    frames: list[bytes] = []
    try:
        stream = _open_respeaker_stream(
            pyaudio=pyaudio,
            audio=audio,
            frames_per_buffer=DEFAULT_CHUNK_FRAMES,
        )
        chunk_count = max(1, math.ceil(seconds * SAMPLE_RATE / DEFAULT_CHUNK_FRAMES))
        for _ in range(chunk_count):
            raw_chunk = stream.read(DEFAULT_CHUNK_FRAMES, exception_on_overflow=False)
            frames.append(_extract_channel0(raw_chunk))
    except ReSpeakerAudioError:
        raise
    except Exception as exc:
        raise ReSpeakerAudioError(f"ReSpeaker 固定时长录音失败：{exc}") from exc
    finally:
        _close_stream(stream)
        audio.terminate()

    return b"".join(frames)


def record_respeaker_channel0_hardware_vad(
    max_seconds: float = 8.0,
    start_timeout: float = 4.0,
    end_silence_seconds: float = 0.55,
    min_speech_seconds: float = 0.20,
    pre_roll_seconds: float = 0.25,
    vad_poll_seconds: float = 0.02,
    should_stop: Callable[[], bool] | None = None,
    on_speech_start: Callable[[], None] | None = None,
) -> bytes:
    """Record channel 0 until the ReSpeaker hardware VAD sees speech end.

    ``on_speech_start`` (optional) fires ONCE, on this thread, the instant the
    hardware VAD first reports voice -- i.e. the user has actually started
    speaking (not merely the mic idling). It lets a caller distinguish
    "mid-utterance" from "idle-listening" so a proactive turn can fire in the
    idle gaps without cutting off a half-spoken sentence (P5 reaction-in-voice).
    """
    _validate_hardware_vad_args(
        max_seconds=max_seconds,
        start_timeout=start_timeout,
        end_silence_seconds=end_silence_seconds,
        min_speech_seconds=min_speech_seconds,
        pre_roll_seconds=pre_roll_seconds,
        vad_poll_seconds=vad_poll_seconds,
    )

    try:
        control = _create_hardware_vad()
    except _HardwareVadUnavailable as exc:
        return _fallback_or_raise(max_seconds=max_seconds, reason=str(exc))

    audio = None
    stream = None
    frames_per_buffer = max(1, round(SAMPLE_RATE * vad_poll_seconds))
    chunk_seconds = frames_per_buffer / SAMPLE_RATE
    pre_roll_chunks = max(1, math.ceil(pre_roll_seconds / chunk_seconds))
    pre_roll: deque[bytes] = deque(maxlen=pre_roll_chunks)
    recorded: list[bytes] = []
    started = False
    session_seconds = 0.0
    speech_elapsed = 0.0
    silence_elapsed = 0.0
    fallback_reason: str | None = None

    try:
        pyaudio = _load_pyaudio()
        audio = pyaudio.PyAudio()
        stream = _open_respeaker_stream(
            pyaudio=pyaudio,
            audio=audio,
            frames_per_buffer=frames_per_buffer,
        )

        while True:
            if should_stop is not None and should_stop():
                raise ReSpeakerRecordingCancelled("ReSpeaker 录音已取消。")

            raw_chunk = stream.read(frames_per_buffer, exception_on_overflow=False)
            channel0 = _extract_channel0(raw_chunk)
            session_seconds += chunk_seconds

            try:
                voice = control.is_voice()
            except ReSpeakerControlError as exc:
                raise _HardwareVadUnavailable(str(exc)) from exc

            if voice:
                if not started:
                    started = True
                    recorded.extend(pre_roll)
                    pre_roll.clear()
                    logger.info("ReSpeaker hardware VAD started recording")
                    if on_speech_start is not None:
                        try:
                            on_speech_start()
                        except Exception:  # noqa: BLE001 -- a hint cb must never kill recording
                            logger.warning("on_speech_start hint failed", exc_info=True)
                recorded.append(channel0)
                speech_elapsed += chunk_seconds
                silence_elapsed = 0.0
            elif started:
                recorded.append(channel0)
                speech_elapsed += chunk_seconds
                silence_elapsed += chunk_seconds
                if silence_elapsed >= end_silence_seconds and speech_elapsed >= min_speech_seconds:
                    logger.info(
                        "ReSpeaker hardware VAD ended recording after %.2fs speech and %.2fs trailing silence",
                        speech_elapsed,
                        silence_elapsed,
                    )
                    return b"".join(recorded)
            else:
                pre_roll.append(channel0)
                if session_seconds >= start_timeout:
                    raise ReSpeakerNoSpeechError("没有检测到语音输入。")

            if started and session_seconds >= max_seconds:
                logger.info("ReSpeaker hardware VAD reached max_seconds %.2fs", max_seconds)
                return b"".join(recorded)
            if not started and session_seconds >= max_seconds:
                raise ReSpeakerNoSpeechError("没有检测到语音输入。")
    except _HardwareVadUnavailable as exc:
        fallback_reason = str(exc)
    except ReSpeakerAudioError:
        raise
    except Exception as exc:
        raise ReSpeakerAudioError(f"ReSpeaker 硬件 VAD 录音失败：{exc}") from exc
    finally:
        _close_stream(stream)
        if audio is not None:
            audio.terminate()
        control.close()

    if fallback_reason is not None:
        return _fallback_or_raise(max_seconds=max_seconds, reason=fallback_reason)

    return b"".join(recorded)


class _HardwareVadUnavailable(RuntimeError):
    pass


def _create_hardware_vad() -> ReSpeakerControl:
    control: ReSpeakerControl | None = None
    try:
        control = ReSpeakerControl()
        control.is_voice()
        logger.info("ReSpeaker hardware VAD is available")
        return control
    except ReSpeakerControlError as exc:
        if control is not None:
            control.close()
        raise _HardwareVadUnavailable(str(exc)) from exc


def _fallback_or_raise(max_seconds: float, reason: str) -> bytes:
    if respeaker_env_overrides()["require_hardware_vad"] == "1":
        raise ReSpeakerAudioError(f"ReSpeaker 硬件 VAD 不可用：{reason}")

    fallback_seconds = min(max_seconds, 3.0)
    logger.warning(
        "ReSpeaker hardware VAD unavailable, falling back to fixed %.2fs recording: %s",
        fallback_seconds,
        reason,
    )
    return record_respeaker_channel0(seconds=fallback_seconds)


def _load_pyaudio():
    try:
        import pyaudio
    except Exception as exc:
        raise ReSpeakerAudioError(
            "缺少 PyAudio，无法从 ReSpeaker 录音。请在 gptsovits 环境安装 PyAudio。"
        ) from exc
    return pyaudio


def _open_respeaker_stream(pyaudio, audio, frames_per_buffer: int):
    device_index = _find_respeaker_device_index(audio)
    try:
        return audio.open(
            format=pyaudio.paInt16,
            channels=CHANNELS,
            rate=SAMPLE_RATE,
            input=True,
            input_device_index=device_index,
            frames_per_buffer=frames_per_buffer,
        )
    except Exception as exc:
        device_hint = f"device_index={device_index}" if device_index is not None else "default input device"
        raise ReSpeakerAudioError(
            f"无法打开 ReSpeaker 6ch/16000Hz/s16le 录音流（{device_hint}）：{exc}"
        ) from exc


def _find_respeaker_device_index(audio) -> int | None:
    env_index = respeaker_env_overrides()["input_device_index"]
    if env_index:
        try:
            return int(env_index)
        except ValueError as exc:
            raise ReSpeakerAudioError(f"RESPEAKER_INPUT_DEVICE_INDEX 不是有效整数：{env_index}") from exc

    try:
        device_count = audio.get_device_count()
    except Exception:
        return None

    for index in range(device_count):
        try:
            info = audio.get_device_info_by_index(index)
        except Exception:
            continue

        name = str(info.get("name", "")).lower()
        max_input_channels = int(info.get("maxInputChannels") or 0)
        if max_input_channels >= CHANNELS and any(keyword in name for keyword in RESPEAKER_DEVICE_KEYWORDS):
            return index
    return None


def _extract_channel0(raw_chunk: bytes) -> bytes:
    frame_width = CHANNELS * SAMPLE_WIDTH
    if len(raw_chunk) % frame_width != 0:
        raise ReSpeakerAudioError(
            f"ReSpeaker 输入数据长度异常：{len(raw_chunk)} bytes 不能整除 {frame_width}"
        )

    channel0 = bytearray(len(raw_chunk) // CHANNELS)
    out_index = 0
    for frame_index in range(0, len(raw_chunk), frame_width):
        channel0[out_index:out_index + SAMPLE_WIDTH] = raw_chunk[frame_index:frame_index + SAMPLE_WIDTH]
        out_index += SAMPLE_WIDTH
    return bytes(channel0)


def _close_stream(stream) -> None:
    if stream is None:
        return
    try:
        stream.stop_stream()
    except Exception:
        pass
    try:
        stream.close()
    except Exception:
        pass


def _validate_hardware_vad_args(
    *,
    max_seconds: float,
    start_timeout: float,
    end_silence_seconds: float,
    min_speech_seconds: float,
    pre_roll_seconds: float,
    vad_poll_seconds: float,
) -> None:
    values = {
        "max_seconds": max_seconds,
        "start_timeout": start_timeout,
        "end_silence_seconds": end_silence_seconds,
        "min_speech_seconds": min_speech_seconds,
        "pre_roll_seconds": pre_roll_seconds,
        "vad_poll_seconds": vad_poll_seconds,
    }
    invalid = [name for name, value in values.items() if value < 0]
    if invalid:
        raise ReSpeakerAudioError(f"录音参数不能为负数：{', '.join(invalid)}")
    if max_seconds <= 0:
        raise ReSpeakerAudioError("max_seconds 必须大于 0。")
    if vad_poll_seconds <= 0:
        raise ReSpeakerAudioError("vad_poll_seconds 必须大于 0。")

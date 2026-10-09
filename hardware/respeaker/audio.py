from __future__ import annotations

import logging
import math
from collections import deque
from typing import Callable

from spica.config.manager import respeaker_env_overrides

from .control import ReSpeakerControl, ReSpeakerControlError, ReSpeakerDeviceMismatchError, usb_address_for_input


SAMPLE_RATE = 16000
CHANNELS = 6
SAMPLE_WIDTH = 2
DEFAULT_CHUNK_FRAMES = 320
RESPEAKER_DEVICE_KEYWORDS = ("respeaker", "seeed", "2886")

# Trailing silence (seconds) before the hardware-VAD loop declares the utterance
# finished. Raised from the original 0.55 so a slow speaker / a mid-sentence pause /
# a re-stressed syllable is not cut off mid-phrase. Tunable per-machine via
# RESPEAKER_END_SILENCE_SECONDS (resolve_end_silence_seconds); 0.7-1.4 is a sane band
# (lower -> snappier finalize but more clipping; higher -> never clips but waits
# longer after you stop before she responds).
DEFAULT_END_SILENCE_SECONDS = 0.9

logger = logging.getLogger(__name__)


class ReSpeakerAudioError(RuntimeError):
    pass


class ReSpeakerNoSpeechError(ReSpeakerAudioError):
    pass


class ReSpeakerRecordingCancelled(ReSpeakerAudioError):
    pass


class ReSpeakerCaptureReleaseError(ReSpeakerAudioError):
    """Keep failed native owners alive and block reuse until desktop restart."""

    def __init__(self, stream, audio=None):
        super().__init__('采音资源尚未释放，请检查设备连接，退出并重新启动桌面程序。')
        self.stream, self.audio = stream, audio


def resolve_end_silence_seconds() -> float:
    """Resolve the trailing-silence endpoint threshold from the config layer.

    Reads RESPEAKER_END_SILENCE_SECONDS via ``respeaker_env_overrides()`` (the only
    sanctioned env path -- never ``os.getenv`` here) and coerces it to a positive
    float; an unset / blank / non-numeric / non-positive value falls back to
    ``DEFAULT_END_SILENCE_SECONDS``. Consumer-side coercion, matching the raw-string
    contract of ``respeaker_env_overrides``."""
    raw = respeaker_env_overrides()["end_silence_seconds"]
    if raw is None:
        return DEFAULT_END_SILENCE_SECONDS
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_END_SILENCE_SECONDS
    return value if value > 0 else DEFAULT_END_SILENCE_SECONDS


def _raise_if_cancelled(should_stop):
    if should_stop is not None and should_stop():
        raise ReSpeakerRecordingCancelled()


def record_respeaker_channel0(seconds: float = 8.0, *, input_device: str = "",
                              should_stop=None, on_ready=None) -> bytes:
    """Record fixed-duration ReSpeaker audio and return channel 0 as 16kHz s16le PCM."""
    if seconds <= 0:
        return b""

    pyaudio = _load_pyaudio()
    audio = pyaudio.PyAudio()
    stream = None
    frames: list[bytes] = []
    try:
        _raise_if_cancelled(should_stop)
        stream = _open_respeaker_stream(
            pyaudio=pyaudio,
            audio=audio,
            frames_per_buffer=DEFAULT_CHUNK_FRAMES,
            **({"input_device": input_device} if input_device else {}),
        )
        chunk_count = max(1, math.ceil(seconds * SAMPLE_RATE / DEFAULT_CHUNK_FRAMES))
        for index in range(chunk_count):
            _raise_if_cancelled(should_stop)
            raw_chunk = stream.read(DEFAULT_CHUNK_FRAMES, exception_on_overflow=False)
            if index == 0 and on_ready is not None:
                on_ready()
            frames.append(_extract_channel0(raw_chunk))
        _raise_if_cancelled(should_stop)
    except ReSpeakerRecordingCancelled:
        raise
    except ReSpeakerAudioError:
        raise
    except Exception as exc:
        raise ReSpeakerAudioError(f"ReSpeaker 固定时长录音失败：{exc}") from exc
    finally:
        release_audio_capture(stream, audio)

    return b"".join(frames)


def record_respeaker_channel0_hardware_vad(
    max_seconds: float = 8.0,
    start_timeout: float = 4.0,
    end_silence_seconds: float = DEFAULT_END_SILENCE_SECONDS,
    min_speech_seconds: float = 0.20,
    pre_roll_seconds: float = 0.25,
    vad_poll_seconds: float = 0.02,
    should_stop: Callable[[], bool] | None = None,
    on_speech_start: Callable[[], None] | None = None,
    on_ready: Callable[[], None] | None = None,
    input_device: str = "",
) -> bytes:
    """Record channel 0 until the ReSpeaker hardware VAD sees speech end.

    ``start_timeout`` bounds waiting for the first voice frame; ``max_seconds``
    bounds the utterance from that frame, including pauses and trailing silence.
    Only VAD-positive frames count toward ``min_speech_seconds``. Short noise
    bursts raise ``ReSpeakerNoSpeechError`` at the endpoint or duration cap.

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

    audio = None
    stream = None
    control = None
    frames_per_buffer = max(1, round(SAMPLE_RATE * vad_poll_seconds))
    chunk_seconds = frames_per_buffer / SAMPLE_RATE
    pre_roll_chunks = max(1, math.ceil(pre_roll_seconds / chunk_seconds))
    pre_roll: deque[bytes] = deque(maxlen=pre_roll_chunks)
    recorded: list[bytes] = []
    started = False
    idle_chunks = 0
    utterance_chunks = 0
    speech_chunks = 0
    silence_chunks = 0
    fallback_reason: str | None = None
    ready_sent = False

    try:
        pyaudio = _load_pyaudio()
        audio = pyaudio.PyAudio()
        device_index = _resolve_respeaker_input(audio, input_device)
        usb_address = usb_address_for_input(audio.get_device_info_by_index(device_index)) if device_index is not None else None
        control = _create_hardware_vad(usb_address=usb_address)
        stream = _open_respeaker_stream(
            pyaudio=pyaudio,
            audio=audio,
            frames_per_buffer=frames_per_buffer,
            **({"input_device": input_device} if input_device else {}),
        )

        while True:
            if should_stop is not None and should_stop():
                raise ReSpeakerRecordingCancelled("ReSpeaker 录音已取消。")

            raw_chunk = stream.read(frames_per_buffer, exception_on_overflow=False)
            channel0 = _extract_channel0(raw_chunk)
            if not ready_sent:
                ready_sent = True
                if on_ready is not None:
                    on_ready()

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
                speech_chunks += 1
                silence_chunks = 0
            elif started:
                silence_chunks += 1
            else:
                pre_roll.append(channel0)
                idle_chunks += 1
                if idle_chunks * chunk_seconds >= start_timeout:
                    raise ReSpeakerNoSpeechError("没有检测到语音输入。")
                continue

            recorded.append(channel0)
            utterance_chunks += 1
            silence_finished = not voice and silence_chunks * chunk_seconds >= end_silence_seconds
            if silence_finished or utterance_chunks * chunk_seconds >= max_seconds:
                speech_seconds = speech_chunks * chunk_seconds
                if speech_seconds < min_speech_seconds:
                    logger.info("ReSpeaker hardware VAD discarded %.2fs voice burst", speech_seconds)
                    raise ReSpeakerNoSpeechError("没有检测到足够长的语音输入。")
                logger.info(
                    "ReSpeaker hardware VAD ended recording (%s): %.2fs utterance, "
                    "%.2fs voiced, %.2fs trailing silence",
                    "silence" if silence_finished else "max_seconds",
                    utterance_chunks * chunk_seconds,
                    speech_seconds,
                    silence_chunks * chunk_seconds,
                )
                return b"".join(recorded)
    except _HardwareVadUnavailable as exc:
        fallback_reason = str(exc)
    except ReSpeakerDeviceMismatchError as exc:
        raise ReSpeakerAudioError(f"无法打开麦克风：{exc}") from exc
    except ReSpeakerAudioError:
        raise
    except Exception as exc:
        raise ReSpeakerAudioError(f"ReSpeaker 硬件 VAD 录音失败：{exc}") from exc
    finally:
        try:
            release_audio_capture(stream, audio)
        finally:
            if control is not None:
                control.close()

    if fallback_reason is not None:
        return _fallback_or_raise(max_seconds=max_seconds, reason=fallback_reason,
                                  should_stop=should_stop, on_ready=on_ready, input_device=input_device)

    return b"".join(recorded)


class _HardwareVadUnavailable(RuntimeError):
    pass


def _create_hardware_vad(*, usb_address=None) -> ReSpeakerControl:
    control: ReSpeakerControl | None = None
    try:
        control = ReSpeakerControl(**({'usb_address': usb_address} if usb_address is not None else {}))
        control.is_voice()
        logger.info("ReSpeaker hardware VAD is available")
        return control
    except ReSpeakerDeviceMismatchError:
        raise  # Identity failures cannot fall back to an unrelated hardware VAD.
    except ReSpeakerControlError as exc:
        if control is not None:
            control.close()
        raise _HardwareVadUnavailable(str(exc)) from exc


def _fallback_or_raise(max_seconds: float, reason: str, *, input_device: str = "",
                       should_stop=None, on_ready=None) -> bytes:
    if respeaker_env_overrides()["require_hardware_vad"] == "1":
        raise ReSpeakerAudioError(f"ReSpeaker 硬件 VAD 不可用：{reason}")

    fallback_seconds = min(max_seconds, 3.0)
    logger.warning(
        "ReSpeaker hardware VAD unavailable, falling back to fixed %.2fs recording: %s",
        fallback_seconds,
        reason,
    )
    return record_respeaker_channel0(seconds=fallback_seconds, should_stop=should_stop,
                                     on_ready=on_ready, input_device=input_device)


def _load_pyaudio():
    try:
        import pyaudio
    except Exception as exc:
        raise ReSpeakerAudioError(
            "缺少 PyAudio，无法从 ReSpeaker 录音。请在当前 Python 环境安装 PyAudio。"
        ) from exc
    return pyaudio


def _resolve_respeaker_input(audio, input_device):
    if input_device:
        from hardware.audio_input.devices import resolve_input_device
        try:
            device_index = resolve_input_device(audio, input_device)
        except RuntimeError as exc:
            raise ReSpeakerAudioError(f"无法打开麦克风：{exc}") from exc
        info = audio.get_device_info_by_index(device_index)
        if (int(info.get("maxInputChannels", 0)) < CHANNELS
                or not any(keyword in str(info["name"]).lower() for keyword in RESPEAKER_DEVICE_KEYWORDS)):
            raise ReSpeakerAudioError("无法打开麦克风：所选输入不是受支持的 ReSpeaker 多通道设备，请使用普通麦克风模式。")
    else:
        device_index = _find_respeaker_device_index(audio)
    return device_index


def _open_respeaker_stream(pyaudio, audio, frames_per_buffer: int, *, input_device: str = ""):
    device_index = _resolve_respeaker_input(audio, input_device)
    try:
        usb_address = usb_address_for_input(audio.get_device_info_by_index(device_index)) if device_index is not None else None
    except ReSpeakerControlError as exc:
        raise ReSpeakerAudioError(f'无法打开麦克风：{exc}') from exc
    _configure_input_gain(usb_address=usb_address)
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


def _configure_input_gain(*, usb_address=None) -> None:
    raw = respeaker_env_overrides()["agc_max_gain"]
    if raw is None or not raw.strip():
        return
    try:
        maximum = float(raw)
        if not 1 <= maximum <= 1000:
            raise ValueError
    except ValueError as exc:
        raise ReSpeakerAudioError("RESPEAKER_AGC_MAX_GAIN 必须是 1–1000 的有限线性增益。") from exc
    control = None
    try:
        control = ReSpeakerControl(**({'usb_address': usb_address} if usb_address is not None else {}))
        control.limit_agc_gain(maximum)
    except ReSpeakerDeviceMismatchError as exc:
        raise ReSpeakerAudioError(f"无法打开麦克风：{exc}") from exc
    except ReSpeakerControlError as exc:
        raise ReSpeakerAudioError(f"ReSpeaker AGC 校准失败：{exc}") from exc
    finally:
        if control is not None:
            control.close()


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


def _close_stream(stream) -> bool:
    if stream is None:
        return True
    try:
        stream.stop_stream()
    except Exception:
        pass
    try:
        stream.close()
        return True
    except Exception:
        return False


def release_audio_capture(stream, audio) -> None:
    """Try every native cleanup even if stop/close fails; never hide failure.

    Successful PyAudio termination closes all of its streams. If that also
    fails, the recorder must transfer the unconfirmed owners to its caller.
    No native retry is performed by GUI status/property reads.
    """
    closed = _close_stream(stream)
    if audio is not None:
        try:
            audio.terminate()
        except Exception as exc:
            raise ReSpeakerCaptureReleaseError(stream, audio) from exc
    elif not closed:
        raise ReSpeakerCaptureReleaseError(stream)


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

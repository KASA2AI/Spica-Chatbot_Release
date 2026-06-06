from __future__ import annotations

from PySide6.QtCore import QThread, Signal

from .audio import (
    ReSpeakerNoSpeechError,
    ReSpeakerRecordingCancelled,
    record_respeaker_channel0_hardware_vad,
)


FATAL_SPEECH_ERROR_MARKERS = (
    "缺少 speech_recognition",
    "缺少 PyAudio",
    "Could not find PyAudio",
    "No Default Input Device",
    "Invalid input device",
    "无法打开 ReSpeaker",
    "ReSpeaker 硬件 VAD 不可用",
)


def is_fatal_speech_error(message: str) -> bool:
    return any(marker in message for marker in FATAL_SPEECH_ERROR_MARKERS)


class SpeechWorker(QThread):
    status_changed = Signal(str)
    recognized = Signal(str)
    failed = Signal(str)

    def run(self) -> None:
        try:
            import speech_recognition as sr
        except Exception:
            self.failed.emit("缺少 speech_recognition / PyAudio，无法启用麦克风识别。")
            return

        try:
            recognizer = sr.Recognizer()
            self.status_changed.emit("正在等待 ReSpeaker 硬件 VAD...")
            pcm = record_respeaker_channel0_hardware_vad(
                should_stop=self.isInterruptionRequested,
            )
            if self.isInterruptionRequested():
                return
            if not pcm:
                self.failed.emit("没有检测到语音输入。")
                return
            audio = sr.AudioData(pcm, 16000, 2)
            self.status_changed.emit("识别中...")
            if self.isInterruptionRequested():
                return
            text = recognizer.recognize_google(audio, language="zh-CN")
        except ReSpeakerRecordingCancelled:
            return
        except ReSpeakerNoSpeechError:
            self.failed.emit("没有检测到语音输入。")
            return
        except sr.UnknownValueError:
            self.failed.emit("没有听清楚，请再说一次。")
            return
        except sr.RequestError as exc:
            self.failed.emit(f"语音识别服务不可用：{exc}")
            return
        except Exception as exc:
            self.failed.emit(f"语音识别失败：{exc}")
            return

        text = (text or "").strip()
        if text:
            self.recognized.emit(text)
        else:
            self.failed.emit("没有识别到有效中文。")

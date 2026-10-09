from __future__ import annotations

import logging
from contextlib import closing

from PySide6.QtCore import QThread, Signal

from hardware.audio_input.keyword_spotter import StreamingKeywordSpotter, microphone_frames
from hardware.audio_input.keyword_capture import KeywordCaptureInterrupted, CaptureReleaseError

logger = logging.getLogger(__name__)


class WakeWordWorker(QThread):
    capture_ready = Signal()
    wake_detected = Signal(str)
    failed = Signal(str)

    def __init__(self, parent=None, *, detector: StreamingKeywordSpotter, words: tuple[str, ...], mic_backend: str, input_device: str = ""):
        super().__init__(parent)
        self._detector = detector
        self._words = words
        self._mic_backend = mic_backend
        self._input_device = input_device
        self._capturing = False
        self._capture_released = False
        self.capture_release_error = None

    @property
    def capture_released(self):
        return (self.capture_release_error.released() if self.capture_release_error is not None
                else self._capture_released)

    def is_capturing_user_speech(self) -> bool:
        return self._capturing

    def run(self) -> None:
        try:
            stream = self._detector.create_stream(self._words)
            if self.isInterruptionRequested():
                return
            with closing(microphone_frames(self._mic_backend, self.isInterruptionRequested,
                         **({"input_device": self._input_device} if self._input_device else {}))) as frames:
                logger.info("event=desktop_kws_listening backend=%s", self._mic_backend)
                for index, pcm in enumerate(frames):
                    if self.isInterruptionRequested():
                        return
                    if index == 0:
                        logger.info('event=desktop_kws_audio_ready backend=%s', self._mic_backend)
                        self.capture_ready.emit()
                    keyword = self._detector.accept_pcm(stream, pcm)
                    if keyword:
                        self._capturing = True
                        break
                else:
                    return
            # Release the microphone before the role may start responding.
            if not self.isInterruptionRequested():
                self.wake_detected.emit(keyword)
        except CaptureReleaseError as exc:
            self.capture_release_error = exc
            self.failed.emit(str(exc))  # Cleanup outcomes also matter after cancellation.
        except KeywordCaptureInterrupted as exc:
            if not self.isInterruptionRequested():
                logger.warning('event=desktop_kws_capture_reopen reason=%s', exc)
                # Keep wake_enabled. handle_finished uses the existing bounded
                # retry delay and creates a fresh decoder/capture session.
                self.failed.emit('唤醒采音暂时中断，准备重新连接。')
        except Exception as exc:
            if not self.isInterruptionRequested():
                logger.warning("event=desktop_kws_failed error=%s", exc)
                self.failed.emit(f"唤醒监听不可用：{exc}")
        finally:
            self._capture_released = True

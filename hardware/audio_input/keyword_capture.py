"""Bounded native capture for the long-lived wake-word listener.

ALSA/PortAudio can spin inside a blocking read after suspend. Keeping just that
device handle in an owned process lets cancellation release it without killing
the desktop or loading a second keyword model. No microphone opens on import.
"""
from contextlib import closing
import multiprocessing
from multiprocessing.connection import wait
import signal
import struct
import threading
import time

from hardware.respeaker.audio import ReSpeakerAudioError

STARTUP_TIMEOUT = 5.
FRAME_TIMEOUT = 2.
FRAME_BYTES = 320 * 2


class KeywordCaptureInterrupted(ReSpeakerAudioError):
    """Drop the current decoder stream; the existing voice loop may reopen it."""


class CaptureReleaseError(ReSpeakerAudioError):
    """Retain an unconfirmed native owner instead of claiming capture stopped."""
    def __init__(self, process):
        super().__init__('采音进程尚未释放，请检查设备连接。')
        self.process = process

    def released(self):
        if self.process is None:
            return True
        if self.process.is_alive():
            return False
        self.process.close()
        self.process = None
        return True


def _capture_pcm(backend, output, stopped, input_device=""):
    parent = multiprocessing.parent_process()
    if parent is not None:
        def parent_exited():
            wait([parent.sentinel])
            signal.raise_signal(signal.SIGTERM)
        threading.Thread(target=parent_exited, name='keyword-capture-parent', daemon=True).start()
    opened = False
    def opened_stream():
        nonlocal opened
        opened = True
    try:
        from hardware.audio_input.keyword_spotter import _native_microphone_frames
        with closing(_native_microphone_frames(backend, stopped.is_set, on_open=opened_stream,
                     **({"input_device": input_device} if input_device else {}))) as frames:
            while not stopped.is_set():
                captured_at = time.time()
                try:
                    pcm = next(frames)
                except StopIteration:
                    break
                if len(pcm) != FRAME_BYTES:
                    raise ReSpeakerAudioError('唤醒采音帧长度异常')
                # Small, fixed messages (< 1 KiB) fit in an atomic pipe write;
                # terminating a stuck producer cannot leave half a PCM message.
                output.send_bytes(b'F' + struct.pack('!d', captured_at) + pcm)
    except Exception as exc:
        try:
            output.send_bytes((b'R' if opened else b'E') + str(exc).encode('utf-8')[:900])
        except (OSError, EOFError):
            pass
    finally:
        output.close()


def capture_frames(backend, should_stop, *, input_device=""):
    """Yield fresh mono PCM; cancellation also owns native-process cleanup."""
    context = multiprocessing.get_context('spawn')
    incoming, outgoing = context.Pipe(duplex=False)
    stopped = context.Event()
    args = (backend, outgoing, stopped, input_device) if input_device else (backend, outgoing, stopped)
    process = context.Process(target=_capture_pcm, args=args,
                              name='spica-keyword-capture', daemon=True)
    started = False
    try:
        if should_stop():
            return
        process.start()
        started = True
        outgoing.close()
        timeout = STARTUP_TIMEOUT
        deadline = time.monotonic() + timeout
        while not should_stop():
            if incoming.poll(.05):
                try:
                    packet = incoming.recv_bytes(1024)
                except (OSError, EOFError) as exc:
                    raise KeywordCaptureInterrupted('采音进程已断开') from exc
                if should_stop():
                    return
                if packet[:1] == b'E':
                    raise ReSpeakerAudioError(packet[1:].decode('utf-8', errors='replace'))
                if packet[:1] == b'R':
                    raise KeywordCaptureInterrupted(packet[1:].decode('utf-8', errors='replace'))
                if len(packet) != 9 + FRAME_BYTES or packet[:1] != b'F':
                    raise KeywordCaptureInterrupted('采音数据不完整')
                age = time.time() - struct.unpack('!d', packet[1:9])[0]
                if not 0 <= age <= timeout:
                    raise KeywordCaptureInterrupted('恢复后旧音频已过期，重新连接麦克风')
                timeout = FRAME_TIMEOUT
                deadline = time.monotonic() + timeout
                yield packet[9:]
            elif not process.is_alive() or time.monotonic() >= deadline:
                raise KeywordCaptureInterrupted('麦克风未继续提供音频，重新连接采音')
    finally:
        stopped.set()
        outgoing.close()
        incoming.close()
        if started:
            process.join(.1)
            if process.is_alive():
                process.terminate()
                process.join(.25)
            if process.is_alive():
                process.kill()
                process.join(.25)
            if process.is_alive():
                raise CaptureReleaseError(process)
        process.close()

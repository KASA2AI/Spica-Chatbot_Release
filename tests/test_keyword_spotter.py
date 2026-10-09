from __future__ import annotations

from types import SimpleNamespace

import pytest

from hardware.audio_input.keyword_spotter import microphone_frames


def _one_frame_then_hang(backend, output, stopped):
    import struct
    import time
    output.send_bytes(b'F' + struct.pack('!d', time.time()) + bytes(640))
    time.sleep(60)


def _old_frame_after_resume(backend, output, stopped):
    import struct
    import time
    output.send_bytes(b'F' + struct.pack('!d', time.time()-60) + bytes(640))
    time.sleep(60)


@pytest.mark.parametrize('native', [_one_frame_then_hang, _old_frame_after_resume])
def test_keyword_capture_reaps_stalled_or_pre_sleep_audio_before_reopening(monkeypatch, native):
    import multiprocessing
    from hardware.audio_input import keyword_capture as capture
    before = {p.pid for p in multiprocessing.active_children()}
    monkeypatch.setattr(capture, '_capture_pcm', native)
    monkeypatch.setattr(capture, 'FRAME_TIMEOUT', .15)
    frames = microphone_frames('respeaker', lambda: False)
    if native is _one_frame_then_hang:
        assert next(frames) == bytes(640)
    with pytest.raises(capture.KeywordCaptureInterrupted):
        next(frames)
    assert {p.pid for p in multiprocessing.active_children()} == before
    # The same adapter can immediately acquire a new child after cleanup.
    monkeypatch.setattr(capture, '_capture_pcm', _one_frame_then_hang)
    again = microphone_frames('respeaker', lambda: False)
    assert next(again) == bytes(640)
    again.close()
    assert {p.pid for p in multiprocessing.active_children()} == before


def test_keyword_capture_can_cancel_when_native_read_never_returns(monkeypatch):
    """A suspended ALSA read must not own the desktop microphone forever."""
    import multiprocessing
    import threading
    from hardware.respeaker import audio

    if 'fork' not in multiprocessing.get_all_start_methods():
        pytest.skip('Linux native-read fault injection')
    context = multiprocessing.get_context('fork')
    entered, release, stop = context.Event(), context.Event(), threading.Event()
    done, failures = threading.Event(), []

    class SuspendedStream:
        def read(self, *args, **kwargs):
            entered.set()
            release.wait(10)  # Reproduces native read ignoring the Python stop flag.
            return bytes(320 * 6 * 2)
        def stop_stream(self):
            pass
        def close(self):
            pass

    monkeypatch.setattr(multiprocessing, 'get_context', lambda *args: context)
    monkeypatch.setattr(audio, '_load_pyaudio', lambda: SimpleNamespace(
        PyAudio=lambda: SimpleNamespace(terminate=lambda: None)))
    monkeypatch.setattr(audio, '_open_respeaker_stream', lambda *a, **kw: SuspendedStream())

    def consume():
        try:
            list(microphone_frames('respeaker', stop.is_set))
        except Exception as exc:
            failures.append(exc)
        finally:
            done.set()

    reader = threading.Thread(target=consume, daemon=True)
    reader.start()
    try:
        assert entered.wait(2)
        stop.set()
        assert done.wait(1), 'The stale native read still blocks microphone handover'
        assert not failures
    finally:
        stop.set()
        # A killed child cannot acknowledge multiprocessing.Event.notify_all.
        # Only release the baseline's still-live blocked reader.
        if reader.is_alive():
            release.set()
        reader.join(2)


@pytest.mark.parametrize('fail_open', [False, True])
def test_native_capture_distinguishes_open_failure_from_recoverable_read_failure(monkeypatch, fail_open):
    import multiprocessing
    from hardware.audio_input import generic_mic
    from hardware.audio_input.keyword_capture import KeywordCaptureInterrupted
    from hardware.respeaker.audio import ReSpeakerAudioError
    if 'fork' not in multiprocessing.get_all_start_methods():
        pytest.skip('Linux device failure injection')
    context = multiprocessing.get_context('fork')
    monkeypatch.setattr(multiprocessing, 'get_context', lambda *a: context)
    def failed_read(*args, **kwargs):
        raise OSError('device lost')
    def open_device(frames):
        if fail_open:
            raise ReSpeakerAudioError('无法打开麦克风')
        return SimpleNamespace(read=failed_read, close=lambda: None)
    monkeypatch.setattr(generic_mic, '_open_default_mic_stream', open_device)
    with pytest.raises(ReSpeakerAudioError) as caught:
        next(microphone_frames('generic', lambda: False))
    assert type(caught.value) is (ReSpeakerAudioError if fail_open else KeywordCaptureInterrupted)


@pytest.fixture(scope="session")
def qapp():
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


@pytest.mark.parametrize("word", ["斯皮卡", "Spica"])
def test_role_name_keeps_its_spoken_pronunciations_under_one_keyword(tmp_path, monkeypatch, word):
    import sys
    from hardware.audio_input.keyword_spotter import StreamingKeywordSpotter

    phones = "s ī p í k ǎ S P IY1 K AH0 AA1".split()
    (tmp_path / "tokens.txt").write_text("\n".join(f"{phone} {i}" for i, phone in enumerate(phones)))
    (tmp_path / "en.phone").write_text("SPICA S P IY1 K AH0\n")
    monkeypatch.setitem(sys.modules, "sherpa_onnx", SimpleNamespace(
        text2token=lambda *args, **kwargs: [["s", "ī", "p", "í", "k", "ǎ"]],
    ))
    encoded = StreamingKeywordSpotter(tmp_path)._encode_keywords((word,)).splitlines()
    assert "S P IY1 K AA1 @wake_0" in encoded
    assert "S AH0 P IY1 K AA1 @wake_0" in encoded
    assert {line.split()[-1] for line in encoded} == {"@wake_0"}


@pytest.mark.parametrize("word", ["紗凪", "纱凪", "Sana", "沙娜"])
def test_sana_spoken_variant_has_its_own_threshold_and_keeps_configured_name(tmp_path, monkeypatch, word):
    import sys
    from hardware.audio_input.keyword_spotter import StreamingKeywordSpotter

    phones = "sh s ā zh ǐ n à l e S AA1 N AH0".split()
    (tmp_path / "tokens.txt").write_text("\n".join(f"{phone} {i}" for i, phone in enumerate(phones)))
    (tmp_path / "en.phone").write_text("SANA S AA1 N AH0\n")
    monkeypatch.setitem(sys.modules, "sherpa_onnx", SimpleNamespace(
        text2token=lambda *args, **kwargs: [["sh", "ā", "zh", "ǐ"]],
    ))

    encoded = StreamingKeywordSpotter(tmp_path)._encode_keywords((word,)).splitlines()
    assert "sh ā n à @wake_0" in encoded
    assert "sh ā l ā #0.15 @wake_0" in encoded
    assert "s ā l e @wake_0" in encoded
    assert {line.split()[-1] for line in encoded} == {"@wake_0"}


def test_streaming_mic_reuses_respeaker_channel_zero_and_closes_on_wake(monkeypatch):
    from hardware.audio_input.keyword_spotter import _native_microphone_frames
    import hardware.respeaker.audio as audio
    events = []
    raw = b"\x01\x00" + b"\x02\x00" * 5
    stream = SimpleNamespace(read=lambda *args, **kwargs: raw)
    device = SimpleNamespace(terminate=lambda: events.append("terminate"))
    monkeypatch.setattr(audio, "_load_pyaudio", lambda: SimpleNamespace(PyAudio=lambda: device))
    monkeypatch.setattr(audio, "_open_respeaker_stream", lambda *args: stream)
    monkeypatch.setattr(audio, "_close_stream", lambda stream: events.append("close"))
    frames = _native_microphone_frames("respeaker", lambda: False)
    assert next(frames) == b"\x01\x00"
    frames.close()
    assert events == ["close", "terminate"]


def test_streaming_generic_mic_releases_after_read_failure(monkeypatch):
    from hardware.audio_input.keyword_spotter import _native_microphone_frames
    import hardware.audio_input.generic_mic as audio
    events = []

    def fail(*args, **kwargs):
        raise OSError("device unplugged")

    stream = SimpleNamespace(read=fail, close=lambda: events.append("close"))
    monkeypatch.setattr(audio, "_open_default_mic_stream", lambda frames: stream)
    with pytest.raises(OSError, match="unplugged"):
        next(_native_microphone_frames("generic", lambda: False))
    assert events == ["close"]


def test_wake_worker_releases_mic_before_signalling_keyword(qapp, monkeypatch):
    import ui.workers.wake_word_worker as module
    events = []

    def frames(backend, stopped):
        try:
            yield b"pcm"
        finally:
            events.append("closed")

    detector = SimpleNamespace(create_stream=lambda words: object(), accept_pcm=lambda stream, pcm: "斯皮卡")
    monkeypatch.setattr(module, "microphone_frames", frames)
    worker = module.WakeWordWorker(detector=detector, words=("斯皮卡",), mic_backend="generic")
    worker.wake_detected.connect(lambda word: events.append(word))
    worker.run()
    assert events == ["closed", "斯皮卡"]
    worker.deleteLater()
    qapp.processEvents()


def test_passive_voice_controller_creates_kws_worker_without_calling_whisper(qapp, monkeypatch):
    from PySide6.QtCore import QCoreApplication, QEvent
    from ui.controllers.voice_input_controller import VoiceInputController
    from ui.workers.wake_word_worker import WakeWordWorker
    monkeypatch.setattr(WakeWordWorker, "start", lambda self: None)
    controller = VoiceInputController(None, lambda value: None, lambda value: None,
        lambda: False, lambda text: None, lambda text: None, lambda: True)
    controller.wake_enabled = True
    controller.wake_words = ("斯皮卡",)
    controller._start_speech_worker()
    assert isinstance(controller.speech_worker, WakeWordWorker)
    assert controller.speech_worker._words == ("斯皮卡",)
    controller.shutdown()
    controller.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

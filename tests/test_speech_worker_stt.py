"""ASR wiring, microphone selection, and no implicit network fallback."""

from types import SimpleNamespace

import pytest
from PySide6.QtWidgets import QApplication

from ui.workers.speech_worker import SpeechWorker


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def test_transcribe_uses_local_stt_when_wired(qapp):
    # Default path: the injected adapter handles it -- NO speech_recognition import,
    # NO network (so the old recognize_google freeze cannot occur).
    calls = []

    def _transcribe(pcm, *, sample_rate=16000):
        calls.append((pcm, sample_rate))
        return "你好世界"

    worker = SpeechWorker(stt_port=SimpleNamespace(transcribe=_transcribe))
    assert worker._transcribe(b"\x01\x02\x03\x04") == "你好世界"
    assert calls == [(b"\x01\x02\x03\x04", 16000)]


def test_transcribe_never_falls_back_when_unwired(qapp):
    worker = SpeechWorker(stt_port=None)
    with pytest.raises(RuntimeError, match="未配置"):
        worker._transcribe(b"\x00\x00")


def test_run_passes_resolved_end_silence_to_recorder(qapp, monkeypatch):
    # The configured trailing-silence threshold must actually reach the recorder:
    # RESPEAKER_END_SILENCE_SECONDS -> resolve_end_silence_seconds() -> the record call.
    import ui.workers.speech_worker as sw

    captured = {}
    monkeypatch.setattr(
        sw, "record_respeaker_channel0_hardware_vad",
        lambda **kw: (captured.update(kw), b"")[1],  # empty PCM -> run() returns after the call
    )
    monkeypatch.setenv("RESPEAKER_END_SILENCE_SECONDS", "1.3")

    SpeechWorker(stt_port=SimpleNamespace(), mic_backend="respeaker").run()

    assert captured["end_silence_seconds"] == 1.3


def test_run_dispatches_to_generic_backend(qapp, monkeypatch):
    # W3: mic_backend is a STRING; SpeechWorker dispatches internally (the host
    # never holds a recorder callable). The generic lane must receive the same
    # call face the respeaker lane gets (should_stop/on_speech_start/end_silence).
    import hardware.audio_input.generic_mic as gm

    captured = {}
    monkeypatch.setattr(
        gm, "record_generic_mic_software_vad",
        lambda **kw: (captured.update(kw), b"")[1],
    )
    monkeypatch.setenv("RESPEAKER_END_SILENCE_SECONDS", "1.1")

    SpeechWorker(stt_port=SimpleNamespace(), mic_backend="generic").run()

    assert captured["end_silence_seconds"] == 1.1
    assert callable(captured["should_stop"]) and callable(captured["on_speech_start"])


def test_explicit_respeaker_backend_is_preserved(qapp, monkeypatch):
    # Byte-equivalence guard: no mic_backend argument -> the existing hardware
    # path, resolved through the MODULE namespace (so existing monkeypatch-based
    # tests and the production import both keep working).
    import ui.workers.speech_worker as sw

    called = []
    monkeypatch.setattr(
        sw, "record_respeaker_channel0_hardware_vad", lambda **kw: (called.append(1), b"")[1]
    )
    SpeechWorker(stt_port=SimpleNamespace(), mic_backend="respeaker").run()
    assert called == [1]


def test_run_unknown_backend_fails_fatally_not_forever(qapp):
    # P2-3: a mis-wired backend string must stop the voice loop (fatal marker),
    # never spin the retry loop.
    from ui.workers.speech_worker import is_fatal_speech_error

    failures = []
    worker = SpeechWorker(stt_port=SimpleNamespace(), mic_backend="usb")
    worker.failed.connect(failures.append)
    worker.run()
    assert len(failures) == 1
    assert is_fatal_speech_error(failures[0])


def test_apphost_builds_selected_backend_without_loading_models():
    from spica.config.schema import AppConfig, SttConfig
    from spica.host.app_host import AppHost
    from spica.config.secrets import Secrets
    host = AppHost()
    host.secrets = Secrets(dashscope_api_key="test-asr-key")
    host.config = AppConfig(stt=SttConfig(backend="qwen_cloud"))
    cloud = host._new_stt_adapter()
    assert cloud.name == "qwen_cloud"
    cloud.close()
    host.config = AppConfig(stt=SttConfig(model="/missing"))
    local = host._new_stt_adapter()
    assert local.name == "qwen_asr" and local._process is None
    local.close()


def test_interrupt_after_recognition_never_delivers_stale_transcript(qapp, monkeypatch):
    worker = SpeechWorker(stt_port=SimpleNamespace())
    def transcribe(pcm):
        worker.requestInterruption()
        return "cancelled transcript"
    interrupted = []
    monkeypatch.setattr(worker, "requestInterruption", lambda: interrupted.append(True))
    monkeypatch.setattr(worker, "isInterruptionRequested", lambda: bool(interrupted))
    monkeypatch.setattr(worker, "_record", lambda **kwargs: b"\1\0")
    monkeypatch.setattr(worker, "_transcribe", transcribe)
    recognized = []
    worker.recognized.connect(recognized.append)
    worker.run()
    assert recognized == []

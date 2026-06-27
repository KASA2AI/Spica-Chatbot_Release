"""Plan B wiring: SpeechWorker dispatches PCM->text to the injected local STT by
default (no network), and only falls back to recognize_google when unwired; and
AppHost builds the resident adapter only for backend=faster_whisper."""

from types import SimpleNamespace

import pytest
from PySide6.QtWidgets import QApplication

from hardware.respeaker.speech_worker import SpeechWorker


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


def test_transcribe_falls_back_to_google_only_when_unwired(qapp, monkeypatch):
    sr = pytest.importorskip("speech_recognition")
    monkeypatch.setattr(sr, "Recognizer", lambda: SimpleNamespace(
        recognize_google=lambda audio, language=None: "google text"))
    monkeypatch.setattr(sr, "AudioData", lambda *a, **k: object())
    worker = SpeechWorker(stt_port=None)  # no adapter -> legacy fallback
    assert worker._transcribe(b"\x00\x00") == "google text"


def test_run_passes_resolved_end_silence_to_recorder(qapp, monkeypatch):
    # The configured trailing-silence threshold must actually reach the recorder:
    # RESPEAKER_END_SILENCE_SECONDS -> resolve_end_silence_seconds() -> the record call.
    import hardware.respeaker.speech_worker as sw

    captured = {}
    monkeypatch.setattr(
        sw, "record_respeaker_channel0_hardware_vad",
        lambda **kw: (captured.update(kw), b"")[1],  # empty PCM -> run() returns after the call
    )
    monkeypatch.setenv("RESPEAKER_END_SILENCE_SECONDS", "1.3")

    SpeechWorker(stt_port=None).run()

    assert captured["end_silence_seconds"] == 1.3


def test_apphost_builds_stt_adapter_only_for_faster_whisper():
    from spica.config.schema import AppConfig, SttConfig
    from spica.host.app_host import AppHost

    host = AppHost()
    host.config = AppConfig(stt=SttConfig(backend="google"))
    assert host._new_stt_adapter() is None  # google -> no local adapter (worker fallback)

    host.config = AppConfig(stt=SttConfig(backend="faster_whisper", model="x", device="cpu"))
    adapter = host._new_stt_adapter()
    assert adapter is not None and adapter.name == "faster_whisper"
    assert adapter._model is None  # built but NOT loaded (lazy)


if __name__ == "__main__":
    import unittest

    unittest.main()

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hardware.respeaker import audio as respeaker_audio
from hardware.respeaker.control import _find_tuning_py


class FakeStream:
    def __init__(self):
        self.read_count = 0
        self.closed = False

    def read(self, frames, exception_on_overflow=False):
        self.read_count += 1
        raw = bytearray()
        for _ in range(frames):
            for channel in range(respeaker_audio.CHANNELS):
                raw.extend((self.read_count * 10 + channel).to_bytes(2, "little", signed=True))
        return bytes(raw)

    def stop_stream(self):
        pass

    def close(self):
        self.closed = True


class FakeAudio:
    def __init__(self):
        self.stream = FakeStream()
        self.open_kwargs = None
        self.terminated = False

    def get_device_count(self):
        return 3

    def get_device_info_by_index(self, index):
        devices = [
            {"name": "default", "maxInputChannels": 2},
            {"name": "monitor", "maxInputChannels": 2},
            {"name": "SEEED ReSpeaker 4 Mic Array", "maxInputChannels": 6},
        ]
        return devices[index]

    def open(self, **kwargs):
        self.open_kwargs = kwargs
        return self.stream

    def terminate(self):
        self.terminated = True


class FakePyAudioModule:
    paInt16 = 8

    def __init__(self, audio):
        self._audio = audio

    def PyAudio(self):
        return self._audio


class FakeControl:
    values = [False]

    def __init__(self):
        self.index = 0
        self.closed = False

    def is_voice(self):
        value = self.values[min(self.index, len(self.values) - 1)]
        self.index += 1
        return value

    def close(self):
        self.closed = True


class ReSpeakerAudioTests(unittest.TestCase):
    def test_extract_channel0_from_six_channel_s16le(self):
        raw = bytearray()
        for frame in range(3):
            for channel in range(6):
                raw.extend((frame * 10 + channel).to_bytes(2, "little", signed=True))

        self.assertEqual(
            respeaker_audio._extract_channel0(bytes(raw)),
            b"\x00\x00\x0a\x00\x14\x00",
        )

    def test_hardware_vad_records_preroll_until_trailing_silence(self):
        fake_audio = FakeAudio()
        fake_pyaudio = FakePyAudioModule(fake_audio)
        FakeControl.values = [False, False, False, True, True, False, False]

        with patch.object(respeaker_audio, "_load_pyaudio", return_value=fake_pyaudio), patch.object(
            respeaker_audio, "ReSpeakerControl", FakeControl
        ):
            pcm = respeaker_audio.record_respeaker_channel0_hardware_vad(
                max_seconds=2.0,
                start_timeout=1.0,
                end_silence_seconds=0.04,
                min_speech_seconds=0.02,
                pre_roll_seconds=0.04,
                vad_poll_seconds=0.02,
            )

        self.assertEqual(len(pcm), 6 * 320 * 2)
        self.assertEqual(fake_audio.open_kwargs["channels"], 6)
        self.assertEqual(fake_audio.open_kwargs["rate"], 16000)
        self.assertEqual(fake_audio.open_kwargs["input_device_index"], 2)
        self.assertTrue(fake_audio.terminated)

    def test_on_speech_start_fires_once_when_vad_triggers(self):
        fake_audio = FakeAudio()
        fake_pyaudio = FakePyAudioModule(fake_audio)
        # idle, idle, SPEECH (x2), trailing silence -> started flips once.
        FakeControl.values = [False, False, True, True, False, False]
        fired = []

        with patch.object(respeaker_audio, "_load_pyaudio", return_value=fake_pyaudio), patch.object(
            respeaker_audio, "ReSpeakerControl", FakeControl
        ):
            respeaker_audio.record_respeaker_channel0_hardware_vad(
                max_seconds=2.0, start_timeout=1.0, end_silence_seconds=0.04,
                min_speech_seconds=0.02, pre_roll_seconds=0.04, vad_poll_seconds=0.02,
                on_speech_start=lambda: fired.append(True),
            )

        self.assertEqual(fired, [True])  # exactly once, at speech onset

    def test_on_speech_start_not_fired_when_no_speech(self):
        fake_audio = FakeAudio()
        fake_pyaudio = FakePyAudioModule(fake_audio)
        FakeControl.values = [False]  # never any voice -> start_timeout -> NoSpeech
        fired = []

        with patch.object(respeaker_audio, "_load_pyaudio", return_value=fake_pyaudio), patch.object(
            respeaker_audio, "ReSpeakerControl", FakeControl
        ):
            with self.assertRaises(respeaker_audio.ReSpeakerNoSpeechError):
                respeaker_audio.record_respeaker_channel0_hardware_vad(
                    max_seconds=1.0, start_timeout=0.1, end_silence_seconds=0.04,
                    min_speech_seconds=0.02, pre_roll_seconds=0.04, vad_poll_seconds=0.02,
                    on_speech_start=lambda: fired.append(True),
                )

        self.assertEqual(fired, [])  # idle-listening never marks capturing

    def test_tuning_path_can_come_from_environment(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tuning_path = Path(tmpdir) / "tuning.py"
            tuning_path.write_text("class Tuning: pass\n", encoding="utf-8")
            with patch.dict(os.environ, {"RESPEAKER_TUNING_PATH": tmpdir}):
                self.assertEqual(_find_tuning_py(), tuning_path)


class EndSilenceResolutionTests(unittest.TestCase):
    """The endpoint trailing-silence threshold: raised default + RESPEAKER_* knob.

    The recording LOOP logic is unchanged (test_hardware_vad_records_* still pin it);
    these pin only the new tunable: a higher default so slow speech / mid-sentence
    pauses are not cut off, overridable per-machine via RESPEAKER_END_SILENCE_SECONDS.
    """

    def test_default_is_raised_to_0_9(self):
        # Intentional value change from the old hardcoded 0.55 (documented in the dump).
        self.assertEqual(respeaker_audio.DEFAULT_END_SILENCE_SECONDS, 0.9)

    def test_function_default_uses_the_constant(self):
        import inspect

        default = inspect.signature(
            respeaker_audio.record_respeaker_channel0_hardware_vad
        ).parameters["end_silence_seconds"].default
        self.assertEqual(default, respeaker_audio.DEFAULT_END_SILENCE_SECONDS)

    def test_resolve_unset_returns_default(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("RESPEAKER_END_SILENCE_SECONDS", None)
            self.assertEqual(respeaker_audio.resolve_end_silence_seconds(), 0.9)

    def test_resolve_reads_env_override(self):
        with patch.dict(os.environ, {"RESPEAKER_END_SILENCE_SECONDS": "1.25"}):
            self.assertEqual(respeaker_audio.resolve_end_silence_seconds(), 1.25)

    def test_resolve_falls_back_on_invalid_or_nonpositive(self):
        for bad in ("", "abc", "-1", "0"):
            with patch.dict(os.environ, {"RESPEAKER_END_SILENCE_SECONDS": bad}):
                self.assertEqual(
                    respeaker_audio.resolve_end_silence_seconds(), 0.9, msg=f"value={bad!r}"
                )


if __name__ == "__main__":
    unittest.main()

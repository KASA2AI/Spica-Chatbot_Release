import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from hardware.respeaker import audio as respeaker_audio
from hardware.respeaker import control as respeaker_control
from hardware.respeaker.control import ReSpeakerControl, _find_tuning_py


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

    def test_short_vad_pulse_is_rejected_at_endpoint_or_duration_cap(self):
        for max_seconds in (8.0, 0.08):
            with self.subTest(max_seconds=max_seconds):
                fake_audio = FakeAudio()
                control = FakeControl()
                control.values = [False, True, False]  # preflight, 20 ms pulse, silence
                with patch.object(
                    respeaker_audio, "_load_pyaudio", return_value=FakePyAudioModule(fake_audio)
                ), patch.object(respeaker_audio, "ReSpeakerControl", return_value=control):
                    with self.assertRaises(respeaker_audio.ReSpeakerNoSpeechError):
                        respeaker_audio.record_respeaker_channel0_hardware_vad(
                            max_seconds=max_seconds,
                        )
                self.assertTrue(fake_audio.stream.closed)
                self.assertTrue(fake_audio.terminated)
                self.assertTrue(control.closed)

    def test_waiting_for_speech_does_not_consume_utterance_duration_cap(self):
        fake_audio = FakeAudio()
        control = FakeControl()
        control.values = [False] + [False] * 6 + [True] * 4
        with patch.object(
            respeaker_audio, "_load_pyaudio", return_value=FakePyAudioModule(fake_audio)
        ), patch.object(respeaker_audio, "ReSpeakerControl", return_value=control):
            pcm = respeaker_audio.record_respeaker_channel0_hardware_vad(
                start_timeout=0.2, max_seconds=0.08,
                min_speech_seconds=0.02, pre_roll_seconds=0.04,
            )
        self.assertEqual(fake_audio.stream.read_count, 10)
        self.assertEqual(len(pcm), 6 * 320 * 2)  # 2 pre-roll + 4 speech frames
        self.assertTrue(fake_audio.stream.closed)
        self.assertTrue(fake_audio.terminated)
        self.assertTrue(control.closed)

    def test_short_speech_meeting_minimum_is_kept(self):
        fake_audio = FakeAudio()
        control = FakeControl()
        control.values = [False] + [True] * 10 + [False] * 2
        with patch.object(
            respeaker_audio, "_load_pyaudio", return_value=FakePyAudioModule(fake_audio)
        ), patch.object(respeaker_audio, "ReSpeakerControl", return_value=control):
            pcm = respeaker_audio.record_respeaker_channel0_hardware_vad(
                end_silence_seconds=0.04,
            )
        self.assertEqual(len(pcm), 12 * 320 * 2)  # 200 ms speech + trailing silence

    def test_tuning_path_can_come_from_environment(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tuning_path = Path(tmpdir) / "tuning.py"
            tuning_path.write_text("class Tuning: pass\n", encoding="utf-8")
            with patch.dict(os.environ, {"RESPEAKER_TUNING_PATH": tmpdir}):
                self.assertEqual(_find_tuning_py(), tuning_path)


class InputGainTests(unittest.TestCase):
    def setUp(self):
        self.values = {"AGCMAXGAIN": 31.6, "AGCGAIN": 15.0}
        self.control = object.__new__(ReSpeakerControl)
        self.control._tuning = Mock()
        self.control._tuning.read.side_effect = self.values.__getitem__
        self.control._tuning.write.side_effect = self.values.__setitem__
        self.control.close = Mock()
        self.factory = self.enterContext(patch.object(
            respeaker_audio, "ReSpeakerControl", return_value=self.control,
        ))
        self.enterContext(patch.dict(os.environ, {"RESPEAKER_AGC_MAX_GAIN": "8"}))
        self.audio = FakeAudio()

    def open_stream(self):
        return respeaker_audio._open_respeaker_stream(
            FakePyAudioModule(self.audio), self.audio, 320,
        )

    def test_capture_reapplies_gain_cap_after_device_reset_without_boosting_quiet_gain(self):
        for initial_gain in (15.0, 2.0):
            with self.subTest(initial_gain=initial_gain):
                self.values.update(AGCMAXGAIN=31.6, AGCGAIN=initial_gain)
                self.assertIs(self.open_stream(), self.audio.stream)
                self.assertEqual(self.values["AGCMAXGAIN"], 8.0)
                self.assertEqual(self.values["AGCGAIN"], min(initial_gain, 8.0))
        self.assertEqual(self.control.close.call_count, 2)

    def test_unconfigured_capture_does_not_change_hardware(self):
        os.environ.pop("RESPEAKER_AGC_MAX_GAIN")
        self.assertIs(self.open_stream(), self.audio.stream)
        self.factory.assert_not_called()

    def test_invalid_gain_is_rejected_before_opening_hardware(self):
        for value in ("nan", "0", "1001"):
            with self.subTest(value=value):
                os.environ["RESPEAKER_AGC_MAX_GAIN"] = value
                with self.assertRaisesRegex(respeaker_audio.ReSpeakerAudioError, "AGC"):
                    self.open_stream()
        self.factory.assert_not_called()
        self.assertIsNone(self.audio.open_kwargs)

    def test_gain_write_failure_releases_usb_and_does_not_start_capture(self):
        self.control._tuning.write.side_effect = OSError("device unplugged")
        with self.assertRaisesRegex(respeaker_audio.ReSpeakerAudioError, "device unplugged"):
            self.open_stream()
        self.control.close.assert_called_once()
        self.assertIsNone(self.audio.open_kwargs)


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


def _usb_controls(monkeypatch, devices):
    class Tuning:
        def __init__(self, device):
            self.device = device

        def is_voice(self):
            values = self.device.voices
            return values.pop(0) if len(values) > 1 else values[0]

        def read(self, key):
            return self.device.gain[key]

        def write(self, key, value):
            self.device.gain[key] = value

    def find(**options):
        return iter(devices) if options.get('find_all') else devices[0] if devices else None
    monkeypatch.setattr(ReSpeakerControl, '_load_pyusb', staticmethod(lambda: (
        SimpleNamespace(find=find), SimpleNamespace(dispose_resources=lambda device: None))))
    monkeypatch.setattr(ReSpeakerControl, '_load_tuning_module', staticmethod(lambda: SimpleNamespace(Tuning=Tuning)))


@pytest.mark.parametrize('selected_present', [True, False])
def test_selected_respeaker_binds_pcm_vad_and_gain_to_the_same_usb(monkeypatch, selected_present):
    from hardware.audio_input import devices
    import json
    a = SimpleNamespace(bus=1, address=4, voices=[False], gain={'AGCMAXGAIN': 31.6, 'AGCGAIN': 15.})
    b = SimpleNamespace(bus=1, address=9, voices=[False, True, True, False],
                        gain={'AGCMAXGAIN': 31.6, 'AGCGAIN': 15.})
    _usb_controls(monkeypatch, [a, b] if selected_present else [a])
    selected = json.dumps(['alsa_pcm', 'ArrayB', 0], separators=(',', ':'))
    monkeypatch.setattr(devices, '_alsa_capture_devices', lambda: [
        {'id': selected, 'card': 8, 'pcm': 0}])
    read_text = Path.read_text
    monkeypatch.setattr(Path, 'read_text', lambda path, *args, **kwargs:
        '001/009\n' if path == Path('/proc/asound/card8/usbbus') else read_text(path, *args, **kwargs))
    audio = FakeAudio()
    info = [
        {'name': 'default', 'maxInputChannels': 2, 'hostApi': 0},
        {'name': 'ReSpeaker A (hw:3,0)', 'maxInputChannels': 6, 'hostApi': 0},
        {'name': 'ReSpeaker B (hw:8,0)', 'maxInputChannels': 6, 'hostApi': 0},
    ]
    audio.get_device_info_by_index = lambda index: info[index]
    audio.get_host_api_info_by_index = lambda index: {'name': 'ALSA'}
    monkeypatch.setattr(respeaker_audio, '_load_pyaudio', lambda: FakePyAudioModule(audio))
    monkeypatch.setenv('RESPEAKER_AGC_MAX_GAIN', '8')
    monkeypatch.delenv('RESPEAKER_REQUIRE_HARDWARE_VAD', raising=False)
    if selected_present:
        pcm = respeaker_audio.record_respeaker_channel0_hardware_vad(
            input_device=selected, start_timeout=.04, min_speech_seconds=.02, end_silence_seconds=.02)
        assert pcm and audio.open_kwargs['input_device_index'] == 2
        assert b.gain == {'AGCMAXGAIN': 8., 'AGCGAIN': 8.}
    else:
        with pytest.raises(respeaker_audio.ReSpeakerAudioError, match='USB'):
            respeaker_audio.record_respeaker_channel0_hardware_vad(input_device=selected, start_timeout=.04)
        assert audio.open_kwargs is None  # No fallback capture after an identity mismatch.
    assert a.gain == {'AGCMAXGAIN': 31.6, 'AGCGAIN': 15.}
    assert audio.terminated


def test_ambiguous_respeaker_usb_without_pcm_mapping_is_rejected(monkeypatch):
    devices = [SimpleNamespace(bus=1, address=number) for number in (4, 9)]
    _usb_controls(monkeypatch, devices)
    with pytest.raises(respeaker_control.ReSpeakerControlError, match='无法唯一'):
        ReSpeakerControl()


if __name__ == "__main__":
    unittest.main()

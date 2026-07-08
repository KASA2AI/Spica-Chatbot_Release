"""W3 generic-mic recorder: webrtcvad software segmentation behavior against
fake streams / fake VAD frames (no real microphone, no PyAudio), plus the P2-3
fatal-envelope contract (open failures must hit FATAL_SPEECH_ERROR_MARKERS).

Frame math used throughout: 16 kHz mono int16, 20 ms frames -> 320 samples ->
640 bytes per frame; a frame of b"\\x01..." is "speech" to the fake VAD and
b"\\x00..." is silence (the fake keys off the first byte).
"""

import unittest

from hardware.audio_input.generic_mic import (
    FRAME_BYTES,
    record_generic_mic_software_vad,
)
from hardware.respeaker.audio import (
    ReSpeakerAudioError,
    ReSpeakerNoSpeechError,
    ReSpeakerRecordingCancelled,
)
from hardware.respeaker.speech_worker import is_fatal_speech_error

SPEECH = b"\x01" * FRAME_BYTES
SILENCE = b"\x00" * FRAME_BYTES


class _FakeStream:
    """Duck-typed stream: .read(frames, exception_on_overflow=False) -> bytes."""

    def __init__(self, frames):
        self._frames = list(frames)
        self.closed = False

    def read(self, _frames_per_buffer, exception_on_overflow=False):
        assert exception_on_overflow is False  # overflow tolerance is part of the contract
        if not self._frames:
            raise AssertionError("test script exhausted before the recorder finished")
        item = self._frames.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def close(self):
        self.closed = True


class _FakeVad:
    def is_speech(self, frame, sample_rate):
        assert sample_rate == 16000
        assert len(frame) == FRAME_BYTES  # never feed webrtcvad an illegal frame
        return frame[0] == 1


def _record(frames, **kwargs):
    stream = _FakeStream(frames)
    kwargs.setdefault("vad", _FakeVad())
    kwargs.setdefault("stream_factory", lambda frames_per_buffer: stream)
    # Small windows so scripts stay short: 3 silence frames (0.06 s) end an
    # utterance; 5 frames (0.1 s) of leading silence time out the start.
    kwargs.setdefault("end_silence_seconds", 0.06)
    kwargs.setdefault("start_timeout", 0.1)
    kwargs.setdefault("min_speech_seconds", 0.02)
    kwargs.setdefault("pre_roll_seconds", 0.04)
    kwargs.setdefault("max_seconds", 10.0)
    return stream, record_generic_mic_software_vad(**kwargs)


class SegmentationTest(unittest.TestCase):
    def test_speech_then_trailing_silence_closes_segment(self):
        # 2 speech frames then silence: closes after 3 consecutive silence frames
        # and the segment contains speech + trailing silence.
        stream, pcm = _record([SPEECH, SPEECH, SILENCE, SILENCE, SILENCE, SILENCE])
        self.assertEqual(pcm, SPEECH * 2 + SILENCE * 3)
        self.assertTrue(stream.closed)

    def test_pre_roll_frames_are_included(self):
        # 2 leading silence frames sit in the pre-roll (0.04 s = 2 frames) and are
        # prepended when speech starts -- word onsets are not clipped.
        _, pcm = _record([SILENCE, SILENCE, SPEECH, SILENCE, SILENCE, SILENCE])
        self.assertEqual(pcm, SILENCE * 2 + SPEECH + SILENCE * 3)

    def test_mid_utterance_pause_shorter_than_end_silence_does_not_close(self):
        # 2-frame pause (0.04 s) < end_silence (0.06 s): the utterance continues.
        _, pcm = _record([SPEECH, SILENCE, SILENCE, SPEECH, SILENCE, SILENCE, SILENCE])
        self.assertEqual(pcm, SPEECH + SILENCE * 2 + SPEECH + SILENCE * 3)

    def test_all_silence_times_out_with_no_speech_error(self):
        with self.assertRaises(ReSpeakerNoSpeechError):
            _record([SILENCE] * 6)

    def test_should_stop_cancels(self):
        with self.assertRaises(ReSpeakerRecordingCancelled):
            _record([SPEECH, SPEECH], should_stop=lambda: True)

    def test_min_speech_not_met_keeps_recording_past_silence_window(self):
        # min_speech 0.1 s = 5 frames: after 1 speech + 3 silence (elapsed 4 frames,
        # < 5) the segment must NOT close; it closes at the next silence frame
        # (elapsed 5 frames >= min AND trailing silence >= 3 frames).
        _, pcm = _record(
            [SPEECH, SILENCE, SILENCE, SILENCE, SILENCE, SILENCE],
            min_speech_seconds=0.1,
        )
        self.assertEqual(pcm, SPEECH + SILENCE * 4)

    def test_max_seconds_caps_a_started_recording(self):
        # max 0.08 s = 4 frames of session time: returns what was recorded.
        _, pcm = _record([SPEECH, SPEECH, SPEECH, SPEECH, SPEECH], max_seconds=0.08)
        self.assertEqual(pcm, SPEECH * 4)

    def test_on_speech_start_fires_once_and_exceptions_never_kill_recording(self):
        calls = []

        def _hint():
            calls.append(1)
            raise RuntimeError("hint boom")

        _, pcm = _record(
            [SILENCE, SPEECH, SPEECH, SILENCE, SILENCE, SILENCE],
            on_speech_start=_hint,
        )
        self.assertEqual(calls, [1])
        self.assertTrue(pcm)


class FailureEnvelopeTest(unittest.TestCase):
    def test_stream_factory_blowup_is_fatal_cannot_open_mic(self):
        with self.assertRaises(ReSpeakerAudioError) as ctx:
            record_generic_mic_software_vad(
                vad=_FakeVad(),
                stream_factory=lambda fpb: (_ for _ in ()).throw(RuntimeError("PortAudio: device unavailable")),
            )
        message = str(ctx.exception)
        self.assertIn("无法打开麦克风", message)
        self.assertIn("device unavailable", message)
        self.assertTrue(is_fatal_speech_error(message))  # P2-3: must STOP the loop

    def test_missing_pyaudio_is_fatal_with_existing_marker(self):
        import hardware.audio_input.generic_mic as gm

        original = gm._load_pyaudio
        gm._load_pyaudio = lambda: (_ for _ in ()).throw(
            ReSpeakerAudioError("缺少 PyAudio，无法从麦克风录音。")
        )
        try:
            with self.assertRaises(ReSpeakerAudioError) as ctx:
                record_generic_mic_software_vad(vad=_FakeVad())
        finally:
            gm._load_pyaudio = original
        self.assertTrue(is_fatal_speech_error(str(ctx.exception)))

    def test_short_read_is_transient_not_fatal(self):
        # A truncated read (dying device) fails THIS recording with a readable,
        # NON-fatal envelope -- retry unit is the whole recording (P2-3): the next
        # open attempt either works or hits the fatal open envelope.
        with self.assertRaises(ReSpeakerAudioError) as ctx:
            _record([SPEECH, b"\x01" * 10])
        message = str(ctx.exception)
        self.assertFalse(is_fatal_speech_error(message))

    def test_transient_read_blowup_is_not_fatal(self):
        with self.assertRaises(ReSpeakerAudioError) as ctx:
            _record([SPEECH, OSError("Input overflowed-ish transient")])
        self.assertFalse(is_fatal_speech_error(str(ctx.exception)))

    def test_cancel_and_no_speech_are_not_wrapped(self):
        # The ReSpeaker* control-flow exceptions must pass through unwrapped so
        # SpeechWorker's except arms keep their current semantics.
        with self.assertRaises(ReSpeakerRecordingCancelled):
            _record([SPEECH], should_stop=lambda: True)
        with self.assertRaises(ReSpeakerNoSpeechError):
            _record([SILENCE] * 6)


if __name__ == "__main__":
    unittest.main()

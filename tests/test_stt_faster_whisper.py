"""Plan B: local faster-whisper STT adapter.

The load-bearing pin is test_model_loaded_once_across_many_transcribes: the heavy
WhisperModel must be constructed EXACTLY ONCE and reused, never per call (the whole
point of the resident-singleton design). WhisperModel is patched, so these run on
CI with no GPU and no model download.
"""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

from spica.adapters.stt.faster_whisper import FasterWhisperAdapter
from spica.ports.stt import SpeechToTextPort


def _seg(text):
    return SimpleNamespace(text=text)


def _adapter():
    return FasterWhisperAdapter(model="x", device="cpu", compute_type="int8", language="zh")


class FasterWhisperAdapterTest(unittest.TestCase):
    def test_is_speech_to_text_port(self):
        self.assertIsInstance(_adapter(), SpeechToTextPort)

    def test_construction_does_not_load_model(self):
        # Construction must be cheap (no WhisperModel build) so app startup never
        # blocks on it -- the model loads at warmup/first transcribe instead.
        with patch("faster_whisper.WhisperModel") as MockModel:
            _adapter()
            self.assertEqual(MockModel.call_count, 0)

    def test_model_loaded_once_across_many_transcribes(self):
        # THE never-reload pin: many transcribes -> exactly ONE WhisperModel build.
        with patch("faster_whisper.WhisperModel") as MockModel:
            MockModel.return_value.transcribe.return_value = ([_seg("你好")], SimpleNamespace())
            adapter = _adapter()
            pcm = np.zeros(1600, dtype=np.int16).tobytes()
            for _ in range(5):
                adapter.transcribe(pcm)
            self.assertEqual(MockModel.call_count, 1)  # loaded once, reused 5x
            self.assertEqual(MockModel.return_value.transcribe.call_count, 5)

    def test_warmup_then_transcribe_no_second_load(self):
        # Warmup loads the model; the subsequent real transcribe must NOT reload it.
        with patch("faster_whisper.WhisperModel") as MockModel:
            MockModel.return_value.transcribe.return_value = ([_seg("x")], SimpleNamespace())
            adapter = _adapter()
            self.assertTrue(adapter.warmup()["ok"])
            adapter.transcribe(np.zeros(1600, dtype=np.int16).tobytes())
            self.assertEqual(MockModel.call_count, 1)  # warmup + transcribe share one model

    def test_pcm_int16_to_float32_normalized(self):
        with patch("faster_whisper.WhisperModel") as MockModel:
            MockModel.return_value.transcribe.return_value = ([_seg("")], SimpleNamespace())
            adapter = _adapter()
            pcm = np.array([0, 32767, -32768, 16384], dtype=np.int16).tobytes()
            adapter.transcribe(pcm)
            audio = MockModel.return_value.transcribe.call_args[0][0]
            self.assertEqual(audio.dtype, np.float32)
            self.assertAlmostEqual(float(audio[1]), 32767 / 32768.0, places=5)
            self.assertAlmostEqual(float(audio[2]), -1.0, places=5)

    def test_transcribe_joins_segments_and_strips(self):
        with patch("faster_whisper.WhisperModel") as MockModel:
            MockModel.return_value.transcribe.return_value = (
                [_seg(" 你好"), _seg("，"), _seg("世界 ")], SimpleNamespace(),
            )
            self.assertEqual(_adapter().transcribe(b"\x00\x00"), "你好，世界")

    def test_transcribe_passes_config_to_model(self):
        with patch("faster_whisper.WhisperModel") as MockModel:
            MockModel.return_value.transcribe.return_value = ([_seg("x")], SimpleNamespace())
            FasterWhisperAdapter(
                model="m", device="cpu", compute_type="int8", language="zh",
                beam_size=7, vad_filter=True,
            ).transcribe(b"\x00\x00")
            _args, kwargs = MockModel.return_value.transcribe.call_args
            self.assertEqual(kwargs["language"], "zh")
            self.assertEqual(kwargs["beam_size"], 7)
            self.assertTrue(kwargs["vad_filter"])

    def test_warmup_failure_is_non_fatal(self):
        # A load failure during warmup is REPORTED (ok=False + error), never raised
        # (mirrors the TTS warmup contract; startup must not crash).
        with patch("faster_whisper.WhisperModel", side_effect=RuntimeError("no model")):
            result = _adapter().warmup()
            self.assertFalse(result["ok"])
            self.assertIn("no model", result["error"])

    def test_transcribe_load_failure_raises_clear_cause(self):
        # When the model can't load, transcribe RAISES (so SpeechWorker emits a
        # clear "语音识别失败：<cause>" -> diagnosable, not a silent no-text mic).
        with patch("faster_whisper.WhisperModel", side_effect=RuntimeError("model not found")):
            with self.assertRaises(RuntimeError):
                _adapter().transcribe(b"\x00\x00")

    def test_warmup_and_transcribe_never_decode_concurrently(self):
        # 2026-07 review P1: warmup (warmup-worker thread) now REALLY decodes,
        # while the mic is already live -- without _infer_lock a first utterance
        # decodes concurrently on the same CTranslate2 model (reproduced at
        # max_concurrent=2 with a fake model). The lock must cover the
        # transcribe() call AND the full lazy-generator drain.
        import threading
        import time

        adapter = _adapter()
        state = {"active": 0, "max": 0}
        gate = threading.Lock()

        def _lazy_segments():
            with gate:
                state["active"] += 1
                state["max"] = max(state["max"], state["active"])
            time.sleep(0.05)  # hold the decode window open across threads
            yield _seg("x")
            with gate:
                state["active"] -= 1

        adapter._model = SimpleNamespace(
            transcribe=lambda *a, **k: (_lazy_segments(), SimpleNamespace())
        )
        pcm = np.zeros(1600, dtype=np.int16).tobytes()
        threads = [threading.Thread(target=adapter.warmup)] + [
            threading.Thread(target=adapter.transcribe, args=(pcm,)) for _ in range(3)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(state["max"], 1)


if __name__ == "__main__":
    unittest.main()

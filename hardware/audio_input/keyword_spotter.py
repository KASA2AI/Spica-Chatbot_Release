"""Local CPU keyword spotting. No downloads or microphone handles on import."""
from __future__ import annotations

import json
import logging
import re
import tempfile
import unicodedata
from collections.abc import Callable, Iterator
from pathlib import Path

import numpy as np

from hardware.respeaker.audio import ReSpeakerAudioError

logger = logging.getLogger(__name__)
MODEL_NAME = "sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20"
DEFAULT_MODEL_DIR = Path(__file__).resolve().parents[2] / "models" / "wake-word" / MODEL_NAME
MODEL_FILES = {
    "encoder": "encoder-epoch-13-avg-2-chunk-16-left-64.int8.onnx",
    "decoder": "decoder-epoch-13-avg-2-chunk-16-left-64.onnx",
    "joiner": "joiner-epoch-13-avg-2-chunk-16-left-64.int8.onnx",
    "tokens": "tokens.txt",
}
FRAME_SAMPLES = 320
PRONUNCIATIONS_PATH = Path(__file__).with_name("keyword_pronunciations.json")


class StreamingKeywordSpotter:
    def __init__(self, model_dir: Path = DEFAULT_MODEL_DIR) -> None:
        self.model_dir = Path(model_dir)
        self._engine = None
        self._words: tuple[str, ...] = ()
        self._labels: dict[str, str] = {}

    def create_stream(self, words: tuple[str, ...]):
        if self._engine is None or words != self._words:
            import sherpa_onnx

            paths = {key: str(self.model_dir / name) for key, name in MODEL_FILES.items()}
            if any(not Path(path).is_file() for path in paths.values()):
                raise ReSpeakerAudioError("唤醒模型尚未安装，请按 docs/VOICE_WAKE.md 安装本地 KWS 模型。")
            encoded = self._encode_keywords(words)
            # sherpa 1.10 requires a real file, read only during construction.
            with tempfile.TemporaryDirectory(prefix="spica-kws-") as directory:
                keyword_file = Path(directory) / "keywords.txt"
                keyword_file.write_text(encoded, encoding="utf-8")
                engine = sherpa_onnx.KeywordSpotter(
                    **paths, keywords_file=str(keyword_file), provider="cpu",
                    num_threads=2, keywords_score=1.0, keywords_threshold=0.25,
                )
            self._engine = engine
            self._words = tuple(words)
            self._labels = {f"wake_{i}": word for i, word in enumerate(words)}
            logger.info("event=desktop_kws_loaded provider=cpu keywords=%s", len(words))
        return self._engine.create_stream()

    def accept_pcm(self, stream, pcm: bytes) -> str | None:
        samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
        stream.accept_waveform(16000, samples)
        while self._engine.is_ready(stream):
            self._engine.decode_stream(stream)
            label = self._engine.get_result(stream)
            if label:
                return self._labels.get(label)
        return None

    def _encode_keywords(self, words: tuple[str, ...]) -> str:
        import sherpa_onnx

        if not words:
            raise ValueError("请填写当前角色的唤醒词。")
        tokens_path = self.model_dir / "tokens.txt"
        vocabulary = {line.split()[0] for line in tokens_path.read_text(encoding="utf-8").splitlines() if line.strip()}
        pronunciations = json.loads(PRONUNCIATIONS_PATH.read_text(encoding="utf-8"))
        lexicon, g2p = None, None
        lines = []

        def append_pronunciation(phones: list[str], index: int, word: str) -> None:
            # A measured pronunciation may use sherpa's per-keyword threshold.
            # Keep this model-specific tuning out of role data and the global
            # detector threshold, and never let it change the emitted label.
            suffix = ""
            if phones and phones[-1].startswith("#"):
                threshold = float(phones[-1][1:])
                if not 0.0 <= threshold <= 1.0:
                    raise ValueError(f"唤醒读音的检测门槛必须在 0 到 1 之间：{word}")
                suffix = f" #{threshold:g}"
                phones = phones[:-1]
            if not phones or any(phone not in vocabulary for phone in phones):
                raise ValueError(f"唤醒模型不支持该读音：{word}")
            lines.append(f"{' '.join(phones)}{suffix} @wake_{index}")

        for index, word in enumerate(words):
            normalized = unicodedata.normalize("NFKC", word).strip()
            if not normalized or re.fullmatch(r"[A-Za-z' \-\u4e00-\u9fff]+", normalized) is None:
                raise ValueError(f"唤醒词请填写中文或英文读法：{word}")
            # Proper names need their spoken pronunciation; English G2P and
            # Mandarin lexical tones can both be wrong for a borrowed name.
            # All pronunciations still emit the same configured keyword label.
            known = pronunciations.get(normalized.casefold())
            if known:
                for pronunciation in known:
                    append_pronunciation(pronunciation.split(), index, word)
                continue
            phones = []
            for part in re.findall(r"[\u4e00-\u9fff]+|[A-Za-z']+", normalized):
                if '\u4e00' <= part[0] <= '\u9fff':
                    encoded = sherpa_onnx.text2token([part], str(tokens_path), tokens_type="ppinyin")
                    if not encoded:
                        raise ValueError(f"无法生成唤醒词读音：{word}")
                    phones.extend(encoded[0])
                else:
                    if lexicon is None:
                        lexicon = {}
                        for line in (self.model_dir / "en.phone").read_text(encoding="utf-8").splitlines():
                            entry = line.split()
                            if len(entry) > 1:
                                lexicon.setdefault(entry[0], entry[1:])
                    pronunciation = lexicon.get(part.upper())
                    if pronunciation is None:
                        if g2p is None:
                            # Avoid g2p_en's implicit online NLTK downloads.
                            import nltk
                            for resource in ("taggers/averaged_perceptron_tagger.zip", "corpora/cmudict.zip"):
                                nltk.data.find(resource)
                            from g2p_en import G2p
                            g2p = G2p()
                        pronunciation = g2p.predict(part.lower())
                    phones.extend(pronunciation)
            append_pronunciation(phones, index, word)
        return "\n".join(lines) + "\n"


def microphone_frames(mic_backend: str, should_stop: Callable[[], bool], *, input_device: str = "") -> Iterator[bytes]:
    """Own cancellable native capture separately from the resident KWS model."""
    from hardware.audio_input.keyword_capture import capture_frames
    yield from capture_frames(mic_backend, should_stop, **({"input_device": input_device} if input_device else {}))


def _native_microphone_frames(mic_backend: str, should_stop: Callable[[], bool], *, on_open=None, input_device: str = "") -> Iterator[bytes]:
    """Reuse the existing device selection and channel extraction without VAD gaps."""
    if mic_backend == "generic":
        from hardware.audio_input.generic_mic import _open_default_mic_stream
        stream = _open_default_mic_stream(FRAME_SAMPLES, **({"input_device": input_device} if input_device else {}))
        try:
            if on_open is not None:
                on_open()
            while not should_stop():
                yield stream.read(FRAME_SAMPLES, exception_on_overflow=False)
        finally:
            stream.close()
    elif mic_backend == "respeaker":
        from hardware.respeaker.audio import _close_stream, _extract_channel0, _load_pyaudio, _open_respeaker_stream
        pyaudio = _load_pyaudio()
        audio = pyaudio.PyAudio()
        stream = None
        try:
            stream = _open_respeaker_stream(pyaudio, audio, FRAME_SAMPLES, **({"input_device": input_device} if input_device else {}))
            if on_open is not None:
                on_open()
            while not should_stop():
                yield _extract_channel0(stream.read(FRAME_SAMPLES, exception_on_overflow=False))
        finally:
            _close_stream(stream)
            audio.terminate()
    else:
        raise ReSpeakerAudioError(f"无法打开麦克风：未知 mic_backend {mic_backend!r}")

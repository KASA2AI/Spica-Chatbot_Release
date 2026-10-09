"""Optional audio envelope preparation; the existing player owns the clock."""

from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Envelope:
    levels: tuple[float, ...]
    step_ms: float

    def at(self, position_ms):
        index = int(position_ms / self.step_ms)
        return self.levels[index] if 0 <= index < len(self.levels) else 0.0


def read_envelope(path):
    import numpy as np
    import soundfile as sf

    levels = []
    with sf.SoundFile(str(path)) as audio:
        step = max(1, round(audio.samplerate * .02))
        # Analyze bounded blocks; even a song does not retain its PCM in memory.
        for data in audio.blocks(blocksize=step * 1000, dtype="float32", always_2d=True):
            mono = np.mean(data, axis=1)
            mono = np.pad(mono, (0, (-len(mono)) % step))
            rms = np.sqrt(np.mean(mono.reshape(-1, step) ** 2, axis=1))
            levels.extend(np.clip((rms - .008) / .15, 0, 1).tolist())
        return Envelope(tuple(levels), step * 1000 / audio.samplerate)


class CubismAudio:
    def __init__(self, audio_controller):
        self.audio_controller = audio_controller
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="cubism-envelope")
        self._cache = OrderedDict()
        self._song_sources = OrderedDict()
        self._closed = False

    def prepare(self, path):
        if self._closed:
            return None
        path = Path(path)
        future = self._cache.get(path)
        if future is None:
            future = self._executor.submit(read_envelope, path)
            self._cache[path] = future
            while len(self._cache) > 8:
                _, old = self._cache.popitem(last=False)
                old.cancel()
        self._cache.move_to_end(path)
        return future

    def level(self):
        path, position = self.audio_controller.voice_playback_position()
        if path is None or self._closed:
            return 0.0

        future = self.prepare(path)
        if future is None or not future.done() or future.cancelled():
            return 0.0
        try:
            return future.result().at(position)
        except Exception as exc:
            # Cache the failed result so malformed audio never schedules work on
            # every frame; its original playback/error handling stays unchanged.
            if not getattr(future, "_cubism_reported", False):
                logger.warning("event=cubism_envelope_failed path=%s error=%s", path, exc)
                future._cubism_reported = True
            return 0.0

    def register_song(self, mixed_path, vocal_path):
        if mixed_path and vocal_path:
            self._song_sources[Path(mixed_path)] = Path(vocal_path)
            while len(self._song_sources) > 8:
                self._song_sources.popitem(last=False)
            self.prepare(vocal_path)

    def song_voice(self, mixed_path):
        return self._song_sources.get(Path(mixed_path))

    def close(self):
        self._closed = True
        self._executor.shutdown(wait=False, cancel_futures=True)
        self._cache.clear()

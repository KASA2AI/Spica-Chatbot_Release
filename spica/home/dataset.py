"""Explicit, local image collection; never writes inferred or empty labels."""
from datetime import datetime
from pathlib import Path


class ImageCapture:
    def __init__(self, directory, *, interval_seconds=1., frame_ttl_seconds=2.):
        self.directory = Path(directory)
        self.images = self.directory / 'images'
        self.images.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.interval_seconds, self.frame_ttl_seconds = interval_seconds, frame_ttl_seconds
        self.enabled = False
        self.count = 0
        self.error = ''
        self._last_captured = float('-inf')
        self._next_at = float('-inf')

    def start(self):
        self.enabled, self.error = True, ''
        self._next_at = float('-inf')

    def stop(self):
        self.enabled = False

    def observe(self, observation, jpeg, *, now):
        at = observation.captured_mono
        if (not self.enabled or not jpeg or observation.error
                or not 0 <= now-at <= self.frame_ttl_seconds
                or at <= self._last_captured or at < self._next_at):
            return None
        stamp = datetime.fromtimestamp(observation.captured_at).strftime('%Y%m%d_%H%M%S_%f')
        path = self.images / f'{stamp}_{observation.sequence:08d}.jpg'
        opened = False
        try:
            with path.open('xb') as stream:
                opened = True
                stream.write(jpeg)
        except OSError as exc:
            self.enabled, self.error = False, str(exc)
            if opened:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
            return None
        self.count += 1
        self._last_captured = at
        self._next_at = at+self.interval_seconds
        return path

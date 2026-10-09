"""Keep generated audio within a caller-owned lifetime, including cached TTS."""
from dataclasses import replace
from pathlib import Path
import shutil
import uuid


class ScopedAudio:
    def __init__(self, tts, directory):
        self._tts, self._directory = tts, Path(directory)
        self.name = tts.name

    @property
    def requires_full_text(self):
        return bool(getattr(self._tts, 'requires_full_text', False))

    def set_resident(self, owner, enabled):
        retain = getattr(self._tts, 'set_resident', None)
        if retain:
            retain(owner, enabled)

    def release_if_unused(self):
        release = getattr(self._tts, 'release_if_unused', None)
        if release:
            release()

    def synthesize(self, request):
        result = self._tts.synthesize(replace(request, artifact_directory=str(self._directory)))
        if result.ok and result.audio_path:
            source = Path(result.audio_path)
            if not source.resolve().is_relative_to(self._directory.resolve()):
                target = self._directory / (uuid.uuid4().hex + source.suffix)
                shutil.copyfile(source, target)
                result = replace(result, audio_path=str(target))
            result = replace(result, audio_url=None)
        return result

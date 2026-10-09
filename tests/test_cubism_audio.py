import math
import struct
import wave
from types import SimpleNamespace

from ui.controllers.cubism_audio import CubismAudio, read_envelope


def test_lipsync_uses_current_playback_position_and_closes_when_stopped(tmp_path):
    path = tmp_path / "声.wav"
    with wave.open(str(path), "wb") as out:
        out.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        values = [0] * 3200 + [int(12000 * math.sin(i * .2)) for i in range(6400)] + [0] * 3200
        out.writeframes(struct.pack(f"<{len(values)}h", *values))
    envelope = read_envelope(path)
    assert envelope.at(100) == 0
    assert envelope.at(300) > .5
    assert envelope.at(700) == 0
    position = [path, 300]
    audio = SimpleNamespace(voice_playback_position=lambda: position)
    driver = CubismAudio(audio)
    try:
        driver.prepare(path).result(timeout=3)
        assert driver.level() > .5
        position[1] = 700
        assert driver.level() == 0
        position[:] = [None, 0]
        assert driver.level() == 0
    finally:
        driver.close()

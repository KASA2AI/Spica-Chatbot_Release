"""Acceptance pins for the "Spica 语音音量" setting (UI preference, display-side only).

A 0-100% slider controls HER VOICE playback loudness (the chat/TTS audio output:
normal chat + galgame reaction + song-finished report). It is:
  * applied to AudioController._chat_audio_output (linear setVolume), live + every
    preloaded output, so a change takes effect mid-playback without restart;
  * persisted merge-safely to overlay_config.json (only key written -- every other,
    hand-edited key is preserved) and re-applied at startup;
  * scoped to the chat output ONLY -- song playback keeps its own 0.92 level;
  * defaulted to 0.86 (the historical hardcode) so an absent key changes nothing.

Pure ui/: TTS synthesis, run_turn and spica/ are untouched. Qt offscreen, mirroring
test_audio_controller_eom_defer / test_stop_button_interrupt.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")
pytest.importorskip("PySide6.QtMultimedia")

from unittest.mock import patch  # noqa: E402

from PySide6.QtMultimedia import QAudioOutput  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from ui.controllers.audio_controller import AudioController, _PreloadedAudio  # noqa: E402
from ui.overlay_config import (  # noqa: E402
    load_overlay_config,
    save_overlay_config_value,
)
from ui.qt_overlay import OverlayWindow  # noqa: E402
from ui.widgets.settings_panel import SettingsPanel  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


# ---------------- (a) AudioController ---------------- #

def test_default_chat_volume_is_086(qapp):
    # Zero-behaviour-change pin: the default must equal the old hardcoded 0.86.
    assert AudioController(None)._chat_volume == 0.86


def test_set_chat_volume_clamps_to_unit_interval(qapp):
    ac = AudioController(None)
    ac.set_chat_volume(2.0)
    assert ac._chat_volume == 1.0
    ac.set_chat_volume(-1.0)
    assert ac._chat_volume == 0.0
    ac.set_chat_volume(0.5)
    assert ac._chat_volume == 0.5


def test_set_chat_volume_updates_live_and_all_preloaded(qapp):
    # *** the key invariant ***: a mid-playback change reaches the live output AND
    # every preloaded output, so the new level takes effect without a restart.
    ac = AudioController(None)
    live = QAudioOutput(ac)
    live.setVolume(0.86)
    ac._chat_audio_output = live
    p0, p1 = QAudioOutput(ac), QAudioOutput(ac)
    p0.setVolume(0.86)
    p1.setVolume(0.86)
    ac._preloaded_chat[0] = _PreloadedAudio(None, p0, Path("a.wav"))
    ac._preloaded_chat[1] = _PreloadedAudio(None, p1, Path("b.wav"))

    ac.set_chat_volume(0.3)

    assert live.volume() == pytest.approx(0.3, abs=1e-3)
    assert p0.volume() == pytest.approx(0.3, abs=1e-3)
    assert p1.volume() == pytest.approx(0.3, abs=1e-3)


def test_set_chat_volume_leaves_song_output_untouched(qapp):
    # Scope pin: the slider governs her VOICE only; song has a separate output.
    ac = AudioController(None)
    song = QAudioOutput(ac)
    song.setVolume(0.92)
    ac._song_audio_output = song

    ac.set_chat_volume(0.2)

    assert song.volume() == pytest.approx(0.92, abs=1e-3)


# ---------------- (b) overlay_config load / merge-safe save ---------------- #

def test_load_reads_and_clamps_voice_volume(tmp_path):
    p = tmp_path / "cfg.json"
    p.write_text(json.dumps({"spica_voice_volume": 0.5}), encoding="utf-8")
    assert load_overlay_config(p).spica_voice_volume == 0.5
    p.write_text(json.dumps({"spica_voice_volume": 1.8}), encoding="utf-8")
    assert load_overlay_config(p).spica_voice_volume == 1.0  # clamped to 1.0
    p.write_text(json.dumps({"spica_voice_volume": -0.5}), encoding="utf-8")
    assert load_overlay_config(p).spica_voice_volume == 0.0  # clamped to 0.0


def test_missing_voice_volume_defaults_to_086(tmp_path):
    p = tmp_path / "cfg.json"
    p.write_text(json.dumps({"default_ui_scale": 1.2}), encoding="utf-8")
    assert load_overlay_config(p).spica_voice_volume == 0.86


def test_save_is_merge_safe_and_preserves_hand_edited_keys(tmp_path):
    p = tmp_path / "cfg.json"
    original = {
        "default_character_scale": 1.0,
        "default_ui_scale": 1.2,
        "default_typewriter_speed": 1.0,
        "character_label_height_scale": 1.0,
        "overlay_initial_height_scale": 1.0,
        "character_max_height_ratio": 1.0,
    }
    p.write_text(json.dumps(original), encoding="utf-8")

    assert save_overlay_config_value("spica_voice_volume", 0.5, p) is True

    raw = json.loads(p.read_text(encoding="utf-8"))
    assert raw["spica_voice_volume"] == 0.5
    for key, value in original.items():
        assert raw[key] == value  # every hand-edited key untouched


def test_save_creates_file_when_absent(tmp_path):
    p = tmp_path / "nope.json"
    assert save_overlay_config_value("spica_voice_volume", 0.7, p) is True
    assert json.loads(p.read_text(encoding="utf-8"))["spica_voice_volume"] == 0.7


def test_save_skips_corrupt_file_without_clobber(tmp_path):
    # An unparseable hand-edited file must be left intact, not overwritten.
    p = tmp_path / "corrupt.json"
    p.write_text("{not valid json", encoding="utf-8")
    assert save_overlay_config_value("spica_voice_volume", 0.7, p) is False
    assert p.read_text(encoding="utf-8") == "{not valid json"


# ---------------- (c) SettingsPanel widget ---------------- #

def test_set_voice_volume_syncs_controls_without_emitting(qapp):
    panel = SettingsPanel()
    emitted = []
    panel.voice_volume_changed.connect(emitted.append)
    panel.set_voice_volume(0.86)  # initialisation -> no signal
    assert panel.voice_volume_slider.value() == 86
    assert panel.voice_volume_spin.value() == 86
    assert emitted == []


def test_voice_volume_slider_emits_linear_and_syncs_spin(qapp):
    panel = SettingsPanel()
    emitted = []
    panel.voice_volume_changed.connect(emitted.append)
    panel.voice_volume_slider.setValue(50)
    assert emitted[-1] == pytest.approx(0.5)  # 50% -> linear 0.5
    assert panel.voice_volume_spin.value() == 50


def test_voice_volume_spin_emits_linear_and_syncs_slider(qapp):
    panel = SettingsPanel()
    emitted = []
    panel.voice_volume_changed.connect(emitted.append)
    panel.voice_volume_spin.setValue(30)
    assert emitted[-1] == pytest.approx(0.3)
    assert panel.voice_volume_slider.value() == 30


# ---------------- (d) qt_overlay wiring ---------------- #

def _window():
    with patch.object(OverlayWindow, "_init_backend", lambda self: None):
        return OverlayWindow()


def test_startup_applies_persisted_volume_to_audio_controller(qapp):
    window = _window()
    # the controller volume is seeded from overlay_config at startup
    assert window.audio_controller._chat_volume == pytest.approx(window.spica_voice_volume, abs=1e-3)
    window.close()


def test_set_spica_voice_volume_applies_live_and_persists(qapp, monkeypatch):
    window = _window()
    saved = {}
    monkeypatch.setattr(
        "ui.qt_overlay.save_overlay_config_value",
        lambda key, value, *a, **k: (saved.update({key: value}), True)[1],
    )

    window.set_spica_voice_volume(0.42)

    assert window.spica_voice_volume == pytest.approx(0.42)
    assert window.audio_controller._chat_volume == pytest.approx(0.42, abs=1e-3)
    assert saved["spica_voice_volume"] == pytest.approx(0.42)  # persisted
    window.close()


def test_set_spica_voice_volume_clamps(qapp, monkeypatch):
    window = _window()
    monkeypatch.setattr("ui.qt_overlay.save_overlay_config_value", lambda *a, **k: True)
    window.set_spica_voice_volume(5.0)
    assert window.spica_voice_volume == 1.0
    window.close()


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-q"]))

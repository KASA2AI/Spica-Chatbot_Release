"""Regression: the EndOfMedia/InvalidMedia handlers must DEFER all QMediaPlayer
teardown out of the mediaStatusChanged signal-dispatch stack.

Root cause of the 2026-06-27 freeze (2nd leg): _handle_chat_media_status /
_handle_song_media_status ran release_chat_audio() / stop_song() SYNCHRONOUSLY
inside the mediaStatusChanged(EndOfMedia) callback. That release calls
_release_player -> media_player.mediaStatusChanged.disconnect(handler), i.e. it
disconnects the very signal currently being emitted -> Qt's cross-thread signal
dispatch deadlocks. Two py-spy dumps minutes apart froze byte-for-byte at
audio_controller.py:298 (the disconnect).

The fix: the slot does ONLY plain Python (capture the player + null self refs),
then QTimer.singleShot(0, ...) runs the whole teardown (disconnect/stop/
deleteLater) + the callback on the next loop tick, on a clean stack.

These tests lock in BOTH halves, for chat AND song:
  * NOTHING touches the player synchronously inside the slot, and
  * teardown + callback run after one event-loop tick, release BEFORE the callback.
"""

from __future__ import annotations

import os
from typing import Any

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")
pytest.importorskip("PySide6.QtMultimedia")

from PySide6.QtMultimedia import QMediaPlayer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from ui.controllers.audio_controller import AudioController  # noqa: E402
from ui.models.playback import AudioOwner, AudioToken  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


class _FakeSignal:
    def __init__(self, owner: "_FakePlayer") -> None:
        self._owner = owner

    def disconnect(self, handler: Any) -> None:
        self._owner.disconnected = True
        self._owner.seq.append("release")  # disconnect is the first teardown op


class _FakePlayer:
    """Records teardown ops; standing in for a QMediaPlayer so the test never needs
    a real media backend (and so we can see EXACTLY when disconnect/stop happen)."""

    def __init__(self, seq: list[str]) -> None:
        self.disconnected = False
        self.stopped = False
        self.deleted = False
        self.seq = seq
        self._sig = _FakeSignal(self)

    @property
    def mediaStatusChanged(self) -> _FakeSignal:
        return self._sig

    def stop(self) -> None:
        self.stopped = True

    def deleteLater(self) -> None:
        self.deleted = True


def test_daily_and_home_bindings_remain_separate_and_only_explicit_fallback_is_used(qapp, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import Mock
    from ui.controllers import audio_controller as module
    daily, home, backup = [SimpleNamespace(id=lambda value=value: value) for value in (b'desk', b'bed', b'backup')]
    available = [daily, home, backup]
    provider = SimpleNamespace(audioOutputs=lambda: available, defaultAudioOutput=lambda: backup)
    monkeypatch.setattr(module, 'QMediaDevices', provider)
    controller = AudioController(None, output_device_id=b'desk'.hex())
    controller.set_home_output_device(b'bed'.hex())
    output = Mock()
    controller._configure_output(output, 'home')
    output.setDevice.assert_called_with(home)
    controller._configure_output(output, 'daily')
    output.setDevice.assert_called_with(daily)
    available.remove(home)
    with pytest.raises(RuntimeError, match='未切换'):
        controller._configure_output(output, 'home')
    controller.set_home_output_device(b'bed'.hex(), fallback_device_id=b'backup'.hex())
    controller._configure_output(output, 'home')
    output.setDevice.assert_called_with(backup)
    available.append(home)
    controller._configure_output(output, 'home')
    output.setDevice.assert_called_with(home)
    controller.set_home_output_device(None)
    controller._configure_output(output, 'home')
    output.setDevice.assert_called_with(daily)
    controller.set_home_output_device('')
    controller._configure_output(output, 'home')
    output.setDevice.assert_called_with(backup)
    controller._configure_output(output, 'daily')
    output.setDevice.assert_called_with(daily)


def test_unplugged_fixed_output_reports_failure_instead_of_switching_mid_sentence(qapp, monkeypatch):
    from types import SimpleNamespace
    from ui.controllers import audio_controller as module
    backup = SimpleNamespace(id=lambda: b'backup')
    monkeypatch.setattr(module, 'QMediaDevices', SimpleNamespace(audioOutputs=lambda: [backup]))
    controller = AudioController(None, output_device_id=b'desk'.hex())
    controller.set_home_output_device(b'bed'.hex(), fallback_device_id=b'backup'.hex())
    seq, receipts = [], []
    player = _FakePlayer(seq)
    controller._chat_media_player = player
    controller._chat_token = AudioToken(1, AudioOwner.CHAT)
    controller._chat_bound_device = b'bed'.hex()
    controller._chat_on_finished = lambda: seq.append('finished')
    controller._chat_on_playback = lambda outcome, at: receipts.append(outcome)
    controller._outputs_changed()
    assert not player.stopped
    qapp.processEvents()
    assert player.stopped and controller._chat_token is None
    assert receipts == ['failed'] and seq[-1] == 'finished'


def test_chat_endofmedia_teardown_is_deferred_out_of_signal_slot(qapp) -> None:
    controller = AudioController(None)
    seq: list[str] = []
    player = _FakePlayer(seq)
    controller._chat_media_player = player
    controller._chat_audio_output = None
    controller._chat_token = AudioToken(id=1, owner=AudioOwner.CHAT)
    controller._chat_on_finished = lambda: seq.append("on_finished")
    controller._sender_matches_token = lambda *a, **k: True  # isolate the sender check

    controller._handle_chat_media_status(QMediaPlayer.MediaStatus.EndOfMedia)

    # *** regression core ***: the slot touched the player with ZERO Qt ops.
    assert player.disconnected is False, "disconnect ran inside dispatch -> deadlock risk"
    assert player.stopped is False
    assert seq == []  # on_finished not called yet either
    assert controller._chat_media_player is None  # but self refs cleared synchronously

    qapp.processEvents()  # run the deferred teardown

    assert player.disconnected is True  # now disconnect/stop ran on a clean stack
    assert player.stopped is True
    assert seq == ["release", "on_finished"]  # deferred + release BEFORE on_finished


def test_song_endofmedia_teardown_is_deferred_and_calls_on_finished(qapp) -> None:
    controller = AudioController(None)
    seq: list[str] = []
    player = _FakePlayer(seq)
    controller._song_media_player = player
    controller._song_audio_output = None
    controller._song_token = AudioToken(id=2, owner=AudioOwner.SONG)
    controller._song_on_finished = lambda: seq.append("on_finished")
    controller._song_on_error = lambda _msg: seq.append("on_error")
    controller._sender_matches_token = lambda *a, **k: True

    controller._handle_song_media_status(QMediaPlayer.MediaStatus.EndOfMedia)

    assert player.disconnected is False
    assert seq == []
    assert controller._song_media_player is None

    qapp.processEvents()

    assert player.disconnected is True
    assert seq == ["release", "on_finished"]  # EndOfMedia -> on_finished, after release


def test_song_invalidmedia_teardown_is_deferred_and_calls_on_error(qapp) -> None:
    controller = AudioController(None)
    seq: list[str] = []
    player = _FakePlayer(seq)
    controller._song_media_player = player
    controller._song_audio_output = None
    controller._song_token = AudioToken(id=3, owner=AudioOwner.SONG)
    controller._song_on_finished = lambda: seq.append("on_finished")
    controller._song_on_error = lambda _msg: seq.append("on_error")
    controller._sender_matches_token = lambda *a, **k: True

    controller._handle_song_media_status(QMediaPlayer.MediaStatus.InvalidMedia)

    assert player.disconnected is False
    assert seq == []

    qapp.processEvents()

    assert player.disconnected is True
    assert seq == ["release", "on_error"]  # InvalidMedia -> on_error (NOT on_finished)


@pytest.mark.parametrize('started, invalid, expected', [
    (False, False, ['not_started']),
    (True, False, ['started', 'completed']),
    (True, True, ['started', 'failed']),
])
def test_chat_receipts_require_player_confirmation_and_preserve_failure(qapp, started, invalid, expected):
    controller = AudioController(None)
    controller._chat_media_player = _FakePlayer([])
    controller._chat_token = AudioToken(id=10, owner=AudioOwner.CHAT)
    receipts = []
    controller._chat_on_playback = lambda outcome, at: receipts.append((outcome, at))
    controller._sender_matches_token = lambda *a, **k: True
    assert receipts == []  # Owning/preparing a player is not an audio start.
    if started:
        controller._handle_chat_playback_state(QMediaPlayer.PlaybackState.PlayingState)
        controller._handle_chat_playback_state(QMediaPlayer.PlaybackState.PlayingState)
        assert receipts == []  # No business callbacks on Qt's dispatch stack.
    status = QMediaPlayer.MediaStatus.InvalidMedia if invalid else QMediaPlayer.MediaStatus.EndOfMedia
    controller._handle_chat_media_status(status)
    qapp.processEvents()
    assert [outcome for outcome, _ in receipts] == expected
    assert all(at > 0 for _, at in receipts)
    controller.release_chat_audio()
    qapp.processEvents()
    assert len(receipts) == len(expected)


def test_chat_late_player_state_cannot_start_a_replacement(qapp):
    controller = AudioController(None)
    receipts = []
    controller._chat_on_playback = lambda *receipt: receipts.append(receipt)
    controller._sender_matches_token = lambda *a, **k: False
    controller._handle_chat_playback_state(QMediaPlayer.PlaybackState.PlayingState)
    qapp.processEvents()
    assert receipts == []
    assert not controller._chat_started


def test_persisted_speaker_survives_default_change_and_missing_device_fails(qapp, monkeypatch, tmp_path):
    from types import SimpleNamespace
    from PySide6.QtCore import QObject, Signal
    from ui.controllers import audio_controller as module
    from spica.config.manager import ConfigManager

    config = tmp_path/'app.yaml'
    config.write_text('tts:\n  enabled: true\n')
    manager = ConfigManager(config)
    manager.update({'tts': {'output_device_id': b'speakers'.hex()}})
    speaker = SimpleNamespace(id=lambda: b'speakers')
    wrong = SimpleNamespace(id=lambda: b'respeaker')
    available = [wrong, speaker]

    class Output:
        def __init__(self, parent): self.device = wrong
        def setDevice(self, device): self.device = device
        def setVolume(self, volume): pass
        def deleteLater(self): pass

    class Player(QObject):
        mediaStatusChanged = Signal(object)
        playbackStateChanged = Signal(object)
        errorOccurred = Signal(object, str)
        def setAudioOutput(self, output): pass
        def setSource(self, source): pass
        def play(self): pass
        def stop(self): pass

    monkeypatch.setattr(module, 'QAudioOutput', Output)
    monkeypatch.setattr(module, 'QMediaPlayer', Player)
    monkeypatch.setattr(module, 'QMediaDevices', SimpleNamespace(audioOutputs=lambda: available))
    controller = AudioController(None, output_device_id=manager.load().tts.output_device_id)
    path = tmp_path/'speech.wav'
    path.touch()
    assert controller.preload_chat_audio(0, path)
    assert controller._preloaded_chat[0].audio_output.device is speaker
    assert controller.play_chat_audio(path, AudioToken(1, AudioOwner.CHAT), lambda: None)
    assert controller._chat_audio_output.device is speaker
    controller.release_chat_audio()
    assert controller.preload_chat_audio(0, path)
    available[:] = [wrong]
    receipts, finished = [], []
    assert not controller.play_chat_audio(path, AudioToken(2, AudioOwner.CHAT), lambda: finished.append(True),
        on_playback=lambda outcome, at: receipts.append(outcome))
    assert receipts == ['failed'] and finished == [True]
    assert controller._chat_audio_output is None
    assert manager.load().tts.enabled is True


@pytest.mark.parametrize('preloaded', [False, True])
@pytest.mark.parametrize('failure', [None, 'exception', 'signal'])
def test_audio_start_and_volume_override_preserve_normal_playback(qapp, monkeypatch, tmp_path, preloaded, failure):
    from PySide6.QtCore import QObject, Signal
    from ui.controllers import audio_controller as module

    class Output:
        def __init__(self, parent):
            self.volume = None
        def setVolume(self, volume):
            self.volume = volume
        def setDevice(self, device):
            self.device = device
        def deleteLater(self):
            pass

    class Player(QObject):
        mediaStatusChanged = Signal(object)
        playbackStateChanged = Signal(object)
        errorOccurred = Signal(object, str)
        MediaStatus = QMediaPlayer.MediaStatus
        PlaybackState = QMediaPlayer.PlaybackState
        Error = QMediaPlayer.Error
        def setAudioOutput(self, output):
            pass
        def setSource(self, source):
            pass
        def play(self):
            if failure == 'exception':
                raise RuntimeError('synthetic player start failure')
            if failure == 'signal':
                self.errorOccurred.emit(self.Error.ResourceError, 'synthetic output failure')
        def stop(self):
            pass

    monkeypatch.setattr(module, 'QAudioOutput', Output)
    monkeypatch.setattr(module, 'QMediaPlayer', Player)
    controller = AudioController(None)
    path = tmp_path / 'speech.wav'
    path.touch()
    if preloaded:
        assert controller.preload_chat_audio(0, path)
    receipts, finished = [], []
    accepted = controller.play_chat_audio(path, AudioToken(1, AudioOwner.CHAT), lambda: finished.append(True),
        volume=.45, on_playback=lambda outcome, at: receipts.append(outcome))
    if failure:
        assert not accepted
        qapp.processEvents()
        assert receipts == ['failed']
        assert finished == [True]
        assert controller._chat_media_player is None
        assert controller._chat_audio_output is None
        assert controller._chat_volume == .86
        return
    assert accepted
    assert controller._chat_audio_output.volume == .45
    assert receipts == [], 'calling play() must not report that audio started'
    controller.set_chat_volume(.2)
    assert controller._chat_audio_output.volume == .45
    controller._chat_media_player.playbackStateChanged.emit(Player.PlaybackState.PlayingState)
    qapp.processEvents()
    assert receipts == ['started']
    controller.release_chat_audio()
    assert controller.play_chat_audio(path, AudioToken(2, AudioOwner.CHAT), lambda: None)
    assert controller._chat_audio_output.volume == .2
    controller.release_chat_audio()

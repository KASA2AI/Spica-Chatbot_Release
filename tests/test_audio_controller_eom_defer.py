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

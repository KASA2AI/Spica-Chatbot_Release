from __future__ import annotations

import sys
import types

from agent_tools.function_tools.song import netease


class _FakeSession:
    def __init__(self, logged_in: bool) -> None:
        self.logged_in = logged_in


def test_get_audio_url_loads_saved_pyncm_session(monkeypatch, tmp_path):
    session_path = tmp_path / ".pyncm"
    session_path.write_text("saved-session", encoding="utf-8")
    current = {"session": _FakeSession(logged_in=False)}

    pyncm = types.ModuleType("pyncm")
    pyncm.GetCurrentSession = lambda: current["session"]
    pyncm.LoadSessionFromString = lambda value: _FakeSession(logged_in=value == "saved-session")
    pyncm.SetCurrentSession = lambda session: current.update(session=session)

    class _Track:
        @staticmethod
        def GetTrackAudio(song_id, bitrate=320000):
            assert current["session"].logged_in
            assert song_id == 1910623420
            assert bitrate == 320000
            return {"data": [{"url": "https://music.example/song.mp3"}]}

    pyncm.apis = types.SimpleNamespace(track=_Track)
    monkeypatch.setitem(sys.modules, "pyncm", pyncm)
    monkeypatch.setattr(netease, "_default_pyncm_session_path", lambda: session_path)

    assert netease.get_audio_url("1910623420") == "https://music.example/song.mp3"
    assert current["session"].logged_in


def test_get_audio_url_keeps_existing_behavior_without_saved_session(monkeypatch, tmp_path):
    current = {"session": _FakeSession(logged_in=False)}

    pyncm = types.ModuleType("pyncm")
    pyncm.GetCurrentSession = lambda: current["session"]
    pyncm.LoadSessionFromString = lambda value: _FakeSession(logged_in=True)
    pyncm.SetCurrentSession = lambda session: current.update(session=session)

    class _Track:
        @staticmethod
        def GetTrackAudio(song_id, bitrate=320000):
            assert not current["session"].logged_in
            return {"data": [{"url": "https://music.example/free.mp3"}]}

    pyncm.apis = types.SimpleNamespace(track=_Track)
    monkeypatch.setitem(sys.modules, "pyncm", pyncm)
    monkeypatch.setattr(netease, "_default_pyncm_session_path", lambda: tmp_path / "missing")

    assert netease.get_audio_url("1910623420") == "https://music.example/free.mp3"
    assert not current["session"].logged_in

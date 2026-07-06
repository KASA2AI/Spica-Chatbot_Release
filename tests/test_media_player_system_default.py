"""Phase 2: system-default media player adapter -- monkeypatched opener.

F3 contract: the opener is launched via Popen (fire-and-forget + short probe
window), never a blocking ``subprocess.run`` that waits for the player to exit.
"""

from __future__ import annotations

import time

import pytest

from spica.adapters.media_player.system_default import SystemDefaultPlayer
from spica.ports.media_player import MediaPlayerError


class FakeProc:
    def __init__(self, returncode=None):
        self._rc = returncode

    def poll(self):
        return self._rc


class Popen:
    """Popen-shaped fake: records argv/kwargs, returns a poll()-able proc.
    ``returncode=None`` simulates a player that stays alive."""

    def __init__(self, returncode=0, exc=None):
        self.calls: list[tuple[list, dict]] = []
        self._returncode = returncode
        self._exc = exc

    def __call__(self, args, **kw):
        self.calls.append((args, kw))
        if self._exc is not None:
            raise self._exc
        return FakeProc(self._returncode)


def _dir(tmp_path):
    d = tmp_path / "SpicaAnime"
    d.mkdir()
    return d


def _mkv(d, name="ep01.mkv"):
    f = d / name
    f.write_bytes(b"\x00")
    return f


def test_plays_valid_mkv_via_xdg_open(tmp_path):
    d = _dir(tmp_path)
    f = _mkv(d)
    run = Popen()
    SystemDefaultPlayer(str(d), platform="linux", popen=run).play_file(str(f))
    assert len(run.calls) == 1
    args, kw = run.calls[0]
    assert args == ["xdg-open", str(f.resolve())]
    assert kw.get("shell") in (None, False)          # never shell=True


def test_player_command_used_without_shell(tmp_path):
    d = _dir(tmp_path)
    f = _mkv(d)
    run = Popen()
    SystemDefaultPlayer(str(d), player_command="vlc --fullscreen",
                        platform="linux", popen=run).play_file(str(f))
    args, kw = run.calls[0]
    assert args == ["vlc", "--fullscreen", str(f.resolve())]
    assert kw.get("shell") in (None, False)


def test_windows_uses_startfile(tmp_path):
    d = _dir(tmp_path)
    f = _mkv(d)
    opened = []
    SystemDefaultPlayer(str(d), platform="win32", popen=Popen(),
                        startfile=opened.append).play_file(str(f))
    assert opened == [str(f.resolve())]


def test_rejects_outside_download_dir(tmp_path):
    d = _dir(tmp_path)
    run = Popen()
    with pytest.raises(MediaPlayerError) as ei:
        SystemDefaultPlayer(str(d), platform="linux", popen=run).play_file("/etc/passwd")
    assert ei.value.code == "UNSAFE_PATH"
    assert run.calls == []


def test_rejects_prefix_bypass(tmp_path):
    d = _dir(tmp_path)                                # .../SpicaAnime
    evil = tmp_path / "SpicaAnimeEvil"
    evil.mkdir()
    f = _mkv(evil, "x.mkv")                           # .../SpicaAnimeEvil/x.mkv
    run = Popen()
    with pytest.raises(MediaPlayerError) as ei:
        SystemDefaultPlayer(str(d), platform="linux", popen=run).play_file(str(f))
    assert ei.value.code == "UNSAFE_PATH"
    assert run.calls == []


def test_rejects_symlink_escape(tmp_path):
    d = _dir(tmp_path)
    outside = tmp_path / "outside.mkv"
    outside.write_bytes(b"\x00")
    link = d / "ep.mkv"
    link.symlink_to(outside)                          # inside dir, resolves outside
    run = Popen()
    with pytest.raises(MediaPlayerError) as ei:
        SystemDefaultPlayer(str(d), platform="linux", popen=run).play_file(str(link))
    assert ei.value.code == "UNSAFE_PATH"
    assert run.calls == []


@pytest.mark.parametrize("name", ["notes.txt", "ep01.mkv.part", "evil.desktop",
                                   "run.sh", "page.html"])
def test_rejects_non_media_extensions(tmp_path, name):
    d = _dir(tmp_path)
    f = d / name
    f.write_bytes(b"\x00")
    run = Popen()
    with pytest.raises(MediaPlayerError) as ei:
        SystemDefaultPlayer(str(d), platform="linux", popen=run).play_file(str(f))
    assert ei.value.code == "UNSAFE_PATH"
    assert run.calls == []


def test_rejects_missing_file(tmp_path):
    d = _dir(tmp_path)
    run = Popen()
    with pytest.raises(MediaPlayerError):
        SystemDefaultPlayer(str(d), platform="linux", popen=run).play_file(
            str(d / "nope.mkv"))
    assert run.calls == []


# -- F3: fire-and-forget -- a long-lived player must never block the turn -----

def test_play_file_returns_while_real_player_still_running(tmp_path):
    # behavioral repro with the REAL default popen: a player that sleeps 5s.
    # The old subprocess.run semantics blocked play_file for the full 5s.
    d = _dir(tmp_path)
    f = _mkv(d)
    script = tmp_path / "slowplayer.sh"
    script.write_text("#!/bin/sh\nsleep 5\n")
    script.chmod(0o755)
    t0 = time.monotonic()
    SystemDefaultPlayer(str(d), player_command=str(script),
                        platform="linux").play_file(str(f))
    assert time.monotonic() - t0 < 2.0        # probe window only, not 5s


def test_play_file_returns_immediately_with_injected_probe(tmp_path):
    # unit form: never-exiting fake proc + recorded sleep -> bounded probing
    d = _dir(tmp_path)
    f = _mkv(d)
    naps: list[float] = []
    run = Popen(returncode=None)              # stays alive forever
    SystemDefaultPlayer(str(d), platform="linux", popen=run,
                        sleep=naps.append).play_file(str(f))
    assert len(run.calls) == 1                # launched exactly once
    assert 0 < sum(naps) <= 0.3 + 0.05        # probed within the window, then let go


def test_immediate_exit_nonzero_raises_open_failed(tmp_path):
    # a launch that dies inside the probe window with rc!=0 is a failure (F3)
    d = _dir(tmp_path)
    f = _mkv(d)
    run = Popen(returncode=1)
    with pytest.raises(MediaPlayerError) as ei:
        SystemDefaultPlayer(str(d), platform="linux", popen=run).play_file(str(f))
    assert ei.value.code == "OPEN_FAILED"


# -- review tail #2: open failures must surface, not be swallowed ------------

def test_xdg_open_nonzero_returncode_raises(tmp_path):
    d = _dir(tmp_path)
    f = _mkv(d)
    run = Popen(returncode=3)                 # xdg-open failed to launch
    with pytest.raises(MediaPlayerError) as ei:
        SystemDefaultPlayer(str(d), platform="linux", popen=run).play_file(str(f))
    assert ei.value.code == "OPEN_FAILED"


def test_player_command_oserror_raises(tmp_path):
    d = _dir(tmp_path)
    f = _mkv(d)
    run = Popen(exc=FileNotFoundError("no such executable"))
    with pytest.raises(MediaPlayerError) as ei:
        SystemDefaultPlayer(str(d), player_command="nonexistent-player",
                            platform="linux", popen=run).play_file(str(f))
    assert ei.value.code == "OPEN_FAILED"


def test_windows_startfile_oserror_raises(tmp_path):
    d = _dir(tmp_path)
    f = _mkv(d)

    def boom(_path):
        raise OSError("no file association")

    with pytest.raises(MediaPlayerError) as ei:
        SystemDefaultPlayer(str(d), platform="win32", popen=Popen(),
                            startfile=boom).play_file(str(f))
    assert ei.value.code == "OPEN_FAILED"

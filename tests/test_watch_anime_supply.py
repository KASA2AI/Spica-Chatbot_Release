"""Phase 3: watch_anime state-supply + install() does no I/O (review tests)."""

from __future__ import annotations

from types import SimpleNamespace

from spica.anime.library import AnimeLibrary
from spica.config.schema import AnimeConfig
from spica.host.assemblies import anime as anime_assembly
from spica.plugins.registry import CapabilityRegistry


class FakeSource:
    name = "fake"

    def __init__(self):
        self.searched = 0

    def search(self, q, *, deadline=None):
        self.searched += 1
        return []

    def materialize(self, c):
        return None


class FakePlayer:
    def __init__(self):
        self.played = 0

    def play_file(self, p):
        self.played += 1


class FakeTorrent:
    def __init__(self):
        self.added = 0

    def add_magnet(self, m):
        self.added += 1
        return "x"


def _host(*, enabled, sink):
    h = SimpleNamespace()
    h.config = SimpleNamespace(anime=AnimeConfig(enabled=enabled))
    h.secrets = SimpleNamespace(bilibili_cookie=None, qbittorrent_password=None)
    h.registry = CapabilityRegistry()
    h._anime_sink = sink
    return h


def _install(h):
    anime_assembly.install(h, sources=[FakeSource()], torrent=FakeTorrent(),
                           player=FakePlayer(), library=AnimeLibrary())


def _tool_names(registry):
    return {(s.get("name") or s.get("function", {}).get("name"))
            for s in registry.tool_schemas()}


# -- 4-state availability through registry.tool_schemas() --------------------

def test_supply_disabled_no_sink():
    h = _host(enabled=False, sink=None)
    _install(h)
    assert "watch_anime" not in _tool_names(h.registry)


def test_supply_enabled_but_no_sink():
    h = _host(enabled=True, sink=None)
    _install(h)
    assert "watch_anime" not in _tool_names(h.registry)


def test_supply_disabled_with_sink():
    h = _host(enabled=False, sink=lambda ev: None)
    _install(h)
    assert "watch_anime" not in _tool_names(h.registry)


def test_supply_enabled_with_sink():
    h = _host(enabled=True, sink=lambda ev: None)
    _install(h)
    assert "watch_anime" in _tool_names(h.registry)


def test_supply_config_none_is_safe():
    # a host whose config is None must NOT crash the registry -> tool hidden
    h = _host(enabled=True, sink=lambda ev: None)
    _install(h)
    h.config = None
    assert "watch_anime" not in _tool_names(h.registry)   # predicate returns False


def test_broken_predicate_hidden_not_crashing():
    # registry swallows a raising available predicate (state-supply contract)
    reg = CapabilityRegistry()
    reg.register_tool({"name": "boom", "parameters": {}}, lambda: None,
                      available=lambda: 1 // 0, intent_gated=False, effect="act")
    assert "boom" not in {(s.get("name")) for s in reg.tool_schemas()}


# -- install() triggers no HTTP / qbt / player I/O ---------------------------

def test_install_does_no_io():
    h = _host(enabled=True, sink=lambda ev: None)
    src, torrent, player = FakeSource(), FakeTorrent(), FakePlayer()
    anime_assembly.install(h, sources=[src], torrent=torrent, player=player,
                           library=AnimeLibrary())
    assert src.searched == 0        # no search
    assert torrent.added == 0       # no add_magnet
    assert player.played == 0       # no play_file
    # torrent + player held for the Phase 4 worker
    assert h.anime_torrent is torrent
    assert h.anime_player is player

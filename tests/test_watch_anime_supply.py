"""Phase 3: watch_anime state-supply + install() does no I/O (review tests)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent_tools.function_tools.router import tool_schemas_for_user_text
from spica.anime.library import AnimeLibrary
from spica.adapters.tools.sing_song import SING_SONG_SCHEMA
from spica.adapters.tools.watch_anime import WATCH_ANIME_SCHEMA
from spica.config.schema import AnimeConfig
from spica.host.assemblies import anime as anime_assembly
from spica.plugins.registry import CapabilityRegistry
from spica.runtime.tools import RegistryToolSet


class FakeSource:
    name = "fake"

    def __init__(self):
        self.searched = 0

    def search(self, request, *, deadline=None):
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


def _offered_names(registry, user_text):
    return {
        (schema.get("name") or schema.get("function", {}).get("name"))
        for schema in RegistryToolSet(registry).schemas_for_user_text(user_text)
    }


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


@pytest.mark.parametrize(
    "user_text",
    (
        "晚安，Spica。今天辛苦了，陪我说两句吧。",
        "抱抱我",
        "你今天看起来很开心",
        "陪我看看星星",
        "我想看动漫",
        "无职转生好看吗",
        "我刚看完无职转生第三季第一集",
        "不要看无职转生第一集",
        "聊聊无职转生第一集的剧情",
        "把“我想看无职转生第一集”翻译成日语",
        "为什么无职转生第一集评价那么高？",
        "我最喜欢无职转生第一集",
        "第一集看起来很不错",
        "无职转生第一集值得看吗",
        "无职转生第一集什么时候播出",
        "无职转生第一集的播放时间",
    ),
)
def test_watch_anime_is_not_supplied_for_chat_or_non_action_mentions(user_text):
    h = _host(enabled=True, sink=lambda ev: None)
    _install(h)

    assert h.registry.tool_intent_gated("watch_anime") is True
    assert "watch_anime" not in _offered_names(h.registry, user_text)


@pytest.mark.parametrize(
    "user_text",
    (
        "我想看无职转生第三季第一集",
        "播放《幼女战记》第二季第三集",
        "可以播放无职转生第一集吗",
        "看无职转生最新一集",
        "第一集",
        "无职转生第三季第一集",
        "放吧",
        "把刚下好的那集放了吧",
        "play Mushoku Tensei episode 1",
        "無職転生の第1話を見たい",
    ),
)
def test_watch_anime_is_supplied_for_actionable_episode_requests(user_text):
    h = _host(enabled=True, sink=lambda ev: None)
    _install(h)

    assert "watch_anime" in _offered_names(h.registry, user_text)


def test_actionable_anime_request_does_not_also_offer_song_tool():
    offered = tool_schemas_for_user_text(
        "播放《幼女战记》第二季第三集",
        [SING_SONG_SCHEMA, WATCH_ANIME_SCHEMA],
    )

    assert [schema["name"] for schema in offered] == ["watch_anime"]


def test_plain_song_request_keeps_song_tool_without_anime_tool():
    offered = tool_schemas_for_user_text(
        "播放周杰伦的稻香",
        [SING_SONG_SCHEMA, WATCH_ANIME_SCHEMA],
    )

    assert [schema["name"] for schema in offered] == ["sing_song"]


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


def test_cancel_tool_is_only_offered_for_explicit_stop_request():
    h = _host(enabled=True, sink=lambda _event: None)
    h._anime_in_flight = lambda: {
        "request_id": "REQ1",
        "title": "幼女战记 第二季",
    }
    _install(h)

    assert {"watch_anime", "cancel_anime_download"} <= _tool_names(h.registry)
    assert "cancel_anime_download" not in _offered_names(
        h.registry, "今晚陪我聊一会儿"
    )
    assert "cancel_anime_download" in _offered_names(
        h.registry, "取消当前动漫下载"
    )
    assert "cancel_anime_download" in _offered_names(
        h.registry, "不要下无职转生第一集"
    )
    assert "watch_anime" not in _offered_names(
        h.registry, "不要下无职转生第一集"
    )


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

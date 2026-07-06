"""Phase 3: AnimeConfig typed defaults + app.yaml + secrets/env roster."""

from __future__ import annotations

from spica.config.env_roster import consumed_env_names
from spica.config.manager import ConfigManager
from spica.config.schema import AnimeConfig, AppConfig
from spica.config.secrets import Secrets


def test_anime_config_defaults():
    a = AnimeConfig()
    assert a.enabled is False
    assert a.download_dir == "~/Videos/SpicaAnime"
    assert a.bilibili_spaces == ["3493112693394137"]
    assert a.mikan_base_urls == ["https://mikanani.me"]
    assert a.quality == "1080p"
    assert a.subtitle_preference == ["简繁", "简体"]
    assert a.source_timeout_seconds == 15.0
    assert a.resolve_budget_seconds == 45.0
    assert a.qbittorrent_url == "http://127.0.0.1:8080"
    assert a.qbittorrent_username == "admin"


def test_appconfig_has_anime_section():
    assert isinstance(AppConfig().anime, AnimeConfig)


def test_resolved_config_anime_enabled_false():
    # app.yaml ships anime.enabled: false -> resolves false (== default: zero diff)
    assert ConfigManager().load().anime.enabled is False


def test_phase4_knobs_not_yet_added():
    # review #5: don't pre-freeze Phase-4 worker knobs into the config surface
    a = AnimeConfig()
    for absent in ("disk_limit_gb", "auto_play_threshold_seconds",
                   "stall_timeout_minutes", "qbittorrent_poll_seconds",
                   "ytdlp_format", "bilibili_fallback_search", "preferred_subgroups"):
        assert not hasattr(a, absent), f"{absent} should be deferred to Phase 4"


def test_secrets_have_anime_fields_default_none():
    s = Secrets()
    assert s.bilibili_cookie is None
    assert s.qbittorrent_password is None


def test_secret_env_names_in_roster():
    names = consumed_env_names()
    assert "BILIBILI_COOKIE" in names
    assert "QBITTORRENT_PASSWORD" in names

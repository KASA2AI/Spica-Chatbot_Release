"""Phase 3/4: AnimeConfig typed defaults + app.yaml + secrets/env roster."""

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


def test_resolved_config_anime_enabled_true():
    # Phase 4 端到端验收通过后翻 true：app.yaml ships anime.enabled: true -> resolves
    # true (deliberate non-default override; resolved-config diff = this one key).
    assert ConfigManager().load().anime.enabled is True


def test_phase4_worker_knobs_defaults():
    # Phase 4 landed the worker/completion/persistence knobs (yaml-only typed,
    # defaults == the hardcoded values of this round -> zero resolved diff
    # beyond the new anime.* keys).
    a = AnimeConfig()
    assert a.auto_play_threshold_seconds == 300.0
    assert a.qbittorrent_poll_seconds == 5.0
    assert a.stall_timeout_minutes == 30.0
    assert a.ytdlp_format == "bv*[height<=1080]+ba/b[height<=1080]"
    assert a.cookies_file == "data/cookies.txt"
    assert a.library_file == "data/anime/library.json"


def test_phase5_knobs_still_deferred():
    # disk reminder & source preferences stay deferred (Phase 5 打磨)
    a = AnimeConfig()
    for absent in ("disk_limit_gb", "bilibili_fallback_search",
                   "preferred_subgroups"):
        assert not hasattr(a, absent), f"{absent} should be deferred to Phase 5"


def test_anime_config_empty_mikan_urls_accepted():
    # P2-6 (D2): an empty mikan_base_urls is TOLERATED at config load (not
    # rejected by a validator) -- the assembly skips that source instead of
    # crashing startup. Fixes the "容忍不拒" semantics.
    a = AnimeConfig(mikan_base_urls=[])
    assert a.mikan_base_urls == []


def test_secrets_have_anime_fields_default_none():
    s = Secrets()
    assert s.bilibili_cookie is None
    assert s.qbittorrent_password is None


def test_secret_env_names_in_roster():
    names = consumed_env_names()
    assert "BILIBILI_COOKIE" in names
    assert "QBITTORRENT_PASSWORD" in names

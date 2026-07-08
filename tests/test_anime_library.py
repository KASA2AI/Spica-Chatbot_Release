"""Phase 1 tests: library dedup / pointer / disk / reconcile, playback policy."""

from __future__ import annotations

from spica.anime.library import AnimeLibrary, LibraryEntry, episode_key
from spica.anime.models import anime_dirname
from spica.anime.playback_policy import (
    ANNOUNCE,
    AUTO_PLAY,
    decide_playback,
)

GB = 1024 ** 3


def _entry(title="无职转生", season=3, episode=1, size=GB, path=None, played=False,
           added_at="2026-07-06T10:00:00"):
    key = episode_key(title, season, episode)
    return LibraryEntry(
        episode_key=key, title=title, season=season, episode=episode,
        file_path=path or f"/dl/{title}_s{season}e{episode}.mkv",
        size_bytes=size, source="mikan", added_at=added_at, played=played,
    )


# -- dedup / lookup ----------------------------------------------------------

def test_find_hit_and_miss():
    lib = AnimeLibrary([_entry(episode=1)])
    assert lib.find(episode_key("无职转生", 3, 1)) is not None
    assert lib.find(episode_key("无职转生", 3, 2)) is None


def test_episode_key_season_none_is_s1():
    assert episode_key("X", None, 1) == episode_key("X", 1, 1)


# -- anime_dirname: per-anime download subfolder (safe single component) ------

def test_anime_dirname_keeps_readable_name():
    assert anime_dirname("无职转生 第三季") == "无职转生 第三季"


def test_anime_dirname_strips_separators_and_reserved():
    # path separators + Windows-reserved chars removed -> still ONE component
    out = anime_dirname('无职转生/第三季: "01" *?<>|\\x')
    assert "/" not in out and "\\" not in out
    assert not any(c in out for c in ':*?"<>|')
    assert out and out != "未命名"


def test_anime_dirname_blocks_traversal():
    for evil in ("..", ".", "../../etc", "..\\..\\x", "  ..  "):
        out = anime_dirname(evil)
        assert out not in (".", "..")
        assert "/" not in out and "\\" not in out


def test_anime_dirname_empty_or_all_unsafe_falls_back():
    assert anime_dirname("") == "未命名"
    assert anime_dirname("   ") == "未命名"
    assert anime_dirname("///") == "未命名"


def test_anime_dirname_trims_trailing_dot_space():
    assert anime_dirname("name. ") == "name"           # Windows rejects trailing dot/space


def test_anime_dirname_byte_budget_not_char_count():
    out = anime_dirname("超长" * 200)                   # 400 CJK chars = 1200 bytes
    assert len(out.encode("utf-8")) <= 200             # bounded by BYTES, not chars


def test_anime_dirname_no_trailing_dot_after_truncation():
    # truncation boundary lands on a "." -> must be stripped, not left dangling
    out = anime_dirname("x" * 199 + "." + "y" * 20)
    assert not out.endswith((".", " "))


def test_anime_dirname_rewrites_windows_reserved_names():
    assert anime_dirname("CON") == "_CON"
    assert anime_dirname("aux.txt") == "_aux.txt"       # reserved even with extension
    assert anime_dirname("COM1") == "_COM1"
    assert anime_dirname("console") == "console"        # not reserved -> untouched


def test_anime_dirname_reserved_prefix_stays_within_byte_budget():
    # the "_" prefix must not push a max-length reserved name over budget
    out = anime_dirname("CON." + "长" * 200)            # reserved stem + long tail
    assert out.startswith("_")                          # still rewritten
    assert len(out.encode("utf-8")) <= 200              # AND within the byte budget


def test_add_and_contains():
    lib = AnimeLibrary()
    e = _entry()
    lib.add(e)
    assert e.episode_key in lib


def test_add_returns_was_new_and_overwrites():
    lib = AnimeLibrary()
    e = _entry(size=GB)
    assert lib.add(e) is True                       # new key
    assert lib.add(_entry(size=2 * GB)) is False    # same key -> replaced
    assert lib.disk_usage_bytes() == 2 * GB         # overwrite is intentional


def test_added_at_is_timezone_aware():
    e = LibraryEntry(episode_key="k", title="X", season=1, episode=1,
                     file_path="/x.mkv", size_bytes=1, source="mikan")
    assert e.added_at.endswith("+00:00")   # tz-aware default (finding #9)


# -- 「放吧」pointer ---------------------------------------------------------

def test_most_recent_unplayed_pointer():
    lib = AnimeLibrary([
        _entry(episode=1, added_at="2026-07-06T10:00:00"),
        _entry(episode=2, added_at="2026-07-06T12:00:00"),
    ])
    assert lib.most_recent_unplayed().episode == 2


def test_pointer_skips_played():
    lib = AnimeLibrary([
        _entry(episode=1, added_at="2026-07-06T10:00:00", played=True),
        _entry(episode=2, added_at="2026-07-06T09:00:00", played=False),
    ])
    assert lib.most_recent_unplayed().episode == 2


def test_pointer_none_when_all_played():
    lib = AnimeLibrary([_entry(played=True)])
    assert lib.most_recent_unplayed() is None


def test_mark_played():
    e = _entry()
    lib = AnimeLibrary([e])
    lib.mark_played(e.episode_key)
    assert lib.find(e.episode_key).played is True


# -- disk accounting ---------------------------------------------------------

def test_disk_usage_and_over_limit():
    lib = AnimeLibrary([_entry(episode=1, size=60 * GB),
                        _entry(episode=2, size=50 * GB)])
    assert lib.disk_usage_bytes() == 110 * GB
    assert lib.over_limit(100) is True
    assert lib.over_limit(200) is False


# -- reconcile (register-only, P1-9) -----------------------------------------

def test_reconcile_registers_only_new():
    lib = AnimeLibrary([_entry(episode=1)])
    added = lib.reconcile([_entry(episode=1), _entry(episode=2)])
    assert [e.episode for e in added] == [2]   # ep1 already known, not re-added
    assert lib.find(episode_key("无职转生", 3, 2)) is not None


# -- persistence round-trip --------------------------------------------------

def test_json_round_trip():
    lib = AnimeLibrary([_entry(episode=1), _entry(episode=2)])
    lib2 = AnimeLibrary.from_json(lib.to_json())
    assert lib2.disk_usage_bytes() == lib.disk_usage_bytes()
    assert lib2.find(episode_key("无职转生", 3, 2)) is not None


# -- playback policy (D5 / P1-7 / P1-9) --------------------------------------

def test_auto_play_when_fast_and_idle():
    d = decide_playback(elapsed_seconds=120, threshold_seconds=300,
                        is_busy=False, galgame_active=False)
    assert d.action == AUTO_PLAY


def test_announce_when_slow():
    d = decide_playback(elapsed_seconds=600, threshold_seconds=300,
                        is_busy=False, galgame_active=False)
    assert d.action == ANNOUNCE


def test_announce_when_busy():
    d = decide_playback(elapsed_seconds=10, threshold_seconds=300,
                        is_busy=True, galgame_active=False)
    assert d.action == ANNOUNCE


def test_announce_when_galgame_active():
    d = decide_playback(elapsed_seconds=10, threshold_seconds=300,
                        is_busy=False, galgame_active=True)
    assert d.action == ANNOUNCE


def test_announce_when_reconciled_unknown_age():
    d = decide_playback(elapsed_seconds=None, threshold_seconds=300,
                        is_busy=False, galgame_active=False,
                        reconciled_unknown_age=True)
    assert d.action == ANNOUNCE

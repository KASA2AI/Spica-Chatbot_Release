"""R1 (P5 step 4-B) config-wiring + lexicon hot-reload pins.

Covers the knobs moved onto ``GalgameConfig`` while keeping the un-set defaults
byte-identical to the prior hardcoded values (the Layer A snapshot gate proves
that for the resolved config; here we pin the code seams):

- merge_mode_table: None -> the code REACTION_MODE_TABLE *by identity* (做法X:
  an un-set ``reaction_table`` leaves the code table the source of truth); a
  provided override replaces only the named tiers.
- compose_reaction_directive: the reply-length / excerpt caps are now params
  defaulting to the module constants, so a bare call is unchanged and the host's
  config values flow through.
- lexicon hot-reload: editing default.yaml is picked up on the NEXT beat without
  a restart, driven through the REAL ReactionScoringPolicy mtime cache (Phase 4:
  the cache moved off the host into the policy; the host scorer is a delegate).
"""

from __future__ import annotations

import os

import pytest

from spica.galgame.reaction import (
    REACTION_MODE_TABLE,
    BeatLine,
    ReactionBeat,
    ReactionModeParams,
    compose_reaction_directive,
    lexicon_source_mtime,
    load_reaction_lexicon,
    merge_mode_table,
    score_beat,
)


def _twist_beat(reason: str = "idle_flush") -> ReactionBeat:
    return ReactionBeat(
        lines=(BeatLine(None, "真相", "l1"),), game_id="g1", cut_reason=reason
    )


# -- reaction_table merge (做法X) ------------------------------------------------


def test_merge_mode_table_none_is_the_code_table_by_identity():
    # 做法X: an un-set reaction_table -> the code REACTION_MODE_TABLE itself (not a
    # copy), so resolution is byte-identical and the code table stays the truth.
    assert merge_mode_table(None) is REACTION_MODE_TABLE
    assert merge_mode_table({}) is REACTION_MODE_TABLE


def test_merge_mode_table_override_replaces_only_named_tiers():
    override = {"normal": ReactionModeParams(min_score=2, max_per_window=9, cooldown_seconds=10.0)}
    merged = merge_mode_table(override)
    assert merged["normal"] == ReactionModeParams(min_score=2, max_per_window=9, cooldown_seconds=10.0)
    # tiers the override omits fall back to the code defaults (partial table safe)
    assert merged["low"] is REACTION_MODE_TABLE["low"]
    assert merged["high"] is REACTION_MODE_TABLE["high"]
    assert REACTION_MODE_TABLE["normal"].min_score == 4  # the code table is not mutated


# -- compose_reaction_directive parameterization ---------------------------------


def test_directive_default_reply_limit_is_40_and_byte_identical_phrase():
    directive = compose_reaction_directive(_twist_beat())
    assert "不超过40个字" in directive  # default == the prior hardcoded literal


def test_directive_reply_limit_override_flows_into_prompt():
    directive = compose_reaction_directive(_twist_beat(), reply_char_limit=25)
    assert "不超过25个字" in directive
    assert "不超过40个字" not in directive


def test_directive_excerpt_caps_bound_the_story_text():
    long_line = BeatLine("朱比華", "啊" * 200, "l1")
    beat = ReactionBeat(lines=(long_line,), game_id="g1", cut_reason="idle_flush")
    # tiny per-line cap truncates the excerpt; the instruction tail still survives
    directive = compose_reaction_directive(beat, line_char_cap=5, excerpt_char_cap=50)
    assert "啊" * 5 in directive and "啊" * 6 not in directive
    assert "不超过40个字" in directive  # instruction never truncated by the caps


# -- lexicon source mtime --------------------------------------------------------


def test_lexicon_source_mtime_tracks_the_newest_source_file(tmp_path):
    (tmp_path / "default.yaml").write_text("categories: {}\n", encoding="utf-8")
    base = lexicon_source_mtime(None, base_dir=tmp_path)
    assert base > 0
    os.utime(tmp_path / "default.yaml", (base + 50, base + 50))
    assert lexicon_source_mtime(None, base_dir=tmp_path) == base + 50
    # a missing dir -> 0.0 (inert, never raises)
    assert lexicon_source_mtime("nope", base_dir=tmp_path / "absent") == 0.0


# -- hot reload through the REAL ReactionScoringPolicy --------------------------


def _lexicon_policy():
    """A ReactionScoringPolicy on the judge-off path (Phase 4): the production
    mtime hot-reload cache under test lives on the policy; config/game_memory/
    identity are never read on this path, so their providers return None."""
    from spica.galgame.reaction_scoring import ReactionScoringPolicy

    return ReactionScoringPolicy(
        config_provider=lambda: None,
        game_scope_provider=lambda: ("g1", "default", object()),
        game_memory_provider=lambda: None,
        character_scope_provider=lambda: None,
        judge_provider=lambda: None,  # judge off -> the lexicon (hot-reload) path
    )


def test_reaction_scorer_hot_reloads_lexicon_on_mtime_change(tmp_path, monkeypatch):
    """Edit default.yaml -> the NEXT beat scores with the new weight, NO restart.
    Drives the real ReactionScoringPolicy.score (its mtime cache is the
    production hot-reload), with the lexicon data dir pointed at a temp file."""
    import spica.galgame.reaction as reaction

    monkeypatch.setattr(reaction, "_REACTION_DATA_DIR", tmp_path)
    lexicon_path = tmp_path / "default.yaml"
    lexicon_path.write_text(
        "categories:\n  twist:\n    weight: 4\n    words: [真相]\n", encoding="utf-8"
    )

    policy = _lexicon_policy()

    beat = _twist_beat()
    assert policy.score(beat).score == 4  # first read caches (lexicon + mtime)

    base = lexicon_source_mtime("g1")  # reads the patched _REACTION_DATA_DIR
    lexicon_path.write_text(
        "categories:\n  twist:\n    weight: 9\n    words: [真相]\n", encoding="utf-8"
    )
    os.utime(lexicon_path, (base + 10, base + 10))  # force a distinct mtime

    assert policy.score(beat).score == 9  # hot-reloaded, same policy instance


def test_reaction_scorer_does_not_reload_when_file_unchanged(tmp_path, monkeypatch):
    """No mtime change -> the cached lexicon is reused (no per-beat re-read storm)."""
    import spica.galgame.reaction as reaction

    monkeypatch.setattr(reaction, "_REACTION_DATA_DIR", tmp_path)
    (tmp_path / "default.yaml").write_text(
        "categories:\n  twist:\n    weight: 4\n    words: [真相]\n", encoding="utf-8"
    )
    calls = {"n": 0}
    real_load = reaction.load_reaction_lexicon

    def _counting_load(game_id=None, base_dir=None):
        calls["n"] += 1
        return real_load(game_id, base_dir)

    # Phase 4: the cache (and its load import) live on reaction_scoring now.
    monkeypatch.setattr("spica.galgame.reaction_scoring.load_reaction_lexicon", _counting_load)

    policy = _lexicon_policy()

    beat = _twist_beat()
    policy.score(beat)
    policy.score(beat)
    policy.score(beat)
    assert calls["n"] == 1  # loaded once, then served from cache


if __name__ == "__main__":
    import sys

    sys.exit(pytest.main([__file__, "-q"]))

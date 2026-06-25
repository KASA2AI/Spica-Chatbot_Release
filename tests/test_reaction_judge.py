"""P5 v2 step 2 pins: the LLM reaction judge + its host wiring.

Covers the checklist's testable items:
  ① JSON parse (valid / markdown-wrapped / garbage / worth=0 is NOT an error)
  ② degrade fallback (judge raises -> closure falls back to lexicon scoring on the
     LEXICON scale, 不沉默; 叉口②-b)
  ③ window read (the closure feeds the judge the scene window tail-N + summaries +
     progress + recent beats; engine-untouched seam)
  ④ judge-cooldown (within the window the second beat returns 0 without an LLM call)
  ⑥ zero-diff (judge off -> the scorer IS score_beat; the two config fields default
     off/None)

⑤ (engine 0-change) and ⑦ (directive unchanged) are proved by ``git diff
spica/galgame/reaction.py`` being empty + the existing reaction golden suite
staying green -- compose_reaction_directive lives in that untouched file, so ⑦ ⊆ ⑤.
"""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from spica.config.schema import AppConfig, GalgameConfig, LLMConfig
from spica.galgame.reaction import (
    BeatLine,
    LexiconCategory,
    ReactionBeat,
    ReactionLexicon,
    ScoreResult,
    load_reaction_lexicon,
    score_beat,
)
from spica.galgame.reaction_judge import (
    GalgameReactionJudge,
    JudgeVerdict,
    ReactionJudgeError,
    _clamp_worth,
    _parse,
)
from spica.host import app_host as app_host_module
from spica.host.app_host import AppHost


# -- helpers ------------------------------------------------------------------

def _beat(*lines, cut_reason="idle_flush", game_id="limelight"):
    return ReactionBeat(
        lines=tuple(BeatLine(sp, tx, f"l{i}") for i, (sp, tx) in enumerate(lines)),
        game_id=game_id,
        cut_reason=cut_reason,
    )


def _line(text, speaker="S"):
    return SimpleNamespace(speaker=speaker, text=text)


# A controlled lexicon so the degrade test does not depend on the live
# default.yaml contents (which the user hand-edits): "真相" -> twist weight 4.
_KNOWN_LEXICON = ReactionLexicon(
    categories=(LexiconCategory(name="twist", weight=4, words=("真相",)),),
    signals={},
)


class _FakeGM:
    def __init__(self, window=None, summaries=None, progress=None, beats=None):
        self._window, self._summaries = window or [], summaries or []
        self._progress, self._beats = progress, beats or []

    def unsummarized_committed_story_lines(self, game_id, playthrough_id):
        return list(self._window)

    def recent_summaries(self, game_id, playthrough_id, limit=5):
        return list(self._summaries[:limit])

    def get_progress_state(self, game_id, playthrough_id):
        return self._progress

    def recent_companion_beats_for_prompt(self, game_id, user_id, character_id, limit=10):
        return list(self._beats[:limit])


class _RecordingLLM:
    def __init__(self, response='{"worth": 7, "moment": "m", "angle": "吐槽"}'):
        self.response, self.prompt, self.model = response, None, None

    def complete_text(self, prompt, *, model):
        self.prompt, self.model = prompt, model
        return self.response


class _RaisingJudge:
    def judge(self, **kw):
        raise ReactionJudgeError("boom")


class _CountingJudge:
    def __init__(self, verdict):
        self.verdict, self.calls = verdict, 0

    def judge(self, **kw):
        self.calls += 1
        return self.verdict


def _judge_host(judge, *, mode="high", scope=("limelight", "default", object())):
    """A bare AppHost wired just enough to drive ``_reaction_scorer``: judge set,
    a fake game-scope + game_memory, no initialize()/DB/network."""
    host = AppHost()
    host.config = AppConfig(galgame=GalgameConfig(reaction_mode=mode))
    host._reaction_judge = judge
    host._reaction_game_scope = lambda: scope
    host.services = SimpleNamespace(game_memory_adapter=_FakeGM())
    return host


# -- ① JSON parse -------------------------------------------------------------

class JudgeParseTest(unittest.TestCase):
    def test_valid_json_embedded_in_text(self):
        v = _parse('blah {"worth": 7, "moment": "x", "angle": "感想"} trailing')
        self.assertEqual((v.worth, v.moment, v.angle), (7, "x", "感想"))

    def test_markdown_fenced_json(self):
        v = _parse('```json\n{"worth": 5, "moment": "y", "angle": "吐槽"}\n```')
        self.assertEqual((v.worth, v.angle), (5, "吐槽"))

    def test_worth_zero_is_valid_not_an_error(self):
        v = _parse('{"worth": 0}')  # a confident "not worth it" -- must NOT raise
        self.assertEqual(v.worth, 0)

    def test_worth_clamped_to_0_10(self):
        self.assertEqual(_clamp_worth(99), 10)
        self.assertEqual(_clamp_worth(-3), 0)
        self.assertEqual(_clamp_worth("6"), 6)
        self.assertIsNone(_clamp_worth("abc"))

    def test_invalid_angle_dropped_to_empty(self):
        self.assertEqual(_parse('{"worth": 6, "angle": "胡说"}').angle, "")

    def test_garbage_and_malformed_raise(self):
        for bad in ("no json here", "{}", '{"moment": "x"}', '{"worth": "abc"}'):
            with self.assertRaises(ReactionJudgeError):
                _parse(bad)

    def test_judge_wraps_llm_error(self):
        class _Boom:
            def complete_text(self, prompt, *, model):
                raise RuntimeError("net down")

        with self.assertRaises(ReactionJudgeError):
            GalgameReactionJudge(_Boom(), "m").judge(beat_lines=[_line("hi")])

    def test_judge_empty_beat_raises(self):
        with self.assertRaises(ReactionJudgeError):
            GalgameReactionJudge(_RecordingLLM(), "m").judge(beat_lines=[])

    def test_judge_returns_verdict_from_llm(self):
        v = GalgameReactionJudge(_RecordingLLM(), "m").judge(beat_lines=[_line("hi")])
        self.assertEqual(v, JudgeVerdict(worth=7, moment="m", angle="吐槽"))


# -- ② degrade fallback (叉口②-b) --------------------------------------------

class DegradeFallbackTest(unittest.TestCase):
    def setUp(self):
        self.host = _judge_host(_RaisingJudge(), mode="high")
        self.host._reaction_lexicon_for = lambda gid: _KNOWN_LEXICON  # deterministic

    def test_judge_failure_falls_back_to_lexicon_not_silence(self):
        # "真相" -> lexicon score 4 >= high min_score 3 -> fallback PASSES (不沉默)
        result = self.host._reaction_scorer(_beat(("S", "原来这就是真相")))
        self.assertGreater(result.score, 0)
        self.assertIn("lexicon_fallback", result.reasons)

    def test_judge_failure_on_daily_line_scores_zero(self):
        # no lexicon word -> lexicon score 0 < 3 -> fallback returns 0 (correctly quiet)
        result = self.host._reaction_scorer(_beat(("S", "今天天气不错呢")))
        self.assertEqual(result.score, 0)
        self.assertIn("lexicon_fallback", result.reasons)

    def test_fallback_uses_lexicon_scale_not_worth_scale(self):
        # The decisive 叉口②-b pin: the pass decision is the LEXICON min_score (3),
        # NOT a worth-scale engine threshold. lex=4 passes; encoded as a big score
        # so the engine's worth threshold can never silence it.
        passed = self.host._reaction_scorer(_beat(("S", "真相大白")))
        self.assertEqual(passed.score, app_host_module._LEXICON_FALLBACK_PASS_SCORE)


# -- ③ window read ------------------------------------------------------------

class WindowReadTest(unittest.TestCase):
    def test_closure_feeds_window_tail_summaries_progress_recent(self):
        rec = _RecordingLLM()
        host = _judge_host(GalgameReactionJudge(rec, "m"))
        host._reaction_game_scope = lambda: ("limelight", "default", object())
        host.services = SimpleNamespace(
            game_memory_adapter=_FakeGM(
                window=[_line(f"L{i}") for i in range(30)],  # 30 lines; tail-24 kept
                summaries=[SimpleNamespace(summary_zh="SUMMARY_TEXT")],
                progress=SimpleNamespace(
                    route={"name": "ROUTE_X", "confirmed": False, "confidence": 0.5},
                    chapter={"title": "CH1"},
                ),
                beats=[SimpleNamespace(content="RECENT_BEAT")],
            )
        )
        result = host._reaction_scorer(_beat(("S", "BEAT_LINE")))
        prompt = rec.prompt
        self.assertEqual(result.score, 7)  # worth flows through to ScoreResult
        self.assertIn("BEAT_LINE", prompt)  # [刚刚这一下] focus beat
        self.assertIn("L29", prompt)  # window tail present
        self.assertIn("L6", prompt)  # tail boundary (30 - 24)
        self.assertNotIn("L5", prompt)  # beyond tail-24 -> excluded
        self.assertNotIn("L0", prompt)
        self.assertIn("SUMMARY_TEXT", prompt)  # [前情]
        self.assertIn("ROUTE_X", prompt)  # progress route
        self.assertIn("RECENT_BEAT", prompt)  # [她最近说过]


# -- ④ judge-cooldown ---------------------------------------------------------

class JudgeCooldownTest(unittest.TestCase):
    def test_within_cooldown_returns_zero_without_calling_llm(self):
        judge = _CountingJudge(JudgeVerdict(worth=7, moment="m", angle="吐槽"))
        host = _judge_host(judge)
        # settable fake clock: robust to however many monotonic reads a scorer call
        # makes (cooldown check + judge-timing), unlike a fixed side_effect list.
        clock = {"t": 0.0}
        with patch.object(app_host_module.time, "monotonic", lambda: clock["t"]):
            clock["t"] = 0.0
            r1 = host._reaction_scorer(_beat(("S", "a")))   # judge runs, last_at=0
            clock["t"] = 5.0
            r2 = host._reaction_scorer(_beat(("S", "b")))   # 5-0<15 -> cooldown, no LLM
            clock["t"] = 20.0
            r3 = host._reaction_scorer(_beat(("S", "c")))   # 20-0>=15 -> judge runs again
        self.assertEqual(r1.score, 7)
        self.assertEqual((r2.score, r2.reasons), (0, ("judge_cooldown",)))
        self.assertEqual(r3.score, 7)
        self.assertEqual(judge.calls, 2)  # the cooldown beat did NOT hit the LLM


# -- ⑥ zero-diff + config defaults --------------------------------------------

class ZeroDiffTest(unittest.TestCase):
    def test_config_fields_default_off(self):
        self.assertFalse(AppConfig().galgame.reaction_judge_enabled)
        self.assertIsNone(AppConfig().galgame.reaction_judge_model)

    def test_judge_off_scorer_is_score_beat(self):
        host = AppHost()
        host.config = AppConfig(galgame=GalgameConfig(reaction_mode="high"))
        host._reaction_judge = None  # judge off (default)
        host._reaction_game_scope = lambda: None  # no live play -> default lexicon
        beat = _beat(("S", "原来这就是真相"))
        self.assertEqual(
            host._reaction_scorer(beat), score_beat(beat, load_reaction_lexicon(None))
        )

    def test_new_reaction_judge_none_when_disabled(self):
        host = AppHost()
        host.config = AppConfig(galgame=GalgameConfig(reaction_judge_enabled=False))
        host.services = SimpleNamespace(llm_adapter=SimpleNamespace())
        self.assertIsNone(host._new_reaction_judge())

    def test_new_reaction_judge_model_fallback_and_override(self):
        host = AppHost()
        host.services = SimpleNamespace(llm_adapter=SimpleNamespace())
        # None -> dialogue model (mirrors summary_model)
        host.config = AppConfig(
            llm=LLMConfig(model="dialogue-m"),
            galgame=GalgameConfig(reaction_judge_enabled=True),
        )
        self.assertEqual(host._new_reaction_judge()._model, "dialogue-m")
        # explicit override
        host.config = AppConfig(
            llm=LLMConfig(model="dialogue-m"),
            galgame=GalgameConfig(reaction_judge_enabled=True, reaction_judge_model="small-m"),
        )
        self.assertEqual(host._new_reaction_judge()._model, "small-m")


if __name__ == "__main__":
    unittest.main()

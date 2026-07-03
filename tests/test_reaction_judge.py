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

import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from spica.config.schema import AppConfig, GalgameConfig, LLMConfig
from spica.config.secrets import Secrets, load_secrets
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
from spica.galgame.reaction_scoring import (
    _LEXICON_FALLBACK_PASS_SCORE,
    ReactionScoringPolicy,
)
from spica.host.app_host import AppHost
from spica.host.assemblies import reaction as reaction_assembly


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
        # Phase 4: the deterministic-lexicon seam lives on the policy (a host-
        # level attribute override would be dead code behind the thin delegate).
        self.host._reaction_scoring_policy.lexicon_for = lambda gid: _KNOWN_LEXICON

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
        self.assertEqual(passed.score, _LEXICON_FALLBACK_PASS_SCORE)


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
        # settable fake clock, INJECTED into the policy (Phase 4): robust to
        # however many clock reads a scorer call makes (cooldown check +
        # judge-timing), and impossible to fake-green -- without the injection
        # the cooldown window would read the real clock and r2 would call the LLM.
        clock = {"t": 0.0}
        host._reaction_scoring_policy = ReactionScoringPolicy(
            config_provider=lambda: host.config,
            game_scope_provider=lambda: host._reaction_game_scope(),
            game_memory_provider=lambda: host.services.game_memory_adapter,
            character_scope_provider=lambda: host.character_scope,
            judge_provider=lambda: host._reaction_judge,
            clock=lambda: clock["t"],
        )
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


# -- ⑧ Phase 4 patch validity: the three facades are ON the real build path ----

class PatchValidityTest(unittest.TestCase):
    """Phase 4 exit ③ (anti-no-op-patch): assemblies.reaction builds THROUGH the
    AppHost thin delegates, so patch.object(AppHost, ...) -- the shape the
    moondream cutover 15-patch and future tests rely on -- always intercepts
    real construction. A sentinel that fails to arrive means a facade exists
    but is no longer on the build path."""

    def test_install_builds_judge_through_the_host_delegate(self):
        host = AppHost()
        sentinel = object()
        with patch.object(AppHost, "_new_reaction_judge", return_value=sentinel), \
             patch.object(AppHost, "_build_reaction_engine", return_value=None):
            reaction_assembly.install(host)
        self.assertIs(host._reaction_judge, sentinel)

    def test_install_builds_engine_through_the_host_delegate(self):
        host = AppHost()
        sentinel = object()
        with patch.object(AppHost, "_new_reaction_judge", return_value=None), \
             patch.object(AppHost, "_build_reaction_engine", return_value=sentinel):
            reaction_assembly.install(host)
        self.assertIs(host.reaction_engine, sentinel)

    def test_new_reaction_judge_takes_adapter_through_the_host_delegate(self):
        host = AppHost()
        host.config = AppConfig(galgame=GalgameConfig(reaction_judge_enabled=True))
        host.services = SimpleNamespace(llm_adapter=object())
        sentinel_adapter = object()
        with patch.object(AppHost, "_judge_llm_adapter", return_value=sentinel_adapter):
            judge = host._new_reaction_judge()
        self.assertIs(judge._llm, sentinel_adapter)


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


# -- judge LLM-key split (relieve the deepseek endpoint saturation) -----------

class JudgeKeySplitTest(unittest.TestCase):
    """The reaction judge runs on its OWN endpoint (JUDGE_API_KEY + JUDGE_BASE_URL +
    JUDGE_MODEL) so its load never saturates the main chat/summary endpoint. Each
    knob falls back to the main LLM independently; no key -> shares the main adapter
    (zero behaviour change). Chat + summary always stay on the main adapter."""

    def _host(self, judge_key, judge_base_url=None):
        host = AppHost()
        host.config = AppConfig(
            llm=LLMConfig(provider="openai_compatible", model="m", base_url="https://main.example/v1"),
            galgame=GalgameConfig(reaction_judge_base_url=judge_base_url),
        )
        main_adapter = SimpleNamespace(name="main")
        host.services = SimpleNamespace(llm_adapter=main_adapter)
        host.secrets = Secrets(openai_api_key="K1", judge_api_key=judge_key)
        # stub the registry so resolve_llm returns a marker carrying the built client
        # + the reasoning effort (no real provider registration needed for the unit).
        host.registry = SimpleNamespace(
            resolve_llm=lambda provider, client, reasoning_effort="default": SimpleNamespace(
                tag="judge", client=client, reasoning_effort=reasoning_effort))
        return host, main_adapter

    def test_separate_key_builds_distinct_adapter_with_second_key(self):
        host, main = self._host(judge_key="K2")  # base_url unset -> falls back to main
        adapter = host._judge_llm_adapter()
        self.assertIsNot(adapter, main)                 # judge adapter != main
        self.assertEqual(adapter.tag, "judge")
        self.assertEqual(adapter.client.api_key, "K2")  # built from the judge key
        self.assertIn("main.example", str(adapter.client.base_url))  # base_url fell back to main

    def test_separate_base_url_when_set(self):
        host, _main = self._host(judge_key="K2", judge_base_url="https://judge.example/v1")
        adapter = host._judge_llm_adapter()
        self.assertIn("judge.example", str(adapter.client.base_url))  # judge endpoint used
        self.assertEqual(adapter.client.api_key, "K2")

    def test_judge_reasoning_effort_independent_of_main(self):
        host, _ = self._host(judge_key="K2")
        host.config.llm.reasoning_effort = "none"                       # main off
        host.config.galgame.reaction_judge_reasoning_effort = "low"     # judge on (gpt effort)
        adapter = host._judge_llm_adapter()
        self.assertEqual(adapter.reasoning_effort, "low")  # judge gets ITS knob, not main's

    def test_no_judge_key_falls_back_to_main(self):
        host, main = self._host(judge_key=None)
        self.assertIs(host._judge_llm_adapter(), main)  # zero behaviour change

    def test_secrets_none_falls_back_to_main(self):
        host, main = self._host(judge_key="K2")
        host.secrets = None  # e.g. a test/host built without load_secrets
        self.assertIs(host._judge_llm_adapter(), main)

    def test_judge_model_from_env_field_else_main(self):
        # JUDGE_MODEL -> galgame.reaction_judge_model; unset -> config.llm.model.
        host, _ = self._host(judge_key="K2")
        host.config = AppConfig(
            llm=LLMConfig(model="main-m"),
            galgame=GalgameConfig(reaction_judge_enabled=True, reaction_judge_model="judge-m"),
        )
        self.assertEqual(host._new_reaction_judge()._model, "judge-m")

    def test_summary_stays_on_main_key(self):
        host, _main = self._host(judge_key="K2")
        host.config = AppConfig(llm=LLMConfig(model="m"), galgame=GalgameConfig())
        # summary must NOT move to the judge endpoint -- it reads services.llm_adapter.
        self.assertIs(host._new_summarizer()._llm, host.services.llm_adapter)

    def test_load_secrets_reads_both_keys(self):
        import spica.config.secrets as secmod
        with patch.object(secmod, "_ensure_env_loaded", lambda: None), patch.dict(
            os.environ, {"OPENAI_API_KEY": "K1", "JUDGE_API_KEY": "K2"}, clear=True
        ):
            s = load_secrets()
        self.assertEqual((s.openai_api_key, s.judge_api_key), ("K1", "K2"))

    def test_manager_env_overrides_judge_base_url_and_model(self):
        # JUDGE_BASE_URL / JUDGE_MODEL flow through manager.py (config, not secret)
        # onto galgame.reaction_judge_base_url / reaction_judge_model.
        import spica.config.manager as mgr
        with patch.object(mgr.ConfigManager, "_ensure_env_loaded", lambda *a, **k: None), \
                patch.dict(os.environ, {"JUDGE_MODEL": "jm", "JUDGE_BASE_URL": "https://j/v1"}, clear=True):
            g = mgr.ConfigManager(config_path="/no/such/app.yaml").load().galgame
        self.assertEqual(g.reaction_judge_model, "jm")
        self.assertEqual(g.reaction_judge_base_url, "https://j/v1")

    def test_load_secrets_judge_key_none_when_unset(self):
        import spica.config.secrets as secmod
        with patch.object(secmod, "_ensure_env_loaded", lambda: None), patch.dict(
            os.environ, {"OPENAI_API_KEY": "K1"}, clear=True
        ):
            s = load_secrets()
        self.assertIsNone(s.judge_api_key)


if __name__ == "__main__":
    unittest.main()

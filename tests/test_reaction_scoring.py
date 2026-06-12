"""P5 step 2 pins: the lexicon scorer.

① golden: real-machine LimeLight texts (the test_stable_line corpus precedent)
   -> exact (score, reasons), deterministic (same beat + same lexicon -> same
   result, twice);
② OCR-noise normalize: fullwidth/halfwidth + whitespace variants score the same;
③ deep merge (D-P5-9): a per-game file replaces a same-name category wholesale,
   adds new categories, and leaves the default lexicon object untouched;
④ signal triggers: choice_pending keys on cut_reason (STATE signal, no text
   regex), exclamation_density needs >=2 marks, speaker_swarm needs >=3 names;
⑤ the engine seam: score_beat plugs into ReactionEngine through the same
   scorer Callable null_scorer used.
"""

import unittest

from spica.core.companion_events import (
    GalgameStableLineCommittedEvent,
    GalgameStatusChangedEvent,
)
from spica.galgame.reaction import (
    BeatLine,
    ReactionBeat,
    ReactionEngine,
    load_reaction_lexicon,
    score_beat,
)
from spica.galgame.session import GalgameState

_LEXICON = load_reaction_lexicon()  # shipped data/galgame/reaction/default.yaml


def _beat(lines, cut_reason="idle_flush", options=()):
    return ReactionBeat(
        lines=tuple(BeatLine(s, t, f"l{i}") for i, (s, t) in enumerate(lines)),
        game_id="limelight",
        cut_reason=cut_reason,
        choice_options=tuple(options),
    )


class GoldenScoreTest(unittest.TestCase):
    """Real-machine LimeLight originals (test_stable_line corpus precedent)."""

    def test_comedy_plus_speaker_swarm(self):
        beat = _beat([
            ("雪鹰", "我才不会对你想入非非。 比起这个你赶紧去做防护措施1"),
            ("莉莉子", "嗯～··· 这倒也是呢～···"),
            ("月望", "可是， 我也想买各种小吃····"),
        ])
        result = score_beat(beat, _LEXICON)
        self.assertEqual(result.score, 3)  # comedy(2) + speaker_swarm(1)
        self.assertEqual(result.reasons, ("category:comedy", "signal:speaker_swarm"))

    def test_choice_beat_scores_the_state_signal(self):
        beat = _beat(
            [
                (None, "比设定的闹钟先醒来了。"),
                ("雪鹰", "莉莉子在二楼房间整理浴衣， 不好意思能帮我去看看情况吗？"),
                ("雪鹰", "人比想象中要多啊』"),
            ],
            cut_reason="choice",
            options=("去屋顶", "回教室"),
        )
        result = score_beat(beat, _LEXICON)
        self.assertEqual(result.score, 3)  # choice_pending(3) only
        self.assertEqual(result.reasons, ("signal:choice_pending",))

    def test_twist_tension_and_exclamation_density(self):
        beat = _beat([
            ("月岛", "其实我一直骗着你。"),
            ("雪鹰", "什么！你说什么！"),
            ("月岛", "对不起…我没办法说出真相。"),
        ])
        result = score_beat(beat, _LEXICON)
        self.assertEqual(result.score, 8)  # twist(4)+tension(3)+excl_density(1)
        self.assertEqual(
            result.reasons,
            ("category:twist", "category:tension", "signal:exclamation_density"),
        )

    def test_deterministic_same_beat_same_lexicon_same_result(self):
        beat = _beat([("雪鹰", "其实我一直骗着你！"), ("月岛", "诶！"), ("雪鹰", "对不起。")])
        self.assertEqual(score_beat(beat, _LEXICON), score_beat(beat, _LEXICON))

    def test_plain_lines_score_zero(self):
        beat = _beat([(None, "比设定的闹钟先醒来了。"), (None, "今天也是普通的一天。")])
        self.assertEqual(score_beat(beat, _LEXICON).score, 0)


class NoiseNormalizeTest(unittest.TestCase):
    def test_fullwidth_halfwidth_and_whitespace_variants_score_identically(self):
        clean = _beat([
            ("月岛", "其实我一直骗着你。"),
            ("雪鹰", "什么！你说什么！"),
            ("月岛", "对不起…我没办法说出真相。"),
        ])
        noisy = _beat([
            ("月岛", "其实 我一直骗着你."),       # 空白插入 + 。误读成 .
            ("雪鹰", "什么! 你说 什么!"),          # ！误读成 ! + 空白
            ("月岛", "对不起… 我没办法说出 真相。"),
        ])
        self.assertEqual(score_beat(clean, _LEXICON), score_beat(noisy, _LEXICON))


class DeepMergeTest(unittest.TestCase):
    def _write(self, root, name, text):
        (root / f"{name}.yaml").write_text(text, encoding="utf-8")

    def test_per_game_overrides_and_extends_default(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write(root, "default", (
                "categories:\n"
                "  twist: {weight: 4, words: [其实]}\n"
                "  comedy: {weight: 2, words: [笨蛋]}\n"
                "signals: {choice_pending: 3}\n"
            ))
            self._write(root, "limelight", (
                "categories:\n"
                "  twist: {weight: 9, words: [其实]}\n"        # 同名整体替换
                "  idol: {weight: 5, words: [Live, 偶像]}\n"   # 新 category 生效
                "signals: {exclamation_density: 2}\n"          # signals 按键合并
            ))
            merged = load_reaction_lexicon("limelight", base_dir=root)
            default_only = load_reaction_lexicon(base_dir=root)
            absent = load_reaction_lexicon("no_such_game", base_dir=root)

        beat = _beat([("卫哉", "办专场Live之类的？其实可以。")])  # 真机原文词形
        merged_result = score_beat(beat, merged)
        self.assertEqual(merged_result.score, 14)  # twist 9(替换后) + idol 5(新增)
        self.assertEqual(merged_result.reasons, ("category:twist", "category:idol"))
        self.assertEqual(merged.signals, {"choice_pending": 3, "exclamation_density": 2})
        # default 不受 per-game 文件影响;缺 game 文件 = 纯 default
        self.assertEqual(score_beat(beat, default_only).score, 4)
        self.assertEqual(absent, default_only)

    def test_comedy_category_from_default_survives_game_overlay(self):
        # 同名替换不是整文件替换:default 的其他 category 仍在
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write(root, "default", "categories:\n  comedy: {weight: 2, words: [笨蛋]}\n")
            self._write(root, "g1", "categories:\n  extra: {weight: 1, words: [追加]}\n")
            lexicon = load_reaction_lexicon("g1", base_dir=root)
        self.assertEqual(
            sorted(c.name for c in lexicon.categories), ["comedy", "extra"]
        )


class SignalTriggerTest(unittest.TestCase):
    def test_choice_signal_keys_on_cut_reason_not_text(self):
        lines = [("雪鹰", "到底怎么办。"), ("雪鹰", "选哪个呢。")]
        plain = score_beat(_beat(lines, cut_reason="idle_flush"), _LEXICON)
        choice = score_beat(_beat(lines, cut_reason="choice"), _LEXICON)
        self.assertEqual(plain.score, 0)  # 文本提到“选哪个”不加分(非正则)
        self.assertEqual(choice.score, 3)  # 状态信号加分

    def test_exclamation_density_needs_two_marks(self):
        one = _beat([("A", "什么！"), ("B", "嗯。")])
        two = _beat([("A", "什么！"), ("B", "不会吧！")])
        self.assertEqual(score_beat(one, _LEXICON).score, 0)
        self.assertEqual(
            score_beat(two, _LEXICON).reasons, ("signal:exclamation_density",)
        )

    def test_speaker_swarm_needs_three_named_speakers(self):
        two = _beat([("A", "嗯。"), ("B", "嗯。"), (None, "旁白。")])
        three = _beat([("A", "嗯。"), ("B", "嗯。"), ("C", "嗯。")])
        self.assertEqual(score_beat(two, _LEXICON).score, 0)
        self.assertEqual(score_beat(three, _LEXICON).reasons, ("signal:speaker_swarm",))


class EngineSeamTest(unittest.TestCase):
    def test_score_beat_plugs_into_the_engine_scorer_seam(self):
        spoken = []
        engine = ReactionEngine(
            speak=lambda beat, score: spoken.append((beat.cut_reason, score)) or True,
            scorer=lambda beat: score_beat(beat, _LEXICON),
        )
        engine.handle_event(
            GalgameStatusChangedEvent(state=GalgameState.PLAYING.value), now=0.0
        )
        for t, (speaker, text) in enumerate([
            ("月岛", "其实我一直骗着你。"),
            ("雪鹰", "什么！你说什么！"),
            ("月岛", "对不起…我没办法说出真相！"),
        ]):
            engine.handle_event(
                GalgameStableLineCommittedEvent(line_id=f"s{t}", speaker=speaker, text=text),
                now=float(t),
            )
        self.assertEqual(spoken, [("strong_punct", 8)])  # normal 阈值 4 -> 开口


if __name__ == "__main__":
    unittest.main()

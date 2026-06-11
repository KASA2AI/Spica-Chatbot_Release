"""Phase 7: stable-line tracker golden frames (§10.3) + speaker resolution."""

import unittest

from spica.galgame.text_stream import StableLineTracker, StableOutcome, resolve_speaker


class StableLineGoldenTest(unittest.TestCase):
    def _track(self, frames, **kw):
        tracker = StableLineTracker(**kw)
        return [tracker.feed(f) for f in frames]

    def test_two_identical_then_stable(self):
        self.assertEqual(
            self._track(["こんにちは", "こんにちは"]),
            [StableOutcome.PENDING, StableOutcome.NEW_STABLE],
        )

    def test_one_char_jitter_counts_as_same_line(self):
        a = "今日はいい天気ですね散歩でもしましょうか"
        b = "今日はいい天気ですね散歩でもしまレょうか"  # single-char OCR jitter
        self.assertEqual(self._track([a, b])[-1], StableOutcome.NEW_STABLE)  # merged -> stable

    def test_typewriter_takes_final_complete_sentence(self):
        # growing half-sentences keep changing -> never stable until the full text settles
        self.assertEqual(
            self._track(["こ", "こんに", "こんにちは", "こんにちは"]),
            [StableOutcome.PENDING, StableOutcome.PENDING, StableOutcome.PENDING, StableOutcome.NEW_STABLE],
        )

    def test_change_commits_previous_and_same_is_not_reemitted(self):
        self.assertEqual(
            self._track(["セリフA", "セリフA", "セリフA", "セリフB", "セリフB"]),
            [
                StableOutcome.PENDING,
                StableOutcome.NEW_STABLE,  # A settles
                StableOutcome.SAME,        # A still on screen -> not re-emitted
                StableOutcome.PENDING,
                StableOutcome.NEW_STABLE,  # B settles (A should commit, owned by the session)
            ],
        )

    def test_blank_frame_is_empty(self):
        self.assertEqual(StableLineTracker().feed("   "), StableOutcome.EMPTY)


class ResolveSpeakerTest(unittest.TestCase):
    def test_region_strategy(self):
        self.assertEqual(resolve_speaker("region", "麦", "おはよう"), ("麦", "おはよう"))
        self.assertEqual(resolve_speaker("region", "", "旁白です"), (None, "旁白です"))

    def test_parse_from_text(self):
        self.assertEqual(resolve_speaker("parse_from_text", None, "麦「おはよう」"), ("麦", "おはよう"))
        self.assertEqual(resolve_speaker("parse_from_text", None, "地の文"), (None, "地の文"))

    def test_narration_or_unknown(self):
        self.assertEqual(resolve_speaker("narration_or_unknown", None, "風が吹いた"), (None, "風が吹いた"))


class BracketSpeakerGoldenTest(unittest.TestCase):
    """Real LimeLight OCR lines, verbatim from the 1118-line spica_data capture
    (2026-06-11). The bracket format 【名前】 plus the four OCR noise shapes the
    capture actually produced: lost closing 】, closing quote misread as 』 ] 1,
    lost closing quote entirely."""

    def test_complete_bracket_line(self):
        self.assertEqual(
            resolve_speaker(
                "parse_from_text", None,
                "【雪鹰】 「莉莉子在二楼房间整理浴衣， 不好意思能帮我去看看情况吗？」"),
            ("雪鹰", "莉莉子在二楼房间整理浴衣， 不好意思能帮我去看看情况吗？"))

    def test_lost_closing_bracket_still_cuts_name(self):
        # 18/721 real lines lost the 】 -- the name must still cut clean.
        self.assertEqual(
            resolve_speaker("parse_from_text", None, "【月望 「可是， 我也想买各种小吃····"),
            ("月望", "可是， 我也想买各种小吃····"))

    def test_closing_quote_misreads(self):
        self.assertEqual(
            resolve_speaker("parse_from_text", None, "【雪鹰】 「人比想象中要多啊』"),
            ("雪鹰", "人比想象中要多啊"))
        self.assertEqual(
            resolve_speaker("parse_from_text", None, "【莉莉子】 「夏天的感觉啊～]"),
            ("莉莉子", "夏天的感觉啊～"))
        self.assertEqual(
            resolve_speaker(
                "parse_from_text", None,
                "【雪鹰】 「我才不会对你想入非非。 比起这个你赶紧去做防护措施1"),
            ("雪鹰", "我才不会对你想入非非。 比起这个你赶紧去做防护措施"))

    def test_lost_closing_quote_keeps_dialogue(self):
        self.assertEqual(
            resolve_speaker("parse_from_text", None, "【莉莉子】 『嗯～··· 这倒也是呢～···"),
            ("莉莉子", "嗯～··· 这倒也是呢～···"))

    def test_narration_is_untouched(self):
        self.assertEqual(
            resolve_speaker("parse_from_text", None, "爆裂声响彻，五彩斑斓的光辉绽放。"),
            (None, "爆裂声响彻，五彩斑斓的光辉绽放。"))

    def test_lost_opening_bracket_strips_orphan_closer(self):
        # 50/1118 real lines lost the 【 instead -- the prefix path catches them
        # and the orphan 】 must not stay in the name.
        self.assertEqual(
            resolve_speaker("parse_from_text", None, "雪鹰】 「难度好高啊」"),
            ("雪鹰", "难度好高啊"))

    def test_lost_bracket_with_merged_text_bounds_name_at_space(self):
        line = "【大梦 因为整理使用记录时需要用到。 就算现在也行，要让她在网站上提交申请。明白吗？」"
        speaker, _ = resolve_speaker("parse_from_text", None, line)
        self.assertEqual(speaker, "大梦")

    def test_narration_embedding_a_quote_mints_no_speaker(self):
        # The closing quote sits MID-line -> narration quoting a word, not dialogue.
        line = "这家伙说的『注意』到底是指什么"
        self.assertEqual(resolve_speaker("parse_from_text", None, line), (None, line))

    def test_long_narration_with_colon_mints_no_speaker(self):
        line = "隐同学走向新进店的顾客： 去为他们点单。"
        self.assertEqual(resolve_speaker("parse_from_text", None, line), (None, line))

    def test_clock_readout_mints_no_speaker(self):
        self.assertEqual(
            resolve_speaker("parse_from_text", None, "10: 12。"), (None, "10: 12。"))

    def test_trailing_digit_without_quote_never_eaten(self):
        # The dirty-closer strip only applies after an opening quote.
        self.assertEqual(
            resolve_speaker("parse_from_text", None, "倒计时还剩1"),
            (None, "倒计时还剩1"))

    def test_name_only_typewriter_frame_resolves_empty(self):
        # A frame caught before the dialogue renders: empty text -> the tracker
        # reports EMPTY and nothing is written.
        self.assertEqual(
            resolve_speaker("parse_from_text", None, "【雪鹰】「"), ("雪鹰", ""))


class RegionFallbackTest(unittest.TestCase):
    """region strategy with an empty region result falls back to text parsing --
    the LimeLight lesion (strategy='region', speaker_name_region=None shipped in
    the persisted profile, so the speaker was NEVER resolved: 0/1118 lines)."""

    def test_empty_region_falls_back_to_bracket_parse(self):
        self.assertEqual(
            resolve_speaker("region", None, "【卫哉】 「办专场Live之类的？"),
            ("卫哉", "办专场Live之类的？"))

    def test_region_result_wins_and_text_is_untouched(self):
        # A real calibrated region keeps full authority; the dialogue text is
        # passed through as-is (region games carry no bracket markup).
        self.assertEqual(
            resolve_speaker("region", "麦", "「おはよう」"), ("麦", "「おはよう」"))

    def test_empty_region_with_narration_stays_none(self):
        self.assertEqual(
            resolve_speaker("region", "", "爆裂声响彻，五彩斑斓的光辉绽放。"),
            (None, "爆裂声响彻，五彩斑斓的光辉绽放。"))


if __name__ == "__main__":
    unittest.main()

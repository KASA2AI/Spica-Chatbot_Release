"""P5 step 1 pins: the reaction engine skeleton.

① golden decision sequence over a scripted event feed (deterministic trail);
② the SIX cut signals, one pin each (idle flush driven by a fake clock);
③ the speak-state gate: a CHOICE_CHECKING beat holds, speaks on return to
   PLAYING while fresh, drops when stale (D-P5-8);
④ dedupe hash incl. OCR-noise normalization (fullwidth/whitespace variants);
⑤ budget: cooldown / window cap / busy_drop consumes nothing (D-P5-2);
⑥ the D-P5-0 red line: enqueue_event is pure put_nowait (zero work in the sink
   call stack) and the worker thread does the speaking.

The synchronous core (handle_event/handle_idle + explicit now) is the designed
test surface -- no threads, no sleeps, fake clock; only ⑥(b) starts the real
worker once as a smoke test.
"""

import threading
import unittest

from spica.core.companion_events import (
    GalgameChoiceDetectedEvent,
    GalgameStableLineCommittedEvent,
    GalgameStatusChangedEvent,
)
from spica.galgame.reaction import (
    ReactionEngine,
    ReactionModeParams,
    ScoreResult,
)
from spica.galgame.session import GalgameState

_S = GalgameState
_ids = iter(range(10_000))


def _line(text, speaker="少女"):
    return GalgameStableLineCommittedEvent(
        line_id=f"l{next(_ids)}", speaker=speaker, text=text
    )


def _status(state):
    return GalgameStatusChangedEvent(state=state.value, previous="")


def _choice(*texts):
    return GalgameChoiceDetectedEvent(
        choice_id="c1", options=[{"text": t} for t in texts]
    )


class _SpeakSpy:
    def __init__(self, result=True):
        self.calls = []
        self.result = result
        self.fired = threading.Event()

    def __call__(self, beat, score):
        self.calls.append((beat, score))
        self.fired.set()
        return self.result


def _engine(speak=None, *, score=None, params=None):
    spy = speak or _SpeakSpy()
    engine = ReactionEngine(
        speak=spy,
        params_provider=(lambda: params) if params else None,
        scorer=(lambda beat: ScoreResult(score, ("test",))) if score is not None else None,
    )
    return engine, spy


def _kinds(engine):
    return [d.kind for d in engine.decisions]


class GoldenSequenceTest(unittest.TestCase):
    def test_scripted_feed_produces_deterministic_decision_trail(self):
        engine, spy = _engine(score=10)  # normal params: min 4, cap 3, cooldown 90
        engine.handle_event(_status(_S.PLAYING), now=0.0)
        for t, text in ((0, "诶。"), (1, "等等。"), (2, "你是谁！")):
            engine.handle_event(_line(text), now=float(t))
        for t, text in ((10, "啊。"), (11, "怎么会。"), (12, "骗人吧！")):
            engine.handle_event(_line(text), now=float(t))
        for t, text in ((100, "原来如此。"), (101, "是这样吗。"), (102, "太好了！")):
            engine.handle_event(_line(text), now=float(t))
        for t, text in ((200, "诶。"), (201, "等等。"), (202, "你是谁！")):  # A 重放
            engine.handle_event(_line(text), now=float(t))
        self.assertEqual(
            _kinds(engine), ["spoke", "cooldown_drop", "spoke", "dedupe_hash_drop"]
        )
        self.assertEqual(len(spy.calls), 2)
        self.assertEqual(spy.calls[0][0].cut_reason, "strong_punct")


class CutSignalsTest(unittest.TestCase):
    """② one pin per cut signal; default null scorer -> every cut surfaces as
    below_threshold with detail == cut_reason (cut logic isolated from speak)."""

    def _playing_engine(self, **kw):
        engine, spy = _engine(**kw)
        engine.handle_event(_status(_S.PLAYING), now=0.0)
        return engine, spy

    def test_strong_punct_cut_needs_three_lines(self):
        engine, _ = self._playing_engine()
        engine.handle_event(_line("第一句！"), now=0.0)  # ＜3 行: 强标点不切
        engine.handle_event(_line("第二句。"), now=1.0)
        self.assertEqual(engine.decisions, type(engine.decisions)([], maxlen=200))
        engine.handle_event(_line("第三句！"), now=2.0)
        self.assertEqual(_kinds(engine), ["below_threshold"])
        self.assertEqual(engine.decisions[0].detail, "strong_punct")

    def test_choice_cut_holds_with_options(self):
        engine, _ = self._playing_engine(score=10)
        engine.handle_event(_line("到底选哪边。"), now=0.0)
        engine.handle_event(_line("我得想想。"), now=1.0)
        engine.handle_event(_status(_S.CHOICE_CHECKING), now=2.0)
        engine.handle_event(_choice("去屋顶", "回教室"), now=2.0)
        self.assertEqual(_kinds(engine), ["speak_hold"])
        self.assertEqual(engine.decisions[0].detail, "choice")

    def test_max_lines_forces_cut_at_eight(self):
        engine, _ = self._playing_engine()
        for i in range(8):
            engine.handle_event(_line(f"平淡的第{i}句。"), now=float(i))
        self.assertEqual(_kinds(engine), ["below_threshold"])
        self.assertEqual(engine.decisions[0].detail, "max_lines")
        self.assertEqual(len(engine.decisions[0].line_ids), 8)

    def test_idle_flush_fires_after_eight_seconds_fake_clock(self):
        engine, _ = self._playing_engine()
        engine.handle_event(_line("然后呢。"), now=0.0)
        engine.handle_event(_line("说下去啊。"), now=1.0)
        engine.handle_idle(now=5.0)  # deadline = 1+8 = 9: not yet
        self.assertEqual(_kinds(engine), [])
        engine.handle_idle(now=9.5)
        self.assertEqual(_kinds(engine), ["below_threshold"])
        self.assertEqual(engine.decisions[0].detail, "idle_flush")
        engine.handle_idle(now=20.0)  # empty buffer: no-op
        self.assertEqual(len(engine.decisions), 1)

    def test_speaker_switch_is_soft_boundary_never_cuts(self):
        engine, _ = self._playing_engine()
        for i in range(7):  # A/B 来回对话,无强标点 -> 不碎成单行 beat
            engine.handle_event(_line(f"第{i}句。", speaker="A" if i % 2 else "B"), now=float(i))
        self.assertEqual(_kinds(engine), [])
        engine.handle_event(_line("第八句。", speaker="A"), now=8.0)
        self.assertEqual(_kinds(engine), ["below_threshold"])
        self.assertEqual(engine.decisions[0].detail, "max_lines")

    def test_leaving_observe_flushes_unscored_and_ignores_lines(self):
        engine, _ = self._playing_engine()
        engine.handle_event(_line("看到一半。"), now=0.0)
        engine.handle_event(_line("窗口要丢了。"), now=1.0)
        engine.handle_event(_status(_S.WINDOW_LOST), now=2.0)
        self.assertEqual(_kinds(engine), ["observe_flush"])
        engine.handle_event(_line("遮挡期间的行。"), now=3.0)  # 不缓冲
        engine.handle_event(_status(_S.PLAYING), now=4.0)
        for t, text in ((5, "新的一句。"), (6, "又一句。"), (7, "完结！")):
            engine.handle_event(_line(text), now=float(t))
        beat_ids = engine.decisions[-1].line_ids
        self.assertEqual(len(beat_ids), 3)  # 只含恢复后的三行,遮挡行/旧行未混入


class SpeakStateGateTest(unittest.TestCase):
    """③ CHOICE_CHECKING cuts but does not speak; the held beat speaks on the
    return to PLAYING while fresh and drops when stale."""

    def _held_engine(self, speak=None):
        engine, spy = _engine(speak, score=10)
        engine.handle_event(_status(_S.PLAYING), now=0.0)
        engine.handle_event(_line("命运的分歧点。"), now=0.0)
        engine.handle_event(_line("怎么选。"), now=1.0)
        engine.handle_event(_status(_S.CHOICE_CHECKING), now=2.0)
        engine.handle_event(_choice("去屋顶", "回教室"), now=2.0)
        return engine, spy

    def test_held_beat_speaks_on_fresh_return_to_playing(self):
        engine, spy = self._held_engine()
        self.assertEqual(_kinds(engine), ["speak_hold"])
        self.assertEqual(spy.calls, [])  # 选项分析期间她不抢话
        engine.handle_event(_status(_S.PLAYING), now=10.0)  # 8s 后回来,仍新鲜
        self.assertEqual(_kinds(engine), ["speak_hold", "spoke"])
        beat, score = spy.calls[0]
        self.assertEqual(beat.cut_reason, "choice")
        self.assertEqual(beat.choice_options, ("去屋顶", "回教室"))
        self.assertEqual(score, 10)

    def test_held_beat_drops_when_stale(self):
        engine, spy = self._held_engine()
        engine.handle_event(_status(_S.PLAYING), now=40.0)  # 38s > 30s 保鲜期
        self.assertEqual(_kinds(engine), ["speak_hold", "pending_dropped"])
        self.assertEqual(engine.decisions[-1].detail, "stale")
        self.assertEqual(spy.calls, [])


class DedupeHashTest(unittest.TestCase):
    def test_same_content_different_line_ids_and_noise_is_dropped(self):
        engine, spy = _engine(score=10)
        engine.handle_event(_status(_S.PLAYING), now=0.0)
        for t, text in ((0, "其实我一直骗着你。"), (1, "诶。"), (2, "什么！")):
            engine.handle_event(_line(text), now=float(t))
        self.assertEqual(_kinds(engine), ["spoke"])
        # 同内容重放:新 line_id + 全半角/空白噪声(OCR 重读同段的真实形态)
        for t, text in ((200, "其实我 一直骗着你。"), (201, "诶 。"), (202, "什么!")):
            engine.handle_event(_line(text), now=float(t))
        self.assertEqual(_kinds(engine), ["spoke", "dedupe_hash_drop"])
        self.assertEqual(len(spy.calls), 1)


class BudgetTest(unittest.TestCase):
    _PARAMS = ReactionModeParams(min_score=0, max_per_window=2, cooldown_seconds=10.0)

    def _beat(self, engine, base_t, tag):
        for i, text in enumerate((f"{tag}铺垫。", f"{tag}推进。", f"{tag}爆点！")):
            engine.handle_event(_line(text), now=base_t + i)

    def test_cooldown_then_window_cap_then_window_expiry(self):
        engine, spy = _engine(score=10, params=self._PARAMS)
        engine.handle_event(_status(_S.PLAYING), now=0.0)
        self._beat(engine, 0.0, "一")     # t=2 spoke
        self._beat(engine, 5.0, "二")     # t=7, 距上次 5s < 10s -> cooldown
        self._beat(engine, 20.0, "三")    # t=22 spoke (窗口内第 2 条)
        self._beat(engine, 40.0, "四")    # t=42, 冷却已过但窗口满 2 -> cap
        self._beat(engine, 640.0, "五")   # t=642, t=2 的记录滑出 600s 窗 -> spoke
        self.assertEqual(
            _kinds(engine),
            ["spoke", "cooldown_drop", "spoke", "budget_capped_drop", "spoke"],
        )
        self.assertEqual(len(spy.calls), 3)

    def test_busy_drop_consumes_no_budget_and_no_cooldown(self):
        spy = _SpeakSpy(result=False)  # arbiter 恒忙
        engine, _ = _engine(spy, score=10, params=self._PARAMS)
        engine.handle_event(_status(_S.PLAYING), now=0.0)
        self._beat(engine, 0.0, "甲")
        self.assertEqual(_kinds(engine), ["busy_drop"])
        spy.result = True  # 下一个 beat 立刻来:未被 busy 记冷却/扣预算
        self._beat(engine, 3.0, "乙")
        self.assertEqual(_kinds(engine), ["busy_drop", "spoke"])


class SinkAsyncRedLineTest(unittest.TestCase):
    """⑥ D-P5-0: the sink-facing entry does NOTHING but enqueue."""

    def test_enqueue_returns_immediately_with_zero_work_in_stack(self):
        engine, spy = _engine(score=10)  # worker 未启动
        engine.enqueue_event(_status(_S.PLAYING))
        engine.enqueue_event(_line("劲爆台词！"))
        # sink 调用栈内:零 try_speak、零评分、零决策 -- 事件只进了队列
        self.assertEqual(spy.calls, [])
        self.assertEqual(len(engine.decisions), 0)
        self.assertEqual(engine._queue.qsize(), 2)

    def test_worker_thread_does_the_speaking(self):
        spy = _SpeakSpy()
        engine, _ = _engine(spy, score=10)
        engine.start()
        try:
            engine.enqueue_event(_status(_S.PLAYING))
            for text in ("铺垫。", "推进。", "爆点！"):
                engine.enqueue_event(_line(text))
            self.assertTrue(spy.fired.wait(timeout=2.0))  # worker 消费并开口
        finally:
            engine.stop()
        self.assertEqual(len(spy.calls), 1)
        self.assertEqual(spy.calls[0][0].cut_reason, "strong_punct")


if __name__ == "__main__":
    unittest.main()

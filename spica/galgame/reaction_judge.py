"""Galgame reaction judge (P5 v2, step 1): LLM "is this moment worth reacting to?".

A background TOOL call -- NOT run_turn (mirrors ``GalgameSummarizer`` §1.3): it
builds one judge prompt, calls the LLM endpoint once through
``LLMPort.complete_text``, and parses a structured verdict. It NEVER builds a
TurnContext / touches prompt_builder / run_turn (铁律 #3): the judge only
SELECTS (worth + moment); Spica's actual spoken reaction still goes through
run_turn unchanged.

This is the SELECTION half of the v2 reaction upgrade. The blind lexicon gate
(``reaction.score_beat``) is replaced -- through the SAME host
``_reaction_scorer`` seam -- by this judge, which reads a scene WINDOW (not just
the 8-line beat) so it no longer misses wordless drama (the LimeLight failure
mode: strong-keyword lexicons score 0 across natural dialogue).

DEGRADE CONTRACT (load-bearing): ``judge`` RAISES ``ReactionJudgeError`` on any
LLM/parse failure -- it never returns a silent "worth 0". The DECISION to degrade
(fall back to the lexicon ``score_beat``) lives in the HOST CLOSURE, not here,
because the reaction engine's worker loop swallows scorer exceptions into a
silent drop (reaction.py worker ``try/except``); catching in the closure is the
only place a failure turns into lexicon scoring rather than silence. A genuine
``worth: 0`` verdict is a SUCCESS (a confident "not worth it"), not a failure --
it must NOT trigger fallback, so only structural/LLM errors raise.

R18: same as the summarizer -- the dialogue endpoint is treated as R18-capable;
the judge prompt does NOT add refusal/avoidance.

Qt-free (铁律 #1).
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

from spica.ports.llm import LLMPort

logger = logging.getLogger(__name__)

_WORTH_MIN = 0
_WORTH_MAX = 10
_VALID_ANGLES = ("吐槽", "惊讶", "调侃", "感想")
_MOMENT_CHAR_CAP = 60


class ReactionJudgeError(RuntimeError):
    """LLM call failed or returned unparseable output. The host closure catches
    this and falls back to the lexicon scorer (不沉默不崩); never swallowed here.
    A valid ``worth: 0`` verdict is NOT an error and never raises this."""


@dataclass(frozen=True)
class JudgeVerdict:
    worth: int = 0  # 0-10 -> ScoreResult.score (the engine's min_score gates it)
    moment: str = ""  # the one beat worth reacting to (-> reasons; telemetry in step 1)
    angle: str = ""  # 吐槽|惊讶|调侃|感想 (-> reasons; NOT fed to the directive in step 1)


# The judge sees four layers (D: input layering). [前情] = arc (summaries +
# progress), [最近这段] = the recent scene window, [刚刚这一下] = the focus beat,
# [她最近说过] = Spica's recent reactions (so it does not repeat itself). Output
# is a strict JSON object (no Markdown), parsed tolerantly like the summarizer.
_PROMPT = """你在陪「麦」一起玩 galgame。判断【刚刚这一下】剧情值不值得 Spica 主动开口吐一句（吐槽/惊讶/调侃/感想），给 0-10 分。

判断标准（务必克制）：
- 绝大多数对白是日常推进（寒暄、走路、吃饭、客套、过场），不值得单独吐 —— 给 0-3 分。
- 只有真正有 张力 / 反转 / 揭露 / 情感冲击（告白、离别、哭、和解）/ 荒谬好笑 / 名场面 的地方才值得 —— 给 6-10 分。
- 关键：戏可能没有强烈字眼，全靠气氛、沉默、欲言又止、前后落差。这种"无词的戏"同样值得高分，别因为没出现"震惊""真相"这类词就给低分 —— 结合【最近这段】的铺垫判断。
- 如果和【她最近说过】里的点重复或太接近，压低分（别重复吐同一件事）。
- 宁可漏，不可滥。拿不准就给低分。

只输出一个 JSON 对象（不要 Markdown、不要解释），字段：
- worth: 0-10 的整数（值不值得 + 强度）
- moment: 一句话说明值得吐的是哪一下（≤30 字；worth 低时填空字符串）
- angle: 吐槽 / 惊讶 / 调侃 / 感想 其中之一（worth 低时填空字符串）

示例 1（日常推进）—— 【刚刚这一下】：
小春：今天天气不错呢。
主人公：是啊，要不要去买点东西。
输出：{{"worth": 2, "moment": "", "angle": ""}}

示例 2（有铺垫的情感冲击）—— 【最近这段】交代了两人多年的误会；【刚刚这一下】：
枫：……其实那年，我一直在等你回来。
输出：{{"worth": 8, "moment": "枫终于说出等了这么多年", "angle": "感想"}}

示例 3（无强烈字眼但气氛凝重）—— 【刚刚这一下】：
彩音：……
彩音：（转过身，没有再说话）
输出：{{"worth": 6, "moment": "彩音欲言又止、转身沉默的落差", "angle": "感想"}}

[前情]
{context}

[最近这段]
{window}

[刚刚这一下]
{beat}

[她最近说过]
{recent}
"""


class GalgameReactionJudge:
    """Clone of ``GalgameSummarizer`` shape: one ``complete_text`` call per
    verdict, same per-call ``model=`` override, JSON parsed tolerantly. Holds no
    state, no Qt, no run_turn coupling."""

    def __init__(self, llm: LLMPort, model: str) -> None:
        self._llm = llm
        self._model = model

    def judge(
        self,
        *,
        beat_lines: list[Any],
        window_lines: list[Any] | None = None,
        recent_summaries: list[Any] | None = None,
        progress: Any | None = None,
        recent_beats: list[Any] | None = None,
    ) -> JudgeVerdict:
        """Verdict for the focus beat given its scene window + arc context.

        Lines (beat/window) are duck-typed: any object with ``.speaker`` /
        ``.text`` works (BeatLine, StoryLine, fakes) -- same as the summarizer.
        Raises ``ReactionJudgeError`` on empty input / LLM error / unparseable
        output. A valid ``worth: 0`` is returned normally (a confident "no")."""
        if not beat_lines:
            raise ReactionJudgeError("no beat lines to judge")
        prompt = _PROMPT.format(
            context=_format_context(recent_summaries or [], progress),
            window=_format_lines(window_lines or []) or "（无）",
            beat=_format_lines(beat_lines) or "（无）",
            recent=_format_recent_beats(recent_beats or []) or "（无）",
        )
        try:
            raw = self._llm.complete_text(prompt, model=self._model)
        except Exception as exc:  # noqa: BLE001 -- any LLM error -> closure degrades to lexicon
            raise ReactionJudgeError(f"LLM call failed: {exc}") from exc
        return _parse(raw)


def _format_lines(lines: list[Any]) -> str:
    return "\n".join(
        f"{getattr(line, 'speaker', None) or '—'}: {getattr(line, 'text', '') or ''}"
        for line in lines
    )


def _format_recent_beats(beats: list[Any]) -> str:
    out: list[str] = []
    for beat in beats:
        content = (getattr(beat, "content", "") or "").strip()
        if content:
            out.append(f"- {content}")
    return "\n".join(out)


def _format_context(recent_summaries: list[Any], progress: Any) -> str:
    """Clone of summarizer._format_context: route/chapter line + up to 2 前情
    summaries. Empty -> （无）, so the prompt slot is never blank."""
    parts: list[str] = []
    if progress is not None:
        route = getattr(progress, "route", None) or {}
        if route.get("name"):
            confirmed = "已确认" if route.get("confirmed") else f"推测(置信度{route.get('confidence', 0)})"
            parts.append(f"当前线路：{route.get('name')}（{confirmed}）")
        chapter = getattr(progress, "chapter", None) or {}
        if chapter.get("title"):
            parts.append(f"当前章节：{chapter.get('title')}")
    for summary in recent_summaries[:2]:
        text = getattr(summary, "summary_zh", "") or ""
        if text:
            parts.append(f"前情：{text}")
    return "\n".join(parts) or "（无）"


def _parse(raw: str) -> JudgeVerdict:
    data = _extract_json(raw)
    if not isinstance(data, dict) or "worth" not in data:
        raise ReactionJudgeError(f"unparseable judge output: {(raw or '')[:200]!r}")
    worth = _clamp_worth(data.get("worth"))
    if worth is None:
        raise ReactionJudgeError(f"judge output has non-numeric worth: {(raw or '')[:200]!r}")
    angle = str(data.get("angle") or "")
    if angle not in _VALID_ANGLES:
        angle = ""
    return JudgeVerdict(worth=worth, moment=str(data.get("moment") or "")[:_MOMENT_CHAR_CAP], angle=angle)


def _extract_json(raw: str) -> Any:
    match = re.search(r"\{.*\}", (raw or "").strip(), re.DOTALL)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return None


def _clamp_worth(value: Any) -> int | None:
    """0-10 int, tolerant of floats/strings. None -> non-numeric (a parse
    failure, distinct from a real 0)."""
    try:
        worth = int(round(float(value)))
    except (TypeError, ValueError):
        return None
    return max(_WORTH_MIN, min(_WORTH_MAX, worth))

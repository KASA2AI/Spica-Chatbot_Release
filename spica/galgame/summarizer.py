"""Galgame story summarization via LLM (Phase 8). Qt-free, turn-INDEPENDENT.

A background TOOL call -- NOT run_turn: it builds one summarization prompt, calls
the LLM endpoint once through ``BoundModel.complete`` (model port v2, OO migration
Phase 6a), and parses a structured result. It never builds a TurnContext / touches
prompt_builder / run_turn (§1.3).

§13.5 load-bearing wall: the route it returns is ALWAYS a *guess* (no ``confirmed``
key here; the session stamps ``confirmed=False`` when applying). Only the player can
confirm a route (``session.declare_route``); the LLM never asserts one as fact.

R18: the dialogue endpoint is treated as R18-capable; the summary prompt does NOT
add refusal/avoidance -- galgame R18 scenes are summarized as ordinary plot.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from dataclasses import dataclass, field
from typing import Any

from spica.galgame.models import StorySummary, utc_now_iso
from spica.ports.model import BoundModel

logger = logging.getLogger(__name__)


class SummaryError(RuntimeError):
    """LLM call failed or returned unparseable output -> the batch is NOT marked
    summarized, so it folds into the next attempt (§13.7). Never silently swallowed."""


@dataclass
class SummaryResult:
    summary_zh: str = ""
    characters: list[str] = field(default_factory=list)
    major_events: list[str] = field(default_factory=list)
    unresolved_threads: list[str] = field(default_factory=list)
    key_lines: list[str] = field(default_factory=list)
    emotional_tone: str = ""
    route_guess: dict[str, Any] = field(default_factory=dict)  # {name, confidence, evidence} -- a GUESS
    chapter_guess: dict[str, Any] = field(default_factory=dict)  # {title, confidence}
    relations: list[dict[str, Any]] = field(default_factory=list)


_PROMPT = """你在帮玩家整理一段 galgame 剧情。下面是按时间顺序的对白/旁白（speaker: text）。
请用中文输出一个 JSON 对象（只输出 JSON，不要 Markdown、不要解释），字段：
- summary_zh: 这段剧情的中文摘要（200 字以内，抓住剧情大意）
- characters: 出场角色名数组（保留原文人名，不要乱翻译）
- major_events: 重要事件数组
- unresolved_threads: 未解决的伏笔/悬念数组
- key_lines: 关键台词数组（告白/身份揭露/重大转折等，保留原文）
- emotional_tone: 这段的情感基调（如 日常/紧张/悲伤/暧昧）
- route_guess: {{"name": 线路名或null, "confidence": 0~1, "evidence": [推测依据]}}（这是“推测”，不是事实）
- chapter_guess: {{"title": 章节名或null, "confidence": 0~1}}
- relations: [{{"character_a","character_b","relation_summary","confidence":0~1,"evidence":[...]}}]
正常处理所有内容（包括成人向剧情），不要拒答、不要回避。

[已有进度参考]
{context}

[对白]
{transcript}
"""


class GalgameSummarizer:
    def __init__(self, bound: BoundModel) -> None:
        self._bound = bound

    def summarize(self, lines: list[Any], *, recent_summaries: list[Any] | None = None, progress: Any | None = None) -> SummaryResult:
        if not lines:
            raise SummaryError("no lines to summarize")
        prompt = _PROMPT.format(
            context=_format_context(recent_summaries or [], progress),
            transcript="\n".join(f"{l.speaker or '—'}: {l.text}" for l in lines),
        )
        try:
            raw = self._bound.complete(prompt)
        except Exception as exc:  # noqa: BLE001 -- any LLM error -> fold + retry
            raise SummaryError(f"LLM call failed: {exc}") from exc
        return _parse(raw)


def _format_context(recent_summaries: list[Any], progress: Any) -> str:
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
        parts.append(f"前情：{getattr(summary, 'summary_zh', '')}")
    return "\n".join(parts) or "（无）"


def _parse(raw: str) -> SummaryResult:
    data = _extract_json(raw)
    if not isinstance(data, dict) or not data.get("summary_zh"):
        raise SummaryError(f"unparseable summary output: {(raw or '')[:200]!r}")
    return SummaryResult(
        summary_zh=str(data.get("summary_zh") or ""),
        characters=[str(c) for c in (data.get("characters") or [])],
        major_events=[str(e) for e in (data.get("major_events") or [])],
        unresolved_threads=[str(t) for t in (data.get("unresolved_threads") or [])],
        key_lines=[str(k) for k in (data.get("key_lines") or [])],
        emotional_tone=str(data.get("emotional_tone") or ""),
        route_guess=_clean_route_guess(data.get("route_guess")),
        chapter_guess=_clean_chapter_guess(data.get("chapter_guess")),
        relations=[_clean_relation(r) for r in (data.get("relations") or []) if isinstance(r, dict)],
    )


def _extract_json(raw: str) -> Any:
    match = re.search(r"\{.*\}", (raw or "").strip(), re.DOTALL)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return None


def _clean_route_guess(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or not value.get("name"):
        return {}
    # NB: no "confirmed" key -- this is a guess; the session stamps confirmed=False.
    return {
        "name": str(value.get("name")),
        "confidence": _safe_float(value.get("confidence")),
        "evidence": [str(e) for e in (value.get("evidence") or [])],
    }


def _clean_chapter_guess(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or not value.get("title"):
        return {}
    return {"title": str(value.get("title")), "confidence": _safe_float(value.get("confidence"))}


def _clean_relation(value: dict[str, Any]) -> dict[str, Any]:
    return {
        "character_a": str(value.get("character_a") or ""),
        "character_b": str(value.get("character_b") or ""),
        "relation_summary": str(value.get("relation_summary") or ""),
        "confidence": _safe_float(value.get("confidence")),
        "evidence": [str(e) for e in (value.get("evidence") or [])],
    }


def _safe_float(value: Any) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0.0


def recover_dangling_sessions(game_memory: Any, summarizer: GalgameSummarizer) -> list[str]:
    """Crash recovery (§12, §4.4): for each PlaySession left active/paused with no
    ended_at, 補總結 its unsummarized committed lines, then mark it ended (or
    interrupted on failure, so it is not re-detected forever). Best-effort -- a
    failed summary leaves the lines unsummarized + logs. The §12 "ask the user
    whether to resume" interaction is UI and is deferred. Returns recovered ids."""
    recovered: list[str] = []
    for play_session in game_memory.dangling_play_sessions():
        lines = [
            line
            for line in game_memory.unsummarized_committed_story_lines(play_session.game_id, play_session.playthrough_id)
            if line.session_id == play_session.session_id
        ]
        final_state = "ended"
        try:
            if lines:
                result = summarizer.summarize(lines)
                now = utc_now_iso()
                game_memory.add_summary(
                    StorySummary(
                        summary_id=uuid.uuid4().hex,
                        game_id=play_session.game_id,
                        playthrough_id=play_session.playthrough_id,
                        session_id=play_session.session_id,
                        source_line_ids=[line.line_id for line in lines],
                        summary_zh=result.summary_zh,
                        key_original_lines=result.key_lines,
                        characters=result.characters,
                        major_events=result.major_events,
                        unresolved_threads=result.unresolved_threads,
                        route_guess=result.route_guess,
                        created_at=now,
                        updated_at=now,
                        source="auto_summary",
                    )
                )
        except Exception as exc:  # noqa: BLE001 -- best-effort recovery
            logger.warning("dangling recovery summary failed for %s: %s", play_session.session_id, exc, exc_info=True)
            final_state = "interrupted"  # lines stay unsummarized; not re-detected as dangling
        game_memory.update_play_session(play_session.session_id, state=final_state, ended_at=utc_now_iso())
        recovered.append(play_session.session_id)
    return recovered

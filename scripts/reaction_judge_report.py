"""P5 v2 step 1: OFFLINE judge-quality report over a REAL OCR corpus.

The safety valve before wiring the LLM judge into production (mirrors the lexicon
calibration in scripts/reaction_score_report.py, but the scorer is the LLM
``GalgameReactionJudge`` instead of ``score_beat``). It replays the real-machine
committed story lines (spica_data/galgame.sqlite3) through the SAME BeatAggregator
production uses, and for each unique beat asks the judge "worth reacting? +
moment/angle", reading a scene WINDOW (the tail N committed lines up to the beat)
so the judge sees the cross-beat run-up the lexicon gate is blind to.

It prints, so the judge's selection quality is verified BEFORE it touches the
production closure:
  - worth distribution histogram + threshold pass-rates (~/h),
  - judge vs lexicon overlap: JUDGE-ONLY beats (the wordless drama the lexicon
    missed) and LEXICON-ONLY beats (false positives the judge drops),
  - the actually-picked beats with their original lines + moment/angle,
  - cost / latency (chars, rough tokens, per-call latency percentiles).

Fidelity notes (honest, so the report is not over-read):
  - The window is reconstructed from the REPLAY (tail N lines up to each beat),
    not a live DB read, so no future lines leak in.
  - recent_summaries / progress / recent_beats are passed EMPTY here, on purpose:
    DB summaries cover the whole history (future spoilers) and Spica's recent
    reactions don't exist in a replay. Production additionally injects PAST-only
    summaries, so this report is a CONSERVATIVE lower bound on the judge's context
    (if it picks well with only the line window, it does at least as well live).

This makes REAL LLM calls (network + cost + ~1-2s each). Use --limit for a cheap
dry run first. Entry order honors 铁律 #10: load_secrets() before building the client.

Usage:
    python scripts/reaction_judge_report.py --limit 8          # cheap dry run first
    python scripts/reaction_judge_report.py                    # full corpus
    python scripts/reaction_judge_report.py --model deepseek-chat --window 24 --show-threshold 6
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from spica.core.companion_events import (  # noqa: E402
    GalgameStableLineCommittedEvent,
    GalgameStatusChangedEvent,
)
from spica.galgame.reaction import (  # noqa: E402
    REACTION_MODE_TABLE,
    BeatLine,
    ReactionBeat,
    ReactionEngine,
    ReactionModeParams,
    ScoreResult,
    load_reaction_lexicon,
    score_beat,
)
from spica.galgame.reaction_judge import GalgameReactionJudge, ReactionJudgeError  # noqa: E402
from spica.galgame.session import GalgameState  # noqa: E402

# Permissive so every unique beat reaches the scorer (dedupe still applies, as in
# production); budget/cooldown never gate the report.
_PERMISSIVE = ReactionModeParams(min_score=0, max_per_window=10**9, cooldown_seconds=0.0)
_WORTH_BUCKETS = (("0-2", 0, 2), ("3-5", 3, 5), ("6-7", 6, 7), ("8-10", 8, 10))
_WORTH_THRESHOLDS = (4, 5, 6, 7, 8)


@dataclass
class _Rec:
    worth: int  # -1 == judge error (degraded to lexicon in production)
    moment: str
    angle: str
    lexicon_score: int
    beat: ReactionBeat
    latency_ms: float
    prompt_chars: int
    out_chars: int
    error: str = ""


class _MeasuringLLM:
    """Wraps an LLMPort to capture prompt/output sizes for the cost estimate. The
    judge only ever calls ``complete_text``, so that is all this needs to forward."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def complete_text(self, prompt: str, *, model: str) -> str:
        out = self._inner.complete_text(prompt, model=model)
        _MeasuringLLM.last_prompt_chars = len(prompt or "")
        _MeasuringLLM.last_out_chars = len(out or "")
        return out

    last_prompt_chars: int = 0
    last_out_chars: int = 0


def _load_lines(db: Path, game_id: str) -> list[tuple[float, str | None, str]]:
    conn = sqlite3.connect(db)
    rows = conn.execute(
        "SELECT data FROM story_lines WHERE game_id=? AND status='committed' ORDER BY timestamp",
        (game_id,),
    ).fetchall()
    conn.close()
    out: list[tuple[float, str | None, str]] = []
    for (blob,) in rows:
        data = json.loads(blob)
        ts = datetime.fromisoformat(data["timestamp"]).timestamp()
        out.append((ts, data.get("speaker"), data.get("text") or ""))
    return out


def _build_judge(model_override: str | None) -> tuple[GalgameReactionJudge, str]:
    """Standalone LLMPort, built the same way build_agent_services does. 铁律 #10:
    load_secrets() first so the client gets the key/base_url, not empty values."""
    import httpx
    from openai import OpenAI

    from spica.adapters.llm.openai_compatible import OpenAICompatibleAdapter
    from spica.config.manager import ConfigManager
    from spica.config.secrets import load_secrets

    secrets = load_secrets()
    if not secrets.openai_api_key:
        raise SystemExit("没有读取到 OPENAI_API_KEY，请检查 xiaosan.env（铁律 #10）")
    config = ConfigManager().load()
    model = model_override or config.galgame.summary_model or config.llm.model
    client = OpenAI(
        api_key=secrets.openai_api_key,
        base_url=config.llm.base_url,
        http_client=httpx.Client(trust_env=False, timeout=60),
    )
    adapter = OpenAICompatibleAdapter(client)
    return GalgameReactionJudge(_MeasuringLLM(adapter), model), model


def _print_beat(beat: ReactionBeat, indent: str = "      ") -> None:
    for line in beat.lines:
        speaker = f"{line.speaker}：" if line.speaker else ""
        print(f"{indent}{speaker}{line.text}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--game", default="limelight")
    parser.add_argument("--db", default=str(REPO_ROOT / "spica_data" / "galgame.sqlite3"))
    parser.add_argument("--model", default=None, help="judge 模型；默认 summary_model 或 llm.model")
    parser.add_argument("--window", type=int, default=24, help="场景窗口尾部行数")
    parser.add_argument("--limit", type=int, default=0, help="只判前 N 个 beat（0=全部，先小跑省钱）")
    parser.add_argument("--show-threshold", type=int, default=6, help="worth>=此值视为'会吐'，打印原文")
    parser.add_argument("--lexicon-threshold", type=int, default=3, help="词表对照阈（默认 3=high）")
    args = parser.parse_args()

    lines = _load_lines(Path(args.db), args.game)
    if not lines:
        print(f"no committed lines for game_id={args.game!r} in {args.db}")
        return 1
    lexicon = load_reaction_lexicon(args.game)
    judge, model = _build_judge(args.model)
    print(
        f"corpus: {len(lines)} committed lines (game={args.game}); judge model={model}; "
        f"window={args.window}; limit={args.limit or 'all'}"
    )
    print(f"thresholds: judge worth>={args.show_threshold} (会吐); lexicon>={args.lexicon_threshold}\n")

    history: list[BeatLine] = []
    records: list[_Rec] = []
    judged = 0

    def _scorer(beat: ReactionBeat) -> ScoreResult:
        nonlocal judged
        if args.limit and judged >= args.limit:
            return ScoreResult(0, ())  # stop spending once the dry-run cap is hit
        judged += 1
        window = list(history[-args.window:]) if args.window > 0 else []
        lex = score_beat(beat, lexicon).score
        t0 = time.monotonic()
        try:
            verdict = judge.judge(
                beat_lines=list(beat.lines),
                window_lines=window,
                recent_summaries=[],
                progress=None,
                recent_beats=[],
            )
        except ReactionJudgeError as exc:
            records.append(
                _Rec(-1, "", "", lex, beat, (time.monotonic() - t0) * 1000.0,
                     _MeasuringLLM.last_prompt_chars, _MeasuringLLM.last_out_chars, str(exc))
            )
            return ScoreResult(0, ("judge_error",))
        dt = (time.monotonic() - t0) * 1000.0
        records.append(
            _Rec(verdict.worth, verdict.moment, verdict.angle, lex, beat, dt,
                 _MeasuringLLM.last_prompt_chars, _MeasuringLLM.last_out_chars)
        )
        if judged % 10 == 0:
            print(f"  ...judged {judged} beats", file=sys.stderr)
        return ScoreResult(verdict.worth, ())

    engine = ReactionEngine(
        speak=lambda beat, score: True,
        params_provider=lambda: _PERMISSIVE,
        scorer=_scorer,
    )
    engine.handle_event(GalgameStatusChangedEvent(state=GalgameState.PLAYING.value), now=lines[0][0])
    for index, (ts, speaker, text) in enumerate(lines):
        engine.handle_idle(now=ts)  # idle_flush cut sees history WITHOUT the current line
        history.append(BeatLine(speaker, text, f"r{index}"))  # now visible to the scorer
        engine.handle_event(
            GalgameStableLineCommittedEvent(line_id=f"r{index}", speaker=speaker, text=text), now=ts
        )
    engine.handle_idle(now=lines[-1][0] + 9.0)

    ok = [r for r in records if r.worth >= 0]
    errs = [r for r in records if r.worth < 0]
    total = len(ok)
    if not total:
        print("no beats judged (all errored or corpus empty)")
        if errs:
            print(f"judge errors: {len(errs)} (first: {errs[0].error})")
        return 1

    worths = sorted(r.worth for r in ok)
    print(f"\nbeats judged: {total} (judge errors -> lexicon fallback in prod: {len(errs)})")

    print("\nworth histogram:")
    for label, lo, hi in _WORTH_BUCKETS:
        count = sum(1 for w in worths if lo <= w <= hi)
        bar = "#" * round(50 * count / total)
        print(f"  {label:>5}: {count:5d} ({100 * count / total:5.1f}%) {bar}")

    def _pct(p: float) -> int:
        return worths[min(total - 1, int(p * total))]

    print(f"\npercentiles: p50={_pct(0.50)} p75={_pct(0.75)} p90={_pct(0.90)} max={worths[-1]}")

    elapsed_active = sum(min(b[0] - a[0], 120.0) for a, b in zip(lines, lines[1:]))
    hours = max(elapsed_active / 3600.0, 1e-9)
    print(f"\nactive play time (gap-capped): {elapsed_active / 60:.0f} min")
    print("judge worth pass-rates (before budget/cooldown -- the budget caps these):")
    for threshold in _WORTH_THRESHOLDS:
        passing = sum(1 for w in worths if w >= threshold)
        print(f"  worth>={threshold}: {passing:4d} beats ({100 * passing / total:5.1f}%), ~{passing / hours:.1f}/h raw")

    # judge vs lexicon: the whole point -- does the judge catch wordless drama the
    # lexicon missed, and drop the false positives the lexicon passed?
    jt, lt = args.show_threshold, args.lexicon_threshold
    judge_pass = [r for r in ok if r.worth >= jt]
    lex_pass = [r for r in ok if r.lexicon_score >= lt]
    judge_only = [r for r in judge_pass if r.lexicon_score < lt]
    lex_only = [r for r in lex_pass if r.worth < jt]
    overlap = [r for r in judge_pass if r.lexicon_score >= lt]
    print(
        f"\njudge vs lexicon (judge>={jt} vs lexicon>={lt}): "
        f"judge={len(judge_pass)} lexicon={len(lex_pass)} overlap={len(overlap)} "
        f"judge-only(wordless catches)={len(judge_only)} lexicon-only(judge drops)={len(lex_only)}"
    )

    print(f"\n=== JUDGE PICKED (worth>={jt}): {len(judge_pass)} beats ===")
    for r in sorted(judge_pass, key=lambda r: -r.worth):
        tag = "  [lexicon也过]" if r.lexicon_score >= lt else "  [词表漏!]"
        print(f"\n  worth={r.worth} angle={r.angle or '-'} lex={r.lexicon_score}{tag}")
        print(f"      moment: {r.moment or '(空)'}")
        _print_beat(r.beat)

    if lex_only:
        print(f"\n=== LEXICON-ONLY (lexicon>={lt} but judge<{jt}): {len(lex_only)} beats（词表误报，judge 丢弃）===")
        for r in sorted(lex_only, key=lambda r: -r.lexicon_score):
            print(f"\n  worth={r.worth} lex={r.lexicon_score}")
            _print_beat(r.beat)

    # cost / latency
    in_chars = sum(r.prompt_chars for r in records)
    out_chars = sum(r.out_chars for r in records)
    lats = sorted(r.latency_ms for r in records if r.latency_ms > 0)
    n_calls = len(records)
    print(f"\n=== cost / latency over {n_calls} judge calls ===")
    print(f"  input chars: {in_chars} (~{round(in_chars * 0.7)} tokens 粗估)")
    print(f"  output chars: {out_chars} (~{round(out_chars * 0.7)} tokens 粗估)")
    if lats:
        p = lambda q: lats[min(len(lats) - 1, int(q * len(lats)))]  # noqa: E731
        print(f"  latency ms: p50={p(0.5):.0f} p90={p(0.9):.0f} max={lats[-1]:.0f}")
    print("  (tokens 粗估按 0.7 tok/字符；精确计费以模型 tokenizer 为准)")
    if errs:
        print(f"\n  judge errors ({len(errs)}): first = {errs[0].error}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

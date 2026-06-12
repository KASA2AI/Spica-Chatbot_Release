"""P5 step 2 acceptance ④ / step-4 calibration tool: score distribution over a
REAL OCR corpus.

Feeds the real-machine committed story lines (spica_data/galgame.sqlite3, the
same rows production wrote during actual play) through the REAL BeatAggregator
(ReactionEngine with permissive budget so every beat reaches scoring) and
``score_beat`` with the shipped lexicon, then prints the score distribution --
so step-4 threshold calibration (low 6 / normal 4 / high 3) starts from data,
not guesses. Timestamps drive the aggregator clock, so idle_flush cuts happen
exactly where real pauses happened.

Usage:
    python scripts/reaction_score_report.py [--game limelight] [--db PATH]
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from spica.core.companion_events import (  # noqa: E402
    GalgameStableLineCommittedEvent,
    GalgameStatusChangedEvent,
)
from spica.galgame.reaction import (  # noqa: E402
    REACTION_MODE_TABLE,
    ReactionEngine,
    ReactionModeParams,
    load_reaction_lexicon,
    score_beat,
)
from spica.galgame.session import GalgameState  # noqa: E402

_PERMISSIVE = ReactionModeParams(min_score=0, max_per_window=10**9, cooldown_seconds=0.0)
# Thresholds come from the LIVE mode table -- the report exists to catch exactly
# the kind of drift a hardcoded copy would reintroduce.
_THRESHOLDS = tuple(
    (f"{mode}(>={params.min_score})", params.min_score)
    for mode, params in sorted(REACTION_MODE_TABLE.items(), key=lambda kv: kv[1].min_score)
)
_BUCKETS = (("0-2", 0, 2), ("3-4", 3, 4), ("5-6", 5, 6), ("7+", 7, 10**9))


def _load_lines(db: Path, game_id: str) -> list[tuple[float, str | None, str]]:
    conn = sqlite3.connect(db)
    rows = conn.execute(
        "SELECT data FROM story_lines WHERE game_id=? AND status='committed' "
        "ORDER BY timestamp",
        (game_id,),
    ).fetchall()
    conn.close()
    out = []
    for (blob,) in rows:
        data = json.loads(blob)
        ts = datetime.fromisoformat(data["timestamp"]).timestamp()
        out.append((ts, data.get("speaker"), data.get("text") or ""))
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--game", default="limelight")
    parser.add_argument("--db", default=str(REPO_ROOT / "spica_data" / "galgame.sqlite3"))
    args = parser.parse_args()

    lines = _load_lines(Path(args.db), args.game)
    if not lines:
        print(f"no committed lines for game_id={args.game!r} in {args.db}")
        return 1
    lexicon = load_reaction_lexicon(args.game)
    print(f"corpus: {len(lines)} committed lines (game={args.game}); "
          f"lexicon: {len(lexicon.categories)} categories, {len(lexicon.signals)} signals")

    scored: list[tuple[int, tuple[str, ...], str]] = []  # (score, reasons, cut_reason)

    def _speak(beat, score):  # noqa: ANN001 -- engine callback shape
        scored.append((score, score_beat(beat, lexicon).reasons, beat.cut_reason))
        return True

    engine = ReactionEngine(
        speak=_speak,
        params_provider=lambda: _PERMISSIVE,
        scorer=lambda beat: score_beat(beat, lexicon),
    )
    engine.handle_event(
        GalgameStatusChangedEvent(state=GalgameState.PLAYING.value), now=lines[0][0]
    )
    for index, (ts, speaker, text) in enumerate(lines):
        engine.handle_idle(now=ts)  # the worker's debounce timer, replayed from history
        engine.handle_event(
            GalgameStableLineCommittedEvent(line_id=f"r{index}", speaker=speaker, text=text),
            now=ts,
        )
    engine.handle_idle(now=lines[-1][0] + 9.0)

    deduped = sum(1 for d in engine.decisions if d.kind == "dedupe_hash_drop")
    total = len(scored)
    if not total:
        print("no beats produced")
        return 1
    scores = sorted(score for score, _, _ in scored)

    print(f"\nbeats scored: {total} (dedupe-dropped: {deduped})")
    cut_counts: dict[str, int] = {}
    for _, _, cut in scored:
        cut_counts[cut] = cut_counts.get(cut, 0) + 1
    print("cut reasons:", ", ".join(f"{k}={v}" for k, v in sorted(cut_counts.items())))

    print("\nscore histogram:")
    for label, lo, hi in _BUCKETS:
        count = sum(1 for s in scores if lo <= s <= hi)
        bar = "#" * round(50 * count / total)
        print(f"  {label:>4}: {count:5d} ({100 * count / total:5.1f}%) {bar}")

    def _pct(p: float) -> int:
        return scores[min(total - 1, int(p * total))]

    print(f"\npercentiles: p50={_pct(0.50)} p75={_pct(0.75)} p90={_pct(0.90)} "
          f"p99={_pct(0.99)} max={scores[-1]}")

    elapsed_active = sum(
        min(b[0] - a[0], 120.0) for a, b in zip(lines, lines[1:])
    )  # gaps > 2min counted as 2min: rough "active play" time
    hours = max(elapsed_active / 3600.0, 1e-9)
    print(f"\nactive play time (gap-capped): {elapsed_active / 60:.0f} min")
    print("threshold pass rates (before budget/cooldown -- the budget caps these):")
    for label, threshold in _THRESHOLDS:
        passing = sum(1 for s in scores if s >= threshold)
        print(f"  {label:>12}: {passing:4d} beats ({100 * passing / total:5.1f}%), "
              f"~{passing / hours:.1f}/h raw")

    print("\ntop reasons:")
    reason_counts: dict[str, int] = {}
    for _, reasons, _ in scored:
        for reason in reasons:
            reason_counts[reason] = reason_counts.get(reason, 0) + 1
    for reason, count in sorted(reason_counts.items(), key=lambda kv: -kv[1]):
        print(f"  {reason:35s} {count}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

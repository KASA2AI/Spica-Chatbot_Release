"""Completion-consent decision (Phase 1) -- pure function, no Qt, no I/O.

Extracted from the UI controller so it is unit-testable (P2-16). Given a finished
download, decide whether Spica auto-plays or asks for confirmation (D5 / P1-7):

- elapsed <= threshold means auto-play intent;
- elapsed > threshold, unknown age, or startup reconciliation means confirmation.

Transient UI safety state (Spica speaking/singing, user speaking, or galgame
active) deliberately does not belong here. AnimeController delays execution
until it is safe without changing the consent decision.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

AUTO_PLAY = "auto_play"
REQUIRE_CONFIRMATION = "require_confirmation"


@dataclass(frozen=True)
class PlaybackDecision:
    action: str        # AUTO_PLAY | REQUIRE_CONFIRMATION
    reason: str = ""


def decide_playback(
    *,
    elapsed_seconds: float | None,
    threshold_seconds: float,
    reconciled_unknown_age: bool = False,
) -> PlaybackDecision:
    """See module docstring. ``elapsed_seconds=None`` means the age is unknown
    (in-flight across a restart) and therefore requires confirmation."""
    if (not math.isfinite(threshold_seconds) or threshold_seconds < 0
            or reconciled_unknown_age or elapsed_seconds is None
            or not math.isfinite(elapsed_seconds) or elapsed_seconds < 0):
        return PlaybackDecision(
            REQUIRE_CONFIRMATION, "unknown, invalid, or reconciled age")
    if elapsed_seconds > threshold_seconds:
        return PlaybackDecision(REQUIRE_CONFIRMATION, "slow download")
    return PlaybackDecision(AUTO_PLAY, "within auto-play threshold")

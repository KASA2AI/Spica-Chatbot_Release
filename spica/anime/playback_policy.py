"""Completion-behavior decision (Phase 1) -- pure function, no Qt, no I/O.

Extracted from the UI controller so it is unit-testable (P2-16). Given a finished
download, decide whether Spica auto-plays or announces-and-waits (D5 / P1-7):

- auto-play ONLY when ALL hold: elapsed <= threshold (fast), not busy (not
  speaking/singing), galgame not active (a player window popping up would trip
  the companion privacy gate and pause play), and this run is not a
  restart-reconciled task of unknown age (P1-9: those are always announced).
- otherwise announce (a system turn), which the controller then retries if the
  proactive arbiter drops it while busy (P1-5).
"""

from __future__ import annotations

from dataclasses import dataclass

AUTO_PLAY = "auto_play"
ANNOUNCE = "announce"


@dataclass(frozen=True)
class PlaybackDecision:
    action: str        # AUTO_PLAY | ANNOUNCE
    reason: str = ""


def decide_playback(
    *,
    elapsed_seconds: float | None,
    threshold_seconds: float,
    is_busy: bool,
    galgame_active: bool,
    reconciled_unknown_age: bool = False,
) -> PlaybackDecision:
    """See module docstring. ``elapsed_seconds=None`` means the age is unknown
    (in-flight across a restart) -> treated as slow (announce)."""
    if reconciled_unknown_age or elapsed_seconds is None:
        return PlaybackDecision(ANNOUNCE, "unknown age / reconciled")
    if is_busy:
        return PlaybackDecision(ANNOUNCE, "busy")
    if galgame_active:
        return PlaybackDecision(ANNOUNCE, "galgame active")
    if elapsed_seconds > threshold_seconds:
        return PlaybackDecision(ANNOUNCE, "slow download")
    return PlaybackDecision(AUTO_PLAY, "fast and idle")

"""PrivacyGate: the ONE evaluator of "may we look at this window right now"
(OO migration Phase 8-c2, 设计裁决 5).

Absorbs the two evaluation copies that used to live apart (CLAUDE.md §4
截图边界):

- ``purpose="ocr"``   -- the OCR loop's per-cycle pre-check, moved VERBATIM
  from ``OcrStreamRunner._evaluate_safety``: ``locator.check_safety`` first,
  then the overlay-covers-dialog-region check (``OVERLAY_COVERS``);
- ``purpose="watch"`` -- the watch tool's state gate: ONLY a safe-state test,
  deliberately WITHOUT ``check_safety`` (the historical asymmetry -- watch is
  user-initiated and its primary scenario includes CHOICE_CHECKING; the loop's
  own cycle safety keeps monitoring the window).

The named state sets (``WATCH_SAFE_STATES`` 等) STAY in ``session.py`` --
D-P5-8 single home; this gate only consumes the set it is constructed with.
Dynamic inputs (overlay_window_id / overlay_rect / dialog_ratios) are passed
PER CALL, never frozen at construction (the UI pushes overlay rects at
runtime via ``OcrStreamRunner.set_overlay_rect``).

``target.owner_domain`` must be "galgame": a foreign target reaching this
gate is a WIRING BUG (co-watch etc. build their own gate instance with their
own safe states) -- it raises ``ValueError`` loudly instead of guessing.

The check→capture race stays UNnarrowed this phase (equivalence migration;
ledgered in the migration plan). Qt-free (CLAUDE.md #1).
"""

from __future__ import annotations

from typing import Any

from spica.galgame.ocr_region import overlay_covers_region
from spica.ports.window_locator import WindowSafetyResult
from spica.runtime.window import WindowTarget

_OWNER_DOMAIN = "galgame"


def _dialog_region_rect(geom: Any, ratios: tuple[float, float, float, float]) -> tuple[int, int, int, int]:
    # Moved verbatim from OcrStreamRunner._dialog_region_rect (Phase 8-c2).
    rx, ry, rw, rh = ratios
    return (
        geom.x + round(rx * geom.width),
        geom.y + round(ry * geom.height),
        round(rw * geom.width),
        round(rh * geom.height),
    )


class PrivacyGate:
    def __init__(self, locator: Any, *, safe_states: Any) -> None:
        self._locator = locator
        self._safe_states = safe_states

    def evaluate(
        self,
        target: WindowTarget,
        state: Any,
        purpose: str,
        *,
        overlay_window_id: str | None = None,
        overlay_rect: tuple[int, int, int, int] | None = None,
        dialog_ratios: tuple[float, float, float, float] | None = None,
    ) -> WindowSafetyResult:
        if target.owner_domain != _OWNER_DOMAIN:
            raise ValueError(
                f"PrivacyGate(galgame) got a foreign target (owner_domain="
                f"{target.owner_domain!r}) -- wiring bug; this gate never "
                "evaluates another domain's window."
            )
        if purpose == "watch":
            # State gate ONLY -- deliberately no check_safety (the historical
            # asymmetry; the tool refuses BEFORE any capture on unsafe states).
            if state not in self._safe_states:
                return WindowSafetyResult(
                    ok=False,
                    reason_code="GAME_WINDOW_NOT_SAFE",
                    reason="游戏窗口当前不在可截屏状态。",
                )
            return WindowSafetyResult(ok=True)
        if purpose == "ocr":
            # Moved verbatim from OcrStreamRunner._evaluate_safety.
            result = self._locator.check_safety(
                target.window_id, target.match_rule, overlay_window_id
            )
            if not result.ok:
                return result
            if overlay_rect is not None and dialog_ratios is not None:
                geom = self._locator.get_window_geometry(target.window_id)
                if geom is not None and overlay_covers_region(
                    overlay_rect, _dialog_region_rect(geom, dialog_ratios)
                ):
                    return WindowSafetyResult(
                        ok=False,
                        reason_code="OVERLAY_COVERS",
                        reason="Spica overlay 覆盖了 OCR 对白区域。",
                    )
            return result
        raise ValueError(f"unknown PrivacyGate purpose {purpose!r} (wiring bug)")

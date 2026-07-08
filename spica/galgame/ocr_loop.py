"""OCR text-stream runner (Phase 7, §10.2). Background, serial, Qt-free.

ONE daemon thread runs ``while not stopped: cycle(); wait(interval)`` -- strict
"finish then wait", so cycles NEVER overlap (single thread). Cross-path inference
overlap (vs inspect_screen) is prevented by the shared ``_INFER_LOCK`` inside
``ocr_image`` (Phase 7).

Each cycle runs the §7 safety pre-check FIRST:
- playing + safe   -> capture the bound window, crop the dialogue (and name) region
  by ratio, OCR, feed ``session.on_ocr_result``;
- playing + unsafe -> ``session.on_window_lost(reason_code)`` (the reason_code, e.g.
  WINDOW_NOT_FOCUSED, rides the event + log so a normal pause is distinguishable
  from a bug) and capture nothing -- never risk grabbing another app (§7.1);
- window_lost + safe -> ``session.on_window_recovered()``.

Slow cycles (CPU OCR can't hold the interval) are logged as warnings (§4.3). This
thread never touches Qt; events flow through the session's injected sink.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable

from spica.galgame.models import WindowMatchRule
from spica.galgame.ocr_region import crop_by_ratios
from spica.galgame.privacy_gate import PrivacyGate
from spica.galgame.session import (
    WATCH_SAFE_STATES,
    GalgameCompanionSession,
    GalgameState,
    GalgameStateError,
)
from spica.ports.ocr import OCRPort
from spica.ports.screen_capture import ScreenCapturePort
from spica.ports.window_locator import WindowLocatorPort, WindowSafetyResult
from spica.runtime.window import WindowTarget

logger = logging.getLogger(__name__)

# States in which the loop keeps spinning (it only CAPTURES in playing+safe).
_LOOP_STATES = frozenset(
    {
        GalgameState.PLAYING,
        GalgameState.WINDOW_LOST,
        GalgameState.PAUSED,
        GalgameState.CHOICE_CHECKING,
        GalgameState.BACKGROUND_SUMMARIZING,
    }
)

Ratios = tuple[float, float, float, float]


class OcrStreamRunner:
    def __init__(
        self,
        session: GalgameCompanionSession,
        capture: ScreenCapturePort,
        locator: WindowLocatorPort,
        ocr: OCRPort,
        *,
        interval_seconds: float = 1.0,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._session = session
        self._capture = capture
        self._locator = locator
        self._ocr = ocr
        # Phase 8-c2 (设计裁决 5): the safety evaluator. The gate holds only the
        # locator + the named state set; every dynamic input (overlay rect /
        # dialog ratios / overlay window id) is passed per evaluate() call.
        self._gate = PrivacyGate(locator, safe_states=WATCH_SAFE_STATES)
        self._interval = max(0.0, float(interval_seconds))
        self._clock = clock or time.monotonic
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._window_id: str | None = None
        self._match_rule = WindowMatchRule()  # keywords used to verify focus (§17.3)
        self._dialog_ratios: Ratios | None = None
        self._speaker_ratios: Ratios | None = None
        self._overlay_window_id: str | None = None
        self._overlay_rect: tuple[int, int, int, int] | None = None
        self._last_cycle_ms = 0.0

    @property
    def last_cycle_ms(self) -> float:
        return self._last_cycle_ms

    def set_overlay_rect(self, rect: tuple[int, int, int, int] | None) -> None:
        # Physical-pixel (x, y, w, h) of the Spica overlay, pushed by the UI.
        self._overlay_rect = tuple(rect) if rect else None  # type: ignore[assignment]

    def configure(
        self,
        window_id: str,
        *,
        dialog_ratios: Ratios,
        match_rule: WindowMatchRule | None = None,
        speaker_ratios: Ratios | None = None,
        overlay_window_id: str | None = None,
    ) -> None:
        """Set the target window + regions + the focus-match rule WITHOUT starting the
        thread (also the single-step entry for tests, paired with ``run_once``).

        ``match_rule`` carries the game's title_keywords; the safety check verifies
        focus by keyword (§17.3). None -> an empty rule -> focus can't be verified ->
        the loop conservatively stays paused (never mis-captures)."""
        self._window_id = window_id
        self._match_rule = match_rule or WindowMatchRule()
        self._dialog_ratios = dialog_ratios
        self._speaker_ratios = speaker_ratios
        self._overlay_window_id = overlay_window_id

    def start(
        self,
        window_id: str,
        *,
        dialog_ratios: Ratios,
        match_rule: WindowMatchRule | None = None,
        speaker_ratios: Ratios | None = None,
        overlay_window_id: str | None = None,
    ) -> None:
        self.configure(
            window_id,
            dialog_ratios=dialog_ratios,
            match_rule=match_rule,
            speaker_ratios=speaker_ratios,
            overlay_window_id=overlay_window_id,
        )
        self._stop.clear()
        self._thread = threading.Thread(target=self._run_loop, name="galgame-ocr-loop", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    # -- loop -----------------------------------------------------------------
    def _run_loop(self) -> None:
        while not self._stop.is_set():
            if self._session.state not in _LOOP_STATES:
                break  # session left the play states (ended / game_launched)
            started = self._clock()
            try:
                self.run_once()
            except Exception as exc:  # noqa: BLE001 -- a cycle error must not kill the loop
                logger.warning("ocr loop cycle error: %s", exc, exc_info=True)
            self._last_cycle_ms = (self._clock() - started) * 1000.0
            if self._last_cycle_ms > self._interval * 1000.0:
                logger.warning(
                    "ocr_cycle_ms=%.0f exceeded interval_ms=%.0f -- CPU OCR may not hold the interval",
                    self._last_cycle_ms, self._interval * 1000.0,
                )
            self._stop.wait(self._interval)  # "finish then wait"; responsive to stop()

    def run_once(self) -> None:
        """One serial cycle (also the unit-test single-step entry)."""
        state = self._session.state
        safety = self._evaluate_safety()
        # Capture in PLAYING and BACKGROUND_SUMMARIZING -- a background summary runs off
        # this thread and must NOT pause OCR (§16.1).
        if state in (GalgameState.PLAYING, GalgameState.BACKGROUND_SUMMARIZING):
            if not safety.ok:
                self._submit(lambda: self._session.on_window_lost(safety.reason_code))
                return
            text, speaker = self._capture_and_ocr()
            if text:
                self._submit(lambda: self._session.on_ocr_result(text, speaker))
        elif state == GalgameState.WINDOW_LOST and safety.ok:
            self._submit(self._session.on_window_recovered)

    def _evaluate_safety(self) -> WindowSafetyResult:
        # Phase 8-c2: verbatim semantics via the PrivacyGate (ocr purpose =
        # check_safety + overlay-covers-dialog-region, OVERLAY_COVERS kept);
        # dynamic inputs are passed per call, never frozen at construction.
        return self._gate.evaluate(
            WindowTarget(
                window_id=self._window_id,  # type: ignore[arg-type]
                owner_domain="galgame",
                match_rule=self._match_rule,
            ),
            None,  # session state is irrelevant to the ocr purpose
            "ocr",
            overlay_window_id=self._overlay_window_id,
            overlay_rect=self._overlay_rect,
            dialog_ratios=self._dialog_ratios,
        )

    def _capture_and_ocr(self) -> tuple[str | None, str | None]:
        geom = self._locator.get_window_geometry(self._window_id)
        if geom is None:
            return None, None
        captured = self._capture.capture_rect(geom.x, geom.y, geom.width, geom.height)
        dialog_text = self._ocr.recognize(crop_by_ratios(captured.image, self._dialog_ratios)).text
        speaker_text = None
        if self._speaker_ratios is not None:
            speaker_text = self._ocr.recognize(crop_by_ratios(captured.image, self._speaker_ratios)).text or None
        return dialog_text, speaker_text

    def _submit(self, fn: Callable[[], Any]) -> None:
        try:
            fn()
        except GalgameStateError as exc:
            # State changed between read and call (race) -> skip; next cycle re-evaluates.
            logger.debug("ocr loop transition skipped: %s", exc)

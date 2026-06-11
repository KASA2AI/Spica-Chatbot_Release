"""OCR region calibration + single-shot test coordinator (Phase 6), Qt-free.

Single标定+测试, NOT a continuous OCR loop (that is Phase 7). It:

- ``set_dialog_region`` / ``set_speaker_region``: convert a drawn physical screen
  rect to ratios within the bound window, and store an ``OCRRegion`` (ratios +
  pixel rect + window_size_at_calibration, §9.5) into ``GameProfile.ocr_profile``.
- ``run_ocr_test`` (§18.4): grab the whole window once -> crop the region(s) by
  ratio -> emit a PNG preview (with suspect_blank) -> OCR -> emit the recognized
  text. The user then confirms / reframes / hand-corrects in the UI.

It depends only on Qt-free ports (capture/locator/ocr) + game_memory + the sink;
it never imports Qt. Window geometry comes from the locator; capture just grabs a
rect. Best-effort: a missing geometry / read failure emits a context-rich
``galgame_error`` rather than crashing.
"""

from __future__ import annotations

import dataclasses
import logging
from typing import Any

from spica.core.companion_events import (
    CompanionEventSink,
    GalgameErrorEvent,
    GalgameOcrPreviewReadyEvent,
    GalgameOcrTestResultEvent,
    noop_companion_sink,
)
from spica.galgame.models import GameProfile, OCRProfile, OCRRegion, utc_now_iso
from spica.galgame.ocr_region import crop_by_ratios, looks_blank, screen_rect_to_ratios
from spica.ports.ocr import OCRPort
from spica.ports.screen_capture import ScreenCapturePort
from spica.ports.window_locator import WindowGeometry, WindowLocatorPort

logger = logging.getLogger(__name__)


class GalgameOcrCalibrator:
    def __init__(
        self,
        capture: ScreenCapturePort,
        locator: WindowLocatorPort,
        ocr: OCRPort,
        game_memory: Any,
        emit: CompanionEventSink | None = None,
    ) -> None:
        self._capture = capture
        self._locator = locator
        self._ocr = ocr
        self._mem = game_memory
        self._emit: CompanionEventSink = emit or noop_companion_sink

    # -- calibration ----------------------------------------------------------
    def set_dialog_region(self, game_id: str, window_id: str, screen_rect: tuple[int, int, int, int]) -> bool:
        return self._set_region(game_id, window_id, screen_rect, slot="dialog_text_region")

    def set_speaker_region(self, game_id: str, window_id: str, screen_rect: tuple[int, int, int, int]) -> bool:
        return self._set_region(game_id, window_id, screen_rect, slot="speaker_name_region")

    def _set_region(self, game_id: str, window_id: str, screen_rect, slot: str) -> bool:
        geom = self._geometry_or_error(window_id)
        if geom is None:
            return False
        ratios = screen_rect_to_ratios(screen_rect, geom)
        region = OCRRegion(
            x_ratio=ratios[0], y_ratio=ratios[1], w_ratio=ratios[2], h_ratio=ratios[3],
            pixel_rect=[
                round(ratios[0] * geom.width), round(ratios[1] * geom.height),
                round(ratios[2] * geom.width), round(ratios[3] * geom.height),
            ],
            window_size_at_calibration=[geom.width, geom.height],
        )
        ocr_profile, profile = self._load_ocr_profile(game_id)
        ocr_profile = dataclasses.replace(ocr_profile, **{slot: region.to_dict()})
        self._store_ocr_profile(profile, ocr_profile)
        return True

    def confirm(self, game_id: str) -> None:
        ocr_profile, profile = self._load_ocr_profile(game_id)
        now = utc_now_iso()
        updates: dict[str, Any] = {}
        for slot in ("dialog_text_region", "speaker_name_region"):
            region = getattr(ocr_profile, slot)
            if isinstance(region, dict) and region:
                region = dict(region)
                region["last_verified_at"] = now
                updates[slot] = region
        if updates:
            self._store_ocr_profile(profile, dataclasses.replace(ocr_profile, **updates))

    # -- single OCR test (§18.4) ---------------------------------------------
    def run_ocr_test(self, game_id: str, window_id: str) -> None:
        geom = self._geometry_or_error(window_id)
        if geom is None:
            return
        try:
            captured = self._capture.capture_rect(geom.x, geom.y, geom.width, geom.height)
        except Exception as exc:  # noqa: BLE001 -- best-effort, surface as galgame_error
            logger.warning("ocr test capture failed (game=%s): %s", game_id, exc, exc_info=True)
            self._emit(GalgameErrorEvent(message=f"窗口截图失败：{exc}", code="OCR_TEST_CAPTURE_FAILED"))
            return

        ocr_profile, _ = self._load_ocr_profile(game_id)
        dialog_text = self._test_region(captured.image, ocr_profile.dialog_text_region, region_name="dialog")
        speaker_text = None
        if isinstance(ocr_profile.speaker_name_region, dict) and ocr_profile.speaker_name_region:
            speaker_text = self._test_region(captured.image, ocr_profile.speaker_name_region, region_name="speaker")
        self._emit(
            GalgameOcrTestResultEvent(
                dialog_text=dialog_text or "", speaker_text=speaker_text,
                speaker_strategy=ocr_profile.speaker_strategy,
            )
        )

    def _test_region(self, window_image: Any, region: Any, *, region_name: str) -> str | None:
        if not isinstance(region, dict) or not region:
            return None
        ratios = (
            float(region.get("x_ratio", 0.0)), float(region.get("y_ratio", 0.0)),
            float(region.get("w_ratio", 0.0)), float(region.get("h_ratio", 0.0)),
        )
        crop = crop_by_ratios(window_image, ratios)
        blank = looks_blank(crop)
        png = _to_png_bytes(crop)
        self._emit(
            GalgameOcrPreviewReadyEvent(
                region=region_name, image_png=png, width=crop.width, height=crop.height, suspect_blank=blank
            )
        )
        return self._ocr.recognize(crop).text

    # -- helpers --------------------------------------------------------------
    def _geometry_or_error(self, window_id: str) -> WindowGeometry | None:
        geom = self._locator.get_window_geometry(window_id)
        if geom is None:
            self._emit(
                GalgameErrorEvent(
                    message="无法获取游戏窗口几何（窗口可能已关闭或不可定位）。",
                    code="WINDOW_GEOMETRY_UNAVAILABLE",
                )
            )
        return geom

    def _load_ocr_profile(self, game_id: str) -> tuple[OCRProfile, GameProfile]:
        now = utc_now_iso()
        profile = self._mem.get_game_profile(game_id)
        if profile is None:
            profile = GameProfile(game_id=game_id, display_name=game_id, created_at=now, updated_at=now)
        ocr_profile = OCRProfile.from_dict(profile.ocr_profile) if profile.ocr_profile else OCRProfile()
        return ocr_profile, profile

    def _store_ocr_profile(self, profile: GameProfile, ocr_profile: OCRProfile) -> None:
        updated = dataclasses.replace(profile, ocr_profile=ocr_profile.to_dict(), updated_at=utc_now_iso())
        self._mem.upsert_game_profile(updated)


def _to_png_bytes(image: Any) -> bytes:
    from io import BytesIO

    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()

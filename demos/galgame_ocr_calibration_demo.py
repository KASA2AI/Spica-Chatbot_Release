"""Standalone OCR-calibration demo (Phase 6 real-machine acceptance).

Run on your Ubuntu desktop with the game (anemoi) already open:

    python galgame_ocr_calibration_demo.py

Then drag a rectangle over the game's dialog box. The demo will:
  1. mss-capture that screen region (this is the Phase 6 head acceptance: does mss
     get the flatpak/Bottles window's REAL pixels, not a black frame?),
  2. flag suspect-blank if the capture looks black/uniform,
  3. run RapidOCR on the region (does it read the Chinese dialogue?),
  4. pop a preview window with the cropped image + recognized text.

This needs a real screen + display server, which offscreen pytest cannot provide
-- hence a manual demo, not an automated test. Requires: PySide6, mss, Pillow,
and rapidocr-onnxruntime installed in this env.
"""

from __future__ import annotations

import os
import sys


def main() -> None:
    # If a previous test run left QT_QPA_PLATFORM=offscreen in the shell, the demo
    # would be invisible -- force the native platform so the overlay actually shows.
    if os.environ.get("QT_QPA_PLATFORM") == "offscreen":
        os.environ.pop("QT_QPA_PLATFORM")

    from PySide6.QtWidgets import QApplication

    from spica.adapters.ocr import RapidOcrAdapter
    from spica.adapters.screen_capture import MssScreenCapture
    from spica.galgame.ocr_region import looks_blank
    from ui.widgets.ocr_calibration_preview import OcrCalibrationPreview
    from ui.widgets.screenshot_selector import ScreenshotSelectionOverlay

    app = QApplication.instance() or QApplication(sys.argv)
    capture = MssScreenCapture()
    ocr = RapidOcrAdapter()
    preview = OcrCalibrationPreview()
    overlay = ScreenshotSelectionOverlay()

    def on_finished(payload: dict) -> None:
        rect = payload.get("logical_rect")
        dpr = float(payload.get("device_pixel_ratio") or 1.0)
        # mss works in physical pixels; the overlay rect is logical -> scale by DPR.
        left, top = round(rect.x() * dpr), round(rect.y() * dpr)
        width, height = round(rect.width() * dpr), round(rect.height() * dpr)
        print(f"[demo] capture physical rect left={left} top={top} w={width} h={height} (dpr={dpr})")
        try:
            captured = capture.capture_rect(left, top, width, height)
        except Exception as exc:  # noqa: BLE001 -- demo: surface the failure
            print(f"[demo] mss capture FAILED: {type(exc).__name__}: {exc}")
            return
        blank = looks_blank(captured.image)
        print(f"[demo] captured {captured.width}x{captured.height}  suspect_blank={blank}")
        if blank:
            print("[demo] WARNING: capture looks blank/black -- likely Wayland or occlusion.")
        try:
            text = ocr.recognize(captured.image).text
        except Exception as exc:  # noqa: BLE001
            text = f"(OCR failed: {type(exc).__name__}: {exc})"
        print(f"[demo] OCR text:\n{text}\n")
        preview.show_preview(captured.to_png_bytes(), blank)
        preview.show_text(text)
        preview.resize(640, 480)
        preview.show()
        preview.raise_()

    def on_cancelled(reason: str) -> None:
        print(f"[demo] selection cancelled: {reason}")

    overlay.selection_finished.connect(on_finished)
    overlay.selection_cancelled.connect(on_cancelled)
    print("[demo] 拖拽框选游戏对话框区域（Esc 取消）...")
    overlay.begin()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()

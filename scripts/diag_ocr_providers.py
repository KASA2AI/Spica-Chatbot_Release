"""OCR GPU diagnosis (FINDINGS #5 待核): is RapidOCR really on CUDA?

Answers three questions, NO production code touched:

1. Which execution providers are the shared RapidOCR engine's onnxruntime
   sessions ACTUALLY using? (``session.get_providers()`` per det/cls/rec --
   the same authoritative call rapidocr's own ``_verify_providers`` uses.
   NOT ``onnxruntime.get_available_providers()``, which only says what the
   build ships. The old galgame_companion_demo introspection printed ``[]``
   because it skipped every ``callable(child)`` -- and TextDetector /
   OrtInferSession all define ``__call__``, so the walk pruned the whole
   chain at depth 1. This script uses the explicit attribute paths of
   rapidocr_onnxruntime 1.4.x instead, with a gc sweep as fallback.)

2. Steady-state pure-OCR cycle time (no TTS, no Moondream): warmup then N
   timed inferences on a synthetic dialog-strip image. GPU-normal is
   ~110-160ms; CPU fallback is ~700ms.

3. Contention arms reproducing the dialogue-period band while providers
   still say CUDA (i.e. transient contention, NOT a CPU fallback):
   ``--contend``      torch matmuls saturating the GPU (TTS stand-in);
   ``--contend-ocr``  a background thread loops FULL-FRAME ``ocr_image``
                      (what watch_game_screen/inspect_screen do via
                      analyzer.py before Moondream) while the foreground
                      times the dialog strip -- both share ``_INFER_LOCK``,
                      so the foreground wait models exactly what the OCR
                      loop's ``ocr_cycle_ms`` counts when she "looks".
   ``--size WxH``     synthetic image size (default 1280x250 dialog strip;
                      use 1920x1080 for the full-frame cost).

``--cpu`` times a separate CPU-only engine in this diagnostic process for
the ~700ms reference figure (run it in its OWN invocation; it second-loads
the model, which is fine here and forbidden only in the app).

Usage (from repo root, app NOT running for clean steady-state):
    python scripts/diag_ocr_providers.py
    python scripts/diag_ocr_providers.py --contend
    python scripts/diag_ocr_providers.py --cpu
"""

from __future__ import annotations

import argparse
import logging
import statistics
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")


def _engine_sessions(engine):
    """[(name, InferenceSession)] for det/cls/rec via the explicit attribute
    layout of rapidocr_onnxruntime 1.4.x; gc sweep if the layout changed."""
    paths = {
        "det": ("text_det", "infer"),
        "cls": ("text_cls", "infer"),
        "rec": ("text_rec", "session"),  # rec names its wrapper differently
    }
    found = []
    for name, (stage_attr, wrapper_attr) in paths.items():
        wrapper = getattr(getattr(engine, stage_attr, None), wrapper_attr, None)
        session = getattr(wrapper, "session", None)
        if session is not None and hasattr(session, "get_providers"):
            found.append((name, session))
    if not found:
        import gc

        import onnxruntime

        found = [
            ("gc-sweep", obj)
            for obj in gc.get_objects()
            if isinstance(obj, onnxruntime.InferenceSession)
        ]
    return found


def _synth_dialog_image(width=1280, height=250):
    """A dialog-strip-sized crop with real text lines, like what the OCR loop
    feeds after ratio cropping. Synthetic but exercises det+cls+rec."""
    from PIL import Image, ImageDraw, ImageFont

    image = Image.new("RGB", (width, height), (24, 24, 48))
    draw = ImageDraw.Draw(image)
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 34)
    except OSError:
        font = ImageFont.load_default()
    lines = [
        "Spica: The quick brown fox jumps over the lazy dog.",
        "Mugi: Pack my box with five dozen liquor jugs, okay?",
        "Narration: 0123456789 ABCDEFGHIJKLMNOPQRSTUVWXYZ.",
    ]
    # Repeat the block down tall (full-frame) images so text density stays
    # roughly game-like instead of leaving a 1080p frame blank below the strip.
    y = 24
    index = 0
    while y < height - 40:
        draw.text((30, y), lines[index % len(lines)], fill=(240, 240, 240), font=font)
        index += 1
        y += 70 if index % len(lines) else 200
    return image


def _time_cycles(run, image, warmup=2, rounds=10, label=""):
    for _ in range(warmup):
        run(image)
    samples = []
    for _ in range(rounds):
        start = time.perf_counter()
        result = run(image)
        samples.append((time.perf_counter() - start) * 1000.0)
    error = result.get("error") if isinstance(result, dict) else None
    text = result.get("raw_text", "") if isinstance(result, dict) else ""
    print(f"[diag] {label} OCR ms per cycle: " + " ".join(f"{s:.0f}" for s in samples))
    print(
        f"[diag] {label} min={min(samples):.0f}  median={statistics.median(samples):.0f}  "
        f"max={max(samples):.0f}  (warmup={warmup}, rounds={rounds})"
    )
    print(f"[diag] {label} last result: error={error}  text_chars={len(text)}")
    return samples


def _gpu_load(stop_event):
    """Background GPU pressure standing in for TTS synthesis: queue a batch of
    matmuls per sync so the SMs stay saturated (sync-per-matmul leaves gaps)."""
    import torch

    a = torch.randn(4096, 4096, device="cuda")
    b = torch.randn(4096, 4096, device="cuda")
    while not stop_event.is_set():
        for _ in range(32):
            a = (a @ b).tanh()
        torch.cuda.synchronize()
    del a, b
    torch.cuda.empty_cache()


def _ocr_load(stop_event, ocr_image, frame):
    """Background full-frame OCR loop: the watch_game_screen / inspect_screen
    analyzer path OCRs the WHOLE frame (analyzer.py) under the same
    _INFER_LOCK as the galgame loop -- this thread reproduces that."""
    while not stop_event.is_set():
        ocr_image(frame)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--contend", action="store_true", help="time cycles under torch GPU load")
    parser.add_argument("--contend-ocr", action="store_true",
                        help="time the dialog strip while a thread loops full-frame OCR (watch tool stand-in)")
    parser.add_argument("--cpu", action="store_true", help="time a separate CPU-only engine (reference)")
    parser.add_argument("--size", default="1280x250", help="synthetic image WxH (default dialog strip)")
    parser.add_argument("--rounds", type=int, default=10)
    args = parser.parse_args()
    width, height = (int(part) for part in args.size.lower().split("x"))

    import onnxruntime

    from agent_tools.function_tools.screen.backends import rapidocr as ocr_backend

    print(f"[diag] onnxruntime {onnxruntime.__version__}  device={onnxruntime.get_device()}")
    print(f"[diag] available providers (build): {onnxruntime.get_available_providers()}")

    image = _synth_dialog_image(width, height)

    if args.cpu:
        # Reference engine, CUDA off on every stage. Diagnostic-process-only
        # second load; the app must never do this (Phase 0 (5)).
        rapidocr_class = ocr_backend._load_rapidocr_class()
        cpu_engine = rapidocr_class(det_use_cuda=False, cls_use_cuda=False, rec_use_cuda=False)
        for name, session in _engine_sessions(cpu_engine):
            print(f"[diag] cpu-ref session[{name}] providers IN USE: {session.get_providers()}")
        import numpy as np

        prepared = np.asarray(image.convert("RGB"))
        _time_cycles(lambda img: {"raw_text": ""} if cpu_engine(prepared) else {}, image,
                     rounds=args.rounds, label="cpu-ref")
        return 0

    # The PRODUCTION engine path: same singleton + CUDA preload as the app.
    engine = ocr_backend._get_engine()
    sessions = _engine_sessions(engine)
    if not sessions:
        print("[diag] FAILED to locate any InferenceSession -- layout changed AND gc empty")
        return 1
    all_cuda_first = True
    for name, session in sessions:
        providers = session.get_providers()
        all_cuda_first = all_cuda_first and providers[0] == "CUDAExecutionProvider"
        print(f"[diag] session[{name}] providers IN USE: {providers}")
    print(f"[diag] verdict: {'GPU (CUDA first on every session)' if all_cuda_first else 'NOT all-CUDA -- check sessions above'}")

    _time_cycles(ocr_backend.ocr_image, image, rounds=args.rounds, label="steady")

    if args.contend:
        stop_event = threading.Event()
        load_thread = threading.Thread(target=_gpu_load, args=(stop_event,), daemon=True)
        load_thread.start()
        time.sleep(1.0)  # let the load ramp before sampling
        try:
            _time_cycles(ocr_backend.ocr_image, image, rounds=args.rounds, label="contended-gpu")
        finally:
            stop_event.set()
            load_thread.join(timeout=5.0)

    if args.contend_ocr:
        frame = _synth_dialog_image(1920, 1080)
        stop_event = threading.Event()
        load_thread = threading.Thread(
            target=_ocr_load, args=(stop_event, ocr_backend.ocr_image, frame), daemon=True
        )
        load_thread.start()
        time.sleep(0.5)
        try:
            _time_cycles(ocr_backend.ocr_image, image, rounds=args.rounds, label="contended-ocr")
        finally:
            stop_event.set()
            load_thread.join(timeout=15.0)

    if args.contend and args.contend_ocr:
        # Both at once = the real dialogue-period worst case: TTS/Moondream on
        # the GPU while the watch tool's full-frame OCR holds _INFER_LOCK.
        frame = _synth_dialog_image(1920, 1080)
        stop_event = threading.Event()
        threads = [
            threading.Thread(target=_gpu_load, args=(stop_event,), daemon=True),
            threading.Thread(target=_ocr_load, args=(stop_event, ocr_backend.ocr_image, frame), daemon=True),
        ]
        for thread in threads:
            thread.start()
        time.sleep(1.0)
        try:
            _time_cycles(ocr_backend.ocr_image, image, rounds=args.rounds, label="contended-both")
        finally:
            stop_event.set()
            for thread in threads:
                thread.join(timeout=15.0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

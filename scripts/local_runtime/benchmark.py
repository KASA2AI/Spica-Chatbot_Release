"""OCR benchmark: CUDA EP vs TRT EP, cold vs warm (LOCAL_RUNTIME_PLAN cut 2, §6 / D).

Measures, per provider:
  - engine build / session init time (cold = empty engine cache; warm = cache present),
  - per-image OCR time (mean over the reference set),
  - batch total over the set.
TRT cold-start pays the engine compile; warm reuses ORT's on-disk engine cache. Use
this to confirm TRT actually buys speedup after the warm cache amortizes the build.

Manual / real-machine tool (real RapidOCR + GPU + TRT). NOT CI. Run:

  python -m scripts.local_runtime.benchmark --images <dir-of-pngs> [--cache-dir artifacts/trt] [--out artifacts/benchmarks]

ENV NAMES (§3.3): reads NONE.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def _time_ocr(recognize, images) -> dict:
    per_image_ms = []
    for img in images:
        t0 = time.perf_counter()
        recognize(img)
        per_image_ms.append((time.perf_counter() - t0) * 1000.0)
    return {
        "per_image_ms_mean": round(sum(per_image_ms) / len(per_image_ms), 3),
        "per_image_ms_max": round(max(per_image_ms), 3),
        "batch_total_ms": round(sum(per_image_ms), 3),
    }


def _bench_cuda(images) -> dict:
    from agent_tools.function_tools.screen.backends import rapidocr as backend

    backend.clear_rapidocr_engine()
    t0 = time.perf_counter()
    backend.ocr_image(images[0])  # forces build + first inference (cold init)
    init_ms = (time.perf_counter() - t0) * 1000.0
    timing = _time_ocr(backend.ocr_image, images)  # warm (engine resident)
    return {"provider": "cuda", "init_ms": round(init_ms, 3), **timing}


def _bench_trt(images, cache_dir: Path, fp16: bool, cold: bool) -> dict:
    from spica.local_runtime.ocr.rapidocr_trt_runtime import RapidOcrTrtEpRuntime

    if cold and cache_dir.exists():
        shutil.rmtree(cache_dir)
    t0 = time.perf_counter()
    runtime = RapidOcrTrtEpRuntime(
        fp16=fp16, engine_cache_dir=str(cache_dir), timing_cache=True
    )  # build + warmup (cold: TRT engine compile)
    init_ms = (time.perf_counter() - t0) * 1000.0
    timing = _time_ocr(runtime.recognize, images)
    return {
        "provider": "trt",
        "used_providers": runtime.used_providers,
        "fp16": fp16,
        "cold": cold,
        "init_ms": round(init_ms, 3),
        **timing,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="OCR benchmark: CUDA vs TRT, cold/warm.")
    parser.add_argument("--images", required=True)
    parser.add_argument("--cache-dir", default="artifacts/trt")
    parser.add_argument("--out", default="artifacts/benchmarks")
    parser.add_argument("--fp16", action="store_true", help="bench fp16 instead of fp32 (D4: default fp32)")
    args = parser.parse_args()

    from PIL import Image

    images_dir = Path(args.images)
    paths = sorted(p for p in images_dir.glob("*.png") if p.is_file())
    if not paths:
        parser.error(f"no .png images in {images_dir}")
    images = [p.read_bytes() for p in paths]

    cache_dir = Path(args.cache_dir)
    if not cache_dir.is_absolute():
        cache_dir = _REPO_ROOT / cache_dir

    results = {
        "images": len(images),
        "fp16": bool(args.fp16),
        "cuda": _bench_cuda(images),
        "trt_cold": _bench_trt(images, cache_dir, args.fp16, cold=True),
        "trt_warm": _bench_trt(images, cache_dir, args.fp16, cold=False),
    }

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    out_path = out_dir / f"ocr_bench_{stamp}.json"
    out_path.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")

    for key in ("cuda", "trt_cold", "trt_warm"):
        r = results[key]
        print(
            f"{key:9} init={r['init_ms']:>9.1f}ms  per_image_mean={r['per_image_ms_mean']:>8.2f}ms  "
            f"batch={r['batch_total_ms']:>9.1f}ms"
            + (f"  used={r.get('used_providers')}" if "used_providers" in r else "")
        )
    print(f"report: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

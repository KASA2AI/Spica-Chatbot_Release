"""OCR parity report executor (LOCAL_RUNTIME_PLAN §6.1 gate / §11 step 7 / cut 2).

Runs the parity harness on a REAL-machine reference image set: old ``rapidocr``
(CUDA) vs a new provider (``rapidocr_ort`` or, cut 2, ``rapidocr_trt_ep``),
character-level text diff + timings, and archives the report to
``artifacts/parity/ocr_<timestamp>.json`` (NOT in git, §7.3). That archived report
is the SOLE evidence gate (§6.1) for later switching the default provider /
removing the fallback -- this cut does NOT switch the default.

PARITY POLICY (§5): the bar is character-EXACT (verdict strict). Mismatching samples
are listed individually (old vs new) and NOT loosened away -- whether to accept a
non-100% match on real frames is the human's call from those samples. fp32 is the
cut-2 default (D4): confirm the TRT integration is correct before fp16.

MANUAL-ACCEPTANCE tool (needs a real OCR model + GPU + images), never CI (§6.5). Run:

  python -m scripts.local_runtime.verify_parity --images <dir> [--new rapidocr_trt_ep] [--fp16] [--out artifacts/parity]

ENV NAMES (§3.3): reads NONE.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from spica.adapters.ocr import (  # noqa: E402
    RapidOcrAdapter,
    RapidOcrOrtAdapter,
    RapidOcrTrtEpAdapter,
)
from spica.local_runtime.parity import run_parity  # noqa: E402
from spica.local_runtime.parity.comparators import text_diff  # noqa: E402


def _load_reference_images(images_dir: Path) -> list[bytes]:
    paths = sorted(p for p in images_dir.glob("*.png") if p.is_file())
    return [p.read_bytes() for p in paths]


def _build_new_adapter(name: str, cache_dir: Path, fp16: bool):
    if name == "rapidocr_ort":
        return RapidOcrOrtAdapter()
    if name == "rapidocr_trt_ep":
        return RapidOcrTrtEpAdapter(fp16=fp16, engine_cache_dir=str(cache_dir), timing_cache=True)
    raise SystemExit(f"unknown --new provider: {name}")


def main() -> int:
    parser = argparse.ArgumentParser(description="OCR parity: rapidocr vs a new provider.")
    parser.add_argument("--images", required=True, help="Directory of reference PNG images.")
    parser.add_argument(
        "--new", default="rapidocr_trt_ep", choices=["rapidocr_ort", "rapidocr_trt_ep"]
    )
    parser.add_argument("--cache-dir", default="artifacts/trt", help="TRT engine cache dir.")
    parser.add_argument("--fp16", action="store_true", help="TRT fp16 (D4 default is fp32).")
    parser.add_argument("--out", default="artifacts/parity", help="Report output directory.")
    args = parser.parse_args()

    images_dir = Path(args.images)
    if not images_dir.is_dir():
        parser.error(f"--images is not a directory: {images_dir}")
    reference = _load_reference_images(images_dir)
    if not reference:
        parser.error(f"no .png reference images found in {images_dir}")

    cache_dir = Path(args.cache_dir)
    if not cache_dir.is_absolute():
        cache_dir = _REPO_ROOT / cache_dir

    old = RapidOcrAdapter()
    new = _build_new_adapter(args.new, cache_dir, args.fp16)
    if hasattr(new, "warmup"):
        new.warmup()  # surface TRT build / fallback before timing

    report = run_parity(
        reference,
        run_old=lambda png: old.recognize(png).text,
        run_new=lambda png: new.recognize(png).text,
        comparator=text_diff,
        model="ocr",
        provider_old="rapidocr",
        provider_new=args.new,
    )

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    out_path = out_dir / f"ocr_{args.new}_{stamp}.json"
    out_path.write_text(json.dumps(report.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")

    agg = report.aggregate
    print(f"provider_new: {args.new}  fp16: {args.fp16}")
    print(f"verdict: {report.verdict}")
    print(f"match_rate: {agg['match_rate']:.4f}  max_error: {agg['max_error']:.4f}")
    print(f"mean_old_ms: {agg['mean_old_ms']:.2f}  mean_new_ms: {agg['mean_new_ms']:.2f}")

    mismatches = [item for item in report.per_input if not item.match]
    if mismatches:
        print(f"\n{len(mismatches)} MISMATCH sample(s) (NOT loosened -- human review, §5):")
        for item in mismatches:
            print(f"  [{item.idx}] err={item.error_value:.3f}")
            print(f"      old: {item.old!r}")
            print(f"      new: {item.new!r}")
    print(f"\nreport: {out_path}")
    return 0 if report.is_pass else 1


if __name__ == "__main__":
    raise SystemExit(main())

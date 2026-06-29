"""OCR parity report executor (LOCAL_RUNTIME_PLAN §6.1 gate / §11 step 7).

Runs the parity harness on a REAL-machine reference image set: old ``rapidocr``
vs new ``rapidocr_ort``, character-level text diff + timings, and archives the
report to ``artifacts/parity/ocr_<timestamp>.json`` (NOT in git, §7.3). That
archived report is the SOLE evidence gate (§6.1) for later switching the default
provider / removing the fallback -- this cut does NOT switch the default.

This is a MANUAL-ACCEPTANCE tool (needs a real OCR model + real images), never run
in CI (§6.5 -- CI uses synthetic stubs in test_parity_harness). Run:

  python -m scripts.local_runtime.verify_parity --images <dir-of-pngs> [--out artifacts/parity]

ENV NAMES (§3.3): reads NONE. As a ``scripts/`` CLI it MAY read env, but doesn't;
declare any future env knob here to keep the surface documented in one place.
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

from spica.adapters.ocr import RapidOcrAdapter, RapidOcrOrtAdapter  # noqa: E402
from spica.local_runtime.parity import run_parity  # noqa: E402
from spica.local_runtime.parity.comparators import text_diff  # noqa: E402


def _load_reference_images(images_dir: Path) -> list[bytes]:
    paths = sorted(p for p in images_dir.glob("*.png") if p.is_file())
    return [p.read_bytes() for p in paths]


def main() -> int:
    parser = argparse.ArgumentParser(description="OCR parity: rapidocr vs rapidocr_ort.")
    parser.add_argument("--images", required=True, help="Directory of reference PNG images.")
    parser.add_argument("--out", default="artifacts/parity", help="Report output directory.")
    args = parser.parse_args()

    images_dir = Path(args.images)
    if not images_dir.is_dir():
        parser.error(f"--images is not a directory: {images_dir}")
    reference = _load_reference_images(images_dir)
    if not reference:
        parser.error(f"no .png reference images found in {images_dir}")

    old = RapidOcrAdapter()
    new = RapidOcrOrtAdapter()
    report = run_parity(
        reference,
        run_old=lambda png: old.recognize(png).text,
        run_new=lambda png: new.recognize(png).text,
        comparator=text_diff,
        model="ocr",
        provider_old="rapidocr",
        provider_new="rapidocr_ort",
    )

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    out_path = out_dir / f"ocr_{stamp}.json"
    out_path.write_text(json.dumps(report.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")

    agg = report.aggregate
    print(f"verdict: {report.verdict}")
    print(f"match_rate: {agg['match_rate']:.4f}  max_error: {agg['max_error']:.4f}")
    print(f"mean_old_ms: {agg['mean_old_ms']:.2f}  mean_new_ms: {agg['mean_new_ms']:.2f}")
    print(f"report: {out_path}")
    return 0 if report.is_pass else 1


if __name__ == "__main__":
    raise SystemExit(main())

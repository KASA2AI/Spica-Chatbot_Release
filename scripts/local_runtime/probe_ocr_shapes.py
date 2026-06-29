"""Measure det/cls/rec ONNX input shapes (LOCAL_RUNTIME_PLAN cut 2, §2 measurement).

The profile decision (D3 / §4) must be driven by REAL data, not presumption. This
records, per stage:
  1. each ONNX session's declared input metadata (which dims are dynamic);
  2. the ACTUAL preprocessed tensor shapes fed to each session over a real image set.
Then it prints the distinct-shape count + histogram per stage so you can decide
whether explicit TRT shape profiles are needed (many shapes) or the engine cache
suffices (few shapes).

Manual / real-machine tool (needs the real RapidOCR model). NOT CI. Run:

  python -m scripts.local_runtime.probe_ocr_shapes --images <dir-of-pngs> [--out artifacts/parity]

ENV NAMES (§3.3): reads NONE. As a scripts/ CLI it MAY read env; declare any future
knob here to keep the surface documented in one place.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def _session_input_meta(session) -> list[dict]:
    return [{"name": v.name, "shape": list(v.shape)} for v in session.session.get_inputs()]


def main() -> int:
    parser = argparse.ArgumentParser(description="Probe RapidOCR det/cls/rec input shapes.")
    parser.add_argument("--images", required=True, help="Directory of reference PNG images.")
    parser.add_argument("--out", default="artifacts/parity", help="Report output directory.")
    args = parser.parse_args()

    import numpy as np
    from PIL import Image
    import rapidocr_onnxruntime.utils.infer_engine as infer_engine
    from rapidocr_onnxruntime import RapidOCR

    images_dir = Path(args.images)
    paths = sorted(p for p in images_dir.glob("*.png") if p.is_file())
    if not paths:
        parser.error(f"no .png images in {images_dir}")

    engine = RapidOCR(det_use_cuda=True, cls_use_cuda=True, rec_use_cuda=True)
    stage_by_id = {
        id(engine.text_det.infer): "det",
        id(engine.text_cls.infer): "cls",
        id(engine.text_rec.session): "rec",
    }
    static_meta = {
        "det": _session_input_meta(engine.text_det.infer),
        "cls": _session_input_meta(engine.text_cls.infer),
        "rec": _session_input_meta(engine.text_rec.session),
    }
    observed: dict[str, Counter] = {"det": Counter(), "cls": Counter(), "rec": Counter()}

    # Class-level patch of __call__ (special methods resolve on the type, not the
    # instance); attribute the call to a stage by the session's identity.
    original_call = infer_engine.OrtInferSession.__call__

    def recording_call(self, input_content):
        stage = stage_by_id.get(id(self))
        if stage is not None:
            observed[stage][tuple(int(d) for d in np.asarray(input_content).shape)] += 1
        return original_call(self, input_content)

    infer_engine.OrtInferSession.__call__ = recording_call
    try:
        for path in paths:
            engine(np.asarray(Image.open(path).convert("RGB")))
    finally:
        infer_engine.OrtInferSession.__call__ = original_call

    report = {
        "images": len(paths),
        "static_input_meta": static_meta,
        "observed": {
            stage: {
                "distinct_shapes": len(counter),
                "histogram": [
                    {"shape": list(shape), "count": count}
                    for shape, count in counter.most_common()
                ],
            }
            for stage, counter in observed.items()
        },
    }

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    out_path = out_dir / f"shapes_{stamp}.json"
    out_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"images: {len(paths)}")
    for stage in ("det", "cls", "rec"):
        info = report["observed"][stage]
        print(f"  {stage}: {info['distinct_shapes']} distinct shape(s)")
        for entry in info["histogram"][:8]:
            print(f"      {tuple(entry['shape'])} x{entry['count']}")
    print(f"report: {out_path}")
    print(
        "\nprofile hint: few distinct det/rec shapes -> rely on engine cache (no explicit "
        "profile); many -> set trt_profile_min/opt/max + record in manifest dynamic_shapes (§4)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

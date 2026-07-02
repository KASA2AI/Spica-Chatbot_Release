"""Standalone RVC subprocess worker (LOCAL_RUNTIME_PLAN cut 3, Phase 1A).

Runs ONE RVC inference in an isolated process so the Applio import tree (cached
GLOBALLY in ``rvc._load_core`` -- ~4472 modules that persist in ``sys.modules``)
never lands in the main app process.

Deliberately MINIMAL: file-path runnable, stdlib-only imports at module load, and
it does NOT import the ``spica`` / ``agent_tools`` packages. It loads
``agent_tools/function_tools/song/rvc.py`` via ``spec_from_file_location`` (that
module is stdlib-only at import time), so ONE source of truth for the infer logic
is reused -- not copied -- and this worker can later run under a separate RVC env
(Phase 2) that lacks the spica / GPT-SoVITS deps.

Contract (Phase 1A):
  argv:  worker.py --request <request.json>
  request.json keys: rvc_module_path, input_vocal_path, output_vocal_path,
    model_path, index_path, applio_root, result_path, seed (optional),
    params{...} (the pipeline's ``_rvc_params`` dict, forwarded VERBATIM).
  On SUCCESS: ATOMICALLY publishes ``result_path`` (``{ok: true, output_path,
    side}``) via a temp file + ``os.replace`` as the LAST action -- so a partial
    write can never masquerade as success. That file is the ONLY success signal;
    ANY failure (bad module load, infer exception, ...) leaves NO result file and
    a non-zero exit + stderr traceback. The parent NEVER judges success by the
    wav alone (a crash can leave a half-written wav).

Applio / core print logs + tqdm bars to stdout, so the result MUST NOT be a bare
stdout JSON (it would be interleaved with that noise). Hence the result file.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from pathlib import Path


def _load_infer(rvc_module_path: str):
    """Load the stdlib-only ``rvc.py`` by file path (bypasses agent_tools/__init__)."""
    spec = importlib.util.spec_from_file_location("spica_rvc_infer", rvc_module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load rvc module by path: {rvc_module_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module.infer_spica_vocal


def _maybe_seed(seed) -> None:
    if seed is None:
        return
    import torch

    torch.manual_seed(int(seed))
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(int(seed))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="RVC subprocess worker (Phase 1A).")
    ap.add_argument("--request", required=True, help="path to the request JSON")
    args = ap.parse_args(argv)

    req = json.loads(Path(args.request).read_text(encoding="utf-8"))
    result_path = Path(req["result_path"])
    tmp_path = result_path.with_name(result_path.name + ".tmp")
    # Never trust a stale success signal (or a half-written tmp) from a prior run.
    for stale in (result_path, tmp_path):
        if stale.exists():
            stale.unlink()

    _maybe_seed(req.get("seed"))
    infer_spica_vocal = _load_infer(req["rvc_module_path"])
    output = infer_spica_vocal(
        input_vocal_path=req["input_vocal_path"],
        output_vocal_path=req["output_vocal_path"],
        model_path=req["model_path"],
        index_path=req.get("index_path"),
        applio_root=req["applio_root"],
        **(req.get("params") or {}),
    )

    # SUCCESS SIGNAL -- reached ONLY when inference returned. Written atomically:
    # a partial/crashed write leaves the .tmp, never a usable result.json.
    tmp_path.write_text(
        json.dumps({"ok": True, "output_path": str(output), "side": "subprocess"}, ensure_ascii=False),
        encoding="utf-8",
    )
    os.replace(tmp_path, result_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""RVC execution dispatch (LOCAL_RUNTIME_PLAN cut 3, Phase 1A).

``run_rvc`` routes ONE RVC inference to either:
  * ``in_process`` (the DEFAULT) -- calls ``rvc.infer_spica_vocal`` directly,
    byte-identical to the pre-cut behaviour; OR
  * ``subprocess`` -- spawns ``worker.py`` so the Applio import tree stays out of
    the caller process (the whole point of cut 3).

Phase 1A ships the seam with the default kept at ``in_process`` (zero behaviour
change). Flipping the default to ``subprocess`` is Phase 1B (separate review).

Success of a subprocess run is judged by the worker's ``result.json`` (exists +
``ok``), NEVER by the output wav alone -- a crash can leave a half-written wav.
On failure ``run_rvc`` raises with the worker exit code, a stderr tail, the
result path, and whether the wav exists.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

_WORKER = Path(__file__).resolve().parent / "worker.py"
# repo_root/spica/local_runtime/rvc/driver.py -> parents[3] == repo root
_RVC_MODULE = (
    Path(__file__).resolve().parents[3] / "agent_tools" / "function_tools" / "song" / "rvc.py"
)
DEFAULT_TIMEOUT_SEC = 900


def run_rvc(
    *,
    input_vocal_path: str,
    output_vocal_path: str,
    model_path: str,
    index_path: str | None,
    applio_root: str,
    execution_mode: str = "in_process",
    worker_python: str | None = None,
    timeout_sec: float | None = None,
    seed: int | None = None,
    **params: Any,
) -> str:
    """Run RVC inference; return the output wav path. See module docstring."""
    if execution_mode != "subprocess":
        # Legacy default -- in-process, byte-identical to before the seam.
        from agent_tools.function_tools.song.rvc import infer_spica_vocal

        return str(
            infer_spica_vocal(
                input_vocal_path=input_vocal_path,
                output_vocal_path=output_vocal_path,
                model_path=model_path,
                index_path=index_path,
                applio_root=applio_root,
                **params,
            )
        )
    return _run_subprocess(
        input_vocal_path=input_vocal_path,
        output_vocal_path=output_vocal_path,
        model_path=model_path,
        index_path=index_path,
        applio_root=applio_root,
        worker_python=worker_python,
        timeout_sec=timeout_sec,
        seed=seed,
        params=params,
    )


def _run_subprocess(
    *,
    input_vocal_path: str,
    output_vocal_path: str,
    model_path: str,
    index_path: str | None,
    applio_root: str,
    worker_python: str | None,
    timeout_sec: float | None,
    seed: int | None,
    params: dict[str, Any],
) -> str:
    out = Path(output_vocal_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    req_path = out.with_name(out.name + ".rvc_request.json")
    result_path = out.with_name(out.name + ".rvc_result.json")
    for stale in (req_path, result_path):
        if stale.exists():
            stale.unlink()

    request = {
        "rvc_module_path": str(_RVC_MODULE),
        "input_vocal_path": input_vocal_path,
        "output_vocal_path": output_vocal_path,
        "model_path": model_path,
        "index_path": index_path,
        "applio_root": applio_root,
        "result_path": str(result_path),
        "seed": seed,
        "params": params,  # the pipeline's _rvc_params dict, forwarded VERBATIM
    }
    req_path.write_text(json.dumps(request, ensure_ascii=False), encoding="utf-8")

    python = worker_python or sys.executable
    proc = subprocess.run(
        [python, "-B", str(_WORKER), "--request", str(req_path)],
        capture_output=True,
        text=True,
        timeout=timeout_sec or DEFAULT_TIMEOUT_SEC,
    )

    wav_exists = Path(output_vocal_path).exists()
    stderr_tail = (proc.stderr or "").strip()[-800:]
    if not result_path.exists():
        raise RuntimeError(
            "RVC subprocess worker produced no result.json "
            f"(exit={proc.returncode}, wav_exists={wav_exists}, result={result_path}). "
            f"stderr tail:\n{stderr_tail}"
        )
    result = json.loads(result_path.read_text(encoding="utf-8"))
    if not result.get("ok"):
        raise RuntimeError(
            "RVC subprocess worker reported failure "
            f"(exit={proc.returncode}, wav_exists={wav_exists}, result={result}). "
            f"stderr tail:\n{stderr_tail}"
        )
    return str(result.get("output_path") or output_vocal_path)

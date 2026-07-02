"""RVC execution dispatch (LOCAL_RUNTIME_PLAN cut 3, Phase 1A).

``run_rvc`` routes ONE RVC inference to either:
  * ``in_process`` (the DEFAULT) -- calls ``rvc.infer_spica_vocal`` directly,
    byte-identical to the pre-cut behaviour; OR
  * ``subprocess`` -- spawns ``worker.py`` so the Applio import tree stays out of
    the caller process (the whole point of cut 3).

Phase 1A ships the seam with the default kept at ``in_process`` (zero behaviour
change). Flipping the default to ``subprocess`` is Phase 1B (separate review).

``execution_mode`` is validated -- an unknown / mistyped value raises rather than
silently falling back to in-process, so a bad app.yaml override can never lose
the isolation silently once the default flips.

EVERY subprocess failure path (timeout, launch error, non-zero exit, missing /
unparseable / partial result.json, ``ok=false``, ``ok=true`` but the wav is
missing) is funneled into ONE structured ``RuntimeError`` carrying the worker
returncode (or the timeout / exception type), ``timeout_sec``, ``result_path``,
``wav_exists``, and stdout / stderr tails -- so a real-machine failure is
diagnosable from the message alone. Success is judged by result.json (exists,
parseable, ``ok is True``) AND the output wav existing -- never the wav alone.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

_VALID_MODES = ("in_process", "subprocess")

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
    if execution_mode not in _VALID_MODES:
        raise ValueError(
            f"invalid RVC execution_mode {execution_mode!r}; "
            f"expected one of {_VALID_MODES} (no silent fallback)"
        )
    if execution_mode == "in_process":
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


def _tail(text: str | None) -> str:
    return (text or "").strip()[-800:]


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
    tmp_result = out.with_name(result_path.name + ".tmp")
    # No stale request / (partial) result / tmp may survive into this run.
    for stale in (req_path, result_path, tmp_result):
        if stale.exists():
            stale.unlink()

    timeout = timeout_sec or DEFAULT_TIMEOUT_SEC

    def _fail(reason: str, *, returncode: Any = None, stdout: str = "", stderr: str = "", detail: str = "") -> RuntimeError:
        wav_exists = Path(output_vocal_path).exists()
        return RuntimeError(
            f"RVC subprocess worker failed: {reason} "
            f"(returncode={returncode}, timeout_sec={timeout}, "
            f"result_path={result_path}, wav_exists={wav_exists})"
            + (f"\ndetail: {detail}" if detail else "")
            + f"\nstdout tail:\n{_tail(stdout)}"
            + f"\nstderr tail:\n{_tail(stderr)}"
        )

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
    try:
        proc = subprocess.run(
            [python, "-B", str(_WORKER), "--request", str(req_path)],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise _fail(
            "timed out", returncode="timeout",
            stdout=exc.stdout or "", stderr=exc.stderr or "",
            detail=f"TimeoutExpired after {timeout}s",
        ) from exc
    except Exception as exc:  # e.g. worker_python not found -> could not launch
        raise _fail("could not launch worker", detail=f"{type(exc).__name__}: {exc}") from exc

    if not result_path.exists():
        raise _fail("no result.json produced", returncode=proc.returncode,
                    stdout=proc.stdout, stderr=proc.stderr)

    try:
        result = json.loads(result_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, ValueError, OSError) as exc:
        raise _fail("result.json is unparseable / partial", returncode=proc.returncode,
                    stdout=proc.stdout, stderr=proc.stderr,
                    detail=f"{type(exc).__name__}: {exc}") from exc

    if not isinstance(result, dict) or result.get("ok") is not True:
        raise _fail("worker reported failure", returncode=proc.returncode,
                    stdout=proc.stdout, stderr=proc.stderr, detail=f"result={result}")

    output_path = str(result.get("output_path") or output_vocal_path)
    if not Path(output_path).exists():
        raise _fail("worker reported ok but the output wav is missing",
                    returncode=proc.returncode, stdout=proc.stdout, stderr=proc.stderr,
                    detail=f"output_path={output_path}")
    return output_path

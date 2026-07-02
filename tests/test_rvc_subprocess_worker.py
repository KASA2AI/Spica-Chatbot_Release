"""cut 3 Phase 1A: RVC subprocess worker + dispatch driver (+ hardening).

All mock-based -- NO GPU, NO real Applio. Covers: execution_mode validation
(loud fail on unknown values), dispatch routing, the worker's ATOMIC result.json
contract (fake core loaded by file path), the unified subprocess error envelope
across every failure path (timeout / launch error / non-zero exit / missing /
partial / ok=false / ok=true-but-no-wav result.json / bad module load), and the
parent-process isolation invariant. Real subprocess-vs-in-process wav parity is a
machine gate, not covered here.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from spica.local_runtime.rvc import worker as rvc_worker
from spica.local_runtime.rvc import driver as rvc_driver
from spica.local_runtime.rvc.driver import run_rvc


_FAKE_RVC_OK = '''
from pathlib import Path
def infer_spica_vocal(*, input_vocal_path, output_vocal_path, model_path,
                      index_path, applio_root, **kwargs):
    Path(output_vocal_path).write_bytes(b"RIFFfakewav")
    return output_vocal_path
'''

_FAKE_RVC_RAISES = '''
def infer_spica_vocal(*, input_vocal_path, output_vocal_path, model_path,
                      index_path, applio_root, **kwargs):
    raise RuntimeError("boom inside applio")
'''


def _base_kwargs(out: Path) -> dict:
    return {
        "input_vocal_path": "/in/vocal.wav",
        "output_vocal_path": str(out),
        "model_path": "/model.pth",
        "index_path": "/model.index",
        "applio_root": "/applio",
    }


def _read_req(argv):
    return json.loads(Path(argv[argv.index("--request") + 1]).read_text(encoding="utf-8"))


def _proc(returncode=0, stdout="", stderr=""):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def _fake_worker_ok(argv, **kwargs):
    """A worker that succeeds: creates the wav AND publishes result.json."""
    req = _read_req(argv)
    Path(req["output_vocal_path"]).write_bytes(b"RIFFfakewav")
    Path(req["result_path"]).write_text(
        json.dumps({"ok": True, "output_path": req["output_vocal_path"], "side": "subprocess"}),
        encoding="utf-8",
    )
    return _proc(0, stdout="applio: tqdm 100%", stderr="")


# -- execution_mode validation (loud fail) -----------------------------------------


@pytest.mark.parametrize("bad", ["SUBPROCESS", "subproces", "in-process", "inprocess", "", "thread"])
def test_unknown_execution_mode_raises_value_error(tmp_path, monkeypatch, bad):
    # must reject BEFORE touching infer or subprocess
    monkeypatch.setattr(
        "agent_tools.function_tools.song.rvc.infer_spica_vocal",
        lambda **kw: pytest.fail("infer must not run on an invalid mode"),
    )
    monkeypatch.setattr(rvc_driver.subprocess, "run",
                        lambda *a, **k: pytest.fail("subprocess must not run on an invalid mode"))
    with pytest.raises(ValueError, match="execution_mode"):
        run_rvc(execution_mode=bad, **_base_kwargs(tmp_path / "rvc.wav"))


# -- dispatch routing --------------------------------------------------------------


def test_dispatch_in_process_calls_infer_verbatim(tmp_path, monkeypatch):
    seen = {}
    monkeypatch.setattr(
        "agent_tools.function_tools.song.rvc.infer_spica_vocal",
        lambda **kw: seen.update(kw) or kw["output_vocal_path"],
    )
    out = tmp_path / "rvc.wav"
    result = run_rvc(execution_mode="in_process", f0_method="rmvpe", sid=0, **_base_kwargs(out))
    assert result == str(out)
    assert seen["f0_method"] == "rmvpe" and seen["sid"] == 0 and seen["applio_root"] == "/applio"
    # in_process forwards none of the seam-only kwargs into infer
    assert not ({"execution_mode", "worker_python", "seed"} & set(seen))


def test_dispatch_default_is_in_process(tmp_path, monkeypatch):
    called = {}
    monkeypatch.setattr(
        "agent_tools.function_tools.song.rvc.infer_spica_vocal",
        lambda **kw: called.setdefault("hit", True) or kw["output_vocal_path"],
    )
    monkeypatch.setattr(rvc_driver.subprocess, "run",
                        lambda *a, **k: pytest.fail("subprocess must not run on the in_process default"))
    run_rvc(**_base_kwargs(tmp_path / "rvc.wav"))
    assert called.get("hit")


def test_dispatch_subprocess_success_reads_result_json(tmp_path, monkeypatch):
    monkeypatch.setattr(rvc_driver.subprocess, "run", _fake_worker_ok)
    out = tmp_path / "rvc.wav"
    result = run_rvc(execution_mode="subprocess", seed=1234, f0_method="rmvpe", **_base_kwargs(out))
    assert result == str(out) and out.exists()
    req = json.loads((tmp_path / "rvc.wav.rvc_request.json").read_text(encoding="utf-8"))
    assert req["seed"] == 1234 and req["params"]["f0_method"] == "rmvpe"
    assert req["rvc_module_path"].endswith("agent_tools/function_tools/song/rvc.py")


# -- unified subprocess error envelope (every failure path) -------------------------


def test_envelope_timeout(tmp_path, monkeypatch):
    def fake_timeout(argv, **kwargs):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=kwargs.get("timeout"),
                                        output="stdout so far", stderr="stderr so far")

    monkeypatch.setattr(rvc_driver.subprocess, "run", fake_timeout)
    with pytest.raises(RuntimeError) as exc:
        run_rvc(execution_mode="subprocess", timeout_sec=0.01, **_base_kwargs(tmp_path / "rvc.wav"))
    msg = str(exc.value)
    assert "timed out" in msg and "timeout_sec=0.01" in msg
    assert "result_path" in msg and "wav_exists" in msg
    assert "stdout so far" in msg and "stderr so far" in msg


def test_envelope_launch_failure(tmp_path, monkeypatch):
    def fake_launch_fail(argv, **kwargs):
        raise FileNotFoundError("no such interpreter: /bad/python")

    monkeypatch.setattr(rvc_driver.subprocess, "run", fake_launch_fail)
    with pytest.raises(RuntimeError) as exc:
        run_rvc(execution_mode="subprocess", worker_python="/bad/python",
                **_base_kwargs(tmp_path / "rvc.wav"))
    assert "could not launch worker" in str(exc.value) and "FileNotFoundError" in str(exc.value)


def test_envelope_nonzero_exit_no_result(tmp_path, monkeypatch):
    monkeypatch.setattr(rvc_driver.subprocess, "run",
                        lambda argv, **k: _proc(1, stderr="Traceback: ImportError applio core"))
    with pytest.raises(RuntimeError) as exc:
        run_rvc(execution_mode="subprocess", **_base_kwargs(tmp_path / "rvc.wav"))
    msg = str(exc.value)
    assert "no result.json produced" in msg and "returncode=1" in msg and "applio core" in msg


def test_envelope_partial_result_json_not_naked_jsondecode(tmp_path, monkeypatch):
    def fake_partial(argv, **kwargs):
        req = _read_req(argv)
        Path(req["result_path"]).write_text('{"ok": true, "output', encoding="utf-8")  # truncated
        return _proc(0)

    monkeypatch.setattr(rvc_driver.subprocess, "run", fake_partial)
    with pytest.raises(RuntimeError) as exc:  # RuntimeError, NOT json.JSONDecodeError
        run_rvc(execution_mode="subprocess", **_base_kwargs(tmp_path / "rvc.wav"))
    assert "unparseable" in str(exc.value)


def test_envelope_result_ok_false(tmp_path, monkeypatch):
    def fake_ok_false(argv, **kwargs):
        req = _read_req(argv)
        Path(req["result_path"]).write_text(json.dumps({"ok": False}), encoding="utf-8")
        return _proc(3, stderr="partial failure")

    monkeypatch.setattr(rvc_driver.subprocess, "run", fake_ok_false)
    with pytest.raises(RuntimeError) as exc:
        run_rvc(execution_mode="subprocess", **_base_kwargs(tmp_path / "rvc.wav"))
    assert "worker reported failure" in str(exc.value) and "returncode=3" in str(exc.value)


def test_envelope_ok_true_but_wav_missing(tmp_path, monkeypatch):
    def fake_ok_no_wav(argv, **kwargs):
        req = _read_req(argv)  # result says ok, but the wav is never created
        Path(req["result_path"]).write_text(
            json.dumps({"ok": True, "output_path": req["output_vocal_path"]}), encoding="utf-8")
        return _proc(0)

    monkeypatch.setattr(rvc_driver.subprocess, "run", fake_ok_no_wav)
    with pytest.raises(RuntimeError) as exc:
        run_rvc(execution_mode="subprocess", **_base_kwargs(tmp_path / "rvc.wav"))
    assert "output wav is missing" in str(exc.value)


def test_subprocess_stale_result_is_cleared_before_run(tmp_path, monkeypatch):
    out = tmp_path / "rvc.wav"
    (tmp_path / "rvc.wav.rvc_result.json").write_text(
        json.dumps({"ok": True, "output_path": "/OLD/stale.wav"}), encoding="utf-8")
    (tmp_path / "rvc.wav.rvc_result.json.tmp").write_text("half", encoding="utf-8")
    monkeypatch.setattr(rvc_driver.subprocess, "run",
                        lambda argv, **k: _proc(1, stderr="worker died before writing result"))
    with pytest.raises(RuntimeError):  # must NOT read the stale success
        run_rvc(execution_mode="subprocess", **_base_kwargs(out))


# -- worker contract (real worker.main, fake core loaded by file path) -------------


def _write_request(tmp_path: Path, rvc_module: Path, out: Path) -> Path:
    req_path = out.with_name(out.name + ".rvc_request.json")
    req_path.write_text(json.dumps({
        "rvc_module_path": str(rvc_module),
        "input_vocal_path": "/in/vocal.wav",
        "output_vocal_path": str(out),
        "model_path": "/model.pth",
        "index_path": None,
        "applio_root": "/applio",
        "result_path": str(out.with_name(out.name + ".rvc_result.json")),
        "seed": None,
        "params": {"f0_method": "rmvpe", "sid": 0},
    }), encoding="utf-8")
    return req_path


def test_worker_atomically_publishes_result_on_success(tmp_path):
    rvc_module = tmp_path / "fake_rvc.py"
    rvc_module.write_text(_FAKE_RVC_OK, encoding="utf-8")
    out = tmp_path / "rvc.wav"
    req_path = _write_request(tmp_path, rvc_module, out)
    result_path = out.with_name(out.name + ".rvc_result.json")

    rc = rvc_worker.main(["--request", str(req_path)])

    assert rc == 0
    assert result_path.exists()
    assert not result_path.with_name(result_path.name + ".tmp").exists()  # tmp consumed by os.replace
    result = json.loads(result_path.read_text(encoding="utf-8"))
    assert result["ok"] is True and result["output_path"] == str(out)
    assert out.exists()  # fake core wrote the wav


def test_worker_no_result_json_when_infer_raises(tmp_path):
    rvc_module = tmp_path / "fake_rvc.py"
    rvc_module.write_text(_FAKE_RVC_RAISES, encoding="utf-8")
    out = tmp_path / "rvc.wav"
    req_path = _write_request(tmp_path, rvc_module, out)
    result_path = out.with_name(out.name + ".rvc_result.json")

    with pytest.raises(RuntimeError, match="boom inside applio"):
        rvc_worker.main(["--request", str(req_path)])
    assert not result_path.exists()  # no success signal on failure


def test_worker_bad_rvc_module_path_no_result(tmp_path):
    out = tmp_path / "rvc.wav"
    req_path = _write_request(tmp_path, tmp_path / "does_not_exist.py", out)
    result_path = out.with_name(out.name + ".rvc_result.json")

    with pytest.raises(Exception):  # load failure -> loud, no false success
        rvc_worker.main(["--request", str(req_path)])
    assert not result_path.exists()


def test_worker_clears_stale_result_and_tmp_at_start(tmp_path):
    rvc_module = tmp_path / "fake_rvc.py"
    rvc_module.write_text(_FAKE_RVC_OK, encoding="utf-8")
    out = tmp_path / "rvc.wav"
    req_path = _write_request(tmp_path, rvc_module, out)
    result_path = out.with_name(out.name + ".rvc_result.json")
    result_path.write_text(json.dumps({"ok": True, "output_path": "/OLD.wav"}), encoding="utf-8")
    result_path.with_name(result_path.name + ".tmp").write_text("half", encoding="utf-8")

    rvc_worker.main(["--request", str(req_path)])

    result = json.loads(result_path.read_text(encoding="utf-8"))
    assert result["output_path"] == str(out)  # fresh, not the stale /OLD.wav


# -- parent-process isolation invariant --------------------------------------------


def test_subprocess_dispatch_leaks_no_applio_into_parent(tmp_path, monkeypatch):
    monkeypatch.setattr(rvc_driver.subprocess, "run", _fake_worker_ok)
    modules_before = set(sys.modules)
    path_before = list(sys.path)

    run_rvc(execution_mode="subprocess", **_base_kwargs(tmp_path / "rvc.wav"))

    leaked = [m for m in set(sys.modules) - modules_before
              if "applio" in m.lower() or m == "spica_applio_core"]
    assert not leaked, f"subprocess dispatch leaked Applio modules into parent: {leaked}"
    assert "/applio" not in set(sys.path) - set(path_before)

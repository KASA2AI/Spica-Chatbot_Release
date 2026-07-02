"""cut 3 Phase 1A: RVC subprocess worker + dispatch driver.

All mock-based -- NO GPU, NO real Applio. Covers: dispatch routing (in_process
vs subprocess), the worker's result.json contract (fake core module loaded by
file path), the subprocess success signal being result.json (not the wav), and
the parent-process isolation invariant (a subprocess run pulls no Applio module
into the caller). Real subprocess-vs-in-process wav parity is a machine gate.
"""

from __future__ import annotations

import json
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


# -- dispatch: in_process (default) ------------------------------------------------


def test_dispatch_in_process_calls_infer_verbatim(tmp_path, monkeypatch):
    seen = {}

    def fake_infer(**kwargs):
        seen.update(kwargs)
        return kwargs["output_vocal_path"]

    monkeypatch.setattr("agent_tools.function_tools.song.rvc.infer_spica_vocal", fake_infer)
    out = tmp_path / "rvc.wav"
    result = run_rvc(execution_mode="in_process", f0_method="rmvpe", sid=0, **_base_kwargs(out))

    assert result == str(out)
    # params are forwarded verbatim; in_process passes no seed/worker_python through
    assert seen["f0_method"] == "rmvpe" and seen["sid"] == 0
    assert seen["applio_root"] == "/applio"
    assert "execution_mode" not in seen and "worker_python" not in seen and "seed" not in seen


def test_dispatch_default_is_in_process(tmp_path, monkeypatch):
    called = {}
    monkeypatch.setattr(
        "agent_tools.function_tools.song.rvc.infer_spica_vocal",
        lambda **kw: called.setdefault("hit", True) or kw["output_vocal_path"],
    )
    # no subprocess.run should ever fire on the default path
    monkeypatch.setattr(
        rvc_driver.subprocess, "run",
        lambda *a, **k: pytest.fail("subprocess must not run on the in_process default"),
    )
    run_rvc(**_base_kwargs(tmp_path / "rvc.wav"))
    assert called.get("hit")


# -- dispatch: subprocess (mocked) -------------------------------------------------


def _fake_worker_run_ok(argv, **kwargs):
    req = json.loads(Path(argv[argv.index("--request") + 1]).read_text(encoding="utf-8"))
    Path(req["result_path"]).write_text(
        json.dumps({"ok": True, "output_path": req["output_vocal_path"], "side": "subprocess"}),
        encoding="utf-8",
    )
    return SimpleNamespace(returncode=0, stderr="")


def test_dispatch_subprocess_success_reads_result_json(tmp_path, monkeypatch):
    monkeypatch.setattr(rvc_driver.subprocess, "run", _fake_worker_run_ok)
    out = tmp_path / "rvc.wav"
    result = run_rvc(execution_mode="subprocess", seed=1234, f0_method="rmvpe", **_base_kwargs(out))
    assert result == str(out)
    # request carried the full param set + seed + the rvc module path, verbatim
    req = json.loads((tmp_path / "rvc.wav.rvc_request.json").read_text(encoding="utf-8"))
    assert req["seed"] == 1234
    assert req["params"]["f0_method"] == "rmvpe"
    assert req["rvc_module_path"].endswith("agent_tools/function_tools/song/rvc.py")


def test_dispatch_subprocess_no_result_json_raises(tmp_path, monkeypatch):
    def fake_run(argv, **kwargs):
        return SimpleNamespace(returncode=1, stderr="traceback: applio exploded")

    monkeypatch.setattr(rvc_driver.subprocess, "run", fake_run)
    with pytest.raises(RuntimeError) as exc:
        run_rvc(execution_mode="subprocess", **_base_kwargs(tmp_path / "rvc.wav"))
    msg = str(exc.value)
    assert "no result.json" in msg and "exit=1" in msg and "applio exploded" in msg


def test_dispatch_subprocess_result_not_ok_raises(tmp_path, monkeypatch):
    def fake_run(argv, **kwargs):
        req = json.loads(Path(argv[argv.index("--request") + 1]).read_text(encoding="utf-8"))
        Path(req["result_path"]).write_text(json.dumps({"ok": False}), encoding="utf-8")
        return SimpleNamespace(returncode=3, stderr="partial failure")

    monkeypatch.setattr(rvc_driver.subprocess, "run", fake_run)
    with pytest.raises(RuntimeError) as exc:
        run_rvc(execution_mode="subprocess", **_base_kwargs(tmp_path / "rvc.wav"))
    assert "reported failure" in str(exc.value) and "exit=3" in str(exc.value)


def test_subprocess_stale_result_is_cleared_before_run(tmp_path, monkeypatch):
    out = tmp_path / "rvc.wav"
    stale = tmp_path / "rvc.wav.rvc_result.json"
    stale.write_text(json.dumps({"ok": True, "output_path": "/OLD/stale.wav"}), encoding="utf-8")

    def fake_run(argv, **kwargs):  # worker does NOT write a fresh result -> stale must be gone
        return SimpleNamespace(returncode=1, stderr="worker died before writing result")

    monkeypatch.setattr(rvc_driver.subprocess, "run", fake_run)
    with pytest.raises(RuntimeError):
        run_rvc(execution_mode="subprocess", **_base_kwargs(out))  # not the stale success


# -- worker contract (real worker.main, fake core loaded by file path) -------------


def _write_request(tmp_path: Path, rvc_module: Path, out: Path) -> Path:
    req = {
        "rvc_module_path": str(rvc_module),
        "input_vocal_path": "/in/vocal.wav",
        "output_vocal_path": str(out),
        "model_path": "/model.pth",
        "index_path": None,
        "applio_root": "/applio",
        "result_path": str(out.with_name(out.name + ".rvc_result.json")),
        "seed": None,
        "params": {"f0_method": "rmvpe", "sid": 0},
    }
    req_path = out.with_name(out.name + ".rvc_request.json")
    req_path.write_text(json.dumps(req), encoding="utf-8")
    return req_path


def test_worker_writes_result_json_on_success(tmp_path):
    rvc_module = tmp_path / "fake_rvc.py"
    rvc_module.write_text(_FAKE_RVC_OK, encoding="utf-8")
    out = tmp_path / "rvc.wav"
    req_path = _write_request(tmp_path, rvc_module, out)

    rc = rvc_worker.main(["--request", str(req_path)])

    assert rc == 0
    result_path = out.with_name(out.name + ".rvc_result.json")
    assert result_path.exists()
    result = json.loads(result_path.read_text(encoding="utf-8"))
    assert result["ok"] is True and result["output_path"] == str(out)
    assert out.exists()  # the fake core wrote the wav


def test_worker_no_result_json_when_infer_raises(tmp_path):
    rvc_module = tmp_path / "fake_rvc.py"
    rvc_module.write_text(_FAKE_RVC_RAISES, encoding="utf-8")
    out = tmp_path / "rvc.wav"
    req_path = _write_request(tmp_path, rvc_module, out)
    result_path = out.with_name(out.name + ".rvc_result.json")

    with pytest.raises(RuntimeError, match="boom inside applio"):
        rvc_worker.main(["--request", str(req_path)])

    assert not result_path.exists()  # no success signal on failure


# -- parent-process isolation invariant --------------------------------------------


def test_subprocess_dispatch_leaks_no_applio_into_parent(tmp_path, monkeypatch):
    monkeypatch.setattr(rvc_driver.subprocess, "run", _fake_worker_run_ok)
    modules_before = set(sys.modules)
    path_before = list(sys.path)

    run_rvc(execution_mode="subprocess", applio_root=str(tmp_path / "applio"),
            input_vocal_path="/in.wav", output_vocal_path=str(tmp_path / "rvc.wav"),
            model_path="/m.pth", index_path=None)

    new_modules = set(sys.modules) - modules_before
    leaked = [m for m in new_modules if "applio" in m.lower() or m == "spica_applio_core"]
    assert not leaked, f"subprocess dispatch leaked Applio modules into parent: {leaked}"
    assert str(tmp_path / "applio") not in set(sys.path) - set(path_before)

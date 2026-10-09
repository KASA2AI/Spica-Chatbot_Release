"""Exercise the source launcher with PowerShell, without launching Qt or Conda."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.fixture
def launcher(tmp_path):
    powershell = shutil.which("powershell") or shutil.which("pwsh")
    if not powershell:
        pytest.skip("PowerShell is not installed")
    root = tmp_path / "桌宠 [preview]"
    script = root / "scripts/windows/run_spica.ps1"
    script.parent.mkdir(parents=True)
    shutil.copyfile(Path(__file__).resolve().parents[1] / "scripts/windows/run_spica.ps1", script)
    (root / "webui_qt.py").write_text(
        "import json, pathlib, sys\n"
        "print(json.dumps([str(pathlib.Path.cwd()), sys.argv[1:], sys.flags.utf8_mode]))\n"
        "raise SystemExit(7)\n", encoding="utf-8",
    )
    return powershell, root, script


def test_explicit_python_preserves_cwd_arguments_utf8_and_exit_code(launcher, tmp_path):
    powershell, root, script = launcher
    result = subprocess.run(
        [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script),
         "-PythonExe", sys.executable, "-platform", "offscreen"],
        cwd=tmp_path, capture_output=True, encoding="utf-8", timeout=30,
    )
    assert result.returncode == 7, result.stderr
    assert json.loads(result.stdout) == [str(root), ["-platform", "offscreen"], 1]


def test_default_conda_matches_readme_and_propagates_failure(launcher, tmp_path):
    powershell, _, script = launcher
    # Shadow Conda within this test process only; no environment is changed.
    quoted = str(script).replace("'", "''")
    result = subprocess.run(
        [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command",
         "function conda { ConvertTo-Json -InputObject @($args) -Compress; $global:LASTEXITCODE = 9 }; "
         f"& '{quoted}'; exit $LASTEXITCODE"],
        cwd=tmp_path, capture_output=True, encoding="utf-8", timeout=30,
    )
    assert result.returncode == 9, result.stderr
    assert json.loads(result.stdout) == ["run", "-n", "spica", "--no-capture-output", "python", "-X", "utf8", "webui_qt.py"]


def test_missing_python_reports_failure(launcher, tmp_path):
    powershell, _, script = launcher
    result = subprocess.run(
        [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script),
         "-PythonExe", str(tmp_path / "missing-python.exe")],
        cwd=tmp_path, capture_output=True, encoding="utf-8", timeout=30,
    )
    assert result.returncode != 0
    assert "Spica Chatbot could not start" in result.stderr


def test_launcher_enables_utf8_for_python_workers(launcher, tmp_path):
    powershell, root, script = launcher
    (root / "webui_qt.py").write_text(
        "import subprocess, sys\n"
        "raise SystemExit(subprocess.call([sys.executable, '-c', "
        "'import sys; print(sys.flags.utf8_mode)']))\n", encoding="utf-8",
    )
    environment = {**os.environ, "PYTHONUTF8": "0"}
    result = subprocess.run(
        [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script),
         "-PythonExe", sys.executable], cwd=tmp_path, env=environment,
        capture_output=True, encoding="utf-8", timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "1"

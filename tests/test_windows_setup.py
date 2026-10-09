from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from scripts.windows import setup_environment
from scripts import setup_qwen_asr


def test_plan_does_not_install_or_create_environments(monkeypatch, capsys):
    monkeypatch.setattr(setup_environment.sys, "platform", "win32")
    monkeypatch.setattr(setup_environment.subprocess, "run", lambda *_a, **_k: pytest.fail("plan must not execute"))
    assert setup_environment.main(["--profile", "full"]) == 0
    output = capsys.readouterr().out
    assert sys.executable in output and "Plan only" in output
    assert "venv" not in output and "conda create" not in output


def test_failed_install_stops_before_provider_removal(monkeypatch):
    monkeypatch.setattr(setup_environment.sys, "platform", "win32")
    monkeypatch.setattr(setup_environment.sys, "base_prefix", "/different-base")
    seen = []

    def run(command, **_):
        seen.append(command)
        if len(seen) == 2:
            raise subprocess.CalledProcessError(1, command)

    monkeypatch.setattr(setup_environment.subprocess, "run", run)
    with pytest.raises(subprocess.CalledProcessError):
        setup_environment.main(["--profile", "full", "--install"])
    assert len(seen) == 2
    assert all(command[0] == sys.executable and "uninstall" not in command for command in seen)


def test_base_install_refuses_to_overwrite_existing_gpu_provider(monkeypatch):
    monkeypatch.setattr(setup_environment.sys, "platform", "win32")
    monkeypatch.setattr(setup_environment.sys, "base_prefix", "/different-base")
    monkeypatch.setattr(setup_environment.metadata, "version", lambda _name: "1.26.0")
    monkeypatch.setattr(setup_environment.subprocess, "run", lambda *_a, **_k: pytest.fail("must preserve GPU environment"))
    with pytest.raises(SystemExit) as caught:
        setup_environment.main(["--profile", "base", "--install"])
    assert caught.value.code == 2


def test_windows_qwen_reuses_current_python_without_installing(monkeypatch, tmp_path, capsys):
    import importlib.metadata
    monkeypatch.setattr(setup_qwen_asr.sys, "platform", "win32")
    monkeypatch.setattr(setup_qwen_asr, "ROOT", tmp_path)
    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(cuda=SimpleNamespace(is_available=lambda: True)))
    monkeypatch.setattr(importlib.metadata, "version", lambda name: {
        "qwen-asr": "0.0.6", "silero-vad": "6.0.0", "transformers": "4.57.6",
    }[name])
    monkeypatch.setattr(setup_qwen_asr.subprocess, "run", lambda *_a, **_k: pytest.fail("no installation or download requested"))
    monkeypatch.setattr(setup_qwen_asr.venv, "EnvBuilder", lambda **_k: pytest.fail("no second Windows environment"))
    assert setup_qwen_asr.main(["--model-dir", str(tmp_path / "model")]) == 0
    assert f"Qwen Python: {sys.executable}" in capsys.readouterr().out
    assert not (tmp_path / ".venv-qwen-asr").exists()


def test_linux_qwen_keeps_separate_environment(monkeypatch, tmp_path):
    monkeypatch.setattr(setup_qwen_asr.sys, "platform", "linux")
    monkeypatch.setattr(setup_qwen_asr, "ROOT", tmp_path)
    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(cuda=SimpleNamespace(is_available=lambda: True)))
    created, commands = [], []
    monkeypatch.setattr(setup_qwen_asr.venv, "EnvBuilder", lambda **kw: SimpleNamespace(create=lambda p: created.append((p, kw))))
    monkeypatch.setattr(setup_qwen_asr.subprocess, "run", lambda argv, **_: commands.append(argv))
    assert setup_qwen_asr.main([]) == 0
    assert created == [(tmp_path / ".venv-qwen-asr", {"with_pip": True, "system_site_packages": True})]
    assert commands[0][0] == str(tmp_path / ".venv-qwen-asr/bin/python")
    assert commands[0][0] != sys.executable

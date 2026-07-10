"""StartupWarmupWorker terminal-signal contract (2026-07 review P2).

Pins:
- finished_ok / failed fire exactly ONCE, after the WHOLE warmup returns
  (historically every stage's "ready" fired finished_ok, so dangling-session
  recovery started after the FIRST stage while STT warmup was still running);
- the failure terminal state carries the FIRST error message (a later stage's
  success text must not mask an earlier failure);
- an unexpected exception from host.warmup still emits failed (the terminal
  signal is guaranteed, so recovery chained on finished/failed always runs).

Signals are delivered synchronously (direct connection, run() called inline).
"""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from ui.workers.startup_warmup_worker import StartupWarmupWorker  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _worker_with_stages(qapp, stages):
    def warmup(on_progress):
        for stage, message in stages:
            on_progress(stage, message)

    worker = StartupWarmupWorker(SimpleNamespace(warmup=warmup))
    events = {"status": [], "ok": [], "failed": []}
    worker.status_changed.connect(events["status"].append)
    worker.finished_ok.connect(events["ok"].append)
    worker.failed.connect(events["failed"].append)
    return worker, events


def test_all_ready_emits_finished_ok_once_at_the_end(qapp):
    worker, events = _worker_with_stages(qapp, [
        ("initializing", "LLM ok"),
        ("ready", "TTS ready"),
        ("ready", "STT ready"),
    ])
    worker.run()
    assert events["ok"] == ["STT ready"]  # exactly once, AFTER both stages
    assert events["failed"] == []
    assert events["status"] == ["LLM ok", "TTS ready", "STT ready"]


def test_stage_ready_does_not_fire_terminal_early(qapp):
    # The old bug: TTS's "ready" fired finished_ok while STT was still warming.
    fired_at = []

    def warmup(on_progress):
        on_progress("ready", "TTS ready")
        fired_at.append(len(terminal))  # how many terminal signals so far
        on_progress("ready", "STT ready")

    worker = StartupWarmupWorker(SimpleNamespace(warmup=warmup))
    terminal = []
    worker.finished_ok.connect(terminal.append)
    worker.failed.connect(terminal.append)
    worker.run()
    assert fired_at == [0]  # nothing terminal between the two stages
    assert terminal == ["STT ready"]


def test_first_error_is_preserved_over_later_success(qapp):
    # 复现 review 场景 A: TTS error 后 STT ready -- 终态必须是 failed(TTS boom),
    # 不能被成功文案覆盖成 failed("STT ready")。
    worker, events = _worker_with_stages(qapp, [
        ("error", "TTS boom"),
        ("ready", "STT ready"),
    ])
    worker.run()
    assert events["failed"] == ["TTS boom"]
    assert events["ok"] == []


def test_warmup_exception_still_emits_failed(qapp):
    # 复现 review 场景 B: host.warmup 抛异常 -- 终态信号必须仍然发出,
    # 否则 dangling recovery 永不启动。
    def warmup(on_progress):
        raise RuntimeError("unexpected warmup crash")

    worker = StartupWarmupWorker(SimpleNamespace(warmup=warmup))
    events = {"ok": [], "failed": []}
    worker.finished_ok.connect(events["ok"].append)
    worker.failed.connect(events["failed"].append)
    worker.run()
    assert events["ok"] == []
    assert len(events["failed"]) == 1
    assert "unexpected warmup crash" in events["failed"][0]

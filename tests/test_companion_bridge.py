"""Phase 6 + stage 3: CompanionEventBridge (Qt offscreen) -- sink(event) emits the
signal carrying the RuntimeEvent; the GalgameController dispatches OCR events,
drives the status label / 🎮 active state from status events, and RESETS both on
every failure path (hard rule)."""

import os
import time
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from spica.core.companion_events import (  # noqa: E402
    GalgameBindFailedEvent,
    GalgameErrorEvent,
    GalgameOcrPreviewReadyEvent,
    GalgameOcrTestResultEvent,
    GalgameStatusChangedEvent,
    GalgameSummaryDoneEvent,
    GalgameSummaryStartedEvent,
    GalgameWindowLostEvent,
    GalgameWindowRecoveredEvent,
)
from ui.controllers.companion_event_bridge import CompanionEventBridge  # noqa: E402
from ui.controllers.galgame_controller import (  # noqa: E402
    GalgameController,
    selection_to_physical_rect,
)


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def test_sink_emits_companion_event(qapp):
    bridge = CompanionEventBridge()
    received = []
    bridge.companion_event.connect(received.append)
    event = GalgameOcrTestResultEvent(dialog_text="こんにちは")
    bridge.sink(event)
    assert received == [event]
    assert received[0].dialog_text == "こんにちは"


def test_controller_dispatches_ocr_events(qapp):
    bridge = CompanionEventBridge()
    controller = GalgameController(bridge)
    bridge.sink(GalgameOcrPreviewReadyEvent(region="dialog", image_png=b"", width=10, height=5, suspect_blank=True))
    bridge.sink(GalgameOcrTestResultEvent(dialog_text="テスト"))
    assert controller._preview is not None  # preview widget created on the GUI thread
    assert controller._preview._text.toPlainText() == "テスト"


def _wired_controller(bridge):
    """Stage 3: a dispatch-mode controller with recording callbacks."""
    statuses, actives, toasts = [], [], []
    controller = GalgameController(
        bridge,
        set_status=statuses.append,
        set_companion_active=actives.append,
        toast=toasts.append,
    )
    return controller, statuses, actives, toasts


def test_status_legs_drive_label_and_active_state(qapp):
    bridge = CompanionEventBridge()
    controller, statuses, actives, _toasts = _wired_controller(bridge)

    bridge.sink(GalgameStatusChangedEvent(state="playing"))
    assert actives[-1] is True
    assert "陪玩中" in statuses[-1]

    bridge.sink(GalgameWindowLostEvent(reason="WINDOW_NOT_FOCUSED"))
    assert "窗口丢失" in statuses[-1] and "WINDOW_NOT_FOCUSED" in statuses[-1]

    bridge.sink(GalgameWindowRecoveredEvent())
    assert "陪玩中" in statuses[-1]

    bridge.sink(GalgameSummaryStartedEvent(reason="background"))
    assert "总结中" in statuses[-1]
    bridge.sink(GalgameSummaryDoneEvent(summary_id="s1"))  # not active (host=None) -> no new status

    bridge.sink(GalgameStatusChangedEvent(state="summarizing"))
    assert "总结中" in statuses[-1]

    bridge.sink(GalgameStatusChangedEvent(state="game_launched"))  # session finalized
    assert actives[-1] is False
    assert statuses[-1] == ""


def test_bind_failed_resets_button_and_status(qapp):
    # HARD RULE: every failure path resets 🎮 + status to the REAL state -- the
    # button must never stay checked while nothing is actually playing.
    bridge = CompanionEventBridge()
    controller, statuses, actives, toasts = _wired_controller(bridge)
    controller._busy = True  # simulate mid-flow (after a click started binding)

    bridge.sink(GalgameBindFailedEvent(reason="没有匹配到目标游戏窗口", code="NO_WINDOW"))

    assert controller._busy is False  # gate released
    assert actives[-1] is False  # button reset (host=None -> real state = not playing)
    assert statuses[-1] == ""  # status chip cleared
    assert any("绑定失败" in toast and "没有匹配到目标游戏窗口" in toast for toast in toasts)


def test_selection_to_physical_rect_dpr():
    assert selection_to_physical_rect((10, 20, 30, 40), 1.0) == (10, 20, 30, 40)
    assert selection_to_physical_rect((10, 20, 30, 40), 2.0) == (20, 40, 60, 80)
    assert selection_to_physical_rect((10, 20, 30, 40), 0.0) == (10, 20, 30, 40)  # 0 -> treated as 1


# --- stage 4: M1 calibration-failure resets + M2 switch flow ----------------- #

def _spin_until(qapp, predicate, timeout=3.0):
    """Pump the event loop until predicate() (worker signals are queued)."""
    deadline = time.time() + timeout
    while not predicate():
        qapp.processEvents()
        time.sleep(0.005)
        assert time.time() < deadline, "timed out waiting for the worker round-trip"


def _drain_workers(qapp, controller, timeout=3.0):
    """Fully retire all workers BEFORE the test returns: spin until the done-slot
    removed them, join the threads, then flush deferred deletes. Without this the
    pending finished/deleteLater events outlive the test's objects and corrupt the
    NEXT test (segfault) -- a test-harness hazard only; the real app's event loop
    is permanent."""
    snapshot = list(controller._workers)
    _spin_until(qapp, lambda: not controller._workers, timeout)
    for worker in snapshot:
        worker.wait(2000)
    qapp.processEvents()
    qapp.processEvents()  # second pass flushes deferred deletions


def test_m1_galgame_error_during_calibration_resets(qapp):
    # M1 leg 1: the backend only EMITS galgame_error when calibration capture /
    # geometry fails -- this leg must reset, or the UI stays busy forever.
    bridge = CompanionEventBridge()
    controller, statuses, actives, toasts = _wired_controller(bridge)
    controller._busy = True
    controller._calibrating = True

    bridge.sink(GalgameErrorEvent(message="窗口截图失败", code="OCR_TEST_CAPTURE_FAILED"))

    assert controller._busy is False
    assert controller._calibrating is False
    assert actives[-1] is False
    assert statuses[-1] == ""
    assert any("校准失败" in toast for toast in toasts)


def test_m1_calibrate_worker_raises_when_geometry_gone(qapp):
    # M1 leg 2 (belt-and-braces): set_dialog_region returning False (NO event)
    # raises inside the worker -> the existing failed path resets.
    bridge = CompanionEventBridge()
    statuses, actives, toasts = [], [], []
    fake_calibrator = SimpleNamespace(
        set_dialog_region=lambda game_id, window_id, rect: False,  # window gone
        run_ocr_test=lambda game_id, window_id: pytest.fail("must not run after a failed set_dialog_region"),
    )
    host = SimpleNamespace(
        _companion_controller=None,  # real state: not playing
        new_ocr_calibrator=lambda: fake_calibrator,
    )
    controller = GalgameController(
        bridge, host=host,
        set_status=statuses.append, set_companion_active=actives.append, toast=toasts.append,
        select_region=lambda window_id, on_done: on_done((1, 2, 3, 4)),  # immediate selection
    )
    controller._busy = True
    controller._pending_game_id, controller._pending_window_id = "g1", "0x1"

    controller._begin_calibration()  # -> select_region -> worker(_calibrate) raises
    _spin_until(qapp, lambda: not controller._busy)
    _drain_workers(qapp, controller)

    assert controller._calibrating is False
    assert actives[-1] is False
    assert any("陪玩操作失败" in toast and "窗口几何" in toast for toast in toasts)


def test_m2_switch_runs_stop_before_start(qapp):
    # M2: the switch worker runs stop(A) -- synchronous full teardown -- BEFORE
    # start(B); on success the button/status reflect B.
    bridge = CompanionEventBridge()
    calls = []
    fake_ctrl = SimpleNamespace(
        stop=lambda: calls.append("stop"),
        start=lambda window_id, game_id=None, window_title=None: calls.append(("start", game_id)) or game_id,
        is_active=True,
    )
    host = SimpleNamespace(companion_controller=lambda: fake_ctrl, _companion_controller=fake_ctrl)
    statuses, actives, toasts = [], [], []
    controller = GalgameController(
        bridge, host=host,
        set_status=statuses.append, set_companion_active=actives.append, toast=toasts.append,
    )
    controller._busy = True
    controller._switching = True
    controller._pending_game_id, controller._pending_window_id, controller._pending_title = "b1", "0x2", "B"

    controller._start_play()
    assert any("正在切换到 b1" in status for status in statuses)
    _spin_until(qapp, lambda: not controller._busy)
    _drain_workers(qapp, controller)

    assert calls == ["stop", ("start", "b1")]  # stop COMPLETED before start began
    assert controller._switching is False
    assert actives[-1] is True
    assert "陪玩中：b1" in statuses[-1]


def test_m2_switch_start_failure_resets_with_a_stopped(qapp):
    # Switch mid-failure: stop(A) already ran, start(B) raised -> honest reset
    # (A stopped, B not started -> button unchecked, status cleared).
    bridge = CompanionEventBridge()
    calls = []

    def _start(window_id, game_id=None, window_title=None):
        calls.append(("start", game_id))
        raise RuntimeError("game 'b1' has no calibrated dialog region")

    fake_ctrl = SimpleNamespace(stop=lambda: calls.append("stop"), start=_start, is_active=False)
    host = SimpleNamespace(companion_controller=lambda: fake_ctrl, _companion_controller=fake_ctrl)
    statuses, actives, toasts = [], [], []
    controller = GalgameController(
        bridge, host=host,
        set_status=statuses.append, set_companion_active=actives.append, toast=toasts.append,
    )
    controller._busy = True
    controller._switching = True
    controller._pending_game_id, controller._pending_window_id, controller._pending_title = "b1", "0x2", "B"

    controller._start_play()
    _spin_until(qapp, lambda: not controller._busy)
    _drain_workers(qapp, controller)

    assert calls[0] == "stop"  # A's teardown happened
    assert controller._switching is False
    assert actives[-1] is False  # honest: nothing playing now
    assert statuses[-1] == ""
    assert any("陪玩操作失败" in toast for toast in toasts)


def test_m2_active_click_switch_choice_enters_switch_flow(qapp):
    # Entry wiring: choosing "switch" on an active click starts the bind flow with
    # the switching flag up (A keeps playing through B's pick/calibration).
    bridge = CompanionEventBridge()
    began = []
    fake_binder = SimpleNamespace(begin_bind=lambda game_id, manual=False: began.append((game_id, manual)))
    host = SimpleNamespace(
        _companion_controller=SimpleNamespace(is_active=True),
        new_game_binder=lambda: fake_binder,
    )
    controller = GalgameController(bridge, host=host, ask_active_action=lambda: "switch")

    controller.on_companion_clicked()
    _spin_until(qapp, lambda: bool(began))
    _drain_workers(qapp, controller)

    assert controller._switching is True
    assert controller._busy is True  # mid-flow, waiting for the candidates event
    assert began == [("", True)]  # manual bind, game_id guessed at pick time

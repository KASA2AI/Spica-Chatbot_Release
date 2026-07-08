"""GalgameController (ui/): the galgame companion UI coordinator (stage 3).

Phase 6 made this the consumer of the companion event channel (calibration
preview/test legs). Stage 3 expands it into the thin UI-side flow coordinator:

    🎮 click -> bind flow (binder.begin_bind -> candidates event -> picker ->
    resolve_selection) -> game_bound -> calibrated? (silent reuse) : (auto
    calibration: region select -> set_dialog_region + run_ocr_test -> preview
    confirm) -> controller.start(reads profile) -> status events drive the
    button + status label. Active click -> stop / recalibrate / cancel.

Discipline: ALL logic that can live below does (controller/host, unit-tested);
this class only forwards clicks, runs blocking actions on CompanionActionWorker
(one in-flight action at a time), and renders text. It touches widgets ONLY via
injected callbacks (set_status / set_companion_active / toast / pick_window /
select_region / ask_active_action), so every leg is offscreen-testable.

HARD RULE: the 🎮 checked state is written ONLY from backend events / completed
actions -- every failure path (bind_failed / picker cancel / calibration cancel
/ start error) resets the button and the status label to the REAL state.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable

from PySide6.QtCore import QEvent, QObject

from spica.galgame.companion_controller import guess_game_id_from_title
from ui.controllers.companion_event_bridge import CompanionEventBridge
from ui.workers.companion_action_worker import CompanionActionWorker

logger = logging.getLogger(__name__)

# Injected UI callbacks (all optional -> dispatch-only mode for tests/Phase 6 use).
SetText = Callable[[str], None]
SetBool = Callable[[bool], None]
PickWindow = Callable[[list[dict[str, Any]]], str | None]
SelectRegion = Callable[[str, Callable[[tuple[int, int, int, int] | None], None]], None]
AskActiveAction = Callable[[], str | None]  # "stop" | "recalibrate" | None


def selection_to_physical_rect(
    logical_rect: tuple[int, int, int, int], device_pixel_ratio: float
) -> tuple[int, int, int, int]:
    """DEPRECATED (W1, WINDOWS_COMPAT_PLAN §5-W1 内容 6): superseded by
    ``selection_to_physical_screen_rect`` (per-screen dpr + multi-monitor origin
    folding); kept with its ORIGINAL name/signature/semantics because
    tests/test_companion_bridge.py pins this exact behaviour (P1-1). Removal is a
    separate future step (W5 裁决), NOT part of the W series.

    Global logical Qt coords -> physical screen coords for the calibrator
    (same coordinate system as wmctrl/xprop geometry). v1: uniform dpr scaling;
    dpr=1 (the X11 norm here) is the identity. dpr!=1 / mixed-dpr multi-monitor
    is a KNOWN LIMITATION (GALGAME_FINDINGS.md)."""
    x, y, w, h = logical_rect
    dpr = float(device_pixel_ratio or 1.0)
    return (round(x * dpr), round(y * dpr), round(w * dpr), round(h * dpr))


# -- W1 multi-screen geometry (L3/L4). Pure functions: screen layout comes in as
# plain data (the qt_overlay call sites collect it from QGuiApplication.screens()),
# so every branch is testable with synthetic dpr/origin -- no display needed.


@dataclass(frozen=True)
class ScreenGeometry:
    """One screen's layout as pure data: its global-LOGICAL rect (Qt coordinate
    space), the same rect in global-PHYSICAL pixels (wmctrl/xprop / Win32 space),
    and its device pixel ratio. On X11 (dpr=1 everywhere) logical == physical."""

    logical: tuple[int, int, int, int]
    physical: tuple[int, int, int, int]
    device_pixel_ratio: float = 1.0


def _rect_contains(rect: tuple[int, int, int, int], x: int, y: int) -> bool:
    # Integer-rect containment, same semantics as QRect.contains: the right/bottom
    # edges (x + w, y + h) are OUTSIDE.
    rx, ry, rw, rh = rect
    return rx <= x < rx + rw and ry <= y < ry + rh


def selection_to_physical_screen_rect(
    logical_rect: tuple[int, int, int, int],
    screens: list[ScreenGeometry],
) -> tuple[int, int, int, int]:
    """Global logical Qt coords -> global physical coords, folding the containing
    screen's dpr AND origin (the L3 fix: ``selection_to_physical_rect`` scaled
    uniformly and never subtracted a screen origin, wrong on per-monitor-DPI
    multi-screen). The selection's screen is matched by its top-left, falling back
    to its centre. With every screen at dpr=1 (the X11 norm) this is the identity,
    byte-equal to the old function -- pinned by the W1 goldens. No/unmatched
    screens -> the old uniform-dpr=1 behaviour (identity), never a crash."""
    x, y, w, h = logical_rect
    screen = next((s for s in screens if _rect_contains(s.logical, x, y)), None)
    if screen is None:
        screen = next(
            (s for s in screens if _rect_contains(s.logical, x + w // 2, y + h // 2)), None
        )
    if screen is None:
        return (x, y, w, h)
    dpr = float(screen.device_pixel_ratio or 1.0)
    lx, ly = screen.logical[0], screen.logical[1]
    px, py = screen.physical[0], screen.physical[1]
    return (
        px + round((x - lx) * dpr),
        py + round((y - ly) * dpr),
        round(w * dpr),
        round(h * dpr),
    )


def physical_point_to_screen_index(
    point: tuple[int, int], screens: list[ScreenGeometry]
) -> int | None:
    """Global PHYSICAL point (wmctrl/Win32 window centre) -> index of the screen
    whose PHYSICAL rect contains it, or None (the L4 fix: the old call fed
    physical coords to ``QGuiApplication.screenAt``, which expects LOGICAL
    coords). At dpr=1 physical == logical, so the match is unchanged -- pinned by
    the W1 goldens; W2 validates real per-monitor-DPI behaviour."""
    x, y = point
    for index, screen in enumerate(screens):
        if _rect_contains(screen.physical, x, y):
            return index
    return None


def _noop_text(_text: str) -> None:
    return None


def _noop_bool(_value: bool) -> None:
    return None


class GalgameController(QObject):
    def __init__(
        self,
        bridge: CompanionEventBridge,
        parent: QObject | None = None,
        *,
        host: Any | None = None,
        set_status: SetText | None = None,
        set_companion_active: SetBool | None = None,
        toast: SetText | None = None,
        pick_window: PickWindow | None = None,
        select_region: SelectRegion | None = None,
        ask_active_action: AskActiveAction | None = None,
        overlay_window_id_provider: Callable[[], str | None] | None = None,
    ) -> None:
        super().__init__(parent)
        self._bridge = bridge
        self._host = host
        self._set_status: SetText = set_status or _noop_text
        self._set_companion_active: SetBool = set_companion_active or _noop_bool
        self._toast: SetText = toast or _noop_text
        self._pick_window: PickWindow = pick_window or (lambda _candidates: None)
        self._select_region: SelectRegion = select_region or (lambda _wid, on_done: on_done(None))
        self._ask_active_action: AskActiveAction = ask_active_action or (lambda: None)
        # window_lost fix (overlay focus exemption): supplies the Spica overlay's own
        # X11 id so check_safety's focus exemption fires while the user TYPES to her
        # (focus on the overlay != "left the game" -> no false WINDOW_NOT_FOCUSED). A
        # PROVIDER, not a value: winId() is read at start time (after the window is
        # shown), never at construct time. None -> exemption stays off -- unchanged.
        self._overlay_window_id_provider = overlay_window_id_provider

        self._preview = None  # OcrCalibrationPreview, created lazily
        self._calibrator: Any | None = None  # host.new_ocr_calibrator(), built lazily
        self._binder: Any | None = None  # per-flow GameBinder (selection-only mode)
        self._workers: list[CompanionActionWorker] = []
        # One flow/action in flight at a time; cleared on completion or any reset.
        self._busy = False
        self._calibrating = False
        self._recalibrating = False
        self._switching = False  # M2: a switch flow ends with stop(A) -> start(B)
        self._pending_game_id: str | None = None
        self._pending_window_id: str | None = None
        self._pending_title: str | None = None
        self._active_game_id: str | None = None
        self._active_window_id: str | None = None
        bridge.companion_event.connect(self._on_companion_event)

    # -- entry ------------------------------------------------------------------
    def on_companion_clicked(self) -> None:
        if self._host is None:
            return
        if self._busy:
            self._toast("陪玩操作进行中，请稍候…")
            return
        if self._companion_is_active():
            choice = self._ask_active_action()
            if choice == "stop":
                self._begin_stop()
            elif choice == "recalibrate":
                self._begin_recalibration()
            elif choice == "switch":
                self._begin_switch_flow()
            return
        self._begin_bind_flow()

    def shutdown(self, timeout_ms: int = 3000) -> None:
        """Close-during-play (designed crash-equivalent path): run stop() off the
        UI thread and wait briefly; on timeout ABANDON the wait -- the dangling
        PlaySession is silently 補總結'd by recover_dangling on next startup."""
        for worker in list(self._workers):
            if worker.isRunning():
                worker.wait(timeout_ms)
        if self._host is None:
            return
        controller = getattr(self._host, "_companion_controller", None)
        if controller is None or not controller.is_active:
            return
        worker = CompanionActionWorker(controller.stop, self)
        worker.start()
        worker.wait(timeout_ms)

    def _begin_switch_flow(self) -> None:
        """M2 (stage 4): switch from playing A to playing B. A stays LIVE through
        B's pick/calibration (cancelling mid-way leaves A untouched); the final
        worker runs ``controller.stop()`` -- A's final summary + history card
        complete synchronously -- THEN ``controller.start(B)``. controller itself
        gains no new method: the switch is pure stop->start sequencing."""
        self._switching = True
        self._begin_bind_flow()

    # -- flow: bind -> (calibrate) -> start --------------------------------------
    def _begin_bind_flow(self) -> None:
        self._busy = True
        self._set_status("正在查找游戏窗口…")
        # Selection/persistence-only binder (session=None): controller.start()
        # builds + binds its own session afterwards. game_id="" -> empty rule ->
        # every window qualifies -> forced pick (§17.3); the guess happens at
        # resolve time, from the PICKED window's title.
        self._binder = self._host.new_game_binder()
        binder = self._binder
        self._run(lambda: binder.begin_bind("", manual=True))
        # continues via galgame_window_candidates / galgame_bind_failed events

    def _on_window_candidates(self, event: Any) -> None:
        if not self._busy or self._binder is None:
            return  # not our flow (e.g. a future launch flow drives its own binder)
        candidates = [dict(c) for c in (getattr(event, "candidates", None) or [])]
        window_id = self._pick_window(candidates)
        if not window_id:
            self._binder = None
            self._reset_to_real_state("已取消选窗。")
            return
        by_id = {str(c.get("window_id") or ""): c for c in candidates}
        title = str((by_id.get(window_id) or {}).get("title") or "")
        game_id = guess_game_id_from_title(title)
        if not game_id:
            self._binder = None
            self._reset_to_real_state("无法从窗口标题推断游戏 ID（纯非拉丁标题暂需后续支持）。")
            return
        self._pending_window_id, self._pending_game_id, self._pending_title = window_id, game_id, title
        self._set_status(f"正在绑定 {game_id}…")
        binder = self._binder
        self._run(lambda: binder.resolve_selection(window_id, game_id_override=game_id))
        # continues via galgame_game_bound / galgame_bind_failed

    def _on_game_bound(self, event: Any) -> None:
        if not self._busy:
            return
        self._binder = None
        self._pending_game_id = str(getattr(event, "game_id", "") or self._pending_game_id or "")
        self._pending_window_id = str(getattr(event, "window_id", "") or self._pending_window_id or "")
        # Single sqlite read (ms) -- acceptable on the UI thread.
        if self._host.companion_controller().has_calibrated_dialog_region(self._pending_game_id):
            self._start_play()  # silent reuse of the persisted calibration (debt #8)
        else:
            self._begin_calibration()

    def _begin_calibration(self) -> None:
        if not self._pending_window_id:
            self._reset_to_real_state("没有可校准的窗口。")
            return
        self._calibrating = True
        self._set_status("请框选游戏对白区域（Esc 取消）…")
        self._select_region(self._pending_window_id, self._on_region_selected)

    def _on_region_selected(self, rect: tuple[int, int, int, int] | None) -> None:
        if rect is None:
            self._calibrating = False
            self._reset_to_real_state("已取消校准。")
            return
        game_id, window_id = self._pending_game_id, self._pending_window_id
        calibrator = self._get_calibrator()
        self._set_status("正在测试识别效果…")

        def _calibrate() -> None:
            # M1 belt-and-braces: set_dialog_region returns False (no event) when
            # the window geometry is gone -- raise so the worker's failed path
            # resets the UI instead of leaving it stuck on "正在测试识别效果…".
            if not calibrator.set_dialog_region(game_id, window_id, tuple(rect)):
                raise RuntimeError("无法获取游戏窗口几何（游戏可能已关闭）")
            calibrator.run_ocr_test(game_id, window_id)  # first call may load the OCR engine (seconds)

        self._run(_calibrate)
        # preview/test events arrive -> preview window (existing legs); the user
        # then confirms / reframes / closes (close == cancel, via eventFilter).

    def _on_calibration_confirmed(self) -> None:
        if not self._calibrating:
            return
        self._calibrating = False
        if self._preview is not None:
            self._preview.hide()  # hide() does not fire Close -> no cancel-reset
        game_id = self._pending_game_id
        calibrator = self._get_calibrator()
        if self._recalibrating:
            self._recalibrating = False
            self._run(
                lambda: calibrator.confirm(game_id),
                on_ok=lambda _r: self._reset_to_real_state("校准已更新，新区域将在下次开始陪玩时生效。"),
            )
            return
        self._set_status("校准完成，正在启动陪玩…")
        self._run(lambda: calibrator.confirm(game_id), on_ok=lambda _r: self._start_play())

    def _on_reframe_requested(self) -> None:
        if not self._calibrating:
            return
        if self._preview is not None:
            self._preview.hide()
        self._begin_calibration()

    def _start_play(self) -> None:
        game_id, window_id, title = self._pending_game_id, self._pending_window_id, self._pending_title
        controller = self._host.companion_controller()
        switching = self._switching
        self._set_status(f"正在切换到 {game_id}…" if switching else f"正在启动陪玩 {game_id}…")

        def _start() -> Any:
            if switching:
                # stop() is a SYNCHRONOUS full teardown: A's final summary + history
                # card complete before this returns -- start(B) can never skip them.
                controller.stop()
            # dialog_ratios omitted on purpose: start() reads the persisted profile.
            overlay_window_id = (
                self._overlay_window_id_provider() if self._overlay_window_id_provider else None
            )
            return controller.start(
                window_id, game_id=game_id, window_title=title or None,
                overlay_window_id=overlay_window_id,
            )

        def _ok(_result: Any) -> None:
            self._busy = False
            self._switching = False
            self._active_game_id, self._active_window_id = game_id, window_id
            self._set_companion_active(True)
            self._set_status(self._playing_status())
            self._toast(f"开始陪玩 {game_id}。游戏窗口失焦会自动暂停采集。")

        self._run(_start, on_ok=_ok)

    # -- flow: active click -> stop / recalibrate --------------------------------
    def _begin_stop(self) -> None:
        self._busy = True
        self._set_status("⏳ 正在结束陪玩（生成剧情总结中…）")
        controller = self._host.companion_controller()

        def _ok(_result: Any) -> None:
            self._busy = False
            self._active_game_id = self._active_window_id = None
            self._set_companion_active(False)
            self._set_status("")
            self._toast("陪玩已结束，剧情总结已保存。")

        self._run(controller.stop, on_ok=_ok)

    def _begin_recalibration(self) -> None:
        if not self._active_game_id or not self._active_window_id:
            self._toast("没有可重校准的陪玩会话。")
            return
        self._busy = True
        self._recalibrating = True
        self._pending_game_id, self._pending_window_id = self._active_game_id, self._active_window_id
        self._begin_calibration()

    # -- event dispatch (queued from backend threads onto the GUI thread) --------
    def _on_companion_event(self, event: Any) -> None:
        kind = getattr(event, "kind", "")
        if kind == "galgame_ocr_preview_ready":
            self._preview_widget().show_preview(
                getattr(event, "image_png", b""), getattr(event, "suspect_blank", False)
            )
            self._preview_widget().show()
        elif kind == "galgame_ocr_test_result":
            self._preview_widget().show_text(getattr(event, "dialog_text", ""))
        elif kind == "galgame_window_candidates":
            self._on_window_candidates(event)
        elif kind == "galgame_game_bound":
            self._on_game_bound(event)
        elif kind == "galgame_bind_failed":
            self._on_bind_failed(event)
        elif kind == "galgame_status_changed":
            self._on_status_changed(event)
        elif kind == "galgame_window_lost":
            reason = str(getattr(event, "reason", "") or "")
            self._set_status(f"⚠ 窗口丢失/失焦，已暂停采集（{reason}）" if reason else "⚠ 窗口丢失/失焦，已暂停采集")
        elif kind == "galgame_window_recovered":
            self._set_status(self._playing_status())
        elif kind == "galgame_summary_started":
            if getattr(event, "reason", "") == "background":
                self._set_status(self._playing_status() + "（总结中…）")
        elif kind == "galgame_summary_done":
            if self._companion_is_active():
                self._set_status(self._playing_status())
        elif kind == "galgame_error":
            logger.warning("galgame error event: %s", getattr(event, "message", ""))
            # M1 (stage 4): a calibration-time backend error (window geometry gone /
            # capture failed) only EMITS this event -- without this leg the flow
            # stayed busy forever on "正在测试识别效果…" (stuck-state class).
            if self._busy and self._calibrating:
                self._calibrating = False
                self._recalibrating = False
                self._reset_to_real_state("校准失败/游戏窗口异常，已取消。")

    def _on_bind_failed(self, event: Any) -> None:
        # HARD RULE: every failure path resets button + status to the real state.
        reason = str(getattr(event, "reason", "") or getattr(event, "code", "") or "未知原因")
        logger.warning("galgame bind failed: %s (%s)", reason, getattr(event, "code", ""))
        self._binder = None
        self._calibrating = False
        self._recalibrating = False
        self._reset_to_real_state(f"绑定失败：{reason}")

    def _on_status_changed(self, event: Any) -> None:
        state = str(getattr(event, "state", "") or "")
        if state == "playing":
            self._set_companion_active(True)
            self._set_status(self._playing_status())
        elif state == "paused":
            self._set_status("⏸ 已暂停")
        elif state == "window_lost":
            self._set_status("⚠ 窗口丢失/失焦，已暂停采集")
        elif state == "background_summarizing":
            self._set_status(self._playing_status() + "（总结中…）")
        elif state == "summarizing":
            self._set_status("⏳ 总结中…")
        elif state in ("game_launched", "idle"):
            # Session finalized (stop/end). Mid-flow (busy) the action's own
            # completion handler owns the reset; otherwise reflect reality now.
            if not self._busy:
                self._set_companion_active(False)
                self._set_status("")

    # -- plumbing -----------------------------------------------------------------
    def _run(
        self,
        fn: Callable[[], Any],
        on_ok: Callable[[Any], None] | None = None,
        on_fail: Callable[[str], None] | None = None,
    ) -> None:
        # THREADING: never connect bare closures to worker signals -- a closure has
        # no QObject thread affinity, so AutoConnection degrades to DIRECT and the
        # callback runs ON THE WORKER THREAD (stage-3 latent defect, exposed by the
        # stage-4 switch test as a segfault). All completion paths go through bound
        # methods of this controller (GUI-thread affinity -> queued delivery); the
        # per-worker callbacks ride as plain attributes and run on the GUI thread.
        worker = CompanionActionWorker(fn, self)
        worker._on_ok_cb = on_ok  # type: ignore[attr-defined]
        worker._on_fail_cb = on_fail  # type: ignore[attr-defined]
        self._workers.append(worker)
        worker.finished_ok.connect(self._dispatch_worker_ok)
        worker.failed.connect(self._dispatch_worker_fail)
        worker.finished.connect(self._dispatch_worker_done)
        worker.start()

    def _dispatch_worker_ok(self, result: Any) -> None:  # GUI thread (queued)
        callback = getattr(self.sender(), "_on_ok_cb", None)
        if callback is not None:
            callback(result)

    def _dispatch_worker_fail(self, message: str) -> None:  # GUI thread (queued)
        callback = getattr(self.sender(), "_on_fail_cb", None)
        (callback or self._on_action_failed)(message)

    def _dispatch_worker_done(self) -> None:  # GUI thread (queued)
        worker = self.sender()
        if worker in self._workers:
            self._workers.remove(worker)
        if worker is not None:
            worker.deleteLater()

    def _on_action_failed(self, message: str) -> None:
        logger.warning("galgame companion action failed: %s", message)
        self._binder = None
        self._calibrating = False
        self._recalibrating = False
        self._reset_to_real_state(f"陪玩操作失败：{message}")

    def _reset_to_real_state(self, message: str | None = None) -> None:
        """Failure/cancel convergence point (HARD RULE): busy gate released, the 🎮
        button + status label snap back to the REAL companion state -- never stuck
        checked while nothing is actually playing. A cancelled switch lands here
        too: A is still live (stop only runs in the final worker) -> back to A."""
        self._busy = False
        self._switching = False
        self._pending_game_id = self._pending_window_id = self._pending_title = None
        active = self._companion_is_active()
        self._set_companion_active(active)
        self._set_status(self._playing_status() if active else "")
        if message:
            self._toast(message)

    def _companion_is_active(self) -> bool:
        # Peek the singleton WITHOUT building it (no side effects on a reset path).
        if self._host is None:
            return False
        controller = getattr(self._host, "_companion_controller", None)
        return bool(controller is not None and controller.is_active)

    def _playing_status(self) -> str:
        game_id = self._active_game_id or self._pending_game_id or ""
        return f"🎮 陪玩中：{game_id}" if game_id else "🎮 陪玩中"

    def _get_calibrator(self) -> Any:
        if self._calibrator is None:
            self._calibrator = self._host.new_ocr_calibrator()
        return self._calibrator

    def _preview_widget(self):
        if self._preview is None:
            from ui.widgets.ocr_calibration_preview import OcrCalibrationPreview

            self._preview = OcrCalibrationPreview()
            self._preview.confirmed.connect(self._on_calibration_confirmed)
            self._preview.reframe_requested.connect(self._on_reframe_requested)
            self._preview.installEventFilter(self)  # closing the window == cancel
        return self._preview

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802 - Qt override
        if watched is self._preview and event.type() == QEvent.Type.Close and self._calibrating:
            self._calibrating = False
            self._recalibrating = False
            self._reset_to_real_state("已取消校准。")
        return super().eventFilter(watched, event)

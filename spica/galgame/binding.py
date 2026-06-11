"""GameBinder: launch + window-binding coordinator (Phase 5), Qt-free.

Drives the flow up to ``game_launched``: launch (or skip for manual bind) ->
enumerate windows -> score by title_keywords -> decide none/unique/multiple ->
emit a companion event for the ui/ picker -> receive the user's choice
(``resolve_selection``) -> persist the WindowMatchRule -> ``session.bind_game``.

Boundary (CLAUDE.md #1): this is backend domain logic; the only thing crossing to
ui/ is serialized candidate dicts (out, via the sink) and a window_id string (in,
via resolve_selection). No Qt here; the picker/confirm dialog lives in ui/.

Failures never crash the flow: launch failure / unavailable enumeration / no match
/ a rejected bind_game all become a readable ``galgame_bind_failed`` (§4.4). In
particular a ``GalgameStateError`` from bind_game (session not in a bindable state)
is caught and surfaced, never allowed to escape.
"""

from __future__ import annotations

import dataclasses
import logging
import threading
from typing import Any

from spica.core.companion_events import (
    CompanionEventSink,
    GalgameBindFailedEvent,
    GalgameGameBoundEvent,
    GalgameWindowCandidatesEvent,
    noop_companion_sink,
)
from spica.galgame.models import GameProfile, LaunchProfile, WindowMatchRule, utc_now_iso
from spica.galgame.session import GalgameCompanionSession, GalgameStateError
from spica.galgame.window_match import WindowMatchOutcome, classify, score_candidates
from spica.ports.game_launcher import GameLauncherPort
from spica.ports.window_locator import WindowCandidate, WindowLocatorPort

logger = logging.getLogger(__name__)

_UNAVAILABLE_OPTIONS = {
    "WMCTRL_MISSING": ["install_wmctrl", "cancel"],
    "WAYLAND_UNSUPPORTED": ["cancel"],  # v1 needs X11; no manual-bind path either (can't enumerate)
}


class GameBinder:
    def __init__(
        self,
        launcher: GameLauncherPort,
        locator: WindowLocatorPort,
        game_memory: Any,
        session: GalgameCompanionSession | None = None,
        emit: CompanionEventSink | None = None,
    ) -> None:
        # session=None (stage 3): selection/persistence-only mode -- the UI picks a
        # window and persists the binding, then GalgameCompanionController.start()
        # builds + binds its OWN session. resolve_selection skips bind_game then.
        self._launcher = launcher
        self._locator = locator
        self._mem = game_memory
        self._session = session
        self._emit: CompanionEventSink = emit or noop_companion_sink
        self._lock = threading.RLock()
        self._pending: dict[str, Any] | None = None

    def begin_bind(
        self, game_id: str, launch_profile: LaunchProfile | None = None, *, manual: bool = False
    ) -> None:
        with self._lock:
            self._pending = None
            if not manual:
                if launch_profile is None:
                    self._fail(game_id, "未提供启动方式。", "NO_LAUNCH_PROFILE")
                    return
                result = self._launcher.launch(launch_profile)
                if not result.ok:
                    self._fail(game_id, result.error or "启动失败。", "LAUNCH_FAILED")
                    return

            enumeration = self._locator.enumerate_windows()
            if not enumeration.available:
                options = _UNAVAILABLE_OPTIONS.get(enumeration.reason_code, ["retry", "manual_bind", "cancel"])
                self._emit(
                    GalgameBindFailedEvent(reason=enumeration.reason, code=enumeration.reason_code, options=options)
                )
                return

            rule = self._rule_for(game_id)
            scored = score_candidates(enumeration.windows, rule)
            outcome = classify(scored)
            if outcome is WindowMatchOutcome.NONE:
                self._fail(game_id, "没有匹配到目标游戏窗口，请手动绑定。", "NO_WINDOW",
                           options=["manual_bind", "retry", "cancel"])
                return

            candidates = [item.candidate for item in scored]
            self._pending = {
                "game_id": game_id,
                "candidates": {c.window_id: c for c in candidates},
                "launch_profile": launch_profile,
            }
            # Even a unique match still needs a first-time confirm (§17.3) -- never auto-bind.
            mode = "confirm" if outcome is WindowMatchOutcome.UNIQUE else "pick"
            self._emit(GalgameWindowCandidatesEvent(candidates=[c.to_dict() for c in candidates], mode=mode))

    def resolve_selection(self, window_id: str, game_id_override: str | None = None) -> None:
        """Confirm the user's pick. ``game_id_override`` (stage 3) supplies the
        game_id when it was unknown at begin_bind time -- the first-time flow can
        only guess it FROM the picked window's title, so begin_bind ran with
        game_id="" (empty rule -> everyone qualifies -> forced pick, §17.3) and the
        UI passes the guess here."""
        with self._lock:
            if self._pending is None:
                self._emit(GalgameBindFailedEvent(reason="没有待确认的绑定。", code="NO_PENDING_BIND", options=["cancel"]))
                return
            candidate = self._pending["candidates"].get(window_id)
            if candidate is None:
                self._emit(
                    GalgameBindFailedEvent(reason=f"未知窗口 {window_id}。", code="UNKNOWN_WINDOW", options=["pick_again", "cancel"])
                )
                return
            game_id = game_id_override or self._pending["game_id"]
            if not game_id:
                self._pending = None
                self._emit(
                    GalgameBindFailedEvent(reason="无法确定游戏 ID（标题不可推断且未提供）。", code="NO_GAME_ID", options=["cancel"])
                )
                return
            launch_profile = self._pending["launch_profile"]
            self._store_binding(game_id, candidate, launch_profile)
            if self._session is not None:
                try:
                    self._session.bind_game(game_id)
                except GalgameStateError as exc:
                    # G3: a rejected FSM transition must NOT escape -- surface it.
                    logger.warning("bind_game rejected for %s: %s", game_id, exc)
                    self._pending = None
                    self._emit(
                        GalgameBindFailedEvent(
                            reason=f"当前状态无法绑定游戏：{exc}", code="SESSION_NOT_BINDABLE", options=["cancel"]
                        )
                    )
                    return
            self._pending = None
            self._emit(GalgameGameBoundEvent(game_id=game_id, window_id=candidate.window_id, title=candidate.title))

    def cancel_bind(self) -> None:
        with self._lock:
            self._pending = None

    # -- helpers --------------------------------------------------------------
    def _rule_for(self, game_id: str) -> WindowMatchRule:
        profile = self._mem.get_game_profile(game_id)
        if profile is not None and profile.window_match:
            return WindowMatchRule.from_dict(profile.window_match)
        return WindowMatchRule()

    def _store_binding(
        self, game_id: str, candidate: WindowCandidate, launch_profile: LaunchProfile | None
    ) -> None:
        now = utc_now_iso()
        profile = self._mem.get_game_profile(game_id)
        if profile is None:
            profile = GameProfile(game_id=game_id, display_name=game_id, created_at=now, updated_at=now)
        rule = WindowMatchRule.from_dict(profile.window_match) if profile.window_match else WindowMatchRule()
        # last_full_title is historical reference only; title_keywords stay the match key.
        rule = dataclasses.replace(
            rule,
            last_full_title=candidate.title,
            process_name=candidate.process_name or rule.process_name,
            app_id=candidate.app_id or rule.app_id,
            confirmed_once=True,
        )
        launch_profiles = dict(profile.launch_profiles)
        if launch_profile is not None:
            launch_profiles["active"] = launch_profile.to_dict()
        profile = dataclasses.replace(
            profile, window_match=rule.to_dict(), launch_profiles=launch_profiles, updated_at=now
        )
        self._mem.upsert_game_profile(profile)

    def _fail(self, game_id: str, reason: str, code: str, *, options: list[str] | None = None) -> None:
        self._pending = None
        self._emit(
            GalgameBindFailedEvent(
                reason=reason, code=code, options=options or ["rechoose_launch", "manual_bind", "cancel"]
            )
        )

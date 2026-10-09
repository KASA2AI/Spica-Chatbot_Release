"""Local memory maintenance, owned by the same desktop host as dialogue."""
from __future__ import annotations

import logging
from spica.ports.memory import MemoryScope

logger = logging.getLogger(__name__)


def install(host) -> None:
    adapter = host.services.memory_adapter
    if not callable(getattr(adapter, "start_maintenance", None)):
        return
    from spica.core.character_memory import restore_character_save
    restore_character_save(host.character_package, host.services)
    config = host.config.memory
    model = host.model_router.for_role("memory") if config.consolidation_enabled else None
    adapter.expire_raw()
    adapter.start_maintenance(model, idle_seconds=config.consolidation_idle_seconds)


def shutdown(host, *, timeout: float = 0) -> bool:
    services = getattr(host, "services", None)
    adapter = getattr(services, "memory_adapter", None)
    close = getattr(adapter, "shutdown", None)
    return close(timeout=timeout) if callable(close) else True


def record_play_history(host, game_id: str, card: str, session_id: str) -> None:
    """Publish the existing compact game card through sourced personal memory.

    A play session identifies this observation across stop/recovery retries;
    replaying a deleted card cannot create a fresh source. The service-owned
    organizer maintains the per-game relationship episode, with game framing.
    Its original role/person come from the durable play session, including
    when a different role is selected at recovery.
    """
    if not isinstance(session_id, str) or not session_id:
        raise ValueError('play history requires its actual play session identity')
    session = host.services.game_memory_adapter.get_play_session(session_id)
    if session is None or session.game_id != game_id:
        raise ValueError('play history must belong to its recorded game session')
    if not session.character_id or not session.principal_id:
        return  # legacy scope is unknown, never inferred from the current role
    host.services.memory_adapter.record_evidence(
        MemoryScope(session.character_id, session.principal_id, "default"),
        event_id=f"galgame_history:{session_id}", kind="system_event", source="galgame_companion",
        modality="system", content=card,
        metadata={"turn_id": f"galgame_history:{session_id}",
                  "game_id": game_id, "play_session_id": session_id,
                  "memory_key": f"galgame_history:{game_id}", "narrative_scope": "game"},
    )


def record_business_evidence(host, scope, **event):
    """Return persistence acknowledgement; Home retains its durable retry identity."""
    from concurrent.futures import Future
    from copy import deepcopy
    future = Future()
    frozen = deepcopy(event)
    # Home initializes this existing job owner before starting its sensor thread.
    jobs = host._business_evidence_jobs
    def write():
        try:
            adapter = host.chat_engine.deps.memory
            result = adapter.record_evidence(scope, **frozen)
            future.set_result(result)
        except Exception as exc:
            future.set_exception(exc)
    jobs.submit(write)
    return future

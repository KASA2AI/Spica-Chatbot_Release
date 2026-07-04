"""ActiveDomainRouter (OO migration Phase 8-c1, 设计裁决 1).

The host-side single home for "which domain owns the current turn binding".
Domains PUBLISH their turn binding when they go live (galgame: the companion
controller's publish-LAST point) and RETRACT it when they stop (clear-FIRST
point); ``ChatEngine`` keeps its ONE provider slot pointing at
``router.current`` (D6: the router is the only injector).

Contract (pinned by tests/test_domain_router.py):
- the constructor is INERT (no I/O, no config reads);
- ``publish``/``retract`` are IN-MEMORY and NO-THROW (lock + dict ops -- the
  controller's binding sink must never be able to break start/stop);
- ``current()`` returns the highest-priority live binding; a priority TIE is a
  configuration error -- the latest publish wins deterministically and a
  WARNING is logged once at publish time (never raises);
- ``current_for(domain)`` is the domain-filtered read galgame-only closures
  may use (设计裁决 修正 1: galgame-only closures such as
  ``_companion_game_binding`` / the note write-back / the reaction scope never
  read the unfiltered ``current()``).

Bindings are opaque here (``Any``): galgame publishes ``GameTurnBinding``
(legacy lane), future domains publish ``DomainTurnBinding`` (generic lane) --
the lane dispatch lives in ``ChatEngine._request``, never in the router.
This module must not import ``AppHost`` or ``spica.galgame`` (no cycles; the
assemblies/model_router precedent). Qt-free (CLAUDE.md #1).
"""

from __future__ import annotations

import logging
import threading
from typing import Any

logger = logging.getLogger(__name__)


class ActiveDomainRouter:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        # domain -> (priority, publish_seq, binding); seq breaks priority ties
        # (latest publish wins) and makes current() deterministic.
        self._entries: dict[str, tuple[int, int, Any]] = {}
        self._seq = 0

    def publish(self, domain: str, binding: Any, priority: int = 0) -> None:
        """Publish/replace a domain's live binding. NO-THROW by contract."""
        with self._lock:
            if any(
                entry_priority == priority
                for entry_domain, (entry_priority, _, _) in self._entries.items()
                if entry_domain != domain
            ):
                logger.warning(
                    "domain binding priority tie (domain=%s, priority=%s) -- "
                    "latest publish wins; ties are a configuration error",
                    domain, priority,
                )
            self._seq += 1
            self._entries[domain] = (priority, self._seq, binding)

    def retract(self, domain: str) -> None:
        """Remove a domain's binding; missing domain is a no-op. NO-THROW."""
        with self._lock:
            self._entries.pop(domain, None)

    def current(self) -> Any | None:
        """The live binding that owns the next turn (highest priority; ties ->
        latest publish), or None when no domain is live (plain chat)."""
        with self._lock:
            if not self._entries:
                return None
            _, _, binding = max(self._entries.values(), key=lambda e: (e[0], e[1]))
            return binding

    def current_for(self, domain: str) -> Any | None:
        """The named domain's own live binding (domain-filtered read -- the form
        galgame-only closures are allowed to use), or None."""
        with self._lock:
            entry = self._entries.get(domain)
            return entry[2] if entry is not None else None

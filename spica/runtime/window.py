"""Window identity value objects (OO migration Phase 8-c2, 设计裁决 4).

``WindowTarget`` is the PURE-IDENTITY half of "which window may be looked at":
ids and value facts only -- capability handles (locator/capture) and runtime
state deliberately stay OUT of the value object, so passing a target around
never leaks capture authority. ``WatchContext`` is the named carrier the host's
watch provider returns (replacing the historical bare 5-tuple): target +
the capability handles + the lock-free session-state snapshot, unpacked BY
NAME by the watch tool.

Runtime layer: no galgame / host / Qt imports (CLAUDE.md #1; test_layering).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, NamedTuple


@dataclass(frozen=True)
class WindowTarget:
    """Identity of a watchable window. ``owner_domain`` names the domain that
    published it (PrivacyGate refuses foreign targets -- wiring errors are
    loud); ``game_id`` rides along for tool logs/metadata (galgame keeps its
    byte-identical observation shape); ``match_rule`` feeds
    ``locator.check_safety`` on the OCR purpose (None where the caller does
    not carry one, e.g. the watch tool's state-gate-only purpose)."""

    window_id: str
    owner_domain: str
    game_id: str | None = None
    match_rule: Any | None = None


class WatchContext(NamedTuple):
    """What the host's watch provider hands the watch tool for one call."""

    target: WindowTarget
    locator: Any
    capture: Any
    state: Any

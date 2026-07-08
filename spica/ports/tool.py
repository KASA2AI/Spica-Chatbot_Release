"""Tool capability port (Phase 5).

Kept (unlike ASR) because function tools already exist and plugins will register
tools in Phase 8. The current ``agent_tools.function_tools`` registry is not
reshaped in this phase; this Protocol defines the target surface for new tools.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class ToolPort(Protocol):
    def schema(self) -> dict[str, Any]:
        ...

    def run(self, **kwargs: Any) -> dict[str, Any]:
        ...

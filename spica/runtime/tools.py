"""Tool surface for a turn (core C3a).

``ToolSet`` is how the runtime asks "which tools apply to this user text?" and
"run this tool" without knowing whether the tools come from the legacy function
table or (C7) the CapabilityRegistry. C3a wires in ``LegacyFunctionToolSet`` --
a *minimal* wrapper over the existing ``TOOL_SCHEMAS`` / ``run_local_tool`` /
``is_screen_intent_explicit`` gate, adding NO new behaviour. C7 swaps the
implementation for a registry-backed one behind the same protocol.

The intent gate lives inside ``schemas_for_user_text`` (it returns the
inspect_screen schema only when the user text explicitly asks for a screen look),
so it travels with the tool surface, not the orchestrator.

Pure: no ``agent`` import (``agent_tools`` is the tools package, not the
conversation core), Qt-free (CLAUDE.md #1).
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from agent_tools.function_tools import run_local_tool, tool_schemas_for_user_text


@runtime_checkable
class ToolSet(Protocol):
    """The runtime's view of the available tools for a turn."""

    def schemas_for_user_text(self, user_text: str) -> list[dict[str, Any]]:
        """Tool schemas applicable to ``user_text`` (includes the intent gate)."""
        ...

    def run(self, name: str, arguments: str) -> str:
        """Run tool ``name`` with JSON ``arguments``, returning the tool result."""
        ...


class LegacyFunctionToolSet:
    """Minimal wrapper over the legacy ``TOOL_SCHEMAS`` / ``run_local_tool`` pair.

    Adds nothing: ``schemas_for_user_text`` is the existing intent-gated selector,
    ``run`` is the existing local dispatcher. C7 replaces this with a
    registry-backed ToolSet behind the same protocol.
    """

    def __init__(
        self,
        schemas: list[dict[str, Any]],
        functions: dict[str, Any],
    ) -> None:
        self._schemas = schemas
        self._functions = functions

    @classmethod
    def from_services(cls, services: Any) -> "LegacyFunctionToolSet":
        return cls(services.tool_schemas, services.tool_functions)

    def schemas_for_user_text(self, user_text: str) -> list[dict[str, Any]]:
        return tool_schemas_for_user_text(user_text, self._schemas)

    def run(self, name: str, arguments: str) -> str:
        return run_local_tool(self._functions, name, arguments)

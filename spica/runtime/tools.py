"""Tool surface for a turn (core C3a; C7: registry-backed).

``ToolSet`` is how the runtime asks "which tools apply to this user text?" and
"run this tool" without knowing whether the tools come from a host
``CapabilityRegistry`` (production, where ``inspect_screen`` is a ``ToolPort``) or a
legacy function table injected on ``services`` (tests). The intent gate lives inside
``schemas_for_user_text`` so it travels with the tool surface, not the orchestrator.

``RegistryToolSet`` (C7) reads schemas + handlers from a registry object (anything
with ``tool_schemas()`` / ``tool_handler(name)``). A ToolPort handler returns a dict;
a legacy function handler returns the ``tool_success`` / ``tool_error`` string. ``run``
serializes the dict ONCE and passes a string through unchanged, so the LLM tool round
is byte-identical either way.

INVARIANT (N5): the runtime resolves tools from the registry; it never re-imports the
static ``TOOL_SCHEMAS`` / ``default_tool_functions`` -- it only adapts the legacy
tools already injected on ``services``.

Pure: no ``agent`` import (``agent_tools`` is the tools package), Qt-free (CLAUDE.md #1).
"""

from __future__ import annotations

import json
from typing import Any, Callable, Protocol, runtime_checkable

from agent_tools.function_tools import tool_error, tool_schemas_for_user_text, tool_success
from agent_tools.function_tools.screen.analyzer import clear_last_screen_analysis_metadata
from agent_tools.function_tools.screen.schema import ScreenToolError


@runtime_checkable
class ToolSet(Protocol):
    """The runtime's view of the available tools for a turn."""

    def schemas_for_user_text(self, user_text: str) -> list[dict[str, Any]]:
        """Tool schemas applicable to ``user_text`` (includes the intent gate)."""
        ...

    def run(self, name: str, arguments: str) -> str:
        """Run tool ``name`` with JSON ``arguments``, returning the tool result."""
        ...


class _FunctionTableRegistry:
    """Adapts a legacy schema list + function table to the registry tool surface
    (``tool_schemas`` / ``tool_handler``). Lets the runtime wrap the legacy tools
    already injected on ``services`` WITHOUT re-importing the static TOOL_SCHEMAS (N5)."""

    def __init__(self, schemas: list[dict[str, Any]], functions: dict[str, Callable[..., Any]]) -> None:
        self._schemas = list(schemas)
        self._functions = dict(functions)

    def tool_schemas(self) -> list[dict[str, Any]]:
        return self._schemas

    def tool_handler(self, name: str) -> Callable[..., Any] | None:
        return self._functions.get(name)


class RegistryToolSet:
    """ToolSet backed by a registry: the host ``CapabilityRegistry`` (ToolPort tools)
    or a ``_FunctionTableRegistry`` over the legacy services tools."""

    def __init__(self, registry: Any) -> None:
        self._registry = registry

    @classmethod
    def from_function_table(
        cls,
        schemas: list[dict[str, Any]],
        functions: dict[str, Callable[..., Any]],
    ) -> "RegistryToolSet":
        return cls(_FunctionTableRegistry(schemas, functions))

    def schemas_for_user_text(self, user_text: str) -> list[dict[str, Any]]:
        return tool_schemas_for_user_text(user_text, self._registry.tool_schemas())

    def run(self, name: str, arguments: str) -> str:
        handler = self._registry.tool_handler(name)
        if handler is None:
            return tool_error("UNKNOWN_TOOL", f"未知工具：{name}")
        try:
            parsed_args: dict[str, Any] = json.loads(arguments or "{}")
        except json.JSONDecodeError as exc:
            return tool_error("INVALID_TOOL_ARGUMENTS_JSON", f"工具参数不是合法 JSON：{exc}")
        try:
            if name == "inspect_screen":
                clear_last_screen_analysis_metadata()
            result = handler(**parsed_args)
        except ScreenToolError as exc:
            # A ToolPort handler signals errors by raising; the legacy str handler
            # returns tool_error itself (so it never reaches here).
            return tool_error(exc.code, exc.message)
        except TypeError as exc:
            return tool_error("TOOL_ARGUMENTS_MISMATCH", f"工具参数不匹配：{exc}")
        except Exception as exc:  # noqa: BLE001 -- mirror run_local_tool's catch-all
            return tool_error("TOOL_EXECUTION_ERROR", f"工具执行失败：{exc}")
        # A ToolPort handler returns a dict -> wrap once; a legacy string passes through.
        return result if isinstance(result, str) else tool_success(result)

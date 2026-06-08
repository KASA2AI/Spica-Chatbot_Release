"""CapabilityRegistry (Phase 5).

A name -> factory registry per capability kind. Built-in adapters register here;
``AppHost`` then resolves the active instance by the name in config (e.g.
``config.llm.provider``). This is what makes "swap the engine by changing a
config name, with no core code change" work, and is the seam Phase 8 plugins
register into.

Factories are zero-arg-or-kwargs callables returning a ready instance; resolve
passes through kwargs so the host can supply construction context (client,
config, paths) without the registry knowing the concrete types.
"""

from __future__ import annotations

from typing import Any, Callable

Factory = Callable[..., Any]


def _tool_schema_name(schema: dict[str, Any]) -> str:
    """Resolve a tool name from either a flat schema (top-level ``name``) or an
    OpenAI-style nested one (``{"type": "function", "function": {"name": ...}}``).

    Inlined here (a few lines) rather than importing ``agent_tools``'s equivalent,
    so ``spica`` does not depend on ``agent_tools`` just for this.
    """
    name = schema.get("name")
    if isinstance(name, str) and name:
        return name
    function = schema.get("function")
    if isinstance(function, dict):
        nested = function.get("name")
        if isinstance(nested, str) and nested:
            return nested
    return ""


class CapabilityRegistry:
    def __init__(self) -> None:
        self._llm: dict[str, Factory] = {}
        self._tts: dict[str, Factory] = {}
        self._visual: dict[str, Factory] = {}
        self._memory: dict[str, Factory] = {}
        self._tools: dict[str, tuple[dict[str, Any], Callable[..., Any]]] = {}

    # -- registration ---------------------------------------------------------
    def register_llm(self, name: str, factory: Factory) -> None:
        self._llm[name] = factory

    def register_tts(self, name: str, factory: Factory) -> None:
        self._tts[name] = factory

    def register_visual(self, name: str, factory: Factory) -> None:
        self._visual[name] = factory

    def register_memory(self, name: str, factory: Factory) -> None:
        self._memory[name] = factory

    def register_tool(self, schema: dict[str, Any], handler: Callable[..., Any]) -> None:
        # Accept both flat and OpenAI-nested schemas; the schema is stored VERBATIM
        # (tool_schemas() returns it unchanged), only the lookup name is resolved.
        name = _tool_schema_name(schema)
        if not name:
            raise ValueError("tool schema must include a 'name' (top-level or under 'function')")
        self._tools[name] = (schema, handler)

    # -- resolution -----------------------------------------------------------
    def resolve_llm(self, name: str, **kwargs: Any) -> Any:
        return self._resolve(self._llm, "llm", name, **kwargs)

    def resolve_tts(self, name: str, **kwargs: Any) -> Any:
        return self._resolve(self._tts, "tts", name, **kwargs)

    def resolve_visual(self, name: str, **kwargs: Any) -> Any:
        return self._resolve(self._visual, "visual", name, **kwargs)

    def resolve_memory(self, name: str, **kwargs: Any) -> Any:
        return self._resolve(self._memory, "memory", name, **kwargs)

    # -- tool resolution (C7: the registry-backed ToolSet reads tools from here) --
    def tool_schemas(self) -> list[dict[str, Any]]:
        return [schema for (schema, _handler) in self._tools.values()]

    def tool_handler(self, name: str) -> Callable[..., Any] | None:
        entry = self._tools.get(name)
        return entry[1] if entry else None

    # -- introspection (used by ManagementSurface in Phase 8) -----------------
    def list_adapters(self, kind: str) -> list[str]:
        table = {
            "llm": self._llm,
            "tts": self._tts,
            "visual": self._visual,
            "memory": self._memory,
            "tool": self._tools,
        }.get(kind)
        if table is None:
            raise ValueError(f"unknown capability kind: {kind}")
        return sorted(table.keys())

    @staticmethod
    def _resolve(table: dict[str, Factory], kind: str, name: str, **kwargs: Any) -> Any:
        factory = table.get(name)
        if factory is None:
            available = ", ".join(sorted(table)) or "(none)"
            raise KeyError(f"no {kind} adapter registered as {name!r}; available: {available}")
        return factory(**kwargs)

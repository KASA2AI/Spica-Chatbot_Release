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

from typing import Any, Callable, NamedTuple

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


class ToolEntry(NamedTuple):
    """One registered tool's stored record (OO migration Phase 4R).

    Field ORDER matches the historical anonymous 7-tuple, so the NamedTuple is a
    strict drop-in (it still IS a tuple). Field semantics:

    - ``schema``: stored VERBATIM (flat or OpenAI-nested); ``tool_schemas()``
      returns it unchanged.
    - ``handler``: the callable ``tools.run`` dispatches to.
    - ``available``: optional STATE predicate -- the tool is only offered while
      it returns True (e.g. watch/note during companion play); None = always.
    - ``intent_gated``: whether the router wordlist pre-filter applies (supply
      gating only -- it never hijacks/swallows the message).
    - ``chainable``: True lets the streaming tool round loop (P1); False (every
      current tool) keeps the single-round path.
    - ``compact_output``: optional followup-prompt compactor (P1).
    - ``effect``: "read" | "write" | "act" footprint tier (P2) -- audit metadata;
      the SAFETY boundary for "act" stays the host-closure port pattern.

    New metadata (Phase 4/9 candidates) must be added HERE as a named field --
    never by widening an anonymous tuple (test_registry pins ``_fields``).
    """

    schema: dict[str, Any]
    handler: Callable[..., Any]
    available: Callable[[], bool] | None
    intent_gated: bool
    chainable: bool
    compact_output: Callable[[str], str] | None
    effect: str


class CapabilityRegistry:
    def __init__(self) -> None:
        self._llm: dict[str, Factory] = {}
        self._tts: dict[str, Factory] = {}
        self._visual: dict[str, Factory] = {}
        self._memory: dict[str, Factory] = {}
        # Field semantics documented on ToolEntry (Phase 4R: the historical
        # anonymous 7-tuple, now with named fields).
        self._tools: dict[str, ToolEntry] = {}

    # -- registration ---------------------------------------------------------
    def register_llm(self, name: str, factory: Factory) -> None:
        self._llm[name] = factory

    def register_tts(self, name: str, factory: Factory) -> None:
        self._tts[name] = factory

    def register_visual(self, name: str, factory: Factory) -> None:
        self._visual[name] = factory

    def register_memory(self, name: str, factory: Factory) -> None:
        self._memory[name] = factory

    def register_tool(
        self,
        schema: dict[str, Any],
        handler: Callable[..., Any],
        *,
        available: Callable[[], bool] | None = None,
        intent_gated: bool = True,
        chainable: bool = False,
        compact_output: Callable[[str], str] | None = None,
        effect: str = "read",
    ) -> None:
        # Accept both flat and OpenAI-nested schemas; the schema is stored VERBATIM
        # (tool_schemas() returns it unchanged), only the lookup name is resolved.
        #
        # ``available`` (trigger-layer refactor): an optional STATE predicate -- the
        # tool is only offered while it returns True (e.g. watch_game_screen during
        # companion play). ``intent_gated=False`` skips the router wordlist
        # pre-filter: the schema is offered whenever available, and "call or not"
        # is the LLM's structured decision via the description. Defaults reproduce
        # the pre-refactor behaviour exactly (always available, word-gated).
        #
        # ``chainable`` (P1): True lets the streaming tool round loop -- after this
        # tool runs, the LLM is probed again WITH tools and may chain further calls
        # (browser-style flows). False (every current tool) keeps the single-round
        # path byte-identical. ``compact_output`` (P1): optional callable applied to
        # this tool's output before it enters a followup prompt (inspect_screen
        # registers its historical compactor; a global hard cap backstops the rest).
        #
        # ``effect`` (P2 risk tiers): "read" | "write" | "act". Metadata for
        # audit/logging and future policy hooks -- the SAFETY boundary for "act"
        # tools is the port pattern (a whitelisted action surface behind a host
        # closure; never exec/eval/shell/arbitrary URLs), not this flag.
        if effect not in ("read", "write", "act"):
            raise ValueError(f"tool effect must be read|write|act, got {effect!r}")
        name = _tool_schema_name(schema)
        if not name:
            raise ValueError("tool schema must include a 'name' (top-level or under 'function')")
        self._tools[name] = ToolEntry(
            schema=schema,
            handler=handler,
            available=available,
            intent_gated=intent_gated,
            chainable=chainable,
            compact_output=compact_output,
            effect=effect,
        )

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
        # State-filtered: a tool with an ``available`` predicate is only offered
        # while it returns True (a failing predicate must never break the turn).
        offered: list[dict[str, Any]] = []
        for entry in self._tools.values():
            if entry.available is not None:
                try:
                    if not entry.available():
                        continue
                except Exception:  # noqa: BLE001 -- a broken predicate hides the tool
                    continue
            offered.append(entry.schema)
        return offered

    def tool_handler(self, name: str) -> Callable[..., Any] | None:
        entry = self._tools.get(name)
        return entry.handler if entry else None

    def tool_intent_gated(self, name: str) -> bool:
        """Whether the router wordlist pre-filter applies to this tool (True for
        every pre-refactor tool; False = offered whenever available)."""
        entry = self._tools.get(name)
        return entry.intent_gated if entry else True

    def tool_chainable(self, name: str) -> bool:
        """Whether the streaming tool round may loop after this tool runs (P1).
        False for every tool that does not declare otherwise."""
        entry = self._tools.get(name)
        return entry.chainable if entry else False

    def tool_compact_output(self, name: str) -> Callable[[str], str] | None:
        """The tool's declared followup-prompt compactor, or None."""
        entry = self._tools.get(name)
        return entry.compact_output if entry else None

    def tool_effect(self, name: str) -> str:
        """The tool's declared footprint tier (P2): read | write | act."""
        entry = self._tools.get(name)
        return entry.effect if entry else "read"

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

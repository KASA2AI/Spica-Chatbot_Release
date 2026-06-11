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
        # name -> (schema, handler, available, intent_gated, chainable,
        # compact_output, effect). available=None means always offered;
        # intent_gated=True means the router wordlist pre-filter applies (the
        # pre-refactor behaviour for every tool); chainable=False means a single
        # execution ends the tool round (P1); compact_output optionally shrinks
        # the tool's output before it enters a followup prompt (P1); effect (P2)
        # classifies the tool's footprint: "read" = pure observation, "write" =
        # mutates own-domain data, "act" = operates the user's environment /
        # starts jobs / claims shared resources.
        self._tools: dict[
            str,
            tuple[
                dict[str, Any],
                Callable[..., Any],
                Callable[[], bool] | None,
                bool,
                bool,
                Callable[[str], str] | None,
                str,
            ],
        ] = {}

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
        self._tools[name] = (
            schema, handler, available, intent_gated, chainable, compact_output, effect
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
        for schema, _handler, available, _gated, _chainable, _compact, _effect in self._tools.values():
            if available is not None:
                try:
                    if not available():
                        continue
                except Exception:  # noqa: BLE001 -- a broken predicate hides the tool
                    continue
            offered.append(schema)
        return offered

    def tool_handler(self, name: str) -> Callable[..., Any] | None:
        entry = self._tools.get(name)
        return entry[1] if entry else None

    def tool_intent_gated(self, name: str) -> bool:
        """Whether the router wordlist pre-filter applies to this tool (True for
        every pre-refactor tool; False = offered whenever available)."""
        entry = self._tools.get(name)
        return entry[3] if entry else True

    def tool_chainable(self, name: str) -> bool:
        """Whether the streaming tool round may loop after this tool runs (P1).
        False for every tool that does not declare otherwise."""
        entry = self._tools.get(name)
        return entry[4] if entry else False

    def tool_compact_output(self, name: str) -> Callable[[str], str] | None:
        """The tool's declared followup-prompt compactor, or None."""
        entry = self._tools.get(name)
        return entry[5] if entry else None

    def tool_effect(self, name: str) -> str:
        """The tool's declared footprint tier (P2): read | write | act."""
        entry = self._tools.get(name)
        return entry[6] if entry else "read"

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

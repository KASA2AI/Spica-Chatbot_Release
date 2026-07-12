"""Configuration manager (Phase 3).

Builds a validated :class:`AppConfig` from defaults, an optional
``data/config/app.yaml`` file, and environment overrides (env wins, so behaviour
is identical to today when no file is present). This module and ``secrets.py``
are the only places in business code permitted to read ``os.getenv`` (CLAUDE.md
#4); a guard test (``tests/test_no_getenv.py``) enforces that.

The legacy ``tts_config.json`` / ``visual_config.json`` consolidation into YAML
is intentionally deferred (see ``migrate``); their existing loaders are untouched
in this phase.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

import yaml
from dotenv import load_dotenv

from spica.config.env_roster import APP_ENV_MAP, RESPEAKER_ENV_MAP, SCREEN_ENV_MAP
from spica.config.environment_snapshot import EnvironmentSnapshot
from spica.config.immutable import freeze_config_tree, thaw_config_tree
from spica.config.schema import AppConfig

_REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = _REPO_ROOT / "data" / "config" / "app.yaml"


def screen_env_overrides() -> dict[str, str | None]:
    """Raw env strings for the screen domain (P0b step 1, F6 收编).

    The env NAMES live in ``env_roster.SCREEN_ENV_MAP``. Values are returned
    RAW (None when unset) and read at CALL time -- no dotenv priming here,
    exactly matching the loader's old direct ``os.getenv`` behaviour (the
    entry point primes env first, CLAUDE.md #10). Step 2a layers the env-side
    coercion on top in ``screen_env_config_overrides``.
    """
    return {field: os.getenv(name) for field, name in SCREEN_ENV_MAP.items()}


_SCREEN_ENV_BOOL_FIELDS = (
    "enabled", "reasoning", "preload", "ocr_enabled", "log_timing", "debug_save_images",
)
_SCREEN_FILE_STRING_FIELDS = (
    "provider",
    "model_id",
    "revision",
    "device",
    "ocr_engine",
)
_SCREEN_ENV_TRUE_WORDS = {"1", "true", "yes", "y", "on"}


def screen_env_config_overrides() -> dict[str, Any]:
    """Coerced, set-keys-only env overrides for the screen section (P0b 2a).

    Replicates the pre-2a loader's ENV-side semantics exactly (Layer B pins):
    - empty/whitespace-only env -> key OMITTED (falls through to file/default);
    - bools -> the 1/true/yes/y/on wordlist, case-insensitive; any other
      non-empty string coerces to False AND overrides the file value;
    - max_side -> included only when int()-parseable (an unparseable env int
      falls through to the file value -- unlike infer_timeout_sec, whose
      invalid env value IS included and coerces to the default downstream,
      skipping the file value: the pinned asymmetry);
    - strings -> whitespace-stripped (env values were always stripped; file
      values never are).

    The returned dict feeds both ``ScreenConfig`` resolution paths: the screen
    loader's merge (env > json > defaults) and ``_env_overrides`` below
    (env > app.yaml > defaults).
    """
    overrides: dict[str, Any] = {}
    for field, value in screen_env_overrides().items():
        cleaned = (value or "").strip()
        if not cleaned:
            continue
        if field in _SCREEN_ENV_BOOL_FIELDS:
            overrides[field] = cleaned.lower() in _SCREEN_ENV_TRUE_WORDS
        elif field == "max_side":
            try:
                int(cleaned)
            except ValueError:
                continue  # unparseable env int falls through to file (pinned)
            overrides[field] = cleaned
        else:
            overrides[field] = cleaned
    return overrides


def respeaker_env_overrides() -> dict[str, str | None]:
    """Raw env strings for the ReSpeaker hardware layer (P0b step 1, D2).

    Same contract as ``screen_env_overrides``: raw values, call-time reads,
    coercion stays at the consumer (``hardware/respeaker``).
    """
    return {field: os.getenv(name) for field, name in RESPEAKER_ENV_MAP.items()}


class ConfigManager:
    def __init__(self, config_path: str | Path | None = None) -> None:
        self.config_path = Path(config_path) if config_path else DEFAULT_CONFIG_PATH

    def load(self) -> AppConfig:
        """defaults -> optional yaml file -> env overrides -> validate."""
        self._ensure_env_loaded()
        names = set(APP_ENV_MAP.values()) | set(SCREEN_ENV_MAP.values())
        snapshot = EnvironmentSnapshot.from_mapping(
            {name: value for name in names if (value := os.getenv(name)) is not None},
            layer="process",
        )
        return self.resolve_snapshot(self._read_yaml(self.config_path), snapshot).to_app_config()

    def resolve_snapshot(
        self,
        raw_document: dict[str, Any],
        environment_snapshot: EnvironmentSnapshot,
    ) -> "ConfigResolution":
        """Resolve one document using explicit environment values.

        This is the production-owner seam used by both normal startup and the
        Config Studio.  It does not read or mutate process environment state.
        """
        _validate_document_graph(raw_document)
        environment_overrides = self._env_overrides_from_snapshot(environment_snapshot)
        data = self.merge({}, raw_document)
        data = self.merge(data, environment_overrides)
        data = self.migrate(data)
        config = self.validate(data)
        return ConfigResolution.from_resolved(
            config=config,
            raw_document=raw_document,
            environment_overrides=environment_overrides,
            environment_snapshot=environment_snapshot,
        )

    # -- sources --------------------------------------------------------------

    @staticmethod
    def _ensure_env_loaded() -> None:
        load_dotenv(_REPO_ROOT / "xiaosan.env")
        load_dotenv(_REPO_ROOT.parent / "xiaosan.env", override=False)

    @staticmethod
    def _read_yaml(path: Path) -> dict[str, Any]:
        if not path.is_file():
            return {}
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
        return loaded if isinstance(loaded, dict) else {}

    @staticmethod
    def _env_overrides() -> dict[str, Any]:
        """Map the historical env knobs onto the AppConfig shape.

        Only keys whose env var is set to a non-empty value are included, so an
        unset/empty var falls through to the file/default -- exactly matching the
        old ``int(os.getenv(X) or N)`` / ``os.getenv(X) or default`` behaviour.
        """
        names = set(APP_ENV_MAP.values()) | set(SCREEN_ENV_MAP.values())
        snapshot = EnvironmentSnapshot.from_mapping(
            {name: value for name in names if (value := os.getenv(name)) is not None},
            layer="process",
        )
        return ConfigManager._env_overrides_from_snapshot(snapshot)

    @staticmethod
    def _env_overrides_from_snapshot(
        snapshot: EnvironmentSnapshot,
    ) -> dict[str, Any]:
        llm: dict[str, Any] = {}
        if snapshot.get("MODEL"):
            llm["model"] = snapshot.get("MODEL")
        if snapshot.get("OPENAI_BASE_URL"):
            llm["base_url"] = snapshot.get("OPENAI_BASE_URL")
        if snapshot.get("REASONING_EFFORT"):
            llm["reasoning_effort"] = snapshot.get("REASONING_EFFORT")

        memory: dict[str, Any] = {}
        for env_key, field in (
            ("RECENT_MEMORY_TURNS", "recent_memory_turns"),
            ("RECENT_CONTEXT_LIMIT", "recent_context_limit"),
            ("LONG_TERM_MEMORY_LIMIT", "long_term_memory_limit"),
            ("LONG_TERM_MEMORY_BUDGET_CHARS", "long_term_memory_budget_chars"),
            ("RECENT_TURN_CHAR_LIMIT", "recent_turn_char_limit"),
            ("MAX_LONG_TERM_MEMORIES", "max_long_term_memories"),
        ):
            value = snapshot.get(env_key)
            if value:
                memory[field] = int(value)

        character: dict[str, Any] = {}
        if snapshot.get("SPICA_USER_NAME"):
            character["interlocutor_name"] = snapshot.get("SPICA_USER_NAME")
        if snapshot.get("SPICA_CHARACTER_PROFILE"):
            character["profile_override"] = snapshot.get("SPICA_CHARACTER_PROFILE")
        if snapshot.get("SPICA_SKILL_DIR"):
            character["skill_dir"] = snapshot.get("SPICA_SKILL_DIR")

        stream: dict[str, Any] = {}
        for env_key, field in (
            ("PLAY_UNIT_MIN_CHARS", "play_unit_min_chars"),
            ("PLAY_UNIT_MAX_CHARS", "play_unit_max_chars"),
            ("VISUAL_STREAM_WORKERS", "visual_stream_workers"),
        ):
            value = snapshot.get(env_key)
            if value:
                stream[field] = int(value)

        # Reaction-judge LLM endpoint (the ONLY galgame fields with env names): the
        # non-secret base_url + model halves of a swappable judge endpoint. The key
        # half is the secret JUDGE_API_KEY (secrets.py). Roster: env_roster.APP_ENV_MAP.
        galgame: dict[str, Any] = {}
        if snapshot.get("JUDGE_MODEL"):
            galgame["reaction_judge_model"] = snapshot.get("JUDGE_MODEL")
        if snapshot.get("JUDGE_BASE_URL"):
            galgame["reaction_judge_base_url"] = snapshot.get("JUDGE_BASE_URL")
        if snapshot.get("JUDGE_REASONING_EFFORT"):
            galgame["reaction_judge_reasoning_effort"] = snapshot.get(
                "JUDGE_REASONING_EFFORT"
            )

        overrides: dict[str, Any] = {}
        if llm:
            overrides["llm"] = llm
        if memory:
            overrides["memory"] = memory
        if character:
            overrides["character"] = character
        if stream:
            overrides["stream"] = stream
        if galgame:
            overrides["galgame"] = galgame
        # P0b 2a: the screen section folds env with the SCREEN coercion rules
        # (wordlist bools, clamp ints) -- NOT the loud int() the knobs above use.
        screen = _screen_env_config_overrides_from_snapshot(snapshot)
        if screen:
            overrides["screen"] = screen
        if snapshot.get("MAX_TOOL_ROUNDS"):
            overrides["max_tool_rounds"] = int(snapshot.get("MAX_TOOL_ROUNDS"))
        return overrides

    # -- helpers --------------------------------------------------------------

    @staticmethod
    def merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
        """Recursive dict merge; ``override`` wins. Inputs are not mutated."""
        result = dict(base)
        for key, value in override.items():
            if isinstance(value, dict) and isinstance(result.get(key), dict):
                result[key] = ConfigManager.merge(result[key], value)
            else:
                result[key] = value
        return result

    def migrate(self, data: dict[str, Any]) -> dict[str, Any]:
        # Phase 3: the tunable knobs were env-only, so there is no legacy on-disk
        # schema to migrate yet. Passthrough placeholder; legacy tts/visual JSON
        # consolidation is deferred to a later phase.
        return data

    @staticmethod
    def validate(data: dict[str, Any]) -> AppConfig:
        return AppConfig.model_validate(data)

    def save(self, config: AppConfig, path: str | Path | None = None) -> None:
        target = Path(path) if path else self.config_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            yaml.safe_dump(config.model_dump(), allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )


def _screen_env_config_overrides_from_snapshot(
    snapshot: EnvironmentSnapshot,
) -> dict[str, Any]:
    overrides: dict[str, Any] = {}
    for field, env_name in SCREEN_ENV_MAP.items():
        cleaned = (snapshot.get(env_name) or "").strip()
        if not cleaned:
            continue
        if field in _SCREEN_ENV_BOOL_FIELDS:
            overrides[field] = cleaned.lower() in _SCREEN_ENV_TRUE_WORDS
        elif field == "max_side":
            try:
                int(cleaned)
            except ValueError:
                continue
            overrides[field] = cleaned
        else:
            overrides[field] = cleaned
    return overrides


def _validate_document_graph(
    document: Any,
    *,
    max_depth: int = 64,
    max_collection_items: int = 4096,
    max_nodes: int = 50_000,
) -> None:
    active: set[int] = set()
    visited: set[int] = set()
    node_count = 0

    def visit(node: Any, depth: int) -> None:
        nonlocal node_count
        node_count += 1
        if node_count > max_nodes:
            raise ValueError("configuration document exceeds node budget")
        if depth > max_depth:
            raise ValueError("configuration document exceeds depth budget")
        if isinstance(node, Mapping):
            identity = id(node)
            if identity in active:
                raise ValueError("configuration document contains an alias cycle")
            if identity in visited:
                return
            if len(node) > max_collection_items:
                raise ValueError("configuration mapping exceeds item budget")
            active.add(identity)
            try:
                for key, value in node.items():
                    visit(key, depth + 1)
                    visit(value, depth + 1)
            finally:
                active.remove(identity)
            visited.add(identity)
        elif isinstance(node, (list, tuple)):
            identity = id(node)
            if identity in active:
                raise ValueError("configuration document contains an alias cycle")
            if identity in visited:
                return
            if len(node) > max_collection_items:
                raise ValueError("configuration list exceeds item budget")
            active.add(identity)
            try:
                for value in node:
                    visit(value, depth + 1)
            finally:
                active.remove(identity)
            visited.add(identity)

    visit(document, 0)


@dataclass(frozen=True)
class ResolutionSource:
    kind: str
    environment_variable: str | None = None
    environment_layer: str | None = None


@dataclass(frozen=True, repr=False)
class ResolvedLeaf:
    next_launch_value: Any
    source: ResolutionSource

    def __repr__(self) -> str:
        return f"ResolvedLeaf(<redacted>, source={self.source.kind!r})"


class ConfigResolution:
    """Immutable resolved data plus per-leaf provenance."""

    __slots__ = ("_config_data", "_leaves")

    def __init__(
        self,
        config_data: dict[str, Any],
        leaves: dict[tuple[str | int, ...], ResolvedLeaf],
    ) -> None:
        self._config_data = freeze_config_tree(config_data)
        self._leaves = MappingProxyType(dict(leaves))

    @classmethod
    def from_resolved(
        cls,
        *,
        config: AppConfig,
        raw_document: dict[str, Any],
        environment_overrides: dict[str, Any],
        environment_snapshot: EnvironmentSnapshot,
    ) -> "ConfigResolution":
        config_data = config.model_dump()
        environment_paths = set(_flatten_paths(environment_overrides))
        leaves: dict[tuple[str | int, ...], ResolvedLeaf] = {}
        for path, value in _flatten_values(config_data).items():
            env_name = _environment_name_for_path(path)
            if env_name is not None and environment_snapshot.is_tainted(env_name):
                source = ResolutionSource(
                    kind="secret_tainted_env_override",
                    environment_variable=env_name,
                    environment_layer=environment_snapshot.layer_for(env_name),
                )
                value = None
            elif path in environment_paths and env_name is not None:
                source = ResolutionSource(
                    kind="env_override",
                    environment_variable=env_name,
                    environment_layer=environment_snapshot.layer_for(env_name),
                )
            elif _document_effectively_owns_path(raw_document, path):
                source = ResolutionSource(kind="file")
            else:
                source = ResolutionSource(kind="default")
            leaves[path] = ResolvedLeaf(
                next_launch_value=freeze_config_tree(value),
                source=source,
            )
        return cls(config_data=config_data, leaves=leaves)

    def to_app_config(self) -> AppConfig:
        return AppConfig.model_validate(thaw_config_tree(self._config_data))

    def leaf(self, path: tuple[str | int, ...]) -> ResolvedLeaf:
        return self._leaves[path]

    def resolved_at(self, path: tuple[str | int, ...]) -> ResolvedLeaf:
        exact = self._leaves.get(path)
        if exact is not None:
            return exact
        descendants = [
            leaf
            for leaf_path, leaf in self._leaves.items()
            if leaf_path[: len(path)] == path
        ]
        if not descendants:
            raise KeyError(path)
        sources = {leaf.source for leaf in descendants}
        source = sources.pop() if len(sources) == 1 else ResolutionSource(kind="mixed")
        return ResolvedLeaf(next_launch_value=self._value_at(path), source=source)

    def __repr__(self) -> str:
        return f"ConfigResolution(<{len(self._leaves)} leaves>)"

    def _value_at(self, path: tuple[str | int, ...]) -> Any:
        current = self._config_data
        for segment in path:
            current = current[segment]
        return current


def _environment_name_for_path(path: tuple[str | int, ...]) -> str | None:
    if all(isinstance(segment, str) for segment in path):
        dotted = ".".join(path)
        if dotted.startswith("screen."):
            return SCREEN_ENV_MAP.get(dotted.removeprefix("screen."))
        return APP_ENV_MAP.get(dotted)
    return None


def _flatten_paths(node: Any, prefix: tuple[str | int, ...] = ()) -> list[tuple[str | int, ...]]:
    if isinstance(node, dict):
        paths: list[tuple[str | int, ...]] = []
        for key, value in node.items():
            paths.extend(_flatten_paths(value, prefix + (str(key),)))
        return paths
    if isinstance(node, list):
        paths = []
        for index, value in enumerate(node):
            paths.extend(_flatten_paths(value, prefix + (index,)))
        return paths
    return [prefix]


def _flatten_values(
    node: Any,
    prefix: tuple[str | int, ...] = (),
) -> dict[tuple[str | int, ...], Any]:
    if isinstance(node, dict) and node:
        result: dict[tuple[str | int, ...], Any] = {}
        for key, value in node.items():
            result.update(_flatten_values(value, prefix + (str(key),)))
        return result
    if isinstance(node, list) and node:
        result = {}
        for index, value in enumerate(node):
            result.update(_flatten_values(value, prefix + (index,)))
        return result
    return {prefix: node}


def _document_contains_path(node: Any, path: tuple[str | int, ...]) -> bool:
    current = node
    for segment in path:
        if isinstance(segment, str) and isinstance(current, dict) and segment in current:
            current = current[segment]
        elif isinstance(segment, int) and isinstance(current, list) and segment < len(current):
            current = current[segment]
        else:
            return False
    return True


def _document_effectively_owns_path(
    node: Any,
    path: tuple[str | int, ...],
) -> bool:
    if not _document_contains_path(node, path):
        return False
    if len(path) != 2 or path[0] != "screen" or not isinstance(node, dict):
        return True
    screen = node.get("screen")
    if not isinstance(screen, dict):
        return True
    field = path[1]
    raw_value = screen.get(field)
    if field in (*_SCREEN_FILE_STRING_FIELDS, "dtype", "capture_format"):
        return bool(raw_value)
    if field == "max_side":
        try:
            int(raw_value)
        except (TypeError, ValueError):
            return False
    if field == "infer_timeout_sec":
        try:
            return float(raw_value) > 0
        except (TypeError, ValueError):
            return False
    return True

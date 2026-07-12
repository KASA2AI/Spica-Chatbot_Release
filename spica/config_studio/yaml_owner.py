"""Construction-free strict YAML loading for Config Studio owners."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import yaml
from yaml.events import AliasEvent
from yaml.nodes import MappingNode


class YamlOwnerError(ValueError):
    """A bounded YAML contract failure with no source content in its repr."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code

    def __repr__(self) -> str:
        return f"YamlOwnerError(code={self.code!r})"


class _DuplicateKeyError(yaml.YAMLError):
    pass


class _UniqueKeySafeLoader(yaml.SafeLoader):
    def construct_mapping(self, node: MappingNode, deep: bool = False) -> Any:
        if not isinstance(node, MappingNode):
            return super().construct_mapping(node, deep=deep)
        seen: set[Any] = set()
        for key_node, _ in node.value:
            # A merge key is an instruction, not a second lexical declaration.
            # The referenced mapping is checked when its own node is constructed.
            if key_node.tag == "tag:yaml.org,2002:merge":
                continue
            key = self.construct_object(key_node, deep=deep)
            try:
                duplicate = key in seen
            except TypeError:
                # The production safe loader will reject an unhashable mapping key.
                continue
            if duplicate:
                raise _DuplicateKeyError("duplicate YAML mapping key")
            seen.add(key)
        return super().construct_mapping(node, deep=deep)


def load_yaml_mapping(
    content: bytes,
    *,
    reject_aliases: bool,
) -> dict[str, Any]:
    """Load one UTF-8 YAML mapping with strict lexical key semantics.

    Read-only owners may retain aliases for bounded projection. Authoring owners
    reject alias references so one typed operation cannot mutate another branch
    through shared object identity.
    """

    if not isinstance(content, bytes):
        raise TypeError("YAML content must be bytes")
    try:
        text = content.decode("utf-8")
        root_node = yaml.compose(text, Loader=yaml.SafeLoader) if text else None
        if root_node is None:
            return {}
        if reject_aliases and any(
            isinstance(event, AliasEvent)
            for event in yaml.parse(text, Loader=yaml.SafeLoader)
        ):
            raise YamlOwnerError("YAML_ALIAS_UNSUPPORTED")
        loaded = yaml.load(text, Loader=_UniqueKeySafeLoader)
    except YamlOwnerError:
        raise
    except _DuplicateKeyError as exc:
        raise YamlOwnerError("YAML_DUPLICATE_KEY") from exc
    except (UnicodeError, yaml.YAMLError, RecursionError) as exc:
        raise YamlOwnerError("YAML_INVALID") from exc
    if not isinstance(loaded, Mapping):
        raise YamlOwnerError("YAML_ROOT_NOT_MAPPING")
    return dict(loaded)


def reject_yaml_alias_graph(
    value: Any,
    *,
    max_depth: int = 64,
    max_nodes: int = 8192,
) -> None:
    """Reject repeated mutable YAML-container identity without recursive descent."""

    pending: list[tuple[Any, int]] = [(value, 0)]
    seen: set[int] = set()
    nodes = 0
    while pending:
        current, depth = pending.pop()
        if not isinstance(current, (Mapping, list)):
            continue
        if depth > max_depth or nodes >= max_nodes:
            raise YamlOwnerError("YAML_GRAPH_UNSUPPORTED")
        identity = id(current)
        if identity in seen:
            raise YamlOwnerError("YAML_ALIAS_UNSUPPORTED")
        seen.add(identity)
        nodes += 1
        children = current.values() if isinstance(current, Mapping) else current
        pending.extend((child, depth + 1) for child in children)


__all__ = ["YamlOwnerError", "load_yaml_mapping", "reject_yaml_alias_graph"]

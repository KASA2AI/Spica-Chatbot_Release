"""Layering guards for the platform core.

Three AST-based invariants over ``spica/``:

- **Qt isolation** (CLAUDE.md #1): nothing under ``spica/`` may import PySide / Qt /
  any GUI binding, so the host stays framework-agnostic.
- **N3-layer** (C4): ``spica`` must not import the ``agent`` package -- it was
  deleted in C4; the conversation domain lives in ``spica/conversation`` and the
  turn runtime in ``spica/runtime``. (``agent_tools`` is a separate package and is
  fine.)
- **N1-final** (C4): RuntimeEvent production is confined to the turn facade. The
  pure transform layers -- the conversation domain and the turn stages -- must not
  import ``spica.core.events``; they return ``ctx`` and never emit.

If any goes red, a real leak/cycle was introduced. Fix it; do NOT delete a guard
or add an exemption. The scans are AST-based, so they also catch imports hidden
behind ``if TYPE_CHECKING:`` guards.
"""

import ast
import importlib
import unittest
from pathlib import Path

SPICA_ROOT = Path(__file__).resolve().parents[1] / "spica"

# Subpackages that make up the platform skeleton. Importing each confirms the
# skeleton is importable with no errors or cycles.
SPICA_PACKAGES = [
    "spica",
    "spica.host",
    "spica.core",
    "spica.config",
    "spica.ports",
    "spica.plugins",
    "spica.runtime",
    "spica.adapters",
    "spica.memory",
    "spica.galgame",
    "spica.adapters.game_memory",
    "spica.adapters.game_launcher",
    "spica.adapters.window_locator",
    "spica.adapters.screen_capture",
    "spica.adapters.ocr",
]


def _is_banned(top_level_module: str) -> bool:
    return (
        top_level_module.startswith("PySide")
        or top_level_module.startswith("PyQt")
        or top_level_module.startswith("shiboken")
    )


def _qt_imports_in(path: Path) -> list[str]:
    """Return human-readable descriptions of any Qt imports in ``path``."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    violations: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _is_banned(alias.name.split(".")[0]):
                    violations.append(f"line {node.lineno}: import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            # node.module is None for bare relative imports (`from . import x`),
            # which can never reach a Qt top-level package.
            if node.module and _is_banned(node.module.split(".")[0]):
                violations.append(f"line {node.lineno}: from {node.module} import ...")
    return violations


def _agent_imports_in(path: Path) -> list[str]:
    """Return any imports of the ``agent`` package (N3-layer). ``agent_tools`` is a
    separate top-level package and is intentionally allowed."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    violations: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] == "agent":
                    violations.append(f"line {node.lineno}: import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.module.split(".")[0] == "agent":
                violations.append(f"line {node.lineno}: from {node.module} import ...")
    return violations


# Pure transform layers: they return ``ctx`` and must never produce RuntimeEvent.
TRANSFORM_LAYER_FILES = [
    SPICA_ROOT / "runtime" / "stages.py",
    *sorted((SPICA_ROOT / "conversation").rglob("*.py")),
]


def _imports_runtime_events_in(path: Path) -> list[str]:
    """Return any import of ``spica.core.events`` (where RuntimeEvent lives)."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    violations: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "spica.core.events":
            violations.append(f"line {node.lineno}: from spica.core.events import ...")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "spica.core.events":
                    violations.append(f"line {node.lineno}: import {alias.name}")
    return violations


class LayeringGuardTest(unittest.TestCase):
    def test_no_qt_imports_under_spica(self):
        offenders: dict[str, list[str]] = {}
        for path in sorted(SPICA_ROOT.rglob("*.py")):
            found = _qt_imports_in(path)
            if found:
                offenders[str(path.relative_to(SPICA_ROOT.parent))] = found

        self.assertEqual(
            offenders,
            {},
            msg=(
                "spica/ must not import Qt (CLAUDE.md INVARIANT #1). "
                f"Offending imports: {offenders}"
            ),
        )

    def test_spica_packages_import_cleanly(self):
        for name in SPICA_PACKAGES:
            with self.subTest(package=name):
                importlib.import_module(name)

    def test_no_agent_imports_under_spica(self):
        # N3-layer (C4): spica is self-contained; the agent package is deleted.
        offenders: dict[str, list[str]] = {}
        for path in sorted(SPICA_ROOT.rglob("*.py")):
            found = _agent_imports_in(path)
            if found:
                offenders[str(path.relative_to(SPICA_ROOT.parent))] = found

        self.assertEqual(
            offenders,
            {},
            msg=(
                "N3-layer: spica/ must not import the agent package (deleted in C4 -- "
                "domain is spica.conversation, runtime is spica.runtime). agent_tools "
                f"is a separate package and is fine. Offending imports: {offenders}"
            ),
        )

    def test_transform_layers_do_not_produce_runtime_events(self):
        # N1-final (C4): only the turn facade (run_turn) produces RuntimeEvent. The
        # conversation domain and the turn stages are pure (ctx, ...) -> ctx
        # transforms; importing the event type would be the first step to emitting.
        offenders: dict[str, list[str]] = {}
        for path in TRANSFORM_LAYER_FILES:
            found = _imports_runtime_events_in(path)
            if found:
                offenders[str(path.relative_to(SPICA_ROOT.parent))] = found

        self.assertEqual(
            offenders,
            {},
            msg=(
                "N1-final: the conversation domain + turn stages must not import "
                "spica.core.events (RuntimeEvent). Stages return ctx; only run_turn / "
                f"the orchestrator emit. Offending imports: {offenders}"
            ),
        )


if __name__ == "__main__":
    unittest.main()

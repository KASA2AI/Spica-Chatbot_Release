"""Phase 2 layering guard: the platform core must never import Qt.

INVARIANT (CLAUDE.md #1): nothing under ``spica/`` may import PySide / Qt / any
GUI binding. This keeps the host framework-agnostic, so a future Web/React front
end is just another subscriber to the host -- the core stays untouched.

If this test goes red, a real Qt leak has been introduced into ``spica/``. Fix
the leak. Do NOT delete this test or add an exemption.

The scan is AST-based, so it also catches Qt imports hidden behind
``if TYPE_CHECKING:`` guards.
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


if __name__ == "__main__":
    unittest.main()

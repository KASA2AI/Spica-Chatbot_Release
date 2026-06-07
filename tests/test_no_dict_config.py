"""C3b guard (INVARIANT N3-config): the runtime runs on typed deps, not a dict.

After C3b the runtime turn reads configuration from ``deps.config`` (a typed
``AppConfig``) and capability ports from ``deps.llm`` / ``deps.memory`` -- never
``services.config`` (the legacy dict) and never the ``services.llm_adapter`` /
``services.memory_adapter`` dual-field fallback. The single place allowed to
bridge a legacy dict-config services bundle into typed deps is
``spica/runtime/deps.py`` (``TurnDeps.from_legacy_services`` /
``from_services``), allowlisted like ``exec_strategy.py`` is for N4.

AST-based access scan (like ``test_no_getenv`` / ``test_no_raw_threadpool``):
bans ``services.config`` / ``services.llm_adapter`` / ``services.memory_adapter``
attribute reads under ``spica/runtime/``.
"""

import ast
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_DIR = REPO_ROOT / "spica" / "runtime"

# The one module allowed to read the legacy services bundle (the deps bridge).
ALLOWLIST = {"spica/runtime/deps.py"}
# Legacy dict config + the client/adapter dual-field components.
BANNED_ATTRS = {"config", "llm_adapter", "memory_adapter"}


def _legacy_services_reads(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    hits: list[str] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "services"
            and node.attr in BANNED_ATTRS
        ):
            hits.append(f"line {node.lineno}: services.{node.attr}")
    return hits


class NoDictConfigGuardTest(unittest.TestCase):
    def test_runtime_runs_on_typed_deps_not_dict_config(self):
        offenders: dict[str, list[str]] = {}
        for path in sorted(RUNTIME_DIR.rglob("*.py")):
            rel = path.relative_to(REPO_ROOT).as_posix()
            if rel in ALLOWLIST:
                continue
            hits = _legacy_services_reads(path)
            if hits:
                offenders[rel] = hits

        self.assertEqual(
            offenders,
            {},
            msg=(
                "Runtime must read config from deps.config and ports from "
                "deps.llm/deps.memory (N3-config), not the legacy services dict / "
                f"adapter dual-field. Bridge via spica.runtime.deps. {offenders}"
            ),
        )

    def test_allowlist_points_at_the_bridge(self):
        for rel in ALLOWLIST:
            self.assertTrue((REPO_ROOT / rel).is_file(), f"Stale allowlist entry: {rel}")


if __name__ == "__main__":
    unittest.main()

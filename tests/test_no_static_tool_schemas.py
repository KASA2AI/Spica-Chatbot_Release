"""N5 guard (C7): the runtime resolves tools via the registry, not static lists.

``spica/runtime`` must not import the static ``TOOL_SCHEMAS`` /
``default_tool_functions`` from ``agent_tools.function_tools`` -- ``inspect_screen``
is resolved through the CapabilityRegistry (production, a ToolPort) or a registry
adapted from the services-injected tool table (tests). The intent gate
(``tool_schemas_for_user_text`` / ``is_screen_intent_explicit``) and the dispatch
helpers (``tool_success`` / ``tool_error``) remain importable -- they are gate /
serialization logic, not the static tool registry.

AST-based import scan (like ``test_no_log_timing`` / ``test_no_dict_config``).
"""

import ast
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_DIR = REPO_ROOT / "spica" / "runtime"
BANNED_NAMES = {"TOOL_SCHEMAS", "default_tool_functions"}


def _static_tool_imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    hits: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith(
            "agent_tools.function_tools"
        ):
            for alias in node.names:
                if alias.name in BANNED_NAMES:
                    hits.append(f"line {node.lineno}: from {node.module} import {alias.name}")
    return hits


class NoStaticToolSchemasGuardTest(unittest.TestCase):
    def test_runtime_resolves_tools_via_registry_not_static_lists(self):
        offenders: dict[str, list[str]] = {}
        for path in sorted(RUNTIME_DIR.rglob("*.py")):
            hits = _static_tool_imports(path)
            if hits:
                offenders[path.relative_to(REPO_ROOT).as_posix()] = hits

        self.assertEqual(
            offenders,
            {},
            msg=(
                "N5: spica/runtime must resolve tools through the registry-backed "
                "ToolSet, not import the static TOOL_SCHEMAS / default_tool_functions. "
                f"Offenders: {offenders}"
            ),
        )


if __name__ == "__main__":
    unittest.main()

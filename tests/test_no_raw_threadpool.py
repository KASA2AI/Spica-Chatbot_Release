"""C2 guard (INVARIANT N4-concurrency): the turn fans out via ExecStrategy.

After C2, concurrency in the runtime turn is an injected policy. Stage /
orchestrator code must not spin up its own ``ThreadPoolExecutor`` -- the only
place allowed to own pools is ``spica/runtime/exec_strategy.py`` (the ``Threaded``
strategy). Streaming gets ``Threaded``, the sync path gets ``Inline``.

AST-based instantiation scan (like ``test_no_getenv`` / ``test_no_manual_reorder``):
catches ``ThreadPoolExecutor(...)`` whether referenced bare or as
``concurrent.futures.ThreadPoolExecutor``. Scope is ``spica/runtime/`` -- adapters
elsewhere may have their own legitimate pools; this invariant is about the turn.
"""

import ast
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_DIR = REPO_ROOT / "spica" / "runtime"

# The one module allowed to own thread pools: the concurrency strategy itself.
ALLOWLIST = {"spica/runtime/exec_strategy.py"}


def _threadpool_instantiations(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    hits: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else (
            func.attr if isinstance(func, ast.Attribute) else None
        )
        if name == "ThreadPoolExecutor":
            hits.append(f"line {node.lineno}")
    return hits


class NoRawThreadPoolGuardTest(unittest.TestCase):
    def test_runtime_stages_do_not_create_thread_pools(self):
        offenders: dict[str, list[str]] = {}
        for path in sorted(RUNTIME_DIR.rglob("*.py")):
            rel = path.relative_to(REPO_ROOT).as_posix()
            if rel in ALLOWLIST:
                continue
            hits = _threadpool_instantiations(path)
            if hits:
                offenders[rel] = hits

        self.assertEqual(
            offenders,
            {},
            msg=(
                "Raw ThreadPoolExecutor in the runtime turn is forbidden (N4). "
                f"Submit through the injected ExecStrategy instead. {offenders}"
            ),
        )

    def test_allowlist_points_at_a_real_strategy_module(self):
        for rel in ALLOWLIST:
            self.assertTrue((REPO_ROOT / rel).is_file(), f"Stale allowlist entry: {rel}")


if __name__ == "__main__":
    unittest.main()

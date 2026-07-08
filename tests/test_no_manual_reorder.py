"""C1 guard (INVARIANT N2): ordered release goes through the Sequencer.

After C1, the streaming turn must not reorder play units with a hand-rolled
index buffer (the old ``ready_units`` dict + ``next_emit`` pointer + ``ready_lock``
+ ``put_ready`` reorder loop). Ordering is the ``Sequencer`` primitive's job; the
orchestrator only drains a completion queue into it.

This is a pragmatic identifier guard in the same spirit as ``test_no_getenv``: an
AST scan bans the exact manual-reorder *identifiers* C1 removed from
``spica/runtime/`` (so docstrings naming the old buffer -- e.g. the Sequencer's
own "replaces ready_units" note -- don't false-positive) and positively asserts
the orchestrator imports + uses ``Sequencer``. A determined re-introduction under
new names would slip past, but the common regression -- copying the old buffer
back -- is caught.
"""

import ast
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_DIR = REPO_ROOT / "spica" / "runtime"
ORCHESTRATOR = RUNTIME_DIR / "orchestrator.py"

# The manual index-reorder buffer C1 deleted. Its return as a real identifier
# (variable / parameter / function name) is a regression.
BANNED_IDENTIFIERS = {"ready_units", "next_emit", "ready_lock", "put_ready"}


def _banned_identifiers(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    hits: list[str] = []
    for node in ast.walk(tree):
        name = None
        if isinstance(node, ast.Name):
            name = node.id
        elif isinstance(node, ast.arg):
            name = node.arg
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            name = node.name
        if name in BANNED_IDENTIFIERS:
            hits.append(f"line {getattr(node, 'lineno', '?')}: {name}")
    return hits


class NoManualReorderGuardTest(unittest.TestCase):
    def test_runtime_has_no_manual_reorder_buffer(self):
        offenders: dict[str, list[str]] = {}
        for path in sorted(RUNTIME_DIR.rglob("*.py")):
            hits = _banned_identifiers(path)
            if hits:
                offenders[path.relative_to(REPO_ROOT).as_posix()] = hits

        self.assertEqual(
            offenders,
            {},
            msg=(
                "Manual play-unit reorder is forbidden (N2). Release ordering must "
                f"go through spica.runtime.sequencer.Sequencer. {offenders}"
            ),
        )

    def test_orchestrator_uses_the_sequencer_primitive(self):
        text = ORCHESTRATOR.read_text(encoding="utf-8")
        self.assertIn(
            "from spica.runtime.sequencer import Sequencer", text,
            "orchestrator must import the Sequencer ordering primitive (N2).",
        )
        self.assertIn(
            "sequencer.complete(", text,
            "orchestrator must release units via sequencer.complete (N2).",
        )


if __name__ == "__main__":
    unittest.main()

"""N4-observe guard (C5): turn/stage timing goes through the injected TurnObserver.

The turn/stage orchestration layer under ``spica/runtime`` must not call
``log_timing`` directly -- it routes timing/logging through ``deps.observer``
(``span`` / ``mark`` / ``event``). ``spica/runtime/observer.py`` (the
``DefaultTurnObserver``) is the ONE place allowed to wrap ``log_timing``, like
``deps.py`` is the bridge allowlisted for N3-config.

N4-observe constrains ONLY the turn/stage layer: adapter-internal diagnostics
(``spica/adapters/*`` -- LLM / TTS / screen) keep their low-level ``log_timing``;
they are not scanned here.

AST-based call scan (like ``test_no_raw_threadpool`` / ``test_no_dict_config``).
"""

import ast
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_DIR = REPO_ROOT / "spica" / "runtime"

# The default observer is the single component that wraps log_timing.
ALLOWLIST = {"spica/runtime/observer.py"}


def _log_timing_calls(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    hits: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = (
            func.id if isinstance(func, ast.Name)
            else func.attr if isinstance(func, ast.Attribute)
            else None
        )
        if name == "log_timing":
            hits.append(f"line {node.lineno}: log_timing(...)")
    return hits


class NoLogTimingGuardTest(unittest.TestCase):
    def test_turn_stage_layer_routes_timing_through_observer(self):
        offenders: dict[str, list[str]] = {}
        for path in sorted(RUNTIME_DIR.rglob("*.py")):
            rel = path.relative_to(REPO_ROOT).as_posix()
            if rel in ALLOWLIST:
                continue
            hits = _log_timing_calls(path)
            if hits:
                offenders[rel] = hits

        self.assertEqual(
            offenders,
            {},
            msg=(
                "N4-observe: the spica/runtime turn/stage layer must route timing "
                "through deps.observer (span/mark/event), not call log_timing. The "
                "DefaultTurnObserver (observer.py) is the one allowlisted wrapper; "
                f"adapter internals are exempt. Offenders: {offenders}"
            ),
        )

    def test_allowlist_points_at_the_default_observer(self):
        for rel in ALLOWLIST:
            self.assertTrue((REPO_ROOT / rel).is_file(), f"Stale allowlist entry: {rel}")


if __name__ == "__main__":
    unittest.main()

"""Phase 7-c2 guard: the runtime knows NOTHING about the v1 LLM surface or
provider families.

After the Phase 7 flip, ``spica/runtime/orchestrator.py`` (7-c1) and
``spica/runtime/tool_round.py`` (7-c2) run entirely on the v2 model port
(``deps.model``: BoundModel -> TextModel/ToolCallingModel). This guard bans the
FULL v1/provider-family surface -- ten names, not just the historically-hit
five -- because the invariant is "the runtime does not know provider family
details", not "the current call sites are gone":

    prefers_chat_completions / has_chat_completions / iter_response_text /
    create_responses / complete_chat / create_chat_with_tools /
    iter_chat_with_tools / complete_text / traits / provider_traits

Scope is EXACTLY the two flipped files. ``spica/runtime/stages.py``
(``call_llm_node`` and the sync-only stages) is the FROZEN MUSEUM -- permanent
v1, never scanned, never migrated (Phase 7 forbidden file; see the migration
plan's Museum entry). ``deps.py`` is the bridge and owns the ``llm`` field for
the museum's sake -- also out of scope by design.

AST-based (docstrings/comments never trip it); liveness tests prove the
detector catches every banned form so the guard cannot rot into a no-op.
"""

import ast
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCAN_FILES = (
    "spica/runtime/orchestrator.py",
    "spica/runtime/tool_round.py",
)
BANNED = {
    "prefers_chat_completions",
    "has_chat_completions",
    "iter_response_text",
    "create_responses",
    "complete_chat",
    "create_chat_with_tools",
    "iter_chat_with_tools",
    "complete_text",
    "traits",
    "provider_traits",
}


def _v1_hits(tree: ast.AST) -> list[str]:
    hits: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in BANNED:
            hits.append(f"line {node.lineno}: .{node.attr}")
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name in BANNED:
                    hits.append(f"line {node.lineno}: from {node.module} import {alias.name}")
    return hits


class NoV1LLMInRuntimeTest(unittest.TestCase):
    def test_flipped_runtime_files_have_no_v1_surface(self):
        offenders: dict[str, list[str]] = {}
        for rel in SCAN_FILES:
            path = REPO_ROOT / rel
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            hits = _v1_hits(tree)
            if hits:
                offenders[rel] = hits

        self.assertEqual(
            offenders,
            {},
            msg=(
                "orchestrator/tool_round must not touch the v1 LLM surface or "
                "provider-family details (Phase 7-c2): run on deps.model "
                f"(BoundModel probe/probe_stream/stream). {offenders}"
            ),
        )

    def test_scan_files_exist(self):
        for rel in SCAN_FILES:
            self.assertTrue((REPO_ROOT / rel).is_file(), f"Stale scan entry: {rel}")

    def test_guard_catches_each_banned_form(self):
        # Liveness (the 6a/Phase-5 precedent): every banned name must trip the
        # detector as an attribute read, plus the import form.
        for name in sorted(BANNED):
            with self.subTest(name=name):
                self.assertTrue(_v1_hits(ast.parse(f"x.{name}(1)")), f"missed .{name}")
        self.assertTrue(
            _v1_hits(ast.parse("from spica.adapters.llm.openai_compatible import complete_text"))
        )

    def test_guard_ignores_the_v2_surface(self):
        for src in (
            "deps.model.stream(p, ctx)",
            "deps.model.probe(p, tools, ctx)",
            "deps.model.probe_stream(p, tools, ctx)",
            "handle.deltas",
            "handle.calls",
            "result.usage",
        ):
            with self.subTest(src=src):
                self.assertEqual(_v1_hits(ast.parse(src)), [], f"false positive: {src}")


if __name__ == "__main__":
    unittest.main()

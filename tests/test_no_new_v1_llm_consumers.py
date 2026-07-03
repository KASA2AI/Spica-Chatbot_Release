"""D3 weak guard (OO migration Phase 6a): no NEW v1 LLM consumers.

After Phase 6a the only two turn-external v1 consumers (GalgameSummarizer /
GalgameReactionJudge) run on BoundModel (spica/ports/model.py), so
``spica/galgame/**`` + ``spica/host/**`` must stay CLEAN of the v1 surface:
importing ``spica.ports.llm`` / naming ``LLMPort`` / calling the v1 method
family. Any new hit is a regression toward the dual-track rot D3 exists to
stop -- new text consumers take a BoundModel; the tool-probe family flips in
Phase 7 (runtime files are NOT scanned here; Phase 7-c2 brings the full
``test_no_v1_llm_in_runtime`` guard for orchestrator/tool_round).

The frozen allowlist is EMPTY by design (the strongest form): Phase 6a left
zero v1 references in the scanned trees. If a legitimate exception ever
appears, allowlist the exact file with a written reason -- never widen the
scan out.

AST-based (like test_no_dict_config / test_no_getenv); docstrings and comments
do not trip it. The liveness tests prove the detector actually catches each
banned form, so the guard can never rot into a silent no-op.
"""

import ast
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCAN_DIRS = ("spica/galgame", "spica/host")
V1_METHODS = {
    # Phase 7-c2 upgrade: the full eight-method v1 family (the traits pair is
    # runtime-guard-specific, see tests/test_no_v1_llm_in_runtime.py).
    "complete_text",
    "prefers_chat_completions",
    "has_chat_completions",
    "iter_response_text",
    "create_responses",
    "complete_chat",
    "create_chat_with_tools",
    "iter_chat_with_tools",
}
# Frozen allowlist: EMPTY (see module docstring). Entries are repo-relative
# file paths and require a written reason next to them.
ALLOWLIST: set[str] = set()


def _v1_hits(tree: ast.AST) -> list[str]:
    hits: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "spica.ports.llm":
            hits.append(f"line {node.lineno}: from spica.ports.llm import ...")
        elif isinstance(node, ast.ImportFrom) and node.module == "spica.ports":
            # Package-level escape hatches (BUG-5, mirrored from the runtime
            # guard): spica/ports/__init__.py re-exports LLMPort and exposes
            # the llm submodule -- pulling either through the parent package is
            # the same v1 carrier. PRECISE names only: ``from spica.ports
            # import model`` (v2) and every other legal port must never trip.
            for alias in node.names:
                if alias.name in {"LLMPort", "llm"}:
                    hits.append(f"line {node.lineno}: from spica.ports import {alias.name}")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "spica.ports.llm":
                    hits.append(f"line {node.lineno}: import spica.ports.llm")
        elif isinstance(node, ast.Name) and node.id == "LLMPort":
            hits.append(f"line {node.lineno}: LLMPort")
        elif isinstance(node, ast.Attribute) and node.attr == "LLMPort":
            # Package-alias pull (``ports.LLMPort``): importing the package is
            # legal; touching .LLMPort trips (the name is unique to the v1
            # Protocol, any receiver).
            hits.append(f"line {node.lineno}: .LLMPort")
        elif isinstance(node, ast.Attribute) and node.attr in V1_METHODS:
            hits.append(f"line {node.lineno}: .{node.attr}")
    return hits


class NoNewV1LLMConsumersTest(unittest.TestCase):
    def test_galgame_and_host_have_no_v1_llm_surface(self):
        offenders: dict[str, list[str]] = {}
        for scan_dir in SCAN_DIRS:
            for path in sorted((REPO_ROOT / scan_dir).rglob("*.py")):
                rel = path.relative_to(REPO_ROOT).as_posix()
                if rel in ALLOWLIST:
                    continue
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
                hits = _v1_hits(tree)
                if hits:
                    offenders[rel] = hits

        self.assertEqual(
            offenders,
            {},
            msg=(
                "New v1 LLM consumer in spica/galgame or spica/host (D3 weak "
                "guard, Phase 6a): text consumers take a BoundModel "
                f"(spica/ports/model.py), never the v1 surface. {offenders}"
            ),
        )

    def test_allowlist_points_at_real_files(self):
        for rel in ALLOWLIST:
            self.assertTrue((REPO_ROOT / rel).is_file(), f"Stale allowlist entry: {rel}")

    def test_guard_catches_each_banned_form(self):
        # Liveness (the Phase 5 exemption-liveness precedent): the detector must
        # flag every banned form -- a guard that cannot catch a synthetic new
        # consumer is a silent no-op, not a guard.
        for src in (
            "x.complete_text(p, model=m)",
            "y.prefers_chat_completions()",
            "y.has_chat_completions()",
            "z.iter_response_text(req, ctx)",
            "z.create_responses(model=m, input=p)",
            "z.complete_chat(m, p, s)",
            "a.create_chat_with_tools(model=m, prompt=p, tools=t, state=s)",
            "b.iter_chat_with_tools(model=m)",
            "from spica.ports.llm import LLMPort",
            "import spica.ports.llm",
            "def f(llm: LLMPort): pass",
            # BUG-5 package-level smuggle forms (ports/__init__ re-exports):
            "from spica.ports import LLMPort",
            "from spica.ports import llm",
            "from spica.ports import llm\nx = llm.LLMPort",
            "import spica.ports as ports\nx = ports.LLMPort",
            "from spica import ports\nx = ports.LLMPort",
        ):
            with self.subTest(src=src):
                self.assertTrue(_v1_hits(ast.parse(src)), f"guard missed: {src}")

    def test_guard_ignores_the_v2_surface(self):
        for src in (
            "bound.complete(p)",
            "bound.stream(p, state)",
            "from spica.ports.model import BoundModel",
            # BUG-5 negatives: legal ports stay importable/reachable via the
            # package -- only the v1 carrier names trip.
            "from spica.ports import model",
            "from spica.ports import game_memory",
            "import spica.ports as ports\nx = ports.model",
            "from spica import ports\nx = ports.game_memory",
        ):
            with self.subTest(src=src):
                self.assertEqual(_v1_hits(ast.parse(src)), [], f"false positive: {src}")

    def test_scan_roots_exist_and_are_nonempty(self):
        # BUG-6 liveness: a moved/renamed scan root would make rglob("*.py")
        # an empty iterator and the main scan vacuously green. Mirror of the
        # runtime guard's test_scan_files_exist.
        for scan_dir in SCAN_DIRS:
            root = REPO_ROOT / scan_dir
            self.assertTrue(root.is_dir(), f"scan root missing: {scan_dir}")
            self.assertTrue(
                any(root.rglob("*.py")),
                f"scan root has no .py files (vacuous scan): {scan_dir}",
            )


if __name__ == "__main__":
    unittest.main()

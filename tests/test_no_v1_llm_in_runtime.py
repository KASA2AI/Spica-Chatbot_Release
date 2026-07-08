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

...AND the v1 CARRIERS themselves (review hardening): ``deps.llm`` attribute
reads (precise: only when the receiver is the bare name ``deps`` --
``deps.config.llm.model`` is a legal typed-config read and must never trip),
``LLMPort`` name references, and any ``spica.ports.llm`` import. Banning only
method names would let a future edit smuggle ``deps.llm`` back in and call it
through an alias.

Scope (review NEW-2 widening): EVERY ``spica/runtime/**/*.py`` file, minus the
two files that legitimately carry v1 today -- ``stages.py`` (the FROZEN MUSEUM:
``call_llm_node`` and the sync-only stages, permanent v1, never migrated) and
``deps.py`` (the bridge; owns the ``llm`` field for the museum's sake). The
original two-file scope let a NEW runtime module carry v1 calls consumed
indirectly by orchestrator/tool_round without any guard firing -- narrower
than the invariant this docstring states. Exemptions are LIVE-CHECKED: each
exempt file must currently contain v1 hits, so an exemption cannot outlive its
reason. ``sync_chain.py`` is frozen-museum adjacent but has ZERO v1 hits today,
so it needs no exemption and stays scanned -- if it ever starts hitting, that
is a stop-and-adjudicate signal, not a whitelist edit.

AST-based (docstrings/comments never trip it); liveness tests prove the
detector catches every banned form so the guard cannot rot into a no-op.
"""

import ast
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
# Exempt = legitimately v1-carrying today (museum + bridge); liveness-checked.
EXEMPT_FILES = frozenset({"stages.py", "deps.py"})
SCAN_FILES = tuple(
    sorted(
        p.relative_to(REPO_ROOT).as_posix()
        for p in (REPO_ROOT / "spica" / "runtime").rglob("*.py")
        if p.name not in EXEMPT_FILES
    )
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
        elif (
            # v1 carrier: a ``deps.llm`` read -- PRECISE (receiver must be the
            # bare name ``deps``), so ``deps.config.llm.model`` never trips.
            isinstance(node, ast.Attribute)
            and node.attr == "llm"
            and isinstance(node.value, ast.Name)
            and node.value.id == "deps"
        ):
            hits.append(f"line {node.lineno}: deps.llm")
        elif isinstance(node, ast.Name) and node.id == "LLMPort":
            hits.append(f"line {node.lineno}: LLMPort")
        elif isinstance(node, ast.Attribute) and node.attr == "LLMPort":
            # Package-alias escape hatch (review hardening #3): ``ports.LLMPort``
            # after ``import spica.ports as ports`` / ``from spica import ports``.
            # Importing the package itself stays legal; only touching .LLMPort
            # trips (any receiver -- the name is unique to the v1 Protocol).
            hits.append(f"line {node.lineno}: .LLMPort")
        elif isinstance(node, ast.ImportFrom):
            if node.module == "spica.ports.llm":
                hits.append(f"line {node.lineno}: from spica.ports.llm import ...")
            elif node.module == "spica.ports":
                # Package-level escape hatches (review hardening #2): the v1
                # module or the Protocol pulled in through the parent package.
                # PRECISE names only -- ``from spica.ports import model`` (v2)
                # and future legal ports must never trip.
                for alias in node.names:
                    if alias.name in {"LLMPort", "llm"}:
                        hits.append(f"line {node.lineno}: from spica.ports import {alias.name}")
            else:
                for alias in node.names:
                    if alias.name in BANNED:
                        hits.append(f"line {node.lineno}: from {node.module} import {alias.name}")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "spica.ports.llm":
                    hits.append(f"line {node.lineno}: import spica.ports.llm")
    return hits


class NoV1LLMInRuntimeTest(unittest.TestCase):
    def test_runtime_files_have_no_v1_surface(self):
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
                "spica/runtime (minus the live-checked museum/bridge exemptions) "
                "must not touch the v1 LLM surface or provider-family details "
                "(Phase 7-c2 + review NEW-2): run on deps.model "
                f"(BoundModel probe/probe_stream/stream). {offenders}"
            ),
        )

    def test_scan_files_exist(self):
        for rel in SCAN_FILES:
            self.assertTrue((REPO_ROOT / rel).is_file(), f"Stale scan entry: {rel}")

    def test_scan_set_covers_the_flipped_files(self):
        # BUG-6 discipline: a dynamic scan set must be proven non-degenerate --
        # the two historically flipped files must be inside it.
        self.assertIn("spica/runtime/orchestrator.py", SCAN_FILES)
        self.assertIn("spica/runtime/tool_round.py", SCAN_FILES)
        self.assertGreater(len(SCAN_FILES), 2)  # ...and it widened past them

    def test_exemptions_are_alive(self):
        # An exemption may only exist while its file REALLY carries v1 -- the
        # day deps.py/stages.py go v1-free, this fails and the exemption must
        # be removed (guard grows, never rots).
        for name in sorted(EXEMPT_FILES):
            with self.subTest(file=name):
                path = REPO_ROOT / "spica" / "runtime" / name
                self.assertTrue(path.is_file(), f"Stale exemption: {name}")
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
                self.assertTrue(
                    _v1_hits(tree),
                    f"{name} no longer carries v1 -- remove it from EXEMPT_FILES",
                )

    def test_guard_catches_each_banned_form(self):
        # Liveness (the 6a/Phase-5 precedent): every banned name must trip the
        # detector as an attribute read, plus the import form.
        for name in sorted(BANNED):
            with self.subTest(name=name):
                self.assertTrue(_v1_hits(ast.parse(f"x.{name}(1)")), f"missed .{name}")
        self.assertTrue(
            _v1_hits(ast.parse("from spica.adapters.llm.openai_compatible import complete_text"))
        )

    def test_guard_catches_each_v1_carrier_form(self):
        # Review hardening: the four carrier forms must trip the detector.
        for src in (
            "deps.llm",                                  # bare read
            "deps.llm.anything(r, c)",                   # aliased call through the carrier
            "def f(x: LLMPort): pass",                   # name reference (annotation)
            "port = LLMPort",                            # name reference (value)
            "import spica.ports.llm",                    # module import
            "from spica.ports.llm import LLMPort",       # from-import
            "from spica.ports.llm import anything_else", # from-import, any name
            "from spica.ports import LLMPort",           # package-level Protocol pull
            "from spica.ports import llm",               # package-level module pull
            "from spica.ports import llm\nx = llm.LLMPort",  # chained: the import itself trips
            "import spica.ports as ports\nx = ports.LLMPort",  # package-alias attribute pull
            "from spica import ports\nx = ports.LLMPort",      # parent-package alias pull
        ):
            with self.subTest(src=src):
                self.assertTrue(_v1_hits(ast.parse(src)), f"guard missed: {src}")

    def test_guard_ignores_the_v2_surface(self):
        for src in (
            "deps.model.stream(p, ctx)",
            "deps.model.probe(p, tools, ctx)",
            "deps.model.probe_stream(p, tools, ctx)",
            "deps.config.llm.model",   # legal typed-config read -- must NOT trip
            "model = deps.config.llm.model",
            "other.llm",               # only the ``deps`` carrier is banned
            "from spica.ports import model",       # legal v2 port import
            "from spica.ports import game_memory", # any other legal port
            "ports.model",                          # package-alias access to v2 stays legal
            "ports.game_memory",                    # ...and to any other port
            "handle.deltas",
            "handle.calls",
            "result.usage",
        ):
            with self.subTest(src=src):
                self.assertEqual(_v1_hits(ast.parse(src)), [], f"false positive: {src}")


if __name__ == "__main__":
    unittest.main()

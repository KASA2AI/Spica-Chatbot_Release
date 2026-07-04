"""Guard: ``game_context_request`` is galgame's PERMANENT dedicated slot
(OO migration Phase 8 design ruling 2; delivered by the post-review knife
NEW-1 -- the Phase 8 plan promised this guard, this file is that promise).

The rule (CLAUDE.md §2 / GUARDRAILS decision tree): non-galgame domains ride
``DomainTurnBinding`` / ``domain_context_requests``; NOTHING outside the
whitelisted galgame lane may read or fill the legacy slot. The scan is
AST-based over all of ``spica/`` and catches THREE access forms (review
correction: the galgame contributor itself reads via ``getattr``, so an
Attribute-only scan would miss the most important reader AND the most likely
future-offender form):

- ``x.game_context_request``                      (Attribute, read or write)
- ``getattr(x, "game_context_request", ...)``     (dynamic read)
- ``f(game_context_request=...)``                 (keyword fill/construction)

Docstrings, comments and dataclass field DEFINITIONS (``AnnAssign`` with a
bare name target, e.g. ``context.py``'s TurnRequest/GameTurnBinding fields)
produce none of these nodes -- mentions never trip the guard (pinned below).

The whitelist is FORM-LEVEL and asserted with EXACT equality in both
directions: a whitelisted file that stops hitting (or changes form) fails the
guard too, so entries cannot rot.
"""

import ast
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# file (repo-relative) -> the exact access forms that file is allowed to use.
ALLOWED_FORMS: dict[str, frozenset[str]] = {
    # GameTurnBinding legacy lane: reads binding.game_context_request (attr)
    # and fills TurnRequest(game_context_request=...) (kwarg).
    "spica/core/chat_engine.py": frozenset({"attr", "kwarg"}),
    # Constructs GameTurnBinding(game_context_request=...) at publish-LAST.
    # Construction ONLY -- it must never read the slot back off a request.
    "spica/galgame/companion_controller.py": frozenset({"kwarg"}),
    # THE galgame gate -- the one and only request-slot reader (getattr form).
    "spica/galgame/context_contributor.py": frozenset({"getattr"}),
    # galgame-only closures (reaction scope / note write-back) read the slot
    # off the CONTROLLER-published binding (never off router.current()).
    "spica/host/app_host.py": frozenset({"attr"}),
}


def _slot_hits(tree: ast.AST) -> set[str]:
    """The set of access forms a module uses for the galgame slot."""
    forms: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr == "game_context_request":
            forms.add("attr")
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "getattr"
            and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
            and node.args[1].value == "game_context_request"
        ):
            forms.add("getattr")
        elif isinstance(node, ast.keyword) and node.arg == "game_context_request":
            forms.add("kwarg")
    return forms


def _scan_spica() -> dict[str, set[str]]:
    hits: dict[str, set[str]] = {}
    for path in sorted((REPO_ROOT / "spica").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        forms = _slot_hits(tree)
        if forms:
            hits[path.relative_to(REPO_ROOT).as_posix()] = forms
    return hits


class SlotIsolationTest(unittest.TestCase):
    def test_slot_access_is_exactly_the_whitelist(self):
        # EXACT equality, both directions: a new non-galgame toucher fails, and
        # a whitelist entry that stops hitting (rot) fails too.
        self.assertEqual(
            _scan_spica(),
            {k: set(v) for k, v in ALLOWED_FORMS.items()},
            msg=(
                "game_context_request is galgame's PERMANENT slot (Phase 8 裁决 2): "
                "non-galgame context rides DomainTurnBinding/domain_context_requests. "
                "New toucher -> use the generic lane; vanished whitelist hit -> "
                "prune ALLOWED_FORMS."
            ),
        )


class DetectorLivenessTest(unittest.TestCase):
    def test_catches_attribute_read(self):
        self.assertEqual(_slot_hits(ast.parse("x = request.game_context_request")), {"attr"})

    def test_catches_getattr_read(self):
        self.assertEqual(
            _slot_hits(ast.parse('gcr = getattr(request, "game_context_request", None)')),
            {"getattr"},
        )

    def test_catches_keyword_fill(self):
        self.assertEqual(
            _slot_hits(ast.parse("req = TurnRequest(game_context_request=gcr)")), {"kwarg"}
        )

    def test_docstring_and_comment_mentions_never_trip(self):
        src = (
            '"""Docs may say game_context_request freely."""\n'
            "# comment: game_context_request is galgame-only\n"
            'label = "game_context_request"  # a bare string is not an access\n'
            "game_context_request: int = 0  # field DEFINITION (AnnAssign name)\n"
        )
        self.assertEqual(_slot_hits(ast.parse(src)), set())

    def test_definition_only_files_really_have_zero_hits(self):
        # context.py / prompt_context.py mention the slot in docstrings and
        # define the dataclass fields -- the scan must see NOTHING there
        # (that's the no-false-positive pin on real code, not synthetic).
        hits = _scan_spica()
        self.assertNotIn("spica/runtime/context.py", hits)
        self.assertNotIn("spica/runtime/prompt_context.py", hits)


if __name__ == "__main__":
    unittest.main()

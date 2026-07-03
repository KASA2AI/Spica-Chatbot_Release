"""Phase 3 guards: PromptContextContributor seam + D2 anti-regrowth pins.

Covers (migration plan Phase 3 "characterization tests to add" + review P2s):
- auto-fill semantics: TurnDeps.context_contributors None -> EXACTLY the galgame
  contributor, one entry (the compatibility shim must never grow a second);
- explicit () = injection off (byte-level no-op even on an active turn);
- an explicitly registered tuple is respected verbatim;
- span/timing name stays ``retrieve_game_context_node`` (single-contributor era);
- D2 AST guards: the alias must be a PURE ASSIGNMENT (never re-defed) and
  ``contribute_context_node`` has a source-line cap -- new domain logic must go
  into contributors, not back into the node;
- failure containment: a contributor whose ``mode()`` raises is treated as
  "none" (no span, no prompt change, no crash -- a broken gate must not break
  plain chat); a failing ``sections()`` keeps the span but injects nothing;
- import boundary AST guard for spica/galgame/context_contributor.py
  (no spica.runtime.stages / spica.galgame.session / spica.core.events).
"""

import ast
import unittest
from pathlib import Path

from spica.config.schema import AppConfig, CharacterConfig
from spica.galgame.context_contributor import GalgameContextContributor, galgame_contributor
from spica.runtime.context import PromptBundle, TurnContext, TurnError, TurnRequest
from spica.runtime.deps import TurnDeps
from spica.runtime.observer import DefaultTurnObserver
from spica.runtime.prompt_context import PromptContextContributor
from spica.runtime.stages import contribute_context_node, retrieve_game_context_node
from spica.runtime.tools import RegistryToolSet

SPICA_ROOT = Path(__file__).resolve().parents[1] / "spica"
BASE_PROMPT = "[CURRENT_USER_INPUT]\n刚才发生了什么"
_UNSET = object()

# D2: cap the generic node's size (current implementation is 54 lines incl.
# docstring). New domain logic landing INSIDE the node necessarily blows past
# the margin -- it belongs in a contributor.
NODE_LINE_CAP = 65


def _active_request() -> TurnRequest:
    return TurnRequest(user_input="刚才发生了什么", conversation_id="default", interaction_mode="galgame")


def _ctx(request: TurnRequest) -> TurnContext:
    ctx = TurnContext(request)
    ctx.prompt = PromptBundle(prompt_input=BASE_PROMPT)
    return ctx


def _deps(ctx: TurnContext, contributors=_UNSET) -> TurnDeps:
    kwargs = {}
    if contributors is not _UNSET:
        kwargs["context_contributors"] = contributors
    return TurnDeps(
        config=AppConfig(character=CharacterConfig(character_id="spica", interlocutor_name="麦")),
        llm=None,
        tts=None,
        visual=None,
        memory=None,
        tools=RegistryToolSet.from_function_table([], {}),
        observer=DefaultTurnObserver(ctx.timing),
        **kwargs,
    )


class _StaticContributor:
    name = "fake"
    priority = 5

    def __init__(self, mode="active", sections=("[FAKE_SECTION]\nbody",)):
        self._mode = mode
        self._sections = list(sections)

    def mode(self, request):
        return self._mode

    def sections(self, ctx, deps, mode):
        return list(self._sections)


class _BadModeContributor:
    name = "bad-mode"
    priority = 0

    def mode(self, request):
        raise RuntimeError("gate boom")

    def sections(self, ctx, deps, mode):
        raise AssertionError("sections must not run after mode() failed")


class _BadSectionsContributor:
    name = "bad-sections"
    priority = 0

    def mode(self, request):
        return "active"

    def sections(self, ctx, deps, mode):
        raise RuntimeError("sections boom")


class RegistrationSemanticsTest(unittest.TestCase):
    def test_none_autofills_exactly_the_galgame_contributor(self):
        ctx = _ctx(_active_request())
        deps = _deps(ctx)  # context_contributors left as None
        self.assertEqual(deps.context_contributors, (galgame_contributor,))
        self.assertIs(deps.context_contributors[0], galgame_contributor)
        self.assertEqual(len(deps.context_contributors), 1)  # the shim NEVER grows

    def test_explicit_empty_tuple_disables_injection_byte_level(self):
        ctx = _ctx(_active_request())
        deps = _deps(ctx, contributors=())
        self.assertEqual(deps.context_contributors, ())
        before_timing = dict(ctx.timing)
        contribute_context_node(ctx, None, deps)
        self.assertEqual(ctx.prompt.prompt_input, BASE_PROMPT)  # prompt untouched
        self.assertEqual(ctx.timing, before_timing)  # NO span opened
        self.assertNotIn("retrieve_game_context_node_ms", ctx.timing)

    def test_explicit_tuple_is_respected_verbatim(self):
        ctx = _ctx(_active_request())
        fake = _StaticContributor()
        registered = (fake,)
        deps = _deps(ctx, contributors=registered)
        self.assertIs(deps.context_contributors, registered)  # no auto-fill mixing
        contribute_context_node(ctx, None, deps)
        self.assertIn("[FAKE_SECTION]", ctx.prompt.prompt_input)

    def test_galgame_contributor_satisfies_the_protocol(self):
        self.assertIsInstance(galgame_contributor, PromptContextContributor)
        self.assertIsInstance(galgame_contributor, GalgameContextContributor)


class SpanNamePinTest(unittest.TestCase):
    def test_active_turn_opens_span_under_the_historical_name(self):
        # Auto-filled galgame contributor, no game_memory: still opens the span
        # under the OLD name and leaves the prompt alone (golden #2(d) semantics).
        ctx = _ctx(_active_request())
        contribute_context_node(ctx, None, _deps(ctx))
        self.assertIn("retrieve_game_context_node_ms", ctx.timing)
        self.assertEqual(ctx.prompt.prompt_input, BASE_PROMPT)

    def test_alias_is_the_same_object(self):
        self.assertIs(retrieve_game_context_node, contribute_context_node)


class FailureContainmentTest(unittest.TestCase):
    def test_mode_exception_is_treated_as_none_no_span_no_prompt_change(self):
        ctx = _ctx(_active_request())
        deps = _deps(ctx, contributors=(_BadModeContributor(),))
        before_timing = dict(ctx.timing)
        with self.assertLogs("spica.runtime.stages", level="WARNING"):
            out = contribute_context_node(ctx, None, deps)  # must not raise
        self.assertIs(out, ctx)
        self.assertEqual(ctx.prompt.prompt_input, BASE_PROMPT)
        self.assertEqual(ctx.timing, before_timing)  # no span for an all-failed gate
        self.assertNotIn("retrieve_game_context_node_ms", ctx.timing)

    def test_bad_mode_contributor_does_not_drag_down_a_healthy_one(self):
        ctx = _ctx(_active_request())
        deps = _deps(ctx, contributors=(_BadModeContributor(), _StaticContributor()))
        with self.assertLogs("spica.runtime.stages", level="WARNING"):
            contribute_context_node(ctx, None, deps)  # must not raise
        self.assertIn("[FAKE_SECTION]", ctx.prompt.prompt_input)  # healthy one injected

    def test_sections_exception_keeps_span_and_prompt_and_turn(self):
        ctx = _ctx(_active_request())
        deps = _deps(ctx, contributors=(_BadSectionsContributor(),))
        with self.assertLogs("spica.runtime.stages", level="WARNING"):
            contribute_context_node(ctx, None, deps)  # must not raise
        self.assertIn("retrieve_game_context_node_ms", ctx.timing)  # span was open
        self.assertEqual(ctx.prompt.prompt_input, BASE_PROMPT)  # nothing injected


class PriorErrorCompatTest(unittest.TestCase):
    def test_prior_error_is_noop_without_touching_services_or_deps(self):
        # Alias compatibility (review P1): the pre-Phase-3 node returned an
        # errored turn untouched BEFORE ever bridging deps -- so the call shape
        # (ctx, None, None) must stay a silent no-op, not an AttributeError.
        ctx = _ctx(_active_request())  # active/galgame request + prompt set
        ctx.error = TurnError("BOOM", "prior failure")
        out = contribute_context_node(ctx, None, None)  # must not raise
        self.assertIs(out, ctx)
        self.assertEqual(ctx.prompt.prompt_input, BASE_PROMPT)  # prompt untouched
        self.assertNotIn("retrieve_game_context_node_ms", ctx.timing)  # no span


class D2AstGuardTest(unittest.TestCase):
    """AST pins over source files -- the anti-regrowth half of D2."""

    def _stages_tree(self) -> ast.Module:
        path = SPICA_ROOT / "runtime" / "stages.py"
        return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

    def test_alias_is_a_pure_assignment_never_redefed(self):
        tree = self._stages_tree()
        assigns = [
            node
            for node in tree.body
            if isinstance(node, ast.Assign)
            and any(
                isinstance(t, ast.Name) and t.id == "retrieve_game_context_node"
                for t in node.targets
            )
        ]
        self.assertEqual(len(assigns), 1, "alias must exist exactly once at module level")
        value = assigns[0].value
        self.assertIsInstance(value, ast.Name)
        self.assertEqual(value.id, "contribute_context_node")
        redefs = [
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "retrieve_game_context_node"
        ]
        self.assertEqual(redefs, [], "alias must never be re-defed as a function")

    def test_node_source_line_cap(self):
        tree = self._stages_tree()
        node = next(
            n
            for n in tree.body
            if isinstance(n, ast.FunctionDef) and n.name == "contribute_context_node"
        )
        lines = node.end_lineno - node.lineno + 1
        self.assertLessEqual(
            lines,
            NODE_LINE_CAP,
            "contribute_context_node grew past the D2 cap -- new domain logic "
            "belongs in a PromptContextContributor, not in the node",
        )

    def test_context_contributor_import_boundary(self):
        # Review P2: layering pins Qt/core.events over the transform layer; this
        # additionally forbids the two edges layering does not watch --
        # spica.runtime.stages (cycle) and spica.galgame.session (state owner).
        path = SPICA_ROOT / "galgame" / "context_contributor.py"
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        banned = ("spica.runtime.stages", "spica.galgame.session", "spica.core.events")
        violations = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                violations += [a.name for a in node.names if a.name.startswith(banned)]
            elif isinstance(node, ast.ImportFrom) and node.module:
                if node.module.startswith(banned):
                    violations.append(node.module)
        self.assertEqual(violations, [])


if __name__ == "__main__":
    unittest.main()

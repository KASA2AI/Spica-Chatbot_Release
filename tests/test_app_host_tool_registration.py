"""Phase 0 characterization: AppHost tool-registration metadata (OO migration).

Pins the PUBLIC registry surface the host wires its built-in tools onto, so the
Phase 4R ToolEntry refactor (internal 7-tuple -> NamedTuple) has a zero-change
regression gate. HARD RULE (migration plan Phase 0 #1): only the public registry
accessors are used -- ``list_adapters`` / ``tool_schemas`` / ``tool_intent_gated``
/ ``tool_effect`` / ``tool_compact_output`` / ``tool_handler``. No ``_tools`` or
any underscore attribute access.

The effect-tier assertions deliberately REPEAT tests/test_sing_song_tool.py's
per-tool coverage: this file is the centralized endorsement of the whole built-in
set (the migration plan's "补缺口 + 集中背书"). The genuinely new gaps closed
here are tool_intent_gated("sing_song") and tool_compact_output("inspect_screen").

Post-initialize / companion-state supply behaviour is NOT tested here --
tests/test_watch_game_screen.py / test_note_game_observation.py already cover it.
"""

import unittest

from spica.host.app_host import AppHost


def _schema_name(schema: dict) -> str:
    """Tool name from a flat schema (top-level ``name``) or an OpenAI-nested one
    (``{"type": "function", "function": {"name": ...}}``)."""
    name = schema.get("name")
    if isinstance(name, str) and name:
        return name
    function = schema.get("function")
    if isinstance(function, dict):
        nested = function.get("name")
        if isinstance(nested, str) and nested:
            return nested
    return ""


class AppHostToolRegistrationTest(unittest.TestCase):
    def setUp(self):
        # Registration happens in __init__ (host closures); initialize() is NOT
        # called -- this pins the pre-initialize supply surface.
        #
        # Per-test (NOT setUpClass) on purpose: AppHost() triggers a real config
        # load whose dotenv side writes the developer's local xiaosan.env into
        # os.environ; conftest's autouse _restore_os_environ only undoes leaks
        # that happen INSIDE a test's function scope -- setUpClass would run
        # before the first snapshot and bake the leak into the whole session.
        self.host = AppHost()
        self.registry = self.host.registry

    def _offered_names(self) -> set[str]:
        return {_schema_name(schema) for schema in self.registry.tool_schemas()}

    def test_watch_and_note_registered_but_not_offered_before_initialize(self):
        registered = self.registry.list_adapters("tool")
        self.assertIn("watch_game_screen", registered)
        self.assertIn("note_game_observation", registered)
        # Their ``available`` predicates are False before initialize / outside
        # companion play -- expressed purely through the public supply surface.
        offered = self._offered_names()
        self.assertNotIn("watch_game_screen", offered)
        self.assertNotIn("note_game_observation", offered)

    def test_inspect_and_sing_offered_without_state_gate(self):
        # Membership assertions only (not set equality): plugins/future tools may
        # add names without invalidating this pin.
        offered = self._offered_names()
        self.assertIn("inspect_screen", offered)
        self.assertIn("sing_song", offered)

    def test_intent_gating(self):
        # watch/note are supplied by STATE, not by the router wordlist.
        self.assertIs(self.registry.tool_intent_gated("watch_game_screen"), False)
        self.assertIs(self.registry.tool_intent_gated("note_game_observation"), False)
        # sing_song keeps the wordlist pre-filter (supply gating, never hijack).
        self.assertIs(self.registry.tool_intent_gated("sing_song"), True)

    def test_effect_tiers(self):
        # Intentional overlap with test_sing_song_tool.py:246-250 -- centralized
        # endorsement of the whole built-in footprint classification.
        self.assertEqual(self.registry.tool_effect("watch_game_screen"), "read")
        self.assertEqual(self.registry.tool_effect("note_game_observation"), "write")
        self.assertEqual(self.registry.tool_effect("sing_song"), "act")
        self.assertEqual(self.registry.tool_effect("inspect_screen"), "read")

    def test_compact_output_and_handler(self):
        # inspect_screen registers its historical followup-prompt compactor.
        self.assertIsNotNone(self.registry.tool_compact_output("inspect_screen"))
        # The handler is registered even while the tool is state-hidden.
        self.assertIsNotNone(self.registry.tool_handler("watch_game_screen"))


if __name__ == "__main__":
    unittest.main()

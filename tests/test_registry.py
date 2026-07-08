"""Unit tests for CapabilityRegistry tool registration.

register_tool must accept both a flat schema (top-level ``name``) and an OpenAI-style
nested one (``{"type": "function", "function": {"name": ...}}``), storing the schema
VERBATIM and keying the handler by the resolved name.
"""

import unittest

from spica.plugins.registry import CapabilityRegistry, ToolEntry


def _handler(**kwargs):
    return {}


class RegisterToolSchemaTest(unittest.TestCase):
    def test_flat_schema_registers_by_top_level_name(self):
        registry = CapabilityRegistry()
        schema = {"type": "function", "name": "flat_tool", "parameters": {}}
        registry.register_tool(schema, _handler)
        self.assertEqual(registry.list_adapters("tool"), ["flat_tool"])
        self.assertIs(registry.tool_handler("flat_tool"), _handler)
        self.assertEqual(registry.tool_schemas(), [schema])

    def test_openai_nested_schema_registers_by_function_name(self):
        registry = CapabilityRegistry()
        schema = {"type": "function", "function": {"name": "nested_tool", "parameters": {}}}
        registry.register_tool(schema, _handler)
        self.assertEqual(registry.list_adapters("tool"), ["nested_tool"])
        self.assertIs(registry.tool_handler("nested_tool"), _handler)
        # schema is stored VERBATIM -- the nested form is NOT flattened.
        self.assertEqual(registry.tool_schemas(), [schema])
        self.assertEqual(registry.tool_schemas()[0]["function"]["name"], "nested_tool")

    def test_schema_without_a_name_anywhere_raises(self):
        registry = CapabilityRegistry()
        with self.assertRaises(ValueError):
            registry.register_tool({"type": "function", "parameters": {}}, _handler)


class ToolEntryShapeTest(unittest.TestCase):
    """Phase 4R: pin the named-field record. New tool metadata must land as a
    NAMED field here (and update this pin) -- never as anonymous-tuple widening."""

    def test_tool_entry_fields(self):
        self.assertEqual(
            ToolEntry._fields,
            ("schema", "handler", "available", "intent_gated", "chainable",
             "compact_output", "effect"),
        )


class ToolMetadataReadersTest(unittest.TestCase):
    def test_explicit_metadata_reads_back(self):
        registry = CapabilityRegistry()
        compactor = lambda text: text[:8]  # noqa: E731
        registry.register_tool(
            {"name": "meta_tool", "parameters": {}},
            _handler,
            intent_gated=False,
            chainable=True,
            compact_output=compactor,
            effect="act",
        )
        self.assertIs(registry.tool_intent_gated("meta_tool"), False)
        self.assertIs(registry.tool_chainable("meta_tool"), True)
        self.assertIs(registry.tool_compact_output("meta_tool"), compactor)
        self.assertEqual(registry.tool_effect("meta_tool"), "act")

    def test_unregistered_name_defaults(self):
        registry = CapabilityRegistry()
        self.assertIsNone(registry.tool_handler("ghost"))
        self.assertIs(registry.tool_intent_gated("ghost"), True)
        self.assertIs(registry.tool_chainable("ghost"), False)
        self.assertIsNone(registry.tool_compact_output("ghost"))
        self.assertEqual(registry.tool_effect("ghost"), "read")

    def test_invalid_effect_raises(self):
        registry = CapabilityRegistry()
        with self.assertRaises(ValueError):
            registry.register_tool({"name": "bad_effect"}, _handler, effect="bogus")


class AvailablePredicateSupplyTest(unittest.TestCase):
    def test_false_predicate_hides_from_supply_but_not_from_listing(self):
        registry = CapabilityRegistry()
        offered = {"value": False}
        registry.register_tool(
            {"name": "gated_tool", "parameters": {}}, _handler,
            available=lambda: offered["value"],
        )
        self.assertEqual(registry.list_adapters("tool"), ["gated_tool"])  # registered...
        self.assertEqual(registry.tool_schemas(), [])  # ...but not offered
        offered["value"] = True
        self.assertEqual(len(registry.tool_schemas()), 1)  # state flip -> offered

    def test_raising_predicate_hides_the_tool_without_breaking_supply(self):
        registry = CapabilityRegistry()

        def _boom() -> bool:
            raise RuntimeError("predicate boom")

        registry.register_tool({"name": "broken_gate", "parameters": {}}, _handler, available=_boom)
        registry.register_tool({"name": "healthy", "parameters": {}}, _handler)
        names = [s.get("name") for s in registry.tool_schemas()]  # must not raise
        self.assertEqual(names, ["healthy"])


class ReRegisterOverwriteTest(unittest.TestCase):
    def test_same_name_registration_replaces_handler_and_schema(self):
        registry = CapabilityRegistry()
        old_schema = {"name": "dup_tool", "parameters": {}, "description": "old"}
        new_schema = {"name": "dup_tool", "parameters": {}, "description": "new"}

        def _new_handler(**kwargs):
            return {}

        registry.register_tool(old_schema, _handler)
        registry.register_tool(new_schema, _new_handler, effect="write")
        self.assertEqual(registry.list_adapters("tool"), ["dup_tool"])  # still one entry
        self.assertIs(registry.tool_handler("dup_tool"), _new_handler)
        self.assertEqual(registry.tool_schemas(), [new_schema])
        self.assertEqual(registry.tool_effect("dup_tool"), "write")


if __name__ == "__main__":
    unittest.main()

"""Unit tests for CapabilityRegistry tool registration.

register_tool must accept both a flat schema (top-level ``name``) and an OpenAI-style
nested one (``{"type": "function", "function": {"name": ...}}``), storing the schema
VERBATIM and keying the handler by the resolved name.
"""

import unittest

from spica.plugins.registry import CapabilityRegistry


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


if __name__ == "__main__":
    unittest.main()

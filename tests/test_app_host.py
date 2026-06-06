"""Phase 1 smoke test for the AppHost composition root.

Verifies the new package root imports, the management-surface placeholder, and
that ``conversation_surface`` aliases the held agent. ``initialize()`` is NOT
called here -- it constructs the real SimpleAgent/TTS/Visual stack which needs
runtime config and credentials; full wiring is covered by the manual launch and
the Phase 0 golden suite.
"""

import unittest

from spica.host.app_host import AppHost


class AppHostSmokeTest(unittest.TestCase):
    def test_package_root_imports_with_empty_services(self):
        host = AppHost()
        self.assertIsNone(host.chat_engine)
        self.assertIsNone(host.services)
        self.assertIsNone(host.visual_tool)
        self.assertIsNone(host.tts_adapter)
        self.assertIsNone(host.tts_tool)

    def test_conversation_surface_is_chat_engine(self):
        host = AppHost()
        self.assertIsNone(host.conversation_surface)  # before initialize()
        sentinel = object()
        host.chat_engine = sentinel
        self.assertIs(host.conversation_surface, sentinel)

    def test_management_surface_lists_builtin_adapters(self):
        # Phase 8: management_surface is implemented; before initialize() it
        # already exposes the registry's built-in adapters and no plugins.
        host = AppHost()
        ms = host.management_surface
        self.assertIn("openai_compatible", ms.list_adapters("llm"))
        self.assertIn("sqlite", ms.list_adapters("memory"))
        self.assertEqual(ms.list_plugins(), [])


if __name__ == "__main__":
    unittest.main()

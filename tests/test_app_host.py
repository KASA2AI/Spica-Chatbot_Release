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

    def test_management_surface_not_implemented_until_phase_8(self):
        host = AppHost()
        with self.assertRaises(NotImplementedError):
            _ = host.management_surface


if __name__ == "__main__":
    unittest.main()

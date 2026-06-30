"""Slim-parity import preflight logic (LOCAL_RUNTIME_PLAN B1 step4, CI-pure).

The preflight ACTUALLY imports the vendored inference from the slim base in a fresh
subprocess; here we test its decision logic via an INJECTED importer (no torch / GPU /
real model). A missing module must block parity and name the module.
"""

import unittest

from scripts.local_runtime.verify_tts_slim_parity import import_check


class ImportCheckTest(unittest.TestCase):
    def test_import_ok(self):
        ok, detail = import_check("/slim/base", importer=lambda root: ("c", "c", "g", "i"))
        self.assertTrue(ok)
        self.assertIsNone(detail)

    def test_missing_module_blocks_and_names_it(self):
        def missing(root):
            raise ModuleNotFoundError("No module named 'tools.assets'", name="tools.assets")

        ok, detail = import_check("/slim/base", importer=missing)
        self.assertFalse(ok)            # blocks parity
        self.assertIn("tools.assets", detail)  # names the missing module

    def test_any_import_error_blocks(self):
        def boom(root):
            raise RuntimeError("cuda init failed")

        ok, detail = import_check("/slim/base", importer=boom)
        self.assertFalse(ok)
        self.assertIn("RuntimeError", detail)


if __name__ == "__main__":
    unittest.main()

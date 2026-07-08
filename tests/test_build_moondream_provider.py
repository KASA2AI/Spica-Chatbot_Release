"""build_moondream_provider factory (LOCAL_RUNTIME_PLAN cut 4).

The single Moondream-provider selection point. Pins: default ``moondream_local``
returns ``None`` (zero-diff -- host installs nothing, manager seam calls the legacy
backend); ``moondream_hf`` returns the isolated ``MoondreamHfProvider``; unknown /
blank names degrade to the fallback (``None`` -> legacy) instead of crashing
startup. Mirror of ``test_build_ocr_adapter``.
"""

import unittest

from spica.host.agent_assembly import build_moondream_provider
from spica.local_runtime.vision import MoondreamHfProvider


class BuildMoondreamProviderTest(unittest.TestCase):
    def test_default_is_none_zero_diff(self):
        # default moondream_local -> None -> host installs nothing -> legacy seam.
        self.assertIsNone(build_moondream_provider())
        self.assertIsNone(build_moondream_provider("moondream_local"))

    def test_moondream_hf_selectable(self):
        provider = build_moondream_provider("moondream_hf")
        self.assertIsInstance(provider, MoondreamHfProvider)
        self.assertEqual(provider.name, "moondream_hf")

    def test_unknown_provider_falls_back_to_legacy_none(self):
        # unknown + default fallback (moondream_local) -> None -> legacy.
        self.assertIsNone(
            build_moondream_provider("totally_unknown", fallback_provider="moondream_local")
        )

    def test_unknown_with_no_fallback_is_none(self):
        self.assertIsNone(build_moondream_provider("totally_unknown", fallback_provider=None))

    def test_unknown_falling_back_to_hf_yields_hf(self):
        # the fallback path is generic: an explicit hf fallback resolves to hf.
        provider = build_moondream_provider("totally_unknown", fallback_provider="moondream_hf")
        self.assertIsInstance(provider, MoondreamHfProvider)

    def test_blank_provider_defaults_to_none(self):
        self.assertIsNone(build_moondream_provider("  "))


if __name__ == "__main__":
    unittest.main()

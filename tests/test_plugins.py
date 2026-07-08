"""Phase 8: plugin loading + ManagementSurface.

An external plugin package registers a new TTS adapter via register(registry);
it then appears in the adapter list and is resolvable -- with no core change.
"""

import tempfile
import unittest
from pathlib import Path

import yaml

from spica.config.manager import ConfigManager
from spica.host.management import ManagementSurface
from spica.plugins.host import PluginHost
from spica.plugins.manifest import load_plugin_manifest
from spica.plugins.registry import CapabilityRegistry

PLUGIN_CODE = (
    "def register(registry):\n"
    "    registry.register_tts('example_tts', lambda **kw: {'name': 'example_tts'})\n"
)


def _make_plugin(plugins_root: Path, name: str, code: str) -> None:
    pkg = plugins_root / name
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text(code, encoding="utf-8")


def _make_manifest(path: Path, entries: list[tuple[str, bool]]) -> None:
    plugins = [{"name": n, "enabled": e} for n, e in entries]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump({"plugins": plugins}), encoding="utf-8")


class PluginHostTest(unittest.TestCase):
    def test_enabled_plugin_registers_adapter(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "plugins"
            _make_plugin(root, "example_tts", PLUGIN_CODE)
            manifest = Path(tmp) / "plugins.yaml"
            _make_manifest(manifest, [("example_tts", True)])
            registry = CapabilityRegistry()
            host = PluginHost(registry, plugins_root=root, manifest_path=manifest)
            host.load()
            self.assertEqual(host.loaded_plugins(), ["example_tts"])
            self.assertIn("example_tts", registry.list_adapters("tts"))
            # Resolvable without touching core code.
            self.assertEqual(registry.resolve_tts("example_tts")["name"], "example_tts")

    def test_disabled_plugin_not_loaded(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "plugins"
            _make_plugin(root, "example_tts", PLUGIN_CODE)
            manifest = Path(tmp) / "plugins.yaml"
            _make_manifest(manifest, [("example_tts", False)])
            registry = CapabilityRegistry()
            host = PluginHost(registry, plugins_root=root, manifest_path=manifest)
            host.load()
            self.assertEqual(host.loaded_plugins(), [])
            self.assertNotIn("example_tts", registry.list_adapters("tts"))

    def test_bad_plugin_recorded_not_raised(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "plugins"
            _make_plugin(root, "broken", "x = 1  # no register()\n")
            manifest = Path(tmp) / "plugins.yaml"
            _make_manifest(manifest, [("broken", True)])
            registry = CapabilityRegistry()
            host = PluginHost(registry, plugins_root=root, manifest_path=manifest)
            host.load()  # must not raise
            self.assertEqual(host.loaded_plugins(), [])
            self.assertIn("broken", host.errors())

    def test_no_manifest_loads_nothing(self):
        registry = CapabilityRegistry()
        host = PluginHost(registry, plugins_root="/nope", manifest_path="/nope/plugins.yaml")
        host.load()
        self.assertEqual(host.loaded_plugins(), [])


class ManagementSurfaceTest(unittest.TestCase):
    def test_lists_adapters_plugins_characters(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "plugins"
            _make_plugin(root, "example_tts", PLUGIN_CODE)
            manifest = Path(tmp) / "plugins.yaml"
            _make_manifest(manifest, [("example_tts", True)])
            chars = Path(tmp) / "characters"
            (chars / "mina").mkdir(parents=True)
            (chars / "mina" / "meta.json").write_text('{"slug": "mina", "name": "Mina"}', encoding="utf-8")

            registry = CapabilityRegistry()
            host = PluginHost(registry, plugins_root=root, manifest_path=manifest)
            host.load()
            ms = ManagementSurface(
                registry=registry,
                config_manager=ConfigManager(config_path=Path(tmp) / "app.yaml"),
                plugin_host=host,
                characters_root=chars,
                plugins_manifest_path=manifest,
            )
            self.assertIn("example_tts", ms.list_adapters("tts"))
            self.assertEqual(ms.list_plugins(), ["example_tts"])
            self.assertEqual([c["character_id"] for c in ms.list_characters()], ["mina"])

    def test_uninstall_then_install_toggles_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            manifest = Path(tmp) / "plugins.yaml"
            _make_manifest(manifest, [("example_tts", True)])
            ms = ManagementSurface(
                registry=CapabilityRegistry(),
                config_manager=ConfigManager(config_path=Path(tmp) / "app.yaml"),
                plugin_host=PluginHost(CapabilityRegistry(), manifest_path=manifest),
                characters_root=Path(tmp),
                plugins_manifest_path=manifest,
            )
            ms.uninstall_plugin("example_tts")
            entry = next(e for e in load_plugin_manifest(manifest) if e.name == "example_tts")
            self.assertFalse(entry.enabled)
            ms.install_plugin("example_tts")
            entry = next(e for e in load_plugin_manifest(manifest) if e.name == "example_tts")
            self.assertTrue(entry.enabled)


if __name__ == "__main__":
    unittest.main()

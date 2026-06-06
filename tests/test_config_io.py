"""Phase 3b: config IO supports JSON and YAML; tts/visual configs moved to YAML."""

import json
import tempfile
import unittest
from pathlib import Path

import yaml

from agent_tools.config_io import read_config_file, write_config_file


class ConfigIOTest(unittest.TestCase):
    def test_json_and_yaml_read_equivalent(self):
        data = {"a": 1, "b": ["x", "y"], "c": {"d": "中文"}}
        with tempfile.TemporaryDirectory() as tmp:
            jp = Path(tmp) / "c.json"
            yp = Path(tmp) / "c.yaml"
            jp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            yp.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
            self.assertEqual(read_config_file(jp), data)
            self.assertEqual(read_config_file(yp), data)

    def test_write_read_roundtrip(self):
        data = {"k": "v", "n": 2, "nested": {"x": [1, 2]}}
        with tempfile.TemporaryDirectory() as tmp:
            for name in ("c.yaml", "c.json"):
                p = Path(tmp) / name
                write_config_file(p, data)
                self.assertEqual(read_config_file(p), data)


class MigratedConfigTest(unittest.TestCase):
    def test_tts_config_default_is_yaml(self):
        from agent_tools.tts import load_tts_config

        cfg = load_tts_config()
        self.assertEqual(cfg.get("provider"), "gptsovits_current")
        self.assertTrue(cfg.get("gptsovits_root"))
        self.assertTrue(str(cfg.get("_config_path")).endswith("tts.yaml"))

    def test_visual_config_default_is_yaml_and_rules_resolve(self):
        from agent_tools.visual import VisualDiffService

        visual = VisualDiffService()
        self.assertTrue(str(visual.config_path).endswith("visual.yaml"))
        self.assertTrue(visual.config.get("diff_root"))
        # rules_path resolved relative to data/config/ and loaded.
        self.assertTrue(visual.rules.get("expressions"))


if __name__ == "__main__":
    unittest.main()

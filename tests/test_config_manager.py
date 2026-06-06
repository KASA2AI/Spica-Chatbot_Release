"""Phase 3 tests for the typed config layer.

Hermetic: ``load_dotenv`` is patched to a no-op so the real ``xiaosan.env`` never
leaks into these assertions, and ``os.environ`` is replaced per-case.
"""

import os
import unittest
from pathlib import Path
from unittest.mock import patch

from pydantic import ValidationError

from spica.config.manager import ConfigManager
from spica.config.schema import AppConfig

_MISSING_YAML = Path("/nonexistent/spica-test/app.yaml")


@patch("spica.config.manager.load_dotenv", lambda *a, **k: None)
class ConfigManagerTest(unittest.TestCase):
    def test_defaults_match_historical_fallbacks(self):
        with patch.dict(os.environ, {}, clear=True):
            cfg = ConfigManager(config_path=_MISSING_YAML).load()
        self.assertEqual(cfg.llm.model, "gpt-4.1-mini")
        self.assertIsNone(cfg.llm.base_url)
        self.assertEqual(cfg.memory.recent_memory_turns, 3)
        self.assertEqual(cfg.memory.recent_context_limit, 3)
        self.assertEqual(cfg.memory.long_term_memory_limit, 5)
        self.assertEqual(cfg.memory.long_term_memory_budget_chars, 1200)
        self.assertEqual(cfg.memory.recent_turn_char_limit, 360)
        self.assertEqual(cfg.memory.max_long_term_memories, 200)
        self.assertEqual(cfg.max_tool_rounds, 3)
        self.assertIsNone(cfg.character.interlocutor_name)
        self.assertIsNone(cfg.character.profile_override)
        self.assertIsNone(cfg.character.skill_dir)

    def test_env_overrides_apply(self):
        env = {
            "MODEL": "deepseek-chat",
            "OPENAI_BASE_URL": "https://api.deepseek.com/v1",
            "RECENT_MEMORY_TURNS": "7",
            "MAX_LONG_TERM_MEMORIES": "42",
            "MAX_TOOL_ROUNDS": "5",
            "SPICA_USER_NAME": "テスト",
            "SPICA_SKILL_DIR": "spica_data/Other_skill",
        }
        with patch.dict(os.environ, env, clear=True):
            cfg = ConfigManager(config_path=_MISSING_YAML).load()
        self.assertEqual(cfg.llm.model, "deepseek-chat")
        self.assertEqual(cfg.llm.base_url, "https://api.deepseek.com/v1")
        self.assertEqual(cfg.memory.recent_memory_turns, 7)
        self.assertEqual(cfg.memory.max_long_term_memories, 42)
        self.assertEqual(cfg.max_tool_rounds, 5)
        self.assertEqual(cfg.character.interlocutor_name, "テスト")
        self.assertEqual(cfg.character.skill_dir, "spica_data/Other_skill")

    def test_empty_env_var_falls_back_to_default(self):
        # Old behaviour: ``os.getenv("MODEL") or "gpt-4.1-mini"`` -> "" yields default.
        with patch.dict(os.environ, {"MODEL": "", "RECENT_MEMORY_TURNS": ""}, clear=True):
            cfg = ConfigManager(config_path=_MISSING_YAML).load()
        self.assertEqual(cfg.llm.model, "gpt-4.1-mini")
        self.assertEqual(cfg.memory.recent_memory_turns, 3)

    def test_env_overrides_yaml_overrides_defaults(self):
        yaml_data = {"llm": {"model": "from-yaml"}, "max_tool_rounds": 9}
        with patch.object(ConfigManager, "_read_yaml", staticmethod(lambda path: yaml_data)):
            with patch.dict(os.environ, {}, clear=True):
                cfg = ConfigManager().load()
                self.assertEqual(cfg.llm.model, "from-yaml")
                self.assertEqual(cfg.max_tool_rounds, 9)
            with patch.dict(os.environ, {"MODEL": "from-env"}, clear=True):
                cfg = ConfigManager().load()
                self.assertEqual(cfg.llm.model, "from-env")  # env wins over yaml
                self.assertEqual(cfg.max_tool_rounds, 9)  # yaml still wins over default

    def test_merge_is_recursive_and_nonmutating(self):
        base = {"llm": {"model": "a", "base_url": "x"}, "max_tool_rounds": 1}
        override = {"llm": {"model": "b"}, "max_tool_rounds": 2}
        merged = ConfigManager.merge(base, override)
        self.assertEqual(merged["llm"], {"model": "b", "base_url": "x"})
        self.assertEqual(merged["max_tool_rounds"], 2)
        self.assertEqual(base["llm"], {"model": "a", "base_url": "x"})  # untouched

    def test_validate_rejects_bad_type(self):
        with self.assertRaises(ValidationError):
            ConfigManager.validate({"memory": {"recent_memory_turns": "not-an-int"}})

    def test_validate_accepts_empty(self):
        self.assertIsInstance(ConfigManager.validate({}), AppConfig)


if __name__ == "__main__":
    unittest.main()

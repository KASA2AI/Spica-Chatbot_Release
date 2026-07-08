"""Phase 4 tests: generic {{char}}/{{user}} templating.

Confirms the prompt-building mechanism is character-agnostic: feeding Spica's
values reproduces Spica behaviour, and feeding a different character's values
yields that character -- with no Spica/麦 literals in the generic code path.
"""

import unittest

from spica.conversation.character_loader import (
    DEFAULT_CHARACTER_NAME,
    DEFAULT_INTERLOCUTOR_NAME,
    render_character_template,
)
from spica.conversation.prompt_builder import build_spica_prompt, build_system_prompt


class CharacterTemplateTest(unittest.TestCase):
    def test_render_substitutes_both_placeholders(self):
        out = render_character_template("{{char}}对{{user}}说", char="ミナ", user="レン")
        self.assertEqual(out, "ミナ对レン说")
        # No leftover placeholders.
        self.assertNotIn("{{", out)

    def test_spica_defaults_reproduce_legacy_prompt(self):
        # Defaults are the Spica template values.
        self.assertEqual(DEFAULT_CHARACTER_NAME, "スピカ")
        self.assertEqual(DEFAULT_INTERLOCUTOR_NAME, "麦")
        system = build_system_prompt()  # defaults: char=スピカ, user=麦
        self.assertIn("你是 スピカ 的日语语音聊天 agent。", system)
        self.assertIn("当前对话对象固定是麦", system)
        # JSON braces survive as single braces (no .format double-brace artefact).
        self.assertIn('{\n  "answer"', system)
        self.assertNotIn("{{", system)

    def test_generic_character_threads_through_full_prompt(self):
        prompt = build_spica_prompt(
            user_input="やあ",
            recent_context=[{"user_text": "おはよう", "assistant_text": "うん。"}],
            long_term_memories=[{"scope": "character", "content": "ツンデレ"}],
            character_profile="設定",
            interlocutor_name="レン",
            character_name="ミナ",
        )
        self.assertIn("你是 ミナ 的日语语音聊天 agent。", prompt)
        self.assertIn("当前对话对象固定是レン", prompt)
        self.assertIn("ミナ对レン", prompt)  # interlocutor profile line
        self.assertIn("レン: おはよう\nミナ: うん。", prompt)  # recent-context speaker
        self.assertIn("(ミナ/", prompt)  # _scope_label for "character" scope
        # No Spica defaults leaked when a different character is supplied.
        self.assertNotIn("スピカ", prompt)

    def test_recent_context_speaker_uses_character_name(self):
        prompt = build_spica_prompt(
            user_input="x",
            recent_context=[{"user_text": "a", "assistant_text": "b"}],
            long_term_memories=[],
            character_profile="",
            interlocutor_name="kasa",
        )
        self.assertIn("kasa: a\nスピカ: b", prompt)  # default char = スピカ


if __name__ == "__main__":
    unittest.main()

"""Display-only bilingual dialog (character.dialog_display_language == "zh").

The model appends a ⟦中文⟧ translation after each Japanese sentence; the dialog
box displays the translation while TTS / memory / done keep the pure-Japanese
side. ja mode (default) must stay byte-identical. Self-contained fakes; no real
LLM/TTS.
"""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from agent_tools.function_tools import TOOL_SCHEMAS, default_tool_functions
from agent_tools.tts.schemas import TTSRequest, TTSResult
from memory.recent import RecentMemory
from memory.store import SQLiteMemoryStore
from spica.config.schema import AppConfig, CharacterConfig, StreamConfig
from spica.conversation.prompt_builder import (
    BILINGUAL_DISPLAY_RULES,
    SYSTEM_PROMPT_TEMPLATE,
    build_spica_prompt,
    build_system_prompt,
)
from spica.conversation.text_normalizer import split_dialog_translation
from spica.core.chat_engine import ChatEngine
from spica.core.events import DoneEvent, UnitReadyEvent, UnitTextReadyEvent
from spica.core.proactive import may_become_no_comment
from spica.runtime.context import TurnContext, TurnRequest
from spica.runtime.orchestrator import stream_voice_events
from spica.runtime.play_unit_splitter import PlayUnitSplitter
from spica.runtime.services import AgentServices


BILINGUAL_ANSWER = "おはよう、麦。⟦早上好，麦。⟧今日は何する？⟦今天做什么？⟧"
SPOKEN_ANSWER = "おはよう、麦。今日は何する？"


class _FakeResponse:
    def __init__(self, text):
        self.id = "fake-stream-response"
        self.output_text = text
        self.output = []
        self.usage = SimpleNamespace(input_tokens=10, output_tokens=5, total_tokens=15)


class _FakeResponses:
    def __init__(self, text):
        self.text = text
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs.get("stream"):
            chunks = [self.text[index:index + 9] for index in range(0, len(self.text), 9)]
            events = [
                SimpleNamespace(type="response.output_text.delta", delta=chunk)
                for chunk in chunks
            ]
            events.append(SimpleNamespace(type="response.completed", response=_FakeResponse(self.text)))
            return iter(events)
        return _FakeResponse(self.text)


class _FakeLLMClient:
    def __init__(self, text):
        self.responses = _FakeResponses(text)


class _FakeVisual:
    def __init__(self):
        self.calls = []

    def prepare_stream_context(self, requested_costume=None, requested_mode=None):
        return {"costume": "school", "costume_mode": "fixed", "classifier_version": "fake"}

    def build_unit_visual_payload(self, **kwargs):
        self.calls.append(kwargs)
        return {
            "costume": "school", "classifier_version": "fake",
            "selection_source": "local_vote_classifier", "selection_error": None,
            "classifier": {"duration_ms": 3.0}, "dialog": {}, "character": {},
            "cue": {"index": kwargs["unit_index"], "text": kwargs["current_unit_text"],
                    "expression_id": "002", "hand_pose": "normal", "image_url": "/f.png", "reason": "f"},
        }


class _FakeTTS:
    name = "fake_tts"

    def __init__(self):
        self.calls = []

    def synthesize(self, request):
        assert isinstance(request, TTSRequest)
        self.calls.append({"text": request.text, "emotion": request.emotion})
        return TTSResult(ok=True, provider=self.name, audio_url="/v.wav", audio_path="/tmp/v.wav",
                         timing={"tts_total_ms": 1.0}, duration_ms=1.0)


def _make_services(tmpdir, answer_text, **config_extra):
    raw = json.dumps(
        {"answer": answer_text, "emotion": "happy", "emotion_reason": "説明口調。"},
        ensure_ascii=False,
    )
    config = {
        "model": "fake-model",
        "character_profile": "profile",
        "recent_context_limit": 3,
        "long_term_memory_limit": 5,
        "max_tool_rounds": 2,
    }
    config.update(config_extra)
    return AgentServices(
        llm_client=_FakeLLMClient(raw),
        tts_adapter=_FakeTTS(),
        visual_tool=_FakeVisual(),
        memory_store=SQLiteMemoryStore(Path(tmpdir) / "memory.sqlite3"),
        recent_memory=RecentMemory(max_turns=3),
        config=config,
        logger=lambda *args, **kwargs: None,
        tool_functions=default_tool_functions(),
        tool_schemas=TOOL_SCHEMAS,
    )


class SplitDialogTranslationTests(unittest.TestCase):
    def test_text_without_brackets_passes_through_untouched(self):
        text = "おはよう、麦。  今日は何する？"  # spacing preserved: byte-identity short-circuit
        self.assertEqual(split_dialog_translation(text), (text, ""))

    def test_pairs_split_into_spoken_and_subtitle(self):
        self.assertEqual(
            split_dialog_translation(BILINGUAL_ANSWER),
            (SPOKEN_ANSWER, "早上好，麦。今天做什么？"),
        )

    def test_unclosed_bracket_treats_rest_as_subtitle(self):
        self.assertEqual(
            split_dialog_translation("おはよう。⟦早上好"),
            ("おはよう。", "早上好"),
        )

    def test_all_translation_returns_empty_spoken(self):
        self.assertEqual(split_dialog_translation("⟦只有中文。⟧"), ("", "只有中文。"))

    def test_stray_close_is_dropped_from_spoken(self):
        self.assertEqual(split_dialog_translation("おはよう。⟧⟦早上好。⟧"), ("おはよう。", "早上好。"))

    def test_no_comment_with_translation_still_reads_as_sentinel(self):
        # The orchestrator judges sentinel-compat on the spoken side: a model
        # that appends ⟦⟧ to NO_COMMENT must not release the system-turn hold.
        spoken, _ = split_dialog_translation("NO_COMMENT⟦无可奉告⟧")
        self.assertTrue(may_become_no_comment(spoken))


class BilingualPlayUnitSplitterTests(unittest.TestCase):
    def test_terminators_inside_brackets_do_not_cut(self):
        splitter = PlayUnitSplitter(min_chars=1, max_chars=96, bilingual_brackets=True)
        units = splitter.feed(BILINGUAL_ANSWER) + splitter.flush()
        self.assertEqual(
            units,
            ["おはよう、麦。⟦早上好，麦。⟧", "今日は何する？⟦今天做什么？⟧"],
        )

    def test_sentence_at_buffer_end_waits_for_possible_translation(self):
        splitter = PlayUnitSplitter(min_chars=1, max_chars=96, bilingual_brackets=True)
        self.assertEqual(splitter.feed("おはよう、麦。"), [])  # ⟦ may still follow
        units = splitter.feed("⟦早上好，麦。⟧今日は何する？⟦今天做什么？⟧") + splitter.flush()
        self.assertEqual(
            units,
            ["おはよう、麦。⟦早上好，麦。⟧", "今日は何する？⟦今天做什么？⟧"],
        )

    def test_flush_releases_unclosed_translation_tail(self):
        splitter = PlayUnitSplitter(min_chars=1, max_chars=96, bilingual_brackets=True)
        self.assertEqual(splitter.feed("おはよう、麦。⟦早上好"), [])
        self.assertEqual(splitter.flush(), ["おはよう、麦。⟦早上好"])

    def test_unit_sizing_counts_spoken_side_only(self):
        # Both Japanese sides are short (6 + 10 = 16 < 18) so the pairs merge
        # into ONE unit even though the raw pair text is far beyond min_chars.
        splitter = PlayUnitSplitter(min_chars=18, max_chars=96, bilingual_brackets=True)
        units = splitter.feed("こんにちは。⟦你好。⟧今日は何をして遊ぶ？⟦今天玩点什么？⟧") + splitter.flush()
        self.assertEqual(len(units), 1)
        self.assertIn("こんにちは。⟦你好。⟧", units[0])
        self.assertIn("今日は何をして遊ぶ？⟦今天玩点什么？⟧", units[0])

    def test_overlong_pair_stays_atomic(self):
        # Sub-splitting by pause marks would cut inside ⟦⟧ -- an overlong pair
        # is kept whole (the TTS engine re-chunks internally).
        splitter = PlayUnitSplitter(min_chars=1, max_chars=10, bilingual_brackets=True)
        sentence = "あいうえお、かきくけこ、さしすせそ。⟦中文翻译，带逗号，也很长。⟧"
        units = splitter.feed(sentence) + splitter.flush()
        self.assertEqual(units, [sentence])


class BilingualPromptTests(unittest.TestCase):
    def test_ja_mode_is_the_default_and_unchanged(self):
        self.assertEqual(build_system_prompt("麦"), build_system_prompt("麦", dialog_display_language="ja"))
        self.assertNotIn("双语字幕模式", build_system_prompt("麦"))

    def test_zh_mode_inserts_rule_between_rules_and_format(self):
        prompt = build_system_prompt("麦", dialog_display_language="zh")
        rule = BILINGUAL_DISPLAY_RULES
        self.assertIn(rule, prompt)
        self.assertLess(prompt.index("写死回复规则。"), prompt.index(rule))
        self.assertLess(prompt.index(rule), prompt.index("JSON 格式："))
        self.assertIn("⟦⟧", prompt)
        # Everything from the ja template is still present around the new rule.
        rules_part, format_part = SYSTEM_PROMPT_TEMPLATE.split("\n\nJSON 格式：\n", 1)
        self.assertIn("JSON 格式：", prompt)
        self.assertIn(format_part.splitlines()[0], prompt)
        self.assertIn(rules_part.splitlines()[-1].replace("{{user}}", "麦").split("。")[0], prompt)

    def test_build_spica_prompt_threads_the_flag(self):
        prompt = build_spica_prompt(
            user_input="こんにちは",
            recent_context=[],
            long_term_memories=[],
            character_profile="profile",
            dialog_display_language="zh",
        )
        self.assertIn("双语字幕模式", prompt)


class BilingualStreamingTests(unittest.TestCase):
    def _run(self, answer, **config_extra):
        with tempfile.TemporaryDirectory() as tmpdir:
            services = _make_services(tmpdir, answer, **config_extra)
            events = list(
                stream_voice_events(
                    TurnContext(TurnRequest(conversation_id="c1", user_input="説明して")),
                    services,
                )
            )
        return services, events

    def test_zh_mode_displays_subtitles_and_speaks_japanese(self):
        services, events = self._run(
            BILINGUAL_ANSWER, dialog_display_language="zh", play_unit_min_chars=6
        )
        text_events = [e["data"] for e in events if e["event"] == "unit_text_ready"]
        ready_events = [e["data"] for e in events if e["event"] == "unit_ready"]
        done = [e for e in events if e["event"] == "done"][-1]["data"]

        self.assertEqual([d["display_text"] for d in text_events], ["早上好，麦。", "今天做什么？"])
        self.assertEqual([d["display_text"] for d in ready_events], ["早上好，麦。", "今天做什么？"])
        for data in text_events + ready_events:
            self.assertNotIn("⟦", data["tts_text"])
        # TTS synthesizes the Japanese side only.
        self.assertEqual(
            [call["text"] for call in services.tts_adapter.calls],
            ["おはよう、麦。", "今日は何する？"],
        )
        # Internal consumers (visual classifier) keep the Japanese side.
        self.assertEqual(
            [call["current_unit_text"] for call in services.visual_tool.calls],
            ["おはよう、麦。", "今日は何する？"],
        )
        # The terminal answer (memory + done) is pure Japanese.
        self.assertEqual(done["answer"], SPOKEN_ANSWER)
        # The prompt sent to the LLM carries the bilingual rule (stages wiring).
        prompt_sent = str(services.llm_client.responses.calls[0].get("input") or "")
        self.assertIn("双语字幕模式", prompt_sent)

    def test_ja_mode_prompt_carries_no_bilingual_rule(self):
        services, events = self._run("おはよう、麦。今日は何する？", play_unit_min_chars=6)
        done = [e for e in events if e["event"] == "done"][-1]["data"]
        self.assertEqual(done["answer"], "おはよう、麦。今日は何する？")
        prompt_sent = str(services.llm_client.responses.calls[0].get("input") or "")
        self.assertNotIn("双语字幕模式", prompt_sent)

    def test_ja_mode_preserves_literal_bracket_glyphs(self):
        # Flag-gate regression: a plain (default ja) answer that happens to
        # contain a literal ⟦abc⟧ must NOT be split -- the glyphs survive
        # verbatim in display_text, tts_text, and the terminal done.answer,
        # byte-identical to pre-bilingual behaviour.
        answer = "これは記号の説明です⟦abc⟧。"
        services, events = self._run(answer, play_unit_min_chars=6)
        text_events = [e["data"] for e in events if e["event"] == "unit_text_ready"]
        done = [e for e in events if e["event"] == "done"][-1]["data"]

        joined_display = "".join(d["display_text"] for d in text_events)
        joined_tts = "".join(d["tts_text"] for d in text_events)
        self.assertIn("⟦abc⟧", joined_display)
        self.assertIn("⟦abc⟧", joined_tts)
        self.assertEqual(joined_display, answer)
        self.assertEqual(done["answer"], answer)
        # The Japanese side reached TTS untouched (no split happened).
        self.assertEqual("".join(c["text"] for c in services.tts_adapter.calls), answer)

    def test_all_translation_unit_degrades_to_visible_playable_unit(self):
        # Hole-2 fallback: a unit that is ONLY ⟦中文⟧ (broken pair format) must
        # still produce a visible, playable unit -- never be silently dropped.
        services, events = self._run("⟦只有中文没有日语。⟧", dialog_display_language="zh")
        text_events = [e["data"] for e in events if e["event"] == "unit_text_ready"]
        done = [e for e in events if e["event"] == "done"][-1]["data"]
        self.assertEqual([d["display_text"] for d in text_events], ["只有中文没有日语。"])
        self.assertEqual([call["text"] for call in services.tts_adapter.calls], ["只有中文没有日语。"])
        self.assertEqual(done["answer"], "只有中文没有日语。")


class BilingualTypedEventBoundaryTests(unittest.TestCase):
    """Through ChatEngine.stream_voice_runtime: the typed RuntimeEvent dataclasses
    must carry the subtitle -- catches a typed boundary dropping the field."""

    def test_typed_events_carry_subtitle_and_japanese_answer(self):
        config = AppConfig(
            character=CharacterConfig(
                profile_override="麦のプロフィール",
                dialog_display_language="zh",
            ),
            stream=StreamConfig(play_unit_min_chars=6),
        )
        with tempfile.TemporaryDirectory() as tmp:
            engine = ChatEngine(_make_services(tmp, BILINGUAL_ANSWER), config)
            events = list(engine.stream_voice_runtime("説明して"))

        text_events = [e for e in events if isinstance(e, UnitTextReadyEvent)]
        ready_events = [e for e in events if isinstance(e, UnitReadyEvent)]
        done = [e for e in events if isinstance(e, DoneEvent)][-1]
        self.assertEqual([e.display_text for e in text_events], ["早上好，麦。", "今天做什么？"])
        self.assertEqual([e.display_text for e in ready_events], ["早上好，麦。", "今天做什么？"])
        for event in text_events + ready_events:
            self.assertNotIn("⟦", event.tts_text)
        self.assertEqual(done.answer, SPOKEN_ANSWER)


class DialogDisplayLanguageConfigTests(unittest.TestCase):
    def test_default_is_ja(self):
        self.assertEqual(AppConfig().character.dialog_display_language, "ja")

    def test_zh_is_accepted(self):
        self.assertEqual(
            CharacterConfig(dialog_display_language="zh").dialog_display_language, "zh"
        )

    def test_typo_fails_loud(self):
        with self.assertRaises(Exception):
            CharacterConfig(dialog_display_language="cn")


if __name__ == "__main__":
    unittest.main()

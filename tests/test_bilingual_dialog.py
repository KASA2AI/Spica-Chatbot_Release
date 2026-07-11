"""Display-only bilingual dialog (character.dialog_display_language == "zh").

The model appends a ⟦中文⟧ translation after each Japanese sentence; the dialog
box displays the translation while TTS / memory / done keep the pure-Japanese
side. ja mode (default) must stay byte-identical. Self-contained fakes; no real
LLM/TTS.
"""

import json
import random
import tempfile
import unittest
import unicodedata
from pathlib import Path
from types import SimpleNamespace

from agent_tools.function_tools import TOOL_SCHEMAS, default_tool_functions
from agent_tools.tts.schemas import TTSRequest, TTSResult
from memory.recent import RecentMemory
from memory.store import SQLiteMemoryStore
from spica.adapters.memory.sqlite import scoped_conversation_id
from spica.config.schema import AppConfig, CharacterConfig, StreamConfig
from spica.conversation.prompt_builder import (
    BILINGUAL_DISPLAY_RULES,
    BILINGUAL_OUTPUT_REMINDER,
    SYSTEM_PROMPT_TEMPLATE,
    bilingual_output_reminder,
    build_spica_prompt,
    build_system_prompt,
)
from spica.conversation.text_normalizer import (
    build_bilingual_display,
    spoken_channel_is_paired,
    spoken_channel_or_fallback,
    split_dialog_translation,
)
from spica.runtime.tool_round import build_tool_followup_prompt
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
    def test_grouped_translation_waits_for_complete_translation_channel(self):
        cases = (
            (
                "パンはパンでも、食べられないパンは？フライパンよ。"
                "⟦面包里有一种不能吃的面包，是哪种？是平底锅。⟧",
                "面包里有一种不能吃的面包，是哪种？是平底锅。",
            ),
            (
                "忘れたら、……私が困るのよ。⟦要是忘了，……我会很麻烦的。⟧",
                "要是忘了，……我会很麻烦的。",
            ),
        )
        for text, expected_display in cases:
            with self.subTest(text=text):
                splitter = PlayUnitSplitter(min_chars=1, max_chars=200, bilingual_brackets=True)
                units = []
                for char in text:  # worst-case streaming: one Unicode scalar per delta
                    units.extend(splitter.feed(char))
                units.extend(splitter.flush())
                self.assertEqual(units, [text])
                self.assertEqual(build_bilingual_display(units[0]), expected_display)

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

    def test_quote_bracket_terminators_do_not_desync_translation(self):
        # Spica quoting a galgame line: a 。 INSIDE 【】 / 「」 is quoted content,
        # not a sentence boundary. Cutting there would emit the leading Japanese as
        # a translation-less unit (subtitle shows raw JP) and strand its ⟦中文⟧ on a
        # later fragment. The 日语【…】⟦中文⟧ pair must stay ONE unit.
        for text in (
            "理理は【今日はいい天気ね。】と言った。⟦理理说【今天天气真好呢。】。⟧",
            "彼女は「行こう。」と誘った。⟦她邀请说「走吧。」。⟧",       # 「」 same class
            "彼は『嬉しい。楽しい。』と叫んだ。⟦他喊道『好开心。好快乐。』。⟧",  # multi-sentence quote
        ):
            splitter = PlayUnitSplitter(min_chars=6, max_chars=200, bilingual_brackets=True)
            units = splitter.feed(text) + splitter.flush()
            self.assertEqual(units, [text])                       # atomic, no desync
            self.assertNotIn("は", build_bilingual_display(units[0]))  # subtitle is pure Chinese


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
        # The display may use the supplied Chinese subtitle, but the spoken /
        # memory channel must remain Japanese.
        services, events = self._run("⟦只有中文没有日语。⟧", dialog_display_language="zh")
        text_events = [e["data"] for e in events if e["event"] == "unit_text_ready"]
        done = [e for e in events if e["event"] == "done"][-1]["data"]
        self.assertEqual([d["display_text"] for d in text_events], ["只有中文没有日语。"])
        self.assertEqual(
            [call["text"] for call in services.tts_adapter.calls],
            ["すみません、もう一度話しかけてください。"],
        )
        self.assertEqual(done["answer"], "すみません、もう一度話しかけてください。")
        recent = services.recent_memory.get_recent(scoped_conversation_id("spica", "c1"))
        self.assertEqual(recent[-1]["assistant_text"], "すみません、もう一度話しかけてください。")

    def test_all_translation_japanese_never_bypasses_zh_display_validation(self):
        services, events = self._run("⟦おはよう。⟧", dialog_display_language="zh")
        text_events = [e["data"] for e in events if e["event"] == "unit_text_ready"]
        done = [e for e in events if e["event"] == "done"][-1]["data"]

        self.assertEqual(
            [data["display_text"] for data in text_events],
            ["（中文字幕暂时缺失。）"],
        )
        self.assertEqual(
            [call["text"] for call in services.tts_adapter.calls],
            ["すみません、もう一度話しかけてください。"],
        )
        self.assertEqual(done["answer"], "すみません、もう一度話しかけてください。")
        recent = services.recent_memory.get_recent(scoped_conversation_id("spica", "c1"))
        self.assertEqual(recent[-1]["assistant_text"], "すみません、もう一度話しかけてください。")

    def test_zh_partial_compliance_never_leaks_japanese_into_subtitles(self):
        # The model translated はい but DROPPED the final うん's ⟦⟧. zh is a
        # Chinese-subtitle contract: the display must use a
        # Chinese missing-subtitle notice instead of exposing the spoken Japanese.
        # TTS / done still keep every Japanese sentence intact.
        services, events = self._run(
            "はい。⟦好。⟧うん。", dialog_display_language="zh", play_unit_min_chars=6
        )
        text_events = [e["data"] for e in events if e["event"] == "unit_text_ready"]
        done = [e for e in events if e["event"] == "done"][-1]["data"]
        joined = "".join(d["display_text"] for d in text_events)
        self.assertNotIn("⟦", joined)
        self.assertNotIn("⟧", joined)
        self.assertIn("好", joined)            # translated sentence shows Chinese
        self.assertIn("中文字幕暂时缺失", joined)
        self.assertNotIn("うん", joined)        # untranslated Japanese never reaches UI
        for call in services.tts_adapter.calls:
            self.assertNotIn("⟦", call["text"])
            self.assertNotIn("好", call["text"])
        self.assertNotIn("⟦", done["answer"])
        self.assertNotIn("好", done["answer"])
        self.assertIn("うん", done["answer"])  # memory keeps the pure-Japanese line

    def test_zh_zero_compliance_uses_chinese_notice_and_keeps_japanese_voice(self):
        services, events = self._run(
            "翻訳を全部忘れた返事です。",
            dialog_display_language="zh",
            play_unit_min_chars=1,
        )
        text_events = [e["data"] for e in events if e["event"] == "unit_text_ready"]
        done = [e for e in events if e["event"] == "done"][-1]["data"]

        self.assertEqual(
            [data["display_text"] for data in text_events],
            ["（中文字幕暂时缺失。）"],
        )
        self.assertEqual(
            [call["text"] for call in services.tts_adapter.calls],
            ["翻訳を全部忘れた返事です。"],
        )
        self.assertEqual(done["answer"], "翻訳を全部忘れた返事です。")

    def test_zh_unpaired_chinese_never_enters_spoken_or_memory_channels(self):
        services, events = self._run(
            "只有中文，没有日语。",
            dialog_display_language="zh",
            play_unit_min_chars=1,
        )
        text_events = [e["data"] for e in events if e["event"] == "unit_text_ready"]
        done = [e for e in events if e["event"] == "done"][-1]["data"]

        self.assertEqual(
            [data["display_text"] for data in text_events],
            ["（中文字幕暂时缺失。）"],
        )
        self.assertEqual(
            [call["text"] for call in services.tts_adapter.calls],
            ["すみません、もう一度話しかけてください。"],
        )
        self.assertEqual(done["answer"], "すみません、もう一度話しかけてください。")
        recent = services.recent_memory.get_recent(scoped_conversation_id("spica", "c1"))
        self.assertEqual(recent[-1]["assistant_text"], "すみません、もう一度話しかけてください。")

    def test_zh_translation_prefix_does_not_trust_unpaired_chinese_tail(self):
        services, events = self._run(
            "⟦中文字幕。⟧只有中文尾巴。",
            dialog_display_language="zh",
            play_unit_min_chars=6,
        )
        text_events = [e["data"] for e in events if e["event"] == "unit_text_ready"]
        done = [e for e in events if e["event"] == "done"][-1]["data"]

        joined_display = "".join(data["display_text"] for data in text_events)
        self.assertNotIn("日", joined_display)
        self.assertIn("中文字幕", joined_display)
        self.assertEqual(
            [call["text"] for call in services.tts_adapter.calls],
            ["すみません、もう一度話しかけてください。"],
        )
        self.assertEqual(done["answer"], "すみません、もう一度話しかけてください。")
        recent = services.recent_memory.get_recent(scoped_conversation_id("spica", "c1"))
        self.assertEqual(recent[-1]["assistant_text"], "すみません、もう一度話しかけてください。")

    def test_zh_stray_close_does_not_forge_pair_for_chinese_tail(self):
        services, events = self._run(
            "⟦中文字幕。⟧只有中文尾巴。⟧",
            dialog_display_language="zh",
            play_unit_min_chars=6,
        )
        text_events = [e["data"] for e in events if e["event"] == "unit_text_ready"]
        done = [e for e in events if e["event"] == "done"][-1]["data"]

        self.assertTrue(all("⟧" not in data["display_text"] for data in text_events))
        self.assertEqual(
            [call["text"] for call in services.tts_adapter.calls],
            ["すみません、もう一度話しかけてください。"],
        )
        self.assertEqual(done["answer"], "すみません、もう一度話しかけてください。")
        recent = services.recent_memory.get_recent(scoped_conversation_id("spica", "c1"))
        self.assertEqual(recent[-1]["assistant_text"], "すみません、もう一度話しかけてください。")

    def test_zh_paired_kanji_only_japanese_remains_in_spoken_and_memory_channels(self):
        services, events = self._run(
            "了解。⟦明白。⟧あとで行く。⟦之后去。⟧",
            dialog_display_language="zh",
            play_unit_min_chars=1,
        )
        text_events = [e["data"] for e in events if e["event"] == "unit_text_ready"]
        done = [e for e in events if e["event"] == "done"][-1]["data"]

        self.assertEqual(
            [data["display_text"] for data in text_events],
            ["明白。", "之后去。"],
        )
        self.assertEqual(
            [call["text"] for call in services.tts_adapter.calls],
            ["了解。", "あとで行く。"],
        )
        self.assertEqual(done["answer"], "了解。あとで行く。")
        recent = services.recent_memory.get_recent(scoped_conversation_id("spica", "c1"))
        self.assertEqual(recent[-1]["assistant_text"], "了解。あとで行く。")

    def test_zh_rejects_japanese_inside_the_translation_channel(self):
        services, events = self._run(
            "おはよう。⟦おはよう。⟧",
            dialog_display_language="zh",
            play_unit_min_chars=1,
        )
        text_events = [e["data"] for e in events if e["event"] == "unit_text_ready"]

        self.assertEqual(
            [data["display_text"] for data in text_events],
            ["（中文字幕暂时缺失。）"],
        )
        self.assertEqual(
            [call["text"] for call in services.tts_adapter.calls],
            ["おはよう。"],
        )

    def test_zh_rejects_supplementary_kana_inside_translation_channel(self):
        _, events = self._run(
            "古い仮名。⟦中文𛀀。⟧",
            dialog_display_language="zh",
            play_unit_min_chars=1,
        )
        text_events = [e["data"] for e in events if e["event"] == "unit_text_ready"]

        self.assertEqual(
            [data["display_text"] for data in text_events],
            ["（中文字幕暂时缺失。）"],
        )


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


class BuildBilingualDisplayTests(unittest.TestCase):
    """Per-sentence zh display: trusted ⟦中文⟧ or a Chinese missing notice.

    Spoken Japanese is never a display fallback.
    """

    def test_all_sentences_translated_show_chinese(self):
        self.assertEqual(
            build_bilingual_display("日语1。⟦中文1。⟧日语2。⟦中文2。⟧"), "中文1。中文2。"
        )

    def test_untranslated_tail_uses_chinese_missing_notice(self):
        self.assertEqual(
            build_bilingual_display("日语1。⟦中文1。⟧日语2。"),
            "中文1。（中文字幕暂时缺失。）",
        )

    def test_no_markers_uses_chinese_missing_notice(self):
        self.assertEqual(
            build_bilingual_display("日语1。日语2。"),
            "（中文字幕暂时缺失。）",
        )

    def test_grouped_multi_sentence_translation_shows_pure_chinese(self):
        # The model groups several Japanese sentences under ONE ⟦中文⟧ (real prod
        # output). The whole run must render as the Chinese -- NOT leading-Japanese
        # + Chinese (the mixed-subtitle bug this file's real-frame case exposed).
        self.assertEqual(
            build_bilingual_display(
                "ふぅん……麦。こんな時間に珍しいわね。⟦哼……麦。这个时间来还真少见呢。⟧"
            ),
            "哼……麦。这个时间来还真少见呢。",
        )

    def test_grouped_then_untranslated_tail(self):
        # A translated run followed by a sentence the model left untranslated.
        self.assertEqual(
            build_bilingual_display("あ。い。⟦啊。以。⟧う。"),
            "啊。以。（中文字幕暂时缺失。）",
        )

    def test_unclosed_translation_tail(self):
        self.assertEqual(build_bilingual_display("日语1。⟦中文1"), "中文1")

    def test_all_translation_only(self):
        self.assertEqual(build_bilingual_display("⟦只有中文。⟧"), "只有中文。")

    def test_comma_is_not_a_sentence_boundary(self):
        self.assertEqual(build_bilingual_display("おはよう、麦。⟦早上好，麦。⟧"), "早上好，麦。")

    def test_generated_malformed_subtitles_never_emit_japanese_script(self):
        rng = random.Random(20260711)
        spoken = ["おはよう。", "どうする？", "古い仮名𛀀。", "ｶﾀｶﾅ。", "只有中文。"]
        translations = [
            "早上好。",
            "怎么办？",
            "",
            "おはよう。",
            "中文𛀀。",
            "中文ｶﾅ。",
            "中文〱。",  # VERTICAL KANA REPEAT MARK (outside kana letter blocks)
        ]

        for _ in range(1000):
            left = rng.choice(spoken)
            right = rng.choice(spoken)
            translation = rng.choice(translations)
            shape = rng.randrange(6)
            if shape == 0:
                source = left
            elif shape == 1:
                source = f"{left}⟦{translation}⟧"
            elif shape == 2:
                source = f"{left}⟦{translation}⟧{right}"
            elif shape == 3:
                source = f"⟦{translation}⟧"
            elif shape == 4:
                source = f"⟦{translation}⟧{right}"
            else:
                source = f"⟦{translation}⟧{right}⟧"

            display = build_bilingual_display(source)
            leaked = [
                char
                for char in display
                if any(
                    marker in unicodedata.name(char, "")
                    for marker in ("HIRAGANA", "KATAKANA", "KANA")
                )
            ]
            self.assertEqual(leaked, [], msg=f"source={source!r}, display={display!r}")

            raw_spoken, _ = split_dialog_translation(source)
            paired_spoken = spoken_channel_is_paired(source)
            trusted_spoken = spoken_channel_or_fallback(
                raw_spoken,
                paired_subtitle=paired_spoken,
            )
            has_japanese_script = any(
                any(
                    marker in unicodedata.name(char, "")
                    for marker in ("HIRAGANA", "KATAKANA", "KANA")
                )
                for char in trusted_spoken
            )
            if not paired_spoken:
                self.assertTrue(
                    has_japanese_script,
                    msg=f"source={source!r}, spoken={trusted_spoken!r}",
                )


class BilingualPromptHardeningTests(unittest.TestCase):
    def test_zh_prompt_forbids_japanese_inside_translation_channel(self):
        prompt = build_spica_prompt(
            user_input="こんにちは",
            recent_context=[],
            long_term_memories=[],
            character_profile="profile",
            dialog_display_language="zh",
        )
        self.assertIn("⟦⟧ 内只允许使用中文", prompt)
        self.assertIn("不得保留日语假名或未翻译的日文原句", prompt)
        self.assertTrue(prompt.rstrip().endswith(BILINGUAL_OUTPUT_REMINDER))

    def test_zh_json_example_shows_the_bilingual_shape(self):
        prompt = build_system_prompt("麦", dialog_display_language="zh")
        self.assertIn("日语台词。⟦中文翻译。⟧", prompt)          # the JSON answer example
        self.assertNotIn('"answer": "日语回答文本"', prompt)     # pure-JP example is gone

    def test_ja_json_example_is_unchanged(self):
        prompt = build_system_prompt("麦")
        self.assertIn('"answer": "日语回答文本"', prompt)
        self.assertNotIn("⟦中文翻译。⟧", prompt)

    def test_full_prompt_reminder_is_the_last_block_in_zh(self):
        prompt = build_spica_prompt(
            user_input="こんにちは",
            recent_context=[],
            long_term_memories=[],
            character_profile="profile",
            dialog_display_language="zh",
        )
        self.assertIn("[OUTPUT_FORMAT_REMINDER]", prompt)
        # The reminder is a real recency anchor: it sits AFTER the user input.
        self.assertLess(
            prompt.index("[CURRENT_USER_INPUT]"), prompt.index("[OUTPUT_FORMAT_REMINDER]")
        )
        self.assertTrue(prompt.rstrip().endswith(BILINGUAL_OUTPUT_REMINDER))

    def test_ja_full_prompt_has_no_reminder(self):
        prompt = build_spica_prompt(
            user_input="こんにちは",
            recent_context=[],
            long_term_memories=[],
            character_profile="profile",
        )
        self.assertNotIn("[OUTPUT_FORMAT_REMINDER]", prompt)

    def test_reminder_helper_gates_on_language(self):
        self.assertEqual(bilingual_output_reminder("zh"), BILINGUAL_OUTPUT_REMINDER)
        self.assertEqual(bilingual_output_reminder("ja"), "")
        self.assertEqual(bilingual_output_reminder(), "")


class BilingualToolFollowupTests(unittest.TestCase):
    """The tool-followup prompt (streaming production chain) must re-anchor the
    bilingual format LAST in zh mode, and stay byte-identical in ja mode."""

    def test_zh_followup_reanchors_bilingual_format_after_tool_sections(self):
        prompt = build_tool_followup_prompt("[SYSTEM] ...", [], dialog_display_language="zh")
        self.assertIn("[OUTPUT_FORMAT_REMINDER]", prompt)
        self.assertLess(prompt.index("[NEXT_STEP]"), prompt.index("[OUTPUT_FORMAT_REMINDER]"))
        self.assertTrue(prompt.rstrip().endswith(BILINGUAL_OUTPUT_REMINDER))

    def test_ja_followup_is_unchanged(self):
        prompt = build_tool_followup_prompt("[SYSTEM] ...", [])
        self.assertNotIn("[OUTPUT_FORMAT_REMINDER]", prompt)
        self.assertTrue(prompt.rstrip().endswith("不要解释工具链。"))


if __name__ == "__main__":
    unittest.main()

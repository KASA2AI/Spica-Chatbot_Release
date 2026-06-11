"""Phase 3 unit tests for the gated galgame-context stage.

Covers: gate three-state injection (active 5-6 sections -- [RECENT_GAME_SUMMARIES]
joins when summaries exist, stage 2 / offline 4 / none 0), the `none` branch as a
byte-level no-op (red line), empty-section omission, the §27① fallback being
really consumed by retrieve_long_term_memory_node (a/b/c), the active-summaries
limit + the buffer/summaries no-overlap wall (stage 2), and the gate never
touching the LLM.
"""

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from memory.store import SQLiteMemoryStore
from spica.adapters.game_memory.sqlite import GameMemorySqliteAdapter
from spica.adapters.memory.sqlite import SqliteMemoryAdapter, scoped_conversation_id
from spica.config.schema import AppConfig, CharacterConfig
from spica.galgame.manual import ManualGameMemory
from spica.galgame.models import (
    CharacterRelation,
    GameProfile,
    StorySummary,
    game_conversation_id,
    utc_now_iso,
)
from spica.runtime.context import GameContextRequest, PromptBundle, TurnContext, TurnRequest
from spica.runtime.deps import TurnDeps
from spica.runtime.observer import DefaultTurnObserver
from spica.runtime.stages import retrieve_game_context_node, retrieve_long_term_memory_node
from spica.runtime.tools import RegistryToolSet

BASE_PROMPT = "[CURRENT_USER_INPUT]\n刚才发生了什么"


class _ExplodingLLM:
    """Any attribute access fails -- proves the gate never touches the LLM."""

    def __getattr__(self, name):
        raise AssertionError(f"gate must not touch the LLM (accessed {name!r})")


def _ctx(request: TurnRequest, prompt: str = BASE_PROMPT) -> TurnContext:
    ctx = TurnContext(request)
    ctx.prompt = PromptBundle(prompt_input=prompt)
    return ctx


def _deps(ctx, *, game_memory=None, memory=None, llm=None, character_id="spica", user_id="麦"):
    return TurnDeps(
        config=AppConfig(character=CharacterConfig(character_id=character_id, interlocutor_name=user_id)),
        llm=llm,
        tts=None,
        visual=None,
        memory=memory,
        tools=RegistryToolSet.from_function_table([], {}),
        game_memory=game_memory,
        observer=DefaultTurnObserver(ctx.timing),
    )


def _feed_all(gm: GameMemorySqliteAdapter) -> None:
    facade = ManualGameMemory(gm, character_id="spica", user_id="麦")
    facade.manual_set_progress_state("ABC", chapter={"title": "第一章", "confidence": 0.7}, current_scene_summary="教室")
    facade.manual_add_story_line("ABC", "朱比華", "こんにちは")  # committed, unsummarized -> buffer
    facade.manual_add_choice_event(
        "ABC", options=[{"index": 1, "text": "原谅她"}, {"index": 2, "text": "离开"}], selected_option=2
    )
    facade.manual_add_companion_beat("ABC", "reaction", "我就知道这个人有问题")
    # CharacterRelation has no manual_* facade method -> write via the adapter.
    gm.upsert_character_relation(
        CharacterRelation(
            relation_id="R1", game_id="ABC", character_a="朱比華", character_b="麦",
            relation_summary="青梅竹马", updated_at=utc_now_iso(),
        )
    )


class NoneBranchTest(unittest.TestCase):
    def test_none_branch_is_byte_level_noop(self):
        with TemporaryDirectory() as tmp:
            gm = GameMemorySqliteAdapter(Path(tmp) / "g.sqlite3")
            # populate the store: even with data present, a chat turn injects nothing.
            ManualGameMemory(gm, character_id="spica", user_id="麦").manual_set_progress_state(
                "ABC", current_scene_summary="教室"
            )
            ctx = _ctx(TurnRequest(user_input="hi", conversation_id="default"))  # mode == none
            deps = _deps(ctx, game_memory=gm)
            before_prompt = ctx.prompt.prompt_input
            before_timing = dict(ctx.timing)
            before_metadata = dict(ctx.metadata)

            out = retrieve_game_context_node(ctx, None, deps)

            self.assertIs(out, ctx)
            self.assertEqual(ctx.prompt.prompt_input, before_prompt)  # prompt untouched
            self.assertEqual(ctx.timing, before_timing)  # NO span opened
            self.assertNotIn("retrieve_game_context_node_ms", ctx.timing)
            self.assertEqual(ctx.metadata, before_metadata)

    def test_none_branch_short_circuits_on_prior_error(self):
        # interaction_mode galgame but ctx.error set -> still returns ctx untouched.
        ctx = _ctx(TurnRequest(user_input="x", interaction_mode="galgame", conversation_id="default"))
        from spica.runtime.context import TurnError

        ctx.error = TurnError("BOOM", "prior failure")
        with TemporaryDirectory() as tmp:
            deps = _deps(ctx, game_memory=GameMemorySqliteAdapter(Path(tmp) / "g.sqlite3"))
            before = ctx.prompt.prompt_input
            retrieve_game_context_node(ctx, None, deps)
            self.assertEqual(ctx.prompt.prompt_input, before)
            self.assertNotIn("retrieve_game_context_node_ms", ctx.timing)


class ActiveModeTest(unittest.TestCase):
    def test_active_injects_all_five_sections_via_each_trigger(self):
        triggers = {
            "interaction_mode": TurnRequest(
                user_input="刚才?", conversation_id="default", interaction_mode="galgame",
                game_context_request=GameContextRequest(mode="active", game_id="ABC"),
            ),
            "conversation_namespace": TurnRequest(
                user_input="刚才?", conversation_id=game_conversation_id("ABC")
            ),
            "gcr_mode_active": TurnRequest(
                user_input="刚才?", conversation_id="default",
                game_context_request=GameContextRequest(mode="active", game_id="ABC"),
            ),
        }
        for label, req in triggers.items():
            with self.subTest(trigger=label), TemporaryDirectory() as tmp:
                gm = GameMemorySqliteAdapter(Path(tmp) / "g.sqlite3")
                _feed_all(gm)
                ctx = _ctx(req)
                retrieve_game_context_node(ctx, None, _deps(ctx, game_memory=gm))
                prompt = ctx.prompt.prompt_input
                for header in (
                    "[GAME_PROGRESS]", "[CURRENT_GAME_BUFFER]", "[GAME_RELATIONS]",
                    "[GAME_CHOICES]", "[COMPANION_CONTEXT]",
                ):
                    self.assertIn(header, prompt)
                self.assertTrue(prompt.startswith(BASE_PROMPT))  # appended after the base
                self.assertIn("retrieve_game_context_node_ms", ctx.timing)  # span opened for active

    def test_empty_sections_are_omitted(self):
        with TemporaryDirectory() as tmp:
            gm = GameMemorySqliteAdapter(Path(tmp) / "g.sqlite3")
            ManualGameMemory(gm, character_id="spica", user_id="麦").manual_set_progress_state(
                "ABC", current_scene_summary="教室"
            )  # only progress, nothing else
            req = TurnRequest(
                user_input="x", interaction_mode="galgame", conversation_id="default",
                game_context_request=GameContextRequest(mode="active", game_id="ABC"),
            )
            ctx = _ctx(req)
            retrieve_game_context_node(ctx, None, _deps(ctx, game_memory=gm))
            prompt = ctx.prompt.prompt_input
            self.assertIn("[GAME_PROGRESS]", prompt)
            for header in (
                "[RECENT_GAME_SUMMARIES]",  # stage 2: omitted too when no summaries exist
                "[CURRENT_GAME_BUFFER]", "[GAME_RELATIONS]", "[GAME_CHOICES]", "[COMPANION_CONTEXT]",
            ):
                self.assertNotIn(header, prompt)

    def test_active_with_no_data_leaves_prompt_unchanged(self):
        with TemporaryDirectory() as tmp:
            gm = GameMemorySqliteAdapter(Path(tmp) / "g.sqlite3")  # empty store
            req = TurnRequest(
                user_input="x", interaction_mode="galgame", conversation_id="default",
                game_context_request=GameContextRequest(mode="active", game_id="ABC"),
            )
            ctx = _ctx(req)
            retrieve_game_context_node(ctx, None, _deps(ctx, game_memory=gm))
            self.assertEqual(ctx.prompt.prompt_input, BASE_PROMPT)  # nothing appended


class ActiveSummariesInjectionTest(unittest.TestCase):
    """Stage 2: active mode also injects [RECENT_GAME_SUMMARIES] (limit 2), so
    story summarized OUT of the buffer stays visible to a companion turn."""

    @staticmethod
    def _active_req():
        return TurnRequest(
            user_input="刚才?", conversation_id="default", interaction_mode="galgame",
            game_context_request=GameContextRequest(mode="active", game_id="ABC"),
        )

    @staticmethod
    def _section_json(prompt: str, header: str):
        # Sections are appended as "\n\n<header>\n<single-line-json>" blocks --
        # parse the JSON; substring assertions on the whole prompt would
        # false-positive (e.g. the placeholder summary_zh contains line texts).
        chunk = prompt.split(header + "\n", 1)[1]
        return json.loads(chunk.split("\n\n", 1)[0])

    def test_active_injects_recent_summaries_when_present(self):
        with TemporaryDirectory() as tmp:
            gm = GameMemorySqliteAdapter(Path(tmp) / "g.sqlite3")
            facade = ManualGameMemory(gm, character_id="spica", user_id="麦")
            facade.manual_add_story_line("ABC", "朱比華", "第一句")
            facade.manual_flush_summary("ABC")
            ctx = _ctx(self._active_req())
            retrieve_game_context_node(ctx, None, _deps(ctx, game_memory=gm))
            prompt = ctx.prompt.prompt_input
            self.assertIn("[RECENT_GAME_SUMMARIES]", prompt)
            items = self._section_json(prompt, "[RECENT_GAME_SUMMARIES]")
            self.assertEqual(len(items), 1)
            self.assertIn("第一句", items[0]["summary_zh"])  # placeholder carries the text

    def test_active_summary_limit_is_two(self):
        with TemporaryDirectory() as tmp:
            gm = GameMemorySqliteAdapter(Path(tmp) / "g.sqlite3")
            # Explicit created_at stamps: recent_summaries orders by created_at DESC
            # and same-second rows tiebreak on the RANDOM uuid summary_id -- the
            # "newest two" assertion is only deterministic with controlled stamps.
            for stamp, text in (
                ("2026-06-10T10:00:00", "最旧"),
                ("2026-06-10T10:00:01", "次新"),
                ("2026-06-10T10:00:02", "最新"),
            ):
                gm.add_summary(StorySummary(
                    summary_id=f"S{stamp[-2:]}", game_id="ABC", summary_zh=text,
                    created_at=stamp, updated_at=stamp,
                ))
            ctx = _ctx(self._active_req())
            retrieve_game_context_node(ctx, None, _deps(ctx, game_memory=gm))
            items = self._section_json(ctx.prompt.prompt_input, "[RECENT_GAME_SUMMARIES]")
            self.assertEqual([i["summary_zh"] for i in items], ["最新", "次新"])  # exactly 2, newest first
            # Regression pin: offline keeps its limit of 5 -> all three visible.
            ctx_off = _ctx(TurnRequest(
                user_input="昨天玩到哪", conversation_id="default",
                game_context_request=GameContextRequest(mode="offline", game_id="ABC"),
            ))
            retrieve_game_context_node(ctx_off, None, _deps(ctx_off, game_memory=gm))
            items_off = self._section_json(ctx_off.prompt.prompt_input, "[RECENT_GAME_SUMMARIES]")
            self.assertEqual(len(items_off), 3)

    def test_buffer_and_summaries_never_overlap(self):
        # The no-double-injection wall: lines covered by an injected summary's
        # source_line_ids must NEVER also appear in [CURRENT_GAME_BUFFER] -- guards
        # future buffer-advance semantic changes against double injection.
        with TemporaryDirectory() as tmp:
            gm = GameMemorySqliteAdapter(Path(tmp) / "g.sqlite3")
            facade = ManualGameMemory(gm, character_id="spica", user_id="麦")
            facade.manual_add_story_line("ABC", "A", "L1")
            facade.manual_add_story_line("ABC", "A", "L2")
            facade.manual_flush_summary("ABC")  # the summary covers L1+L2
            facade.manual_add_story_line("ABC", "B", "L3")
            facade.manual_add_story_line("ABC", "B", "L4")
            ctx = _ctx(self._active_req())
            retrieve_game_context_node(ctx, None, _deps(ctx, game_memory=gm))
            prompt = ctx.prompt.prompt_input
            buffer_texts = {item["text"] for item in self._section_json(prompt, "[CURRENT_GAME_BUFFER]")}
            self.assertEqual(buffer_texts, {"L3", "L4"})
            # Resolve the DISPLAYED summaries' source_line_ids back to texts via the
            # adapter (the injected JSON does not carry the ids itself).
            displayed_zh = {item["summary_zh"] for item in self._section_json(prompt, "[RECENT_GAME_SUMMARIES]")}
            by_id = {line.line_id: line.text for line in gm.committed_story_lines("ABC")}
            summarized_texts = set()
            for summary in gm.recent_summaries("ABC", limit=10):
                if summary.summary_zh in displayed_zh:
                    summarized_texts |= {by_id[i] for i in summary.source_line_ids}
            self.assertEqual(summarized_texts, {"L1", "L2"})
            self.assertFalse(buffer_texts & summarized_texts)


class OfflineModeTest(unittest.TestCase):
    def test_offline_injects_four_sections_no_buffer_no_companion(self):
        with TemporaryDirectory() as tmp:
            gm = GameMemorySqliteAdapter(Path(tmp) / "g.sqlite3")
            _feed_all(gm)
            ManualGameMemory(gm, character_id="spica", user_id="麦").manual_flush_summary("ABC")
            req = TurnRequest(
                user_input="昨天玩到哪了", conversation_id="default", command_intent="ask_last_progress",
                game_context_request=GameContextRequest(mode="offline", game_id="ABC"),
            )
            ctx = _ctx(req)
            retrieve_game_context_node(ctx, None, _deps(ctx, game_memory=gm))
            prompt = ctx.prompt.prompt_input
            for header in ("[GAME_PROGRESS]", "[RECENT_GAME_SUMMARIES]", "[GAME_RELATIONS]", "[GAME_CHOICES]"):
                self.assertIn(header, prompt)
            self.assertNotIn("[CURRENT_GAME_BUFFER]", prompt)
            self.assertNotIn("[COMPANION_CONTEXT]", prompt)

    def test_offline_companion_only_with_ask_companion_memory(self):
        with TemporaryDirectory() as tmp:
            gm = GameMemorySqliteAdapter(Path(tmp) / "g.sqlite3")
            facade = ManualGameMemory(gm, character_id="spica", user_id="麦")
            facade.manual_set_progress_state("ABC", current_scene_summary="教室")
            facade.manual_add_companion_beat("ABC", "reaction", "我就知道")
            # offline but not asking about shared memory -> no companion section
            ctx_no = _ctx(TurnRequest(
                user_input="x", conversation_id="default",
                game_context_request=GameContextRequest(mode="offline", game_id="ABC"),
            ))
            retrieve_game_context_node(ctx_no, None, _deps(ctx_no, game_memory=gm))
            self.assertNotIn("[COMPANION_CONTEXT]", ctx_no.prompt.prompt_input)
            # explicitly asking about shared memory -> companion section appears
            ctx_yes = _ctx(TurnRequest(
                user_input="我们之前玩这个说过啥", conversation_id="default",
                command_intent="ask_companion_memory",
                game_context_request=GameContextRequest(mode="offline", game_id="ABC"),
            ))
            retrieve_game_context_node(ctx_yes, None, _deps(ctx_yes, game_memory=gm))
            self.assertIn("[COMPANION_CONTEXT]", ctx_yes.prompt.prompt_input)

    def test_offline_game_id_falls_back_to_last_played(self):
        with TemporaryDirectory() as tmp:
            gm = GameMemorySqliteAdapter(Path(tmp) / "g.sqlite3")
            ManualGameMemory(gm, character_id="spica", user_id="麦").manual_set_progress_state(
                "ABC", current_scene_summary="教室"
            )
            gm.upsert_game_profile(GameProfile(
                game_id="ABC", display_name="G", created_at=utc_now_iso(),
                updated_at=utc_now_iso(), last_played_at=utc_now_iso(),
            ))
            # offline via command_intent, NO game_id / gcr -> resolves last_played_game()
            req = TurnRequest(user_input="昨天玩到哪了", conversation_id="default", command_intent="ask_last_progress")
            ctx = _ctx(req)
            retrieve_game_context_node(ctx, None, _deps(ctx, game_memory=gm))
            self.assertIn("[GAME_PROGRESS]", ctx.prompt.prompt_input)


class GateNeverCallsLLMTest(unittest.TestCase):
    def test_gate_never_touches_llm(self):
        with TemporaryDirectory() as tmp:
            gm = GameMemorySqliteAdapter(Path(tmp) / "g.sqlite3")
            ManualGameMemory(gm, character_id="spica", user_id="麦").manual_set_progress_state(
                "ABC", current_scene_summary="教室"
            )
            req = TurnRequest(
                user_input="x", interaction_mode="galgame", conversation_id="default",
                game_context_request=GameContextRequest(mode="active", game_id="ABC"),
            )
            ctx = _ctx(req)
            deps = _deps(ctx, game_memory=gm, llm=_ExplodingLLM())
            retrieve_game_context_node(ctx, None, deps)  # must not raise
            self.assertIn("[GAME_PROGRESS]", ctx.prompt.prompt_input)


class MemoryConversationIdConsumptionTest(unittest.TestCase):
    """§27① welded: retrieve_long_term_memory_node really uses
    effective_memory_conversation_id, not the raw conversation_id."""

    def _run_long_term(self, store, request):
        ctx = TurnContext(request)
        deps = _deps(ctx, memory=SqliteMemoryAdapter(store))
        retrieve_long_term_memory_node(ctx, None, deps)
        return [m["content"] for m in (ctx.recent.long_term_memories if ctx.recent else [])]

    def test_a_b_c(self):
        with TemporaryDirectory() as tmp:
            store = SQLiteMemoryStore(Path(tmp) / "m.sqlite3")
            # Spica's long-term character memory lives under "<char_id>::default".
            store.add_memory(
                scoped_conversation_id("spica", "default"),
                scope="user", content="麦 喜欢慢慢看剧情", importance=0.9,
            )
            galgame_cid = game_conversation_id("ABC")

            # (c) plain chat turn: no memory_conversation_id -> effective == "default" -> HIT
            self.assertIn(
                "麦 喜欢慢慢看剧情",
                self._run_long_term(store, TurnRequest(user_input="剧情", conversation_id="default")),
            )
            # (b) galgame turn WITHOUT decoupling: effective == galgame cid -> MISS
            #     (this is exactly the §27① bug if we had used the raw conversation_id)
            self.assertNotIn(
                "麦 喜欢慢慢看剧情",
                self._run_long_term(store, TurnRequest(user_input="剧情", conversation_id=galgame_cid)),
            )
            # (a) galgame turn WITH memory_conversation_id="default" -> HIT
            self.assertIn(
                "麦 喜欢慢慢看剧情",
                self._run_long_term(
                    store,
                    TurnRequest(user_input="剧情", conversation_id=galgame_cid, memory_conversation_id="default"),
                ),
            )


if __name__ == "__main__":
    unittest.main()

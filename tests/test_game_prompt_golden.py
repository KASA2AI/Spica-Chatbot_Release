"""Phase 0 characterization: galgame prompt full-section golden (OO migration).

Freezes the EXACT prompt text ``retrieve_game_context_node`` injects for a fully
fed game memory, in both gate modes, so Phase 1 (prompt_sections move) and
Phase 3 (contributor seam) have a byte-level parity judge.

HARD RULES (migration plan Phase 0 #2):
- real ``GameMemorySqliteAdapter`` on a tmp path; models are written DIRECTLY --
  ``ManualGameMemory`` is BANNED here (it stamps ``utc_now_iso()`` into
  ``last_played_at`` / ``created_at``, which would render wall-clock time into
  the golden and make it flaky);
- every timestamp / created_at / updated_at / last_played_at is an explicit
  fixed value, mutually staggered, pinning each reader's ORDER BY;
- the expected prompts are LITERAL constants embedded below (generated once from
  this exact fixture, then hand-checked field by field against
  ``stages._format_*``); the test never re-derives them dynamically.

Fixture design notes (what each row pins):
- 3 summaries pin the active-mode limit (_GAME_CONTEXT_ACTIVE_SUMMARY_LIMIT = 2,
  newest first) vs offline's galgame.prompt_context_recent_limit = 5 (all 3);
- L001 is covered by S01.source_line_ids -> excluded from [CURRENT_GAME_BUFFER]
  (buffer = committed AND unsummarized);
- L003 has speaker=None -> its buffer entry omits the "speaker" key;
- LP01 is PENDING_CURRENT under the live session id -> [CURRENT_LINE], active only;
- B03 carries meta={"silent": True} -> excluded from [COMPANION_CONTEXT] (P5);
- offline without a companion command_intent has NO [COMPANION_CONTEXT] -- expected.
"""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from spica.adapters.game_memory.sqlite import GameMemorySqliteAdapter
from spica.config.schema import AppConfig, CharacterConfig, GalgameConfig
from spica.galgame.models import (
    CharacterRelation,
    ChoiceEvent,
    CompanionBeat,
    GameProgressState,
    StoryLine,
    StoryLineStatus,
    StorySummary,
)
from spica.runtime.context import GameContextRequest, PromptBundle, TurnContext, TurnRequest
from spica.runtime.deps import TurnDeps
from spica.runtime.observer import DefaultTurnObserver
from spica.runtime.stages import retrieve_game_context_node
from spica.runtime.tools import RegistryToolSet

GAME = "ABC"
LIVE_SESSION = "S-LIVE"

ACTIVE_BASE_PROMPT = "[CURRENT_USER_INPUT]\n刚才发生了什么"
OFFLINE_BASE_PROMPT = "[CURRENT_USER_INPUT]\n昨天玩到哪了"

# -- frozen goldens (literal constants; see module docstring) ------------------

ACTIVE_GOLDEN = """[CURRENT_USER_INPUT]
刚才发生了什么

[GAME_PROGRESS]
{"chapter": {"title": "第一章", "confidence": 0.7}, "route": {"confirmed": true, "name": "朱比華", "confidence": 0.8}, "location": "教室", "current_scene_summary": "放学后的文化祭准备", "major_events": ["初次见面"], "unresolved_threads": ["屋顶的约定"], "last_played_at": "2026-01-01T12:00:00"}

[RECENT_GAME_SUMMARIES]
[{"summary_zh": "放学：社团活动的邀请", "characters": ["朱比華", "部长"], "major_events": [], "unresolved_threads": [], "created_at": "2026-01-01T10:00:03"}, {"summary_zh": "午休：屋顶上的闲聊", "characters": ["朱比華"], "major_events": [], "unresolved_threads": [], "created_at": "2026-01-01T10:00:02"}]

[CURRENT_GAME_BUFFER]
[{"speaker": "朱比華", "text": "今日は文化祭の準備だね"}, {"text": "……ちょっと緊張してきた"}]

[CURRENT_LINE]
[{"speaker": "朱比華", "text": "麦、こっちに来て"}]

[GAME_RELATIONS]
[{"character_a": "朱比華", "character_b": "麦", "relation_summary": "青梅竹马", "confidence": 0.9}, {"character_a": "朱比華", "character_b": "部长", "relation_summary": "社团前辈", "confidence": 0.6}]

[GAME_CHOICES]
[{"options": [{"index": 1, "text": "去屋顶"}, {"index": 2, "text": "回教室"}], "selected_option_index": 2, "selected_option_text": "回教室", "selection_source": "inferred"}, {"options": [{"index": 1, "text": "帮她"}, {"index": 2, "text": "旁观"}], "selected_option_index": 1, "selected_option_text": "帮她", "selection_source": "user_reported"}]

[COMPANION_CONTEXT]
[{"type": "shared_observation", "content": "麦说想先走朱比華线", "source": "user"}, {"type": "reaction", "content": "这个部长绝对有问题", "source": "spica"}]"""

OFFLINE_GOLDEN = """[CURRENT_USER_INPUT]
昨天玩到哪了

[GAME_PROGRESS]
{"chapter": {"title": "第一章", "confidence": 0.7}, "route": {"confirmed": true, "name": "朱比華", "confidence": 0.8}, "location": "教室", "current_scene_summary": "放学后的文化祭准备", "major_events": ["初次见面"], "unresolved_threads": ["屋顶的约定"], "last_played_at": "2026-01-01T12:00:00"}

[RECENT_GAME_SUMMARIES]
[{"summary_zh": "放学：社团活动的邀请", "characters": ["朱比華", "部长"], "major_events": [], "unresolved_threads": [], "created_at": "2026-01-01T10:00:03"}, {"summary_zh": "午休：屋顶上的闲聊", "characters": ["朱比華"], "major_events": [], "unresolved_threads": [], "created_at": "2026-01-01T10:00:02"}, {"summary_zh": "序章：两人在教室重逢", "characters": ["朱比華", "麦"], "major_events": ["重逢"], "unresolved_threads": ["约定"], "created_at": "2026-01-01T10:00:01"}]

[GAME_RELATIONS]
[{"character_a": "朱比華", "character_b": "麦", "relation_summary": "青梅竹马", "confidence": 0.9}, {"character_a": "朱比華", "character_b": "部长", "relation_summary": "社团前辈", "confidence": 0.6}]

[GAME_CHOICES]
[{"options": [{"index": 1, "text": "去屋顶"}, {"index": 2, "text": "回教室"}], "selected_option_index": 2, "selected_option_text": "回教室", "selection_source": "inferred"}, {"options": [{"index": 1, "text": "帮她"}, {"index": 2, "text": "旁观"}], "selected_option_index": 1, "selected_option_text": "帮她", "selection_source": "user_reported"}]"""


def _feed(gm: GameMemorySqliteAdapter) -> None:
    """Direct model writes only -- every stamp explicit, fixed, staggered."""
    gm.upsert_progress_state(GameProgressState(
        game_id=GAME,
        last_played_at="2026-01-01T12:00:00",
        chapter={"title": "第一章", "confidence": 0.7},
        route={"confirmed": True, "name": "朱比華", "confidence": 0.8},
        location="教室",
        current_scene_summary="放学后的文化祭准备",
        major_events=["初次见面"],
        unresolved_threads=["屋顶的约定"],
    ))
    # 3 summaries, created_at staggered (recent_summaries orders created_at DESC).
    gm.add_summary(StorySummary(
        summary_id="S01", game_id=GAME, session_id="S-OLD",
        source_line_ids=["L001"], summary_zh="序章：两人在教室重逢",
        characters=["朱比華", "麦"], major_events=["重逢"],
        unresolved_threads=["约定"],
        created_at="2026-01-01T10:00:01", updated_at="2026-01-01T10:00:01",
    ))
    gm.add_summary(StorySummary(
        summary_id="S02", game_id=GAME, session_id="S-OLD",
        summary_zh="午休：屋顶上的闲聊", characters=["朱比華"],
        created_at="2026-01-01T10:00:02", updated_at="2026-01-01T10:00:02",
    ))
    gm.add_summary(StorySummary(
        summary_id="S03", game_id=GAME, session_id=LIVE_SESSION,
        summary_zh="放学：社团活动的邀请", characters=["朱比華", "部长"],
        created_at="2026-01-01T10:00:03", updated_at="2026-01-01T10:00:03",
    ))
    # Committed lines, timestamp staggered (committed_story_lines orders ASC).
    # L001 is summarized by S01 -> buffer excludes it.
    gm.add_story_line(StoryLine(
        line_id="L001", session_id="S-OLD", game_id=GAME, text="こんにちは",
        timestamp="2026-01-01T10:01:01", speaker="朱比華",
        source="ocr", status=StoryLineStatus.COMMITTED,
    ))
    gm.add_story_line(StoryLine(
        line_id="L002", session_id=LIVE_SESSION, game_id=GAME,
        text="今日は文化祭の準備だね", timestamp="2026-01-01T10:01:02",
        speaker="朱比華", source="ocr", status=StoryLineStatus.COMMITTED,
    ))
    gm.add_story_line(StoryLine(
        line_id="L003", session_id=LIVE_SESSION, game_id=GAME,
        text="……ちょっと緊張してきた", timestamp="2026-01-01T10:01:03",
        speaker=None, source="ocr", status=StoryLineStatus.COMMITTED,
    ))
    # The pending on-screen line under the LIVE session -> [CURRENT_LINE].
    gm.add_story_line(StoryLine(
        line_id="LP01", session_id=LIVE_SESSION, game_id=GAME,
        text="麦、こっちに来て", timestamp="2026-01-01T10:02:00",
        speaker="朱比華", source="ocr", status=StoryLineStatus.PENDING_CURRENT,
    ))
    # Relations, updated_at staggered (character_relations orders DESC).
    gm.upsert_character_relation(CharacterRelation(
        relation_id="R01", game_id=GAME, character_a="朱比華", character_b="麦",
        relation_summary="青梅竹马", confidence=0.9, updated_at="2026-01-01T11:00:02",
    ))
    gm.upsert_character_relation(CharacterRelation(
        relation_id="R02", game_id=GAME, character_a="朱比華", character_b="部长",
        relation_summary="社团前辈", confidence=0.6, updated_at="2026-01-01T11:00:01",
    ))
    # Choices, timestamp staggered (recent_choice_events orders DESC).
    gm.add_choice_event(ChoiceEvent(
        choice_id="C01", game_id=GAME, session_id="S-OLD",
        timestamp="2026-01-01T11:10:01",
        options=[{"index": 1, "text": "帮她"}, {"index": 2, "text": "旁观"}],
        selected_option_index=1, selected_option_text="帮她",
        selection_source="user_reported",
    ))
    gm.add_choice_event(ChoiceEvent(
        choice_id="C02", game_id=GAME, session_id=LIVE_SESSION,
        timestamp="2026-01-01T11:10:02",
        options=[{"index": 1, "text": "去屋顶"}, {"index": 2, "text": "回教室"}],
        selected_option_index=2, selected_option_text="回教室",
        selection_source="inferred",
    ))
    # Beats, created_at staggered (DESC); B03 is silent -> filtered from prompt.
    scope = {"character_id": "spica", "user_id": "麦", "game_id": GAME}
    gm.add_companion_beat(CompanionBeat(
        beat_id="B01", game_id=GAME, session_id=LIVE_SESSION, type="reaction",
        content="这个部长绝对有问题", source="spica",
        created_at="2026-01-01T11:20:01", scope=scope,
    ))
    gm.add_companion_beat(CompanionBeat(
        beat_id="B02", game_id=GAME, session_id=LIVE_SESSION,
        type="shared_observation", content="麦说想先走朱比華线", source="user",
        created_at="2026-01-01T11:20:02", scope=scope,
    ))
    gm.add_companion_beat(CompanionBeat(
        beat_id="B03", game_id=GAME, session_id=LIVE_SESSION, type="reaction",
        content="（被吞掉的沉默吐槽）", source="spica",
        created_at="2026-01-01T11:20:03", scope=scope, meta={"silent": True},
    ))


def _active_request() -> TurnRequest:
    return TurnRequest(
        user_input="刚才发生了什么", conversation_id="default",
        interaction_mode="galgame",
        game_context_request=GameContextRequest(
            mode="active", game_id=GAME, session_id=LIVE_SESSION
        ),
    )


def _offline_request() -> TurnRequest:
    return TurnRequest(
        user_input="昨天玩到哪了", conversation_id="default",
        game_context_request=GameContextRequest(mode="offline", game_id=GAME),
    )


def _run_node(gm, request: TurnRequest, base_prompt: str) -> TurnContext:
    ctx = TurnContext(request)
    ctx.prompt = PromptBundle(prompt_input=base_prompt)
    deps = TurnDeps(
        config=AppConfig(
            character=CharacterConfig(character_id="spica", interlocutor_name="麦"),
            galgame=GalgameConfig(),
        ),
        llm=None, tts=None, visual=None, memory=None,
        tools=RegistryToolSet.from_function_table([], {}),
        game_memory=gm,
        observer=DefaultTurnObserver(ctx.timing),
    )
    retrieve_game_context_node(ctx, None, deps)
    return ctx


class GamePromptGoldenTest(unittest.TestCase):
    def test_active_golden(self):
        with TemporaryDirectory() as tmp:
            gm = GameMemorySqliteAdapter(Path(tmp) / "g.sqlite3")
            _feed(gm)
            ctx = _run_node(gm, _active_request(), ACTIVE_BASE_PROMPT)
            self.assertEqual(ctx.prompt.prompt_input, ACTIVE_GOLDEN)

    def test_offline_golden(self):
        # Offline without a companion command_intent: [COMPANION_CONTEXT] absent
        # is the expected shape (see _should_inject_companion).
        with TemporaryDirectory() as tmp:
            gm = GameMemorySqliteAdapter(Path(tmp) / "g.sqlite3")
            _feed(gm)
            ctx = _run_node(gm, _offline_request(), OFFLINE_BASE_PROMPT)
            self.assertEqual(ctx.prompt.prompt_input, OFFLINE_GOLDEN)

    def test_same_input_twice_is_byte_identical(self):
        # Determinism pin: two independent runs over the same store must produce
        # byte-identical prompts (both also equal to the frozen golden).
        with TemporaryDirectory() as tmp:
            gm = GameMemorySqliteAdapter(Path(tmp) / "g.sqlite3")
            _feed(gm)
            first = _run_node(gm, _active_request(), ACTIVE_BASE_PROMPT).prompt.prompt_input
            second = _run_node(gm, _active_request(), ACTIVE_BASE_PROMPT).prompt.prompt_input
            self.assertEqual(first, second)
            self.assertEqual(first, ACTIVE_GOLDEN)

    def test_active_without_game_memory_opens_span_but_leaves_prompt(self):
        # Phase 3 span-semantics baseline: active mode + deps.game_memory=None
        # STILL opens the observer span (timing key present) while the prompt is
        # untouched. Phase 3's contributor node must keep this byte for byte.
        ctx = TurnContext(_active_request())
        ctx.prompt = PromptBundle(prompt_input=ACTIVE_BASE_PROMPT)
        deps = TurnDeps(
            config=AppConfig(
                character=CharacterConfig(character_id="spica", interlocutor_name="麦"),
                galgame=GalgameConfig(),
            ),
            llm=None, tts=None, visual=None, memory=None,
            tools=RegistryToolSet.from_function_table([], {}),
            game_memory=None,
            observer=DefaultTurnObserver(ctx.timing),
        )
        retrieve_game_context_node(ctx, None, deps)
        self.assertIn("retrieve_game_context_node_ms", ctx.timing)
        self.assertEqual(ctx.prompt.prompt_input, ACTIVE_BASE_PROMPT)


if __name__ == "__main__":
    unittest.main()

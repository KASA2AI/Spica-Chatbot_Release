"""Phase 1 unit tests for the galgame domain models (spica/galgame/models.py).

Locks: 12-model serialization round-trip, route_key materialized + default null,
StoryLine.status transition graph (legal + illegal), conversation_id format,
naive-UTC timestamp shape.
"""

import re
import unittest

from spica.galgame.models import (
    CharacterRelation,
    ChoiceEvent,
    CompanionBeat,
    GameProfile,
    GameProgressState,
    LaunchProfile,
    OCRProfile,
    OCRRegion,
    PlaySession,
    StoryLine,
    StoryLineStatus,
    StoryLineStatusError,
    StorySummary,
    WindowMatchRule,
    game_conversation_id,
    utc_now_iso,
)


def _full_models() -> list:
    """One populated instance of every model (non-default values where possible)."""
    return [
        LaunchProfile(
            platform="linux", launch_type="command", launch_target=None,
            command="bottles-cli run -b X -p game", working_dir="/tmp", enabled=True,
        ),
        WindowMatchRule(
            platform="linux", title_keywords=["シナリオ", "Game"],
            last_full_title="Game - シナリオ", process_name="game.exe",
            app_id="game", confirmed_once=True,
        ),
        OCRRegion(
            x_ratio=0.1, y_ratio=0.7, w_ratio=0.8, h_ratio=0.25,
            pixel_rect=[100, 700, 800, 250], window_size_at_calibration=[1280, 720],
            last_verified_at="2026-06-09T10:00:00",
        ),
        OCRProfile(
            languages=["ja", "zh"], dialog_text_region={"x_ratio": 0.1},
            speaker_name_region={"x_ratio": 0.1}, speaker_strategy="region",
            stability_required_count=2, interval_seconds=1.0,
            similarity_threshold=0.9, raw_cache_retention_days=7,
        ),
        GameProfile(
            game_id="ABC", display_name="Some Game", created_at="2026-06-09T10:00:00",
            updated_at="2026-06-09T10:00:00", aliases=["那个游戏", "SG"],
            last_played_at="2026-06-09T11:00:00", active_playthrough_id="default",
            launch_profiles={"linux": {"enabled": True}}, window_match={"title_keywords": ["X"]},
            ocr_profile={"languages": ["ja"]}, proactive_commentary={"enabled": False},
        ),
        PlaySession(
            session_id="S1", game_id="ABC", started_at="2026-06-09T10:00:00",
            playthrough_id="default", route_key=None, ended_at=None, state="active",
            ocr_line_count=12, summary_count=1,
        ),
        StoryLine(
            line_id="L1", session_id="S1", game_id="ABC", text="こんにちは",
            timestamp="2026-06-09T10:01:00", playthrough_id="default", speaker="朱比華",
            source="ocr", confidence=0.95, raw_hash="abc123", status=StoryLineStatus.COMMITTED,
        ),
        StorySummary(
            summary_id="SM1", game_id="ABC", playthrough_id="default", route_key=None,
            session_id="S1", source_line_ids=["L1", "L2"], summary_zh="开场。",
            key_original_lines=["こんにちは"], characters=["朱比華"], major_events=["相遇"],
            unresolved_threads=["谜团"], route_guess={"name": "A线", "confidence": 0.6},
            created_at="2026-06-09T10:05:00", updated_at="2026-06-09T10:05:00",
            source="auto_summary", revision=1,
        ),
        GameProgressState(
            game_id="ABC", playthrough_id="default", route_key=None,
            last_played_at="2026-06-09T11:00:00", chapter={"title": "第一章", "confidence": 0.7},
            route={"confirmed": False, "name": "A线", "confidence": 0.6, "evidence": ["e1"]},
            location="教室", current_scene_summary="在教室对话", major_events=["相遇"],
            unresolved_threads=["谜团"],
            last_ocr_anchor={"speaker": "朱比華", "text": "またね", "timestamp": "2026-06-09T11:00:00"},
        ),
        CharacterRelation(
            relation_id="R1", game_id="ABC", playthrough_id="default", character_a="朱比華",
            character_b="麦", relation_summary="青梅竹马", evidence=["e1"], confidence=0.8,
            updated_at="2026-06-09T11:00:00", source="auto_summary",
        ),
        ChoiceEvent(
            choice_id="C1", game_id="ABC", playthrough_id="default", session_id="S1",
            timestamp="2026-06-09T10:30:00",
            options=[{"index": 1, "text": "原谅她"}, {"index": 2, "text": "离开"}],
            selected_option_index=2, selected_option_text="离开",
            selection_source="user_reported", confidence=1.0, screen_analysis_summary="两个选项",
        ),
        CompanionBeat(
            beat_id="B1", game_id="ABC", playthrough_id="default", session_id="S1",
            type="reaction", content="我就知道这个人有问题", source="user",
            created_at="2026-06-09T10:31:00",
            scope={"character_id": "spica", "user_id": "麦", "game_id": "ABC"},
        ),
    ]


class SerializationRoundTripTest(unittest.TestCase):
    def test_all_twelve_models_round_trip(self):
        models = _full_models()
        self.assertEqual(len(models), 12)
        for model in models:
            with self.subTest(model=type(model).__name__):
                restored = type(model).from_dict(model.to_dict())
                self.assertEqual(restored, model)

    def test_to_dict_is_json_safe_plain_types(self):
        # status enum must serialize to its string value, not the enum object.
        line = StoryLine(
            line_id="L1", session_id="S1", game_id="ABC", text="x",
            timestamp="2026-06-09T10:01:00", status=StoryLineStatus.COMMITTED,
        )
        self.assertEqual(line.to_dict()["status"], "committed")
        self.assertIsInstance(line.to_dict()["status"], str)

    def test_from_dict_ignores_unknown_keys(self):
        data = GameProfile(
            game_id="ABC", display_name="G", created_at="t", updated_at="t"
        ).to_dict()
        data["a_v2_only_field"] = 123
        restored = GameProfile.from_dict(data)
        self.assertEqual(restored.game_id, "ABC")


class RouteKeyTest(unittest.TestCase):
    def test_route_key_materialized_and_defaults_null_on_three_models(self):
        self.assertIsNone(PlaySession(session_id="S", game_id="G", started_at="t").route_key)
        self.assertIsNone(StorySummary(summary_id="SM", game_id="G").route_key)
        self.assertIsNone(GameProgressState(game_id="G").route_key)

    def test_route_key_only_on_those_three(self):
        without = (StoryLine, CharacterRelation, ChoiceEvent, CompanionBeat, GameProfile)
        for cls in without:
            with self.subTest(model=cls.__name__):
                self.assertNotIn("route_key", {f for f in cls.__dataclass_fields__})


class StoryLineStatusTransitionTest(unittest.TestCase):
    def _line(self, status: StoryLineStatus) -> StoryLine:
        return StoryLine(line_id="L", session_id="S", game_id="G", text="t", timestamp="ts", status=status)

    def test_legal_transitions(self):
        legal = [
            (StoryLineStatus.PENDING_CURRENT, StoryLineStatus.COMMITTED),
            (StoryLineStatus.PENDING_CURRENT, StoryLineStatus.DISCARDED),
            (StoryLineStatus.COMMITTED, StoryLineStatus.DISCARDED),
        ]
        for src, dst in legal:
            with self.subTest(transition=f"{src.value}->{dst.value}"):
                line = self._line(src)
                advanced = line.with_status(dst)
                self.assertEqual(advanced.status, dst)
                # immutability: original is untouched
                self.assertEqual(line.status, src)

    def test_illegal_transitions_raise(self):
        illegal = [
            (StoryLineStatus.COMMITTED, StoryLineStatus.PENDING_CURRENT),
            (StoryLineStatus.DISCARDED, StoryLineStatus.PENDING_CURRENT),
            (StoryLineStatus.DISCARDED, StoryLineStatus.COMMITTED),
            # same -> same is not in the pinned graph -> illegal, not a silent no-op
            (StoryLineStatus.PENDING_CURRENT, StoryLineStatus.PENDING_CURRENT),
            (StoryLineStatus.COMMITTED, StoryLineStatus.COMMITTED),
            (StoryLineStatus.DISCARDED, StoryLineStatus.DISCARDED),
        ]
        for src, dst in illegal:
            with self.subTest(transition=f"{src.value}->{dst.value}"):
                with self.assertRaises(StoryLineStatusError):
                    self._line(src).with_status(dst)

    def test_with_status_accepts_string_value(self):
        line = self._line(StoryLineStatus.PENDING_CURRENT)
        self.assertEqual(line.with_status("committed").status, StoryLineStatus.COMMITTED)


class ConversationIdTest(unittest.TestCase):
    def test_format(self):
        self.assertEqual(
            game_conversation_id("ABC"), "galgame::ABC::playthrough::default"
        )
        self.assertEqual(
            game_conversation_id("ABC", "ng+"), "galgame::ABC::playthrough::ng+"
        )


class TimestampShapeTest(unittest.TestCase):
    def test_utc_now_iso_is_naive_second_precision(self):
        stamp = utc_now_iso()
        # naive (no tz suffix), second precision -> YYYY-MM-DDTHH:MM:SS
        self.assertRegex(stamp, r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$")
        self.assertNotIn("+", stamp)
        self.assertFalse(stamp.endswith("Z"))
        self.assertNotIn(".", stamp)  # no microseconds


if __name__ == "__main__":
    unittest.main()

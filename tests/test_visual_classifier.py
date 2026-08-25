import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_tools.visual import VisualDiffService


class VisualClassifierTests(unittest.TestCase):
    def make_service(self, *, director: bool = False):
        temp = tempfile.TemporaryDirectory()
        root = Path(temp.name)
        diff_root = root / "diffs"
        costume = diff_root / "school"
        for folder in ("normal", "arms_crossed", "index_finger"):
            folder_path = costume / folder
            folder_path.mkdir(parents=True, exist_ok=True)
            for expression_id in (
                "000", "001", "002", "003", "004", "007", "008", "009",
                "010", "011", "013", "016", "019", "026",
            ):
                (folder_path / f"spica_face001_{expression_id}.png").write_bytes(b"")

        rules_path = diff_root / "expression_hand_pose_rules.json"
        rules = {
                    "expressions": [
                        {
                            "id": "000",
                            "main_description": "neutral",
                            "keywords": ["中性", "疑問"],
                            "use_when": ["普通対話"],
                            "emotion_group": "neutral",
                            "emotion_subtype": "attentive",
                            "intensity": 1,
                            "recommended_hand_pose": "normal",
                            "compatible_hand_poses": ["normal", "arms_crossed", "index_finger"],
                            "avoid_hand_poses": [],
                        },
                        {
                            "id": "001",
                            "main_description": "serious",
                            "keywords": ["认真", "冷静"],
                            "use_when": ["正常说明"],
                            "emotion_group": "neutral",
                            "emotion_subtype": "serious",
                            "intensity": 2,
                            "recommended_hand_pose": "arms_crossed",
                            "compatible_hand_poses": ["arms_crossed", "normal", "index_finger"],
                            "avoid_hand_poses": [],
                        },
                        {
                            "id": "002",
                            "main_description": "soft smile",
                            "keywords": ["温柔", "安心"],
                            "use_when": ["安慰"],
                            "emotion_group": "joy",
                            "emotion_subtype": "soft_smile",
                            "intensity": 2,
                            "recommended_hand_pose": "normal",
                            "compatible_hand_poses": ["normal", "index_finger"],
                            "avoid_hand_poses": ["arms_crossed"],
                        },
                        {
                            "id": "003",
                            "main_description": "closed eye smile",
                            "keywords": ["开心", "感谢", "温柔"],
                            "use_when": ["表达感谢", "关系亲近"],
                            "emotion_group": "joy",
                            "emotion_subtype": "closed_eye_smile",
                            "intensity": 3,
                            "recommended_hand_pose": "normal",
                            "compatible_hand_poses": ["normal"],
                            "avoid_hand_poses": ["arms_crossed", "index_finger"],
                        },
                        {
                            "id": "004",
                            "main_description": "talking light",
                            "keywords": ["解释", "说明", "軽快"],
                            "use_when": ["正在讲述", "主动回应"],
                            "emotion_group": "joy",
                            "emotion_subtype": "talking_light",
                            "intensity": 3,
                            "recommended_hand_pose": "index_finger",
                            "compatible_hand_poses": ["index_finger", "normal"],
                            "avoid_hand_poses": [],
                        },
                        {
                            "id": "009",
                            "main_description": "surprise",
                            "keywords": ["惊讶", "疑惑"],
                            "use_when": ["听到意外信息"],
                            "emotion_group": "surprise",
                            "emotion_subtype": "mild",
                            "intensity": 2,
                            "recommended_hand_pose": "normal",
                            "compatible_hand_poses": ["normal"],
                            "avoid_hand_poses": ["arms_crossed"],
                        },
                        {
                            "id": "010",
                            "main_description": "sad",
                            "keywords": ["难过", "低落"],
                            "use_when": ["轻度悲伤"],
                            "emotion_group": "sad",
                            "emotion_subtype": "downcast",
                            "intensity": 3,
                            "recommended_hand_pose": "normal",
                            "compatible_hand_poses": ["normal"],
                            "avoid_hand_poses": ["index_finger"],
                        },
                        {
                            "id": "013",
                            "main_description": "cold displeased",
                            "keywords": ["不爽", "冷淡", "警惕"],
                            "use_when": ["吐槽", "质疑"],
                            "emotion_group": "anger",
                            "emotion_subtype": "cold_displeased",
                            "intensity": 4,
                            "recommended_hand_pose": "arms_crossed",
                            "compatible_hand_poses": ["arms_crossed", "normal"],
                            "avoid_hand_poses": ["index_finger"],
                        },
                        {
                            "id": "026",
                            "main_description": "relieved smile",
                            "keywords": ["安心", "温柔", "轻笑"],
                            "use_when": ["轻声感谢", "温柔收尾"],
                            "emotion_group": "joy",
                            "emotion_subtype": "relieved_smile",
                            "intensity": 2,
                            "recommended_hand_pose": "normal",
                            "compatible_hand_poses": ["normal"],
                            "avoid_hand_poses": ["arms_crossed", "index_finger"],
                        },
                    ],
                    "hand_poses": {
                        "normal": {"folder": "normal", "keywords": ["普通"], "best_for": ["普通对话", "低落"], "avoid_for": []},
                        "arms_crossed": {"folder": "arms_crossed", "keywords": ["不满"], "best_for": ["拒绝", "质疑"], "avoid_for": ["温柔安慰"]},
                        "index_finger": {"folder": "index_finger", "keywords": ["解释", "说明"], "best_for": ["解释规则", "提醒对方"], "avoid_for": ["哭泣"]},
                    },
                }
        if director:
            rules["director_policy"] = {
                "enabled": True,
                "version": "galgame-director-v2",
                "archetypes": {
                    "attentive": "000",
                    "serious": "001",
                    "warm": "002",
                    "relieved": "026",
                    "bright_joy": "011",
                    "awkward": "007",
                    "explain": "004",
                    "surprise": "009",
                    "downcast": "010",
                    "vulnerable": "016",
                    "bittersweet": "007",
                    "confrontation": "008",
                    "restrained": "019",
                },
            }
        else:
            rules["director_policy"] = {"enabled": False}
        rules_path.write_text(
            json.dumps(
                rules,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        config_path = root / "visual_config.json"
        config_path.write_text(
            json.dumps(
                {
                    "enabled": True,
                    "diff_root": str(diff_root),
                    "rules_path": str(rules_path),
                    "costume_mode": "fixed",
                    "selected_costume": "school",
                    "split_punctuation": "。！？!?",
                    "segments": {
                        "min_chars": 1,
                        "max_chars": 120,
                        "max_units": 1,
                        "merge_short_segments": False,
                    },
                    "selection": {"enable_smoothing": False, "min_hold_segments": 1, "max_changes": 99},
                    "character": {"default_expression_id": "002", "default_hand_pose": "normal"},
                    "dialog": {},
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        service = VisualDiffService(config_path=config_path)
        self.addCleanup(temp.cleanup)
        return service

    def test_local_vote_classifier_selects_explanatory_pose(self):
        service = self.make_service()

        payload = service.build_visual_payload("つまり、分類モデルの損失関数を説明します。", "happy")

        self.assertEqual(payload["selection_source"], "local_vote_classifier")
        self.assertEqual(payload["classifier"]["version"], "local_vote_v1")
        self.assertEqual(payload["cues"][0]["expression_id"], "004")
        self.assertEqual(payload["cues"][0]["hand_pose"], "index_finger")

    def test_director_is_opt_in_when_asset_metadata_is_old(self):
        policy = VisualDiffService.director_policy_for_rules({"expressions": []})

        self.assertIsNone(policy)

    def test_asset_metadata_can_explicitly_disable_tracked_director_policy(self):
        policy = VisualDiffService.director_policy_for_rules(
            {"expressions": [], "director_policy": {"enabled": False}}
        )

        self.assertIsNone(policy)

    def test_invalid_asset_policy_metadata_keeps_legacy_vote(self):
        policy = VisualDiffService.director_policy_for_rules(
            {"expressions": [], "director_policy": None}
        )

        self.assertIsNone(policy)

    def test_local_vote_classifier_selects_emotional_diffs(self):
        service = self.make_service()

        payload = service.build_visual_payload("そんな言い方はだめ。少し不爽だわ。", "angry")
        self.assertEqual(payload["cues"][0]["expression_id"], "013")
        self.assertEqual(payload["cues"][0]["hand_pose"], "arms_crossed")

        payload = service.build_visual_payload("ごめんね。少し难过なの。", "sad")
        self.assertEqual(payload["cues"][0]["expression_id"], "010")
        self.assertEqual(payload["cues"][0]["hand_pose"], "normal")

    def test_director_policy_owns_ordered_stream_state_before_image_resolution(self):
        service = self.make_service(director=True)
        context = service.prepare_stream_context()
        context["turn_visual_scene"] = "explain|explain>attentive|teach_once"

        opening = service.prepare_unit_visual_direction(
            "センサーってのは信号を測る装置よ。", "happy", context
        )
        continuation = service.prepare_unit_visual_direction(
            "その信号を平均化して、揺れを減らすの。", "happy", context
        )
        payload = service.build_unit_visual_payload(
            current_unit_text="センサーってのは信号を測る装置よ。",
            emotion="happy",
            unit_index=0,
            runtime_context=context,
            prepared_direction=opening,
        )

        self.assertEqual(opening.hand_pose, "index_finger")
        self.assertEqual(continuation.hand_pose, "normal")
        self.assertEqual(payload["selection_source"], "galgame_director")
        self.assertEqual(payload["classifier_version"], "galgame_director_v3")
        self.assertEqual(payload["cue"]["expression_id"], "004")
        self.assertEqual(payload["cue"]["hand_pose"], "index_finger")

    def test_director_stream_uses_model_plan_for_the_first_medical_beat(self):
        service = self.make_service(director=True)
        context = service.prepare_stream_context()
        context["turn_visual_scene"] = "risk_active|serious|none"

        direction = service.prepare_unit_visual_direction(
            "……今度は本当に、って。",
            "surprised",
            context,
            turn_user_text="今度は本当に胸が痛くて、息が苦しい。",
        )

        self.assertEqual(direction.hand_pose, "normal")
        self.assertEqual(direction.face_archetype, "serious")
        self.assertEqual(direction.pose_arc, "risk_active")

    def test_director_stream_consumes_bounded_model_scene_from_turn_context(self):
        service = self.make_service(director=True)
        context = service.prepare_stream_context()
        context["turn_visual_scene"] = "boundary"

        direction = service.prepare_unit_visual_direction(
            "我不能帮助你公开真人隐私。",
            "angry",
            context,
            turn_user_text="一种规则尚未见过的隐私胁迫说法。",
        )

        self.assertEqual(direction.hand_pose, "arms_crossed")
        self.assertEqual(direction.face_archetype, "serious")
        self.assertEqual(direction.pose_arc, "boundary")

    def test_director_policy_sync_path_uses_one_session_for_all_segments(self):
        service = self.make_service(director=True)

        payload = service.build_visual_payload(
            "センサーってのは信号を測る装置よ。その信号を平均化して、揺れを減らすの。",
            "happy",
        )

        self.assertEqual(payload["selection_source"], "galgame_director")
        self.assertEqual(payload["classifier"]["version"], "galgame_director_v3")
        self.assertEqual([cue["hand_pose"] for cue in payload["cues"]], ["normal", "normal"])

    def test_director_missing_pose_asset_keeps_face_and_uses_normal_asset_fallback(self):
        service = self.make_service(director=True)
        context = service.prepare_stream_context()
        context["turn_visual_scene"] = "explain|explain>attentive|teach_once"
        direction = service.prepare_unit_visual_direction(
            "センサーってのは信号を測る装置よ。", "happy", context
        )
        missing = (
            Path(context["config"]["diff_root"])
            / "school"
            / "index_finger"
            / "spica_face001_004.png"
        )
        missing.unlink()

        with patch.object(service, "rule_classifier", side_effect=AssertionError("must not reclassify")):
            payload = service.build_unit_visual_payload(
                current_unit_text="センサーってのは信号を測る装置よ。",
                emotion="happy",
                unit_index=0,
                runtime_context=context,
                prepared_direction=direction,
            )

        self.assertEqual(payload["cue"]["expression_id"], "004")
        self.assertEqual(payload["cue"]["hand_pose"], "normal")
        self.assertIn("missing_image", payload["selection_error"])


if __name__ == "__main__":
    unittest.main()

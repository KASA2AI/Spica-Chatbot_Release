import json
import tempfile
import unittest
from pathlib import Path

from agent_tools.visual import VisualDiffService


class VisualClassifierTests(unittest.TestCase):
    def make_service(self):
        temp = tempfile.TemporaryDirectory()
        root = Path(temp.name)
        diff_root = root / "diffs"
        costume = diff_root / "school"
        for folder in ("normal", "arms_crossed", "index_finger"):
            folder_path = costume / folder
            folder_path.mkdir(parents=True, exist_ok=True)
            for expression_id in (
                "000", "001", "002", "003", "004", "007", "008", "009",
                "010", "011", "012", "013", "015", "016", "019", "026",
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
        # Include the actual competing subtypes involved in the regressions;
        # a fixture containing only smiles cannot catch these wrong selections.
        for expression_id, group, subtype, intensity in (
            ("007", "awkward", "forced_smile", 3),
            ("012", "fear", "worried", 4),
            ("015", "tired", "low_energy", 2),
        ):
            rules["expressions"].append({
                "id": expression_id,
                "emotion_group": group,
                "emotion_subtype": subtype,
                "intensity": intensity,
                "keywords": [],
                "use_when": [],
                "recommended_hand_pose": "normal",
                "compatible_hand_poses": ["normal"],
                "avoid_hand_poses": ["index_finger"],
            })
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

    def test_local_vote_classifier_selects_emotional_diffs(self):
        service = self.make_service()

        payload = service.build_visual_payload("そんな言い方はだめ。少し不爽だわ。", "angry")
        self.assertEqual(payload["cues"][0]["expression_id"], "013")
        self.assertEqual(payload["cues"][0]["hand_pose"], "arms_crossed")

        payload = service.build_visual_payload("ごめんね。少し难过なの。", "sad")
        self.assertEqual(payload["cues"][0]["expression_id"], "010")
        self.assertEqual(payload["cues"][0]["hand_pose"], "normal")

    def test_ordinary_words_do_not_vote_for_hesitation_or_sleepiness(self):
        service = self.make_service()
        for text, wrong_expression in (
            ("その本は机の上にあるわ。", "007"),
            ("そのあと、一緒に続きを見ましょう。", "007"),
            ("困ったら、いつでも声をかけて。", "015"),
        ):
            with self.subTest(text=text):
                payload = service.build_visual_payload(text, "happy")
                self.assertNotEqual(payload["cues"][0]["expression_id"], wrong_expression)

        for text, expression in (
            ("えっと、その……うまく言えないの。", "007"),
            ("眠いから、もう寝るわ。", "015"),
        ):
            with self.subTest(text=text):
                payload = service.build_visual_payload(text, "happy")
                self.assertEqual(payload["cues"][0]["expression_id"], expression)

    def test_negated_feelings_and_reassurance_do_not_vote_as_affirmations(self):
        service = self.make_service()
        for text, emotion, absent_signal in (
            ("嫌いじゃないわ。", "happy", "anger"),
            ("嫌いになるわけないでしょ。", "happy", "anger"),
            ("好きじゃない！", "angry", "affection"),
            ("心配しないで、ここで話を聞くわ。", "happy", "worry"),
            ("もう、ほんと、何も不安にならなくていいのよ。", "happy", "worry"),
            ("别担心，我就在这里。", "happy", "worry"),
            ("我不喜欢这样。", "angry", "affection"),
            # Captured from real Spica API replies, not prompted fixed lines.
            ("私があなたを嫌うわけないでしょ。憎たらしいことも言うけど、嫌いになったりしないわ。", "happy", "anger"),
            ("無理に話さなくていいから。声なら、聞かせてあげるわ。", "happy", "sad"),
            ("別に、嬉しいとか、そういうの……全然ないんだから。", "happy", "positive"),
            ("どうしても好きになれない、受けつけない——ってこと。", "sad", "affection"),
        ):
            with self.subTest(text=text):
                analysis = service.analyze_visual_text(text, emotion)
                self.assertNotIn(absent_signal, analysis["signal_scores"])
                payload = service.build_visual_payload(text, emotion)
                self.assertNotEqual(payload["cues"][0]["expression_id"], "012")
                if emotion == "angry":
                    self.assertNotIn(payload["cues"][0]["expression_id"], {"002", "003", "004", "026"})
                elif emotion == "happy" and absent_signal != "positive":
                    self.assertNotEqual(payload["cues"][0]["expression_id"], "013")

    def test_scoped_negation_keeps_real_feelings_elsewhere(self):
        service = self.make_service()
        for text, signal in (
            ("心配しないで。でも暗い道は怖いわ。", "worry"),
            ("嫌いじゃない。でも、そんな言い方はやめて。", "anger"),
            ("許せない！", "anger"),
            ("嫌いじゃないわけじゃない。", "anger"),
            ("君が好きじゃないわけじゃない。", "affection"),
        ):
            with self.subTest(text=text):
                self.assertIn(signal, service.analyze_visual_text(text, "happy")["signal_scores"])

    def test_metalinguistic_quotes_are_not_spicas_current_feelings(self):
        service = self.make_service()
        for text in (
            "「嫌い」は英語で hate という意味よ。",
            "「嫌い」ね。文字どおりは『讨厌』って意味よ。",
            "「嫌い」ね。字のままいえば「讨厌」って意味よ。",
            "単語としては、『讨厌、不喜欢』って意味よ。",
        ):
            with self.subTest(text=text):
                # The live unit can contain a quoted word followed by its
                # definition; replay the same unit, without re-segmenting it.
                selection = service.local_vote_selection_for_text(text, "happy")
                self.assertEqual(selection["hand_pose"], "index_finger")
                self.assertNotIn("anger", service.analyze_visual_text(text, "happy")["signal_scores"])

        # Quotation marks alone are not a reason to discard an acted line.
        self.assertIn("anger", service.analyze_visual_text("「嫌い！」って言いたくなるわ。", "angry")["signal_scores"])

    def test_overlapping_terms_cast_one_vote_per_span(self):
        service = self.make_service()
        for text, signal, term in (
            ("大好き！", "affection", "大好き"),
            ("ありがとう。", "thanks", "ありがとう"),
            ("Spica と呼んでね。", "greeting", "spica"),
        ):
            with self.subTest(text=text):
                analysis = service.analyze_visual_text(text, "happy")
                self.assertEqual(analysis["signal_scores"][signal], service.term_weight(term))

        self.assertNotIn("positive", service.analyze_visual_text("いい加減にして！", "angry")["signal_scores"])

    def test_gentle_permission_is_not_excited_praise(self):
        service = self.make_service()
        analysis = service.analyze_visual_text("でも、そんな日はゆっくりでいいのよ。", "happy")
        self.assertNotIn("positive", analysis["signal_scores"])
        self.assertIn("positive", service.analyze_visual_text("いいね、楽しそう！", "happy")["signal_scores"])

    def test_pointing_requires_explanation_or_reminder_intent(self):
        service = self.make_service()
        for text in (
            "べ、別にあんたのためじゃないんだからね！",
            "まずは水を飲んで、少し休もう。",
            "だから、一緒に帰りたいの。",
            "覚えていてくれたのね、ありがとう。",
        ):
            with self.subTest(text=text):
                payload = service.build_visual_payload(text, "happy")
                self.assertNotEqual(payload["cues"][0]["hand_pose"], "index_finger")

        for text in (
            "つまり、分類モデルの損失関数を説明します。",
            "忘れないで、約束よ。",
            "出る前に確認しなさいよ。",
        ):
            with self.subTest(text=text):
                payload = service.build_visual_payload(text, "happy")
                self.assertEqual(payload["cues"][0]["hand_pose"], "index_finger")

        # Real Japanese model output must not rely on a Chinese-only 分类 token.
        payload = service.build_visual_payload("分類ってのはね、データをグループに分けることよ。", "happy")
        self.assertEqual(payload["cues"][0]["hand_pose"], "index_finger")
        self.assertNotIn("question", service.analyze_visual_text("本当に楽しい時間だったわ。", "happy")["signal_scores"])
        self.assertNotIn("cold", service.analyze_visual_text("新しいデータにも、境界線を勝手に当てはめるの。", "happy")["signal_scores"])
        self.assertIn("cold", service.analyze_visual_text("もう勝手にして。", "angry")["signal_scores"])

if __name__ == "__main__":
    unittest.main()

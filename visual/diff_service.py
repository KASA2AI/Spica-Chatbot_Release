import copy
import json
import random
import re
import threading
from pathlib import Path
from typing import Any
from urllib.parse import quote

from common.timing import elapsed_ms, now_ms


BASE_DIR = Path(__file__).resolve().parents[1]
PROJECT_ROOT = BASE_DIR
SPICA_DATA_DIR = PROJECT_ROOT / "spica_data"
DEFAULT_DIFF_ROOT = SPICA_DATA_DIR / "diffs"
DEFAULT_RULES_PATH = DEFAULT_DIFF_ROOT / "expression_hand_pose_rules.json"
DEFAULT_CONFIG_PATH = BASE_DIR / "config" / "visual_config.json"

HAND_POSE_ALIASES = {
    "normal": "normal",
    "普通": "normal",
    "普通动作": "normal",
    "arms_crossed": "arms_crossed",
    "crossed_arms": "arms_crossed",
    "抱肩": "arms_crossed",
    "抱胸": "arms_crossed",
    "index_finger": "index_finger",
    "finger": "index_finger",
    "pointing": "index_finger",
    "竖食指": "index_finger",
    "食指": "index_finger",
}

FALLBACK_BY_EMOTION = {
    "happy": ("002", "normal"),
    "angry": ("013", "arms_crossed"),
    "sad": ("010", "normal"),
    "surprised": ("009", "normal"),
}

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
VISUAL_CLASSIFIER_VERSION = "local_vote_v1"

SIGNAL_LEXICON = {
    "explain": (
        "つまり", "だから", "まず", "例えば", "たとえば", "説明", "解説", "意味", "仕組み",
        "使います", "できます", "分解", "積分", "離散", "変換", "周波数", "基底", "式",
        "具体例", "必要なら", "ポイント", "注意", "要するに", "说明", "解释", "比如",
        "例如", "公式", "模型", "函数", "分类", "回归", "预测", "学习",
    ),
    "remind": (
        "覚えて", "忘れない", "注意", "気をつけ", "大事", "重要", "ポイント", "建议",
        "提醒", "记住", "注意して", "確認",
    ),
    "question": (
        "?", "？", "えっ", "え？", "本当", "まさか", "どうして", "なぜ", "なんで",
        "真的吗", "不会吧", "为什么", "怎么会", "吗", "呢",
    ),
    "greeting": (
        "こんにちは", "おはよう", "こんばんは", "やっほ", "你好", "早上好", "晚上好",
        "spica", "Spica",
    ),
    "thanks": (
        "ありがとう", "ありがと", "感謝", "谢谢", "謝謝", "助かった", "うれしい",
    ),
    "affection": (
        "好き", "大好き", "愛して", "喜欢", "喜歡", "爱你", "可愛い", "かわいい",
        "幸せ", "安心",
    ),
    "positive": (
        "いい", "よかった", "楽しい", "嬉しい", "开心", "高兴", "期待", "すごい",
        "素敵", "できた", "大丈夫",
    ),
    "apology": (
        "ごめん", "すみません", "申し訳", "抱歉", "对不起", "道歉",
    ),
    "sad": (
        "悲しい", "寂しい", "つらい", "辛い", "難过", "难过", "伤心", "委屈",
        "失落", "低落", "泣", "哭", "無理", "絶望", "消沉",
    ),
    "worry": (
        "心配", "不安", "怖い", "こわい", "危ない", "大丈夫かな", "担心", "害怕",
        "危险", "紧张", "慌",
    ),
    "anger": (
        "だめ", "駄目", "やめて", "嫌", "許せ", "怒", "不行", "不要", "别这样",
        "讨厌", "生气", "不爽", "烦", "ふざけ", "いい加減",
    ),
    "cold": (
        "別に", "知らない", "勝手", "どうでも", "冷淡", "嫌弃", "警惕", "怀疑",
        "质疑", "压迫感", "不想理",
    ),
    "tease": (
        "ふふ", "へえ", "得意", "坏笑", "挑衅", "小恶魔", "捉弄", "吐槽",
        "からか", "冗談",
    ),
    "awkward": (
        "えっと", "その", "まあ", "苦笑", "尴尬", "无奈", "心虚", "勉强",
        "逞强", "被戳穿",
    ),
    "tired": (
        "疲れ", "眠い", "困", "无语", "敷衍", "懒得", "低能量", "冷场",
    ),
    "shout": (
        "！！", "!!", "!", "！", "怒鳴", "叫", "喊", "爆发", "抓狂", "崩溃",
        "忍无可忍",
    ),
}

SIGNAL_TO_GROUP = {
    "explain": {"neutral": 5, "joy": 3},
    "remind": {"neutral": 4, "joy": 2, "anger": 1},
    "question": {"surprise": 7, "fear": 2, "neutral": 1},
    "greeting": {"joy": 8},
    "thanks": {"joy": 8},
    "affection": {"joy": 8},
    "positive": {"joy": 6},
    "apology": {"sad": 6, "awkward": 2},
    "sad": {"sad": 8},
    "worry": {"fear": 7, "surprise": 2},
    "anger": {"anger": 8},
    "cold": {"anger": 5, "neutral": 3},
    "tease": {"smug": 8, "joy": 2},
    "awkward": {"awkward": 8, "sad": 2},
    "tired": {"tired": 8, "neutral": 2},
    "shout": {"anger": 3, "sad": 2, "surprise": 1},
}

EMOTION_GROUP_PRIORS = {
    "happy": {"joy": 6, "neutral": 2},
    "angry": {"anger": 7, "smug": 2, "neutral": 1},
    "sad": {"sad": 7, "fear": 2, "awkward": 2, "tired": 1},
    "surprised": {"surprise": 7, "fear": 2, "neutral": 1},
}

SUBTYPE_SIGNAL_PRIORS = {
    "explain": {"talking_light": 16, "serious": 8, "attentive": 4, "soft_smile": 4},
    "remind": {"talking_light": 12, "serious": 8, "soft_smile": 4},
    "greeting": {"closed_eye_smile": 13, "soft_smile": 8, "talking_light": 5},
    "thanks": {"relieved_smile": 14, "closed_eye_smile": 10, "soft_smile": 7},
    "affection": {"closed_eye_smile": 13, "relieved_smile": 10, "soft_smile": 8},
    "positive": {"excited": 10, "closed_eye_smile": 7, "soft_smile": 5},
    "question": {"mild": 11, "protest": 8, "blank_mild": 7, "attentive": 3},
    "apology": {"downcast": 9, "quiet_hurt": 8, "forced_smile": 5, "crying": 3},
    "sad": {"downcast": 9, "quiet_hurt": 8, "enduring_pain": 7, "crying": 4, "shadow_depressed": 4},
    "worry": {"worried": 14, "mild": 4},
    "anger": {"cold_displeased": 9, "pout": 7, "tsundere_pout": 7, "angry_hurt": 6, "shouting": 4},
    "cold": {"cold_displeased": 13, "restrained": 8, "serious": 5, "dark_rage": 3},
    "tease": {"teasing": 16, "closed_eye_smile": 3},
    "awkward": {"forced_smile": 16, "quiet_hurt": 3},
    "tired": {"low_energy": 16, "restrained": 4},
    "shout": {"shouting": 9, "crying_shout": 7, "dark_rage": 6, "protest": 4},
}


class VisualDiffService:
    """Selects Galgame-style character diffs for answer segments."""

    def __init__(self, config_path: str | Path = DEFAULT_CONFIG_PATH):
        self.config_path = Path(config_path).resolve()
        self.config_dir = self.config_path.parent
        self._lock = threading.RLock()
        self._config_mtime = 0.0
        self._rules_mtime = 0.0
        self.config: dict[str, Any] = {}
        self.rules: dict[str, Any] = {}
        self.reload_config(force=True)

    def reload_config(self, force: bool = False) -> None:
        try:
            mtime = self.config_path.stat().st_mtime
        except FileNotFoundError as exc:
            raise FileNotFoundError(f"视觉配置不存在：{self.config_path}") from exc

        if not force and mtime == self._config_mtime:
            return

        with self.config_path.open("r", encoding="utf-8") as file:
            config = json.load(file)

        if not isinstance(config, dict):
            raise ValueError("visual_config.json 必须是 JSON 对象。")

        self.config = config
        self._config_mtime = mtime
        self.reload_rules(force=True)

    def reload_rules(self, force: bool = False) -> None:
        rules_path = self._resolve_path(self.config.get("rules_path") or DEFAULT_RULES_PATH)
        try:
            mtime = rules_path.stat().st_mtime
        except FileNotFoundError as exc:
            raise FileNotFoundError(f"差分规则不存在：{rules_path}") from exc

        if not force and mtime == self._rules_mtime:
            return

        with rules_path.open("r", encoding="utf-8") as file:
            rules = json.load(file)

        if not isinstance(rules, dict) or not isinstance(rules.get("expressions"), list):
            raise ValueError("差分规则 JSON 缺少 expressions 列表。")

        self.rules = rules
        self._rules_mtime = mtime

    def public_config(self) -> dict[str, Any]:
        with self._lock:
            self.reload_config()
            costumes = self.list_costume_sets()
            preview_costume = self._preview_costume(costumes)
            default_expression = str(self.config.get("character", {}).get("default_expression_id") or "000").zfill(3)
            default_pose = self.normalize_hand_pose(self.config.get("character", {}).get("default_hand_pose") or "normal")
            default_image_path = self.resolve_expression_image(preview_costume, default_pose, default_expression) if preview_costume else None
            dialog = copy.deepcopy(self.config.get("dialog", {}))
            return {
                "config": copy.deepcopy(self.config),
                "costumes": costumes,
                "default_costume": preview_costume,
                "background_url": self.asset_url(self._resolve_optional_path(self.config.get("background_path"))),
                "dialog_filter_url": self.asset_url(self._resolve_optional_path(dialog.get("filter_path"))),
                "default_character_url": self.asset_url(default_image_path),
            }

    def update_config(self, new_config: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(new_config, dict):
            raise ValueError("视觉配置必须是 JSON 对象。")

        with self._lock:
            with self.config_path.open("w", encoding="utf-8") as file:
                json.dump(new_config, file, ensure_ascii=False, indent=2)
                file.write("\n")
            self.reload_config(force=True)
            return self.public_config()

    def build_visual_payload(
        self,
        answer: str,
        emotion: str,
        requested_costume: str | None = None,
        requested_mode: str | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            self.reload_config()
            self.reload_rules()
            config = copy.deepcopy(self.config)
            rules = copy.deepcopy(self.rules)

        segments = self.split_segments(answer, config=config)
        costumes = self.list_costume_sets(config=config, rules=rules)
        costume, costume_mode = self.choose_costume(costumes, requested_costume, requested_mode, config=config)
        dialog = copy.deepcopy(config.get("dialog", {}))

        selection_error = None
        selection_source = "local_vote_classifier"
        classifier_start_ms = now_ms()
        selections = self.local_vote_classifier(segments, emotion, rules=rules)
        classifier_ms = elapsed_ms(classifier_start_ms)

        raw_selections_by_index = {
            int(item.get("index")): item
            for item in selections
            if isinstance(item, dict) and str(item.get("index", "")).isdigit()
        }
        missing_indexes = [index for index in range(len(segments)) if index not in raw_selections_by_index]
        returned_segments = len(raw_selections_by_index)
        if missing_indexes:
            rule_selections = self.rule_classifier(segments, emotion, rules=rules)
            for index in missing_indexes:
                raw_selections_by_index[index] = rule_selections[index]
            selection_error = self.append_selection_error(
                selection_error,
                f"local_classifier_missing_indexes={missing_indexes}",
            )
        selections_by_index = self.smooth_selections(raw_selections_by_index, len(segments), emotion, config=config, rules=rules)

        cues = []
        for index, text in enumerate(segments):
            selection = selections_by_index.get(index, {})
            expression_id, hand_pose, reason = self.normalize_selection(selection, emotion, rules=rules)
            image_path = self.resolve_expression_image(costume, hand_pose, expression_id, config=config, rules=rules) if costume else None
            if image_path is None and costume:
                selection_error = self.append_selection_error(
                    selection_error,
                    f"missing_image:index={index},expression_id={expression_id},hand_pose={hand_pose}",
                )
                fallback = self.rule_classifier([text], emotion, rules=rules)[0]
                expression_id, hand_pose, reason = self.normalize_selection(fallback, emotion, rules=rules)
                reason = "图片不存在，已切换到规则兜底差分。"
                image_path = self.resolve_expression_image(costume, hand_pose, expression_id, config=config, rules=rules)

            cues.append(
                {
                    "index": index,
                    "text": text,
                    "expression_id": expression_id,
                    "hand_pose": hand_pose,
                    "hand_pose_folder": self.hand_pose_folder(hand_pose, rules=rules),
                    "image_url": self.asset_url(image_path),
                    "image_path": self.project_relative_path(image_path),
                    "reason": reason,
                }
            )

        background_path = self._resolve_optional_path(config.get("background_path"))
        filter_path = self._resolve_optional_path(dialog.get("filter_path"))
        return {
            "enabled": bool(config.get("enabled", True)),
            "costume": costume,
            "costume_mode": costume_mode,
            "background_url": self.asset_url(background_path),
            "background_path": self.project_relative_path(background_path),
            "dialog": {
                **dialog,
                "filter_url": self.asset_url(filter_path),
            },
            "character": copy.deepcopy(config.get("character", {})),
            "selection_source": selection_source,
            "selection_error": selection_error,
            "classifier": {
                "version": VISUAL_CLASSIFIER_VERSION,
                "segments": len(segments),
                "answer_chars": len(answer or ""),
                "duration_ms": classifier_ms,
                "returned_segments": returned_segments,
                "missing_indexes": missing_indexes,
            },
            "cues": cues,
        }

    def prepare_stream_context(
        self,
        requested_costume: str | None = None,
        requested_mode: str | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            self.reload_config()
            self.reload_rules()
            config = copy.deepcopy(self.config)
            rules = copy.deepcopy(self.rules)

        costumes = self.list_costume_sets(config=config, rules=rules)
        costume, costume_mode = self.choose_costume(costumes, requested_costume, requested_mode, config=config)
        dialog = copy.deepcopy(config.get("dialog", {}))
        background_path = self._resolve_optional_path(config.get("background_path"))
        filter_path = self._resolve_optional_path(dialog.get("filter_path"))

        return {
            "enabled": bool(config.get("enabled", True)),
            "config": config,
            "rules": rules,
            "costume": costume,
            "costume_mode": costume_mode,
            "background_url": self.asset_url(background_path),
            "background_path": self.project_relative_path(background_path),
            "dialog": {
                **dialog,
                "filter_url": self.asset_url(filter_path),
            },
            "character": copy.deepcopy(config.get("character", {})),
            "classifier_version": VISUAL_CLASSIFIER_VERSION,
        }

    def build_unit_visual_payload(
        self,
        current_unit_text: str,
        emotion: str,
        unit_index: int,
        previous_units: list[str] | None = None,
        full_answer_so_far: str | None = None,
        runtime_context: dict[str, Any] | None = None,
        requested_costume: str | None = None,
        requested_mode: str | None = None,
    ) -> dict[str, Any]:
        context = runtime_context or self.prepare_stream_context(
            requested_costume=requested_costume,
            requested_mode=requested_mode,
        )
        config = context.get("config") if isinstance(context.get("config"), dict) else self.config
        rules = context.get("rules") if isinstance(context.get("rules"), dict) else self.rules
        unit_text = (current_unit_text or "").strip()

        selection_error = None
        selection_source = "local_vote_classifier"
        classifier_start_ms = now_ms()
        selection = self.local_vote_selection_for_text(unit_text, emotion, rules=rules)
        classifier_ms = elapsed_ms(classifier_start_ms)

        raw_selections_by_index = {
            int(unit_index): {
                "index": int(unit_index),
                **selection,
            }
        }
        selection = raw_selections_by_index[int(unit_index)]
        missing_indexes = []
        returned_segments = len(raw_selections_by_index)

        expression_id, hand_pose, reason = self.normalize_selection(selection, emotion, rules=rules)
        costume = context.get("costume")
        image_path = self.resolve_expression_image(costume, hand_pose, expression_id, config=config, rules=rules) if costume else None
        if image_path is None and costume:
            selection_error = self.append_selection_error(
                selection_error,
                f"missing_image:index={unit_index},expression_id={expression_id},hand_pose={hand_pose}",
            )
            fallback = self.rule_classifier([unit_text], emotion, rules=rules)[0]
            expression_id, hand_pose, reason = self.normalize_selection(fallback, emotion, rules=rules)
            reason = "图片不存在，已切换到规则兜底差分。"
            image_path = self.resolve_expression_image(costume, hand_pose, expression_id, config=config, rules=rules)

        cue = {
            "index": int(unit_index),
            "text": unit_text,
            "expression_id": expression_id,
            "hand_pose": hand_pose,
            "hand_pose_folder": self.hand_pose_folder(hand_pose, rules=rules),
            "image_url": self.asset_url(image_path),
            "image_path": self.project_relative_path(image_path),
            "reason": reason,
        }

        return {
            "enabled": bool(context.get("enabled", True)),
            "costume": costume,
            "costume_mode": context.get("costume_mode"),
            "background_url": context.get("background_url"),
            "background_path": context.get("background_path"),
            "dialog": copy.deepcopy(context.get("dialog", {})),
            "character": copy.deepcopy(context.get("character", {})),
            "classifier_version": VISUAL_CLASSIFIER_VERSION,
            "selection_source": selection_source,
            "selection_error": selection_error,
            "classifier": {
                "version": VISUAL_CLASSIFIER_VERSION,
                "segments": 1,
                "answer_chars": len(unit_text),
                "duration_ms": classifier_ms,
                "returned_segments": returned_segments,
                "missing_indexes": missing_indexes,
                "unit_index": int(unit_index),
                "confidence": selection.get("confidence"),
                "signals": selection.get("signals", []),
            },
            "cue": cue,
            "cues": [cue],
        }

    def split_segments(self, text: str, config: dict[str, Any] | None = None) -> list[str]:
        config = config if isinstance(config, dict) else self.config
        clean = re.sub(r"\s+", " ", (text or "").strip())
        if not clean:
            return []

        punctuation = re.escape(str(config.get("split_punctuation") or "。！？!?、，,；;…"))
        pattern = re.compile(rf"[^{punctuation}]+[{punctuation}]*")
        raw_segments = [match.group(0).strip() for match in pattern.finditer(clean) if match.group(0).strip()]
        if not self.segment_settings_for_config(config)["merge_short_segments"]:
            return raw_segments or [clean]
        return self.merge_segments(raw_segments or [clean], config=config)

    def merge_segments(self, segments: list[str], config: dict[str, Any] | None = None) -> list[str]:
        settings = self.segment_settings_for_config(config)
        min_chars = settings["min_chars"]
        max_chars = settings["max_chars"]
        max_units = settings["max_units"]
        merged = []
        current = ""
        current_units = 0

        for segment in segments:
            if not current:
                current = segment
                current_units = 1
                continue

            candidate = current + segment
            if current_units < max_units and (len(current) < min_chars or len(candidate) <= max_chars):
                current = candidate
                current_units += 1
                continue

            merged.append(current)
            current = segment
            current_units = 1

        if current:
            merged.append(current)
        return merged

    @property
    def segment_settings(self) -> dict[str, Any]:
        return self.segment_settings_for_config(self.config)

    def segment_settings_for_config(self, config: dict[str, Any] | None = None) -> dict[str, Any]:
        config = config if isinstance(config, dict) else self.config
        settings = config.get("segments", {})
        if not isinstance(settings, dict):
            settings = {}

        try:
            min_chars = int(settings.get("min_chars", 36))
        except (TypeError, ValueError):
            min_chars = 36

        try:
            max_chars = int(settings.get("max_chars", 90))
        except (TypeError, ValueError):
            max_chars = 90

        try:
            max_units = int(settings.get("max_units", 2))
        except (TypeError, ValueError):
            max_units = 2

        merge_short_segments = bool(settings.get("merge_short_segments", False))

        return {
            "min_chars": max(1, min_chars),
            "max_chars": max(max_chars, min_chars),
            "max_units": max(1, max_units),
            "merge_short_segments": merge_short_segments,
        }

    def local_vote_classifier(
        self,
        segments: list[str],
        emotion: str,
        rules: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        selections = []
        for index, text in enumerate(segments):
            selection = self.local_vote_selection_for_text(text, emotion, rules=rules)
            selections.append({"index": index, **selection})
        return selections

    def local_vote_selection_for_text(
        self,
        text: str,
        emotion: str,
        rules: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        rules = rules if isinstance(rules, dict) else self.rules
        expression_items = [item for item in rules.get("expressions", []) if isinstance(item, dict)]
        if not expression_items:
            expression_id, hand_pose, reason = self.fallback_selection(emotion, "本地分类：没有可用表情规则。")
            return {
                "expression_id": expression_id,
                "hand_pose": hand_pose,
                "reason": reason,
                "confidence": 0.0,
                "signals": [],
            }

        analysis = self.analyze_visual_text(text, emotion)
        scored = [
            (self.score_expression(item, text, analysis), item)
            for item in expression_items
        ]
        scored.sort(key=lambda item: item[0], reverse=True)
        best_score, expression = scored[0]
        second_score = scored[1][0] if len(scored) > 1 else best_score
        expression_id = str(expression.get("id")).zfill(3)
        hand_pose, pose_score = self.choose_hand_pose_for_expression(expression, text, analysis, rules=rules)
        confidence = self.score_confidence(best_score, second_score)
        signals = self.top_signal_names(analysis)
        reason = (
            "本地投票："
            f"signals={','.join(signals) or 'neutral'}；"
            f"group={expression.get('emotion_group')}/{expression.get('emotion_subtype')}；"
            f"expr_score={best_score:.1f}；pose_score={pose_score:.1f}。"
        )
        return {
            "expression_id": expression_id,
            "hand_pose": hand_pose,
            "reason": reason,
            "confidence": confidence,
            "signals": signals,
        }

    def analyze_visual_text(self, text: str, emotion: str) -> dict[str, Any]:
        raw_text = text or ""
        normalized = raw_text.lower()
        signal_scores: dict[str, float] = {}
        matched_terms: dict[str, list[str]] = {}
        for signal, terms in SIGNAL_LEXICON.items():
            score = 0.0
            matches = []
            for term in terms:
                term_text = str(term)
                if not term_text:
                    continue
                if term_text.lower() in normalized or term_text in raw_text:
                    matches.append(term_text)
                    score += self.term_weight(term_text)
            if matches:
                signal_scores[signal] = score
                matched_terms[signal] = matches[:4]

        group_scores = dict(EMOTION_GROUP_PRIORS.get(emotion, EMOTION_GROUP_PRIORS["happy"]))
        for signal, score in signal_scores.items():
            for group, weight in SIGNAL_TO_GROUP.get(signal, {}).items():
                group_scores[group] = group_scores.get(group, 0.0) + weight * max(1.0, score / 3.0)

        action_scores = {"normal": 0.0, "arms_crossed": 0.0, "index_finger": 0.0}
        action_scores["index_finger"] += 8.0 * signal_scores.get("explain", 0.0)
        action_scores["index_finger"] += 7.0 * signal_scores.get("remind", 0.0)
        action_scores["arms_crossed"] += 8.0 * signal_scores.get("anger", 0.0)
        action_scores["arms_crossed"] += 7.0 * signal_scores.get("cold", 0.0)
        action_scores["arms_crossed"] += 4.0 * signal_scores.get("tease", 0.0)
        action_scores["normal"] += 8.0 * signal_scores.get("sad", 0.0)
        action_scores["normal"] += 8.0 * signal_scores.get("apology", 0.0)
        action_scores["normal"] += 7.0 * signal_scores.get("worry", 0.0)
        action_scores["normal"] += 5.0 * signal_scores.get("thanks", 0.0)
        action_scores["normal"] += 5.0 * signal_scores.get("affection", 0.0)
        action_scores["normal"] += 3.0 * signal_scores.get("greeting", 0.0)
        if not signal_scores:
            action_scores["normal"] += 4.0

        target_intensity = self.target_intensity(emotion, signal_scores, raw_text)
        return {
            "signal_scores": signal_scores,
            "matched_terms": matched_terms,
            "group_scores": group_scores,
            "action_scores": action_scores,
            "target_intensity": target_intensity,
            "text": raw_text,
        }

    def score_expression(self, expression: dict[str, Any], text: str, analysis: dict[str, Any]) -> float:
        score = 0.0
        group = str(expression.get("emotion_group") or "")
        subtype = str(expression.get("emotion_subtype") or "")
        intensity = self.safe_int(expression.get("intensity"), 2)

        score += float(analysis["group_scores"].get(group, 0.0)) * 4.0
        for signal, signal_score in analysis["signal_scores"].items():
            subtype_weight = SUBTYPE_SIGNAL_PRIORS.get(signal, {}).get(subtype, 0)
            score += subtype_weight * max(1.0, min(2.5, signal_score / 3.0))

        for term in expression.get("keywords", []):
            if self.term_matches(term, text):
                score += 12.0 + min(4.0, len(str(term)) * 0.4)
        for term in expression.get("use_when", []):
            if self.term_matches(term, text):
                score += 9.0 + min(3.0, len(str(term)) * 0.25)

        target_intensity = int(analysis["target_intensity"])
        score += max(0.0, 8.0 - abs(intensity - target_intensity) * 2.4)

        if analysis["signal_scores"].get("explain") and group in {"sad", "fear"}:
            score -= 8.0
        if analysis["signal_scores"].get("thanks") or analysis["signal_scores"].get("affection"):
            if group == "anger":
                score -= 14.0
        if analysis["signal_scores"].get("sad") and group == "joy":
            score -= 10.0
        if analysis["signal_scores"].get("anger") and group == "joy":
            score -= 12.0
        if analysis["signal_scores"].get("question") and group == "joy" and subtype != "talking_light":
            score -= 5.0

        return score

    def choose_hand_pose_for_expression(
        self,
        expression: dict[str, Any],
        text: str,
        analysis: dict[str, Any],
        rules: dict[str, Any] | None = None,
    ) -> tuple[str, float]:
        rules = rules if isinstance(rules, dict) else self.rules
        hand_pose_ids = self.hand_pose_ids_for_rules(rules)
        if not hand_pose_ids:
            return self.normalize_hand_pose(expression.get("recommended_hand_pose") or "normal"), 0.0

        compatible = {self.normalize_hand_pose(item) for item in expression.get("compatible_hand_poses", [])}
        avoid = {self.normalize_hand_pose(item) for item in expression.get("avoid_hand_poses", [])}
        recommended = self.normalize_hand_pose(expression.get("recommended_hand_pose") or "normal")
        intensity = self.safe_int(expression.get("intensity"), 2)
        group = str(expression.get("emotion_group") or "")

        scored = []
        for pose in sorted(hand_pose_ids):
            score = float(analysis["action_scores"].get(pose, 0.0))
            if pose == recommended:
                score += 12.0
            if compatible:
                score += 7.0 if pose in compatible else -18.0
            if pose in avoid:
                score -= 30.0

            pose_rule = rules.get("hand_poses", {}).get(pose, {})
            if isinstance(pose_rule, dict):
                for term in pose_rule.get("keywords", []):
                    if self.term_matches(term, text):
                        score += 4.0
                for term in pose_rule.get("best_for", []):
                    if self.term_matches(term, text):
                        score += 6.0
                for term in pose_rule.get("avoid_for", []):
                    if self.term_matches(term, text):
                        score -= 8.0

            negative_high = intensity >= 4 and group in {"anger", "sad", "fear"}
            explicit_correction = analysis["signal_scores"].get("explain") or analysis["signal_scores"].get("remind")
            if negative_high and pose == "index_finger" and not explicit_correction:
                score -= 24.0
            if pose == "arms_crossed" and (
                analysis["signal_scores"].get("thanks") or analysis["signal_scores"].get("affection")
            ):
                score -= 22.0
            if pose == "index_finger" and (
                analysis["signal_scores"].get("sad") or analysis["signal_scores"].get("apology")
            ):
                score -= 18.0

            scored.append((score, pose))

        scored.sort(key=lambda item: item[0], reverse=True)
        return scored[0][1], scored[0][0]

    def target_intensity(self, emotion: str, signal_scores: dict[str, float], text: str) -> int:
        base = 2
        if emotion in {"angry", "sad", "surprised"}:
            base = 3
        if signal_scores.get("positive") or signal_scores.get("question"):
            base = max(base, 3)
        if signal_scores.get("sad") or signal_scores.get("worry"):
            base = max(base, 3)
        if signal_scores.get("anger") or signal_scores.get("cold"):
            base = max(base, 3)
        if signal_scores.get("shout"):
            base += 1
        if re.search(r"[!！]{2,}", text or ""):
            base += 1
        if any(term in (text or "") for term in ("崩壊", "崩溃", "絶望", "大哭", "震怒", "忍无可忍")):
            base += 1
        if signal_scores.get("explain") and not (signal_scores.get("anger") or signal_scores.get("sad")):
            base = min(base, 3)
        return max(1, min(5, base))

    def score_confidence(self, best_score: float, second_score: float) -> float:
        if best_score <= 0:
            return 0.25
        margin = max(0.0, best_score - second_score)
        return round(max(0.35, min(0.98, 0.55 + margin / max(30.0, best_score))), 3)

    def top_signal_names(self, analysis: dict[str, Any]) -> list[str]:
        return [
            name
            for name, _score in sorted(
                analysis["signal_scores"].items(),
                key=lambda item: item[1],
                reverse=True,
            )[:4]
        ]

    def term_matches(self, term: Any, text: str) -> bool:
        value = str(term or "").strip()
        if not value:
            return False
        text = text or ""
        return value in text or value.lower() in text.lower()

    def term_weight(self, term: str) -> float:
        if term in {"!", "！", "?", "？"}:
            return 1.0
        return max(1.0, min(4.0, len(term) / 2.0))

    def safe_int(self, value: Any, default: int) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    def rule_classifier(
        self,
        segments: list[str],
        emotion: str,
        rules: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        return self.local_vote_classifier(segments, emotion, rules=rules)

    def rule_selection_for_text(
        self,
        text: str,
        emotion: str,
        rules: dict[str, Any] | None = None,
    ) -> tuple[str, str, str]:
        expression_id, hand_pose = FALLBACK_BY_EMOTION.get(emotion, FALLBACK_BY_EMOTION["happy"])
        expression_ids = self.expression_ids_for_rules(rules)
        hand_pose_ids = self.hand_pose_ids_for_rules(rules)
        text = text or ""

        if any(word in text for word in ("好き", "大好き", "喜欢", "喜歡", "爱你", "愛して", "うれしい", "嬉しい", "ありがとう", "ありがと", "谢谢", "感謝", "开心", "幸せ")):
            expression_id = "026" if "026" in expression_ids else ("003" if "003" in expression_ids else expression_id)
            hand_pose = "normal" if "normal" in hand_pose_ids else hand_pose
            reason = "规则兜底：亲密、感谢或开心语气。"
        elif any(word in text for word in ("こんにちは", "你好", "おはよう", "こんばんは", "やっほ", "spica", "Spica")):
            expression_id = "003" if "003" in expression_ids else expression_id
            hand_pose = "normal" if "normal" in hand_pose_ids else hand_pose
            reason = "规则兜底：轻快问候语气。"
        elif any(mark in text for mark in ("?", "？", "えっ", "本当", "まさか", "真的吗", "不会吧")):
            expression_id, hand_pose = FALLBACK_BY_EMOTION["surprised"]
            reason = "规则兜底：疑问或惊讶语气。"
        elif emotion == "angry" or any(word in text for word in ("不行", "やめて", "だめ", "嫌", "不要", "别这样")):
            expression_id, hand_pose = FALLBACK_BY_EMOTION["angry"]
            reason = "规则兜底：拒绝或不满语气。"
        elif emotion == "sad" or any(word in text for word in ("ごめん", "抱歉", "难过", "寂しい", "悲しい")):
            expression_id, hand_pose = FALLBACK_BY_EMOTION["sad"]
            reason = "规则兜底：低落或道歉语气。"
        elif any(
            word in text
            for word in (
                "つまり",
                "だから",
                "まず",
                "例えば",
                "たとえば",
                "説明",
                "学習",
                "モデル",
                "損失",
                "関数",
                "予測",
                "正解",
                "回帰",
                "分類",
                "クロスエントロピー",
                "勾配",
                "更新",
                "使われます",
                "说明",
                "解释",
                "比如",
                "例如",
                "学习",
                "模型",
                "损失",
                "函数",
                "预测",
                "分类",
                "回归",
                "梯度",
                "更新",
                "记住",
                "覚えて",
            )
        ):
            expression_id = "004" if "004" in expression_ids else ("002" if "002" in expression_ids else expression_id)
            hand_pose = "index_finger" if "index_finger" in hand_pose_ids else hand_pose
            reason = "规则兜底：说明或提醒语气。"
        else:
            reason = "本地投票兜底：未命中明确语气，使用总体情绪默认差分。"

        if expression_id not in expression_ids:
            expression_id = FALLBACK_BY_EMOTION.get(emotion, FALLBACK_BY_EMOTION["happy"])[0]
        if hand_pose not in hand_pose_ids:
            hand_pose = FALLBACK_BY_EMOTION.get(emotion, FALLBACK_BY_EMOTION["happy"])[1]
        return expression_id, hand_pose, reason

    @property
    def selection_settings(self) -> dict[str, Any]:
        return self.selection_settings_for_config(self.config)

    def selection_settings_for_config(self, config: dict[str, Any] | None = None) -> dict[str, Any]:
        config = config if isinstance(config, dict) else self.config
        settings = config.get("selection", {})
        if not isinstance(settings, dict):
            settings = {}

        try:
            min_hold_segments = int(settings.get("min_hold_segments", 2))
        except (TypeError, ValueError):
            min_hold_segments = 2

        try:
            max_changes = int(settings.get("max_changes", 99))
        except (TypeError, ValueError):
            max_changes = 99

        return {
            "min_hold_segments": max(1, min_hold_segments),
            "max_changes": max(0, max_changes),
            "enable_smoothing": bool(settings.get("enable_smoothing", False)),
        }

    def smooth_selections(
        self,
        selections_by_index: dict[int, dict[str, Any]],
        segment_count: int,
        emotion: str,
        config: dict[str, Any] | None = None,
        rules: dict[str, Any] | None = None,
    ) -> dict[int, dict[str, Any]]:
        if segment_count <= 0:
            return {}

        normalized = []
        for index in range(segment_count):
            expression_id, hand_pose, reason = self.normalize_selection(
                selections_by_index.get(index, {}),
                emotion,
                rules=rules,
            )
            normalized.append(
                {
                    "index": index,
                    "expression_id": expression_id,
                    "hand_pose": hand_pose,
                    "reason": reason,
                }
            )

        settings = self.selection_settings_for_config(config)
        if not settings["enable_smoothing"]:
            return {item["index"]: item for item in normalized}

        min_hold_segments = settings["min_hold_segments"]
        max_changes = min(settings["max_changes"], max(0, segment_count - 1))
        smoothed = [normalized[0]]
        changes = 0
        last_change_index = 0

        for index in range(1, segment_count):
            previous = smoothed[-1]
            candidate = normalized[index]
            same_diff = (
                previous["expression_id"] == candidate["expression_id"]
                and previous["hand_pose"] == candidate["hand_pose"]
            )

            should_hold = index - last_change_index < min_hold_segments
            change_limit_reached = changes >= max_changes
            if same_diff or should_hold or change_limit_reached:
                held = dict(previous)
                held["index"] = index
                if not same_diff:
                    held["reason"] = "为保持演出连贯，沿用上一段差分。"
                smoothed.append(held)
                continue

            smoothed.append(candidate)
            changes += 1
            last_change_index = index

        return {item["index"]: item for item in smoothed}

    def parse_json(self, raw_text: str) -> Any:
        raw_text = (raw_text or "").strip()
        try:
            return json.loads(raw_text)
        except json.JSONDecodeError:
            match = re.search(r"(\{.*\}|\[.*\])", raw_text, flags=re.DOTALL)
            if match:
                return json.loads(match.group(1))
        raise ValueError("差分选择模型返回的内容不是合法 JSON。")

    def normalize_selection(
        self,
        selection: dict[str, Any],
        emotion: str,
        rules: dict[str, Any] | None = None,
    ) -> tuple[str, str, str]:
        rules = rules if isinstance(rules, dict) else self.rules
        fallback_id, fallback_pose = FALLBACK_BY_EMOTION.get(emotion, FALLBACK_BY_EMOTION["happy"])
        raw_id = str(selection.get("expression_id") or selection.get("id") or fallback_id).strip()
        match = re.search(r"(\d{1,3})", raw_id)
        expression_id = match.group(1).zfill(3) if match else fallback_id
        expression_ids = self.expression_ids_for_rules(rules)
        if expression_id not in expression_ids:
            expression_id = fallback_id

        hand_pose_ids = self.hand_pose_ids_for_rules(rules)
        hand_pose = self.normalize_hand_pose(selection.get("hand_pose") or selection.get("pose") or "")
        if hand_pose not in hand_pose_ids:
            hand_pose = self.expression_by_id_for_rules(rules).get(expression_id, {}).get("recommended_hand_pose") or fallback_pose
        hand_pose = self.normalize_hand_pose(hand_pose)
        if hand_pose not in hand_pose_ids:
            hand_pose = fallback_pose

        reason = str(selection.get("reason") or "模型按断句语气选择。").strip()
        return expression_id, hand_pose, reason

    def fallback_selection(self, emotion: str, reason: str) -> tuple[str, str, str]:
        expression_id, hand_pose = FALLBACK_BY_EMOTION.get(emotion, FALLBACK_BY_EMOTION["happy"])
        return expression_id, hand_pose, reason

    @property
    def expression_ids(self) -> set[str]:
        return self.expression_ids_for_rules(self.rules)

    def expression_ids_for_rules(self, rules: dict[str, Any] | None = None) -> set[str]:
        rules = rules if isinstance(rules, dict) else self.rules
        return {str(item.get("id")).zfill(3) for item in rules.get("expressions", []) if isinstance(item, dict)}

    @property
    def expression_by_id(self) -> dict[str, dict[str, Any]]:
        return self.expression_by_id_for_rules(self.rules)

    def expression_by_id_for_rules(self, rules: dict[str, Any] | None = None) -> dict[str, dict[str, Any]]:
        rules = rules if isinstance(rules, dict) else self.rules
        return {
            str(item.get("id")).zfill(3): item
            for item in rules.get("expressions", [])
            if isinstance(item, dict)
        }

    @property
    def hand_pose_ids(self) -> set[str]:
        return self.hand_pose_ids_for_rules(self.rules)

    def hand_pose_ids_for_rules(self, rules: dict[str, Any] | None = None) -> set[str]:
        rules = rules if isinstance(rules, dict) else self.rules
        return {str(key) for key in rules.get("hand_poses", {}).keys()}

    def normalize_hand_pose(self, value: Any) -> str:
        key = str(value or "").strip()
        return HAND_POSE_ALIASES.get(key, HAND_POSE_ALIASES.get(key.lower(), key))

    def hand_pose_folder(self, hand_pose: str, rules: dict[str, Any] | None = None) -> str | None:
        rules = rules if isinstance(rules, dict) else self.rules
        item = rules.get("hand_poses", {}).get(hand_pose)
        if isinstance(item, dict):
            return str(item.get("folder") or "")
        return None

    def resolve_expression_image(
        self,
        costume: str,
        hand_pose: str,
        expression_id: str,
        config: dict[str, Any] | None = None,
        rules: dict[str, Any] | None = None,
    ) -> Path | None:
        if not costume:
            return None

        diff_root = self.diff_root_for_config(config)
        folder = self.hand_pose_folder(hand_pose, rules=rules) or hand_pose
        search_dir = diff_root / costume / folder
        if not search_dir.exists():
            return None

        matches = sorted(search_dir.glob(f"*face001_{expression_id}.png"))
        if matches:
            return matches[0].resolve()
        return None

    def list_costume_sets(
        self,
        config: dict[str, Any] | None = None,
        rules: dict[str, Any] | None = None,
    ) -> list[str]:
        rules = rules if isinstance(rules, dict) else self.rules
        diff_root = self.diff_root_for_config(config)
        if not diff_root.exists():
            return []

        required_folders = {
            str(item.get("folder"))
            for item in rules.get("hand_poses", {}).values()
            if isinstance(item, dict) and item.get("folder")
        }
        costumes = []
        for path in sorted(diff_root.iterdir(), key=lambda item: item.name):
            if not path.is_dir():
                continue
            if required_folders and not all((path / folder).is_dir() for folder in required_folders):
                continue
            costumes.append(path.name)
        return costumes

    def choose_costume(
        self,
        costumes: list[str],
        requested_costume: str | None = None,
        requested_mode: str | None = None,
        config: dict[str, Any] | None = None,
    ) -> tuple[str | None, str]:
        config = config if isinstance(config, dict) else self.config
        if not costumes:
            return None, "none"

        requested_costume = (requested_costume or "").strip()
        requested_mode = (requested_mode or "").strip().lower()
        config_mode = str(config.get("costume_mode") or "random").lower()
        mode = requested_mode if requested_mode in {"random", "fixed"} else config_mode

        if requested_costume and requested_costume in costumes:
            return requested_costume, "fixed"

        selected = str(config.get("selected_costume") or "").strip()
        if mode == "fixed" and selected in costumes:
            return selected, "fixed"

        return random.choice(costumes), "random"

    def _preview_costume(self, costumes: list[str]) -> str | None:
        if not costumes:
            return None
        selected = str(self.config.get("selected_costume") or "").strip()
        return selected if selected in costumes else costumes[0]

    @property
    def diff_root(self) -> Path:
        return self.diff_root_for_config(self.config)

    def diff_root_for_config(self, config: dict[str, Any] | None = None) -> Path:
        config = config if isinstance(config, dict) else self.config
        return self._resolve_path(config.get("diff_root") or DEFAULT_DIFF_ROOT)

    def append_selection_error(self, current: str | None, message: str) -> str:
        if not current:
            return message
        if message in current:
            return current
        return f"{current}; {message}"

    def resolve_public_asset(self, asset_path: str) -> Path:
        candidate = (PROJECT_ROOT / asset_path).resolve()
        if not self._is_under_project(candidate):
            raise FileNotFoundError("资源路径不在项目目录内。")
        if not candidate.exists() or candidate.suffix.lower() not in IMAGE_SUFFIXES:
            raise FileNotFoundError(f"资源不存在或类型不支持：{asset_path}")
        return candidate

    def asset_url(self, path: Path | None) -> str | None:
        if path is None:
            return None
        rel = self.project_relative_path(path)
        if rel is None:
            return None
        return "/visual/file/" + quote(rel, safe="/")

    def project_relative_path(self, path: Path | None) -> str | None:
        if path is None:
            return None
        try:
            return path.resolve().relative_to(PROJECT_ROOT).as_posix()
        except ValueError:
            return None

    def _resolve_path(self, value: Any) -> Path:
        path = Path(str(value))
        if not path.is_absolute():
            path = self.config_dir / path
        return path.resolve()

    def _resolve_optional_path(self, value: Any) -> Path | None:
        if value in (None, ""):
            return None
        path = self._resolve_path(value)
        return path if path.exists() else None

    def _is_under_project(self, path: Path) -> bool:
        try:
            path.relative_to(PROJECT_ROOT)
            return True
        except ValueError:
            return False

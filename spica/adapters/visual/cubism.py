"""Pack-authored local Cubism director; selection never advances playback state."""

from pathlib import Path
import re
from time import perf_counter
from uuid import uuid4

from agent_tools.visual.diff_service import VisualDiffService
from spica.core.cubism import load_bindings, load_director


_QUOTES = re.compile(r'「[^」]*」|『[^』]*』|“[^”]*”|‘[^’]*’|"[^"]*"')
_NEGATED_ACTION = re.compile(
    r"\b(?:not|never|don't|won't|can't|cannot|without|avoid|stop)\b"
    r"|不要|别用|别放|别施|不(?:能|会|用|要|施|放|使)|禁止"
    r"|(?:使|撃|放|唱|発動).{0,8}(?:ない|ません)|やめて|しない|しません",
    re.IGNORECASE,
)


def _self_action_text(text):
    # Whole-utterance quotes are common dialogue formatting. Quotes embedded
    # in a report describe somebody else's action and cannot trigger a skill.
    text = text.strip()
    for left, right in (("「", "」"), ("『", "』"), ("“", "”"), ("‘", "’"), ('"', '"')):
        if text.startswith(left) and text.endswith(right):
            text = text[1:-1]
            break
    text = _QUOTES.sub(lambda match: " " * len(match[0]), text)
    sentences = re.findall(r"[^。！？!?;；\n]+[。！？!?;；\n]*", text)
    return " ".join(sentence for sentence in sentences if not _NEGATED_ACTION.search(sentence))


class CubismVisualService(VisualDiffService):
    def __init__(self, config_path):
        super().__init__(config_path)
        root = Path(self.config["package_root"])
        self.bindings = load_bindings(root, Path(self.config["cubism"]).relative_to(root).as_posix())
        self.director = load_director(root, self.bindings)

    def prepare_stream_context(self, requested_costume=None, requested_mode=None):
        context = super().prepare_stream_context(requested_costume, requested_mode)
        context["cubism_turn"] = uuid4().hex
        return context

    def build_unit_visual_payload(self, current_unit_text, emotion, unit_index,
                                  previous_units=None, full_answer_so_far=None,
                                  runtime_context=None, requested_costume=None, requested_mode=None):
        started = perf_counter()
        context = runtime_context or self.prepare_stream_context(requested_costume, requested_mode)
        text = (current_unit_text or "").strip()
        analysis = self.analyze_visual_text(text, emotion)
        clean = analysis["cue_text"]
        costume = context.get("costume")
        spec = self.bindings.models.get(costume)
        action_text = _self_action_text(clean)
        candidates = []
        for rule in self.director.rules:
            if any(self.term_matches(term, text) for term in rule.exclude):
                continue
            pool = list(dict.fromkeys(([rule.motion] if rule.motion else []) + rule.motions))
            is_skill = spec and any(spec.motions[key].kind == "skill" for key in pool if key in spec.motions)
            rule_text = action_text if is_skill else clean
            keyword_score = sum(self.term_matches(term, rule_text) for term in rule.keywords)
            if is_skill and not keyword_score:
                continue
            score = sum(weight * min(2, analysis["signal_scores"].get(signal, 0))
                        for signal, weight in rule.signals.items())
            score += 4 * keyword_score
            if score >= rule.threshold:
                candidates.append((rule.priority, score, rule))
        winner = max(candidates, key=lambda item: item[:2], default=None)
        rule = winner[2] if winner else None
        pool = list(dict.fromkeys(([rule.motion] if rule.motion else []) + rule.motions)) if rule else []
        motions = [key for key in pool if spec and key in spec.motions]
        motion = motions[0] if motions else None
        expression = rule.expression if rule and spec and rule.expression in spec.expressions else None
        effect = rule.effect if rule and spec and rule.effect in spec.effects else None
        image = self.resolve_expression_image(costume, "normal", "000", config=context["config"], rules=context["rules"]) if costume else None
        cue = {"index": int(unit_index), "text": text, "expression_id": "000", "hand_pose": "normal",
               "image_url": self.asset_url(image), "image_path": (self.project_relative_path(image) or str(image)) if image else None,
               "reason": f"cubism:{rule.id if rule else 'idle'}",
               "cubism": {"turn": context["cubism_turn"], "index": int(unit_index), "costume": costume,
                          "intent": rule.id if rule else "idle", "motion": motion, "motions": motions, "expression": expression,
                          "effect": effect, "priority": rule.priority if rule else 0}}
        return {key: context.get(key) for key in ("enabled", "costume", "costume_mode", "background_url",
                "background_path", "dialog", "character")} | {
            "classifier_version": "cubism-local-v1", "selection_source": "cubism_local", "selection_error": None,
            "classifier": {"duration_ms": (perf_counter() - started) * 1000,
                           "confidence": min(1, winner[1] / 10) if winner else 0,
                           "signals": list(analysis["signal_scores"])}, "cue": cue, "cues": [cue]}

    def build_visual_payload(self, answer, emotion, requested_costume=None, requested_mode=None):
        context = self.prepare_stream_context(requested_costume, requested_mode)
        payloads = [self.build_unit_visual_payload(text, emotion, index, runtime_context=context)
                    for index, text in enumerate(self.split_segments(answer))]
        if not payloads:
            return self.build_unit_visual_payload("", emotion, 0, runtime_context=context)
        return payloads[0] | {"cues": [payload["cue"] for payload in payloads]}

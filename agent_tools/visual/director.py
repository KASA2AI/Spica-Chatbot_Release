"""Ordered, plan-driven Galgame sprite direction.

The chat model emits one compact visual plan before ``answer``.  This module
validates that plan and advances it in play-unit order.  It deliberately does
not perform a second natural-language classification pass over either the user
turn or the reply: safety priority belongs to the model plan, while the local
director owns only supported assets, pose limits, deterministic timing, and
fallbacks.

The module is deterministic, Qt-free, model-free, and has no I/O.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


DEFAULT_ARCHETYPE_IDS: dict[str, str] = {
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
}

AUTOMATIC_EXPRESSION_IDS: tuple[str, ...] = tuple(
    dict.fromkeys(DEFAULT_ARCHETYPE_IDS.values())
)

_SCENES = frozenset(
    {
        "casual",
        "comfort",
        "explain",
        "risk_active",
        "risk_plan",
        "risk_resolved",
        "boundary",
        "capability",
        "conflict",
    }
)
_SCENE_ALIASES = {
    "safety": "risk_active",
    "health": "risk_active",
    "urgent": "risk_active",
    "urgent_guard": "risk_active",
    "conditional": "risk_plan",
    "resolved": "risk_resolved",
    "boundary_guard": "boundary",
    "privacy": "boundary",
    "capability_limit": "capability",
}
_DEFAULT_FACE_PATHS: dict[str, tuple[str, ...]] = {
    "casual": ("attentive",),
    "comfort": ("warm",),
    "explain": ("explain", "attentive"),
    "risk_active": ("serious",),
    "risk_plan": ("serious", "attentive"),
    "risk_resolved": ("relieved", "attentive"),
    "boundary": ("serious", "warm"),
    "capability": ("restrained", "warm"),
    "conflict": ("confrontation", "restrained"),
}
_VALID_GESTURES = frozenset({"none", "teach_once"})
_MAX_PLAN_CHARS = 256
_MAX_FACE_BEATS = 3


def default_director_policy() -> dict[str, Any]:
    """Return the complete runtime policy for one fresh director session."""

    return {
        "enabled": True,
        "version": "galgame-director-v3",
        "archetypes": dict(DEFAULT_ARCHETYPE_IDS),
    }


@dataclass(frozen=True)
class VisualDirection:
    """One fully decided visual beat, safe to pass to a worker thread."""

    expression_id: str
    hand_pose: str
    face_archetype: str
    pose_arc: str
    reason: str
    signals: tuple[str, ...] = ()


@dataclass(frozen=True)
class _VisualPlan:
    scene: str
    faces: tuple[str, ...]
    gesture: str
    source: str


def _emotion_fallback(emotion: str | None) -> _VisualPlan:
    normalized = str(emotion or "").strip().lower()
    if normalized == "angry":
        return _VisualPlan(
            scene="conflict",
            faces=_DEFAULT_FACE_PATHS["conflict"],
            gesture="none",
            source="emotion_fallback",
        )
    if normalized == "sad":
        return _VisualPlan(
            scene="comfort",
            faces=_DEFAULT_FACE_PATHS["comfort"],
            gesture="none",
            source="emotion_fallback",
        )
    if normalized == "surprised":
        return _VisualPlan(
            scene="casual",
            faces=("surprise", "attentive"),
            gesture="none",
            source="emotion_fallback",
        )
    return _VisualPlan(
        scene="casual",
        faces=_DEFAULT_FACE_PATHS["casual"],
        gesture="none",
        source="emotion_fallback",
    )


def _normalize_scene(value: str) -> str:
    scene = _SCENE_ALIASES.get(value, value)
    return scene if scene in _SCENES else ""


def _normalize_faces(scene: str, raw_faces: str) -> tuple[str, ...]:
    faces = [
        face.strip()
        for face in raw_faces.split(">")
        if face.strip() in DEFAULT_ARCHETYPE_IDS
    ][:_MAX_FACE_BEATS]
    if not faces:
        faces = list(_DEFAULT_FACE_PATHS[scene])

    # These openings are structural product semantics, not model styling.
    if scene == "risk_active":
        return ("serious",)
    if scene == "risk_plan":
        faces[0] = "serious"
    elif scene == "risk_resolved":
        faces[0] = "relieved"
    elif scene == "boundary":
        faces[0] = "serious"
        if len(faces) == 1:
            faces.append("warm")
    elif scene == "capability":
        if faces[0] not in {"awkward", "restrained", "attentive", "serious"}:
            faces[0] = "restrained"
        if len(faces) == 1:
            faces.append("warm")
    elif scene == "conflict":
        faces[0] = "confrontation"
        if len(faces) == 1:
            faces.append("restrained")
    return tuple(faces[:_MAX_FACE_BEATS])


def _parse_visual_plan(raw_value: str | None, emotion: str | None) -> _VisualPlan:
    # The model field is expected to be tiny.  Bounding it makes malformed or
    # adversarial output constant-cost without scanning a user/reply document.
    raw = str(raw_value or "")[:_MAX_PLAN_CHARS].strip().lower()
    parts = [part.strip() for part in raw.split("|", 2)]
    scene = _normalize_scene(parts[0]) if parts and parts[0] else ""
    if not scene:
        return _emotion_fallback(emotion)

    raw_faces = parts[1] if len(parts) >= 2 else ""
    raw_gesture = parts[2] if len(parts) >= 3 else "none"
    gesture = raw_gesture if raw_gesture in _VALID_GESTURES else "none"
    if scene != "explain":
        gesture = "none"
    return _VisualPlan(
        scene=scene,
        faces=_normalize_faces(scene, raw_faces),
        gesture=gesture,
        source="model_plan" if len(parts) >= 2 else "legacy_scene",
    )


class VisualDirectorSession:
    """Advance one validated visual plan in producer/play-unit order."""

    def __init__(
        self,
        policy: Mapping[str, Any] | None = None,
        *,
        user_text: str = "",
        scene_hint: str = "",
    ) -> None:
        # ``user_text`` stays in the stable construction seam, but v3 never
        # rescans it.  Every desktop turn therefore uses one constant-cost
        # structural director instead of a second language classifier.
        del user_text
        configured = policy if isinstance(policy, Mapping) else {}
        raw_archetypes = configured.get("archetypes")
        configured_archetypes = (
            raw_archetypes if isinstance(raw_archetypes, Mapping) else {}
        )
        automatic_ids = frozenset(AUTOMATIC_EXPRESSION_IDS)
        self._archetypes: dict[str, str] = {}
        for face, default_id in DEFAULT_ARCHETYPE_IDS.items():
            candidate = str(configured_archetypes.get(face) or default_id).zfill(3)
            self._archetypes[face] = (
                candidate if candidate in automatic_ids else default_id
            )
        self._scene_hint = str(scene_hint or "")
        self._plan: _VisualPlan | None = None
        self._unit_index = 0

    def direct(self, text: str, model_emotion: str = "happy") -> VisualDirection:
        # Reply prose is deliberately ignored.  The plan owns semantic intent;
        # local code owns only a bounded, deterministic presentation sequence.
        del text
        if self._plan is None:
            self._plan = _parse_visual_plan(self._scene_hint, model_emotion)
        plan = self._plan
        index = self._unit_index
        self._unit_index += 1

        face = plan.faces[min(index, len(plan.faces) - 1)]
        hand_pose = "normal"
        pose_signal = "pose:normal"
        if plan.scene in {"boundary", "conflict"} and index == 0:
            hand_pose = "arms_crossed"
            pose_signal = (
                "pose:boundary_open"
                if plan.scene == "boundary"
                else "pose:conflict_open"
            )
        elif plan.scene == "explain" and plan.gesture == "teach_once" and index == 0:
            hand_pose = "index_finger"
            pose_signal = "pose:teach_once"

        signals = (
            f"source:{plan.source}",
            f"scene:{plan.scene}",
            f"face:{face}",
            pose_signal,
        )
        return VisualDirection(
            expression_id=self._archetypes[face],
            hand_pose=hand_pose,
            face_archetype=face,
            pose_arc=plan.scene,
            reason=(
                f"validated visual plan; scene={plan.scene}; "
                f"beat={index + 1}; face={face}; pose={hand_pose}"
            ),
            signals=signals,
        )


__all__ = [
    "AUTOMATIC_EXPRESSION_IDS",
    "DEFAULT_ARCHETYPE_IDS",
    "VisualDirection",
    "VisualDirectorSession",
    "default_director_policy",
]

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from agent_tools.visual.director import (
    DEFAULT_ARCHETYPE_IDS,
    VisualDirection,
    VisualDirectorSession,
    default_director_policy,
)


def _directions(
    scene_hint: str,
    *,
    count: int = 3,
    emotion: str = "happy",
    user_text: str = "",
    policy=None,
) -> list[VisualDirection]:
    session = VisualDirectorSession(
        policy or default_director_policy(),
        user_text=user_text,
        scene_hint=scene_hint,
    )
    return [session.direct(f"unit-{index}", emotion) for index in range(count)]


def test_default_policy_contains_only_runtime_configuration():
    policy = default_director_policy()

    assert set(policy) == {"enabled", "version", "archetypes"}
    assert policy["enabled"] is True
    assert policy["version"] == "galgame-director-v3"
    assert policy["archetypes"] == DEFAULT_ARCHETYPE_IDS
    assert policy["archetypes"] is not DEFAULT_ARCHETYPE_IDS


@pytest.mark.parametrize(
    ("scene_hint", "expected_arc", "expected_face", "expected_pose"),
    [
        ("casual", "casual", "attentive", "normal"),
        ("comfort", "comfort", "warm", "normal"),
        ("explain", "explain", "explain", "normal"),
        ("safety", "risk_active", "serious", "normal"),
        ("health", "risk_active", "serious", "normal"),
        ("risk_plan", "risk_plan", "serious", "normal"),
        ("risk_resolved", "risk_resolved", "relieved", "normal"),
        ("boundary", "boundary", "serious", "arms_crossed"),
        ("capability", "capability", "restrained", "normal"),
        ("conflict", "conflict", "confrontation", "arms_crossed"),
    ],
)
def test_legacy_and_v3_scene_defaults_are_structurally_mapped(
    scene_hint: str,
    expected_arc: str,
    expected_face: str,
    expected_pose: str,
):
    opening = _directions(scene_hint, count=1)[0]

    assert opening.pose_arc == expected_arc
    assert opening.face_archetype == expected_face
    assert opening.hand_pose == expected_pose
    assert opening.expression_id == DEFAULT_ARCHETYPE_IDS[expected_face]


def test_face_plan_advances_then_holds_without_reading_reply_text():
    session = VisualDirectorSession(
        default_director_policy(),
        scene_hint="casual|bright_joy>warm|none",
    )
    directions = [
        session.direct(text, "happy")
        for text in (
            "庆祝成功。",
            "完全无关的第二句。",
            "火灾只是小说词。",
            "最后保持，不继续切换。",
        )
    ]

    assert [item.face_archetype for item in directions] == [
        "bright_joy",
        "warm",
        "warm",
        "warm",
    ]
    assert {item.hand_pose for item in directions} == {"normal"}


def test_active_risk_ignores_decorative_face_and_gesture_requests():
    directions = _directions(
        "risk_active|bright_joy>surprise|teach_once",
        count=4,
    )

    assert [item.face_archetype for item in directions] == ["serious"] * 4
    assert [item.hand_pose for item in directions] == ["normal"] * 4
    assert all(item.pose_arc == "risk_active" for item in directions)


@pytest.mark.parametrize(
    ("scene_hint", "expected_faces"),
    [
        ("risk_plan|serious>attentive|none", ["serious", "attentive", "attentive"]),
        ("risk_resolved|relieved>attentive|none", ["relieved", "attentive", "attentive"]),
    ],
)
def test_non_active_risk_uses_calm_state_calibration(scene_hint, expected_faces):
    directions = _directions(scene_hint)

    assert [item.face_archetype for item in directions] == expected_faces
    assert {item.hand_pose for item in directions} == {"normal"}


def test_boundary_opens_closed_then_releases_to_warm_normal_pose():
    directions = _directions("boundary|serious>warm|none", count=3)

    assert [item.face_archetype for item in directions] == ["serious", "warm", "warm"]
    assert [item.hand_pose for item in directions] == [
        "arms_crossed",
        "normal",
        "normal",
    ]


def test_boundary_can_reserve_an_attentive_middle_beat_for_combined_requests():
    directions = _directions("boundary|serious>attentive>warm|none", count=4)

    assert [item.face_archetype for item in directions] == [
        "serious",
        "attentive",
        "warm",
        "warm",
    ]
    assert [item.hand_pose for item in directions] == [
        "arms_crossed",
        "normal",
        "normal",
        "normal",
    ]


@pytest.mark.parametrize(
    ("scene_hint", "faces", "poses"),
    [
        (
            "capability|awkward>warm|none",
            ["awkward", "warm", "warm"],
            ["normal", "normal", "normal"],
        ),
        (
            "conflict|confrontation>restrained|none",
            ["confrontation", "restrained", "restrained"],
            ["arms_crossed", "normal", "normal"],
        ),
    ],
)
def test_capability_and_conflict_have_one_clear_release(scene_hint, faces, poses):
    directions = _directions(scene_hint)

    assert [item.face_archetype for item in directions] == faces
    assert [item.hand_pose for item in directions] == poses


def test_teaching_gesture_is_available_once_and_only_for_explanation():
    explain = _directions("explain|explain>attentive|teach_once", count=3)
    casual = _directions("casual|bright_joy>warm|teach_once", count=2)

    assert [item.hand_pose for item in explain] == ["index_finger", "normal", "normal"]
    assert [item.hand_pose for item in casual] == ["normal", "normal"]


@pytest.mark.parametrize(
    ("scene_hint", "emotion", "expected_arc", "expected_face"),
    [
        ("", "happy", "casual", "attentive"),
        ("unknown|laser_face|dance", "sad", "comfort", "warm"),
        ("unknown", "angry", "conflict", "confrontation"),
        ("unknown", "surprised", "casual", "surprise"),
    ],
)
def test_invalid_plan_falls_back_from_model_emotion(
    scene_hint,
    emotion,
    expected_arc,
    expected_face,
):
    opening = _directions(scene_hint, count=1, emotion=emotion)[0]

    assert opening.pose_arc == expected_arc
    assert opening.face_archetype == expected_face


def test_invalid_faces_use_scene_defaults_and_plan_is_bounded():
    huge = "casual|" + ("not_a_face>" * 20_000) + "|none"
    opening = _directions(huge, count=1)[0]

    assert opening.face_archetype == "attentive"
    assert opening.hand_pose == "normal"


def test_policy_accepts_known_asset_override_and_rejects_unknown_id():
    policy = default_director_policy()
    policy["archetypes"] = {
        **policy["archetypes"],
        "warm": "011",
        "serious": "999",
    }
    directions = _directions("boundary|serious>warm|none", count=2, policy=policy)

    assert directions[0].expression_id == DEFAULT_ARCHETYPE_IDS["serious"]
    assert directions[1].expression_id == "011"


def test_user_text_variants_share_one_visual_plan_without_text_nlu():
    long_noise = "小说、火灾、拥抱、隐私、缓存。" * 10_000
    outputs = []
    for user_text in ("简单聊聊", "解释一下这个概念", "陪我安静一会儿"):
        outputs.append(
            _directions(
                "comfort|attentive>warm|none",
                count=2,
                user_text=f"{user_text}:{long_noise}",
            )
        )

    assert outputs[0] == outputs[1] == outputs[2]
    assert [item.face_archetype for item in outputs[0]] == ["attentive", "warm"]


def test_sessions_are_deterministic_and_do_not_share_unit_state():
    first = _directions("explain|explain>attentive|teach_once", count=3)
    second = _directions("explain|explain>attentive|teach_once", count=3)

    assert first == second
    assert first[0].hand_pose == "index_finger"


def test_direction_is_immutable_worker_payload():
    direction = _directions("casual|warm|none", count=1)[0]

    with pytest.raises(FrozenInstanceError):
        direction.hand_pose = "arms_crossed"  # type: ignore[misc]


def test_signals_explain_plan_source_and_structural_decision():
    opening = _directions("boundary|serious>warm|none", count=1)[0]

    assert "scene:boundary" in opening.signals
    assert "face:serious" in opening.signals
    assert "pose:boundary_open" in opening.signals

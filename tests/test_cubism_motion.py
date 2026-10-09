import pytest

from spica.core.cubism import CubismModel
from spica.core.cubism_motion import CubismPlayback, ParameterEffect


def test_eye_effect_keeps_its_timeline_and_returns_to_rest():
    effect = ParameterEffect({"Version": 3, "Meta": {"Duration": 2}, "Curves": [
        {"Target": "Parameter", "Id": "eye_light", "Segments": [0, 0, 2, .2, 0, 1, .3, 0, .4, 20, .5, 20, 0, 2, 15]},
    ]})
    assert effect.values(.1)["eye_light"] == 0
    assert effect.values(.4)["eye_light"] > 10
    assert effect.values(1)["eye_light"] > 15
    assert effect.values(1.99)["eye_light"] < 2
    assert effect.values(2) == {}
    with pytest.raises(ValueError):
        ParameterEffect({"Version": 3, "Meta": {"Duration": 2}, "Curves": [
            {"Target": "Parameter", "Id": "eye_light", "Segments": [0, 0, 1, 1]},
        ]})


def test_presented_units_hold_faces_and_cannot_retrigger_skill_or_cancelled_effect():
    spec = CubismModel(model="Character.model3.json", idle="idle", motions={
        "idle": {"group": "", "index": 0, "kind": "idle"},
        "cast": {"group": "", "index": 1, "kind": "skill", "cooldown": 20},
    })
    playback = CubismPlayback(spec)
    skill = {"turn": "first", "index": 0, "intent": "explosion", "motion": "cast", "effect": "light"}
    playback.queue(skill)
    assert playback.advance(10)["motion"] == "cast"
    playback.queue(skill | {"index": 1})
    assert playback.advance(11) is None
    playback.queue(skill | {"index": 2, "intent": "idle", "motion": None})
    assert playback.advance(11.1)["intent"] == "idle"
    playback.queue(skill | {"index": 3})
    assert playback.advance(11.2) is None  # Face hold; the current cue waits.
    assert playback.advance(12)["motion"] is None  # Skill cooldown is still active.
    playback.queue(skill | {"index": 4, "intent": "sad"})
    playback.reset()
    assert playback.advance(20) is None


def test_motion_pool_varies_real_presentations_and_shares_skill_cooldown():
    spec = CubismModel(model="Character.model3.json", idle="idle", motions={
        "idle": {"group": "", "index": 0, "kind": "idle"},
        "happy-a": {"group": "", "index": 1, "cooldown": 0},
        "happy-b": {"group": "", "index": 2, "cooldown": 0},
        "cast-a": {"group": "", "index": 3, "kind": "skill", "cooldown": 20},
        "cast-b": {"group": "", "index": 4, "kind": "skill", "cooldown": 20},
    })
    playback = CubismPlayback(spec)
    happy = {"turn": "one", "index": 0, "intent": "happy", "motions": ["happy-a", "happy-b"]}
    playback.queue(happy)
    assert playback.advance(10)["motion"] == "happy-a"
    playback.queue(happy | {"index": 1})
    assert playback.advance(11) is None  # Same feeling across clauses does not restart.
    playback.reset()
    playback.queue(happy | {"turn": "two"})
    assert playback.advance(12)["motion"] == "happy-b"
    cast = {"turn": "two", "index": 1, "intent": "cast", "motions": ["cast-a", "cast-b"], "effect": "glow"}
    playback.queue(cast)
    assert playback.advance(14)["motion"] == "cast-a"
    playback.reset()
    playback.queue(cast | {"turn": "three"})
    blocked = playback.advance(15)
    assert blocked["motion"] is None and blocked["effect"] is None
    playback.reset()
    playback.queue(cast | {"turn": "four"})
    assert playback.advance(35)["motion"] == "cast-b"


def test_costume_change_keeps_skill_cooldown_but_uses_the_new_models_motions():
    first = CubismModel(model="first.model3.json", idle="idle", motions={
        "idle": {"group": "", "index": 0, "kind": "idle"},
        "cast": {"group": "", "index": 1, "kind": "skill", "cooldown": 20},
    })
    second = CubismModel(model="second.model3.json", idle="idle", motions={
        "idle": {"group": "", "index": 0, "kind": "idle"},
        "cast-alt": {"group": "", "index": 2, "kind": "skill", "cooldown": 20},
    })
    playback = CubismPlayback(first)
    cast = {"turn": "one", "index": 0, "intent": "explosion", "motion": "cast", "effect": "light"}
    playback.queue(cast)
    assert playback.advance(10)["motion"] == "cast"
    playback.set_model(second)
    playback.queue(cast | {"turn": "two", "motion": "cast-alt"})
    blocked = playback.advance(11)
    assert blocked["motion"] is None and blocked["effect"] is None
    playback.reset()
    playback.queue(cast | {"turn": "three", "motion": "cast-alt"})
    assert playback.advance(31)["motion"] == "cast-alt"

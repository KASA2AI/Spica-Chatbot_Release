from __future__ import annotations

import pytest

from ui.models.stream_unit import (
    StreamUnitState,
    is_stream_unit_ready_for_playback,
    merge_stream_unit_state,
)


def test_merge_stream_unit_state_does_not_overwrite_existing_payload_with_empty_values() -> None:
    target = StreamUnitState(
        index=0,
        display_text="old display",
        tts_text="old tts",
        audio_path="/tmp/existing.wav",
        visual={"expression_id": "001"},
        cue={"image_path": "/tmp/existing.png"},
        text_ready=True,
        audio_ready=True,
        visual_ready=True,
    )
    source = StreamUnitState(
        index=0,
        display_text="new display",
        tts_text="new tts",
        audio_path=None,
        visual={},
        cue={},
        text_ready=False,
        audio_ready=False,
        visual_ready=False,
    )
    source.timeline.audio_error = "tts failed"
    source.timeline.visual_error = "visual failed"
    source.timeline.visual_ready_at_ms = 123.4

    merge_stream_unit_state(target, source)

    assert target.display_text == "new display"
    assert target.tts_text == "new tts"
    assert target.audio_path == "/tmp/existing.wav"
    assert target.visual == {"expression_id": "001"}
    assert target.cue == {"image_path": "/tmp/existing.png"}
    assert target.text_ready is True
    assert target.audio_ready is True
    assert target.visual_ready is True
    assert target.timeline.audio_error == "tts failed"
    assert target.timeline.visual_error == "visual failed"
    assert target.timeline.visual_ready_at_ms == 123.4


def test_merge_stream_unit_state_accepts_non_empty_compat_unit_payload() -> None:
    target = StreamUnitState(
        index=1,
        display_text="……",
        text_ready=False,
        audio_ready=False,
        visual_ready=False,
    )
    source = StreamUnitState(
        index=1,
        display_text="ready display",
        tts_text="ready tts",
        audio_path="/tmp/ready.wav",
        visual={"expression_id": "002"},
        cue={"image_path": "/tmp/ready.png"},
        text_ready=True,
        audio_ready=True,
        visual_ready=True,
    )

    merge_stream_unit_state(target, source)

    assert target.display_text == "ready display"
    assert target.tts_text == "ready tts"
    assert target.audio_path == "/tmp/ready.wav"
    assert target.visual == {"expression_id": "002"}
    assert target.cue == {"image_path": "/tmp/ready.png"}
    assert target.text_ready is True
    assert target.audio_ready is True
    assert target.visual_ready is True


@pytest.mark.parametrize(
    ("text_ready", "audio_ready", "visual_ready", "expected"),
    [
        (True, True, False, True),
        (True, False, True, False),
        (False, True, True, False),
        (True, True, True, True),
    ],
)
def test_stream_unit_playback_ready_only_depends_on_text_and_audio(
    text_ready: bool,
    audio_ready: bool,
    visual_ready: bool,
    expected: bool,
) -> None:
    unit = StreamUnitState(
        index=0,
        text_ready=text_ready,
        audio_ready=audio_ready,
        visual_ready=visual_ready,
    )

    assert is_stream_unit_ready_for_playback(unit) is expected


def test_stream_unit_playback_ready_ignores_audio_and_visual_errors_when_ready_flags_are_set() -> None:
    unit = StreamUnitState(
        index=0,
        text_ready=True,
        audio_ready=True,
        visual_ready=False,
    )
    unit.timeline.audio_error = "tts failed"
    unit.timeline.visual_error = "visual failed"

    assert is_stream_unit_ready_for_playback(unit) is True

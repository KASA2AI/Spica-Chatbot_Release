"""Phase 3: anime events register_event round-trip + no-secrets invariant."""

from __future__ import annotations

from spica.anime.models import DownloadTerminalCause, DownloadTerminalResult
from spica.core.anime_events import (
    AnimeCancelRequestEvent,
    AnimeReadyEvent,
    AnimeRequestEvent,
)
from spica.core.events import event_from_legacy


def test_anime_request_round_trip():
    ev = AnimeRequestEvent(
        request_id="r1", query="无职转生", title="无职转生 S3E1",
        episode_key="无职转生|s3|e1", source="mikan",
        locator="magnet:?xt=urn:btih:" + "a" * 40, display_title="无职转生 S3E1",
        size_bytes=700, created_at="2026-07-06T00:00:00",
        torrent_payload_b64="dGVzdC10b3JyZW50")
    assert ev.to_legacy_dict()["event"] == "anime_request"
    assert event_from_legacy(ev.to_legacy_dict()) == ev


def test_anime_ready_round_trip():
    ev = AnimeReadyEvent(request_id="r1", episode_key="无职转生|s3|e1",
                         save_path="/dl/ep.mkv", elapsed_seconds=42.0, error=None)
    assert ev.to_legacy_dict()["event"] == "anime_ready"
    assert event_from_legacy(ev.to_legacy_dict()) == ev


def test_anime_ready_error_round_trip():
    ev = AnimeReadyEvent(request_id="r1", episode_key="k",
                         save_path=None, elapsed_seconds=None, error="disk full")
    back = event_from_legacy(ev.to_legacy_dict())
    assert ev.terminal_result is DownloadTerminalResult.FAILED
    assert back == ev and back.error == "disk full"


def test_legacy_anime_ready_without_terminal_fields_derives_result():
    failed = event_from_legacy({
        "event": "anime_ready",
        "data": {"request_id": "r1", "episode_key": "k", "error": "boom"},
    })
    completed = event_from_legacy({
        "event": "anime_ready",
        "data": {"request_id": "r2", "episode_key": "k", "error": None},
    })

    assert failed.terminal_result is DownloadTerminalResult.FAILED
    assert failed.terminal_cause is DownloadTerminalCause.NORMAL
    assert completed.terminal_result is DownloadTerminalResult.COMPLETED
    assert completed.terminal_cause is DownloadTerminalCause.NORMAL


def test_anime_ready_terminal_result_and_cause_round_trip_as_values():
    ev = AnimeReadyEvent(
        request_id="r1",
        episode_key="k",
        error="后台任务可能仍在",
        terminal_result=DownloadTerminalResult.UNCONFIRMED,
        terminal_cause=DownloadTerminalCause.MANUAL,
    )

    legacy = ev.to_legacy_dict()

    assert legacy["data"]["terminal_result"] == "unconfirmed"
    assert legacy["data"]["terminal_cause"] == "manual"
    assert event_from_legacy(legacy) == ev


def test_anime_cancel_request_round_trip():
    ev = AnimeCancelRequestEvent(request_id="r1", title="幼女战记 第二季")
    assert ev.to_legacy_dict() == {
        "event": "anime_cancel_request",
        "data": {"request_id": "r1", "title": "幼女战记 第二季"},
    }
    assert event_from_legacy(ev.to_legacy_dict()) == ev


def test_events_carry_no_secrets():
    ev = AnimeRequestEvent(
        request_id="r", query="q", title="t", episode_key="k", source="mikan",
        locator="magnet:?xt=urn:btih:" + "b" * 40)
    keys = set(ev.to_legacy_dict()["data"])
    assert not (keys & {"cookie", "password", "sessdata", "secret", "token"})

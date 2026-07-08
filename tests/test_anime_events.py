"""Phase 3: anime events register_event round-trip + no-secrets invariant."""

from __future__ import annotations

from spica.core.anime_events import AnimeReadyEvent, AnimeRequestEvent
from spica.core.events import event_from_legacy


def test_anime_request_round_trip():
    ev = AnimeRequestEvent(
        request_id="r1", query="无职转生", title="无职转生 S3E1",
        episode_key="无职转生|s3|e1", source="mikan",
        locator="magnet:?xt=urn:btih:" + "a" * 40, display_title="无职转生 S3E1",
        size_bytes=700, created_at="2026-07-06T00:00:00")
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
    assert back == ev and back.error == "disk full"


def test_events_carry_no_secrets():
    ev = AnimeRequestEvent(
        request_id="r", query="q", title="t", episode_key="k", source="mikan",
        locator="magnet:?xt=urn:btih:" + "b" * 40)
    keys = set(ev.to_legacy_dict()["data"])
    assert not (keys & {"cookie", "password", "sessdata", "secret", "token"})

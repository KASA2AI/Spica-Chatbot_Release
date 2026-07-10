"""Qt-free tests for yt-dlp rolling throughput health."""

from __future__ import annotations

import math

from spica.anime.download_health import (
    DownloadHealthMonitor,
    DownloadProgressSample,
)


MIB = 1024 * 1024


def _sample(
    observed_at: float,
    downloaded_bytes: int | None,
    *,
    status: str = "downloading",
    format_id: str | None = "video",
    tmpfilename: str | None = "episode.video.mp4.part",
) -> DownloadProgressSample:
    return DownloadProgressSample(
        observed_at=observed_at,
        status=status,
        downloaded_bytes=downloaded_bytes,
        format_id=format_id,
        tmpfilename=tmpfilename,
    )


def _monitor() -> DownloadHealthMonitor:
    return DownloadHealthMonitor(
        min_rate_bytes_per_second=512 * 1024,
        window_seconds=15,
    )


def test_sustained_low_rate_requires_the_complete_window():
    health = _monitor()

    assert health.observe(_sample(0, 0)) is False
    assert health.observe(_sample(14.9, int(256 * 1024 * 14.9))) is False
    assert health.observe(_sample(15, 256 * 1024 * 15)) is True


def test_recovered_rate_does_not_reconnect():
    health = _monitor()

    assert health.observe(_sample(0, 0)) is False
    assert health.observe(_sample(10, 256 * 1024 * 10)) is False
    assert health.observe(_sample(15, 8 * MIB)) is False


def test_one_recovered_interval_breaks_the_continuous_low_speed_window():
    health = _monitor()

    assert health.observe(_sample(0, 0)) is False
    assert health.observe(_sample(14, 0)) is False
    # The latest adjacent interval is 600 KiB/s. A 15-second start/end average
    # would still look slow here, but the connection has recovered.
    assert health.observe(_sample(15, 600 * 1024)) is False
    assert health.observe(_sample(29, 600 * 1024)) is False
    assert health.observe(_sample(30, 600 * 1024)) is True


def test_rate_equal_to_the_floor_is_healthy():
    health = _monitor()

    assert health.observe(_sample(0, 0)) is False
    assert health.observe(_sample(15, 512 * 1024 * 15)) is False


def test_video_to_audio_switch_resets_the_window():
    health = _monitor()

    assert health.observe(_sample(0, 0)) is False
    assert health.observe(_sample(15, 256 * 1024 * 15)) is True

    assert health.observe(_sample(
        16,
        0,
        format_id="audio",
        tmpfilename="episode.audio.m4a.part",
    )) is False
    assert health.observe(_sample(
        30,
        256 * 1024 * 14,
        format_id="audio",
        tmpfilename="episode.audio.m4a.part",
    )) is False


def test_tmpfilename_change_resets_even_when_format_id_is_unchanged():
    health = _monitor()

    assert health.observe(_sample(0, 0)) is False
    assert health.observe(_sample(15, 256 * 1024 * 15)) is True
    assert health.observe(_sample(
        16,
        0,
        tmpfilename="episode.second-fragment.mp4.part",
    )) is False


def test_format_change_resets_even_when_tmpfilename_is_unchanged():
    health = _monitor()

    assert health.observe(_sample(0, 0)) is False
    assert health.observe(_sample(15, 256 * 1024 * 15)) is True
    assert health.observe(_sample(16, 0, format_id="audio")) is False


def test_finished_and_byte_regression_reset_the_window():
    health = _monitor()

    assert health.observe(_sample(0, 0)) is False
    assert health.observe(_sample(15, 256 * 1024 * 15)) is True
    assert health.observe(_sample(16, 256 * 1024 * 15, status="finished")) is False

    assert health.observe(_sample(17, 4 * MIB)) is False
    assert health.observe(_sample(20, 1 * MIB)) is False
    assert health.observe(_sample(34, 1 * MIB + 256 * 1024 * 14)) is False


def test_missing_byte_counter_and_disabled_floor_never_trigger():
    health = _monitor()

    assert health.observe(_sample(0, 0)) is False
    assert health.observe(_sample(15, None)) is False

    disabled = DownloadHealthMonitor(
        min_rate_bytes_per_second=0,
        window_seconds=15,
    )
    assert disabled.observe(_sample(0, 0)) is False
    assert disabled.observe(_sample(60, 0)) is False


def test_invalid_sample_resets_instead_of_poisoning_the_window():
    for bad_time in (math.nan, math.inf, -math.inf):
        health = _monitor()
        assert health.observe(_sample(0, 0)) is False
        assert health.observe(_sample(bad_time, 1024)) is False
        assert health.observe(_sample(16, 256 * 1024 * 16)) is False

    health = _monitor()
    assert health.observe(_sample(0, 0)) is False
    assert health.observe(_sample(5, -1)) is False
    assert health.observe(_sample(15, 256 * 1024 * 15)) is False

"""P0b step 2b pins: the song config is resolved ONCE by the host and the
injected instance is what the production SongWorker chain uses.

- SongPipeline(config=...) uses the injected dict and never falls back to
  load_song_config;
- SongWorker passes its injected config into the pipeline (no bare
  SongPipeline() construction left on the production path);
- SongController threads its song_config into every SongWorker it starts;
- _request_song reads search.limit from the resolved config (single source --
  the closure used to hardcode the netease default while the pipeline read
  the config value).
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import patch

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
PySide6 = pytest.importorskip("PySide6")

from agent_tools.function_tools.song.models import SongRequest  # noqa: E402
from agent_tools.function_tools.song.pipeline import SongPipeline  # noqa: E402
from spica.host.app_host import AppHost  # noqa: E402
from ui.workers.song_worker import SongWorker  # noqa: E402


def _song_config(tmp_path, limit=20):
    return {
        "enabled": True,
        "generated_root": str(tmp_path / "generated"),
        "applio_root": str(tmp_path / "applio"),
        "search": {"limit": limit, "bitrate": 320000},
    }


def _request():
    return SongRequest(query="稻香", title="稻香", artist="周杰伦", user_text="唱稻香")


# -- pipeline: injected dict, no fallback load -----------------------------------


def test_song_pipeline_uses_injected_config_without_loading(tmp_path):
    config = _song_config(tmp_path)
    with patch(
        "agent_tools.function_tools.song.pipeline.load_song_config",
        side_effect=AssertionError("injected path must not re-load config"),
    ):
        pipeline = SongPipeline(config=config)
    assert pipeline.config is config  # the very instance, not a re-load
    assert (tmp_path / "generated" / "cache").is_dir()  # dirs from injected root


def test_song_pipeline_none_still_falls_back(tmp_path):
    sentinel = _song_config(tmp_path)
    with patch(
        "agent_tools.function_tools.song.pipeline.load_song_config", return_value=sentinel
    ) as load:
        pipeline = SongPipeline()
    assert pipeline.config is sentinel
    load.assert_called_once()


# -- worker: production path hands the injected config to the pipeline ----------


def test_song_worker_passes_injected_config_to_pipeline(tmp_path):
    config = _song_config(tmp_path)
    constructed = []

    class _RecordingPipeline:
        def __init__(self, config_path=None, config=None):
            constructed.append(config)

        def run(self, request, cancellation, progress=None):
            return SimpleNamespace(ok=True, to_payload=lambda: {"ok": True})

    worker = SongWorker(_request(), job_id=7, config=config)
    with patch("ui.workers.song_worker.SongPipeline", _RecordingPipeline):
        worker.run()
    assert constructed == [config]
    assert constructed[0] is config


# -- controller: song_config threads into every worker it starts ----------------


def test_song_controller_threads_config_to_worker(tmp_path):
    from ui.controllers.song_controller import SongController

    config = _song_config(tmp_path)
    captured = []

    class _CapturingWorker:
        def __init__(self, request, job_id, parent, config=None):
            captured.append(config)
            self.request = request
            self.job_id = job_id
            self.progress = SimpleNamespace(connect=lambda *a, **k: None)
            self.completed = SimpleNamespace(connect=lambda *a, **k: None)
            self.failed = SimpleNamespace(connect=lambda *a, **k: None)
            self.finished = SimpleNamespace(connect=lambda *a, **k: None)

        def start(self):
            pass

        def isRunning(self):  # noqa: N802 -- Qt name
            return False

    controller = SongController(
        parent=None,
        chat_stream_controller=None,
        audio_controller=SimpleNamespace(stop_owner=lambda owner: None),
        set_song_status=lambda text: None,
        set_busy=lambda busy: None,
        focus_input=lambda: None,
        stop_conversation_for_song=lambda: None,
        voice_mode_active_provider=lambda: False,
        schedule_voice_recording=lambda ms: None,
        song_config=config,
    )
    with patch("ui.controllers.song_controller.SongWorker", _CapturingWorker):
        controller.start_song_request(_request())
    assert captured == [config]
    assert captured[0] is config


# -- host closure: search limit single-sourced from the resolved config ---------


def test_request_song_reads_search_limit_from_config():
    host = AppHost()
    host.song_config = {"search": {"limit": 7}}
    seen_limits = []

    def fake_search(request, limit=20):
        seen_limits.append(limit)
        return SimpleNamespace(title="稻香", artist_text="周杰伦")

    host._song_search = fake_search
    result = host._request_song("稻香")
    assert seen_limits == [7]
    assert result == {"title": "稻香", "artist": "周杰伦"}


def test_app_host_resolves_song_config_at_construction():
    host = AppHost()
    assert isinstance(host.song_config, dict)
    assert int(host.song_config["search"]["limit"]) == 20  # today's effective value

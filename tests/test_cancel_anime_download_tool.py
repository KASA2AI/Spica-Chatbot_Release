"""Public contract for the stop-current-anime-download act tool."""

from __future__ import annotations

import json
import threading
from types import SimpleNamespace

from memory.recent import RecentMemory
from spica.adapters.tools.cancel_anime_download import (
    CANCEL_ANIME_DOWNLOAD_SCHEMA,
    CancelAnimeDownloadTool,
)
from spica.config.schema import AnimeConfig, AppConfig
from spica.host.assemblies import anime as anime_assembly
from spica.plugins.registry import CapabilityRegistry
from spica.ports.model import ToolProbeResult
from spica.runtime.context import TurnContext, TurnRequest
from spica.runtime.deps import TurnDeps
from spica.runtime.exec_strategy import Inline
from spica.runtime.services import AgentServices
from spica.runtime.tools import RegistryToolSet
from spica.runtime.turn import run_turn
from agent_tools.function_tools.screen.schema import ScreenToolError


def test_cancel_anime_download_has_strict_no_argument_schema():
    assert CANCEL_ANIME_DOWNLOAD_SCHEMA["name"] == "cancel_anime_download"
    assert CANCEL_ANIME_DOWNLOAD_SCHEMA["strict"] is True
    assert CANCEL_ANIME_DOWNLOAD_SCHEMA["parameters"] == {
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": False,
    }


def test_cancel_anime_download_forwards_host_submission_ack():
    tool = CancelAnimeDownloadTool(
        lambda request_id: {"status": "submitted", "request_id": request_id})
    tool.bind_offer("r1")

    assert tool.run() == {"status": "submitted", "request_id": "r1"}


def test_offer_cannot_be_consumed_from_a_different_thread():
    calls = []
    tool = CancelAnimeDownloadTool(
        lambda request_id: calls.append(request_id) or {"request_id": request_id})
    tool.bind_offer("A")
    caught = []

    def forced_cross_thread_call():
        try:
            tool.run()
        except ScreenToolError as exc:
            caught.append(exc.code)

    thread = threading.Thread(target=forced_cross_thread_call)
    thread.start()
    thread.join(timeout=1)

    assert not thread.is_alive()
    assert caught == ["ANIME_CANCEL_REQUEST_STALE"]
    assert calls == []
    # The main thread's offer was not stolen by the other thread.
    assert tool.run() == {"request_id": "A"}


def test_concurrent_turn_offers_keep_request_ids_thread_local():
    seen = []
    seen_lock = threading.Lock()
    barrier = threading.Barrier(2)

    def submit(request_id):
        with seen_lock:
            seen.append(request_id)
        return {"request_id": request_id}

    tool = CancelAnimeDownloadTool(submit)
    results = []

    def turn(request_id):
        tool.bind_offer(request_id)
        barrier.wait(timeout=1)
        results.append(tool.run()["request_id"])

    threads = [
        threading.Thread(target=turn, args=("A",)),
        threading.Thread(target=turn, args=("B",)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=1)

    assert all(not thread.is_alive() for thread in threads)
    assert sorted(results) == ["A", "B"]
    assert sorted(seen) == ["A", "B"]


class _NoopMemory:
    def retrieve(self, _scope, _query, limit):
        del limit
        return []

    def commit_turn(self, _scope, _user_text, _assistant_text, meta=None):
        del meta
        return {"committed": True}


class _NoopPlayer:
    def play_file(self, _path):
        return None


class _SwitchActiveDuringProbeModel:
    """Responses-family fake at the model port, around the real run_turn."""

    def __init__(self, active):
        self._active = active
        self.followup_prompt = ""

    def probe_stream(self, _prompt, _tools, _state):
        return None

    def probe(self, _prompt, tools, _state):
        assert "cancel_anime_download" in {
            str(schema.get("name") or "") for schema in tools
        }
        assert self._active["request_id"] == "A"
        # The model received the A-bound capability.  Its response is delayed
        # until the UI has already advanced the live single-flight slot to B.
        self._active.clear()
        self._active.update(request_id="B", title="第二集")
        return ToolProbeResult(
            calls=[{"name": "cancel_anime_download", "arguments": "{}"}],
            text="",
        )

    def stream(self, prompt, _state):
        self.followup_prompt = prompt
        yield json.dumps({
            "answer": "停止请求没有落到别的任务上。",
            "emotion": "neutral",
            "emotion_reason": "stale cancel was rejected",
        }, ensure_ascii=False)


def test_real_run_turn_binds_cancel_offer_to_a_and_never_redirects_to_b(
        tmp_path):
    """The production turn path must carry offer identity across model delay."""
    active = {"request_id": "A", "title": "第一集"}
    emitted = []
    host = SimpleNamespace(
        config=SimpleNamespace(anime=AnimeConfig(
            enabled=False,
            download_dir=str(tmp_path / "downloads"),
            library_file=str(tmp_path / "state" / "library.json"),
        )),
        secrets=SimpleNamespace(
            bilibili_cookie=None, qbittorrent_password=None),
        registry=CapabilityRegistry(),
        _anime_sink=emitted.append,
        _anime_in_flight=lambda: dict(active),
    )
    anime_assembly.install(
        host, sources=[], torrent=object(), player=_NoopPlayer())

    recent = RecentMemory(max_turns=3)
    memory = _NoopMemory()
    model = _SwitchActiveDuringProbeModel(active)
    config = AppConfig()
    services = AgentServices(
        llm_client=None,
        tts_adapter=None,
        visual_tool=None,
        memory_store=None,
        recent_memory=recent,
        config={
            "model": config.llm.model,
            "character_profile": "test profile",
            "recent_context_limit": 3,
            "long_term_memory_limit": 5,
            "max_tool_rounds": 3,
            "character_id": "spica",
            "interlocutor_name": "麦",
        },
        logger=lambda *_args, **_kwargs: None,
        tool_registry=host.registry,
    )
    deps = TurnDeps(
        config=config,
        llm=None,
        tts=None,
        visual=None,
        memory=memory,
        recent=recent,
        tools=RegistryToolSet(host.registry),
        model=model,
        context_contributors=(),
        llm_ready=True,
    )

    events = list(run_turn(
        TurnContext(TurnRequest(
            conversation_id="default",
            user_input="不要下了，停掉当前动漫下载",
            include_user_time_context=False,
        )),
        services,
        exec_strategy=Inline(),
        deps=deps,
    ))

    assert any(event.kind == "done" for event in events), events
    assert "ANIME_CANCEL_REQUEST_STALE" in model.followup_prompt
    assert emitted == []

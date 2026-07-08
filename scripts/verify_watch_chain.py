"""verify_watch_chain.py -- 离线单步走真机同款流式链路的活体诊断器。

用法:python scripts/verify_watch_chain.py
下次再出现"工具不触发",先跑它:每一站(schemas 供给 → LLM 请求体 → 工具执行 →
followup)打印实际值,断点直接现形。它就是钉死 FINDINGS #18(chat_completions
路径整段丢工具)用的脚本。

真件:CapabilityRegistry + RegistryToolSet + 真 WatchGameScreenTool + 真 ChatEngine +
真 orchestrator 流式链(stream_voice,与 ChatWorker 同款入口)。
fake:LLM client(记录每次被调的方法/参数)、TTS/Visual、watch 的截图/分析端口。

场景 A:Responses 形态 client(标准 OpenAI)——预期 probe 带 tools,工具执行,capturing 行。
场景 B:DeepSeek 形态 client(base_url 含 deepseek)——修复(FINDINGS #18)后预期:
        chat.completions probe 带 tools(嵌套 function 格式)→ tool_call → 工具执行 →
        followup 流式。修复前此场景只有 1 次无 tools 调用。
"""

from __future__ import annotations

import json
import logging
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

from PIL import Image

from agent_tools.function_tools import TOOL_SCHEMAS, default_tool_functions
from agent_tools.tts.schemas import TTSRequest, TTSResult
from memory.recent import RecentMemory
from memory.store import SQLiteMemoryStore
from spica.adapters.game_memory.sqlite import GameMemorySqliteAdapter
from spica.adapters.llm.openai_compatible import _prefers_chat_completions
from spica.adapters.tools.screen import InspectScreenTool
from spica.adapters.screen import LocalMoondreamScreenAnalysis
from spica.adapters.tools.watch_game_screen import WatchGameScreenTool
from spica.config.schema import AppConfig
from spica.core.chat_engine import ChatEngine
from spica.galgame.models import game_conversation_id
from spica.galgame.session import GalgameState
from spica.plugins.registry import CapabilityRegistry
from spica.ports.screen_capture import CaptureImage
from spica.ports.window_locator import WindowGeometry
from spica.runtime.context import GameContextRequest, GameTurnBinding
from spica.runtime.services import AgentServices
from spica.runtime.window import WatchContext, WindowTarget

RAW_ANSWER = json.dumps({"answer": "画面上是个女孩。", "emotion": "happy", "emotion_reason": "x"}, ensure_ascii=False)
QUESTION = "现在画面有什么"


# ---- fake LLM clients(记录每一次调用)------------------------------------- #

class _Recorder:
    def __init__(self, label):
        self.label = label
        self.calls = []

    def note(self, method, kwargs):
        tools = kwargs.get("tools")
        tool_names = (
            [t.get("name") or (t.get("function") or {}).get("name") for t in tools]
            if tools else None
        )
        nested = bool(tools) and all(isinstance(t.get("function"), dict) for t in tools)
        line = (f"[站b] {self.label}: LLM 被调 -> {method}  stream={kwargs.get('stream')}  "
                f"[站c] tools字段={'有(嵌套function格式)' if nested else ('有' if tools else '无')}"
                + (f" 内容={tool_names}" if tool_names else ""))
        print(line)
        self.calls.append((method, kwargs))


def _stream_events(text):
    for i in range(0, len(text), 12):
        yield SimpleNamespace(type="response.output_text.delta", delta=text[i:i + 12])
    yield SimpleNamespace(
        type="response.completed",
        response=SimpleNamespace(id="done", output_text=text, usage=None),
    )


class _ResponsesAPI:
    """场景 A:Responses 形态。probe(带 tools)→ 返回 function_call;流式 → deltas。"""

    def __init__(self, recorder):
        self._recorder = recorder

    def create(self, **kwargs):
        self._recorder.note("responses.create", kwargs)
        if kwargs.get("stream"):
            return iter(_stream_events(RAW_ANSWER))
        if kwargs.get("tools"):
            # 模拟 LLM 决定调用 watch_game_screen(站 d 的输入)
            return SimpleNamespace(
                id="probe",
                output=[SimpleNamespace(
                    type="function_call", name="watch_game_screen",
                    arguments=json.dumps({"question": QUESTION}, ensure_ascii=False),
                )],
                output_text="", usage=None,
            )
        return SimpleNamespace(id="oneshot", output=[], output_text=RAW_ANSWER, usage=None)


class _ChatCompletionsAPI:
    def __init__(self, recorder):
        self._recorder = recorder
        self.completions = self

    def create(self, **kwargs):
        self._recorder.note("chat.completions.create", kwargs)
        if kwargs.get("stream"):
            def chunks():
                for i in range(0, len(RAW_ANSWER), 12):
                    yield SimpleNamespace(choices=[SimpleNamespace(
                        delta=SimpleNamespace(content=RAW_ANSWER[i:i + 12]))])
            return chunks()
        if kwargs.get("tools"):
            # 模拟 DeepSeek 在 chat 工具 probe 上决定调用 watch_game_screen
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
                content="",
                tool_calls=[SimpleNamespace(id="call_1", type="function",
                    function=SimpleNamespace(
                        name="watch_game_screen",
                        arguments=json.dumps({"question": QUESTION}, ensure_ascii=False)))],
            ))], usage=None)
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=RAW_ANSWER))], usage=None)


def make_client(shape):
    recorder = _Recorder(shape)
    if shape == "responses(openai形态)":
        client = SimpleNamespace(base_url="https://api.openai.com/v1",
                                 responses=_ResponsesAPI(recorder))
    else:
        client = SimpleNamespace(base_url="https://api.deepseek.com/v1",
                                 responses=_ResponsesAPI(recorder),
                                 chat=_ChatCompletionsAPI(recorder))
    return client, recorder


# ---- 其余真件/假件 ----------------------------------------------------------- #

class _FakeTTS:
    name = "verify_tts"

    def synthesize(self, request):
        assert isinstance(request, TTSRequest)
        return TTSResult(ok=True, provider=self.name, audio_url="/x.wav", audio_path="/tmp/x.wav",
                         chunks=[{"index": 0, "text": request.text, "audio_url": "/x.wav", "audio_path": "/tmp/x.wav"}],
                         timing={"tts_total_ms": 1.0}, duration_ms=1.0)


class _FakeVisual:
    def prepare_stream_context(self, requested_costume=None, requested_mode=None):
        return {"costume": "school", "costume_mode": "fixed", "dialog": {}, "character": {}, "classifier_version": "x"}

    def build_unit_visual_payload(self, **kwargs):
        return {"costume": "school", "costume_mode": "fixed", "classifier_version": "x",
                "selection_source": "x", "selection_error": None,
                "classifier": {"duration_ms": 1.0, "confidence": 0.9, "signals": []},
                "dialog": {}, "character": {},
                "cue": {"index": kwargs["unit_index"], "text": kwargs["current_unit_text"],
                        "expression_id": "002", "hand_pose": "normal", "image_url": "/x.png", "reason": "x"}}


class _WatchLocator:
    def get_window_geometry(self, window_id):
        print(f"[站d] 工具执行链: locator.get_window_geometry({window_id!r}) 被调")
        return WindowGeometry(0, 0, 320, 200)


class _WatchCapture:
    def capture_rect(self, left, top, width, height):
        print(f"[站d] 工具执行链: capture_rect({left},{top},{width},{height}) 被调")
        img = Image.new("RGB", (width, height), (30, 30, 30))
        return CaptureImage(image=img, width=width, height=height)


class _WatchAnalysis:
    def analyze_image(self, image, mode, prompt=None, **kwargs):
        print(f"[站d] 工具执行链: Moondream 分析端口被调 mode={mode!r} question={prompt!r}")
        return {"schema_version": "screen_observation.v1", "request": {"target": mode},
                "capture": {"source": "automatic_screenshot"},
                "followup": {"context_for_next_turn": "画面上是一个女孩。"}}


def build_engine(client, tmp):
    registry = CapabilityRegistry()
    registry.register_tool(InspectScreenTool(LocalMoondreamScreenAnalysis()).schema(), lambda **kw: "x")
    watch = WatchGameScreenTool(
        _WatchAnalysis(),
        # privacy gate (review #1); Phase 8-c2: named WatchContext carrier
        lambda: WatchContext(
            target=WindowTarget(window_id="0x07e00005", owner_domain="galgame",
                                game_id="limelight"),
            locator=_WatchLocator(), capture=_WatchCapture(),
            state=GalgameState.PLAYING,
        ),
    )
    registry.register_tool(watch.schema(), watch.run, available=lambda: True, intent_gated=False)
    services = AgentServices(
        llm_client=client, tts_adapter=_FakeTTS(), visual_tool=_FakeVisual(),
        memory_store=SQLiteMemoryStore(Path(tmp) / "m.sqlite3"),
        recent_memory=RecentMemory(max_turns=3),
        game_memory_adapter=GameMemorySqliteAdapter(Path(tmp) / "g.sqlite3"),
        config={"model": "test-model", "character_profile": "p", "recent_context_limit": 3,
                "long_term_memory_limit": 5, "max_tool_rounds": 3, "character_id": "spica",
                "interlocutor_name": "麦"},
        logger=lambda *a, **k: None,
        tool_functions=default_tool_functions(), tool_schemas=TOOL_SCHEMAS,
    )
    services.tool_registry = registry
    engine = ChatEngine(services, AppConfig())
    engine.set_game_binding_provider(lambda: GameTurnBinding(
        conversation_id=game_conversation_id("limelight"),
        game_context_request=GameContextRequest(mode="active", game_id="limelight"),
    ))
    return engine


def run_scene(shape):
    print(f"\n{'=' * 72}\n场景 [{shape}]\n{'=' * 72}")
    client, recorder = make_client(shape)
    print(f"[判定] _prefers_chat_completions(client) = {_prefers_chat_completions(client)}  "
          f"(base_url={client.base_url})")
    with tempfile.TemporaryDirectory() as tmp:
        engine = build_engine(client, tmp)
        offered = [s.get("name") for s in engine.deps.tools.schemas_for_user_text(QUESTION)]
        print(f"[站a] schemas_for_user_text({QUESTION!r}) = {offered}")
        events = list(engine.stream_voice(QUESTION))  # ChatWorker 同款入口
        names = [e.get("event") for e in events]
        done = next((e for e in events if e.get("event") == "done"), None)
        answer = (done or {}).get("data", {}).get("answer", "")
        print(f"[结果] 事件流: {names[:6]}…  最终 answer={answer!r}")
        summary = [
            (method, "带tools" if kwargs.get("tools") else "无tools", f"stream={kwargs.get('stream')}")
            for method, kwargs in recorder.calls
        ]
        print(f"[结果] LLM 共被调 {len(recorder.calls)} 次: {summary}")


if __name__ == "__main__":
    run_scene("responses(openai形态)")
    run_scene("deepseek形态(真机嫌疑)")

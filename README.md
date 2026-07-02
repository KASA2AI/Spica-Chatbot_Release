# Spica Chatbot

Spica Chatbot 是一个本地桌面语音角色扮演陪伴应用。它用 PySide6 提供透明置顶 Overlay，用 OpenAI 兼容接口生成角色回复，并把回复拆成可播放单元，驱动本地立绘差分、GPT-SoVITS 语音合成、短期/长期记忆、屏幕观察、点歌翻唱和可选语音输入。**项目核心是 galgame 陪玩**：Spica 可以绑定游戏窗口、实时 OCR 剧情、看画面回答问题、把共同经历写进记忆、后台总结剧情并在游玩结束后留下履历；唱完歌还会主动开口收尾（turn 发起器）。

当前代码已经完成平台化重构的主要骨架：UI 不再直接组装 LLM/TTS/Visual/Memory 服务，后端由 `AppHost` 统一装配，核心对话由 `ChatEngine` 驱动，能力通过 ports/adapters 和 `CapabilityRegistry` 注册，跨 Host 到 UI 的运行事件用 `RuntimeEvent` dataclass 表达。

核心对话 turn 也已完成绞杀式硬化（C0–C8）：一次对话跑在类型化的 `TurnContext` 上，唯一 emit 路径是 `run_turn`，同步路径是把流式事件 `fold` 成响应；并发（`ExecStrategy`）、可观测（`TurnObserver`）、长期记忆后台化（`JobRunner`，recent memory 仍同步）、工具（registry-backed `ToolSet`，`inspect_screen` 是首个 `ToolPort`）都是注入的能力；旧的 `agent/` 包已删除，纯 domain 在 `spica/conversation/`、运行时在 `spica/runtime/`，边界由 AST 守卫测试钉死。

## 当前状态

- 桌面入口：`webui_qt.py` -> `ui/qt_overlay.py`。
- 组装根：`spica.host.app_host.AppHost.initialize()`。
- 对话核心：`spica.core.chat_engine.ChatEngine`。
- 流式运行时：`spica.runtime.orchestrator.stream_voice_events()`。
- 对话 turn：`spica.runtime.turn.run_turn`（唯一事件路径）、`spica.runtime.context.TurnContext`（类型化子上下文）、`spica.runtime.deps.TurnDeps`（注入 config/ports/observer/jobs/tools）。
- 配置入口：`data/config/app.yaml`（typed config 唯一 app 级载体）+ `xiaosan.env`（只装密钥）+ `ui/overlay_config.json`（UI 偏好）；env 只作 override，经 `spica.config.manager.ConfigManager` 解析。
- 能力注册：`spica.plugins.registry.CapabilityRegistry`。
- 角色包：`spica.core.character.CharacterPackage`，默认使用 `spica_data/Spica_skill`。
- UI 状态：`spica.core.state_machine.ChatStateMachine` 驱动忙碌、生成、播放、暂停和错误状态。
- galgame 陪玩：`spica.galgame.companion_controller.GalgameCompanionController`（start/stop 编排）+ `session.py`（FSM 唯一状态 owner）+ `ocr_loop.py`（串行观察泵）；事件经 `CompanionEventBridge` 跨线程进 UI。
- 主动开口：`spica.core.proactive`（请求/仲裁/全双工钩子位）+ `ChatEngine.stream_system_turn`（`interaction_mode="system"`）。

## 功能

- PySide6 透明置顶桌面 Overlay，包含立绘、对白框、输入框、截图按钮、语音按钮、窗口控制和设置面板。
- OpenAI 兼容 LLM adapter，支持 Responses API，并对 DeepSeek 这类 Chat Completions 兼容客户端做分支适配。
- 流式生成播放：LLM delta -> JSON answer 提取 -> 播放单元切分 -> 并行立绘选择和 TTS -> 按 index 顺序播放。
- GPT-SoVITS 本地日语 TTS，按情绪选择参考音频，支持启动预热。
- 本地立绘差分选择，基于回复文本和情绪投票选择表情、手势、服装、对白样式。
- RecentMemory 短期上下文和 SQLite 长期记忆，长期记忆通过 `MemoryPort` 按 `character_id::conversation_id` 隔离。
- **galgame 陪玩**：🎮 入口选窗绑定游戏 → 框选校准对白区 → OCR 剧情流（稳定行去重 + 说话人解析）→ 剧情后台总结 → 游玩履历写入角色记忆；崩溃残留 session 下次启动自动补总结。游戏记忆走独立库（`spica_data/galgame.sqlite3`），不污染角色长期记忆。
- **工具调用（多轮工具轮）**：LLM 经 probe → 本地执行 → followup 的工具轮决策调用工具；`chainable` 工具可多轮链式（上限 `max_tool_rounds`，超限优雅收尾）。内置四工具：`inspect_screen`(read，看整个屏幕) / `watch_game_screen`(read，陪玩时看绑定的游戏窗口) / `note_game_observation`(write，把对话确认的观察写进游戏记忆) / `sing_song`(act，点歌)。工具按 `effect`（read/write/act）分级，操作类工具一律「专用 port 白名单动作 + host 闭包持权限」。
- 本地屏幕观察全部走本地截图、RapidOCR 和 Moondream，不上传图片；手动截图附件可框选区域随下一条消息发送。
- **点歌/翻唱（B2 工具化后）**：主 LLM 通过 `sing_song` function call 点歌（自然对话，无前置劫持）→ 网易云搜索下载 → 人声分离 → Applio/RVC 变声 → 混音 → 自动播放；播放中「暂停/继续/停止/重唱」走零延迟控制快路径；唱完她会**主动开口**收尾。
- **主动开口（turn 发起器）**：系统事件可触发她主动说话（`ProactiveTurnRequest` → busy 仲裁 → 同一条 run_turn 对话路径生成角色化台词）；v1 策略 drop_if_busy，首个用例是唱完播报。
- 可选 ReSpeaker USB 4 Mic Array 语音输入，使用硬件 VAD 和 `speech_recognition` 中文识别。**当前为半双工**：她说话/唱歌期间语音输入暂停，播放结束自动恢复（全双工在档未做）。
- 插件入口：插件可注册 adapters/tools，当前阶段不开放 UI widget 插件。

## 架构

```mermaid
flowchart TD
    User[用户输入/截图/语音] --> UI[PySide6 Overlay]
    UI --> Host[AppHost]
    Host --> Config[ConfigManager + Secrets]
    Host --> Registry[CapabilityRegistry]
    Registry --> LLM[LLMPort: OpenAICompatibleAdapter]
    Registry --> TTS[TTSPort: GPT-SoVITS / dummy]
    Registry --> Visual[VisualPort: SpicaDiff]
    Registry --> Memory[MemoryPort: SQLite]
    Host --> Engine[ChatEngine]
    Engine --> Runtime[stream_voice_events]
    Runtime --> Tools[tool_round 多轮工具轮: inspect/watch/note/sing_song]
    Runtime --> Splitter[JsonAnswerExtractor + PlayUnitSplitter]
    Splitter --> Jobs[Visual job + TTS job]
    Jobs --> Events[RuntimeEvent / legacy dict bridge]
    Events --> Controller[ChatStreamController]
    Controller --> Playback[Typewriter + Audio + Character Image]
```

### 分层约束

`spica/` 是平台核心，不能 import PySide、PyQt、shiboken 或其他 GUI 库。这个约束由 `tests/test_layering.py` 守住。

业务代码不能直接读取 `os.getenv()` 或 `os.environ`。环境只能由 `spica/config` 三件碰：`manager.py`、`secrets.py`、`runtime_env.py`（vendored 运行时的 env 写垫片）；env 名册单一居所是 `spica/config/env_roster.py`。其他层必须通过 `AppConfig` 或 `Secrets` 获取配置。这个约束由 `tests/test_no_getenv.py` 守住（扫 spica/memory/agent_tools/ui/hardware，临时白名单为空）。

Host 只做组装和窄接口转发，业务逻辑在 `ChatEngine`、runtime 组件、adapters、memory 和 tool 模块中。

### 主要目录

```text
.
├── webui_qt.py                         # 桌面启动入口，处理 Linux Qt/xcb/输入法/ALSA 环境
├── spica/
│   ├── host/                           # AppHost 组装根、backend assembly、ManagementSurface
│   ├── core/                           # ChatEngine、RuntimeEvent、ChatStateMachine、CharacterPackage
│   ├── conversation/                   # 纯 domain：prompt builder、reply parser、text normalizer、time context、character loader
│   ├── runtime/                        # 对话 turn：run_turn、TurnContext/TurnDeps、stages、流式编排、observer/jobs、TTS/Visual job、memory commit
│   ├── ports/                          # LLM/TTS/Visual/Memory/Tool + galgame 五端口（launcher/locator/capture/ocr/game_memory）
│   ├── adapters/                       # OpenAI 兼容 LLM、SQLite memory、GPT-SoVITS TTS、Spica visual、galgame 各 adapter、tools/（watch/note/sing_song 工具垫片）
│   ├── galgame/                        # 陪玩 domain：session FSM、OCR 泵、controller、总结、履历、绑定、校准（Qt-free）
│   ├── config/                         # Pydantic AppConfig、ConfigManager、Secrets
│   └── plugins/                        # CapabilityRegistry、PluginHost、plugin manifest
├── agent_tools/
│   ├── function_tools/screen/           # 本地截图、RapidOCR、Moondream screen pipeline
│   ├── function_tools/song/             # 点歌意图、网易云、分离、RVC、混音 pipeline
│   ├── tts/                             # TTSAdapter、GPT-SoVITS service、dummy adapter
│   └── visual/                          # VisualDiffService 本地立绘差分选择
├── memory/                             # RecentMemory、SQLiteMemoryStore、规则记忆抽取和去重
├── hardware/respeaker/                 # ReSpeaker 录音、USB control、Qt speech worker
├── ui/                                 # PySide6 UI、controllers、workers、models、widgets
├── scripts/                            # 活体诊断器（verify_watch_chain.py / diag_ocr_providers.py）+ 配置守门（dump_resolved_config.py，动配置解析前先 dump 基线）
├── data/config/                        # app.yaml（typed config 唯一 app 级载体）+ tts/visual YAML（角色数据文件）
├── config/                             # （旧 screen json 已迁入 app.yaml，仅存 *.migrated 回滚备份）
├── spica_data/                         # 角色卡、立绘、参考音频、本地记忆数据，发布仓库不带大素材
├── static/generated_voice/             # 对话 TTS 输出，运行时生成
├── static/generated_song/              # 点歌翻唱缓存和输出，运行时生成
├── third_party/                        # 第三方硬件辅助代码，发布仓库不带
├── tests/                              # 单元测试、golden、层级守卫、adapter 合同测试
└── build_release.sh                    # 历史发布脚本，发布规则见本文后面的“发布规则”
```

## 快速启动

### 1. 准备 Python 环境

项目当前开发环境是 conda `gptsovits`，Python 3.10/3.11 均可。示例：

```bash
cd /home/san/ai_code/Spica-Chatbot
conda activate gptsovits
pip install openai httpx python-dotenv pydantic PyYAML PySide6 soundfile numpy pytest
pip install -r requirements-screen.txt
```

可选语音输入依赖：

```bash
pip install SpeechRecognition PyAudio pyusb
```

如果要运行 GPT-SoVITS，还需要按你的 GPT-SoVITS 版本安装其依赖。发布仓库不会包含 GPT-SoVITS vendor 目录内容。

> **外置大文件（不在仓库内，需单独下载）**：
> - 语音识别权重 `spica_data/models/faster-whisper-large-v3-turbo/model.bin`（约 1.6GB，已 gitignore）。从 HuggingFace `Systran/faster-whisper-large-v3-turbo` 下载 `model.bin` 放入该目录即可（同目录的 `config.json` / `tokenizer.json` 等小文件已随仓库提供）。
> - GPT-SoVITS vendor 引擎目录同样不在仓库内，见上一段。

### 2. 准备环境变量

在仓库根目录创建 `xiaosan.env`：

```env
OPENAI_API_KEY=你的密钥
OPENAI_BASE_URL=https://api.openai.com/v1
MODEL=gpt-4.1-mini
```

`OPENAI_API_KEY` 由 `spica.config.secrets.load_secrets()` 读取。`OPENAI_BASE_URL`、`MODEL` 和其他可调参数由 `ConfigManager` 映射到 `AppConfig`。

### 3. 准备 TTS 运行时（GPT-SoVITS slim）

默认 TTS 配置在 `data/config/tts.yaml`。**当前默认运行时是裁剪后的 slim 产物**，不再是仓内 39G vendored 大树：

```text
artifacts/tts_slim/          # base + characters/spcia（gitignored 本地产物）
```

`tts.yaml` 的 `gptsovits_root` / `gpt_model_path` / `sovits_model_path` 已指向 `artifacts/tts_slim`。slim 由 `scripts/local_runtime/build_tts_slim.py` 从一份 GPT-SoVITS v2Pro 源树构建（源树保存在仓外、不随仓分发）。emotion 参考音频（`emotions.*.ref_audio_path` 等）仍在 `spica_data/voice`，与 slim/vendored 无关。跨平台可复现安装方式在打包阶段确定，见 `docs/LOCAL_RUNTIME_CUTOVER_REVIEW.md`。

如需回退到 vendored 大树，或你的模型文件名/目录不同，修改 `data/config/tts.yaml` 的：

- `gptsovits_root`
- `gpt_model_path`
- `sovits_model_path`
- `emotions.*.prompt_text_path`
- `emotions.*.ref_audio_path`
- `emotions.*.inp_refs_path`

### 4. 准备角色数据和素材

默认角色包目录是：

```text
spica_data/Spica_skill
```

需要包含：

```text
spica_data/Spica_skill/
├── meta.json
├── SKILL.md
├── self.md
└── persona.md
```

`meta.json` 支持字段：

```json
{
  "slug": "spica",
  "name": "辻倉朱比華",
  "char_name": "スピカ",
  "visual_config_path": null,
  "tts_config_path": null
}
```

`slug` 会作为 `character_id`，长期记忆会按角色隔离。`visual_config_path` 和 `tts_config_path` 可指向角色包内的专属配置；为空时使用 `data/config/visual.yaml` 和 `data/config/tts.yaml`。

立绘和语音素材默认从 `spica_data` 读取：

```text
spica_data/diffs/                         # 立绘差分、规则、UI 贴图
spica_data/voice/{happy,angry,sad,surprised}/
spica_data/memory.sqlite3                 # 运行时自动创建或迁移
```

发布仓库不会包含大体积 `spica_data` 素材，需要在本地补齐。

### 5. 启动 Overlay

```bash
python webui_qt.py
```

Linux ibus 环境可以用：

```bash
./run_ibus.sh
```

如果 Qt xcb 缺系统库，入口会提示安装 `libxcb-cursor0`。

### 6. 运行测试

只使用下面这条命令：

```bash
python -m pytest tests -q
```

不要在仓库根目录运行裸 `pytest`，它可能递归扫到 vendored GPT-SoVITS runtime，导致第三方包测试收集失败。

## 配置

> 配置体系已统一为三载体（P0b）：`data/config/app.yaml`（typed config，含 llm/memory/character/stream/galgame/screen/song/plugins 八节）+ `xiaosan.env`（只装密钥）+ `ui/overlay_config.json`（UI 偏好）。env 只作 override 且只经 `manager.py`/`secrets.py`；tts.yaml / visual.yaml 是角色数据文件（角色包可整文件覆盖），不算配置载体。改配置解析前先跑 `python scripts/dump_resolved_config.py --out <baseline>`，改完 `--diff` 零差异才算完。

### `data/config/tts.yaml`

控制 TTS provider、GPT-SoVITS 根目录、模型权重、输出目录、预热策略、情绪参考音频和切句参数。

常用字段：

- `provider`: 默认 `gptsovits_current`，测试可改为 `dummy`。
- `output_dir`: 默认 `../../static/generated_voice`。
- `warmup_on_startup`: 是否启动后预热。
- `warmup_emotion` / `warmup_emotions`: 预热情绪。
- `tts_params.sentence_chunking`: 长文本是否切分后送 TTS。
- `emotions`: `happy`、`angry`、`sad`、`surprised` 的 prompt 和参考音频。

### `data/config/visual.yaml`

控制本地立绘差分和 UI 演出素材。

常用字段：

- `diff_root`: 立绘差分根目录。
- `rules_path`: 表情和手势规则。
- `background_path`: 背景预览图。
- `costume_mode`: `random` 或 `fixed`。
- `selected_costume`: 固定服装模式使用的服装。
- `segments`: 非流式视觉 payload 的切段配置。
- `selection`: 差分平滑策略。
- `dialog`: 对白框 speaker、滤镜、颜色和透明度。
- `character`: 默认表情、默认手势、布局比例。

### app.yaml 的 `plugins:` 节

插件 manifest（旧载体 `data/config/plugins.yaml` 已迁入 app.yaml）。每个启用项会加载 `plugins/<name>/__init__.py` 并调用 `register(registry)`。

示例（写在 app.yaml）：

```yaml
plugins:
  - name: example_tts
    enabled: true
```

插件当前阶段只允许注册 adapters/tools，不开放 UI widget。

### `data/config/app.yaml`

typed config 的唯一 app 级文件载体（P0b 后包含 screen/song/plugins 三节——由 `scripts/migrate_config_p0b.py` 自旧 json/yaml 迁入）。键缺省时使用 `AppConfig` 默认值；环境变量覆盖文件值。

可选示例：

```yaml
llm:
  provider: openai_compatible
  model: gpt-4.1-mini
  base_url: https://api.openai.com/v1
memory:
  provider: sqlite
  recent_memory_turns: 3
  recent_context_limit: 3
  long_term_memory_limit: 5
  long_term_memory_budget_chars: 1200
  recent_turn_char_limit: 360
  max_long_term_memories: 200
character:
  interlocutor_name: 麦
  package_dir: spica_data/Spica_skill
stream:
  play_unit_min_chars: 18
  play_unit_max_chars: 96
  visual_stream_workers: 2
max_tool_rounds: 3
```

### app.yaml 的 `screen:` 节

控制本地 screen pipeline（旧载体 `config/screen_vision_config.json` 已迁入 app.yaml，`.migrated` 回滚备份已清理）：

- `provider`: 当前为 `moondream_local`。
- `device`: 当前设计为 `cuda`。
- `dtype`: 默认 `bfloat16`。
- `ocr_enabled`: 是否启用 RapidOCR。
- `debug_save_images`: 默认 `false`，不落盘调试截图。

### `ui/overlay_config.json`

控制桌面 Overlay 的角色缩放、UI 缩放、打字机速度和窗口初始比例。这是 UI 本地外观配置，不属于平台核心配置。

## 对话运行流程

### 同步路径

`ChatEngine.run_voice()` 用 `Inline` 执行策略驱动 `run_turn`，再用 `fold_events` 把产出的事件流折叠成一个完整响应 payload（见 `spica/core/chat_engine.py`）。主要用于一次性得到完整 payload。

非流式的 stage 链（`spica/runtime/sync_chain.py::run_voice_pipeline`，**已冻结：纯 golden 兼容锚，生产零调用方，不再长新能力**——生产同步入口是上面的 `run_voice`）依次执行：

```text
validate_input
load_recent_context
retrieve_long_term_memory
analyze_screen_attachment
build_prompt
call_llm
parse_reply
save_stream_memory
build_visual
synthesize_tts
build_response
```

### 流式路径

`ChatEngine.stream_voice_runtime()` 调用 `spica.runtime.orchestrator.stream_voice_events()`，输出 typed `RuntimeEvent`。

当前 UI 仍通过 `ChatEngine.stream_voice()` 消费 legacy dict，`ChatEngine` 会把 `RuntimeEvent` 转回旧 dict，保证 UI 兼容。

核心事件：

- `status`
- `unit_text_ready`
- `unit_visual_ready`
- `unit_audio_started`
- `unit_audio_ready`
- `unit_ready`
- `done`
- `error`

播放顺序由 `unit_ready.index` 保证。Visual job 可以并行，TTS job 串行，最终 `unit_ready` 按 index 有序进入 UI。

## 记忆

短期记忆由 `memory.recent.RecentMemory` 保存最近几轮对话。长期记忆由 `memory.store.SQLiteMemoryStore` 保存到 `spica_data/memory.sqlite3`。

重构后记忆写入通过 `spica.ports.memory.MemoryPort.commit_turn()`，SQLite adapter 内部执行规则抽取、upsert 去重和裁剪。Runtime 不负责抽取细节。

`MemoryScope` 包含：

- `character_id`
- `user_id`
- `conversation_id`

SQLite adapter 会把 conversation key 命名空间化为：

```text
{character_id}::{conversation_id}
```

因此不同角色不会串长期记忆。

## 插件和能力替换

内置能力注册在 `spica/host/builtins.py::register_builtin_adapters()`（早期文档写的 `AppHost._register_builtin_adapters()` 已重构搬出）：

- LLM: `openai_compatible`
- TTS: `gptsovits_current`、`gptsovits`、`current`、`dummy`
- Visual: `spica_diff`
- Memory: `sqlite`
- Tools: `inspect_screen`（builtins 注册）；`watch_game_screen` / `note_game_observation` / `sing_song` 在 `AppHost.__init__` 注册（它们的执行权限是 host 闭包）。注册元数据四维：`available` / `intent_gated` / `chainable` / `effect`。

插件示例：

```python
# plugins/example_tts/__init__.py

def register(registry):
    registry.register_tts("example_tts", lambda **kwargs: MyTTSAdapter(**kwargs))
```

启用后，在配置中把对应 provider 改成插件注册名即可。插件加载失败会被记录在 ManagementSurface，不会阻断启动。

## 屏幕观察

`inspect_screen` 只有在用户明确要求查看屏幕、桌面、显示器、当前画面、网页、报错等可见内容时才会被选中。

自动工具路径：

```text
capture_full_screen -> RapidOCR -> Moondream local -> screen observation JSON -> prompt 注入
```

手动截图路径：

```text
截图按钮 -> 用户框选区域 -> pending_screen_attachment -> 下一条聊天消息 -> analyze_screen_attachment
```

该链路默认本地运行，不把图片上传到主聊天模型。

## 点歌和翻唱

点歌入口在 `ui.controllers.song_controller.SongController`，意图路由在 `agent_tools.function_tools.song.intent_router.SongIntentRouter`。

完整 pipeline：

```text
用户点歌意图
-> SongIntentRouter
-> SongPipeline
-> 网易云搜索/下载
-> separate_vocals
-> Applio/RVC infer_spica_vocal
-> mix_vocal_with_instrumental
-> static/generated_song 输出
```

配置在 `data/config/app.yaml` 的 `song:` 节（旧载体 `song_config.json` 已迁入，仅存 `.migrated` 回滚备份），缺失字段会用 `agent_tools.function_tools.song.config.DEFAULT_CONFIG` 补齐。

**当前默认 RVC 运行时是 slim 产物 `artifacts/rvc_slim`**（base + characters/spica，gitignored 本地产物），由 `scripts/local_runtime/build_rvc_slim.py` 从 Applio 工程构建（Applio 源树保存在仓外、不随仓分发）。`data/config/app.yaml` 的 `song.applio_root` / `model_path` / `index_path` 已指向 `artifacts/rvc_slim`；点歌翻唱前需本地有 `artifacts/rvc_slim`（分发/安装方式见打包阶段 / `docs/LOCAL_RUNTIME_CUTOVER_REVIEW.md`）。

## 语音输入

语音输入通过 `hardware/respeaker` 接 ReSpeaker USB 4 Mic Array：

- `audio.py`: 录制 16kHz channel 0 PCM，支持硬件 VAD。
- `control.py`: 通过 pyusb 和 `tuning.py` 读取硬件 VAD。
- `speech_worker.py`: Qt 线程，调用 `speech_recognition` 做中文识别。

如果 `RESPEAKER_REQUIRE_HARDWARE_VAD=1`，硬件 VAD 不可用时会直接失败；否则会 fallback 到短时固定录音。

## 开发规则

- 跑测试：`python -m pytest tests -q`。
- 不要裸跑 `pytest`。
- 不要让 `spica/` import Qt。
- 不要在业务层直接读环境变量。
- 不要继续往 `agent/streaming_pipeline.py` 塞功能；新的流式组件在 `spica/runtime/`。
- UI 只消费 Host/ChatEngine 事件和状态，不直接知道 OpenAI、GPT-SoVITS、SQLite 或 VisualDiffService 的细节。

## 发布规则

发布仓库当前目标是：提交除大体积本地资产和第三方引擎外的项目代码、配置、测试、文档和插件骨架。

应排除：

- `.git/`
- `.idea/`
- `.pytest_cache/`
- `__pycache__/`
- `*.pyc`
- `.env`、`*.env` 的真实值
- `third_party/`
- `spica_data/`
- `agent_tools/tts/vendors/GPT-SoVITS-v2pro-20250604-nvidia50/`
- `agent_tools/function_tools/song/Applio/`
- `artifacts/`（`tts_slim` / `rvc_slim` 等本地 runtime 产物、parity/benchmark 输出）
- `static/generated_voice/*`
- `static/generated_song/*`

发布包可以保留必要空目录或占位目录，但不要提交模型权重、语音素材、立绘大图、本地 SQLite 记忆库、生成 wav、Applio 工程和 GPT-SoVITS vendor 内容。

## 常见问题

### 启动提示没有 OPENAI_API_KEY

检查 `xiaosan.env` 是否在仓库根目录，且包含：

```env
OPENAI_API_KEY=...
```

### 找不到 TTS 权重或参考音频

检查 `data/config/tts.yaml` 中所有相对路径是否相对配置文件目录可解析，并确认 GPT-SoVITS vendor、权重、`spica_data/voice` 已补齐。

### 找不到立绘或差分规则

检查 `data/config/visual.yaml` 的 `diff_root`、`rules_path`、`background_path`，以及 `spica_data/diffs` 是否存在。

### screen pipeline 失败

检查 CUDA、torch、transformers、RapidOCR 依赖，以及 `data/config/app.yaml` 的 `screen:` 节（及 SPICA_SCREEN_* env 覆盖）。当前本地 Moondream 配置默认要求 CUDA。

### GitHub Actions 不是 GitHub-hosted runner

`.github/workflows/ci.yml` 当前写给 self-hosted runner，并假设本机有 conda `gptsovits` 环境。如果要改成 GitHub-hosted runner，需要补充环境重建步骤。

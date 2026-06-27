# Spica 架构说明：给算法工程师看的版本

> 读者画像：你熟 LLM / TTS / STT / OCR / VLM / RVC，但不熟大型 Python 桌面应用的分层。
> 目标：30–60 分钟看懂 Spica 怎么跑、以后加需求改哪里、哪些线不能碰。
> 来源：基于第 1 趟只读审计（`docs/REAL_ARCHITECTURE_MAP.md` / `docs/CODE_REVIEW_REPORT_CURRENT.md`）+ 第 2 趟对关键路径的直接复读。
> **可信度**：第 1 趟全量测试 `python -m pytest tests -q` → **821 passed（未触发熔断）**，故本文结论由静态阅读 + 通过的测试共同支撑，不是纯猜测。凡标「不确定」处即未完全核验。
> 术语速记：**port=接口契约**（只定方法签名）、**adapter=某个 port 的具体实现**、**registry=「名字→工厂」注册表**（换引擎=改配置里的名字）、**stage/node=turn 流水线上的一段纯函数**、**RuntimeEvent=后端发给 UI 的事件数据类**。

---

## 0. 快速通道：15 分钟看懂全局

### 0.1 项目一句话
Spica 是**本地运行的桌面语音陪伴 App**（PySide6 透明 overlay + 语音对话），**核心是 galgame 陪玩**：绑定游戏窗口 → 实时 OCR 剧情 → 看画面回答 → 把共同经历写进记忆 → 后台总结剧情。LLM/TTS/STT/OCR/VLM/RVC 都是为这个核心搭桥。

### 0.2 最重要的 10 条铁律（违反=破坏架构，详见 §4 与 §17）
1. **只有 `run_turn` 能让 Spica 开口**。不准再起第二条回答链路。
2. **`ui/` 里不准 new LLM/TTS/Memory/VLM 主服务**，只能找 `AppHost` 要。
3. **`spica/` 不准 import Qt/PySide**（核心层 UI 无关）。
4. **业务代码不准 `os.getenv`**，配置只从注入的 config 拿。
5. **不准为新功能另拼一套 prompt / 调一次 LLM**，统一走 `prompt_builder` + gated stage 注入。
6. **act（有副作用）工具不准直接 exec/eval/shell/任意路径**，动作必须经 host 闭包的白名单面。
7. **OCR 文本不准直接当成用户消息**喂进 `run_turn`。
8. **galgame 剧情不准写进普通聊天的 recent memory**（用独立游戏库）。
9. **不准删守卫测试来让测试变绿**。
10. **不准大范围重命名/搬目录**，除非先写迁移计划。

### 0.3 最典型的一次聊天 turn（记住这条主干，其它都是它的变体）
```text
用户打字/说话
  → ui 把文本交给 ChatEngine.stream_voice()        (spica/core/chat_engine.py)
  → ChatEngine._request() 造一个 TurnRequest（冻结的输入对象）
  → run_turn() 唯一事件出口                         (spica/runtime/turn.py:43)
       validate → 取 recent/长期记忆 → (galgame gate) → build_prompt
       → 可选 tool_round（probe→执行工具→followup）→ LLM 流式输出
       → 把答案切成 play unit → 每个 unit 并行算立绘 + TTS → 按序播放
  → 后端发 RuntimeEvent → ChatEngine 转成 legacy dict → ui 播放
```
**一句话**：所有「她开口」最终都收敛到 `run_turn`。截图问答、点歌播报、galgame 问答、主动吐槽，都是给这条主干换不同的输入/注入，不是另开链路。

### 0.4 以后加需求先看哪里（详见 §16 速查表）
- 加一个**模型/引擎**（换 LLM、换 TTS、加 STT 后端）→ 写一个 adapter，在 registry 注册一个名字，改 `data/config/app.yaml` 的 provider 名。**不碰 runtime。**
- 加一个**工具**（让 LLM 能调用的能力）→ 写 schema + handler，`register_tool(...)`，定好 `effect`（read/write/act）。**act 工具权限必须留在 host 闭包。**
- 加一段**要进 prompt 的上下文** → 写一个 gated stage 注入，不要自己拼 prompt。
- 让她**主动说话** → 发 `ProactiveTurnRequest`，经 `stream_system_turn`，仍走 `run_turn`。
- galgame 相关 → 只通过 `GalgameCompanionSession` 的公共方法提交事件，别碰它的私有状态。

---

## 1. 项目是什么，不是什么

**是**：本地桌面陪伴 App。一个透明置顶窗口里有立绘、对白框、输入框、截图/语音按钮。后端用 OpenAI 兼容接口生成角色回复，本地 GPT-SoVITS 合成日语语音，本地立绘差分选表情，本地 OCR+VLM 看屏，网易云+RVC 点歌翻唱，SQLite 存记忆。核心场景是陪你玩 galgame。

**不是**：不是 Web 服务，不是多用户后端，不是聊天机器人 API。它是单机、单用户、带 GUI 的桌面进程。所以你会看到大量「跨线程」「UI 主线程 vs 后端线程」的讲究——这正是算法同学最容易踩的地方（§14）。

**核心取舍**（来自 `CLAUDE.md`）：涉及选择时，优先服务「Spica 能陪你玩 galgame、记得剧情和共同经历」。

---

## 2. 目录地图

```text
webui_qt.py                 # 进程入口（只做 Linux Qt/输入法/ALSA 环境垫片）→ ui.qt_overlay.main()
spica/                      # 平台核心（UI 无关，禁 import Qt）—— 你的大部分逻辑在这
  host/      app_host.py    # 组装根：把所有零件接起来（薄，只装配+生命周期）
  core/      chat_engine.py # 对话驱动；events.py=RuntimeEvent；proactive.py=主动开口；state_machine.py
  conversation/            # 纯 domain：prompt_builder（拼 prompt 的唯一地方）、reply_parser、text_normalizer
  runtime/   turn.py       # run_turn（唯一事件出口）；orchestrator（流式编排）；context/deps（类型化输入/依赖）
             stages.py     # turn 流水线的各段纯函数（validate/取记忆/build_prompt/call_llm/...）
             tool_round.py # 工具轮：probe→执行→followup；fold.py=同步折叠；sequencer=有序播放
  ports/                   # 接口契约：llm tts visual memory tool stt screen + galgame 五端口
  adapters/                # 各 port 的实现：llm/openai_compatible、memory/sqlite、tts、stt/faster_whisper、
                           #   screen/local_moondream、ocr/rapidocr、game_memory/sqlite、tools/{4 个工具}
  galgame/   session.py    # 陪玩状态机（唯一状态 owner）；ocr_loop；summarizer；reaction；companion_controller
  config/    schema.py      # AppConfig（Pydantic）；manager.py（解析）；secrets.py；env_roster.py（env 名册）
  plugins/   registry.py    # CapabilityRegistry：名字→工厂
  memory/                  # 几乎空（只 __init__，port glue 命名空间）—— 真实记忆实现在下面根级 memory/
agent_tools/
  function_tools/screen/    # 本地截图 + RapidOCR + Moondream（VLM）pipeline
  function_tools/song/      # 网易云搜索/下载 + 人声分离 + RVC 变声 + 混音
  tts/                      # GPT-SoVITS service + dummy adapter
  visual/  diff_service.py  # 本地立绘差分选择
memory/    recent.py store.py extractor.py control.py   # 角色短期(deque)/长期(SQLite)记忆实现体
hardware/respeaker/         # ReSpeaker 麦克风录音 + 硬件 VAD + STT worker
ui/        qt_overlay.py controllers/ workers/ widgets/ models/   # PySide6 界面、线程桥、播放
data/config/  app.yaml      # 唯一 app 级 typed 配置；tts.yaml/visual.yaml=角色数据文件
tests/                      # ~130 文件，含 12 个 AST/语义守卫；全量 821 passed
```

**命名陷阱**：`spica/memory/`（包，几乎空，是 port 层占位）≠ 根级 `memory/`（真实实现：`recent.py`/`store.py`）。别找错。

---

## 3. 启动链路

### 3.1 入口
`webui_qt.py::main()`：只做 Linux 环境垫片（检查 `libxcb-cursor`、修 fcitx→xim 输入法、找 ALSA 插件），然后 `from ui.qt_overlay import main as qt_main; return qt_main()`。**这里直接写 `os.environ` 是允许的**——它是进程入口，在任何对象构造前就要灌注环境（铁律 #4 只管业务代码，入口豁免）。

### 3.2 UI 启动
`ui/qt_overlay.py::main()`（实测代码顺序，关键时序）：
```text
:1348  load_secrets()            ← 必须是第一句！（铁律 #10）
:1357  app = QApplication(...)
:1359  window = OverlayWindow()  ← 内部构造 AppHost、各 controller、worker
:1360  window.show()
:1361  return app.exec()         ← Qt 事件循环；companion sink 已在 exec 前挂好
```
**为什么 `load_secrets()` 必须第一句**：曾经有 bug（F19）——某个对象在构造期就读了还没灌注的 env，拿到空值并永久定格（song 意图分类器因此从未启用）。所以有一个 AST 守卫 `tests/test_env_centralization.py:136-157` 钉死「`qt_overlay.main()` 的第一条语句必须是 `load_secrets()`」。

### 3.3 AppHost 组装
`spica/host/app_host.py`（836 行，但 `initialize()` 本体只 ~84 行纯接线，:201-284）：
```text
OverlayWindow.__init__ → AppHost()
  AppHost.__init__:  resolve_effective_screen_config():142 / song:145   （resolve-once，注入下游）
  AppHost.initialize():
     :210  ConfigManager().load()           # 解析 app.yaml + env override → AppConfig
     :211  load_secrets()                    # xiaosan.env → Secrets（密钥）
     register_builtin_adapters(registry)     # 注册 LLM/TTS/Visual/Memory/inspect_screen
     watch_game_screen/note_game_observation/sing_song 在 __init__ 注册（带 host 闭包，持执行权限）
     ChatEngine(services, config)            # 注入 deps
     + reaction/galgame 闭包 + ProactiveTurnArbiter + ManagementSurface
```
`app_host.py` 体量大不是 bloat：`initialize()` 很薄，剩下的是 reaction/galgame 的 **host 闭包**——按铁律 #6（act 工具/写动作的执行权限必须闭合在 host），写权限和 judge 权限**必须**留在 host，这是规则要求。

### 3.4 ChatEngine 和 Runtime 接线
`ChatEngine.__init__`（`spica/core/chat_engine.py:41`）：
- 存 `services`（已解析好端口的服务包 `AgentServices`）和 `config`（`AppConfig`）。
- 造 `self.deps = TurnDeps.from_services(services, config)`——这是 turn 运行时真正依赖的「类型化依赖包」（§9.3）。
- `set_game_binding_provider(...)`：host 把「陪玩态绑定快照」注入进来，陪玩时 `_request` 会自动把 turn 路由进 galgame 命名空间（§8、§13.5）。

启动总图：
```text
webui_qt.py
  -> ui/qt_overlay.py::main()  (load_secrets 第一句)
  -> OverlayWindow -> AppHost.initialize()
       -> ConfigManager.load() / load_secrets()
       -> CapabilityRegistry (register_builtin_adapters + host 闭包工具)
       -> AgentServices (已解析的 ports/adapters)
       -> ChatEngine (TurnDeps.from_services)
  -> ui controllers / workers (ChatStreamController, AudioController, CompanionEventBridge, ...)
  -> app.exec()
```

---

## 4. 核心原则：为什么不能随便重构

### 4.1 唯一开口链路（铁律 #1、#5）
`spica/runtime/turn.py:43-44`：
```text
run_turn() 内部唯一一行 yield：yield event_from_legacy(legacy)
```
**所有**让 Spica 说话的入口都收敛到这里：
- 用户聊天：`ChatEngine.stream_voice → stream_voice_runtime → run_turn`
- 同步取完整 payload：`ChatEngine.run_voice = run_turn + fold_events`
- 系统主动开口：`ChatEngine.stream_system_turn → stream_voice(interaction_mode="system") → run_turn`

**为什么不能绕**：cancellation（中断防 ghost）、play unit 有序播放、系统 turn 的工具硬关断、同步/流式两路的一致性，全部挂在 `run_turn` 这一条链上。你另起一条链路 = 这些保证全部失效，而且会和主链路行为漂移。第 1 趟反向 grep 已确认：当前**没有**第二条开口链路。

### 4.2 UI 和后端隔离（铁律 #2、#3）
- `spica/`（核心）**不准 import Qt**，由 `tests/test_layering.py` AST 守卫钉死。核心要能在没有 GUI 的测试里跑。
- `ui/` **不准自己 new** LLM/TTS/Memory/VLM 主服务，只能从 `AppHost` 拿现成的。
- 后端 → UI **只能发 `RuntimeEvent` 数据类**（或其 legacy dict 形态），后端线程**不准直接调 Qt widget**（会段错误，见 §14）。

### 4.3 config 单一入口（铁律 #4）
业务代码**不准 `os.getenv`**。只有 `spica/config/` 的三个文件能碰 `os.environ`：`manager.py`、`secrets.py`、`runtime_env.py`。env 名册集中在 `spica/config/env_roster.py`。由 `tests/test_no_getenv.py` AST 守卫（扫 spica/memory/agent_tools/ui/hardware，永久白名单恰好这 3 件）。**为什么**：配置要有单一、验证过的真相源；散落的 `os.getenv` 会拿到空值定格（F19）、无法测试、无法快照对账。

### 4.4 ports / adapters / registry（支撑 #2：换引擎不动核心）
- **port**（`spica/ports/`）= 接口契约，只定方法。例如 `LLMPort` 定了 `iter_response_text` / `create_chat_with_tools`。
- **adapter**（`spica/adapters/`）= 某个 port 的具体实现。例如 `adapters/llm/openai_compatible.py`。
- **registry**（`spica/plugins/registry.py::CapabilityRegistry`）= 「名字→工厂」表。`register_llm("openai_compatible", factory)`，运行时 `resolve_llm(config.llm.provider)`。
**好处**：换引擎 = 改 `app.yaml` 里一个 provider 名，**不动核心代码**。新能力都走这套，别塞进 UI/Host/runtime。

### 4.5 tool 副作用隔离（铁律 #6）
工具按 `effect` 分三级：`read`（纯观察）/ `write`（改自己域的数据）/ `act`（操作用户环境、起任务、占共享资源）。`act` 工具（当前唯一是 `sing_song`）的**安全边界不是那个 flag，而是 port 模式**：动作经 host 闭包的白名单面执行，**LLM 永远只传参数，碰不到 exec/eval/shell/任意路径/任意 URL**。`registry.py:107-111` 的注释把这条写死了。

---

## 5. 一次普通聊天 turn 的完整生命周期

```text
用户输入(文本/语音转写)
  → ui (ChatStreamController → ChatWorker 在后端线程)
  → ChatEngine.stream_voice(user_input, conversation_id, interaction_mode="chat", cancelled=Event)
  → ChatEngine.stream_voice_runtime()
  → ChatEngine._request(...)  造 TurnRequest（冻结输入；若陪玩态，自动填 galgame 字段）
  → run_turn(ctx, services, deps)                       (spica/runtime/turn.py)
       内部 = spica/runtime/orchestrator.stream_voice_events()
       prep 阶段（stages.py，纯函数，按序）：
         validate_input → load_recent_context → retrieve_long_term_memory
         → analyze_screen_attachment → retrieve_game_context_node(gate) → build_prompt_node
       generate 阶段：
         tool_round（若有可用工具）：probe → tools.run() → followup
         → LLM 流式 delta → JsonAnswerExtractor 抽 answer → PlayUnitSplitter 切句
         → 每个 play unit：visual_job ∥ tts_job（并发）→ Sequencer 按 index 收齐
         → save_stream_memory（②cancel 检查后才写）
  → 产出 RuntimeEvent 流：status / unit_text_ready / unit_visual_ready / unit_audio_ready / unit_ready / done
  → ChatEngine.stream_voice 把每个 event .to_legacy_dict() 给当前 UI
  → ui 按 unit_ready.index 顺序播放（打字机 + 立绘 + 音频）
```
关键点：**prep 阶段是纯 `(ctx)→ctx` 变换，不发事件**；只有 `run_turn`/orchestrator 发事件（`test_layering.py` 钉死 stages 不准 import `spica.core.events`）。play unit 的「立绘并行、TTS 并行、最后按 index 有序」由 `sequencer.py` 保证。

---

## 6. 一次截图 / 看屏 turn 的完整生命周期

两条路，都本地、绝不上传图片到聊天 LLM：

**自动看屏（LLM 决定调工具）**：
```text
用户说"看下我屏幕上报什么错"
  → tool_round probe：tools.schemas_for_user_text() 把 inspect_screen schema 给 LLM
  → LLM 决定 call inspect_screen
  → tools.run("inspect_screen", args)  (spica/runtime/tools.py)
       → adapters/tools/screen.py → agent_tools/function_tools/screen/
         mss 本地截图 → RapidOCR 抽字（进程级单例 + _INFER_LOCK 串行）
         → Moondream 本地 VLM 描述（INVARIANT N0：截图绝不上传）
       → 返回 screen observation JSON（compact_output 压缩后）
  → followup：把观察喂回 LLM → 最终答案 → 同 §5 播放路径
```
**手动截图**：截图按钮 → ui 框选区域 → `pending_screen_attachment` → 随下一条消息带上 → `analyze_screen_attachment` stage。

**陪玩看屏**：`watch_game_screen` 工具（只在陪玩态 `available`），截绑定窗口而非整屏，capture 前查「安全态」（窗口可见、未被遮挡）。

工具调用通用图：
```text
user text
  → tools.schemas_for_user_text(user_text)   # 含 intent 词表预筛（只决定"这轮给不给 LLM 看"）
  → LLM tool probe（chat.completions 流式 / responses）
  → tools.run(name, args)                     # 唯一执行入口，registry 解析 handler
  → 工具结果（dict→序列化一次 / 或 ToolError 信封）
  → compact_output 压缩 → followup prompt → 最终 LLM 答案
  → 同 TTS/Visual 播放路径
```

---

## 7. 一次 sing_song 点歌 turn 的完整生命周期

`sing_song` 是唯一的 `act` 工具，演示「act 工具纪律」：
```text
用户"给我唱首歌"
  → tool_round：sing_song schema 给 LLM（intent_gated，词表预筛）
  → LLM call sing_song(query)
  → adapters/tools/sing_song.py：纯转发垫片，只校验非空，调 host 闭包
  → host 闭包 _request_song(query)  (spica/host/app_host.py:673-694)
       网易云白名单搜索 → 造 SongRequestEvent（RuntimeEvent 子类）
       失败 → 抛 ScreenToolError → tools.run 包成 ToolError 信封（不崩 turn）
  → SongRequestEvent 经 UI 桥 → SongWorker（后端线程）
       agent_tools/function_tools/song/pipeline.py：
       搜索/下载 → 人声分离 → Applio/RVC 变声 → 混音 → static/generated_song → 播放
  → 唱完她"主动开口"收尾（系统 turn，见 §8）
```
**残余前置规则**：播放中的「暂停/继续/停止/重唱」走 UI 的**控制词快路径**（`ui/controllers/song_controller.py:167-190`），gated 为 `is_busy() + 置信≥0.9 + {暂停/继续/停止/重唱}`，复用 `parse_song_control_intent`（`agent_tools/function_tools/song/intent.py`，已从 LLM 侧意图路由迁到 UI 控制层，不是死代码）。
**安全点**：LLM 只给 `query` 字符串；真正的搜索/下载/变声/播放都在 host 闭包和 pipeline 里，LLM 碰不到。

---

## 8. 一次 galgame 陪玩 turn 的完整生命周期

陪玩问答仍然走 `run_turn`，只是多一步「gated stage 注入游戏上下文」：
```text
OCR loop 持续把剧情喂进 GalgameCompanionSession（不是用户消息！见 §13.2）
用户问"她刚才为什么生气"
  → ChatEngine._request()：检测到陪玩态（_game_binding_provider 返回 GameTurnBinding）
       → conversation_id 改成 "galgame::<game>::playthrough::<id>"（recent 隔离）
       → memory_conversation_id 保留调用方原 id（§27①：角色长期记忆仍连续）
       → 带上 game_context_request（显式 gate 输入）
  → run_turn → build_prompt_node
  → retrieve_game_context_node（gate, spica/runtime/stages.py:528）
       在 build_prompt 之后、call_llm 之前注入 [剧情摘要/进度/选项/陪玩 beat]
       _game_context_mode() 纯看请求字段（interaction_mode/conversation_id 前缀/mode），
       绝不跑第二次 LLM；mode=="none" 时 byte-level no-op（普通聊天零影响）
  → LLM → 同 §5 播放路径
```
galgame 图：
```text
OCR loop
  → GalgameCompanionSession（唯一状态 owner）
  → game memory（独立库 spica_data/galgame.sqlite3）
  → 用户提问
  → ChatEngine._request()（GameTurnBinding / GameContextRequest 自动填充）
  → build_prompt_node
  → retrieve_game_context_node（gate，纯请求逻辑注入）
  → LLM
  → 同播放路径
```

---

## 9. Runtime 核心对象

### 9.1 TurnRequest（`spica/runtime/context.py:82`）
一次 turn 的**冻结输入**（`@dataclass(frozen=True)`）。字段：`user_input` / `conversation_id` / `emotion_override` / `interaction_mode`（chat|system）/ `screen_attachment` / `tts_param_overrides` / `visual_overrides` / `memory_conversation_id` / `game_context_request` / `cancelled`（`Event`，`compare=False`）。
关键属性：`effective_memory_conversation_id = memory_conversation_id or conversation_id`——这是 galgame「换 conversation 但仍读角色记忆」的单一真相源（§12.4）。

### 9.2 TurnContext（`spica/runtime/context.py:196`）
一次 turn 的**工作上下文**，取代了旧的 `AgentState` 大杂烩黑板。设计精髓：**各 stage 的产出是 typed 子对象，跑到那一段之前是 `None`**：
```text
recent (取记忆后) / screen_observation (看屏后) / prompt (build 后) / answer (生成后) / error
```
所以「prep 阶段读不到生成阶段还没写的字段」由类型强制——这就是拆黑板的意义。横切累加器 `timing/metadata/tools/response_payload` 暂时仍是 flat。

### 9.3 TurnDeps（`spica/runtime/deps.py:43`）
一次 turn 跑所依赖的**类型化依赖包**（frozen）：`config`(AppConfig) / `llm` `tts` `visual` `memory`（4 个 port）/ `tools`(ToolSet) / `game_memory` / `observer` / `jobs` / `exec_strategy`。
由 `TurnDeps.from_services(services, config)` 从 host 装配好的 `AgentServices` 映射而来。**runtime 只认 `deps.xxx`，不直接 import 具体 adapter**——这就是依赖注入边界。`observer/jobs/exec_strategy` 是非 None 占位（方便以后换实现而不用删 None 检查）。

### 9.4 RuntimeEvent / legacy dict bridge（`spica/core/events.py`）
后端 → UI 的统一载体是 `RuntimeEvent` 数据类（`StatusEvent` / `UnitReadyEvent` / `DoneEvent` / `ErrorEvent` ...）。
- `run_turn` 内部把旧式 `{"event","data"}` dict 经 `event_from_legacy()` 升成 RuntimeEvent（唯一升级点）。
- 当前 UI 还消费 legacy dict，所以 `ChatEngine.stream_voice` 又把每个 event `.to_legacy_dict()` 转回去（过渡兼容层）。
- galgame/song 事件（`companion_events.py` / `song_events.py`）也是 RuntimeEvent 子类，但走的是**独立的 `CompanionEventBridge`**（不经 run_turn），这是有意设计。

### 9.5 PlayUnitSplitter / TTS / Visual 并发
LLM 流式输出 → `JsonAnswerExtractor` 抽出 answer 文本 → `PlayUnitSplitter`（`spica/runtime/play_unit_splitter.py`）按 `stream.play_unit_min_chars`/`max_chars` 切成「播放单元」。每个 unit：立绘选择（visual_job，可并行多 worker）和 TTS 合成（tts_job）并发算，最后由 `Sequencer`（`spica/runtime/sequencer.py`）**按 index 收齐再有序 emit `unit_ready`**。所以「立绘/语音并行算、但播放顺序不乱」。

---

## 10. Tool 系统

### 10.1 Registry（`spica/plugins/registry.py`）
`CapabilityRegistry._tools` 存 `name → (schema, handler, available, intent_gated, chainable, compact_output, effect)`。注册入口：
```text
register_tool(schema, handler, *, available=None, intent_gated=True,
              chainable=False, compact_output=None, effect="read")
```
`effect` 只能是 `read|write|act`（否则启动报错）。`tool_schemas()` 会按 `available()` 谓词做状态过滤（**坏掉的谓词只会隐藏该工具，绝不弄崩 turn**）。

### 10.2 ToolSet（`spica/runtime/tools.py`）
runtime 看工具的视角（Protocol）：`schemas_for_user_text(user_text)` 和 `run(name, arguments)`。生产用 `RegistryToolSet`（包 `CapabilityRegistry`），测试用 `_FunctionTableRegistry`（包旧函数表）。**INVARIANT N5**：runtime 只从 registry 解析工具，绝不重新 import 静态 `TOOL_SCHEMAS`。`run()` 是唯一执行入口——无 LLM 绕过 registry 的路径。

### 10.3 intent_gated / available / chainable / effect
- `available`：**状态谓词**，决定「现在供不供这个工具」（如 watch_game_screen 仅陪玩态）。
- `intent_gated`：**词表供给预筛**，只决定「这轮把不把 schema 给 LLM 看」，**绝不劫持/吞用户消息**（B1 教训）。词表在 `agent_tools/function_tools/router.py`。
- `chainable`：True 才允许工具轮多轮链式（当前 4 个工具全 False，单发）。
- `compact_output`：把工具输出在进 followup prompt 前压缩（inspect_screen 注册了历史压缩器）。

### 10.4 read / write / act 的区别
| effect | 含义 | 例子 | 纪律 |
|---|---|---|---|
| read | 纯观察 | inspect_screen / watch_game_screen | 无副作用 |
| write | 改自己域数据 | note_game_observation（写 CompanionBeat） | 经 host 闭包写游戏库 |
| act | 操作用户环境/起任务/占共享资源 | sing_song（起点歌任务） | **必须** host 闭包白名单面 |

四个内置工具一览（schema 与 handler 路径）：
```text
inspect_screen        read  intent_gated  schema: agent_tools/function_tools/screen/tool.py
                                          handler: spica/adapters/tools/screen.py（ToolPort）
watch_game_screen     read  仅陪玩 available  spica/adapters/tools/watch_game_screen.py（host 闭包）
note_game_observation write 仅绑定 available  spica/adapters/tools/note_game_observation.py（host 闭包）
sing_song             act   intent_gated     spica/adapters/tools/sing_song.py（host 闭包 _request_song）
```

### 10.5 为什么 act 工具必须由 Host 闭包持权限
LLM 的输出是**不可信输入**。如果让 LLM 直接决定执行什么命令/打开什么路径，就等于把任意执行权交给了模型。所以：工具垫片（adapter）只做纯转发，真正的动作在 **host 闭包**里（host 闭包闭合了执行权限、白名单、配置），失败以 `ToolError` 信封返回而不是抛崩 turn。这条由 `registry.py:107-111` 注释和 `sing_song` 的实现共同保证。

---

## 11. 配置系统

三个载体，职责分明：

### 11.1 app.yaml（`data/config/app.yaml`）
唯一 app 级 typed 配置，映射到 `AppConfig`（Pydantic，`spica/config/schema.py:299`）。**实测共 10 个键**：
```text
llm / memory / character / stream / galgame / stt / screen   （7 个 typed 子模型）
song（dict，故意 untyped，叠在 song/config.py DEFAULT_CONFIG 上，D-3a 挂账）
plugins（list）
max_tool_rounds（int）
```
键缺省时用 `AppConfig` 默认值；env 覆盖文件值。

### 11.2 xiaosan.env / secrets（`spica/config/secrets.py`）
放密钥（`OPENAI_API_KEY`、`JUDGE_API_KEY`），由 `load_secrets()` 在进程入口第一句灌注。
**注意**：env 本质是「override 层」，技术上能承载非密钥的 override（如 `MODEL`/`SPICA_USER_NAME`/`SPICA_SCREEN_*`），不是只能放密钥——「只放密钥」是推荐约定。

### 11.3 overlay_config.json（`ui/overlay_config.json`）
纯 UI 偏好（立绘缩放、UI 缩放、打字机速度、窗口初始比例）。不属于平台核心配置。

### 11.4 env_roster（`spica/config/env_roster.py`）
所有 env 名的集中册子：`APP_ENV_MAP` / `SECRETS_ENV_MAP` / `SCREEN_ENV_MAP` / `RESPEAKER_ENV_MAP` / `RUNTIME_CACHE_ENV_MAP`。`SCREEN_ENV_MAP`/`RESPEAKER_ENV_MAP` 被 `manager.py` 结构 import（真·单一来源）；`APP_ENV_MAP` 是手工镜像（manager 里硬编码了同名），靠测试 `test_resolved_config_equivalence.py:257-282` 保证「manager 里出现的 env 名都在册子里」（名级防漂移）。tts.yaml/visual.yaml 是**角色数据文件**（角色包可整文件覆盖），不算配置载体。

### 11.5 新增配置的正确流程
1. 在 `schema.py` 对应子模型加 typed 字段（带默认值，保证零 diff）。
2. 若要支持 env override：env 名进 `env_roster.py`，并在 `manager._env_overrides()` 映射。
3. **不准**在业务代码新开 `os.getenv` 直读。
4. 改配置解析前先 `python scripts/dump_resolved_config.py --out <baseline>`，改完 `--diff` 零差异才算完。

---

## 12. 记忆系统

### 12.1 recent memory（`memory/recent.py`）
短期：内存 `deque`，按**裸 conversation_id**存最近几轮对话。**同步写**（turn 返回前完成）。

### 12.2 long-term memory（`memory/store.py` + `spica/adapters/memory/sqlite.py`）
长期：SQLite（`spica_data/memory.sqlite3`），规则抽取 + upsert 去重 + 裁剪。**后台 JobRunner 异步写**（`spica/runtime/memory_commit.py:74-82`，失败只 WARNING）。写入经 `MemoryPort.commit_turn()`，runtime 不管抽取细节。

### 12.3 MemoryScope（`spica/ports/memory.py`）
`MemoryScope(character_id, user_id, conversation_id)`。adapter 把长期记忆的 key 命名空间化为 `{character_id}::{conversation_id}`（`adapters/memory/sqlite.py:30`），所以**不同角色结构上不会串长期记忆**。

### 12.4 memory_conversation_id（§27①，galgame 专用解耦）
普通聊天不设它（= None → 等于 conversation_id，行为和以前逐字节一致）。galgame turn 把 `conversation_id` 设成 `galgame::...`（让 recent memory 隔离），但 `memory_conversation_id` 保留调用方原 id，于是 `effective_memory_conversation_id` 仍指向 "default"——**galgame 问答时她仍能读到平时关于「麦」的长期记忆**，抽取的新记忆也落回原 scope。读（`stages.py:25`）写（`memory_commit.py:67`）对称。这是第 1 趟核验过的关键耦合点，结论：隔离正确、耦合不破。

### 12.5 galgame memory（`spica/adapters/game_memory/sqlite.py`）
独立库 `spica_data/galgame.sqlite3`、独立 `GameMemoryPort`、独立 schema（`game_profiles`/`story_lines`/`play_sessions`/...）。和角色长期记忆**完全隔离**。

### 12.6 记忆污染风险（已知，见第 1 趟报告）
- **[P1]** Phase 7 多角色时 recent memory 仍用裸 conversation_id（未按 character 命名空间），`chat_engine.py:235-240` 已挂 TODO。单角色无害，切角色前必修。
- **[P2]** 长期记忆后台写失败静默无重试。
- OCR 剧情**绝不进** recent（§13.2），这是硬隔离。

---

## 13. galgame 子系统

### 13.1 session 是唯一状态 owner（`spica/galgame/session.py`）
`GalgameCompanionSession` 用 `RLock` 保护私有状态（FSM state、stable_current_line、未总结行 id、窗口绑定），外部**只能**通过公共方法提交事件：`start/pause/resume/end/on_ocr_result/on_window_lost/on_choice_detected/on_user_reported_choice/on_summary_finished`。别从外面碰它的私有字段。

### 13.2 OCR loop（`spica/galgame/ocr_loop.py`）
**串行「完成后等待」模型**（不是固定 tick 叠加）：跑一次 OCR → 等 `ocr_interval_seconds`（默认 0.3s）→ 再跑。RapidOCR 推理可能 >1s 且非线程安全，所以用进程级 `_INFER_LOCK`（`adapters/ocr/rapidocr.py`）序列化所有 OCR（含 inspect_screen）。
**铁律 #7**：OCR 文本 → `session.on_ocr_result()` → stable line 去重 → 私有 buffer，**绝不**直接变成用户消息进 `run_turn`。

### 13.3 story line / summary / progress（`spica/galgame/summarizer.py`）
后台总结读**不可变 snapshot**：总结启动时锁内切出 `list(self._unsummarized_lines)`（`session.py:463`），传给 job；LLM 在锁外跑只读 `StoryLine`，OCR 可继续往 buffer 写而不影响快照。崩溃恢复：`recover_dangling_sessions`（`summarizer.py:164` ← `app_host.py:763` 启动调用），总结失败留 `ended_at=NULL` → 下次启动重检为 dangling，幂等重试。

### 13.4 CompanionBeat
「你和 Spica 一起玩形成的主观经历」，绑 `character_id + user_id + game_id`，经 `note_game_observation`（write 工具）写进游戏库。区别于客观剧情进度。

### 13.5 prompt 注入
经 gated stage `retrieve_game_context_node`（`spica/runtime/stages.py:528`），在 `build_prompt_node` 之后、`call_llm` 之前注入。gate 只看请求字段（`GameContextRequest.mode`、conversation_id 前缀），**绝不跑第二次 LLM 做"要不要注入"的判断**（那等于第二条 LLM 路径，违铁律 #5）。`mode=="none"` 时 byte-level no-op。

### 13.6 reaction / proactive（主动吐槽）
`spica/galgame/reaction.py`：剧情触发她主动吐槽，但**实际开口仍走 `stream_system_turn → run_turn`**（经 `ProactiveTurnArbiter.try_speak`，策略 drop_if_busy）。`reaction_judge.py` 是**独立的 JUDGE LLM，只给吐槽打分**（worth/moment/angle），不产用户台词；判分失败降级到词表打分器。judge 用独立 endpoint/key（`JUDGE_MODEL`/`JUDGE_BASE_URL`/`JUDGE_API_KEY`）以免拖垮主聊天 LLM。

proactive/reaction 图：
```text
域事件（如剧情新行 / 唱完）
  → ProactiveTurnArbiter.try_speak(drop_if_busy)    (spica/core/proactive.py)
  → ChatEngine.stream_system_turn(directive)
  → stream_voice(interaction_mode="system")          # 工具供给硬关断（防自激）
  → run_turn(...)
  → 同播放路径；若答案是 NO_COMMENT 哨兵 → system_silent 吞掉，不播不写
```
**已知风险**（第 1 趟报告，未修）：judge LLM 无 per-call 超时（P2）；OCR/TTS/judge 共享 GPU 无显式护栏（P2，不确定是否仍导致 freeze）。

---

## 14. UI 层

### 14.1 UI 应该做什么
显示（立绘/对白/状态）、输入（键盘/语音/截图框选）、播放（打字机/音频/立绘切换）、线程桥接、用户交互。通过 `AppHost`/`ChatEngine` 接口驱动后端。

### 14.2 UI 不应该做什么
- 不 `new` LLM/TTS/Memory/Visual 主服务（找 AppHost 要）。
- 不碰 galgame domain 内部状态（只经 host factory + `CompanionEventBridge` 事件）。
- 后端线程**不准直接调 Qt widget**——必须经 Qt queued signal 回到 GUI 线程。近期两个段错误/死锁就是这类问题修的（galgame 系统 turn 跨线程 marshaling、音频 teardown 延迟到信号派发之外）。

### 14.3 Worker / Controller / Bridge
- `ChatStreamController`：消费后端事件流，推进播放状态机。
- `AudioController`：管 `QMediaPlayer`，teardown 用 `QTimer.singleShot(0)` 延迟以避开信号派发死锁。
- `SongWorker` / `SpeechWorker`（hardware）/ `CompanionActionWorker`：后端线程干活，用 Qt signal 回 GUI。
- `CompanionEventBridge`：galgame 的 `RuntimeEvent` sink，转成 Qt signal（queued）跨线程进 UI。

---

## 15. 测试和守卫

全量 `python -m pytest tests -q` → **821 passed**（第 1 趟实测，未触发熔断）。

### 15.1 layering guard（`tests/test_layering.py`）
AST 扫 `spica/`：①不准 import Qt；②不准 import 已删的 `agent` 包；③stages+conversation 不准 import `spica.core.events`（只有 run_turn 产事件）。

### 15.2 no_getenv guard（`tests/test_no_getenv.py`）
AST 扫 spica/memory/agent_tools/ui/hardware，除 3 个 config 文件外不准 `os.getenv/os.environ`，带「扫描文件数>100」的 sanity floor 防空扫绿灯。

### 15.3 turn contract（`tests/test_turn_contract.py`）
钉 turn 的事件契约（空输入/多 unit/工具/附件/异常/同步等价 7 场景）。

### 15.4 config equivalence（`tests/test_resolved_config_equivalence.py`）
钉配置解析语义（env>file>default、各 coercion 分支）+ env 名册 meta-pin。改配置解析必须保持它绿。

### 15.5 哪些测试适合先跑
快、无 GPU/Qt 依赖、最能挡架构回归：
```text
python -m pytest tests/test_layering.py tests/test_no_getenv.py -q
python -m pytest tests/test_turn_contract.py tests/test_resolved_config_equivalence.py -q
python -m pytest tests/test_env_centralization.py -q   # 含 #10 load_secrets 首句 AST 钉
```
**不要裸跑 `pytest`**（会扫到 vendored GPT-SoVITS 崩）。固定 `python -m pytest tests -q`。

---

## 16. 新需求应该接在哪里：速查表

| 我想做 | 接在哪 | 模式 | 不要碰 |
|---|---|---|---|
| 换/加 LLM·TTS·Visual·Memory 引擎 | `spica/adapters/<kind>/` 写 adapter + `register_*` | port/adapter/registry，改 app.yaml provider 名 | runtime / UI |
| 加 STT 后端 | `spica/adapters/stt/` + `SttConfig` | 同上 | run_turn |
| 加一个 LLM 可调用的工具 | 写 schema+handler，`register_tool(effect=...)` | read/write/act 分级；act 必须 host 闭包 | 直接 dispatch，绕 registry |
| 加要进 prompt 的上下文 | 写 gated stage（仿 `retrieve_game_context_node`） | gate 用请求字段判断，不跑第二次 LLM | 自己拼 prompt |
| 让她主动说话 | 发 `ProactiveTurnRequest` → `stream_system_turn` | 仍走 run_turn，interaction_mode="system" | 新建播报通道 |
| 加配置项 | `schema.py` 加 typed 字段（带默认） | 需 env 则进 env_roster+manager | 业务码 os.getenv |
| galgame 新事件 | `GalgameCompanionSession` 加公共方法 | session 是唯一状态 owner | 外部改它私有状态 |
| 加 UI 显示/交互 | `ui/` controller/worker，消费 RuntimeEvent | 后端→UI 只走事件+Qt queued signal | ui 里 new 后端服务 / 后端线程碰 widget |

---

## 17. 不要这样做：反模式表

| 反模式 | 为什么错 | 正确做法 |
|---|---|---|
| 为新功能直接调一次 `client.responses.create` | 绕开 run_turn，丢掉 cancellation/有序播放/工具关断 | 走 `ChatEngine`→`run_turn` |
| 在 `ui/` 里 `OpenAI(...)` / new TTS | UI 该是薄视图，破坏可替换性与 Qt 隔离 | 找 `AppHost` 拿服务 |
| 在 `spica/` import PySide6 | 核心要 UI 无关、可测；test_layering 会红 | Qt 只待在 `ui/` |
| 业务里 `os.getenv("X")` | 拿空值定格（F19）、不可测；test_no_getenv 会红 | 进 `schema.py`+`env_roster`，从 deps.config 取 |
| galgame 自己拼 prompt 调 LLM | 第二条 prompt 链路，必漂移 | gated stage 注入，复用 prompt_builder |
| act 工具里 `subprocess`/`eval` 接 LLM 字符串 | 把任意执行权交给模型 | host 闭包白名单面，LLM 只传参数 |
| 把 OCR 文本当 `user_input` 喂 run_turn | OCR 噪声大/隐私；会污染对话 | 进 `session.on_ocr_result()` 的 text stream |
| galgame 剧情写进 recent memory | recent 会爆 + 污染普通聊天 | 写独立 game_memory 库 |
| 删个守卫测试让 CI 绿 | 守卫编码的是不变量，删=架构静默腐烂 | 修代码，不删守卫 |
| 顺手大改名/搬目录 | 断 import/测试/文档，Claude 易乱重构 | 先写迁移计划再动 |

---

## 18. 与第 1 趟报告的差异

第 2 趟对 `chat_engine.py` / `context.py` / `deps.py` / `tools.py` / `registry.py` / `schema.py` 做了直接复读，**结论与第 1 趟（已修正版）报告一致，无矛盾**。补充/加深了几处细节：
- `ChatEngine._request()` 确实存在（不是 `_context_from_request` 之外的臆测），且内含 galgame「双重包裹守卫」：调用方若已在 `galgame::` 命名空间则原样放行（`chat_engine.py:82`）。
- `AppConfig` 实测 10 个键（7 typed 子模型 + 未 typed 的 `song` + `plugins` + `max_tool_rounds`），证实第 1 趟「README/CLAUDE 写 8 节漏了 stt」的发现。
- galgame 的 env 例外被明确：`JUDGE_MODEL`/`JUDGE_BASE_URL`/`JUDGE_API_KEY` 是 galgame 域**唯一**有 env 名的字段（judge 是可换 endpoint，其余 galgame 调参是 yaml-only），`schema.py:122-146` 写死。
- `game_buffer_tail_limit`（`schema.py:162`）是为压 prompt 体积（曾导致 28k 字符 / ~6s 首 token）加的 prompt 视图截断，summarizer 仍读全部未总结行。

第 1 趟自身已记录、本文沿用的**已知风险**（未修，仅文档化）：Phase 7 多角色 recent 未命名空间化（P1）、OCR 隐私门残余 race（P1）、judge 无 per-call 超时 / GPU 争用（P2）、长期记忆后台写静默失败（P2）、song 节 untyped（P2）、README/CLAUDE 文档漂移（P3，第 3 趟修）。

---

## 19. 关键文件索引

```text
入口/装配   webui_qt.py · ui/qt_overlay.py · spica/host/app_host.py · spica/host/builtins.py
对话核心    spica/core/chat_engine.py · spica/core/proactive.py · spica/core/events.py
turn 运行时 spica/runtime/turn.py(唯一 emit) · orchestrator.py · context.py · deps.py · stages.py
           tool_round.py · tools.py · fold.py · sequencer.py · play_unit_splitter.py · memory_commit.py
prompt     spica/conversation/prompt_builder.py
工具/注册   spica/plugins/registry.py · spica/adapters/tools/{screen,watch_game_screen,note_game_observation,sing_song}.py
配置       spica/config/schema.py · manager.py · secrets.py · env_roster.py · data/config/app.yaml
记忆       memory/recent.py · memory/store.py · spica/ports/memory.py · spica/adapters/memory/sqlite.py
游戏记忆    spica/ports/game_memory.py · spica/adapters/game_memory/sqlite.py
galgame    spica/galgame/{session,ocr_loop,summarizer,companion_controller,reaction,reaction_judge}.py
screen/VLM agent_tools/function_tools/screen/ · spica/adapters/{screen/local_moondream,ocr/rapidocr}.py
song/RVC   agent_tools/function_tools/song/ · spica/host/app_host.py:673-694(_request_song)
UI         ui/controllers/{chat_stream_controller,audio_controller,song_controller,companion_event_bridge}.py
守卫测试    tests/test_{layering,no_getenv,turn_contract,resolved_config_equivalence,env_centralization}.py
第 1 趟产物 docs/REAL_ARCHITECTURE_MAP.md · docs/CODE_REVIEW_REPORT_CURRENT.md
```

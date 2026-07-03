# CLAUDE.md

> 这是 Spica 仓库的常驻操作手册。Claude / Claude Code 每次进入本仓库都先读它。
> 详细的 galgame 陪玩系统规格在 `GALGAME_COMPANION_PLAN.md`，本文件只放**每次都要遵守的铁律、架构地图和工作方式**，不重复完整规格。

---

## 📌 新会话必读：护栏闭环 + 文档地图

> 这一节给「新开的 Claude Code 会话加需求」用。先走这条，再按需读下面 §1–§7 和外部 docs。

**文档地图（按需读，别一次全读）：**
- `docs/DEVELOPMENT_GUARDRAILS.md` —— 高危文件清单、落点决策树、各类改动模板、每类改动跑哪些测试、工作流程。
- `docs/FUTURE_FEATURE_PLAYBOOK.md` —— 20 类未来需求「落点→别这样做→最小步骤→先读什么→查哪些边界→跑哪些测试→风险」；**末尾有可复制的新需求提示词模板**。
- `docs/ARCHITECTURE_FOR_ALGORITHM_ENGINEERS.md` —— 给算法工程师的全局说明（15 分钟快速通道）。
- `docs/CODE_REVIEW_REPORT_CURRENT.md` —— 当前架构债务/高风险/铁律逐条核验（含两处 subagent 误报已纠）。
- `docs/REAL_ARCHITECTURE_MAP.md` —— 真实路径 + 各链路 ASCII 图。

**收到新需求后的固定流程（先计划后改码）：**
1. 先读本 `CLAUDE.md`（§1 铁律 + 下面 10 条硬规则）。
2. 再读 `docs/DEVELOPMENT_GUARDRAILS.md`。
3. 按需求类型读 `docs/FUTURE_FEATURE_PLAYBOOK.md` 对应章节。
4. 需要全局背景再读 `docs/ARCHITECTURE_FOR_ALGORITHM_ENGINEERS.md`。
5. **先输出**需求理解、影响范围、推荐落点、不会碰的边界、测试计划——**等确认后再改代码**。

**10 条硬规则（与 §1 铁律同效，「为什么」见 §1/§3/§4）：**
1. 不绕开 `run_turn` 让 Spica 开口（主动开口走 `stream_system_turn`→run_turn）。
2. `ui/` 不 new LLM/TTS/Memory/VLM 主服务（找 `AppHost` 要）。
3. `spica/` 不 import Qt/PySide。
4. 业务代码不 `os.getenv`（只 config 三件可碰 env）。
5. 不为新功能另起第二套 prompt / LLM 链路。
6. act 工具不直接 exec/eval/shell/任意路径（动作经 host 闭包白名单面）。
7. OCR 文本不直接成为用户消息（OCR → session text stream）。
8. galgame 剧情不污染普通聊天 recent memory（独立 game_memory 库）。
9. 不删除/放宽守卫测试来让测试变绿。
10. 不大范围重命名/搬目录，除非先写迁移计划。

> **本项目不接受「为了方便」绕开 `run_turn` / 绕开 config 层 / 绕开 registry / 绕开 UI bridge 的实现。** 嫌正路麻烦不是绕路的理由——正路就是护栏。

---

## 0. 这个项目是什么

Spica 是一个**本地运行的桌面语音角色扮演陪伴应用**（PySide6 透明 overlay + 语音对话）。角色是 Spica（辻倉朱比華），默认对话者是「麦」。

**项目的核心是 galgame 陪玩系统。** LLM、TTS、记忆、屏幕识别这些子系统，本质上都是为「Spica 能陪你一起玩 galgame、记得剧情和你们一起玩的经历」这个目标搭桥。涉及取舍时，优先服务这个核心目标。

平台重构 + 核心 turn 硬化（C0–C8）+ galgame 陪玩（Phase 0–9 / 路 B：看屏 watch、记忆写回 note、后台总结、履历桥、崩溃恢复、UI 接线）+ 架构硬化 P0–P3（守门墙扩域、生产链多轮工具轮、song 工具化 sing_song、主动开口 turn 发起器）+ **P0b 配置统一（三载体收敛，已完成，见 §2「配置体系」）** 已全部完成。当前无已立项的下一步；候选方向（主动吐槽 / 浏览器操控 / 视频陪看）见审查记录，未立项，不写承诺。

---

## 1. 绝对铁律（违反任何一条都算破坏架构）

1. **`spica/` 不准 import Qt / PySide / shiboken。** 核心层必须 UI 无关。所有 Qt 代码只能待在 `ui/`。截图预览、框选、确认窗口等也只能在 `ui/`。
2. **跨 Host → UI 只能走 `RuntimeEvent` dataclass。** 后端线程**不准**直接调用 Qt widget。后端只 emit dataclass / event，UI 主线程消费后更新界面。galgame 的新事件同样遵守（见 §4）。
3. **唯一对话路径是 `run_turn`。不准另起第二套 LLM prompt / 回答链路。** galgame 的所有问答仍然走 `ChatEngine → run_turn`，通过 gated stage 注入游戏上下文（见 §4）。**主动开口（系统 turn）同样：`ChatEngine.stream_system_turn` → run_turn（`interaction_mode="system"`，该模式下工具供给硬关断防自激）。不准为「她主动说话」另起播报通道或第二套 prompt。**
4. **业务代码不准 `os.getenv`。** 只有 `spica/config` 三件（`manager.py`、`secrets.py`、`runtime_env.py`——后者是 vendored 运行时的 env 写垫片）能碰 `os.environ`，其他地方一律通过注入的 config 拿配置。env 名册的单一居所是 `spica/config/env_roster.py`。
5. **Host 必须薄。** `AppHost` 只做组装与生命周期，不放业务逻辑。
6. **测试命令固定为 `python -m pytest tests -q`。** 不准用裸 `pytest`——它会递归扫到 vendored 的 GPT-SoVITS runtime 直接崩。
7. **新能力走现有 ports / adapters / registry 风格。** 不准另起 `spica/platform/` 这种和现仓库冲突的平行目录树。平台差异藏在 adapter 后面。
8. **galgame 记忆用独立 scope，不污染现有 `MemoryScope`。** 角色长期记忆沿用现有 `MemoryPort` / `MemoryScope`，galgame 只读取，不重造一套（见 §4）。
9. **操作类（有副作用的）工具不准让 LLM 直接 exec / eval——动作必须经专用 port 的白名单动作面，执行权限在 host 闭包。** 启动游戏走 `GameLauncherPort`，唱歌走 sing_song 的 host 闭包，皆是此例。
10. **进程入口（`qt_overlay.main()` / 任何新 main）必须在构造任何对象之前先灌注环境（`load_secrets()`）。** 构造期读 env 而灌注在后，会静默拿到空值并永久定格——song 意图分类器曾因此从未启用过（F19，启动时的 "DEEPSEEK fallback disabled" 警告就是它）。`test_env_centralization` AST 钉死 `qt_overlay.main()` 首句必须是 `load_secrets()`。
11. **不准删除或放宽守卫测试来让测试变绿。** `test_layering` / `test_no_getenv` / `test_turn_contract` / `test_resolved_config_equivalence` 等编码的是架构不变量——测试红了改代码，不是改/删测试。
12. **不准大范围重命名 / 搬目录，除非先写迁移计划。** 大改名会断 import / 测试 / 文档；先出迁移计划 + 影响范围 + 回滚，确认后再动。

> 这些规则带「为什么」是为了防止被合理化绕过：看起来更省事的捷径（比如让 OCR 线程直接刷 UI、或让 galgame 自己拼 prompt 调 LLM）正是这些铁律要拦的东西。

---

## 2. 架构地图

### 现有平台（不要重写，要在其上扩展）

| 关注点 | 位置 |
| --- | --- |
| 组装根 / 生命周期 | `spica/host/app_host.py` → `AppHost.initialize()` |
| 对话核心 | `spica/core/chat_engine.py` |
| 流式编排 | `spica/runtime/orchestrator.py` |
| **唯一 turn emit 路径** | `spica/runtime/turn.py::run_turn` |
| 类型化上下文 | `spica/runtime/context.py::TurnContext` |
| 注入依赖 | `spica/runtime/deps.py::TurnDeps` |
| turn stages | `spica/runtime/stages.py` |
| prompt 上下文 contributor | `spica/runtime/prompt_context.py`（Protocol）+ `spica/galgame/context_contributor.py`（galgame gate；经 `deps.context_contributors` 注册：None → galgame auto-fill、`()` 关闭；通用 node `contribute_context_node` 别名 `retrieve_game_context_node` 永久保留——OO 迁移 Phase 3） |
| prompt 组装 | `spica/conversation/prompt_builder.py::build_spica_prompt`（`[LONG_TERM_MEMORY]` 段在此） |
| 记忆端口 | `spica/ports/memory.py` → `MemoryScope(character_id, user_id, conversation_id)` + `MemoryPort` |
| 记忆 adapter | `spica/adapters/memory/sqlite.py`（按 `character_id::conversation_id` 命名空间隔离） |
| recent memory | `memory/recent.py`（内存 deque 哑存储；key 由 scope 策略推导，按 `character_id::conversation_id`——OO 迁移 Phase 2） |
| 记忆 scope 策略 | `spica/runtime/scope.py`（`CharacterScope` + `MemoryScopeStrategy`：身份默认值单一居所 + recent/LTM/clear 三点对称，live-read `config.character`） |
| 能力注册表 | `spica/plugins/registry.py::CapabilityRegistry` |
| 工具轮（流式生产链） | `spica/runtime/tool_round.py`（probe → 执行 → followup；`chainable` 工具进 round 2..max_tool_rounds 多轮循环，超限优雅强制收尾不报错） |
| 主动开口（turn 发起器） | `spica/core/proactive.py`（`ProactiveTurnRequest` / `ProactiveTurnArbiter`，模式无关）+ `ChatEngine.stream_system_turn`；UI 消费 `StreamKind.SYSTEM` |
| 内置工具（registry 注册） | `inspect_screen`(read) / `watch_game_screen`(read) / `note_game_observation`(write) / `sing_song`(act)，全部「工具垫片 + host 闭包持权限」形制 |
| song 事件 | `spica/core/song_events.py::SongRequestEvent`（host 闭包 emit → RuntimeEvent 桥 → UI 起 SongWorker） |
| 兼容同步链（冻结） | `spica/runtime/sync_chain.py`：**纯 golden 锚，生产零调用方，不准长新能力**（生产同步入口是 `run_voice` = run_turn + fold） |
| 屏幕识别工具链 | `agent_tools/function_tools/screen/`（`inspect_screen` ToolPort：本地截图 + RapidOCR + Moondream，**绝不上传**） |
| 手动框选截图 UI | `ui/widgets/screenshot_selector.py::ScreenshotSelectionOverlay` + `ui/workers/screenshot_worker.py` |
| 活体诊断器 | `scripts/verify_watch_chain.py`（工具不触发先跑它）、`scripts/diag_ocr_providers.py`（疑 OCR 回落 CPU 先跑它） |
| 配置快照守门 | `scripts/dump_resolved_config.py`（Layer A 真机快照，`--diff` 守每个生效值）+ `tests/test_resolved_config_equivalence.py`（Layer B 语义钉）——**动配置解析前先 dump 基线，改完零 diff 才算完** |

### galgame 子系统（已落地的实际布局）

```
spica/ports/
  game_launcher.py / window_locator.py / screen_capture.py / ocr.py / game_memory.py

spica/adapters/
  game_launcher/ window_locator/ screen_capture/ ocr/ game_memory/
  tools/watch_game_screen.py     # 看屏工具（绑定窗口截图 + Moondream）
  tools/note_game_observation.py # 记忆写回工具（对话确认的观察 → CompanionBeat）
  tools/sing_song.py             # 点歌工具（B2，第一个操作类工具）

spica/galgame/                   # domain / session 层（Qt-free）
  models.py session.py text_stream.py summarizer.py ocr_loop.py
  companion_controller.py binding.py history.py ocr_calibration.py
  ocr_region.py window_match.py manual.py
  prompt_sections.py             # galgame prompt 段落构建（OO 迁移 Phase 1 自
                                 # stages.py 迁入；gate + node 仍在 stages.py）
  # 注：早期规划的 choices.py / commands.py 从未单独落地——choice 逻辑长在
  # session.py 内，command intent 已随 B2（song 工具化）一并消亡。

ui/                              # galgame 框选 / 校准 / 状态 chip / CompanionActionWorker
```

### 工具系统现状（已实现的真实状态）

- registry `register_tool` 四维元数据：`available`（状态供给谓词，如 watch/note 仅陪玩态供给）、`intent_gated`（词表**供给预筛**——B1 教训：词表只决定「这轮是否把工具给 LLM 看」，绝不劫持/吞消息）、`chainable`（P1：True 才进多轮循环，现有工具全 False 单发）、`effect`（"read"|"write"|"act" 三值足迹分类，`tools.run` 执行日志带标签）。
- 操作类工具纪律（铁律 #9）：动作经专用 port 白名单动作面、执行权限在 host 闭包、工具是纯转发垫片、失败以 ToolError 信封返回不抛崩 turn——`GameLauncherPort`/note/sing_song 为实例，细则见 `sing_song.py` / `registry.py` 注释。
- 系统 turn（`interaction_mode="system"`）工具供给硬关断（防自激，见铁律 #3）。
- followup 压缩两层：工具自声明 `compact_output`（inspect 注册了历史压缩器）+ 8000 字符头尾截断兜底。

### 配置体系（P0b 已完成，三载体）

- **三载体**：`data/config/app.yaml`（typed config 唯一 app 级文件载体，`AppConfig` 各域 section：llm/memory/character/stream/galgame/**stt**/screen/song/plugins + max_tool_rounds，共 10 键）+ `xiaosan.env`（只装 secrets/key，入口 `load_secrets()` 灌注——铁律 #10）+ `ui/overlay_config.json`（UI 偏好）。tts.yaml / visual.yaml 归类为**角色数据文件**（角色包整文件覆盖、visual 带 mtime 热重载），不是配置载体（D1）。
- **env 只作 override 且只经 config 层**：全部 env 名册单一居所 `spica/config/env_roster.py`（Layer A/B + manager 共用；SCREEN/RESPEAKER 名结构 import，APP 级 manager 仍硬编码同名并由 `test_resolved_config_equivalence` 钉名防漂移——细节见 `docs/CODE_REVIEW_REPORT_CURRENT.md` §11）；读取只在 `manager.py`/`secrets.py`，vendored 运行时的 env 写垫片在 `runtime_env.py`——guard（`test_no_getenv`）的永久白名单恰好这三件，扫 spica/memory/agent_tools/ui/hardware 五目录，临时白名单为空。
- **resolve-once + 注入**：`AppHost` 构造期把 screen / song 配置各 resolve 一次，注入全部生产消费方；screen 解析引擎只有一份（env 侧 coercion 在 manager，file 侧在 `ScreenConfig` validator）。
- **旧载体已退役**：`screen_vision_config.json` / `song_config.json` / `plugins.yaml` 已迁入 app.yaml 并改名 `*.migrated`（仅回滚备份，不被读取）；D6 开关的旧链分支仍在（旧文件若重新出现会整链回退并 WARNING），计划下个版本删除旧链读取。
- **守门长期在位**：`scripts/dump_resolved_config.py`（Layer A）+ `tests/test_resolved_config_equivalence.py`（Layer B）。**改配置解析前先 dump 基线，改完零 diff 才算完。**
- **纪律**：新增配置一律走 typed config（env 名进 env_roster + manager），不准再开新的 env 直读；song 节暂为 untyped override dict（D-3a 挂账，typed 化另立项）。

---

## 3. 唯一对话路径（最常被破坏，单列）

任何需要 Spica 开口回复的地方，都必须走：

```
用户发起:  ChatEngine.stream_voice / run_voice → run_turn → runtime stages → build_prompt → LLM → RuntimeEvent → UI
系统发起:  域事件 → ProactiveTurnArbiter(drop_if_busy) → ChatEngine.stream_system_turn
           → 同一条 run_turn（interaction_mode="system"，工具供给硬关断）→ 同一条播放管线
```

song（B2 后）的正确形态：点歌经主 LLM 的 `sing_song` function call（前置劫持/意图路由/第二 LLM 分类器已全部删除）；唯一残余的前置规则是**播放态控制词快路径**（暂停/继续/停止/重唱，仅 song 流程活跃时生效）；唱完播报走上面的系统发起路径。

galgame 的正确做法：

- `GalgameCompanionSession` 负责 session 状态、OCR loop、stable line、buffer、进度状态、游戏记忆读写、选项事件。
- 需要回复时，**仍然调用 `ChatEngine`、仍然走 `run_turn`**，经 PromptContextContributor 把游戏上下文注入 prompt（OO 迁移 Phase 3：galgame gate 在 `spica/galgame/context_contributor.py`，通用 node `contribute_context_node` 的别名 `retrieve_game_context_node` 永久保留）。
- **禁止**：galgame 自己拼 prompt、自己调 LLM、为判断「是否注入游戏上下文」单独跑一次 LLM 分类（那等于第二条 LLM 路径）。gate 只能用显式 `interaction_mode` / `conversation_id` 命名空间 / active session / command intent / 关键词启发式。

---

## 4. galgame 子系统核心规则（详见 PLAN，这里是必记要点）

- **唯一状态 owner = `GalgameCompanionSession`。** session 活状态（FSM state、stable_current_line、未总结行 id、窗口绑定等）只有它能改。外部只能通过 `start/pause/resume/end/on_ocr_result/on_window_lost/on_choice_detected/on_user_reported_choice/on_summary_finished` 这些方法提交事件。
- **OCR loop 串行，不准重叠。** 用「完成后等 1 秒」模型，不是固定 tick 叠加。RapidOCR 推理可能 >1 秒且未必线程安全；同一 adapter 实例用锁或单 worker 队列。
- **总结 / 问答读不可变 snapshot，不碰可变 buffer。** 后台总结启动时切出 `source_line_ids` 快照；问答读 committed 历史快照 + 由 owner 原子读一次当前 `pending_current`。禁止总结任务持有可变 buffer 引用、禁止 `run_turn` 直接读写 session 内部 list。
- **截图边界（隐私）：** v1 **不承诺**离屏窗口捕获。只在「游戏窗口可见 + 未被非 Spica 窗口遮挡 + Spica overlay 没盖住 OCR 区域 + 窗口可可靠识别」时才 OCR；任一条件破坏立即暂停并提示，绝不误截其他应用。v1 建议游戏用**窗口化 / 无边框窗口化**，不承诺独占全屏体验。
- **四类记忆分清：** ① 角色关系记忆 = 现有 `MemoryScope`（只读/复用，不重造）；② 游戏档案 `GameProfile`（启动/窗口/OCR 配置，按 `game_id`）；③ 剧情进度（`GameProgressState` / `StorySummary` / `StoryLine` 等，客观事实）；④ 陪玩共同记忆 `CompanionBeat`（你和 Spica 一起玩形成的主观经历，绑 `character_id + user_id + game_id`）。
- **OCR / screen 模型不双加载。** `OCRPort` 复用现有 RapidOCR 初始化；选项识别用现有 VLM 定位 + RapidOCR 抽字，**不让描述型 VLM 直接生成精确选项文字**，不把整帧 OCR 当主路径。
- **语音输入 vs OCR 输入分流：** 用户语音/文字 → `run_turn` 或 command intent；OCR 文本 → `GalgameCompanionSession` 的 text stream，**绝不**直接变成用户消息。
- **OCR 剧情文本不写进 ChatEngine recent memory**，否则 recent memory 会爆并污染普通聊天。只有「用户 ↔ Spica 的问答 turn」写入对应 conversation_id 的 recent memory。

---

## 5. 怎么干活

> **通用改动**（非 galgame 专项）走 `docs/DEVELOPMENT_GUARDRAILS.md`（落点决策树 + 各类改动模板 + 该跑哪些测试）+ `docs/FUTURE_FEATURE_PLAYBOOK.md`（按需求查）。本节是 galgame 专项流程补充。

1. **先出计划，不要先改代码。** 进入实现前先按 `GALGAME_COMPANION_PLAN.md` §「Phase 0」输出：ports/adapters 列表、session 边界、并发模型、`run_turn` 注入点、UI 事件通道、状态机、FSM↔PlaySession 映射、数据模型、测试计划。
2. **先手喂文本验证读路，再接 OCR。** 顺序是先把「游戏记忆 → prompt 注入 → `run_turn` 回复」这条链路用手动喂文本跑通（Phase 2/3），最后才接最脏最飘的 OCR（Phase 7）。不要倒过来。
3. **改任何代码前，先说清楚将修改哪些文件、不会碰哪些边界。** 尤其不要在没说明的情况下动 `run_turn` / `ChatEngine` / `prompt_builder` / 现有 `MemoryPort`。
4. **每个改动后跑 `python -m pytest tests -q`。** 非确定性部分（OCR 去重、gated stage）用 golden frames / mock / 手喂数据测，不依赖真游戏。
5. **Phase 0 必须先回答 PLAN 里列出的开放问题**——特别是「进入 galgame 专属 conversation_id 后，现有 long-term retrieve 是否还能取到 Spica 平时关于麦的长期记忆」这个耦合点。**这要靠读 `adapters/memory/sqlite.py` 的真实 scope 逻辑回答，不准凭猜。**

---

## 6. 测试

- 命令：`python -m pytest tests -q`（永远不要裸 `pytest`）。
- 必测（不接真游戏）：模型序列化、conversation_id 生成、stable line 去重、`pending_current → committed` 转换、buffer = 未总结 committed 行、summary snapshot 不含 pending_current 也不含快照后新行、ChoiceEvent 两条路径、CompanionBeat 注入与隔离、FSM↔PlaySession 映射、choice_checking drain OCR、总结失败折叠。
- 手动验收（不强制自动化）：真实 Bottles 启动、真实 Wayland 截图、真实 VLM 选项定位质量、真实 R18 剧情总结质量。

---

## 7. 详细规格

完整数据模型、状态机、并发细节、phase 拆解、成功标准、开放问题 → **见 `GALGAME_COMPANION_PLAN.md`**。本文件与该规格冲突时，以 PLAN 中明确写出的细节为准；但本文件 §1 的铁律不可被覆盖。

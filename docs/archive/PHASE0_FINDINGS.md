# PHASE0_FINDINGS.md — Galgame 陪玩系统 · Phase 0 架构确认

> 配套 `CLAUDE.md` + `docs/GALGAME_COMPANION_PLAN.md`。
> 本阶段**未改任何代码**，唯一产物是本文件。所有结论基于真实源码（附 `文件:行号` 与关键片段）。
> 测试命令固定：`python -m pytest tests -q`。
> 读完请 review；**不进入 Phase 1**。

---

## Part A — PLAN §27 开放问题逐条回答

### ① conversation_id × 长期记忆耦合（最高优先，已读代码）

**读了哪些文件**

- `spica/runtime/stages.py:152-185`（`retrieve_long_term_memory_node`，喂 `[LONG_TERM_MEMORY]` 的那条路径）
- `spica/adapters/memory/sqlite.py:22-27, 38-39, 58-73`（`scoped_conversation_id` + `retrieve`）
- `memory/store.py:181-194`（`search_memories` 的真实 SQL）
- `spica/ports/memory.py:25-30`（`MemoryScope`）
- `spica/runtime/context.py:39, 41`（`TurnRequest.conversation_id` 默认值、`interaction_mode` 默认值）
- `spica/runtime/memory_commit.py:54-58`（commit 侧 scope）
- `memory/recent.py:7-34`（recent memory 命名空间）
- `spica/core/chat_engine.py:159-172`（`_ltm_conversation_id`）

**关键代码片段**

读路径构造 scope（`stages.py:159-168`）：

```python
scope = MemoryScope(
    character_id=str(deps.config.character.character_id or "spica"),
    user_id=str(deps.config.character.interlocutor_name or DEFAULT_INTERLOCUTOR_NAME),
    conversation_id=ctx.request.conversation_id,          # ← 用的是这一 turn 的 conversation_id
)
items = deps.memory.retrieve(scope, ctx.user_input, limit=...)
```

adapter 把 scope 压成命名空间 key（`sqlite.py:22-27, 58-59`）：

```python
def scoped_conversation_id(character_id, conversation_id) -> str:
    return f"{character_id}::{conversation_id or 'default'}"      # ← 复合 key
...
rows = self.store.search_memories(self._scoped_conversation_id(scope), query, limit=limit)
```

store 做**精确等值过滤**（`store.py:185-194`）：

```python
rows = conn.execute(
    "SELECT * FROM memories WHERE conversation_id = ? AND status = ? "
    "ORDER BY pinned DESC, importance DESC, updated_at DESC LIMIT 200",
    (conversation_id, DEFAULT_MEMORY_STATUS),          # ← conversation_id 是整条复合 key
).fetchall()
```

`TurnRequest` 默认值（`context.py:39, 41`）：`conversation_id = "default"`，`interaction_mode = "chat"`（注意：`interaction_mode` 字段**已存在**）。

**据此得出的结论**

- 长期记忆的隔离维度是 **`character_id::conversation_id` 复合 key**，不是单纯 `character_id`，且 `retrieve` 走 `WHERE conversation_id = ?` **精确匹配**。
- 普通聊天 `conversation_id="default"` → key = `spica::default`，Spica 关于「麦」的长期记忆全部存在这里。
- 陪玩若用 `galgame::<game_id>::playthrough::<...>` 作为 turn 的 conversation_id → key = `spica::galgame::...` → **匹配不到 `spica::default` 的任何行**。
- **明确结论**：若直接拿 galgame conversation_id 跑 `run_turn`，`retrieve_long_term_memory_node` **读不到** Spica 平时关于麦的长期记忆。这与 PLAN §6.1 / CLAUDE §1.8「galgame 只读取角色记忆、复用现有 MemoryPort」**冲突**，必须解耦，不能照搬。

**最终方案（已拍板：方案 A）**

把「turn 的 conversation_id」与「长期角色记忆检索用的 conversation_id」**解耦**——角色关系记忆始终用角色级命名空间，与游戏问答用的 conversation_id 无关。

- **引入 `memory_conversation_id` 概念**：长期角色记忆的检索（与对齐的 commit）不再直接用 turn 的 `conversation_id`，而用一个独立的 `memory_conversation_id`。
- **默认行为（现有普通 turn 零行为变化）**：`memory_conversation_id` **fallback 到 `conversation_id`**。普通聊天不设该值 → 两者相等 → `retrieve_long_term_memory_node` 行为与今天逐字节一致，golden 测试不变。
- **陪玩 turn 的使用语义**：
  - `conversation_id = galgame::<game_id>::playthrough::<playthrough_id>` —— 用于**游戏问答的 recent memory**（按裸 conversation_id 分桶，与普通聊天 `default` 互不污染，§15.3）。
  - `memory_conversation_id = "default"` —— 用于**读取 Spica 对用户（麦）既有的长期角色记忆**（命中 `spica::default`，§6.1）。
  - `retrieve_long_term_memory_node` 用 `memory_conversation_id` 构 `MemoryScope.conversation_id`；commit 侧同理对齐（见下「连带点 1」）。
- **不选方案 B（长期记忆改成 character-global）**。理由：**不在已硬化（C0–C8）的系统上、为了 galgame 去改全局 memory 语义**。方案 A 是加性、可回退、对现有路径零行为变化的最小改动。
- 满足铁律：不另起第二条 LLM 路径、不重造记忆系统、galgame 只读角色记忆、不污染现有 `MemoryScope`。

> 字段命名 / 默认 / 语义在此定稿。**是否真改 `TurnRequest` / `TurnContext` / `retrieve_long_term_memory_node`、改哪些行**，留到 Phase 1/3 的实现计划逐文件列出——**本轮不改任何代码**。

**两个连带点（Phase 3 决策，先记下）**

1. **commit 侧**：`memory_commit.py:54-58` 也用 `ctx.request.conversation_id` 构 scope。galgame 剧情问答 turn 是否应把长期记忆 commit 到角色命名空间、或干脆不 commit 剧情问答（避免把剧情写进麦的关系记忆），是 Phase 3 决策。方案 A 下同样用 `memory_conversation_id` 即可对齐读写。
2. **recent memory 天然 OK**：`recent.py` 按**裸 conversation_id** 分 deque。陪玩问答写 galgame conversation_id 的 recent、普通聊天写 `default` 的 recent，**互不污染**——这正是 §15.3 想要的，现有结构已支持，无需改动。OCR 剧情文本不进 recent（§4.6/铁律），由 GalgameCompanionSession 写 StoryLine。

---

### ② `route_key` 落点确认（按 PLAN 回答）

PLAN §9 已把 `route_key`（v2 多线路 key）补进 `PlaySession`(§9.6) / `StorySummary`(§9.8) / `GameProgressState`(§9.9)，v1 恒为 `null`。

- **结论**：v1 不引入第二维度做多线路 key，沿用 `route_key`，与 §13.6「数据结构已预留 playthrough_id / route_key」一致。
- **Phase 1 落地要求**：models（`spica/galgame/models.py`）与 `game_memory/sqlite.py` 的表结构都要**物化 `route_key` 列（nullable，默认 `null`）**，避免「预留承诺 vs schema 缺字段」再次脱节。v1 写入恒 `null`/`"default"`，查询不以它为过滤键。无需读现有代码——这是新 schema 的设计确认。

---

### ③ 后台总结进行中又触发一次总结（按 PLAN 回答）

- §13.7 只禁「同一批 `source_line_ids` 重复总结」，没禁「不同批并发」。
- **结论 / 决策时机 = Phase 8**（不在 Phase 0 写死）。建议 v1 简单化：**同一时间只允许一个 in-flight summary job**，飞行期间新 committed 行不进当前 snapshot（§4.4 第 5/6 条），排队等下一轮折叠（§13.7 第 5 条）。
- 实现上对应 §4.5 的 `JobRunner` / 单 worker 队列；接口预留、不写死并发（§4.5 末）。本项目已有 `spica/runtime/jobs.py`（`ThreadJobRunner` / `InlineJobRunner`）可作参考范式，但 galgame 总结队列是独立的 model job 排队，Phase 8 再定。

---

### ④ `RuntimeEvent` vs 新 `CompanionRuntimeEvent`（已读代码）

**读了哪些文件**

- `spica/core/events.py:20-31, 177-189, 192-254`（`RuntimeEvent` 基类、`GenericEvent` 兜底、`_FROM_DATA` + `event_from_legacy`）
- `spica/runtime/turn.py:30-44`（`run_turn` 是唯一 emit 点，把 legacy dict 转 `RuntimeEvent`）
- `spica/runtime/orchestrator.py:52-74, 144-148`（事件经 `output_queue` 由 producer 线程 → 生成器 yield）
- `spica/core/chat_engine.py:114-136`（`stream_voice` → `to_legacy_dict()`）
- `ui/workers/chat_worker.py:35-53`（`ChatWorker(QThread)` 迭代 `stream_voice`，发 `stream_event` Qt signal）
- `ui/controllers/chat_stream_controller.py:288-330`（消费 `stream_event`，`event_from_legacy` 驱动状态机）
- `tests/test_runtime_events.py:106-110`（未知 kind → `GenericEvent` 无损兜底）

**关键事实**

1. `RuntimeEvent` **不是 enum**，是一个 frozen dataclass 基类 + `kind: ClassVar[str]` + `to_legacy_dict()`（`events.py:20-31`）。「扩展枚举」实际等于「加 `RuntimeEvent` 子类」。
2. 整条传输链只认 **`RuntimeEvent` 基类 + `kind` 字符串 + `to_legacy_dict` + `event_from_legacy`**。未注册的新 kind 会落到 `GenericEvent` **无损透传**（`events.py:177-189`，`test_runtime_events.py:106-110`），不会丢事件。
3. **现有 Host→UI 通道是「每 turn 拉取式流」**：`ChatWorker` 这个 QThread 迭代 `chat_engine.stream_voice(...)` 的生成器，逐条发 Qt signal。**没有**面向后台异步事件的通用事件总线。

**据此得出的结论（两部分）**

- **事件类型**：galgame 事件应作为**新的 `RuntimeEvent` 子类**（`kind = "galgame_*"`），放在新模块 `spica/core/companion_events.py`（subclass `RuntimeEvent`），并在 `events.py` 的 `_FROM_DATA` 注册以获得**带类型的**往返；**不要**另起一个并列的 `CompanionRuntimeEvent` 基类。
  理由：并列基类要复制一整套 `to_legacy_dict` / `event_from_legacy` / UI 消费管线；而复用 `RuntimeEvent` 子类即可白嫖现有传输与无损往返（不注册也能 `GenericEvent` 兜底，注册后升级为强类型）。命名上可把这组子类叫「companion events」，但**继承自 `RuntimeEvent`**。
  事件清单按 PLAN §27④ / §25.5：`galgame_status_changed` / `galgame_ocr_preview_ready` / `galgame_ocr_test_result` / `galgame_stable_line_committed` / `galgame_window_lost` / `galgame_window_recovered` / `galgame_summary_started|progress|done` / `galgame_choice_detected` / `galgame_choice_recorded` / `galgame_error`。
- **传输通道（这才是 ④ 的主要工作量）**：galgame 事件**不来自 `run_turn`**——它们由 GalgameCompanionSession 的后台线程（OCR loop、总结 job、窗口监控）异步产生，**现有的每-turn `ChatWorker` 流装不下**。因此需要**新增一条长生命周期事件通道**：后端 session 通过**注入的 sink 回调** emit dataclass（保持 Qt-free，铁律 §1/§2），`ui/` 提供一个 bridge 订阅并 marshal 回 Qt 主线程（与 `ChatWorker.stream_event` 同样的「后端只 emit、UI 主线程消费」模式）。

  > 注意守卫：`tests/test_layering.py` 的 N1-final 只限制 `spica/runtime/stages.py` + `spica/conversation/**` 不许 import `spica.core.events`。`spica/galgame/session.py` 产出 companion 事件不在该清单内——但它必须**通过注入的 sink** emit，仍是 Qt-free；emit `RuntimeEvent` 是允许的（它不是「纯转换 stage」，而是新的事件源，类似 `run_turn`）。

---

### ④补充 — session→UI 长生命周期事件通道（设计定稿）

这是一条**独立于每-turn `run_turn` 流**的新通道，专供 `GalgameCompanionSession` 的后台线程（OCR loop / 总结 job / 窗口监控）异步把状态推给 UI。现有 `ChatWorker` 是「UI 拉一次 turn 的生成器、随 turn 结束而终止」，装不下这种长期、由后端主动触发的事件，所以必须单列。定稿如下。

**1. sink 的形状（Qt-free callable）**

- 契约：`CompanionEventSink = Callable[[RuntimeEvent], None]`（或等价的 `Protocol`，带 `emit(event)`）。选 **callable**，对齐 orchestrator 里 `put_unit_event(event_name, data)` 那种「后端只调注入回调」的既有风格（`orchestrator.py:147-148`）。
- 要求：**线程安全 + 非阻塞**——OCR / 总结线程调用它不能被 UI 卡住。类型定义落在 `spica/`（Qt-free）。
- **默认实现 = no-op sink**：没挂 UI 时（headless / 测试 / 尚未开陪玩）session 也能跑，**绝不硬依赖活的 UI**。

**2. 在哪注入**

- `spica/host`（`app_host.py` / 新 `galgame_assembly.py`）和 `spica/galgame` 都是 Qt-free，**不能**在这里 new 一个 QObject。所以**具体 sink（Qt bridge）在 `ui/` 创建，再向下注入**：
  - `ui/` 构造 `CompanionEventBridge(QObject)`（见第 4 点），取其 Qt-free 的 `sink` 回调；
  - 经 `AppHost` 的一个 seam（如 `attach_companion_sink(sink)`，或一个 companion surface）交给 host assembly；
  - host assembly 在构造 `GalgameCompanionSession` 时把该 sink 注入（构造默认 no-op，UI 挂载后替换 / 转发）。
- 与现有「UI 调 `AppHost().initialize()` 后读 `conversation_surface`」一致：只是多一个 companion 维度的注入点，host 仍薄（铁律 §1.5）。

**3. `GalgameCompanionSession` 只依赖 Qt-free sink**

- session 只持有 `self._emit: CompanionEventSink`；**不 import Qt、不碰任何 widget**（铁律 §1.1 / §1.2）。
- 后台线程（OCR / 总结 / 窗口监控）状态变化 → `self._emit(GalgameStatusChangedEvent(...))` 之类；session 永远只发 dataclass，从不直接刷 UI（铁律 §1.2 + §4.1）。

**4. UI 侧：谁持有 / 谁消费 / 怎么 marshal**

- `ui/` 新增 `CompanionEventBridge(QObject)`：持有一个 Qt `Signal`（如 `companion_event = Signal(object)`，或对齐 `ChatWorker` 的 `Signal(str, dict)`），其 `sink(event)` 方法体只做 `self.companion_event.emit(event)`。
- 关键：信号从**非 GUI 线程**发往主线程的槽走 **Qt 队列连接（queued connection），自动 marshal 回 Qt 主线程**——与 `ChatWorker.stream_event`（`chat_worker.py:51`）→ `ChatStreamController._handle_stream_event`（`chat_stream_controller.py:288`）**完全同一机制**，不引入新并发原语。
- 新增 `ui/controllers/galgame_controller.py`（与 `ChatStreamController` 并列）连接该信号，**在主线程消费**：按 `event.kind` 分发，驱动 overlay 状态、OCR 预览、窗口丢失提示、选项确认、总结进度等 widget。

**5. 为什么复用 `RuntimeEvent` 子类，而不是新建平行事件基类**

- 复用同一套事件类型系统：UI 侧 per-turn 与 companion 两路可统一走 `event_from_legacy` / `event.kind` 分发；未注册 kind 还有 `GenericEvent` 无损兜底（`events.py:177-189`，`test_runtime_events.py:106-110`）。
- 新建平行 `CompanionRuntimeEvent` 基类 = 复制一整套 `to_legacy_dict` / `event_from_legacy` / 分发逻辑，是纯负担。
- 子类放 `spica/core/companion_events.py`，在 `events.py` 的 `_FROM_DATA` 注册以拿强类型往返（不注册则退化为 `GenericEvent`，仍可用）。

**6. 先进入设计的事件**

`galgame_status_changed`（FSM 状态变化）、`galgame_window_lost` / `galgame_window_recovered`、`galgame_summary_done`（及 `galgame_summary_started` / `galgame_summary_progress`）、`galgame_stable_line_committed`、`galgame_choice_detected`（及 `galgame_choice_recorded`）、`galgame_ocr_preview_ready` / `galgame_ocr_test_result`、`galgame_error`。

---

### ⑤ `OCRPort` 能否直接包现有 screen 工具链（已读代码）

**读了哪些文件**

- `agent_tools/function_tools/screen/backends/rapidocr.py:11-13, 16-46, 49-70`（RapidOCR 持有方式 + `ocr_image` 入口）
- `agent_tools/function_tools/screen/model_manager.py:44-59, 136-156, 267-281`（Moondream 单例 manager，含 `_infer_lock`）
- `agent_tools/function_tools/screen/analyzer.py:20-89`（OCR + Moondream 怎么被组合调用）
- `agent_tools/function_tools/screen/config.py:13-29`（`ocr_enabled` 默认 `True`、`ocr_engine="rapidocr"`）
- `agent_tools/function_tools/screen/capture.py:15-51`（现有 capture 只截「主显示器全屏」）
- `spica/ports/screen.py` + `spica/adapters/tools/screen.py`（`ScreenAnalysisPort` + `InspectScreenTool` 现状）
- `tests/test_layering.py:67-80`（N3-layer：`spica` 不许 import `agent`，但 **`agent_tools` 允许**）

**关键代码片段**

RapidOCR 是**进程级模块单例**（`backends/rapidocr.py:11-13, 55-70`）：

```python
_ENGINE: Any | None = None
_ENGINE_LOCK = RLock()
...
def _get_engine():
    global _ENGINE
    with _ENGINE_LOCK:                 # ← 只锁「拿引擎」
        if _ENGINE is not None:
            return _ENGINE
        _ENGINE = _load_rapidocr_class()()   # RapidOCR()  默认配置、无语言参数
        return _ENGINE
```

入口 `ocr_image(image)`（`rapidocr.py:16-34`）：接受 PIL 图或 PNG bytes，返回 `{"engine","raw_text","blocks","error"}`，`blocks=[{"text","confidence","box"}]`。

**据此得出的结论**

- **能直接复用，写一个薄 bridge adapter，不重新加载模型**：`spica/adapters/ocr/rapidocr.py` 只需 `from agent_tools.function_tools.screen.backends.rapidocr import ocr_image` 并包成 `OCRPort`。因为 `ocr_image` → `_get_engine` 命中同一个 `_ENGINE` 全局，**galgame OCR 与 `inspect_screen` 天然共用同一份 RapidOCR**（满足 §8「只加载一份」）。N3-layer 允许 `spica` import `agent_tools`，不违规。
- **选项识别的 VLM 定位**复用 `model_manager.get_moondream_manager(...)`（也是带 `_infer_lock` 的全局单例，`model_manager.py:267-281`）或经 `ScreenAnalysisPort`；选项文字仍走 `ocr_image` 抽取（§8/§14.2）。两者都复用既有单例，不双加载。

**caveat —— 全部标为后续硬约束（binding，Phase 7/9 必须满足，不是「可选优化」）**

1. **RapidOCR 当前是模块级全局单例**：`backends/rapidocr.py:11-13` 的 `_ENGINE` + `_ENGINE_LOCK`，全进程一份。复用它正是「只加载一份」的依据，但也意味着 galgame OCR 与 `inspect_screen` 共享同一个实例。
2. **现有推理未在锁内串行**：`rapidocr.py` 只锁「拿引擎」（`_get_engine`），`engine(prepared)` 推理在锁外（对比 Moondream `model_manager.py:143` 的 `_infer_lock` 把推理也锁住）。
3. **跨 `inspect_screen` 与 galgame OCR 并发时不安全**：`inspect_screen` 默认开 OCR（`config.py:23 ocr_enabled=True`）。若用户问答 turn 触发 `inspect_screen` 时 galgame OCR loop 正在跑 → 同一 RapidOCR 实例上**并发两次推理**，正是 §4.3 警告的场景。galgame 内部 OCR loop 本身串行（§4.3），但**跨路径**并发无人兜底。
4. **【硬约束】Phase 7/9 必须用锁或单 worker 队列解决**：在 OCRPort bridge / session 侧加**共享推理锁或单 worker 队列**，串行化所有经 `ocr_image` 的推理（含 `inspect_screen` 路径）。Phase 0 只标注、**不擅自改 `agent_tools`**；但这是 binding，不能漏。
5. **【硬约束】区域裁剪必须在调用 OCR 前做**：`ocr_image` 是整图 OCR。galgame 的对白/名字区域裁剪要在调用前完成（裁好的 PIL 子图再喂 `ocr_image`），不把整帧 OCR 当主路径（§8 禁止项；整帧只能作低置信度 fallback 并标记）。
6. **【硬约束】按窗口/区域截图是真新增**：现有 `capture.py` 只截「主显示器全屏」（`capture_full_screen`，mss + `_primary_monitor`），**没有**按窗口/区域捕获能力。`ScreenCapturePort`（`mss_visible_window`）是**真正新增**，不是复用——与「OCR/VLM 模型复用」是两回事，别混淆。

---

### ⑥ 跨平台正交提醒（不阻塞 v1，记录边界）

跨平台打包/运行是与 galgame **功能**正交的独立工作流：

- 现有 `capture.py` 是 **mss + Linux 主显示器全屏**；Wayland 完整窗口捕获、Windows PrintWindow/WGC/DXGI 都不在 v1（§7.1）。
- `config.py:61-103` 读了多个 `SPICA_SCREEN_*` 环境变量——它在 `agent_tools` 包内，**不属于 `spica` 业务代码**，不违反「业务代码禁 `os.getenv`」铁律（守卫 `tests/test_no_getenv.py` 只扫 `spica`）。galgame 自己的配置仍走注入 config，不得新增 `os.getenv` 到 `spica`。
- **结论**：Windows launcher/locator/capture、Wayland 窗口捕获**单独排期**，不混进 galgame 功能里做。v1 端口设计预留 `platform` 字段（§9.2/§9.3）即可，adapter 只实现 Linux。

---

## Part B — 五项交付物

### B1. 会触碰哪些现有文件

#### (a) Phase 0「只读」已查阅（本次未改动任何一个）

| 关注点 | 文件 |
| --- | --- |
| 记忆读路 ① | `spica/runtime/stages.py`、`spica/adapters/memory/sqlite.py`、`memory/store.py`、`spica/ports/memory.py`、`memory/recent.py`、`spica/runtime/memory_commit.py`、`spica/core/chat_engine.py` |
| turn / 编排 | `spica/runtime/context.py`、`deps.py`、`turn.py`、`orchestrator.py`、`sync_chain.py` |
| prompt | `spica/conversation/prompt_builder.py`（`[LONG_TERM_MEMORY]` 在 `:155`） |
| 事件 ④ | `spica/core/events.py`、`ui/workers/chat_worker.py`、`ui/controllers/chat_stream_controller.py`、`tests/test_runtime_events.py` |
| 装配 / 注册 | `spica/host/app_host.py`、`spica/host/builtins.py`、`spica/plugins/registry.py`、`spica/config/schema.py` |
| OCR/screen ⑤ | `agent_tools/function_tools/screen/{backends/rapidocr.py, model_manager.py, analyzer.py, config.py, capture.py}`、`spica/ports/screen.py`、`spica/adapters/tools/screen.py` |
| 守卫 | `tests/test_layering.py` |

#### (b) Phase 3+「将修改」的现有文件（全部 gated；对普通聊天承诺零行为变化）

| 文件 | 改什么 | Phase |
| --- | --- | --- |
| `spica/runtime/context.py` | `TurnRequest` 加 `memory_conversation_id`（①，默认=conversation_id）、`command_intent`、`game_context_request`、`game_context_snapshot`（session 原子读出的 pending_current + committed 快照）；`TurnContext` 加 `game_context` 子对象（None 直到 stage 跑） | 3 |
| `spica/runtime/deps.py` | `TurnDeps` 加 `game_memory: GameMemoryPort | None`；`from_services` 注入 | 3 |
| `spica/runtime/stages.py` | 新增 gated `retrieve_game_context_node`；`retrieve_long_term_memory_node` 改用 `memory_conversation_id`（①） | 3 |
| `spica/runtime/orchestrator.py` | prep 序列插入一行 `retrieve_game_context_node`（gated，见 B3） | 3 |
| `spica/runtime/sync_chain.py` | 同步链插入同一节点（保持两路一致） | 3 |
| `spica/core/chat_engine.py` | `_request` / `run_voice` / `stream_voice*` 透传新 request 字段 | 3 |
| `spica/runtime/memory_commit.py` | galgame 问答 commit 的 scope 决策（①连带点 1） | 3 |
| `spica/host/app_host.py` | 装配 galgame 子系统、把 `game_memory` 注入 deps | 4–5 |
| `spica/host/builtins.py`（或新 `galgame_assembly.py`） | 构建/登记 galgame adapters | 4–5 |
| `spica/core/events.py` | `_FROM_DATA` 注册 `galgame_*` 事件（②的强类型往返） | 4 |
| `ui/`（controllers/workers/widgets） | 新增 companion 事件通道 + 框选/预览/确认/校准 UI | 4–9 |
| `tests/test_layering.py` | **仅当**把 game stage 放到新文件时，才把它加进 `TRANSFORM_LAYER_FILES`（若放进 `stages.py` 则已被守卫覆盖，无需改） | 3 |

#### (c) 明确「不会碰」的边界

- **不碰 `agent_tools/function_tools/screen/` 的模型加载逻辑**：只 `import` 复用 `ocr_image` / `get_moondream_manager`（⑤）。唯一可能的小改动（RapidOCR 跨路径推理串行锁，⑤ caveat 1）也只在确认需要时、Phase 7/9 单独提出，不在 Phase 0 假设。
- **不改现有 `MemoryPort` / `MemoryScope` 语义**：galgame 用独立 `GameMemoryPort` + 独立 sqlite DB（铁律 §1.8）；①只做**加性**的 `memory_conversation_id`，不改现有行数据语义。
- **不动 `run_turn` 单一 emit 路径**：galgame 问答仍走 `run_turn`；galgame 异步事件走**另一条**通道（④），二者不混。
- **不 import `agent`**（N3-layer，已删包）；不新建 `spica/platform/` 平行树（铁律 §1.7）。
- **不往 `spica/` 引入 Qt**、不往 `spica` 业务代码引入 `os.getenv`。

---

### B2. 新增 ports / adapters / models（按 CLAUDE §2 布局）

```
spica/ports/
  game_launcher.py     # GameLauncherPort   启动/绑定游戏（铁律 §1.9，LLM 不直接 exec）
  window_locator.py    # WindowLocatorPort  候选窗口枚举 + 标题关键词匹配 + 安全可见性判断
  screen_capture.py    # ScreenCapturePort  按窗口/区域截图（真正新增，现有只有全屏）
  ocr.py               # OCRPort            ← bridge 复用 ocr_image，不重载模型（⑤）
  game_memory.py       # GameMemoryPort     ④类游戏记忆的读写（gated stage 依赖它读 committed）

spica/adapters/
  game_launcher/linux_desktop.py     # desktop entry 扫描 / command / manual bind
  window_locator/linux_x11.py        # 标题关键词 + 用户确认（§17.3）；平台字段预留 windows
  screen_capture/mss_visible_window.py
  ocr/rapidocr.py                    # from agent_tools...rapidocr import ocr_image（薄 bridge）
  game_memory/sqlite.py              # 独立 DB（如 spica_data/galgame.sqlite3），不碰 memory.sqlite3

spica/galgame/                       # domain / session 层（Qt-free）
  models.py        # §9 全部数据模型（含 route_key，②）
  session.py       # GalgameCompanionSession：唯一状态 owner（§4.2）+ companion 事件 emit（④）
  text_stream.py   # stable line 去重 / pending_current→committed（§10）
  summarizer.py    # 2000 字 / 结束 / 崩溃补总结，读不可变 snapshot（§13）
  choices.py       # ChoiceEvent 两条路径（§14.4）
  commands.py      # 自然语言 → 固定 intent（§20）

spica/core/
  companion_events.py  # galgame_* RuntimeEvent 子类（④；继承 RuntimeEvent，非新基类）

ui/                    # 框选 / 预览 / 确认 / 校准 + companion 事件 bridge（④）
```

**数据模型（`spica/galgame/models.py`）**：实现 PLAN §9.1–§9.12 全部——`GameProfile` / `LaunchProfile` / `WindowMatchRule` / `OCRProfile` / `OCRRegion` / `PlaySession` / `StoryLine` / `StorySummary` / `GameProgressState` / `CharacterRelation` / `ChoiceEvent` / `CompanionBeat`，均含 `route_key`（②）。

**装配方式**：这几个 v1 单实现端口**不**走 `CapabilityRegistry` 的「按 config 名解析」（registry 现仅支持 llm/tts/visual/memory/tool 五类，`registry.py:39-45`），而是在 host 里**薄装配**（`app_host.py` 或新 `galgame_assembly.py`）直接注入 `GalgameCompanionSession` 与 `TurnDeps.game_memory`。端口本身仍按 CLAUDE §2 保留 Protocol + adapter（为 Windows 后续 adapter 与可测性），但不强加 config-name 解析（YAGNI）。`OCRPort` 若希望和 `inspect_screen` 一样可被替换，可后续登记入 registry，但 v1 不必。

---

### B3. `retrieve_game_context_node` 插入点与 gate 行为（展开定稿）

**现有 prep 序列**（生产 stream + sync 同源，`orchestrator.py:235-251`；测试用同构链 `sync_chain.py:36-49`）：

```
validate_input → load_recent_context → retrieve_long_term_memory
→ analyze_screen_attachment → build_prompt → (LLM…)
```

`build_spica_prompt`（`prompt_builder.py:147-175`）拼成固定顺序串、`[CURRENT_USER_INPUT]` 在末尾；screen observation 由 `_inject_screen_observation`（`stages.py:663-678`）**在 build_prompt 之后追加到串尾**。这就是要照搬的范式。

**1. 放在哪个 runtime stage 附近**

- 新增节点 `retrieve_game_context_node`，定义在 **`spica/runtime/stages.py`**（已被 `test_layering` 的 `TRANSFORM_LAYER_FILES` 覆盖；它返回 ctx、不 import / 不 emit `spica.core.events`，满足 N1-final，无需改守卫）。
- 插入点：**紧跟 `build_prompt_node` 之后、LLM 调用（`call_llm_node` / 流式 LLM）之前**。两处同插同一节点：`orchestrator.py`（`build_prompt_node` 之后、进入流式 LLM 之前）与 `sync_chain.py`（`build_prompt_node` 之后、`call_llm_node` 之前），保持两路一致。

**2. 如何把五段追加进 prompt 输入**

- 命中 gate 后，节点从 `deps.game_memory`（committed：GameProgressState / 最近 StorySummary / ChoiceEvent / CharacterRelation / CompanionBeat）+ `ctx.request.game_context_snapshot`（session 原子读出的 pending_current + 未总结 committed buffer 快照，§4.4 / §11）组装内容；
- 把段落 **追加到 `ctx.prompt.prompt_input`**，**完全照搬 `_inject_screen_observation` 的追加模式**（在已建好的 prompt 串尾接 section）；
- active 模式注入：`[GAME_PROGRESS]` / `[CURRENT_GAME_BUFFER]` / `[GAME_RELATIONS]` / `[GAME_CHOICES]` / `[COMPANION_CONTEXT]`；
- **不改 `build_prompt_node` 主体、不改 `prompt_builder.py`**（最小承重面）。
- *权衡*：游戏段落出现在 `[CURRENT_USER_INPUT]` 之后（与 screen observation 一致），对 LLM 一般无碍；若严格要求排在用户输入之前，备选才是给 `build_spica_prompt` 传 `game_context` 参数（要改 `prompt_builder.py`，承重更大）——**v1 用追加式**。

**3. gate 不命中 = 一个字都不出现（含空标题）**

- 模式判定为 `none` 时，节点**直接 `return ctx`，对 `prompt_input` 零修改**：不追加任何段落，**绝不输出空标题**（不能出现光秃秃的 `[GAME_PROGRESS]` / `[CURRENT_GAME_BUFFER]` 之类）。
- 即使命中模式，但某段内容为空，也**连该段标题一起省略**（条件构建段落列表，空则整段跳过）。
- 结果：普通聊天 turn 的 prompt 串**逐字节不变**——这是 Phase 3 不破坏主轴的硬验收（golden 据此保持绿）。

**4. offline query mode 的子集**

- 触发：`request.command_intent ∈ {ask_last_progress, ask_game_progress, ask_character_relation}` 或 `game_context_request.mode == "offline"`（§5.2 / §15.2）。
- 只注入 `[GAME_PROGRESS]` / `[RECENT_GAME_SUMMARIES]` / `[GAME_RELATIONS]` / `[GAME_CHOICES]`。
- **默认不注入** `[CURRENT_GAME_BUFFER]` 与 `[COMPANION_CONTEXT]`（仅当用户明确问共同经历，才追加 `[COMPANION_CONTEXT]`，§5.2）。

**5. active mode 的判定信号（纯 request 字段）**

- 任一成立即 `active`：`request.interaction_mode == "galgame"` / `conversation_id` 属于 `galgame::…` 命名空间 / `request.game_context_request.mode == "active"`（由陪玩 UI / command router 设）。注入全部五段。
- pending_current **必须**经 `request.game_context_snapshot` 传入（§4.4 第 8 条 / §15.1）——stage 是纯转换，不得持有可变 session 引用。

**6. gate 绝不额外跑 LLM**

- active / offline / none 的判定**纯用 request 字段**：`interaction_mode` / `conversation_id` 是否属 `galgame::…` / `command_intent` / `game_context_request.mode`。
- **禁止**为「是否注入 game context」单独跑一次 LLM 分类——那等于第二条 LLM 路径（§5.3 / 铁律 §1.3）。Phase 3 测试断言「注入与否只由 request 字段决定」。

---

### B4. 手动喂文本最小闭环（Phase 2，不接 OCR）

**目标**：不用游戏窗口、不用 OCR，就把五类游戏数据写进存储，使 Phase 3 能完整测「五块注入 → run_turn 回复」。

**接口（PLAN §24 Phase 2）**——放在 `GameMemoryPort` 写方法之上的一组 manual facade（Phase 4 之前不依赖 FSM session）：

```
manual_add_story_line(game_id, speaker, text)          # → StoryLine(source="manual", status=committed)
manual_flush_summary(game_id)                          # → 触发一次 summarizer，落 StorySummary
manual_set_progress_state(game_id, **fields)           # → upsert GameProgressState
manual_add_choice_event(game_id, options, selected)    # → ChoiceEvent（含「无 options 仅 selected_text」的 manual 分支，§14.4）
manual_add_companion_beat(game_id, type, content)      # → CompanionBeat（绑 character_id+user_id+game_id）
```

**数据怎么落**：全部经 `GameMemoryPort` → `game_memory/sqlite.py`，写入**独立 DB**（如 `spica_data/galgame.sqlite3`），与 `memory.sqlite3` 物理隔离（铁律 §1.8）。conversation_id 由 helper 生成：`galgame::<game_id>::playthrough::<playthrough_id>`（§15.3，对应单测）。**不写 RecentMemory、不写现有 MemoryPort**（§4.6）。

**Phase 3 怎么靠它测五块注入**：

1. 用 manual_* 喂入 progress / story line(committed) / relation / choice / companion beat；
2. 调 `ChatEngine.run_voice(..., game_context_request={"mode":"active", "game_id":...})`（active）或带 `command_intent=ask_last_progress`（offline）；
3. 断言 `ctx.prompt.prompt_input`（或经 fold 的 payload）**含**对应 `[GAME_*]` 段；
4. 断言 `interaction_mode="chat"` 且无 game request 时**不含**任何 `[GAME_*]`、且 `[COMPANION_CONTEXT]` 不出现（§5.2 默认）；
5. 断言整轮只有一条 LLM/run_turn 路径（无第二次 LLM 分类，§5.3）。
6. **①回归**：用 galgame conversation_id 跑一轮，断言 `[LONG_TERM_MEMORY]` 仍取到角色（麦）记忆（验证 `memory_conversation_id` 解耦生效）。

---

### B5. Phase 1–3 测试方案（命令固定 `python -m pytest tests -q`）

**Phase 1（模型与存储）**

- 模型序列化往返：`GameProfile` / `PlaySession` / `StoryLine` / `StorySummary` / `GameProgressState` / `CharacterRelation` / `ChoiceEvent` / `CompanionBeat`（§25.1.1）。
- `conversation_id` 生成：`galgame::<game_id>::playthrough::<playthrough_id>`（§25.1.2）。
- `route_key` 字段存在且默认 `null`/`"default"`（②）。
- `game_memory/sqlite.py` CRUD + **作用域隔离**：写 galgame DB 不影响 `memory.sqlite3`（铁律 §1.8）；FSM↔PlaySession.state 映射表（§16.5 / §25.1.10）。

**Phase 2（手动喂文本）**

- manual_* 五个接口写入后可从 `GameMemoryPort` 读回（含 §14.4 两条 ChoiceEvent 路径）。
- OCR/剧情文本**不进** RecentMemory（断言 `RecentMemory.dump()` 不含剧情行，§4.6）。
- buffer = unsummarized committed ids；pending_current 不进 summary snapshot；summary 失败保留 source_line_ids（§25.1.5/6/12）——这些可在 Phase 2 用手动数据先建模测，OCR 部分留 Phase 7。

**Phase 3（run_turn 注入）**

- active 模式注入五段 / offline 模式回答「昨天玩到哪了」/ 普通聊天不被注入 / `CompanionBeat` 默认不进普通聊天 / 无第二条 LLM 路径（§25.2）。
- **①解耦回归**（最关键）：galgame conversation_id turn 仍读到角色长期记忆。
- gate 是纯信号、无 LLM 分类（断言注入与否只由 request 字段决定）。

**必须保持绿的现有守卫/golden**

- `tests/test_layering.py`（Qt-free / N3-agent / N1-events）；新 stage 放 `stages.py` 不需改守卫，放新文件需把它加进 `TRANSFORM_LAYER_FILES`。
- `tests/test_no_getenv.py`、`test_no_static_tool_schemas.py`、`test_no_log_timing.py`、`test_no_raw_threadpool.py`、`test_no_manual_reorder.py`。
- `tests/test_runtime_events.py`（companion 事件注册后补往返断言；未注册也应 `GenericEvent` 无损）。
- `tests/test_golden_streaming.py` / `test_golden_sync.py` / `test_memory_commit.py`：**gate 命中 none 时新节点是 no-op**，故普通聊天 golden 不变——这是 Phase 3 不破坏主轴的硬验收。

---

## Phase 0 结论

- ① 已用源码证明：长期记忆按 `character_id::conversation_id` 精确隔离，**陪玩切 conversation_id 会读不到角色记忆**；**已拍板方案 A**（加性 `memory_conversation_id`，fallback 到 `conversation_id`，对现有 turn 零行为变化），不选方案 B（不在已硬化系统上改全局 memory 语义）。
- ④ 复用 `RuntimeEvent` 子类（非并列基类），**且新增一条 session→UI 长生命周期事件通道**（Qt-free callable sink，UI 经 QObject bridge + Qt 队列连接 marshal 回主线程；设计已定稿成小节）。
- ⑤ `OCRPort` = 薄 bridge 包 `ocr_image`，**单份 RapidOCR**；新增的是「按窗口/区域截图」与「跨路径推理串行」，不是模型复用。
- ②③⑥ 按 PLAN：`route_key` v1 恒 null 但 Phase 1 schema 要物化；并发总结 v1 单 in-flight、Phase 8 定；跨平台单独排期。
- 新增端口/适配器/模型/domain 已按 CLAUDE §2 列清；`retrieve_game_context_node` 插在 `build_prompt` 之后、追加式注入、gated，承重面最小。

**停在此处，等 review，不进入 Phase 1。**

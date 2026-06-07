# Spica 核心 turn 硬化重构计划 v3（Core Hardening · 接在 Phase 8 之后）

> 配套文件：仓库根 `CLAUDE.md` + `docs/REFACTOR_PLAN.md`。建议本文件放在 `docs/REFACTOR_PLAN_CORE.md`。
> 本计划专门硬化 **Phase 6 没收完的核心**：把「一轮对话（turn）」从手搓线程汤变成系统的**承重抽象**。
>
> **这是一次硬化，不是重写。** 原则不变：strangler-fig；仓库一直可运行；`python -m pytest tests` 永远绿；
> 一阶段 = 一分支 = 一（或一串）commit；合回 main 前必须绿。
>
> **v2 变更**：拆出 C1.5（run_turn facade）；C3 拆成 C3a/b/c；Sequencer 明确为「排序原语 + 单消费者契约」；
> C2 只切主路径、不删旧文件；ToolSet 协议提前到 C3；C6 只后台化长期记忆；C0 断言分「主轴/遥测」两层；
> §1 约束改为「随阶段晋升」；新增 N7（UI 播放不在本计划内变）。
>
> **v3 变更**：C1 明确 `completion_queue`（producer 内部）≠ `output_queue`（对外），且保留 LLM streaming 主流程、
> 加「`done` 前必须 drain」；C1.5 明确 `run_turn` 产出 `RuntimeEvent`（legacy dict 只在 ChatEngine 兼容层）；
> `ExecStrategy` 协议钉死三 lane（TTS 串行进协议）；C2 把「逐字段一致」改成「关键字段白名单 + 差异白名单」；
> C3a 规定 observer/jobs/exec 非 None 默认实现；C4 把 `agent/` 退场拆成 4 个 commit；N4-observe 排除 adapter 层日志。

---

## §0 这次到底解决什么（诊断）

「缝」（ports / `CapabilityRegistry` / `RuntimeEvent` / `AppConfig` / Qt 隔离）已经做完，**别动**。
但 **turn 的执行模型没做完**，按危害排序：

1. **`spica/runtime/orchestrator.py` 是手搓线程汤。** producer 线程 + `queue.Queue` + `_SENTINEL` +
   三个 `ThreadPoolExecutor`（visual/tts/ready）+ `timing_lock`/`ready_lock` + `first_unit_timer`，
   再用 `ready_units` 字典 + `next_emit` 手动按 index 重排。组件（splitter/job）是纯的、没问题，
   **编排本身**还是命令式线程拼接，是全仓最脆、最核心的一块。
2. **同步与流式是两条重复的 pipeline。** `agent/runtime.py` 的 node 链和 `orchestrator` 重复同一套阶段。
3. **`spica/` 反向依赖 `agent/`。** `chat_engine` → `agent.runtime`，`orchestrator` → `agent.nodes`，
   `memory_commit` → `agent.character_loader`……所谓平台核心其实是套在 `agent/` 上的壳。
4. **`AgentState` 是 god-object 黑板。** ~25 个字段被十个 node + orchestrator 就地乱改，流式还在后台线程里改它。
5. **类型在边界上就被丢掉。** `AppConfig` 立刻被降级成 `services.config: dict[str, Any]`；
   `AgentServices` 同时背 `llm_client`+`llm_adapter`、`memory_store`+`memory_adapter`。

**北极星**：turn = 「请求 → 一条确定性、类型化的 `RuntimeEvent` 流」，由一串近乎纯函数的 stage 产出；
**并发、可观测性、记忆写入是注入进去的策略**。turn 一旦干净，唱歌 / T2I / 插件 / 第二角色都变成「往上加」。

**事件的两层模型（贯穿全计划，尤其 C0 断言）**
- **有序主轴**：`unit_ready`（按 index）→ `done`。**唯一需要排序保证**的东西，由 `Sequencer` 负责。
- **无序遥测**：`status` / `unit_text_ready` / `unit_visual_ready` / `unit_audio_*`。尽力而为、随做随发，
  其相对先后受线程调度影响，**不构成契约**。

---

## §1 不可破坏约束（INVARIANTS · 随阶段晋升）

继承 `CLAUDE.md` 原有 7 条（Qt 隔离 / main 永远能跑 / 机械搬迁零行为 / 配置只走 ConfigManager 无 `os.getenv` /
YAGNI / Host 薄 / 跨边界 dataclass）。本计划**额外**加下面的条目。

> **关键纪律：终态约束在它那一阶段落地、守卫测试变绿之前，不是现行规则——不要为了满足一条还没成立的约束去做额外大改。**
> 通用原则贯穿全程：**不许新增旧债，旧债按阶段清。**（即：任何阶段都不得新增裸 `ThreadPoolExecutor` /
> 散落 `log_timing` / 同步阻塞 `commit_turn` / 手动 index 重排；存量按各自阶段消除。）

| 约束 | 内容 | 何时成为铁律 |
| --- | --- | --- |
| **N0**（现行） | screen 工具必须保留 `is_screen_intent_explicit` 意图门，且**本地分析、绝不上传截图** | 已生效（永久安全规则） |
| **N0b**（现行） | 手动截图是 **attachment**（用户已决定「看这张」），**不是 tool**；不得改成由模型决定是否分析用户已附的图 | 已生效 |
| **N7**（现行） | **UI 播放语义不在本计划内变化。** `ChatStreamController` 接收的 legacy dict / `RuntimeEvent` 语义保持兼容；打字机/音频/立绘切换时序只因后端事件等价而自然变化，**不主动重写 UI 播放控制** | 已生效（整个计划期间） |
| **N2** | 有序释放只能走 `Sequencer`；不许出现手动 index 重排字典 | **C1 落地后** |
| **N4-concurrency** | 并发只能走注入的 `ExecStrategy`；业务 stage 内不许 `new ThreadPoolExecutor` | **C2 落地后** |
| **N3-config** | 运行时核心不许出现 `dict` 配置或 `client+adapter` 双字段兜底；只用 `AppConfig` + 已解析 port | **C3b 落地后** |
| **N1-final** | 只有 `run_turn` / `stream_answer` 能产出 `RuntimeEvent`；其余 stage 是 `(ctx, deps)->ctx`，不许自己 emit | **C4 落地后** |
| **N3-layer** | `spica` 不许 import `agent`（`agent/` 已删） | **C4 落地后** |
| **N4-observe** | turn/stage 编排层的计时只能走注入的 `TurnObserver`，不许直接 `log_timing`（adapter 内部低层诊断日志不在此限） | **C5 落地后** |
| **N4-memory** | 长期记忆 commit 走注入的 `JobRunner`；**recent memory 仍同步、在 `done` 前完成** | **C6 落地后** |
| **N5** | `inspect_screen` 由 `CapabilityRegistry` 注册、运行时从 registry 解析；不再读静态 `TOOL_SCHEMAS` | **C7 落地后** |

> 某阶段合回 main 且测试全绿后，把对应约束「晋升」为现行，并（在该阶段 In scope）加上守卫测试。
> `CLAUDE.md` 里的同名条目同步翻成「已生效」。违反任一现行约束 = 任务失败，回退；**不许删测试或加豁免**。

---

## §2 怎么用 Claude Code 跑每个阶段

### Session 协议（每阶段照此）

1. **基线**：跑 `python -m pytest tests -q` 确认全绿，记下基线（尤其 golden 测试数量与名字）。
2. **读约束**：读 `CLAUDE.md` 全文 + 本文件 §0/§1/§2 + 当前阶段那一节。
3. **先复述、再动手**：先用自己的话说清「这阶段做什么、会新建/改哪些文件、为什么不越界」，**停下来等确认**。
4. **只在 In scope 内动手**。需要碰 Out of scope 的文件 → **停下来报告**。
5. **小步快跑**：每个可独立验证的改动一个 commit。标 `mechanical` 的步骤额外确认行为零变化。
6. **绿着收尾**：结束前 `python -m pytest tests -q` 全绿；必要时人工 `python webui_qt.py` 起一次。
7. **golden 是契约**：标 `mechanical` 的步骤若 golden 变红，**立刻 revert 那个 commit**，先搞清差异再重做。

### 开场 prompt 模板（每阶段粘）

```
读 CLAUDE.md 和 docs/REFACTOR_PLAN_CORE.md 的 "阶段 <C?>" 一节，以及 §0 事件两层模型、§1 INVARIANTS、§2 协议。

先跑 `python -m pytest tests -q` 确认基线全绿（本仓库只能用 python -m pytest tests，绝不在根目录跑裸 pytest，
会扫到 vendored GPT-SoVITS 崩掉），把结果告诉我。

然后不要动手。先用自己的话把该阶段要做什么、会新建/改哪些文件、如何保证不越界，列给我看，停下来等我确认。

我确认后，只在该阶段 "In scope" 范围内执行，严格遵守 §1 当前已生效的 INVARIANTS，尤其：
- golden 区分主轴/遥测：主轴(unit_ready 顺序 / done 在其后 / error 后无 done)精确断言；遥测(status/visual/audio)只断有无，不断顺序次数
- 标 mechanical 的步骤零行为变化；golden 一红立刻 revert 那个 commit
- 不新增旧债（裸 ThreadPoolExecutor / 散 log_timing / 同步阻塞 commit / 手动重排）
- 一个 stage / 一个模块一个 commit，别整包搬

完成后逐条核对该阶段 "Acceptance"，跑 "Verify command"，确认 main 能启动、测试全绿。
任何需要越出 In scope 的改动，先停下来问我。最后给我一个简短 commit message。
```

### 阶段依赖顺序（严格，不要跳）

```
C0 → C1 → C1.5 → C2 → C3a → C3b → C3c → C4 → C5 → C6 → C7 → C8
契约  排序   turn   折叠   typed  杀dict  拆     拆    观测  记忆   工具轨   收尾
焊死  原语   壳    同步流  deps  config 黑板  agent/      后台   迁ToolPort
```

---

## §3 分阶段路线图

每阶段都标 `main 是否仍可运行` —— 答案永远是 **是**。

---

### 阶段 C0 — 契约焊死（golden 网加固，先做，零架构改动）

**Goal**：在动核心前，把「turn 的事件契约」按**两层模型**焊死，并给后续阶段一套**复用的宽松匹配器**，
避免并发策略一改就因无意义差异变红。

**In scope**
- 新建测试助手（`tests/support/event_asserts.py` 或就近）：
  - `assert_ordered_axis(events, expected_units)`：只校验 `unit_ready` 的 **index 单调升序**、`done` 在所有 `unit_ready` 之后、
    `error` 之后**不得**再有 `unit_ready`/`done`，以及每个单元的 `text`/`emotion` 内容。
  - `assert_telemetry_present(events, kinds)`：只校验某类遥测**出现/不出现**，**不校验**其相对顺序与次数。
- 用确定性 fake（复用现有 `FakeLLMClient` 等）覆盖 7 个场景，**只用上面两个助手断言**：
  1. 空输入 → **允许前置 `status`**；最终必须有 `error`；**不得**有 `unit_ready`/`done`。（不要断「第一个事件必须是 error」）
  2. 正常多句回复 → 主轴：`unit_ready` 按 index；`done` 在其后。遥测只断 `unit_text_ready` 出现。
  3. 工具轮（`inspect_screen` 命中意图）→ 断「工具路径被触发」+ 正常出单元；不断 status 出现几次。
  4. 手动截图 attachment → 断「该轮**不触发其他工具**」+ observation 注入 + 正常出单元。
  5. 模型返回非法/无 JSON → 文本启发式兜底，仍出单元。
  6. 单单元兜底（`created_units` 为空但 `answer` 非空）→ 至少一个 `unit_ready`。
  7. 流中途异常 → 有 `error`，其后无 `done`。
- 「dict ↔ RuntimeEvent」双向等价测试（`event_from_legacy` / `to_legacy_dict` 转一圈，**只比主轴字段**）。

**不要断言**：timing 数值；`unit_visual_ready` 与 `unit_audio_*` 的相对先后；`status(thinking)` 出现几次；
任何线程调度导致的非主轴 progress 顺序。

**Out of scope**：任何架构改动、任何文件搬迁。

**Acceptance**
- 7 场景全部用 `assert_ordered_axis` / `assert_telemetry_present` 覆盖；无任何对遥测顺序/次数或 timing 的硬断言。
- dict ↔ RuntimeEvent 主轴等价测试绿。
- `python -m pytest tests -q` 全绿。

**Verify**：`python -m pytest tests -q`　**Rollback**：纯新增，删掉即可。　**main 仍可运行**：是。

---

### 阶段 C1 — 抽出 `Sequencer`（排序原语，只替换 reorder buffer）

**Goal**：把「按 index 单调放行、完成顺序任意」抽成一个**纯排序原语**（不是并发原语），干掉手动重排。

**关键设计**：`Sequencer` **不承担线程安全**，约定**单消费者访问**——`_finalize_unit` 完成后不再直接 `put_ready`，
而是把 `(index, payload)` 投递到一个 **`completion_queue`**；由 `_produce_stream_events` 在**受控位置** drain 该队列、
对每项调 `sequencer.complete(...)`，再 emit 放行批次。**并发由 `ExecStrategy` 管、排序由 `Sequencer` 管**
（C2 接管并发；C1 先只换排序）。

> 两个 queue 不要混：`completion_queue` 是 orchestrator producer 的**内部**队列（只服务 Sequencer）；对外发
> `RuntimeEvent`/legacy event 的仍是既有的 `output_queue`。**C1 禁止把 `completion_queue` 暴露到 `stream_voice_events`
> 外部**，它只在 `_produce_stream_events` 内部服务 Sequencer。同时**保持现有 LLM streaming 主流程不变**——
> 别把 `_produce_stream_events` 重写成全新的消费循环，只是把「排序」那一小段换掉。

**In scope**
- 新建 `spica/runtime/sequencer.py`（纯、单消费者、无 `agent`/无 Qt、可单测；签名见附录 A）。
- 改 `orchestrator`：`_finalize_unit`（仍由 `ready_executor` 等齐 visual+tts）完成后把 `(index, unit)` 投递到
  `completion_queue`；在 `_produce_stream_events` 受控点 drain 并调 `sequencer.complete(...)` → emit 放行批次。
- **本阶段只删**：`ready_units` 字典、`next_emit`、`ready_lock`、`put_ready` 内部的排序逻辑。
- **本阶段保留** `ready_executor`（以及 `_finalize_unit` 等 visual+tts 完成的现有机制），留到 C2 由 `ExecStrategy` 接管。
- 新增 `tests/test_sequencer.py`：乱序 complete、空洞、单元素、重复 index 防御。

**Out of scope**：动 stage、动 `AgentState`、`run_turn`、合并同步/流式、删 `ready_executor`。

**Acceptance**
- `unit_ready` 仍严格按 index 升序（C0 主轴断言不变）。
- **`done` 发出前必须 drain 完所有 `ready_futures` 和 `completion_queue`**（否则 `done` 会先于最后一个 `unit_ready`，踩中主轴）。
- 代码里不再有手动 index 重排字典（**N2 可晋升**）；`completion_queue` 未泄漏到 `stream_voice_events` 外部。
- `python -m pytest tests -q` 全绿；`python webui_qt.py` 跑通一轮。

**Verify**：`python -m pytest tests -q` + 人工　**Rollback**：sequencer.py + 一处接线，单 commit revert。　**main 仍可运行**：是。

---

### 阶段 C1.5 — `run_turn` facade（零行为变化，建主入口外壳）

**Goal**：把 `run_turn` 作为主入口**先立起来**，但此刻只是现有流式编排的透传壳。让 C2 能只专注折叠。

**In scope**
- 新建 `spica/runtime/turn.py`：`run_turn(req, deps) -> Iterator[RuntimeEvent]`。**实现暂时只包一层现有
  `stream_voice_events(...)`，但必须在边界把 legacy 事件转成 `RuntimeEvent`**：
  `for legacy in stream_voice_events(...): yield event_from_legacy(legacy)`。
- `ChatEngine` 的**流式路径**改走 `run_turn`；若 UI 仍要 legacy dict，**只在 ChatEngine 兼容层**用 `event.to_legacy_dict()` 转回。
- **不动**：同步路径、`AgentState`、`ExecStrategy`、`Sequencer`、任何 stage。

**Out of scope**：合并同步/流式（C2）；类型化（C3）。

**Acceptance**
- **`run_turn` 的返回类型是 `RuntimeEvent`；legacy dict 只允许出现在 `ChatEngine` 兼容层。**
- 流式事件主轴与遥测**完全等价**于改前（此处「零行为变化」正是靠 C0 的 `to_legacy_dict(event_from_legacy(x)) == x` 等价测试兜底）。
- `run_turn` 成为流式唯一入口（同步仍走老路，C2 再并）。
- `python -m pytest tests -q` 全绿；`python webui_qt.py` 跑通一轮。

**Verify**：`python -m pytest tests -q` + 人工　**Rollback**：turn.py + 一处转发，单 commit revert。　**main 仍可运行**：是。

---

### 阶段 C2 — `ExecStrategy` + fold（同步切到事件折叠，切路径不删旧文件）

**Goal**：并发变成可注入策略；同步路径 = 「收集 `run_turn` 事件后折叠」。**只切主路径，不删 `agent/runtime.py`。**

**In scope**
- 新建 `spica/runtime/exec_strategy.py`：`ExecStrategy` 协议 + `Threaded`（包现有线程池，**接管 `ready_executor`**）+
  `Inline`（立即执行、返回已完成 Future）。签名见附录 C。**`Inline` 必须把异常透传为 `Future.set_exception`，不得吞掉。**
- `orchestrator` 的 visual/tts/finalize 提交统一走注入的 `ExecStrategy`，不再各自 `new ThreadPoolExecutor`。
- 新建 `spica/runtime/fold.py`：`fold_events(events) -> response_payload`。**失败单元的处理方式必须与流式 error 路径一致**
  （同一种 `error` 语义，保证「同步 = 折叠流式」在错误路径上也成立）。
- `ChatEngine.run_voice` 改为 `fold_events(list(run_turn(..., exec=Inline())))`。
- **`agent/runtime.py` 保留为 compatibility wrapper**（ChatEngine 不再调用它即可）；**到 C4 删除**。

**Out of scope**：拆 `AgentState`、类型化、删 `agent/runtime.py`、观测注入。

**Acceptance**
- 同步 payload 的**关键字段白名单一致**：`answer` / `emotion`（对象与 label）/ `audio_path` / `visual` 选择 /
  `error` / 基础 `timing` / `conversation_id`。其余历史字段（`tools`、`tts_chunks`、`tts_chunk_audio`、`tts_params`、
  完整 visual 语义、error code 粒度）**允许有兼容差异，但必须列入「差异白名单」并在测试里写明**。
- **不追求**与旧 `build_response_node` 逐字段完全一致——事件流相对旧 payload 是有损的；完整 parity 留到 `done` 的
  TurnSummary 在 **C3c/C7** 补全后再追（届时 fold 能从更完整的事件重建）。
- 同步与流式在**错误路径**上行为一致（空输入/中途异常两条用例两路对比）。
- `ChatEngine.run_voice` 不再调用 `agent.runtime.run_voice_pipeline`。
- 业务 stage 内不再 `new ThreadPoolExecutor`（**N4-concurrency 可晋升**）。
- `python -m pytest tests -q` 全绿。

**Verify**：`python -m pytest tests -q` + 人工　**Rollback**：分 commit（exec / fold / 切 run_voice）可逐个 revert。　**main 仍可运行**：是。

---

### 阶段 C3a — 引入 `TurnRequest` / `TurnDeps` / `ToolSet`（内部仍适配老 AgentServices）

**Goal**：把类型化入口立起来，但**不拆 `AgentState`**。Host 装配出 typed deps，内部临时适配旧 `AgentServices`。

**In scope**
- 新建 `spica/runtime/context.py`：`TurnRequest`（签名见附录 B）。
- 新建 `spica/runtime/deps.py`：`TurnDeps`（`config: AppConfig` + 已解析 `llm/tts/visual/memory` port + `tools: ToolSet` +
  observer/jobs/exec 占位）。签名见附录 D。
- 新建 `spica/runtime/tools.py`：`ToolSet` 协议（签名见附录 G）+ **`LegacyFunctionToolSet`**：
  **最小**包装现有 `TOOL_SCHEMAS` / `run_local_tool` / 意图门，**不加任何新功能**。
- `agent_assembly` 构造 `TurnDeps`（解析 port、装 `LegacyFunctionToolSet`）；旧 `AgentServices` 暂作内部 compat 留存。
- `ChatEngine` 外部签名不变；内部开始构造 `TurnRequest`，并把 `TurnDeps` 往下传（可暂与旧 services 并存）。

**Out of scope**：杀 `services.config.get`（C3b）；拆 `AgentState`（C3c）；registry-backed 工具（C7）。

**Acceptance**
- `TurnRequest`/`TurnDeps`/`ToolSet` 存在并被装配；运行时已通过 `deps.tools` 调工具（实现仍是 Legacy）。
- **`TurnDeps` 的 `observer`/`jobs`/`exec` 不允许为 `None`**：C5/C6 前分别用 `NoopTurnObserver` / `InlineJobRunner` /
  （C2 传入的）`Threaded` 兜底，让 C5/C6 是干净的「换实现」而不是「清 None 判断」。
- 所有 golden 不变。`python -m pytest tests -q` 全绿。

**Verify**：`python -m pytest tests -q`　**Rollback**：新增为主 + 装配改动，分 commit revert。　**main 仍可运行**：是。

---

### 阶段 C3b — 消灭 `services.config.get(...)`（一个 stage 一个 stage）

**Goal**：把所有 `services.config.get("x")` 换成 `deps.config.<section>.<field>`，并去掉 `client+adapter` 双字段兜底。

**In scope**
- 逐 stage / 逐调用点替换为 `deps.config.llm.model` 这类强类型读取。
- 端口在装配期已解析好，删除 `x or Adapter(client)` 这种二选一兜底。
- 一个模块一个 commit。

**Out of scope**：拆 `AgentState`（C3c）。

**Acceptance**
- 运行时核心中 `services.config: dict` 与双字段兜底**消失**（**N3-config 可晋升**）。
- 所有 golden 不变。`python -m pytest tests -q` 全绿。

**Verify**：`python -m pytest tests -q`　**Rollback**：逐模块 commit，任一步红立即 revert。　**main 仍可运行**：是。

---

### 阶段 C3c — 拆 `AgentState` 黑板

**Goal**：用类型化 sub-context 取代 god-object；stage N 的读者不可能依赖 stage N+2 才写的字段。

**In scope**
- `TurnContext` 持 `recent / prompt / answer / screen_observation / error` 等**类型化子对象**（各阶段前为 `None`）。
  签名见附录 B。
- 每个 stage 改成 `(ctx, deps) -> ctx`：读写自己负责的子对象。
- 删除 `AgentState`（或仅留过渡 alias，C4 清）。

**Out of scope**：搬文件到 `spica/conversation`（C4）；观测注入（C5）。

**Acceptance**
- `AgentState` 不再被读写。所有 golden 不变。`python -m pytest tests -q` 全绿。

**Verify**：`python -m pytest tests -q`　**Rollback**：一个 stage 一个 commit。　**main 仍可运行**：是。

---

### 阶段 C4 — 重新分层：`agent/` → `spica/conversation` + `spica/runtime/stages`（机械搬迁）

**Goal**：平台核心自给自足。纯 domain 进 `spica/conversation/`，应用编排进 `spica/runtime/`，**`agent/` 消失**。
标 `mechanical`：只搬代码、改 import，零行为变化。

**In scope（一个模块一个 commit）**
- 搬进 `spica/conversation/`（纯函数）：`prompt_builder`、`reply_parser`、`text_normalizer`、`time_context`、
  `character_loader`、`character_compat`。
- 把 `agent/nodes.py` 逻辑拆成 `spica/runtime/stages/`（每个调 conversation + 调 port，**不 emit**）。
- **`agent/` 的退场拆成 4 个 commit，不许一次性删**：
  C4-1 机械搬迁 + `agent/` 改 re-export 壳（含删 C2 留下的 `agent/runtime.py` 壳）→
  C4-2 更新所有内部 import 指向新位置 →
  C4-3 全仓搜索确认无任何 `import agent`（含测试/脚本）→
  C4-4 删除 `agent/` 壳。
- `tests/test_layering.py` 增加断言：**`spica` 不许 import `agent`**。

**Out of scope**：改任何运行时行为；动 screen tool 轨道。

**Acceptance**
- 依赖图：`runtime → conversation + ports`；`adapters → ports`；`host 接线`，无循环。
- `agent/`（含 `runtime.py` 壳）已删；layering 守卫绿（**N1-final、N3-layer 可晋升**）。
- 所有 golden 不变。`python -m pytest tests -q` 全绿；`python webui_qt.py` 跑通一轮。

**Verify**：`python -m pytest tests -q` + 人工　**Rollback**：每模块一个 commit。　**main 仍可运行**：是。

---

### 阶段 C5 — `TurnObserver`（把可观测性从逻辑里拔出来）

**Goal**：计时/日志变横切关注点。stage 只发 span/mark，golden 不被 timing 噪声污染。

**In scope**
- 新建 `spica/runtime/observer.py`：`TurnObserver` 协议 + 默认实现（包现有 `log_timing`/`set_timing_once` 语义）。签名见附录 E。
- 把散落的 `set_timing_once/log_timing/state.timing[...]` 收敛为 `deps.observer.span/mark`；timing 仍进 `done.timing`，来源统一。

**Acceptance**
- `spica/runtime` 的 turn/stage 编排代码内不再直接调 `log_timing`（**N4-observe 可晋升**）。
  **N4-observe 只约束 turn/stage 层**；adapter（LLM/TTS/screen 等）内部的低层诊断日志保留，只要 turn 级计时走 `TurnObserver` 即可。
- 主轴 golden 不变；`done.timing` 字段集合与改前一致（或更干净且有测试说明）。`python -m pytest tests -q` 全绿。

**Verify**：`python -m pytest tests -q`　**Rollback**：observer.py + 替换点，单 commit revert。　**main 仍可运行**：是。

---

### 阶段 C6 — `JobRunner`（**仅长期记忆**后台化）

**Goal**：**长期记忆 commit 不堵 hot path；recent memory 仍同步提交。**（recent context 下一轮就要用，绝不能后台化。）

**In scope**
- 新建 `spica/runtime/jobs.py`：`JobRunner` 协议 + `ThreadJobRunner`（后台）+ `InlineJobRunner`（测试用，同步执行）。签名见附录 F。
- `save_stream_memory`：**保留 recent_memory 写入为同步**；仅把 `memory_adapter.commit_turn(...)`（长期）改为 `deps.jobs.submit(...)`。

**Acceptance**
- `done` **不等待**长期 `commit_turn`；长期 commit 失败不影响事件流（错误进 metadata/日志）。
- **recent_memory append 必须在 `done` 前完成**（加用例断言：done 时上一轮已在 recent context 中）。
- 用 `InlineJobRunner` 的 golden 仍能断言长期 commit 调用与 `MemoryScope` 正确（**N4-memory 可晋升**）。
- `python -m pytest tests -q` 全绿。

**Verify**：`python -m pytest tests -q`　**Rollback**：jobs.py + 一处接线，单 commit revert。　**main 仍可运行**：是。

---

### 阶段 C7 — registry-backed `ToolSet` + `inspect_screen` 迁 `ToolPort`（本次新增需求）

**Goal**：把 C3a 的 `LegacyFunctionToolSet` **换实现**为 registry-backed；让 `inspect_screen` 成为
`CapabilityRegistry` 里**第一个真正的 tool**，点亮 Phase 5 未接的 `ToolPort` 缝。**手动截图保持 attachment。**

**设计裁决（务必照此）**
- **自动看屏幕 = Tool**：`inspect_screen` 迁 `ToolPort`；运行时经 registry 解析（替换 Legacy 实现，不发明新边界）。
- **手动截图 = Attachment**：保持 `analyze_screen_attachment` 预分析 stage，**不**让模型决策是否分析（N0b）。
- **两者共享同一个 screen-analysis adapter**：把本地 Moondream+OCR 引擎正式收成 `ScreenAnalysisPort`；
  tool handler 与 attachment stage 都依赖它（引擎已共享 `analyze_screen_image_local`，本阶段只是正式化，行为不变）。

**In scope**
- 新建 `spica/ports/screen.py`：`ScreenAnalysisPort`。
- 新建 `spica/adapters/screen/local_moondream.py`（或复用现有 `analyze_screen_image_local`）作为其实现。
- 新建 `spica/adapters/tools/screen.py`：`InspectScreenTool`（实现 `ToolPort`：`schema()` 返回 `INSPECT_SCREEN_SCHEMA`，
  `run()` 走 capture+analyze；**保留 `is_screen_intent_explicit` 门 + 本地隐私**，N0/N5）。
- `AppHost._register_builtin_adapters`：`registry.register_tool(tool.schema(), tool.run)`（screen）。
- 用 **registry-backed `ToolSet`** 替换 `LegacyFunctionToolSet`（`deps.tools` 行为不变，实现换底）。
- `analyze_screen_attachment` stage 改依赖 `ScreenAnalysisPort`（同一 adapter），其余行为不变（仍在附图轮关其他工具）。
- 新增测试：`inspect_screen` 经 registry 解析并执行；意图门仍生效；手动截图仍走预分析且该轮不调其他工具。

**Out of scope**：把唱歌做成 tool；vision/多模态 LLM 通路；插件外部贡献 tool 的完整 UI。

**Acceptance**
- `inspect_screen` 由 `CapabilityRegistry` 注册并被运行时按 registry 解析（不再依赖静态 `TOOL_SCHEMAS`，**N5 可晋升**）。
- 意图门 + 本地隐私保持（N0）；手动截图仍是 attachment（N0b）。
- C0 工具轮 / 手动截图两条 golden 不变。`python -m pytest tests -q` 全绿；`python webui_qt.py` 两条 screen 路径各跑一次。

**Verify**：`python -m pytest tests -q` + 人工　**Rollback**：port/adapter/tool 新增 + 装配与门控替换，分 commit revert。　**main 仍可运行**：是。

**为什么手动截图不做成 tool（写给执行者）**
> tool 是「让模型自己去取它没有的信息」；attachment 是「用户已经把信息递过来了」。用户框选+点按钮 = 明确「看这张」，
> 没有「要不要看」留给模型。做成 tool 会：① 撞上 LLM 纯文本通路（图片字节进不去）；② 凭空加延迟+不确定性+
> 「模型不看用户附图」的失败模式；③ 丢掉「附图轮关其他工具」的合理判断。正确的统一是统一**分析引擎**，不是统一**入口语义**。

---

### 阶段 C8 — 收尾与验收（核心至此才算稳）

**Goal**：核对全部约束，确认 turn 已是承重抽象，往后加功能基本不碰主轴。

**In scope**
- 全量跑 §1 已晋升的全部守卫测试 + 原 7 条。
- 一页验收清单：
  - turn 是唯一 emit 路径；排序只走 `Sequencer`。
  - 同步 = 流式 fold（含错误路径一致），无重复 node 链；`agent/` 已删。
  - `AppConfig` 一路贯穿，运行时核心无 `dict` config、无双字段。
  - 并发/观测/长期记忆均注入；recent memory 仍同步。
  - `inspect_screen` 在 registry，意图门+隐私在；手动截图是 attachment。
  - UI 播放语义未变（N7）。
- 更新 `README.md` 架构段 + `docs/REFACTOR_PLAN.md` 标注 Phase 6 核心硬化完成；`CLAUDE.md` 约束全部翻「已生效」。

**Out of scope**：唱歌平台化、T2I、插件 UI（都已是纯加法，另行排期）。

**Acceptance**：清单全绿；`python -m pytest tests -q` 全绿；`python webui_qt.py` 启动+对话+两条 screen 路径正常。
**Verify**：`python -m pytest tests -q` + 人工　**Rollback**：纯文档+验收。　**main 仍可运行**：是。

---

## §4 附录：接口签名 + 目标目录

> 目标形状示意，类型/方法名按实际填充。

### 目标目录（C4 之后）

```text
spica/
├── conversation/          # 纯 domain（无 I/O / 无线程 / 无 adapter）
│   ├── prompt_builder.py / reply_parser.py / text_normalizer.py / time_context.py / character.py
├── runtime/
│   ├── turn.py             # run_turn / stream_answer（C4 后唯一 emit 路径）
│   ├── context.py          # TurnRequest / TurnContext（类型化子对象）
│   ├── deps.py             # TurnDeps（已解析 port + AppConfig + ToolSet + observer/jobs/exec）
│   ├── tools.py            # ToolSet 协议 + LegacyFunctionToolSet(C3a) → registry-backed(C7)
│   ├── sequencer.py        # 排序原语（单消费者）
│   ├── exec_strategy.py    # Threaded / Inline
│   ├── observer.py / jobs.py / fold.py
│   ├── stages/             # 各 stage：(ctx, deps) -> ctx，不 emit
│   └── play_unit_splitter.py / tts_job.py / visual_job.py / memory_commit.py
├── ports/                  # + screen.py（ScreenAnalysisPort）
├── adapters/
│   ├── screen/local_moondream.py
│   └── tools/screen.py     # InspectScreenTool（实现 ToolPort）
└── host/ ...
# agent/ 已删除
```

### A. `Sequencer`（C1 · 排序原语，单消费者，无锁——因为只一个线程调用）

```python
# spica/runtime/sequencer.py
from __future__ import annotations
from typing import Generic, TypeVar
T = TypeVar("T")

class Sequencer(Generic[T]):
    """按 index 单调放行；完成顺序任意，放行顺序严格 0,1,2,...。
    契约：仅由单一 consumer（producer 主循环）调用，本类不做线程同步。
    并发由 ExecStrategy 负责：worker 把 (index, payload) 投递到队列，主循环取出后调 complete()。"""
    def __init__(self, start: int = 0) -> None:
        self._next = start
        self._done: dict[int, T] = {}
    def complete(self, index: int, value: T) -> list[T]:
        if index in self._done or index < self._next:
            raise ValueError(f"duplicate/late index {index}")
        self._done[index] = value
        out: list[T] = []
        while self._next in self._done:
            out.append(self._done.pop(self._next)); self._next += 1
        return out
    @property
    def pending(self) -> int: return len(self._done)
```

### B. `TurnRequest` / `TurnContext`（C3a / C3c）

```python
# spica/runtime/context.py
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any

@dataclass(frozen=True)
class TurnRequest:
    user_input: str
    conversation_id: str = "default"
    emotion_override: str | None = None
    interaction_mode: str = "chat"
    include_user_time_context: bool = True
    screen_attachment: dict[str, Any] | None = None
    tts_param_overrides: dict[str, Any] | None = None
    visual_overrides: dict[str, Any] = field(default_factory=dict)

@dataclass
class RetrievedContext: ...
@dataclass
class PromptBundle: ...
@dataclass
class StreamedAnswer: ...
@dataclass(frozen=True)
class TurnError:
    code: str; message: str

@dataclass
class TurnContext:                       # 各子对象在其 stage 之前为 None，依赖因此显式
    request: TurnRequest
    recent: RetrievedContext | None = None
    prompt: PromptBundle | None = None
    answer: StreamedAnswer | None = None
    screen_observation: dict[str, Any] | None = None
    error: TurnError | None = None
```

### C. `ExecStrategy`（C2 · 三 lane 写死进协议 + Inline 透传异常）

```python
# spica/runtime/exec_strategy.py
from __future__ import annotations
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Callable, Protocol, TypeVar
T = TypeVar("T")

class ExecStrategy(Protocol):
    # 三个 lane 写死进协议：TTS 串行是产品行为，不是实现细节
    def submit_visual(self, fn: Callable[[], T]) -> "Future[T]": ...
    def submit_tts(self, fn: Callable[[], T]) -> "Future[T]": ...      # 串行 lane
    def submit_finalize(self, fn: Callable[[], T]) -> "Future[T]": ...
    def shutdown(self) -> None: ...

def _done(fn: Callable[[], T]) -> "Future[T]":
    f: Future[T] = Future()
    try: f.set_result(fn())
    except BaseException as e: f.set_exception(e)   # 与 Threaded 一致，不得吞掉
    return f

class Inline:                            # 测试用：三个 lane 都同步执行
    def submit_visual(self, fn): return _done(fn)
    def submit_tts(self, fn): return _done(fn)
    def submit_finalize(self, fn): return _done(fn)
    def shutdown(self) -> None: ...

class Threaded:                          # 接管原 visual/tts/ready 三个池
    def __init__(self, visual_workers: int = 2) -> None:
        self._visual = ThreadPoolExecutor(max_workers=max(1, visual_workers))
        self._tts = ThreadPoolExecutor(max_workers=1)        # TTS 串行
        self._finalize = ThreadPoolExecutor(max_workers=4)
    def submit_visual(self, fn): return self._visual.submit(fn)
    def submit_tts(self, fn): return self._tts.submit(fn)
    def submit_finalize(self, fn): return self._finalize.submit(fn)
    def shutdown(self) -> None: ...
```

### D. `TurnDeps`（C3a）

```python
# spica/runtime/deps.py
from __future__ import annotations
from dataclasses import dataclass
from spica.config.schema import AppConfig
from spica.ports.llm import LLMPort
from spica.ports.tts import TTSPort
from spica.ports.visual import VisualPort
from spica.ports.memory import MemoryPort

@dataclass(frozen=True)
class TurnDeps:
    config: AppConfig
    llm: LLMPort; tts: TTSPort; visual: VisualPort; memory: MemoryPort
    tools: "ToolSet"               # C3a=LegacyFunctionToolSet → C7=registry-backed
    observer: "TurnObserver"       # C5 前可为 no-op
    jobs: "JobRunner"              # C6 前可为 InlineJobRunner
    exec: "ExecStrategy"
```

### E. `TurnObserver`（C5）

```python
# spica/runtime/observer.py
from __future__ import annotations
from contextlib import AbstractContextManager
from typing import Any, Protocol
class TurnObserver(Protocol):
    def span(self, name: str, **fields: Any) -> AbstractContextManager[None]: ...
    def mark(self, name: str, value: float | None = None, **fields: Any) -> None: ...
    def snapshot(self) -> dict[str, Any]: ...     # 供 done.timing
```

### F. `JobRunner`（C6）

```python
# spica/runtime/jobs.py
from __future__ import annotations
from typing import Callable, Protocol
class JobRunner(Protocol):
    def submit(self, fn: Callable[[], None]) -> None: ...      # fire-and-forget（仅长期记忆）
    def drain(self, timeout: float | None = None) -> None: ...
class InlineJobRunner:
    def submit(self, fn): fn()
    def drain(self, timeout=None): ...
```

### G. `ToolSet`（C3a 定义 + Legacy 适配；C7 换实现）

```python
# spica/runtime/tools.py
from __future__ import annotations
from typing import Protocol
class ToolSet(Protocol):
    def schemas_for_user_text(self, user_text: str) -> list[dict]: ...   # 含意图门
    def run(self, name: str, arguments: str) -> str: ...

class LegacyFunctionToolSet:        # C3a：最小包装，不加新功能
    """包住现有 TOOL_SCHEMAS / run_local_tool / is_screen_intent_explicit。"""
    ...
# C7：RegistryToolSet —— 同协议，schemas/handlers 取自 CapabilityRegistry；inspect_screen 走 ToolPort
```

### H. screen tool 迁 `ToolPort`（C7）

```python
# spica/ports/screen.py
from __future__ import annotations
from typing import Any, Protocol, runtime_checkable
@runtime_checkable
class ScreenAnalysisPort(Protocol):
    def analyze(self, *, image: Any = None, png_bytes: bytes | None = None,
                target: str, question: str) -> dict[str, Any]: ...     # screen_observation.v1

# spica/adapters/tools/screen.py
from spica.ports.tool import ToolPort
from agent_tools.function_tools.screen.tool import INSPECT_SCREEN_SCHEMA  # 迁移期沿用
class InspectScreenTool:            # 实现 ToolPort
    name = "inspect_screen"
    def __init__(self, screen: "ScreenAnalysisPort") -> None: self._screen = screen
    def schema(self) -> dict: return INSPECT_SCREEN_SCHEMA
    def run(self, *, target: str = "full_screen", question: str = "") -> dict:
        # 1) 保留 is_screen_intent_explicit 意图门（N0）
        # 2) capture_full_screen() -> self._screen.analyze(...)（本地、绝不上传，N0）
        ...
```

---

## §5 一页速查

| 阶段 | 一句话 | 可晋升的约束 | 风险 |
| --- | --- | --- | --- |
| C0 | 契约焊死：主轴精确/遥测宽松 + 复用断言助手 | — | 极低 |
| C1 | `Sequencer` 排序原语（单消费者），只换 reorder | N2 | 低 |
| C1.5 | `run_turn` facade，零行为变化透传壳 | — | 极低 |
| C2 | `ExecStrategy`+fold，切主路径（payload 关键字段白名单一致），不删旧文件 | N4-concurrency | 中 |
| C3a | `TurnRequest`/`TurnDeps`/`ToolSet`(Legacy) | — | 中 |
| C3b | 杀 `services.config.get`，去双字段 | N3-config | 中 |
| C3c | 拆 `AgentState` 黑板 | — | **较高** |
| C4 | 机械搬迁 `agent/`→`spica/`，删兼容壳，加 layering 守卫 | N1-final, N3-layer | 中 |
| C5 | `TurnObserver`，计时拔出逻辑 | N4-observe | 低 |
| C6 | `JobRunner`，**仅长期记忆**后台化，recent 仍同步 | N4-memory | 低 |
| C7 | registry-backed `ToolSet` + `inspect_screen` 迁 `ToolPort`；手动截图保持 attachment | N5 | 中 |
| C8 | 收尾验收 | （全部翻「已生效」） | 低 |

**北极星**：turn = 确定性类型化事件流；领域纯；编排是线性 pipeline + 一个有序 fan-out；
并发/可观测/长期记忆都是注入策略；recent memory 同步；`spica/` 自给自足；类型一路贯穿；UI 播放不在本轮动。
往后加唱歌/T2I/插件/第二角色 = 改配置 + 新增文件，几乎不碰主轴。

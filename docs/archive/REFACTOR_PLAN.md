# Spica 平台化重构计划书（修订版）

> 配套文件：仓库根目录的 `CLAUDE.md`（不可破坏的约束 + 命令 + 完成定义）。
> 本计划书是分阶段路线图，专门按 **Claude Code** 的工作方式组织：
> **每个阶段 = 一个能独立交给 Claude Code 跑完的 session = 一个分支 = 跑完后 main 仍可运行、测试仍绿。**
>
> 用法：开工某阶段时，让 Claude Code 先读 `CLAUDE.md`，再读本文件对应阶段那一节，然后照 §2 的 session 协议执行。
>
> **本修订版的原则**：Phase 1 / 2 / 6 在初版里「过载」，会诱导 Claude Code 越改越大。本版把
> Phase 1 缩到「只搬组装根」、Phase 2 缩到「只立边界守卫」、Phase 6 拆成 6A–6E。每个阶段都小到一个 session 能稳稳吃下。
>
> **状态 — 核心 turn 硬化已完成**：`docs/REFACTOR_PLAN_CORE.md` 的 C0–C8 已全部落地（绞杀式重构，main 全程可运行、测试全绿）。
> turn 已是承重抽象——类型化 `TurnContext`、唯一 emit 的 `run_turn`、同步=流式 fold，注入的并发/观测/长期记忆/工具，旧 `agent/` 已删。
> `CLAUDE.md` 的核心 turn 不变量全部「已生效」且有 AST/行为守卫测试。后续加功能（唱歌平台化、T2I、插件 UI）基本不再碰主轴。

---

## §0 前提：硬地基 vs 存疑参考

**计划书必须立在硬地基上，不要被竞品分析污染。**

### 硬地基（来自本仓库代码，已核实，计划书一切以此为准）

- `ui/qt_overlay.py` 的 `OverlayWindow._init_backend()` 里**直接 new** 出 `VisualDiffService`、加载 TTS 配置、
  构建 TTS adapter、创建 `SimpleAgent` 和 `ChatStreamController`。**组装根（composition root）当前在 UI 里。**
- `agent/simple_agent.py` 的 `SimpleAgent` **直接读 env**（`os.getenv` 一大串）、创建 `OpenAI` client、
  `RecentMemory`、`SQLiteMemoryStore`、tool functions、角色设定，并拼出 `AgentServices`。它同时干**组装**和**驱动**两份活。
- `agent/streaming_pipeline.py` 用**手搓线程 + `queue` + `_SENTINEL` + 多个 `ThreadPoolExecutor`**，
  在**同一个生产函数**里处理 prompt、memory、screen tool、LLM delta、断句、TTS、visual、ready 排序、done/error，
  并**吐裸 `{"event": ..., "data": ...}`**。日语数学读法的 `re.sub` 也**泄漏**在这里（本应在 `agent/text_normalizer.py`）。
- 同步链路（`agent/nodes.py` + `runtime.py`）和流式链路在**重复同一套阶段**（校验/记忆/prompt/解析/写记忆/...）。
- 已有正确雏形：`agent_tools/tts/base.py`（`TTSAdapter` 协议）、`schemas.py`、`manager.py`——adapter 方向是对的，但只做了 TTS。
- 角色硬编码：`replace_mugi_references`、`SPICA_USER_NAME`、写死的「麦」、`spica_data/Spica_skill/`——**引擎只认识一个角色**。
- 已有像样测试（memory / prompt / reply / streaming / tts adapter / visual / song）。结构不烂，**风险是「写死成单一产品」而非「乱」**。

### 存疑参考（Shinsekai，仅作方向参考）

**本计划不依赖 Shinsekai 的任何具体实现细节。** 无论它当前 main 分支、release 包或本地拉取版本采用
PySide、React 设置中心、插件 host、workflow/DAG 还是其他实现，Spica 本轮重构只借鉴**一个工程方向**：
**宿主化架构、控制反转、ports/adapters、框架无关核心、插件边界**。这些是软件工程通则，Shinsekai 怎么实现都不影响它们成立。

> **不要**在代码、提交信息或本计划书里把 Shinsekai 的具体实现写成本项目的事实依据。它不是地基。

---

## §1 承重墙与不可破坏约束

完整 7 条见 `CLAUDE.md`。这里只强调主轴：

> **这次重构的全部内容，一句话：把组装根从 UI 里搬出去，变成 `AppHost.initialize()`。其余都是这句话的展开。**

最关键的一条平台级约束：**Host 及其以下（runtime/adapters/config/memory/core）绝不 import PySide。**
这条守住，将来加 Web/React 前端只是再写一个订阅 Host 事件的薄适配层，核心一行不动。

Host 对外是**两个窄面，不是一个大对象**（否则只是把神类从 `OverlayWindow` 搬到 Host）：

- **ConversationSurface**（给聊天窗）：开一轮、打断/停止、切角色、订阅事件流、查 ready 状态。**Phase 1 就落地。**
- **ManagementSurface**（给设置中心）：列可用 adapter、列已装角色/插件、读写配置、装/卸插件。
  **Phase 8 才正式实现；在那之前只放 `NotImplementedError` 占位**，免得过早设计设置中心接口。

---

## §2 怎么用 Claude Code 跑每个阶段

### Session 协议（每个阶段都照这个来）

1. **基线**：开工先跑 `python -m pytest tests`，确认当前全绿。记下基线。
2. **读约束**：读 `CLAUDE.md` 全文 + 本文件当前阶段那一节。
3. **只在 In scope 内动手**；碰到需要改 Out of scope 的文件，**停下来报告**，不要自作主张扩大范围。
4. **绿着收尾**：结束前 `python -m pytest tests` 全绿；机械搬迁步骤额外确认行为没变。
5. **一步一 commit**：commit message 写明 Phase 几的哪一步。
6. **分支**：每个 Phase（含子阶段如 6A）一个分支，绿了再合 main。

### 交给 Claude Code 的 prompt 模板（每个阶段开头粘这个）

```
读 CLAUDE.md 和 docs/REFACTOR_PLAN.md 的 "Phase <N>" 一节。

先跑 `python -m pytest tests` 确认基线全绿（注意:这个仓库只能用 python -m pytest tests，
绝不在根目录跑裸 pytest，会扫到 vendored GPT-SoVITS 崩掉），把结果告诉我。

然后不要动手，先用自己的话把该阶段要做什么、会新建/改哪些文件列给我看，并停下来等我确认。

我确认后，只在该阶段 "In scope" 列出的文件范围内执行。严格遵守 CLAUDE.md 里的 7 条 INVARIANTS，尤其：
- 标注 mechanical 的步骤零行为变化
- 业务代码禁止 os.getenv（除 config/manager.py、config/secrets.py 和当前 allowlist）
- Host 以下不许 import PySide
- 不为单一实现造抽象

完成后逐条核对该阶段的 "Acceptance"，跑 "Verify command"，确认 main 能启动、测试全绿。
任何需要越出 In scope 的改动，先停下来问我，不要擅自扩大。最后给我一个简短 commit message。
```

`<N>` 处填阶段号，例如 `1`、`2`、`6A`、`6C`。

---

## §3 分阶段路线图

依赖顺序严格，不要跳。每个阶段标了 `main 是否仍可运行`——答案永远是 **是**。

> 阶段一览：0 → 1 → 2 → 3 → 4 → 5 → **6A → 6B → 6C → 6D → 6E** → 7 → 8 →（9 可选）

---

### Phase 0 — 安全网与地基（先做，不动架构）

**Goal**：在动任何核心代码前，先把行为焊死、把规则就位。`streaming_pipeline.py` 是全仓最脆代码，没测试网就重构 = 盲改。

**In scope**
- 新建 `CLAUDE.md`（仓库根目录，已提供）。
- 新建 `.github/workflows/ci.yml`：在 push / PR 上跑 `python -m pytest tests`。
- 新建 characterization（golden）测试：录下 `streaming_pipeline` 当前的**事件序列**（status → unit_text_ready/unit_ready 顺序 → done）作为黄金基准；录下同步链路对固定输入的输出。用 fake LLM/TTS（仓库已有 `FakeLLMClient` 等可复用）。
- **这批 golden 测试要设计成「格式无关」**：断言的是**事件的语义内容和顺序**（哪个 index、什么文本、什么 emotion、什么先后），不是断言「它是个 dict」。这样 Phase 6A 把裸 dict 换成 `RuntimeEvent` 时，同一批测试能直接复用、两边等价。

**Out of scope**：任何架构改动、任何文件搬迁。

**Acceptance**
- CI 在 push 时自动跑测试并能看到结果。
- 新 golden 测试覆盖：流式事件顺序、断句单元数、done/error 路径、同步链路输出快照。
- `python -m pytest tests` 全绿。

**Verify command**：`python -m pytest tests -q`

**Rollback**：纯新增文件，删掉即可。

**main 仍可运行**：是（未碰运行时代码）。

---

### Phase 1 — 控制反转（承重墙，近乎零风险）

**Goal**：把组装根从 UI 搬进 `AppHost.initialize()`。**这一步单独就交付了你最看重的那个反转。**

**In scope**
- 新建 `spica/__init__.py` 和 `spica/host/app_host.py`，实现 `AppHost`，含 `initialize()`。
- **把 `OverlayWindow._init_backend()` 的后端构造逻辑原样搬进 `AppHost.initialize()`**——
  **仍然 new 今天那个 `SimpleAgent` / TTS adapter / `VisualDiffService`，行为零变化**（mechanical）。
- `AppHost` 暴露 `conversation_surface`（先就是对今天 `SimpleAgent` 的薄包装/别名）和持有的 services。
- `AppHost` 加 `management_surface` 属性，但**只放 `raise NotImplementedError("ManagementSurface 在 Phase 8 实现")` 占位**。
- 改 `OverlayWindow`：`self.host = AppHost(); self.host.initialize(); self.engine = self.host.conversation_surface`。
  **UI 不再 new 任何服务。**

**明确不做（这是本修订版相对初版的关键收缩）**
- **不碰 `StartupWarmupWorker`**：warmup 线程、Qt signal、线程生命周期一律原地不动，保持现有行为。
- **不引入启动状态机**（`INITIALIZING/READY/ERROR`）。warmup 事件化与启动阶段留到 **Phase 6E**。
- 一旦发现需要动 warmup / Qt signal / 线程，就说明越界了，停下来——那不属于 Phase 1。

**Out of scope**：拆 `SimpleAgent` 内部、改 `streaming_pipeline`、上 config schema、做 ports、warmup 事件化。

**Acceptance**
- `grep` 确认 `OverlayWindow` 不再直接 import / new `VisualDiffService` / TTS adapter / `SimpleAgent`，只 import `AppHost`。
- 启动行为、对话行为、立绘、TTS、**warmup 行为**与重构前**完全一致**（Phase 0 的 golden 测试不变）。
- `python -c "from spica.host.app_host import AppHost; print(AppHost)"` 能成功（确认新包根 import 正常）。
- `python webui_qt.py` 正常启动；`python -m pytest tests` 全绿。

**Verify command**：`python -c "from spica.host.app_host import AppHost; print(AppHost)"` + `python -m pytest tests -q` + 人工 `python webui_qt.py` 启一次确认

**Rollback**：`AppHost` 是新文件，`OverlayWindow` 的改动是一处装配替换，git revert 单 commit 即可。

**main 仍可运行**：是。

---

### Phase 2 — Qt 隔离守卫 + 新包骨架（**只立边界，不搬旧文件**）

**Goal**：建立平台目录骨架并立起「核心不许 import Qt」的守卫测试。**本阶段不带来任何业务能力，所以也不应该产生任何业务 diff。**

**In scope**
- 建空包骨架（仅 `__init__.py`）：`spica/{core,config,ports,plugins,runtime,adapters,memory}/`（`spica/host/` Phase 1 已建）。
- 新建守卫测试 `tests/test_layering.py`：断言 `spica/**` 下**没有任何模块 import PySide/Qt/GUI 库**（用 AST 或 import 扫描）。

**明确不做（本修订版关键收缩）**
- **不搬** `agent/streaming_pipeline.py`、`nodes.py`、`runtime.py`、`memory/*`、`agent_tools/tts|visual` 进 `spica/`。
  一次大搬迁 = import 地狱 + 大量无业务价值 diff，且不带来平台能力。
- 现有文件**留在原地**。等 Phase 6C 拆 `streaming_pipeline` 时，**拆出来的新组件直接落进 `spica/runtime/`**——
  **边拆边归位，而不是先搬再拆**。adapter 同理，Phase 5 包装时直接放进 `spica/adapters/`。

**Out of scope**：改任何运行时逻辑、搬任何现有文件。

**Acceptance**
- 目录骨架就位（空包），import 图干净，无循环。
- `tests/test_layering.py` 绿（此刻 `spica/` 里基本只有 `app_host.py` 和空包，必然绿；若红说明 `AppHost` 漏了 Qt，修到不漏，**不许删测试或加豁免**）。
- `python webui_qt.py` 启动；`python -m pytest tests` 全绿。

**Verify command**：`python -m pytest tests -q`

**Rollback**：纯新增空包 + 一个测试文件，删掉即可。

**main 仍可运行**：是。

---

### Phase 3 — 类型化配置（带 allowlist，避免提前逼拆 SimpleAgent）

**Goal**：所有可调旋钮收进一份校验过的配置；Host 读配置对象。`os.getenv` 逐步收口，**但不在本阶段强制清零**。

**In scope**
- `spica/config/schema.py`：Pydantic 模型（`AppConfig` 含 `LLMConfig / TTSConfig / VisualConfig / MemoryConfig` 等）。
- `spica/config/manager.py`：`load / save / merge / validate / migrate`。
- `spica/config/secrets.py`：API Key 等机密只从 env 读，**唯一允许长期 `os.getenv` 的地方**。
- 配置文件收拢到 `data/config/*.yaml`，并写迁移：把现有 `tts_config.json` / `visual_config.json` 等映射过来。
- `AppHost` 改为从 `ConfigManager` 读配置对象。`SimpleAgent` **不再直接读大部分 env，改为接收 Host 传入的配置对象**。
- 新建测试 `tests/test_no_getenv.py`：grep/AST 断言业务代码（`spica/**`）无 `os.getenv`，**但带临时 allowlist**：
  - 允许 `spica/config/manager.py`、`spica/config/secrets.py`（永久）。
  - 允许 `agent/simple_agent.py`（**临时**，注释标明「Phase 6D 删除 SimpleAgent 时一并删掉此 allowlist 项」）。

**为什么要 allowlist**：Phase 3 时 ports/registry 还没有、`SimpleAgent` 还没溶解。若此刻强制零 `os.getenv`，
Claude Code 会被迫提前大改 `SimpleAgent` 和 `nodes`，破坏阶段边界。allowlist 让禁令**存在但不反噬**。

**Out of scope**：ports / adapter registry / 角色包 / 拆 SimpleAgent。

**Acceptance**
- 启动只构造一个 `AppConfig`，启动时校验。
- `tests/test_no_getenv.py` 绿（allowlist 内的除外）。
- 行为不变（同样的配置值得到同样的结果）；`python -m pytest tests` 全绿。

**Verify command**：`python -m pytest tests -q`

**Rollback**：config 层是新增；Host 读取方式改动可单独 revert。

**main 仍可运行**：是。

---

### Phase 4 — 引擎角色无关化（平台门槛，**必须早于角色包**）

**Goal**：把 `replace_mugi_references` 这类 Spica 专属逻辑换成通用模板。引擎先做到角色无关，Phase 7 加载任意角色包才有意义。

**In scope**
- 把 `replace_mugi_references`、写死的「麦」、`SPICA_USER_NAME` 逻辑，替换成通用模板变量替换：`{{char}}` / `{{user}}`
  （在 `prompt_builder` / `character_loader` 内完成）。
- Spica 的人物事迹映射改由「角色数据 + 模板值」驱动，而不是代码里的正则。

**Out of scope**：定义完整 `CharacterPackage` schema（那是 Phase 7）；这里先把**替换机制**做成通用的。

**Acceptance（措辞已精确化，避免误伤数据文件）**
- **`runtime / core / host / prompt 构建`等代码中没有写死的 Spica/麦 专属替换逻辑**；
  Spica 专属名称只允许存在于**角色数据、测试 fixture、迁移脚本或兼容数据**中。
- 用模板值喂入当前 Spica 设定，能**复现重构前的 Spica 行为**（prompt golden 测试不变）。
- `python -m pytest tests` 全绿。

**Verify command**：`python -m pytest tests -q`（重点看 prompt 相关测试）

**Rollback**：替换集中在 prompt/character 两处，可单独 revert。

**main 仍可运行**：是。

---

### Phase 5 — ports + CapabilityRegistry（「换引擎」此时才真正可行）

**Goal**：定义能力协议，建注册表，把现有实现包成 adapter，Host 按**配置里的名字**解析激活 adapter。

**In scope**
- `spica/ports/`：定义 `Protocol`——**`LLMPort`、`TTSPort`（把已有 `TTSAdapter` 折进来）、`VisualPort`、`MemoryPort`、`ToolPort`**。
  - **`ASRPort` 本阶段不建**：ASR 当前不是核心对话链路，等真要接第二个 ASR 引擎（如 Whisper）时再定。
  - `ToolPort` 保留：你已有 `agent_tools/function_tools/`（点歌意图路由在用），且插件第一阶段就要注册 tool，不是空想抽象。
  - **`MemoryPort` 必须按「生成式/外部记忆」的富接口定，不是照搬现在 SQLite 的 `retrieve/upsert`。** 理由见下方设计注记；
    接口形状为 `commit_turn(scope, user_text, assistant_text, meta)` + `retrieve(scope, query, limit) -> list[MemoryItem]` + `get_context_block(scope) -> str | None`，
    `scope` 携带 `(character_id, user_id, conversation_id)`。SQLite adapter 实现其子集，未来的生成式记忆 adapter 实现全集。
  - **`MemoryPort` 不是普通 KV/RAG adapter，要为长期演化预留能力。** 把能力分两类，避免为还没有的实现造满抽象：
    - **进 Phase 5 签名（现在就留位）**：角色命名空间（`scope.character_id`）、对话写入（`commit_turn`）、上下文检索（`retrieve` + `get_context_block`）。`MemoryItem` 预留可选 `importance` 字段。
    - **预留为可选扩展点（Phase 5 只留钩子、不实现具体逻辑，SQLite adapter 可 no-op）**：睡眠/空闲期整理、重要经历归档、过期处理、重要性评分、角色专属文件空间。
      实现方式：port 上留一个统一的可选维护钩子 `run_maintenance(scope, reason)`（做不做、做什么由后端决定），外加能力探测 `supports(capability) -> bool`（后端声明支持哪些可选能力，如 `"file_space"` / `"sleep_consolidation"` / `"archival"` / `"importance"`）。
    - **关键纪律**：Phase 5 只给最小接口 + 这些钩子的占位；**不要把上面这些能力实现出来**，也**不要把 `MemoryPort` 锁死成只能同步 `retrieve()` / `upsert()` 的形状**。
  - **测试 fake 可以算作第二实现**让 port 成立，但 fake 本身不构成「扩张抽象」的理由。
- `spica/plugins/registry.py`：`CapabilityRegistry`，含 `register_llm / register_tts / register_visual / register_memory / register_tool`（按 name 注册）。
- `spica/adapters/` 把现有实现包成 adapter（**直接放进 `spica/adapters/`，落位即归位**）：
  - `adapters/llm/openai_compatible.py`：**把 nodes/pipeline 里 DeepSeek vs OpenAI 的分支判断折进这里**，对外只暴露 `generate / stream`。
  - `adapters/tts/gptsovits.py`、`adapters/tts/dummy.py`
  - `adapters/visual/spica_diff.py`
  - `adapters/memory/sqlite.py`：**把现有 `memory/extractor.py` + `memory/control.py`（规则抽取、去重、危险记忆过滤）搬进这个 adapter 内部**——
    抽取策略从此是「记忆后端的实现细节」，不再散在 pipeline 里。`commit_turn` 内部跑你现有的规则抽取；`get_context_block` 可先返回 `None` 或简单近期摘要。
- `AppHost.initialize()`：注册内置 adapter → 按 `config.llm.provider`（如 `openai_compatible`）等**名字**从注册表解析出激活实例。

**Out of scope**：插件外部加载（Phase 8）；拆 `streaming_pipeline`（Phase 6C）；RuntimeEvent（Phase 6A）。

**设计注记：为什么 `MemoryPort` 必须按富接口定（这决定将来换记忆系统会不会大改）**

未来计划接入「生成式/外部记忆」系统（EverMemoryArchive 思路，同 Mem0 / EverOS / supermemory / OpenMemory 一类）。这类系统与现在的 SQLite 有三个本质差异，正是它们决定 port 形状：

1. **谁做抽取**：这类系统自己用 LLM 从原始对话里抽事实，不是由你这边抽好再塞。所以接口是「交整轮对话」(`commit_turn`)，而不是「塞结构化 record」(`upsert`)——抽取是后端的实现细节。
2. **`retrieve` 返回什么**：不只是 `list[dict]`，常带每条的 score/类型/时间，外加一个 profile / context block。故用 `MemoryItem` dataclass，并单列 `get_context_block`。
3. **同步还是异步**：这类系统带网络 + LLM 延迟。`retrieve` 不能假定瞬时；写入本就该走后台 job（见 Phase 6C 的 `MemoryCommitter`），不堵 hot path。

外加**作用域**：这类系统按 user/agent/character 分轨，故 `scope = (character_id, user_id, conversation_id)`，须与 Phase 7 的 CharacterPackage 身份键一致。

**回报**：只要 `MemoryPort` 现在按这个超集定，将来加 EverMemoryArchive = **写一个新 adapter 实现 `MemoryPort` + 配置翻一个名字，核心不动**。反之若现在偷懒用 `retrieve/upsert`，将来会被迫改 port、波及 ChatEngine 与 prompt 注入点。

**Acceptance**
- 把配置里 `llm.provider` 从 `openai_compatible` 改成另一个**已注册**的名字，能换引擎而**不改任何核心代码**。
- DeepSeek/OpenAI 分支逻辑已从 pipeline 节点移到 LLM adapter 内。
- `python -m pytest tests` 全绿（新增 adapter 契约测试）。

**Verify command**：`python -m pytest tests -q`

**Rollback**：ports/registry/adapters 主要是新增 + 装配改动，可分 commit revert。

**main 仍可运行**：是。

---

### Phase 6 — 溶解 SimpleAgent + 拆解 streaming_pipeline（**拆成 6A–6E，逐个 session**）

> 这是全计划最高风险的区域，初版把它写成一个巨型阶段，一个 session 撑不住、中途 golden 变红也分不清是哪一刀。
> 本修订版拆成五个小阶段，每个独立分支、独立可回滚，**全程在 Phase 0 的格式无关 golden 测试保护下进行**。

#### Phase 6A — RuntimeEvent 边界（不拆 pipeline）

**Goal**：把跨 Host→UI 的裸 dict 换成 dataclass，但**不动 pipeline 内部结构**。

**In scope**
- `spica/core/events.py`：`RuntimeEvent` 及子类型（`StatusEvent` / `UnitTextReadyEvent` / `UnitReadyEvent` / `DoneEvent` / `ErrorEvent`）。
- 在 `streaming_pipeline` 的**输出处**做 `dict → RuntimeEvent` 适配（一层薄适配壳）。
- UI 侧加一个临时反向适配：`RuntimeEvent →` 旧 dict 格式，让现有 `ChatStreamController` **暂时不用改**也能跑。
- **双向等价测试**：新增测试确认「旧 dict ↔ RuntimeEvent」两个方向语义等价；并让 Phase 0 的格式无关 golden 测试**同时跑「旧 dict 路径」和「RuntimeEvent 路径」**，两边结果一致。否则适配壳可能悄悄改了语义。

**Out of scope**：拆 pipeline 组件、动 ChatEngine、删 SimpleAgent。

**Acceptance**：跨边界对象可以是 `RuntimeEvent`；双向等价测试 + 两条 golden 路径全绿；UI 行为不变；`python webui_qt.py` 正常。

**Rollback**：events.py + 两层适配 + 测试，单独 revert。 **main 仍可运行**：是。

#### Phase 6B — ChatEngine 接管对话驱动

**Goal**：新建 `ChatEngine` 承接「驱动」职责；`SimpleAgent` 退为兼容壳或被 Host 绕过。**仍不大拆 pipeline。**

**In scope**
- `spica/core/chat_engine.py`：成为 Host `conversation_surface` 背后的对话核心；`SimpleAgent` 的 `run_voice/stream_voice` 驱动逻辑迁移到这里（内部仍可调用现有 pipeline）。
- `AppHost.conversation_surface` 改为指向 `ChatEngine`，不再指向 `SimpleAgent` 包装。

**Out of scope**：拆 streaming_pipeline 内部组件、删 SimpleAgent 文件。

**Acceptance**：对话经由 `ChatEngine` 驱动；golden 全绿；`python webui_qt.py` 跑通一轮。**Rollback**：ChatEngine 新增 + Host 指向改动可 revert。 **main 仍可运行**：是。

#### Phase 6C — 拆解 streaming_pipeline 组件（**拆出来直接落进 `spica/runtime/`**）

**Goal**：把过程式核心拆成纯 Python 组件。**每拆一个，一个 commit，golden 必须仍绿。**

**In scope（每个一个 commit）**
- `spica/runtime/play_unit_splitter.py`（断句/播放单元）
- `spica/runtime/llm_stream.py`（流式 + DeepSeek 兼容——实为调用 Phase 5 的 LLM adapter）
- `spica/runtime/tool_round.py`（tool probe / followup）
- `spica/runtime/tts_job.py`、`spica/runtime/visual_job.py`、`spica/runtime/memory_commit.py`
  - **`memory_commit.py` 只能调 `MemoryPort.commit_turn`，自身不含任何抽取/去重逻辑**——抽取已在 Phase 5 下沉进 memory adapter。
    runtime 这一层对「记忆怎么抽、存哪」一无所知，只负责「这一轮该提交了」。这条是将来无痛换记忆后端的前提。
- `spica/runtime/orchestrator.py`（**只编排，调各 port，不做具体业务**）
- **把日语数学 `re.sub` 从 pipeline 挪回 `text_normalizer`**。

**Out of scope**：删 SimpleAgent、统一同步/流式、ChatState 状态机。

**Acceptance**：组件拆分完成；`orchestrator` 不含具体业务；**`spica/runtime/**` 内无记忆抽取逻辑（抽取只在 memory adapter 里）**；事件顺序 golden **仍全绿**；`python webui_qt.py` 完整跑一轮。
**Rollback**：每拆一个组件一个 commit，任何一步 golden 变红立即 revert 该 commit。 **main 仍可运行**：是（每个小拆分之后都绿）。

#### Phase 6D — 删除 SimpleAgent + 统一同步/流式 + 拆 getenv allowlist

**Goal**：清理。`SimpleAgent` 溶解完毕、删文件；消除同步与流式的重复；移除 Phase 3 的临时 allowlist。

**In scope**
- **删除 `agent/simple_agent.py`**（确认其职责已分别落到 Host 与 ChatEngine/runtime）。
- 统一同步与流式：把共享阶段抽成纯函数两边共用，或让同步路径由流式「收集所有单元后拼接」派生。
- 从 `tests/test_no_getenv.py` 删掉 `agent/simple_agent.py` 的临时 allowlist 项（此时业务代码应已无 `os.getenv`）。

**Out of scope**：ChatState 状态机、warmup 事件化（Phase 6E）。

**Acceptance**：`agent/simple_agent.py` 已删除；`test_no_getenv` 在去掉 allowlist 后仍绿；同步/流式不再重复同套阶段；`python -m pytest tests` 全绿。
**Rollback**：删除操作前先确认调用方已切换；可 revert 回兼容壳状态。 **main 仍可运行**：是。

#### Phase 6E — 状态机 + UI 薄层 + warmup 事件化

**Goal**：把 Phase 1 特意推迟的「启动事件化」连同运行态状态机一起做掉，让 UI 彻底退成「看状态机/收事件」的薄层。
（若一个 session 吃不下，可再分 6E-1「ChatState + 薄 ChatStreamController」与 6E-2「warmup 事件化 + 启动阶段」。）

**In scope**
- `spica/core/state_machine.py`：`ChatState`（`IDLE / LISTENING / GENERATING / STREAMING / SPEAKING / PAUSED / ERROR`）。
- `ChatStreamController` 退化为「接 `RuntimeEvent` → 更新状态机」的薄层，UI 行为只看状态机，不再直接看一堆 bool（`streaming_mode / playback_active / stream_done / ...`）。
- warmup 事件化：把原 `StartupWarmupWorker` 收进 `AppHost.initialize()` 的进度事件；引入启动阶段 `INITIALIZING / READY / ERROR`，UI 看事件显示 loading，而不是自己管一个 worker。

**Out of scope**：角色包、插件加载。

**Acceptance**：UI 不再依赖散落 bool；启动进度走事件；warmup 由 Host 编排；golden 全绿；`python webui_qt.py` 启动与对话正常。
**Rollback**：状态机/事件化为增量替换，可分 6E-1 / 6E-2 revert。 **main 仍可运行**：是。

---

### Phase 7 — CharacterPackage（多角色）

**Goal**：角色变成可移植数据包，引擎不再写死 Spica。

**In scope**
- `spica/core/` 定义 `CharacterPackage` schema：manifest + persona + **带情绪标签的立绘清单** + 语音参考 + 世界书（worldbook）。
- 角色包加载器；`AppHost` 加载「激活角色包」。
- 把现有 Spica 资产整理成一个角色包（沿用 `spica_data/Spica_skill/` 内容，但走包格式）。
- 预留导入/导出（`.char` 之类）的接口位，先不做完整 UI。
- **`CharacterPackage` 的身份键（`character_id`）必须与 Phase 5 `MemoryScope` 的 `character_id` 对齐**：记忆按 (角色, 用户) 隔离。
  切角色时，`ChatEngine` 用新的 `character_id` 构造 `MemoryScope`，确保不同角色的记忆互不串台。

**Out of scope**：AI 辅助生成角色、角色管理 UI（属 Phase 8/9）。

**Acceptance**
- 能加载**第二个**角色包并正常对话（哪怕是个最小测试角色）。
- Spica 作为一个角色包仍正常工作。
- **两个角色的记忆相互隔离**：给角色 A 提交的记忆，切到角色 B 时检索不到（验证 `MemoryScope` 生效）。
- `python -m pytest tests` 全绿（含「加载两个不同角色包」「跨角色记忆隔离」的测试）。

**Verify command**：`python -m pytest tests -q`

**Rollback**：schema + loader 新增，Host 加载点改动可单独 revert。

**main 仍可运行**：是。

---

### Phase 8 — ManagementSurface + 插件 manifest 加载（平台的管理面）

**Goal**：把 Phase 1 的占位实现掉；让外部插件能往注册表里注册能力。

**In scope**
- 正式实现并收紧 `ManagementSurface`（列 adapter / 列角色 + 插件 / 读写配置 / 装卸插件），替掉 Phase 1 的 `NotImplementedError` 占位。
- `spica/plugins/manifest.py` + `spica/plugins/host.py`：从 `data/config/plugins.yaml` 读清单 → 加载 `plugins/<包名>/` →
  插件把 adapter / tool 注册进 `CapabilityRegistry`。**第一阶段插件只允许注册 adapter / tool，先不开放 UI widget。**
- 脚手架（可选）：一个最小的「创建插件骨架」命令。

**Out of scope**：插件贡献 Settings UI / Chat 控件（更后）；MCP（Phase 9）；ASR adapter（除非届时已有稳定实现）。

**Acceptance**
- 放一个外部插件包（注册一个新 TTS adapter），它能在「可用 adapter 列表」里出现并可被配置选用，**核心代码不改**。
- 改 `plugins.yaml` 后重启即生效。
- `python -m pytest tests` 全绿。

**Verify command**：`python -m pytest tests -q` + 用一个示例插件人工验一遍

**Rollback**：插件子系统是新增层，可整体 revert 而不影响内置能力。

**main 仍可运行**：是。

---

### Phase 9 — 之后/可选（核心稳定后再说）

不在本轮硬性范围内，列出以免遗忘，按需再排：

- **ASRPort + 第二个 ASR adapter**（如 Whisper）——此时才有 ≥2 实现，port 才正当。
- MCP 接入（SSE / stdio，工具并入当前会话）。
- T2I（文生图）adapter（如 ComfyUI 工作流）。
- 插件贡献 Settings UI / Tools Tab / Chat 控件。
- 世界书 / 聊天模板的管理 UI、角色 AI 辅助生成。
- 替换/新增前端（Web / React 设置中心）——**因为 Host 框架无关，这只是再写一个订阅 Host 的 surface 消费者，核心一行不动。**

---

## §4 附录：骨架代码（目标形状，Claude Code 照着填实现）

> 下面是**目标形状**示意，类型/方法名按实际填充，以你真实的 `VisualDiffService` / `stream_voice` 签名为准。
> 重点是边界长什么样，不是抄死。

### A. AppHost 的两个窄面

```python
# spica/host/app_host.py   —— 必须薄：只接线 + 转发，不写业务
from typing import Protocol, Callable
from spica.core.events import RuntimeEvent

class ConversationSurface(Protocol):
    """给聊天窗。Phase 1 就落地。"""
    def start_turn(self, user_input: str) -> None: ...
    def interrupt(self) -> None: ...
    def stop(self) -> None: ...
    def switch_character(self, character_id: str) -> None: ...
    def subscribe(self, on_event: Callable[[RuntimeEvent], None]) -> None: ...

class ManagementSurface(Protocol):
    """给设置中心。Phase 8 才正式实现。"""
    def list_adapters(self, kind: str) -> list[str]: ...
    def list_characters(self) -> list[str]: ...
    def list_plugins(self) -> list[str]: ...
    def read_config(self) -> dict: ...
    def write_config(self, patch: dict) -> None: ...
    def install_plugin(self, ref: str) -> None: ...
    def uninstall_plugin(self, name: str) -> None: ...

class AppHost:
    def initialize(self) -> None:
        # Phase 1：这里只放从 _init_backend() 原样搬来的后端构造（仍 new 今天的 SimpleAgent/TTS/Visual），warmup 不动。
        # 下面的分步是「跨多个阶段最终演化成」的样子，不是 Phase 1 一次写完：
        #   1. (Phase 3) 加载 + 校验 Config（ConfigManager，替掉 os.getenv）
        #   2. (Phase 5/8) 建 CapabilityRegistry → 注册内置 adapter → (Phase 8) 从 manifest 加载插件再注册
        #   3. (Phase 5) 按配置里的名字解析激活 adapter（llm/tts/visual/memory/tool）
        #   4. (Phase 7) 加载激活的 CharacterPackage
        #   5. (Phase 3/5) 构造 Memory，绑定当前角色/会话
        #   6. (Phase 6B) 构造 ChatEngine
        #   7. (Phase 6E) emit INITIALIZING / READY / ERROR 进度事件（warmup 收进这里）
        ...

    @property
    def conversation_surface(self) -> ConversationSurface: ...

    @property
    def management_surface(self) -> ManagementSurface:
        raise NotImplementedError("ManagementSurface 在 Phase 8 实现")
```

### B. RuntimeEvent（Phase 6A 起，替掉裸 dict）

```python
# spica/core/events.py
from dataclasses import dataclass, field

@dataclass(frozen=True)
class RuntimeEvent: ...

@dataclass(frozen=True)
class StatusEvent(RuntimeEvent):
    state: str            # thinking / tools / ...
    message: str = ""

@dataclass(frozen=True)
class UnitTextReadyEvent(RuntimeEvent):
    index: int
    display_text: str
    tts_text: str
    emotion: str

@dataclass(frozen=True)
class UnitReadyEvent(RuntimeEvent):
    index: int
    display_text: str
    audio_path: str | None
    visual_cue: dict | None
    timing: dict = field(default_factory=dict)

@dataclass(frozen=True)
class DoneEvent(RuntimeEvent):
    full_answer: str
    final_emotion: str
    unit_count: int
    timing: dict = field(default_factory=dict)

@dataclass(frozen=True)
class ErrorEvent(RuntimeEvent):
    message: str
```

### C. ports 协议（Phase 5；ASR 延后，Tool 保留）

```python
# spica/ports/llm.py
from typing import Protocol, Iterator

class LLMPort(Protocol):
    def generate(self, messages: list[dict], **kw) -> str: ...
    def stream(self, messages: list[dict], **kw) -> Iterator[str]: ...

# spica/ports/tts.py   —— 把现有 TTSAdapter 折进来
class TTSPort(Protocol):
    def synthesize(self, request) -> object: ...   # 返回 TTSResult

# spica/ports/visual.py
class VisualPort(Protocol):
    def build_visual_payload(self, text: str, emotion: str) -> dict: ...
    def build_unit_visual_payload(self, unit) -> dict: ...

# spica/ports/memory.py
from dataclasses import dataclass

@dataclass(frozen=True)
class MemoryScope:
    character_id: str
    user_id: str
    conversation_id: str | None = None

@dataclass(frozen=True)
class MemoryItem:
    text: str
    score: float                 # 检索相关度
    type: str | None = None
    ts: float | None = None
    importance: float | None = None   # 预留：重要性评分（SQLite 可留 None，生成式后端填）

class MemoryPort(Protocol):
    # 交原始轮，抽取策略是 adapter 的实现细节（SQLite adapter 内跑现有规则抽取；生成式 adapter 转发给服务）
    def commit_turn(self, scope: MemoryScope, user_text: str,
                    assistant_text: str, meta: dict | None = None) -> None: ...
    def retrieve(self, scope: MemoryScope, query: str, limit: int) -> list[MemoryItem]: ...
    # profile / preamble 注入；SQLite adapter 可返回 None 或简单近期摘要
    def get_context_block(self, scope: MemoryScope) -> str | None: ...

    # —— 预留扩展点：Phase 5 只留占位，不实现具体逻辑；SQLite adapter 可 no-op / 返回 False ——
    # 睡眠/空闲整理、归档、过期处理等后台维护，由后端决定做什么（reason 例：'idle' / 'sleep' / 'shutdown'）
    def run_maintenance(self, scope: MemoryScope, reason: str) -> None: ...
    # 能力探测：后端声明支持哪些可选能力，如 'file_space'/'sleep_consolidation'/'archival'/'importance'
    def supports(self, capability: str) -> bool: ...
    # 角色专属文件空间等更重的能力，未来用 supports('file_space') 门控后再加具体方法，不在 Phase 5 落地。
# 注意：这是「生成式/外部记忆」的超集形状，不是照搬现在 SQLite 的 retrieve/upsert。
# Memory 不是普通 KV/RAG：长期记忆、睡眠整理、归档、角色文件系统是预留扩展点，不是 Phase 5 的必选实现。
# 理由见 Phase 5 设计注记：将来接 EverMemoryArchive 一类系统时，只写一个 adapter 即可，核心不动。

# spica/ports/tool.py
class ToolPort(Protocol):
    def schema(self) -> dict: ...
    def run(self, **kwargs) -> dict: ...

# 注意：ASRPort 不在 Phase 5 建（ASR 当前非核心链路，等接第二个引擎时再定，见 Phase 9）。
```

### D. CapabilityRegistry（Phase 5）

```python
# spica/plugins/registry.py
class CapabilityRegistry:
    def register_llm(self, name: str, factory): ...
    def register_tts(self, name: str, factory): ...
    def register_visual(self, name: str, factory): ...
    def register_memory(self, name: str, factory): ...
    def register_tool(self, schema: dict, handler): ...

    def resolve_llm(self, name: str): ...   # Host 按配置名解析
    def resolve_tts(self, name: str): ...
    # ...
```

---

## §5 一页速查

| Phase | 一句话 | main 仍可运行 | 风险 |
| --- | --- | --- | --- |
| 0 | CI + **格式无关** golden 测试，焊死现状 | 是 | 极低（纯新增） |
| 1 | **组装根上移进 `AppHost.initialize()`（承重墙）；warmup 不动** | 是 | 极低（机械搬迁 + 一处装配替换） |
| 2 | **只立**新包骨架 + Qt 隔离守卫测试，**不搬旧文件** | 是 | 极低（空包 + 一个测试） |
| 3 | 类型化 Config，Host 读配置；`test_no_getenv` **带临时 allowlist** | 是 | 低 |
| 4 | 引擎角色无关化（模板替正则，**先于角色包**） | 是 | 低 |
| 5 | LLM/TTS/Visual/Memory/**Tool** ports + registry，按名换引擎（**ASR 延后**） | 是 | 中 |
| 6A | RuntimeEvent 边界 + **双向等价测试**，不拆 pipeline | 是 | 低～中 |
| 6B | ChatEngine 接管对话驱动 | 是 | 中 |
| 6C | 拆 streaming_pipeline 组件（**拆出来直接落 `spica/runtime/`**，逐组件 commit） | 是 | **最高**（golden 保护） |
| 6D | 删 SimpleAgent + 统一同步/流式 + 删 getenv allowlist | 是 | 中 |
| 6E | ChatState + 薄 ChatStreamController + warmup 事件化 | 是 | 中 |
| 7 | CharacterPackage，多角色 | 是 | 中 |
| 8 | ManagementSurface + 插件 manifest 加载 | 是 | 中 |
| 9 | ASRPort / MCP / T2I / 换前端 等，核心稳定后 | 是 | 按需 |

**北极星**：Spica 先变成一个小而硬的 host —— 核心稳定、adapter 可换、UI 不碰业务、配置有 schema、运行时走事件、插件有边界。
**第一刀**：Phase 1，把组装根从 UI 搬进 `AppHost.initialize()`，**且只搬这个**。

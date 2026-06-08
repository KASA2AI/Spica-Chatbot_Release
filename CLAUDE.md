# CLAUDE.md — Spica 平台化重构 · 工作纪律

> 这份文件给 Claude Code 每个 session 自动加载。完整的分阶段路线在 `docs/REFACTOR_PLAN.md`。
> 本文件只放**不可破坏的约束**和**怎么干活**。开工前先读它，再读 `docs/REFACTOR_PLAN.md` 里当前阶段那一节。

---

## 这个工程在做什么

把 Spica 从「PySide Overlay 直接组装并驱动 Agent/TTS/Visual/Memory/Streaming 的桌面原型」，
重构成「**UI 框架无关的 AppHost 平台核心**」：核心稳定、adapter 可换、UI 不碰业务、配置有 schema、
运行时走 dataclass 事件、插件有边界。最终目标是**可换引擎、可换角色、可接插件、可换前端**的角色演出平台。

当前正在执行的是一次**绞杀式（strangler-fig）重构**，不是推倒重写。仓库一直是可运行的软件。

---

## 仓库命令（务必照用）

- Python 解释器：`/home/san/anaconda3/envs/gptsovits/bin/python`（conda 环境 `gptsovits`）
- **跑测试只用：`python -m pytest tests`**
- **绝不在仓库根目录跑裸 `pytest`** —— 它会递归扫描 `agent_tools/tts/vendors/GPT-SoVITS-.../runtime/` 里的第三方包，必崩。
- 启动桌面 Overlay：`python webui_qt.py`（Linux ibus 环境用 `./run_ibus.sh`）

---

## 不可破坏的约束（INVARIANTS）

违反下面任何一条，都视为本次任务失败，必须回退。

1. **Qt 隔离**：`spica/host/`、`spica/runtime/`、`spica/config/`、`spica/adapters/`、`spica/ports/`、
   `spica/core/`、`spica/memory/` 这些层**绝对不许 import PySide / Qt / 任何 GUI 库**。
   有一个守卫测试盯着；它变红就是真漏了，必须修，不许删测试或加豁免。

2. **main 永远能跑**：每个阶段做完，`python webui_qt.py` 能正常启动，`python -m pytest tests` 全绿。
   一个阶段 = 一个 feature 分支 = 一个（或一串）commit；分支合回 main 之前必须绿。

3. **机械搬迁步骤零行为变化**：标注「mechanical / move」的步骤，**不许改任何运行时行为**。
   只搬代码、改 import、改装配位置。如果某个改动会改变行为，它就属于另一个步骤，单独做。

4. **配置只走 ConfigManager**：业务代码里**禁止 `os.getenv()`**。
   只有 `spica/config/manager.py` 和 `spica/config/secrets.py` 允许读环境变量。
   API Key 等机密只进 env / secrets，不进普通 config 文件。

5. **不为单一实现造抽象（YAGNI）**：只有当某能力**确实有 ≥2 个实现、或本路线图里明确即将有第二个**时，
   才建 `ports/` 协议 + `adapters/` 实现对。本项目里 LLM / TTS / Visual / Memory / Tool 在 Phase 5 即将被 Host 按名解析
   或马上要用，正当；**ASR 先延后**到真要接第二个 ASR 引擎时再定。其余拿不准就先不抽。

6. **Host 必须薄**：`AppHost` 只能有 `initialize()` 的接线逻辑 + 几个转发方法。
   真正的业务在 `ChatEngine` / `ConfigManager` / `CapabilityRegistry` / 各 adapter 里。
   一旦发现 Host 里开始写业务逻辑，就是它正在变成新神类的信号，立刻把逻辑往下沉。

7. **跨 Host→UI 边界的事件是 dataclass**：用 `RuntimeEvent`（及其子类型），
   **不许再传裸 `{"event": ..., "data": ...}`**。

---

## 每个任务的「完成定义」（Definition of Done）

每次 session 收尾前，逐条自检：

- [ ] 本阶段「Acceptance」里列的点全部满足
- [ ] `python -m pytest tests` 全绿（含本阶段新增/已有的 golden 测试）
- [ ] `python webui_qt.py` 能启动（必要时人工确认，或留 smoke test）
- [ ] Qt 隔离守卫测试绿（Phase 2 之后存在）
- [ ] 没有引入新的 `os.getenv()` 到业务代码（Phase 3 之后有 grep 测试盯着）
- [ ] 改动范围没越出本阶段「In scope」声明的文件；越界的话停下来报告，不要自作主张扩大
- [ ] 写了 commit message，说明这是哪个 Phase 的哪一步

---

## 不要做的事

- 不要一上来重写成 React / Tauri / Web。现在是 PySide，先把 Python 内部边界拆清楚。换前端是最后一层。
- 不要先做大插件系统。先做 adapter registry。插件是开放能力的最后一层，不是用来救内部混乱的。
- 不要继续往 `agent/streaming_pipeline.py` 里加功能。它是核心高危区，只能拆、不能塞。
- 不要让 UI 直接知道 GPT-SoVITS / OpenAI / SQLite / VisualDiffService 的细节。
  UI 只应该知道：「开始聊天」「停止」「切角色」「播放状态变了」「立绘变了」。
- 不要把 `MemoryPort` 设计成普通 RAG/KV 接口（只有同步 `retrieve()`/`upsert()`）。长期记忆、睡眠整理、
  角色文件系统、重要经历归档要作为**后续扩展点预留**（统一可选钩子 + 能力探测），Phase 5 只给最小接口，详见 `docs/REFACTOR_PLAN.md` Phase 5。
- 不要把竞品（Shinsekai）的实现细节当成事实写进代码注释或文档。它的**方向**值得参考，
  但很多被传述的细节（React 设置中心、YAML DAG workflow 等）**未经核实**，可能不准（见 `docs/REFACTOR_PLAN.md` §0）。

## 配套计划：核心 turn 硬化（进行中）

当前正按 docs/REFACTOR_PLAN_CORE.md 硬化核心 turn（阶段 C0–C8）。做该计划内的任务时，
先读那份文件的对应阶段。下面的「核心 turn 不变量」会随阶段落地逐条补全——
**未标「已生效」的条目此刻还不是规则，不要照它改代码。**

### 核心 turn 不变量
- [已生效] screen 工具必须保留 is_screen_intent_explicit 意图门，且本地分析、绝不上传截图。
- [已生效] 手动截图是 attachment（用户已决定“看这张”），不是 tool；不得改成由模型决定是否分析用户已附的图。
- [已生效] 有序释放只能走 Sequencer；不许出现手动 index 重排字典。
- [已生效] 只有 run_turn / stream_answer 能产出 RuntimeEvent；其余 stage 是 (ctx, services, deps)->ctx 的纯转换（services 是过渡载体，C5/C6 退场），返回 ctx、不许自己 emit。守卫：转换层（spica/conversation/ + spica/runtime/stages.py）不许 import spica.core.events（N1-final，tests/test_layering.py）。
- [已生效] 并发只能走注入的 ExecStrategy；业务 stage 内不许 new ThreadPoolExecutor。
- [已生效] 运行时核心（spica/runtime/）不许出现 dict 配置或 client+adapter 双字段兜底；只用 AppConfig + 已解析 port。唯一例外是 deps.py 桥（legacy services → typed deps）。（C4 已落地：stages 读 deps.config / deps.llm / deps.memory，agent/ 已删。）
- [已生效] spica 不许 import agent（agent/ 已删；agent_tools 是独立包，允许）。守卫：tests/test_layering.py（N3-layer）。
- [已生效] turn/stage 编排层的计时/日志只能走注入的 TurnObserver（span/mark/event）；stage 内不许直接 log_timing。observer 的 sink 就是 ctx.timing（done.timing 不变）。唯一包 log_timing 的是 spica/runtime/observer.py；adapter（LLM/TTS/screen）内部低层诊断日志不在此限。守卫：tests/test_no_log_timing.py（N4-observe）。
- [C6 落地后生效] memory commit 走注入的 JobRunner，不堵 hot path。
- [C7 落地后生效] inspect_screen 由 CapabilityRegistry 注册、运行时从 registry 解析；不再读静态 TOOL_SCHEMAS。
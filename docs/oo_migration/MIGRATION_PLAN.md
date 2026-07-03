# Spica Long-Term OO Migration Plan

## Metadata

- **Version**: v2
- **Date**: 2026-07-03
- **Status**: Phase 0 ready for approval
- **Supersedes**: v1（v1 全部有效内容已整合进本文；本文为唯一执行版本，执行任何 phase 不需要回读 v1。v1 原文存档于 `MIGRATION_PLAN_v1.md`，仅供追溯，不作执行依据）
- **Scope**: 把五个 seam 再契约（LLM provider / host domain assembly / prompt context contributor / character-memory scope / tool authority）变成一串可单独批准、单独收口、单独回滚的 phase。终点五个硬能力：① 非 OpenAI provider 只需写一个 adapter；② 新 domain 上下文注入不碰 runtime 高危文件；③ 第二角色接入不产生静默记忆污染；④ 新 domain 装配不使 AppHost 增长（≤15 行/domain）；⑤ act 工具规模化时权限面恒定收敛在 host。
- **Non-goals**: co-watch / browser / 视频陪看等 feature 本身（另行立项，本计划只保证 seam 先于 feature 就位）；Windows / installer / local_runtime / packaging；对当前架构的重新评审。
- **重要声明**：**本文是迁移计划，不代表当前已实现架构。** 文中 file:line 引用是计划制定时的阅读锚点，开工前须按各 phase 的开工 checklist 重新校准。本文不声称任何测试已被执行；所有测试 gate 均为**应执行**命令：`python -m pytest tests -q`（绝不裸 `pytest`，会递归扫 vendored GPT-SoVITS runtime 崩溃）。
- 配套文件：`README.md`（状态板 + 使用规则）、`PROGRESS.md`（逐 phase 收口日志）。

---

## Current Approval State

> 状态取值：`ready for approval` / `ready after dependency` / `not approved` / `feature-triggered`；`complete` / `blocked` 为收口后回填的保留值。

| Phase | 名称 | 前置 | 状态 | 备注 |
|---|---|---|---|---|
| 0 | Characterization 保护面（只加测试） | — | **ready for approval** | 实现提示词见「Phase 0 Implementation Prompt」 |
| 0D | Phase 0 文档收口（微 phase） | 0 收口 | **ready after dependency** | 纯文档 |
| 1 | galgame prompt_sections 出走 stages | 0 | not approved | 拍板顺序：0→1→2→3 |
| 2 | CharacterScope + scoped recent + MemoryScopeStrategy | 0（顺序排在 1 后） | not approved | 全计划唯一主动行为变更 |
| 3 | PromptContextContributor seam | 0, 1 | not approved | contributor 机制已裁决（方案 a·galgame 兼容限定版） |
| 4R | registry ToolEntry NamedTuple（微 phase） | 0 | not approved | 可穿插在 1/2/3 之间任意排期 |
| 4 | ReactionScoringPolicy + reaction assembly | 0, 2, 4R | not approved | 含 patch 有效性退出条件 |
| 5 | deps 单轨化（stages/memory_commit 禁区版） | 4 | not approved | 三 commit：5-c0/c1/c2 |
| 6a | TextModel + BoundModel + summarizer/judge 收编 | 5 | not approved | 同 commit 落 D3 弱守卫 |
| 6b | ModelRouter 收编 host endpoint 决策 | 6a | not approved | 可与 Phase 7 对调，不占 D3 时钟 |
| 7 | ToolCallingModel 生产链 flip | 5, 6a | not approved | 三 commit：7-c0/c1/c2 |
| 8 | ActiveDomainRouter + WindowTarget/PrivacyGate + request 落点泛化 | 3, 4 | **feature-triggered**（co-watch 批准） | 含 context.py / chat_engine.py 受控改动预留 |
| 9 | ToolAuthority + ToolExecutionPolicy | 4, 8 | **feature-triggered**（browser/media 批准） | — |

批准纪律：**一次只施工一个 phase，逐个批准，禁止打包批。** 批准任何新 phase 前，先核对「Dual-Track Governance」登记表——有超期双轨则先收口旧账。

---

## Migration Principles

**1. 不推倒重写。** 本仓的核心资产是保护面，不是代码：12 个 AST/语义守卫（`test_layering` / `test_no_getenv` / `test_turn_contract` / `test_resolved_config_equivalence` / `test_no_dict_config` 等）、golden 锚（`sync_chain.py` 冻结链 + `test_golden_sync` / `test_golden_streaming` / `test_turn_contract` 7 形态）、40+ 处测试对模块路径的 import/patch 耦合（如 `patch("spica.runtime.stages.analyze_screen_attachment")` 5 处、`test_moondream_default_cutover` 15-patch 驱动 `initialize()`）。重写 = 同时作废被保护物与保护物；本仓由 AI 会话高频维护，平行结构是误改温床。

**2. Aggressive strangler。** 新旧两侧共存于同一测试面下（facade 保 import/patch 点、golden 保行为字节），每 phase 独立收口。「aggressive」指节奏与收口彻底性：一个 seam 在一个 phase 内完成切换、守卫封旧、文档改向；绝不允许两个 seam 同时处于半迁移态。

**3. Phase 完成条件（六项全过才算完成）：**

- (a) `python -m pytest tests -q` 全绿（含该 phase 加强后的守卫）；
- (b) 该 phase 定义的 parity gate 达标（prompt/payload 字节级或断言级）；
- (c) 旧 seam 使用计数归零——grep/AST 可验证的禁区内零引用；
- (d) 文档（CLAUDE.md / GUARDRAILS 对应条目）与代码同 commit 更新（test-only phase 例外见第 5 条）；
- (e) 单 commit 或线性小 commit 序列，`git revert` 干净可回；
- (f) 双轨登记表更新（开钟/停钟），并在 `PROGRESS.md` 记收口日志。

**4. 必须停止并回滚的情形。** 任一守卫测试变红且修复需要「放宽守卫」；prompt/payload parity 出现无法解释的字节差；实施中发现需要修改「forbidden files」清单内的文件；实施 agent 提出的「顺手改进」超出 allowed files。任一发生 → `git revert` 本 phase 全部 commit，回到上一收口点，重新评审 phase 定义，而不是现场扩权。

**5. Test-only phase 的 docs 例外（v2 新增）。** 严格只加测试文件的 phase（Phase 0）**不更新** `README.md` / `PROGRESS.md`；文档回写拆为独立微 phase（Phase 0D）。生产 phase（1 起）维持「文档与代码同 commit」。

**6. 白名单完备性批准前置检查（v2 新增）。** 每个 phase 批准前，必须先执行该 phase 定义中列出的「爆炸半径 rg」，确认所有会因本 phase 变红的既有测试文件都已在 allowed files 内。白名单外必改 = 计划缺陷，退回修计划，不是现场扩权。

**7. 双轨寿命治理。** 每个双轨点在「Dual-Track Governance」登记最长并存 phase 数，超期即阻塞后续 phase 批准；旧 seam 退役的同一 commit 加 AST/grep 守卫封死旧入口；CLAUDE.md / GUARDRAILS 决策树在 seam 切换的同一 commit 改向（否则未来 AI 会话被旧模板引回旧落点）；冻结区显名——`sync_chain.py` 一族的「永久 v1」不算双轨，算博物馆，在 Decision Log 单独记账。

---

## Target Interfaces And Objects

> status 取值：**Y1**（第一年做）/ **on-demand**（等真实需求）/ **do-not-build-now**。

### CharacterScope

- **status**: Y1（Phase 2）
- **interface shape**: `@dataclass(frozen=True) CharacterScope: character_id: str; user_id: str`，落 `spica/runtime/scope.py`（不动高危 `context.py`）。
- **v2 correction**: CharacterScope 是 frozen **值类型**；唯一活来源是 `AppHost.character_scope` **property**（每次访问从 `self.config.character` 现算并集中 `or "spica"` / `or "麦"` 回退）。**不给 TurnDeps 挂 character_scope 字段**——PersonaRuntime 落地前必须保持 `set_interlocutor_name`（`chat_engine.py:211-226` 原地突变 config）的今日 rename 语义，冻结 user_id 会改变改名后 CompanionBeat 检索行为。
- **compatibility facade**: 无需——纯增量；app_host 内 14 处身份默认值（`:432-433,489-490,512-513,625-626,702-703,764-765,777,800`）机械替换为 property 读取。
- **exit condition**: `rg -n 'or "spica"|or "麦"' spica` 生产代码仅剩 `scope.py` 常量定义处 1 处命中。

### MemoryScopeStrategy

- **status**: Y1（Phase 2）
- **interface shape**: 具体类（非 Protocol——单实现，立项理由：把「retrieve 与 commit 写读对称」从注释纪律变成单一居所）。`MemoryScopeStrategy(config: AppConfig)`：`recent_key(request) -> str`（= `scoped_conversation_id(character_id, request.conversation_id)`）、`ltm_scope(request) -> MemoryScope`（用 `effective_memory_conversation_id`）、`clear_targets(conversation_id) -> (recent_key, ltm_conversation_id)`。落 `spica/runtime/scope.py`。
- **v2 correction**: 方法**调用时活读 `config.character`**（保持 rename 语义）；stages / memory_commit 调用处以 `deps.config` 现场构造（构造零成本），chat_engine 持一个实例（同一 config 对象）。
- **compatibility facade**: 三个消费点（`stages.py:157` 读、`memory_commit.py:41,64-68` 写、`chat_engine.py:247,250` 清）改为经 strategy，公开函数签名全部不变。
- **exit condition**: 三点全部经 strategy + 对称性测试直接断言「retrieve 与 commit 用同一 `ltm_scope`、clear 双边同 key」。

### PersonaRuntime

- **status**: on-demand（第二角色包真实立项时）
- **interface shape**: 不可变 persona 包（id/名字/profile/skill_dir/visual/tts 引用），切角色 = 换引用，废除 `set_interlocutor_name` 原地突变（`chat_engine.py:211-226`、`agent_assembly.py:180-183`）。前置（CharacterScope、strategy）已在 Phase 2 备齐。
- **compatibility facade**: `set_interlocutor_name` 签名不变、内部换实现。
- **exit condition**: AppConfig 装配后零突变（可加守卫断言）。
- **v2 correction**: 在此之前 strategy/scope 一律活读（见上两条）。

### PromptContextContributor

- **status**: Y1（Phase 3）
- **interface shape**:

  ```
  Protocol: name: str; priority: int
    mode(request: TurnRequest) -> Literal["active","offline","none"]
    sections(ctx: TurnContext, deps: TurnDeps, mode: str) -> list[str]
  ```

  gate 签名**只收 request**。裁决理由：① domain 运行时状态进 gate 的合法通道是 binding 发布盖章进 request（`GameTurnBinding` → `game_context_request`，`chat_engine.py:79-99`），不是 gate 直读活 session；② 给 gate 开 `(ctx, deps)` 等于给未来 contributor 在每个普通聊天 turn 上做 DB 读/开 span 的通道，「none = 字节级 no-op」将只能靠自觉维持；③ `ctx.error` / `ctx.prompt` 检查是 node 级通用逻辑。窄签名是结构性防线，宽签名是纪律性防线。
- **v2 correction（注册机制，已拍板）**: `TurnDeps.context_contributors: tuple[...] | None = None`；`__post_init__` 在其为 `None` 时补 `(galgame_contributor,)`（**galgame 兼容 auto-fill，仅此一项、永不长第二项**；`__post_init__` 内函数级懒 import，deps.py 模块级不引 galgame）；显式传 `()` = 明确关闭。**未来 domain 必须经 assembly 显式注册完整 tuple（含 galgame contributor），不得依赖 auto-fill。** auto-fill **不按 `game_memory` 是否为 None 条件化**——game_memory 缺失由 contributor `sections()` 内部空转处理，这使「active + game_memory/prompt 为 None 时开 span」的今日 timing 语义逐字节保持。
- **v2 correction（telemetry，已拍板）**: 单 contributor 时代 span 名保持 `retrieve_game_context_node`（timing key `retrieve_game_context_node_ms`）。
- **compatibility facade**: `retrieve_game_context_node = contribute_context_node` 模块级别名**永久保留**；直构 TurnDeps 的既有测试（约 25 处直调）零改动。
- **exit condition**: `orchestrator.py:261` / `sync_chain.py:51` 调新名；Phase 0 golden 字节不变；N1 扫描含新文件；三个直调测试文件零改动全绿。

### DomainModule / CapabilityInstaller

- **status**: Y1（Phase 4 立约定，逐 domain 落地）
- **interface shape**: 非基类，是约定：`spica/host/assemblies/<domain>.py :: install(host: AppHost) -> Handle`。installer 在 host 包内 → 铁律 #7 由包边界保住。
- **v2 correction**: 薄委托 facade 必须**仍是唯一构建路径**（`install()` 内部经 AppHost 委托方法构建），不只是「方法存在」——防 `patch.object(AppHost, "_new_reaction_judge", ...)` 静默变 no-op。
- **compatibility facade**: 被搬空的方法留薄委托一个 phase 后删除（Phase 5-c2）。
- **exit condition（Phase 4）**: reaction 接线全部出 `app_host.py`，AppHost 侧仅剩 `assemblies.reaction.install(self)` 一行级调用 + patch 有效性验证常驻。

### ActiveDomainRouter

- **status**: on-demand（co-watch 批准时，Phase 8）
- **interface shape**: `publish(domain, binding: DomainTurnBinding, priority)` / `retract(domain)` / `current() -> DomainTurnBinding|None`。ChatEngine 的单槽 provider（`chat_engine.py:53`）指向 `router.current`。
- **v2 correction**: 「ChatEngine 接口零改动即是 facade」仅对 galgame 成立。域 #2 的 request 落点需要 `context.py`（泛化 `DomainContextRequest` 槽）与 `chat_engine.py._request`（double-wrap guard 从 GALGAME 前缀泛化，`chat_engine.py:82`）的受控改动——已预留进 Phase 8 白名单，具体设计到 Phase 8 再做。
- **exit condition**: galgame 经 router 发布（复用 `companion_controller.py:243-256` publish-last / `:262-265` clear-first 纪律），galgame 全族测试绿。

### ToolAuthority

- **status**: on-demand（browser/media 批准时，Phase 9；模式先入文档）
- **interface shape**: per-domain 类（如 `BrowserAuthority.open_site(site_key, query)`、`MediaAuthority.playback(cmd: Literal[...])`），只在 `spica/host/` 构造，藏 URL 模板白名单/参数校验/窗口所有权/事件分发。
- **compatibility facade**: 现有 4 个闭包（`_request_song` / `_record_game_observation` / `_record_play_history` / beat writer）不强制改造，新 act 工具必须走对象形态。
- **exit condition**: Phase 9 交付首个 authority 类 + 新守卫（`spica/host/` 之外禁实例化 `*Authority`）。

### ToolExecutionPolicy

- **status**: on-demand（Phase 9 与首个真实 policy 同时激活）
- **interface shape**: `check(name, effect, meta) -> Allow|Deny(reason)`，挂唯一执行入口 `RegistryToolSet.run`（`spica/runtime/tools.py:125-131`，effect 现仅日志）。此前不建——choke point 已在，后接约 10 行。
- **exit condition**: 首批 policy（act 忙态互斥/频率闸）有测试。

### TextModel

- **status**: Y1（Phase 6a）
- **interface shape**: `complete(prompt, *, model) -> str`；`stream(prompt, *, model, state) -> Iterator[str]`（request dict 在 adapter 内部组装——这就是 depth）。`BoundModel(adapter, model)`。落 `spica/ports/model.py`。
- **v2 correction**: 与 ModelRouter 拆开——6a 只做 TextModel/BoundModel + 消费者收编，`_new_*` 内手工组 BoundModel。
- **compatibility facade**: LLMPort v1 全保留（冻结链永久用户）；`OpenAICompatibleAdapter` 双实现，v2 方法内部复用 v1 路径（修 bug 单点）。
- **exit condition**: summarizer/judge 只依赖 TextModel/BoundModel（构造签名 `(llm, model)` → `(bound: BoundModel)`）。

### ToolCallingModel

- **status**: Y1（Phase 7）
- **interface shape**: `probe(prompt, tools, *, model, state) -> ToolProbeResult(calls, text)`（非流式）；`probe_stream(...) -> ToolProbeStream`（`.deltas` 迭代器 + 耗尽后可读 `.calls`）。端点家族选择（Responses vs Chat Completions）内化进 adapter。
- **v2 correction（契约，已裁决）**: `.calls` **仅在 `.deltas` 正常耗尽后可读**；cancel 提前弃读或中途异常 → `.calls` 未定义，调用方不得读；probe 中途 cancel → 不产生 STREAM_RESET、不执行工具（对应今日 `tool_round.py:136-137`）。写进 port docstring 并有专测。
- **compatibility facade**: v1 方法族与 `prefers_chat_completions` 保留给冻结链。
- **exit condition**: `tool_round.py` / `orchestrator.py` 零引用 v1 探针方法与 `prefers_chat_completions`（AST 守卫封死）。

### ProviderTraits

- **status**: Y1（Phase 7，最小化）
- **interface shape**: adapter 侧 frozen dataclass（流式探针能力/reasoning 词汇/工具方言），供 adapter 内部路由与 management 面展示。
- **v2 correction**: **runtime 禁读由 AST 守卫执行**（`test_no_v1_llm_in_runtime` 同时禁 `traits` / `provider_traits` 属性读于 orchestrator/tool_round），不再只靠契约注释。
- **exit condition**: 随 Phase 7-c2 落地即完成。

### ModelRouter

- **status**: Y1（Phase 6b）
- **interface shape**: host 侧 resolve-once：`for_role("dialogue"|"judge"|"summary") -> BoundModel(adapter, model)`，收编 `app_host.py:556-598` 三处 fallback（summary_model 回退、judge model/key/base_url 回退树）。
- **v2 correction**: 独立 phase（6b），可与 Phase 7 对调，独立 revert。
- **compatibility facade**: `_new_summarizer` / `_new_reaction_judge` / `_judge_llm_adapter` 方法名保留（cutover 测试 patch 目标），内部改调 router。
- **exit condition**: 三处 endpoint 决策唯一居所。

### WindowTarget

- **status**: on-demand（Phase 8）
- **interface shape**: frozen dataclass（window_id / match_rule / owner_domain），替换 `_companion_watch_context` 返回的裸 5 元组（`app_host.py:680-686`）。

### PrivacyGate

- **status**: on-demand（Phase 8）
- **interface shape**: `evaluate(target, purpose) -> WindowSafetyResult`，收编三份安全拷贝（`ocr_loop.py:149-173`、`session.py:88-100` 状态集、`watch_game_screen.py:158-165`）并顺手收窄已记录的 check→capture race（P1）。单实现立项理由：安全不变量集中化 + 第二消费者（co-watch 截帧）随 Phase 8 到来。
- **exit condition**: 三处旧拷贝删除、gate 有独立单测、galgame 隐私行为回归绿。

### 明确不做（do-not-build-now）

TurnPipeline/Stage 类化（单一生产形状 + locality + N1 守卫全站在函数式一边）、GameSessionRegistry（同类型并发无用户故事且撞 GPU 约束）、ConversationScope（TurnRequest 已是）、ConfigSnapshot（杀原地突变即达成）、ToolConversation、StructuredOutputModel（直至 provider 保证 + 质量刚需）、ScreenEnvironment、MemoryPort 空钩子扩展、工具垫片重继承基类。

---

## Phase Plan

### Phase 0 — Characterization 保护面（只加测试）

- **approval status**: **ready for approval**
- **objective**: 为 Phase 1–7 铺安全网。零生产代码改动，零既有测试改动，零文档改动。
- **why this phase exists**: 后续所有 phase 的 parity 判据在此定义；无它则 Phase 1 的「字节等价」无判据、Phase 7 的「flip 形状不变」无判据、Phase 2 的「红转绿」无基线。
- **allowed files**（只此四件，全部新增）: `tests/test_app_host_tool_registration.py`、`tests/test_game_prompt_golden.py`、`tests/test_responses_probe_shape.py`、`tests/test_recent_memory_scope.py`。
- **forbidden files**: `spica/**`、`memory/**`、`agent_tools/**`、`ui/**`、`docs/**`（含 `README.md` / `PROGRESS.md`——回写归 Phase 0D）、一切既有测试文件。
- **behavior change allowed?**: 否。
- **compatibility facade**: 不适用。
- **characterization tests to add or update**（约束摘要，完整规格见「Phase 0 Implementation Prompt」）:
  1. AppHost 工具注册元数据——**只准公共 registry 接口**（`list_adapters` / `tool_schemas` / `tool_intent_gated` / `tool_effect` / `tool_compact_output` / `tool_handler`），禁止访问 `_tools` 等任何下划线私有属性；watch/note 的 available 状态用公共行为表达（在 `list_adapters("tool")` 中、初始化前不出现在 `tool_schemas()` 供给名单）。定位是「补齐缺口 + 集中背书」：与 `tests/test_sing_song_tool.py:246-250`（effect 全断言）、`tests/test_watch_game_screen.py:342-362`、`tests/test_note_game_observation.py:308-326` 的重复是有意的；真正的新增断言是 `tool_intent_gated("sing_song") is True` 与 `tool_compact_output("inspect_screen") is not None`。
  2. galgame prompt full-section golden——**禁用 `ManualGameMemory`**（`spica/galgame/manual.py:106,120,142,161,181` 自动打 `utc_now_iso()`，会把 wall-clock 渲染进 `[GAME_PROGRESS].last_played_at` 与 `[RECENT_GAME_SUMMARIES].created_at`）；直写模型对象，全部时间戳显式固定且彼此错开；active / offline 两态整段 golden；同输入连调两次逐字节相同；附「active + `deps.game_memory=None` 时 `ctx.timing` 含 `retrieve_game_context_node_ms`」的现状断言（Phase 3 span 语义基线）。
  3. Responses probe request shape——**fake 打在 OpenAI client 层**（`client.responses.create` 录 kwargs，仿 `tests/test_turn_contract.py` 形制），禁止 fake LLMPort 层（port 级 fake 在 Phase 7 flip 时判据自毁）。(a) 工具 probe shape 经 `prepare_prompt_for_streaming` + 真实 `OpenAICompatibleAdapter` 测；(b) 无工具 final request 无 `tools` 键走 **adapter 级 `iter_response_text`** 测——不让 prepare 承担它测不到的断言（无工具时 prepare 直接返回，不发请求）。
  4. recent 跨角色污染基线——`@pytest.mark.xfail(strict=True, reason=...)`；写路径必须经 `save_stream_memory`（deps 用角色 A 的 config 新构造），读路径必须经 `load_recent_context_node`（deps 用角色 B 的 config **重新构造**）；禁止直写 recent deque、禁止原地突变后复用旧 deps；由 Phase 2 转绿。
- **required test gate**: `python -m pytest tests -q` 全量绿（xfail 计 xfailed 不计 failed）；`tests/test_game_prompt_golden.py` 单独连续跑两遍结果一致。
- **rollback**: 单 commit revert（纯新增文件，删除即回滚）。
- **exit conditions**: 四个新文件落地且上述 gate 达标；收尾报告含新增文件清单、全量真实输出、golden 样本生成方式说明。
- **unlocks / blocks**: 解锁 Phase 0D 与 Phase 1；不完成则禁止一切生产代码 phase。

### Phase 0D — Phase 0 文档收口（微 phase）

- **approval status**: **ready after dependency**（Phase 0 收口后）
- **objective**: 回写 `docs/oo_migration/README.md` 状态板 + `PROGRESS.md` Phase 0 条目。
- **why this phase exists**: 迁移原则第 5 条——test-only phase 严格只加测试文件，文档回写拆出以保持 Phase 0 白名单纯净。
- **allowed files**: `docs/oo_migration/README.md`、`docs/oo_migration/PROGRESS.md`。
- **forbidden files**: 其余一切。
- **behavior change allowed?**: 否（纯文档）。
- **compatibility facade**: 不适用。
- **characterization tests to add or update**: 无。
- **required test gate**: 无（纯文档 phase；建议顺手执行 `python -m pytest tests -q` 确认无意外改动）。
- **rollback**: 单 commit revert。
- **exit conditions**: 状态板 Phase 0 行置「已收口」+ PROGRESS 条目按模板补全。
- **unlocks / blocks**: 无阻塞关系；惯例上先于 Phase 1 批准完成。

### Phase 1 — galgame prompt 段落构建器出走 stages.py

- **approval status**: not approved
- **objective**: `stages.py` 的 galgame 展示逻辑迁入 domain 包 `spica/galgame/prompt_sections.py`；stages 只留 gate + node。
- **why this phase exists**: 最小生产刀，验证 facade + golden 纪律；为 Phase 3（contributor 包住干净的 sections 模块）铺路。拍板顺序：排在 Phase 2 前。
- **allowed files**: `spica/galgame/prompt_sections.py`（新）、`spica/runtime/stages.py`（删 + import）、`tests/test_layering.py`（`TRANSFORM_LAYER_FILES` **增加** `prompt_sections.py`——只扩域）、`CLAUDE.md` §2 表、`docs/DEVELOPMENT_GUARDRAILS.md` §9。
- **forbidden files**: `orchestrator.py`、`sync_chain.py`、`tool_round.py`、`context.py`、`deps.py`、`app_host.py`、一切 adapter、一切既有测试（test_layering 除外）。
- **behavior change allowed?**: 否。
- **compatibility facade**: node 与全部 gate 符号原地不动（`retrieve_game_context_node`、`_game_context_mode`、`_resolve_game_target`、`_parse_*`、`_GALGAME_CONVERSATION_PREFIX`、`analyze_screen_attachment` 一带 patch 点全部留在 stages.py）。
- **characterization tests to add or update**: 无新增（判据 = Phase 0 golden #2）；test_layering 只扩扫描域。
- **required test gate**: `python -m pytest tests -q` 全量绿，重点关注 `test_game_prompt_golden`（字节等价判据）、`test_retrieve_game_context_node`、`test_game_context_in_chain`、`test_current_line_injection`、`test_layering`。
- **rollback**: 单 commit revert。
- **exit conditions**:
  1. 搬迁清单完整迁移：`_section`、`_format_progress/_format_summaries/_format_buffer/_format_relations/_format_choices/_format_beats`、`_build_game_context_sections`、`_should_inject_companion`、`_COMPANION_INTENT`、`_GAME_CONTEXT_ACTIVE_SUMMARY_LIMIT`（阅读锚点 `stages.py:65,72,369-372,375-525`）；
  2. 新模块 import 白名单恰为 `json`、`typing.Any`、`spica.conversation.character_loader.DEFAULT_INTERLOCUTOR_NAME`（`_build_game_context_sections` 在 `stages.py:516` 引用）；禁止 import `spica.core.events`、`spica.galgame.session`、`spica.runtime.*`、Qt；
  3. 等价判据：**输出 prompt 字节等价**（Phase 0 golden #2；源码允许最小必要改写，不承诺源码逐字节）；
  4. **依赖边声明（v2）**：phase 报告记录本 phase 诞生 runtime→galgame 首条依赖边（stages.py import prompt_sections；反向 galgame→runtime 已存在于 `session.py` / `companion_controller.py`），并给出模块级无环论证（prompt_sections 只 import json/typing/conversation）；`test_layering::test_spica_packages_import_cleanly` 为该风险 gate；
  5. 开工 checklist：对本节全部行号引用重新 rg 校准。
- **unlocks / blocks**: 解锁 Phase 3；不完成禁止 Phase 3 与任何新 domain 上下文注入需求。

### Phase 2 — CharacterScope + scoped recent + MemoryScopeStrategy v1

- **approval status**: not approved
- **objective**: 多角色数据安全的硬爆点在任何角色需求到来前拆除：recent 桶 key 从裸 `conversation_id` 变 `{character_id}::{conversation_id}`；`clear_memory` 的 recent/LTM 不对称（`chat_engine.py:247` vs `:250`）同步修齐；身份默认值收敛。
- **why this phase exists**: ① recent 是纯内存 deque（`memory/recent.py:10`）——重 key 零数据迁移，是全计划最便宜的硬爆点修复；② 污染类缺陷静默（A 的近期上下文漏进 B，无异常无日志），越晚越可能在无守卫状态下触发；③ Phase 4 要搬 `app_host.py` 的 reaction 接线，先收敛身份默认值，Phase 4 搬的就是干净代码；④ 多角色的「数据安全半」必须最前段完成；「运行时切换半」（PersonaRuntime）等第二角色包真实立项。
- **已裁决事项（施工前提，不得现场重议）**:
  - **frozen-vs-live**: `MemoryScopeStrategy` 各方法调用时活读 `config.character`（保持 `set_interlocutor_name` 的今日 rename 语义）；`AppHost.character_scope` 为 property（每次访问现算）；不给 TurnDeps 挂 scope 字段。
  - **三处身份默认值**: `agent_assembly.py:170`（legacy dict 种子、`chat_engine.py:242` 的上游）**收编**——`"spica"` 回退改 import `scope.py` 的 `DEFAULT_CHARACTER_ID` 常量（单一居所）；`session.py:151-152`、`companion_controller.py:94-95` **显式豁免**（测试便利默认；生产由 `app_host.py:625-626,702-703` 显式传参覆盖）。
- **allowed files**: `spica/runtime/scope.py`（新：常量 + CharacterScope + MemoryScopeStrategy）、`spica/runtime/stages.py`（`:157` recent key、`:182-186` ltm scope 经 strategy）、`spica/runtime/memory_commit.py`（`:41,64-68`）、`spica/core/chat_engine.py`（`:241-250` clear/list/remember + recent key）、`spica/host/app_host.py`（14 处身份默认值机械替换 + `character_scope` property）、`spica/host/agent_assembly.py`（**仅 `:170` 一行**改 import 常量）、`tests/test_recent_memory_scope.py`（摘 xfail）、`tests/test_memory_commit_scope.py`（`:78-84`「recent append 保留 raw id」断言随行为变更改为 scoped——PR 说明）、**`tests/test_cancellation.py`、`tests/test_no_comment_gate.py`、`tests/test_proactive_turn.py`、`tests/test_streaming_pipeline.py`**（全部裸 `get_recent("default"/"c1")` 改读 scoped key；**负向断言 `assertEqual(recent, [])` 也必须改读正确桶，防空转误绿**）、**`spica/galgame/prompt_sections.py`**（2026-07-03 计划修订：Phase 1 已把 stages.py 的段落构建——含一处 `or "spica"` 身份回退——迁入本文件；**只允许**移除身份默认值回退、改由 `stages.py` 侧 resolve 已解析 scope 后作参数传入；**不得 import `spica.runtime.*`，不得破坏 Phase 1 钉死的 import 边界**）、**`tests/test_memory_commit.py`**（2026-07-03 计划修订：**只允许**把 recent append spy 的裸 key 期望（`:89`）更新为 scoped key）、新对称性测试、docs。
- **批准前置检查（爆炸半径 rg）**: `rg -n 'recent_memory\.get_recent\("(default|c1)"\)' tests`——全部命中文件必须已在上述 allowed files 内。**补充（2026-07-03 计划修订）**：`rg -n 'appends\[|append_turn' tests`——捕捉 **spy append 录制形态**的裸 key 断言（`get_recent` 扫描形态之外的漏网，实例：`test_memory_commit.py:89` 断言 `recent.appends[0][:3] == ("c1", ...)`）；全部含裸 key 断言的命中文件同样必须已在 allowed files 内。
- **forbidden files**: `memory/recent.py`（保持哑存储——key 推导全在 strategy）、`adapters/memory/sqlite.py`（其 `recent` 构造参数为休眠字段，禁启用）、`context.py`、`orchestrator.py`、`tool_round.py`、`deps.py`。
- **behavior change allowed?**: **是，且是本计划唯一的主动行为变更**（scoped recent key + clear 对称化）。单角色运行观感不变（同会话读写同 key；进程重启本就清空 recent）。
- **compatibility facade**: 全部公开签名不变。
- **characterization tests to add or update**: 摘 xfail（红转绿）；`test_memory_commit_scope` 预期更新；4 个白名单测试文件断言改 scoped；新增对称性测试；新增 rename 特征测试（`set_interlocutor_name` 后 `ltm_scope().user_id` 跟随——钉 live 语义）。
- **required test gate**: `python -m pytest tests -q` 全量绿，重点关注 memory 五族、`test_recent_memory_scope`（红转绿）、白名单内 4 个测试文件、golden_sync / turn_contract（字节不变）。
- **rollback**: 单 commit（或线性小序列）revert。
- **exit conditions**:
  1. `rg -n 'or "spica"|or "麦"' spica` 生产代码仅剩 `scope.py` 常量定义处 1 处命中（2026-07-03 计划修订注记：Phase 1 已将 `or "spica"` 的一处从 `stages.py` 迁入 `spica/galgame/prompt_sections.py:183`，Phase 2 **必须一并清掉**——否则本条必然不达标；清法限定见 allowed files 对该文件的说明）；
  2. **补充 rg**：`rg -n 'character_id[^=]*=\s*"spica"|user_id[^=]*=\s*"麦"' spica --type py` 预期命中 ledger = `scope.py`（常量单一居所）+ `session.py:151-152` + `companion_controller.py:94-95`（豁免注记），零计划外命中（注：`ui/qt_overlay.py:482,959` 的 `or "spica"` 是显示层 speaker 默认，在 `spica/` 之外，永久豁免）；
  3. xfail 转绿 + 对称性测试常驻 + rename 特征测试常驻。
- **unlocks / blocks**: 解锁第二角色数据层安全接入、Phase 4 更干净的搬迁面；不完成禁止发布任何多角色功能与 PersonaRuntime。

### Phase 3 — PromptContextContributor seam

- **approval status**: not approved
- **objective**: domain 上下文注入从「改三个高危文件」变「新文件 + 注册」。
- **why this phase exists**: Phase 1 已把 sections 模块化；co-watch（domain #2）批准前 seam 必须就位。
- **已裁决事项（施工前提）**:
  - **注册机制**: 方案 a·galgame 兼容限定版——`TurnDeps.context_contributors: tuple | None = None`；`__post_init__` 为 `None` 时补 `(galgame_contributor,)`（函数级懒 import；**兼容垫片，永不长第二项**）；显式 `()` = 关闭；**未来 domain 经 assembly 显式注册完整 tuple**。不按 `game_memory` 条件注册；缺失由 `sections()` 空转。
  - **telemetry**: 单 contributor 时代 span 名保持 `retrieve_game_context_node`（`tests/test_retrieve_game_context_node.py:104,118,149` 三处 timing 断言不改自绿）。
  - **node 检查顺序契约**: `ctx.error` 判断在 span 外（同今日 `stages.py:538`）；逐 contributor 问 `mode`（纯 request 逻辑，span 外）；全 "none" 或 error → 字节级 no-op 不开 span；任一非 none → 开 span（旧名）→ span 内查 `ctx.prompt is None`（同今日 `:545` 语义位置）→ 逐 contributor `sections()`。此设计使「active + game_memory/prompt 为 None → 开 span」的今日 timing 逐字节保持（Phase 0 golden #2 (d) 钉住），无需声明行为变化。
- **allowed files**: `spica/runtime/prompt_context.py`（新：Protocol）、`spica/runtime/stages.py`（node 通用化 + `retrieve_game_context_node = contribute_context_node` 别名）、`spica/galgame/context_contributor.py`（新：包 `prompt_sections` + 迁入 `_game_context_mode` / `_resolve_game_target` / `_parse_*`）、`spica/runtime/deps.py`（字段 + `__post_init__`）、`orchestrator.py` / `sync_chain.py`（各 1 行换名）、**`tests/test_layering.py`**（`TRANSFORM_LAYER_FILES` 增 `context_contributor.py`——只扩域）、**`tests/test_prompt_context_contributors.py`**（新）、**`CLAUDE.md`**（§2 表加 contributor 行；§3/§4 中「gated stage / retrieve_game_context_node 注入」表述改向「PromptContextContributor（`retrieve_game_context_node` 别名保留）」；提交时仍需排除 Agent skills 既有 WIP hunk）、**`docs/DEVELOPMENT_GUARDRAILS.md`**（**仅** §5 落点决策树「写 gated stage（仿 retrieve_game_context_node）」改为「写 contributor（domain 内新文件 + deps/assembly 注册）」+ §9 第 4 条同向更新）——（2026-07-03 计划修订：原文「docs（CLAUDE 决策树改『写 contributor』）」未逐文件点名；决策树物理居所在 GUARDRAILS §5，按 D2「文档同 commit 改向」意图两件并列明确）。
- **forbidden files**: `context.py`、`chat_engine.py`、`app_host.py`、`prompt_builder.py`、**`tests/test_retrieve_game_context_node.py`、`tests/test_current_line_injection.py`、`tests/test_reaction_wiring.py`（三个直调测试文件零改动全绿是收口硬 gate）**。
- **behavior change allowed?**: 否（含 timing 角例，设计上逐字节保持）。
- **compatibility facade**: `retrieve_game_context_node` 别名永久保留（import + 直调方全覆盖）；两处故意重复的 `galgame::` 字面量保持不去重（gate 语义不变性由 Phase 0 golden + 别名承接）。
- **characterization tests to add or update**: `tests/test_prompt_context_contributors.py`（新）——auto-fill 语义（None → 恰为 galgame contributor 一项）、显式 `()` 关闭、显式 tuple 原样尊重、span 名钉死、D2 守卫（别名必须是纯赋值 + node 源码行数上限，见 Dual-Track D2）。
- **required test gate**: `python -m pytest tests -q` 全量绿，重点关注 Phase 0 golden #2（含 (d)）、三个直调测试文件（零改动）、`test_game_context_in_chain`、turn_contract、新 contributor 测试。
- **rollback**: 单 commit revert。
- **exit conditions**: `orchestrator.py` / `sync_chain.py` 调新名；三个直调测试文件零改动全绿；N1 扫描含两个新文件；CLAUDE 决策树同 commit 改向。
- **unlocks / blocks**: 解锁 co-watch/browser 的上下文注入 = domain 内新文件 + assembly 注册；不完成禁止任何第二 domain 的 prompt 注入实现。**域 #2 预留声明**：本 phase 不解决 TurnRequest 的第二域落点；`context.py` / `chat_engine.py._request` 的受控改动归 Phase 8。

### Phase 4R — registry ToolEntry NamedTuple（独立微 phase）

- **approval status**: not approved（可在 1/2/3 之间任意穿插排期）
- **objective**: `spica/plugins/registry.py` 内部 7 元组（`:54-65`）→ `ToolEntry` NamedTuple；全部读取器 API（`tool_schemas` / `tool_handler` / `tool_intent_gated` / `tool_chainable` / `tool_compact_output` / `tool_effect` / `list_adapters`）签名与行为不变。
- **why this phase exists**: 从 v1 Phase 4 拆出——registry 是高危文件，不与 host assembly / policy 下沉混装风险；且 Phase 0 #1 的公共接口纪律恰好是它的回归 gate。
- **allowed files**: `spica/plugins/registry.py`、`tests/test_registry.py`（只加断言）。
- **forbidden files**: 其余一切。
- **behavior change allowed?**: 否。
- **compatibility facade**: 读取器 API 即 facade；内部表示自由。
- **characterization tests to add or update**: `test_registry` 增量断言（可选）。
- **required test gate**: `python -m pytest tests -q` 全量绿，重点关注 `test_registry`、Phase 0 #1（**零改动自绿**——公共接口纪律的直接验证）、工具族。
- **rollback**: 单 commit revert。
- **exit conditions**: 元组索引访问在 registry.py 内消失；对外 API 零变化。
- **unlocks / blocks**: 解锁 Phase 4（消除元组/NamedTuple 双索引窗口）。

### Phase 4 — ReactionScoringPolicy + reaction assembly

- **approval status**: not approved
- **objective**: `app_host.py:400-479` 的 judge 调用/冷却状态/lexicon mtime 缓存/降级逻辑下沉 `spica/galgame/reaction_scoring.py`；reaction 接线出走 `spica/host/assemblies/reaction.py`。AppHost 停止随 domain 增长；policy 与 authority 分层立范。**不含 registry 改动**（已拆 4R）。
- **why this phase exists**: 「新 domain 装配 ≤15 行/domain」预算的立范之作；Phase 2 已把要搬的代码收敛干净。
- **allowed files**: `spica/galgame/reaction_scoring.py`（新 policy）、`spica/host/assemblies/__init__.py` + `reaction.py`（新）、`spica/host/app_host.py`（删搬空逻辑、加 install 调用、留薄委托）、`tests/test_reaction_judge.py`（**先改后搬**：`:232` 的 `patch.object(app_host_module.time, ...)` 改指 policy 的注入 clock；`:189` 的 `app_host_module._LEXICON_FALLBACK_PASS_SCORE` 引用随常量迁移改 import；**DegradeFallbackTest 对 `host._reaction_lexicon_for` 的 deterministic lexicon patch 迁移到 policy 级 `lexicon_for` seam**——2026-07-03 计划修订：委托形态下 host 级属性覆盖会静默失效；**新增 patch 有效性用例**）、**`tests/test_reaction_config.py`**（2026-07-03 计划修订：**仅限**把 lexicon 热重载/缓存两用例从 `AppHost.__new__` + host cache 直驱迁移为直驱 `ReactionScoringPolicy`——断言值、mtime monkeypatch 语义、reload 计数语义逐条保持；reload 计数的 monkeypatch 点随 import 迁移改为 `spica.galgame.reaction_scoring.load_reaction_lexicon`）、**`docs/DEVELOPMENT_GUARDRAILS.md`**（2026-07-03 计划修订，逐处明确：①新增「新 domain 装配模板」小节；②§3.1 `app_host.py` 行加「Phase 4 后禁新增 per-domain 方法，装配走 assemblies」注记——D4 防再生长规则）。
- **forbidden files**: `reaction.py` 引擎本体（scorer seam 签名 `(beat)->ScoreResult` 不动）、写闭包（`_request_song` / `_record_game_observation` / `_record_play_history` / beat writer 留 host）、`session.py`、`registry.py`。
- **behavior change allowed?**: 否（judge 冷却/降级语义逐断言保持）。
- **compatibility facade**: 被搬方法名（`_new_reaction_judge` / `_build_reaction_engine` / `_judge_llm_adapter` 等）留薄委托一个 phase（Phase 5-c2 删），且 **`assemblies.reaction.install()` 必须经这些委托方法构建**——委托必须仍是唯一构建路径，不只是「存在」（防 `tests/test_moondream_default_cutover.py:108-109` 的 patch 静默变 no-op）。
- **characterization tests to add or update**: `test_reaction_judge.py` 注入 clock 改造 + **patch 有效性用例**：`patch.object(AppHost, "_new_reaction_judge", return_value=<sentinel>)` 下走 install 路径，断言 sentinel 真实到达 judge 持有位（常驻，防保护面静默收窄）。
- **required test gate**: `python -m pytest tests -q` 全量绿，重点关注 reaction 五族（judge/config/wiring/no_comment/proactive）、`test_moondream_default_cutover`、Phase 0 #1。
- **rollback**: 单 commit revert。
- **exit conditions**: ① reaction 接线全部出 `app_host.py`，仅剩 `assemblies.reaction.install(self)` 一行级调用；② judge 冷却/降级语义逐断言保持；③ patch 有效性用例常驻且绿（2026-07-03 计划修订，**三条 facade 路径全部以 sentinel/patch 钉死**：`assemblies.reaction.install(host)` 必须经 `host._new_reaction_judge()` 与 `host._build_reaction_engine()` 构建；`assemblies.reaction.new_reaction_judge(host)` 必须经 `host._judge_llm_adapter()` 取 adapter——防「facade 存在但不在构建路径上」的 no-op patch）。
- **unlocks / blocks**: 后续任何 domain 按 assemblies 模板接入；不完成禁止 co-watch/browser 的 host 接线。

### Phase 5 — deps 单轨化（stages/memory_commit 禁区版）

- **approval status**: not approved
- **objective**: stages 与 memory_commit 只读 deps；删 Phase 4 薄委托；守卫扩容。
- **why this phase exists**: 结清 services/deps 双轨债的可结清半（前车之鉴：该双轨自 C4 悬置至今，`spica/runtime/services.py:9-10`）；为 Phase 6/7 的 `deps.model` 备好干净落点。
- **commit 结构（顺序不可倒置，各自可独立 revert）**:
  - **5-c0（测试先行）**: 新增 `LLM_CLIENT_NOT_CONFIGURED` 特征测试（`services.llm_client=None` 经同步链 → payload error code 钉死；该错误路径此前零测试覆盖）。
  - **5-c1（flip）**: `deps.py` 加三字段——`recent`、`llm_ready: bool = True`（bridge 内由 `services.llm_client is not None` 计算；**禁止**把 `stages.py:577` 机械改 `deps.llm is None`——`from_services` 的 `or OpenAICompatibleAdapter(services.llm_client)` 包装（`deps.py:73`）使 `deps.llm` 永非 None）、`available_tool_schema_count: int = 0`（bridge 时从 `len(services.tool_schemas)` 灌入——**保值方案，已拍板**，值逐字节不变）；`stages.py`（`:157` recent、`:577` 改 `if not deps.llm_ready:`、`:603` 改 deps 字段、`:809` / `:849-866` 及 `:104-105` helper → `deps.visual` / `deps.tts`）；`memory_commit.py:41` → `deps.recent`；`orchestrator.py:264-265` → `deps.visual`；`tool_round.py:61` → deps 字段。
  - **5-c2（守卫 + 删委托）**: `test_no_dict_config` BANNED_ATTRS 增补至 `{config, llm_adapter, memory_adapter, tts_adapter, visual_tool, recent_memory, llm_client, tool_schemas}`，**同 commit** ALLOWLIST 扩为 `{deps.py, visual_job.py, tts_job.py}`（后两者注明「D1 登记的永久 facade 载体」——这是净收紧的预先声明，不是放宽守卫）；删 Phase 4 薄委托；`tests/test_moondream_default_cutover.py` patch 目标迁到 assemblies 安装函数。
- **显式不迁声明**: `tool_round.py:36-37`（`services.llm_client` 判空）**归 Phase 7-c2**（写进其退出条件）；`orchestrator.py:121` / `sync_chain.py:43` 的 `services.logger` 为 observer 注入链既有参数，不迁、记入 D1 备注、禁扩散。
- **allowed files**: `spica/runtime/deps.py`、`spica/runtime/stages.py`、`spica/runtime/memory_commit.py`、`spica/runtime/orchestrator.py`、`spica/runtime/tool_round.py`（仅 `:61`）、`spica/host/app_host.py`（仅删委托）、`tests/test_no_dict_config.py`、`tests/test_moondream_default_cutover.py`、新特征测试文件。
- **forbidden files**: `services.py`（字段保留）、stage 签名（第三参 `services` 留惰性参数，守卫禁读）、`sync_chain.py` 行为。
- **behavior change allowed?**: 否（`llm_ready` / schema-count 均逐字节保值；5-c0 先钉死错误路径）。
- **compatibility facade**: `services` 作 unit-job 参数载体永久保留（visual_job/tts_job）；stage 第三参保位。
- **characterization tests to add or update**: 5-c0 特征测试（新）；`test_no_dict_config` 加强；cutover 测试 patch 目标迁移。
- **required test gate**: `python -m pytest tests -q` 全量绿，重点关注 golden_streaming / golden_sync、turn_contract、5-c0 新测试、`test_no_dict_config`（加强后）、`test_moondream_default_cutover`。
- **rollback**: 按 5-c2 → 5-c1 → 5-c0 逆序 revert，各自干净。
- **exit conditions**: 禁区（stages/memory_commit）内 `services.` 属性读仅剩签名与注释；加强版守卫绿；委托删除且 cutover patch 目标迁移完成。
- **unlocks / blocks**: 解锁 Phase 6a（`deps.model` 干净落点）；不完成禁止 Phase 6a。

### Phase 6a — TextModel + BoundModel + summarizer/judge 收编

- **approval status**: not approved
- **objective**: turn 外 LLM 消费者（summarizer/judge）脱离 v1 Protocol；**不引入 ModelRouter**。
- **why this phase exists**: 叶子先迁——summarizer/judge 是 v1 依赖面最小的消费者（各一处 `complete_text` 调用，`summarizer.py` / `reaction_judge.py`），先迁验证 v2 契约套件，再动生产链。
- **allowed files**: `spica/ports/model.py`（新：TextModel + BoundModel）、`spica/adapters/llm/openai_compatible.py`（**只增** v2 方法，内部复用 v1 路径——修 bug 单点）、`spica/galgame/summarizer.py` + `reaction_judge.py`（构造签名 `(llm, model)` → `(bound: BoundModel)`）、`spica/host/app_host.py`（`_new_summarizer` / `_new_reaction_judge` 内**手工组 BoundModel**，方法名保留）、`tests/test_galgame_summarizer.py` / `tests/test_reaction_judge.py`（mock 形状随签名变，先红后绿）、新 v2 契约测试套件（参数化 over adapters——为未来第二 provider 复用）、**新弱守卫 `tests/test_no_new_v1_llm_consumers.py`**（AST：`spica/galgame/**` + `spica/host/**` 禁新增 `LLMPort` / `complete_text` / 探针族引用，存量文件按白名单冻结——D3 止血阀，**同 commit 落地**）、docs。
- **forbidden files**: `tool_round.py`、`orchestrator.py`、`stages.py`、`ports/llm.py`、`deps.py`。
- **behavior change allowed?**: 否。
- **compatibility facade**: LLMPort v1 全保留；adapter 双实现；`_new_*` 方法名保留（cutover 测试 patch 目标）。
- **characterization tests to add or update**: v2 契约套件（新）；summarizer/judge 测试预期更新；弱守卫（新）。
- **required test gate**: `python -m pytest tests -q` 全量绿，重点关注 summarizer/judge/reaction 族、v2 契约套件、弱守卫、cutover 测试。
- **rollback**: 单 commit 链独立 revert。
- **exit conditions**: summarizer/judge 只依赖 BoundModel；弱守卫在位；D3 时钟自本 phase 收口起跳。
- **unlocks / blocks**: 解锁 judge/summary 换任意 provider、Phase 6b、Phase 7；不完成禁止 Phase 7 与任何非 OpenAI adapter 动工。

### Phase 6b — ModelRouter 收编 host endpoint 决策

- **approval status**: not approved（可与 Phase 7 对调，不占 D3 时钟）
- **objective**: `app_host.py:556-598` 三处 endpoint/model fallback（summary_model 回退、judge model/key/base_url 回退树、judge 独立 endpoint 构建）收进 `spica/host/model_router.py::for_role("dialogue"|"judge"|"summary") -> BoundModel`。
- **why this phase exists**: endpoint 决策唯一居所；与 6a 拆开使任一半可独立回滚。
- **allowed files**: `spica/host/model_router.py`（新）、`spica/host/app_host.py`（`_new_summarizer` / `_new_reaction_judge` / `_judge_llm_adapter` 内部改调 router，**方法名保留**）、router 单测（新）、docs。
- **forbidden files**: `ports/model.py`、adapter、summarizer/judge、runtime 全域。
- **behavior change allowed?**: 否（fallback 树逐断言保持：无 JUDGE_API_KEY 共享主 adapter、base_url 独立回退等）。
- **compatibility facade**: `_new_*` 方法名保留。
- **characterization tests to add or update**: router 单测（新）；`test_reaction_judge` host-wiring 族预期不变。
- **required test gate**: `python -m pytest tests -q` 全量绿，重点关注 `test_reaction_judge` host-wiring 族、cutover 测试、router 单测。
- **rollback**: 独立 revert，不牵连 6a。
- **exit conditions**: 三处 endpoint 决策唯一居所（`rg -n 'reaction_judge_base_url|judge_api_key' spica/host/app_host.py` 收敛进 router 调用）。
- **unlocks / blocks**: 无硬阻塞下游（Phase 7 不依赖它）。

### Phase 7 — ToolCallingModel 生产链 flip

- **approval status**: not approved
- **objective**: `prefers_chat_completions` 与 v1 探针方法退出 runtime；provider #2 硬爆点拆除；`services.llm_client` 判空随之消亡。
- **why this phase exists**: 「非 OpenAI provider = 只写一个 v2 adapter」的最后一刀；D3 时钟约束（6a 后 ≤2 个已批生产 phase）驱动排期。
- **commit 结构（各自可独立 revert）**:
  - **7-c0（特征测试先行，v1 下全绿）**: ① mid-stream error（fake client 吐 2 个 delta 后 raise → error 事件、无 done——现有 raising fake 只在 create 即抛，中途异常无判据）；② followup cancel（chat 工具路 `STREAM_RESET` 之后、followup 流中 set cancel → 工具恰执行一次、无 ghost memory、流停——`tool_round.py:145-146` 检查点专测）；③ STREAM_RESET 语义显式断言（preamble 不进最终 answer/memory）。
  - **7-c1**: `orchestrator.py` `iter_response_text` → `deps.model.stream`；`deps.py` 加 `model` 字段（`llm` 保留给冻结链）。
  - **7-c2**: `tool_round.py` probe 族 → `ToolCallingModel.probe` / `probe_stream`；**`tool_round.py:36-37` 的 `services.llm_client` 判空改 `deps.llm_ready`（错误文案字节不变——Phase 5 遗留归属在此结清）**；`ports/model.py` 补 `ToolProbeResult` / `ToolProbeStream` / `ProviderTraits`；新守卫 `tests/test_no_v1_llm_in_runtime.py`（AST：`orchestrator.py` / `tool_round.py` 禁 `prefers_chat_completions` / `iter_response_text` / `create_chat_with_tools` / `iter_chat_with_tools` **及 `traits` / `provider_traits` 属性读**；`stages.py` 冻结区豁免显式注明）；6a 弱守卫升级为完整版。
- **ToolProbeStream 契约（已裁决，写进 port docstring + 专测）**: `.deltas` 正常耗尽后 `.calls` 方可读；cancel 提前弃读或中途异常 → `.calls` 未定义，调用方不得读；probe 中途 cancel → 不产生 STREAM_RESET、不执行工具。
- **十项测试 gate 对号表**:

  | gate | 承接测试 |
  |---|---|
  | streaming probe（chat 路带工具形状） | `test_chat_tool_round`（既有，client 级） |
  | non-streaming probe（Responses 形状） | Phase 0 #3a |
  | forced final | `test_tool_chain_rounds`（既有） |
  | tool overflow（优雅收尾） | `test_tool_chain_rounds`（既有） |
  | NO_COMMENT | `test_no_comment_gate`（既有） |
  | STREAM_RESET | 7-c0 ③ |
  | mid-stream error | 7-c0 ① |
  | followup cancel | 7-c0 ② |
  | cancellation before/after tool call | `test_cancellation`（既有，checkpoint ①）+ 7-c0 ② |
  | ToolProbeStream cancel / `.calls` 契约 | 7-c2 专测 |

- **allowed files**: `spica/ports/model.py`、`spica/adapters/llm/openai_compatible.py`、`spica/runtime/deps.py`、`spica/runtime/tool_round.py`、`spica/runtime/orchestrator.py`、7-c0 / 7-c2 新测试文件、`tests/test_no_new_v1_llm_consumers.py`（升级）、docs（CLAUDE §2 模型层条目改向）。
- **forbidden files**: `stages.py` 的 `call_llm_node`（冻结链永久 v1）、`sync_chain.py`、golden 断言。
- **behavior change allowed?**: 否（十项 gate + turn_contract 7 形态 + Phase 0 #3 client 级形状钉死——client 级 fake 穿越 flip 仍有效，这正是 Phase 0 #3 钉 client 层的原因）。
- **compatibility facade**: v1 Protocol / adapter 方法永久保留（博物馆租金）。
- **characterization tests to add or update**: 7-c0 三件（新，v1 下先绿）；7-c2 契约专测与 AST 守卫（新）。
- **required test gate**: `python -m pytest tests -q` 全量绿，重点关注对号表十项、`test_chat_tool_round`、`test_tool_chain_rounds`、新守卫。
- **rollback**: 按 7-c2 → 7-c1 → 7-c0 逆序 revert，各自干净。
- **exit conditions**: `orchestrator` / `tool_round` 零 v1 引用、零 `services.llm_client` 引用、零 traits 读（AST 守卫全绿）；Phase 0 #3 零修改保持绿。
- **unlocks / blocks**: 解锁「非 OpenAI provider = 只写一个 v2 adapter」（承诺闭环）；不完成禁止 Anthropic/local adapter 立项。

### Phase 8 —【feature-triggered：co-watch 批准】ActiveDomainRouter + WindowTarget/PrivacyGate + request 落点泛化

- **approval status**: feature-triggered（不排日期）
- **objective**: domain #2 的 turn-binding 碰撞点、多窗口安全不变量、以及**域 #2 的 request 落点**在 co-watch 动工前就位。
- **why this phase exists**: ChatEngine 单槽 provider 与 TurnRequest 的 galgame 专用槽（`context.py:105` `game_context_request`；`chat_engine.py:82` double-wrap guard 只认 GALGAME 前缀；`chat_engine.py:176` 对 `source` 的 del 使 system turn 无域标识）在第二 domain 到来时全部撞墙——必须有 phase 拥有这刀。
- **allowed files**: `spica/host/domain_router.py`（新）、`spica/host/app_host.py`（`_companion_game_binding` 改经 router，≤10 行）、`spica/galgame/companion_controller.py`（publish/retract 改向 router，纪律不变）、`spica/runtime/window.py`（新：WindowTarget）+ `spica/galgame/privacy_gate.py`（新：吸收 `ocr_loop.py:149-173` / `session.py:88-100` 状态集引用 / `watch_game_screen.py:158-165` 三份拷贝）、`ocr_loop.py` / `watch_game_screen.py`（改调 gate）、`app_host.py` watch context 5 元组 → WindowTarget、**`spica/runtime/context.py` 与 `spica/core/chat_engine.py`（受控改动预留：泛化 `DomainContextRequest` 槽或域 conversation 前缀注册表；极高危文件，批准本 phase 时必须连同其设计文档一起评审——设计到 Phase 8 再做，已拍板）**。
- **forbidden files**: `chat_engine.py` 中与 binding 泛化无关的部分、`session.py` 的锁与 FSM。
- **behavior change allowed?**: 否（gate 判定逻辑等价迁移；若顺手收窄 check→capture race（P1）须单独 commit 并声明）。
- **compatibility facade**: ChatEngine provider 注入形状不变；galgame binding 语义不变。
- **characterization tests to add or update**: 新 gate 单测；router 单测；binding 泛化契约测试（随设计文档定）。
- **required test gate**: `python -m pytest tests -q` 全量绿，重点关注 galgame 全族、`test_watch_game_screen`、新 gate 单测。
- **rollback**: 线性小序列 revert。
- **exit conditions**: galgame 经 router 发布且全族绿；三处隐私拷贝删除；域 conversation 前缀纪律（「每个 domain 必须认领 conversation 前缀；system turn 要被 contributor 识别必须携带域 conversation_id」）写入 Decision Log 与 GUARDRAILS。
- **unlocks / blocks**: 解锁 co-watch domain 按预算落地（AppHost ≤15 行、runtime 0 行）；不完成禁止 co-watch 任何 turn-binding/截帧实现。

### Phase 9 —【feature-triggered：browser/media 批准】ToolAuthority 对象化 + ToolExecutionPolicy 激活

- **approval status**: feature-triggered（不排日期）
- **objective**: act 规模化的权限与策略基建，与首个 browser/media authority 同 phase 交付。
- **why this phase exists**: choke point（`RegistryToolSet.run`）已在；等首个真实 authority 需求一起立项避免 speculative 建设。
- **allowed files**: `spica/host/authorities/`（新包：首个 `BrowserAuthority` / `MediaAuthority`，URL 模板白名单 + 命令枚举）、`spica/runtime/tools.py`（`run` 加 policy check 约 10 行）、`registry.py`（无 API 变化）、新守卫（host 包外禁实例化 `*Authority`）、对应工具垫片与 assemblies 文件、config 新 typed 节。
- **forbidden files**: 既有 4 个闭包（可后续自愿改造）、`tool_round.py`。
- **behavior change allowed?**: 新能力增量；既有工具行为字节不变（policy 对 read/write 默认放行）。
- **compatibility facade**: 既有闭包不强制改造。
- **characterization tests to add or update**: act 纪律断言（仿 `test_sing_song_tool`）+ policy 测试 + authority 守卫。
- **required test gate**: `python -m pytest tests -q` 全量绿，重点关注工具族与新守卫。
- **rollback**: 线性小序列 revert。
- **exit conditions**: 首个 authority 类 + 守卫在位；首批 policy 有测试。
- **unlocks / blocks**: 全部目标能力就绪；不完成禁止任何绕 authority 的浏览器/播放器控制实现。

---

## Phase 0 Implementation Prompt

```text
你在 /home/san/ai_code/Spica-Chatbot 执行《Spica Long-Term OO Migration Plan v2》Phase 0
（docs/oo_migration/MIGRATION_PLAN.md）。
本 phase 只允许新增测试文件。禁止改动任何生产代码、任何既有测试；
禁止更新 docs/oo_migration/README.md 与 PROGRESS.md（文档收口在 Phase 0D，另行批准）。
先读 CLAUDE.md §1 铁律 + docs/DEVELOPMENT_GUARDRAILS.md §13，然后【先输出计划并等待确认】：
列出你将新增的 4 个测试文件、每个文件的断言清单、你不会碰的文件。确认后再动手。

新增（只此四件）：

1) tests/test_app_host_tool_registration.py
   构造 AppHost()（不调 initialize；先例：tests/test_watch_game_screen.py:349、
   tests/test_note_game_observation.py:309）。
   【硬性规定】只准使用 registry 公共接口：list_adapters / tool_schemas /
   tool_intent_gated / tool_effect / tool_compact_output / tool_handler。
   禁止访问 registry._tools 或任何下划线开头属性（Phase 4R 会把内部元组换 ToolEntry）。
   断言：
   a. "watch_game_screen" 与 "note_game_observation" 在 list_adapters("tool") 中，
      但【不在】tool_schemas() 的名字集合中（= available 谓词此刻为 False 的公共行为表达；
      名字解析需兼容 flat 与 nested 两种 schema 形状）；"inspect_screen" 与 "sing_song"
      【在】tool_schemas() 名字集合中（用成员断言，不用全集相等断言）。
   b. tool_intent_gated("watch_game_screen") is False、("note_game_observation") is False、
      ("sing_song") is True（缺口补齐）。
   c. tool_effect：watch=read、note=write、sing_song=act、inspect_screen=read
      （与 tests/test_sing_song_tool.py:246-250 有意重复，作集中背书，注释说明）。
   d. tool_compact_output("inspect_screen") is not None（缺口补齐）；
      tool_handler("watch_game_screen") is not None。
   不做 initialize 后 / 陪玩态供给验证——tests/test_watch_game_screen.py:349-362 已覆盖。

2) tests/test_game_prompt_golden.py
   用真实 GameMemorySqliteAdapter（tmp 路径）【直写模型对象】喂满
   progress / summaries / buffer(committed 未总结行) / current-line(pending) /
   relations / choices / beats。
   【硬性规定】禁止使用 ManualGameMemory（spica/galgame/manual.py:106,120,142,161,181
   自动打 utc_now_iso()，会把 wall-clock 渲染进 [GAME_PROGRESS].last_played_at 与
   [RECENT_GAME_SUMMARIES].created_at）；全部 timestamp / created_at / updated_at /
   last_played_at 显式传固定值且彼此错开（钉排序；先例：
   tests/test_retrieve_game_context_node.py:296-301）。
   构造带 game_memory 的 TurnDeps（observer=DefaultTurnObserver(ctx.timing)），
   直调 retrieve_game_context_node：
   a. active 态（interaction_mode="galgame" 或 gcr.mode="active"，带 session_id 使
      [CURRENT_LINE] 出现）golden 整段注入后 prompt 文本（测试内嵌常量）；
   b. offline 态（gcr.mode="offline" + 显式 game_id）golden 整段（注：offline 无
      companion intent 时 [COMPANION_CONTEXT] 缺席是预期）；
   c. 同一输入连调两次，输出必须逐字节相同（防非确定性）；
   d. 现状 characterization：active 态 + deps.game_memory=None 时，
      ctx.timing 含 "retrieve_game_context_node_ms" 而 prompt 不变（钉住 Phase 3 的
      span 语义基线）。

3) tests/test_responses_probe_shape.py
   【录制层级硬性规定】fake 打在 OpenAI client 层（client.responses.create 记录 kwargs，
   仿 tests/test_turn_contract.py:61-80 形制）；禁止 fake LLMPort 层
   （port 级 fake 会在 Phase 7 flip 时判据自毁）。
   a. Responses 工具 probe shape：用 prepare_prompt_for_streaming(ctx, services,
      put_status, deps) + 真实 OpenAICompatibleAdapter 驱动（不拖 orchestrator/TTS/visual）。
      断言：probe 的 create 收到的 tools 载荷与 registry 提供的 schema 逐字节相同、
      轮次记账（agent_rounds/agent_response_initial_ms mark 存在）、
      单发工具后 prepare 的返回 prompt 含 "[TOOL_RESULTS]"。
   b. 无工具 final request 无 tools 键：走 adapter 级——调
      OpenAICompatibleAdapter.iter_response_text 后，断言 client.responses.create
      收到的 kwargs 无 "tools" 键（且含 stream=True）。
      OpenAICompatibleAdapter.iter_response_text 的调用以当前真实方法签名为准；
      测试目标是 client.responses.create kwargs 不含 tools。
      不要让 prepare_prompt_for_streaming 承担这条它测不到的断言
      （无工具时 prepare 直接返回，不发请求；该请求由 orchestrator 发出）。

4) tests/test_recent_memory_scope.py
   (a) 现状 characterization：不同 conversation_id 的 recent 互不可见（应通过）。
   (b) @pytest.mark.xfail(strict=True, reason="recent key 未按 character 命名空间隔离；
       由 Phase 2 的 MemoryScopeStrategy 转绿")：
       写路径【必须】经 save_stream_memory（生产写点；deps 用角色 A 的 config 新构造，
       ctx.answer 置非空使 append 真实发生）；
       读路径【必须】经 load_recent_context_node（deps 用角色 B 的 config【重新构造】——
       不准直接写 recent deque，不准原地突变 config 后复用旧 deps；重构 deps 使本测试
       对 Phase 2 的 frozen/live 两种实现都成立）；
       断言角色 B 读同一 conversation_id 为空（今日必失败 → xfail）。

gate：python -m pytest tests -q 全量绿（xfail 计 xfailed 不计 failed）；
     tests/test_game_prompt_golden.py 单独连续跑两遍结果一致。
禁止：任何 spica/**、memory/**、agent_tools/**、ui/**、docs/** 改动；
     任何"顺手"的生产代码修复——xfail 暴露的缺陷由 Phase 2 修，不是本 phase。
收尾报告：新增文件清单、全量真实输出、golden 样本生成方式说明。
（README 状态板 / PROGRESS 收口日志由 Phase 0D 单独回写。）
```

---

## Dual-Track Governance

### D1 — stage/流程读 `services.*` vs `deps.*`

- **旧 seam**: stage/流程读 `services.*`（stages `:157,577,603,809,849-866`、memory_commit `:41`、orchestrator `:264-265`、tool_round `:61`）。
- **新 seam**: `deps.*`（含 `llm_ready` / `available_tool_schema_count` / `recent`）。
- **最长并存**: 至 Phase 5 收口；**`tool_round.py:36-37`（`llm_client` 判空）显式归 Phase 7-c2 结清**。
- **删除/封死条件**: stages/memory_commit 禁区零引用；Phase 7 后 orchestrator/tool_round 零 `llm_client`。
- **防再生长守卫或规则**: `test_no_dict_config` BANNED_ATTRS = `{config, llm_adapter, memory_adapter, tts_adapter, visual_tool, recent_memory, llm_client, tool_schemas}`；ALLOWLIST = `{deps.py, visual_job.py, tts_job.py}`（后两者 = 永久 facade 载体，禁新增读者）；`services.logger`（orchestrator `:121` / sync_chain `:43`）为 observer 注入链既有参数，不迁移、禁扩散。

### D2 — `retrieve_game_context_node` 直连 galgame vs contributor 注册

- **旧 seam**: `retrieve_game_context_node` 直连 galgame。
- **新 seam**: contributor 注册（deps `__post_init__` galgame 兼容 auto-fill）。
- **最长并存**: 别名**永久**（成本≈0）。
- **删除/封死条件**: 不删。
- **防再生长守卫或规则**: AST 守卫（`tests/test_prompt_context_contributors.py`）：① 别名必须是纯赋值 `retrieve_game_context_node = contribute_context_node`（禁重新 def）；② `contribute_context_node` 源码行数上限（钉当前实现 + 小余量）——新增 domain 逻辑必然超限；③ span 名钉 `retrieve_game_context_node`；④ auto-fill 恰为 galgame 一项，未来 domain 必须 assembly 显式注册。CLAUDE 决策树改「写 contributor」（Phase 3 同 commit）。

### D3 — LLMPort v1 vs TextModel / ToolCallingModel

- **旧 seam**: LLMPort v1（探针族 + `prefers_chat_completions`）。
- **新 seam**: TextModel / ToolCallingModel。
- **最长并存**: **6a 收口起算 ≤2 个已批生产 phase 内必须完成 Phase 7**（6b 可与 7 对调，不占时钟）——已拍板。
- **删除/封死条件**: `orchestrator` / `tool_round` 零 v1 引用 + 零 traits 读。
- **防再生长守卫或规则**: 6a 同 commit 落弱守卫 `test_no_new_v1_llm_consumers`（galgame/host 禁新增 v1 消费者，存量文件白名单冻结）；Phase 7-c2 升级为完整 AST 守卫（v1 方法族 + `traits` / `provider_traits` 属性读全禁；冻结区豁免显式注明）。

### D4 — AppHost 内 reaction 方法 vs assemblies + policy

- **旧 seam**: AppHost 内 reaction 方法。
- **新 seam**: assemblies + policy。
- **最长并存**: 薄委托 ≤1 phase（4 → 5-c2 删除）。
- **删除/封死条件**: 委托删除 + cutover patch 目标迁移（已入 Phase 5 白名单）。
- **防再生长守卫或规则**: Phase 4 退出条件含 **patch 有效性验证**（sentinel 注入必达构建路径，防 no-op 化）；Phase 4 后 `app_host.py` 禁新增 per-domain 方法（GUARDRAILS §3.1 更新 + review checklist）。

### D5 — 裸 recent key vs strategy scoped key

- **旧 seam**: 裸 recent key。
- **新 seam**: strategy scoped key。
- **最长并存**: **零并存**（Phase 2 一次 flip 三个生产调用点 + 6 个测试文件）。
- **删除/封死条件**: xfail 转绿 + `test_memory_commit_scope` 更新 + 4 个白名单测试文件改读 scoped。
- **防再生长守卫或规则**: 对称性测试常驻；`memory/recent.py` 保持哑存储禁长 key 逻辑；`SqliteMemoryAdapter.recent` 为休眠参数，禁经 memory adapter 触碰 recent；`dump()` 零消费者，保持原样。

### D6 — AppHost 单槽 binding provider vs ActiveDomainRouter

- **旧 seam**: AppHost 单槽 binding provider。
- **新 seam**: ActiveDomainRouter。
- **最长并存**: Phase 8 一次切换。
- **删除/封死条件**: galgame 经 router 发布且全族绿。
- **防再生长守卫或规则**: `chat_engine.set_game_binding_provider` 禁二次注入（router 是唯一注入者）；域 #2 的 request 落点只能经 Phase 8 白名单内的 `context.py` / `chat_engine.py` 受控改动，禁把非 galgame 上下文塞进 `GameContextRequest` 类型。

### Museum — `sync_chain.py` + `call_llm_node` v1（冻结博物馆，非双轨）

- **定位**: **不是双轨，是冻结博物馆**；无新侧，永不迁移。
- **规则**: **只准冻结，不准长新能力**；LLMPort v1 方法族 + adapter v1 实现为其永久供给面（博物馆租金，Decision Log 记账）；既有 F8 冻结注释在位。

---

## Risk Register

| 级 | 风险 | 触发条件 | 检测方式 | 应对 | 责任 phase | v2 状态 |
|---|---|---|---|---|---|---|
| P0 | 守卫静默失守（拆 stages 后 N1 只扫旧文件 / patch 打空命名空间 / patch 变 no-op） | Phase 1/3/4 搬迁走样 | `test_layering` 扫描域核对 + patch 点清单复核 + Phase 4 patch 有效性用例 | 白名单强制含 `test_layering`（Phase 1 与 Phase 3 均已列入）；`analyze_*` 一带列禁改；委托必须仍是唯一构建路径 | 1、3、4 | 设计已消解（白名单与退出条件已补），待实施验证 |
| P0 | authority 随 policy 一起下沉出 host 包 | Phase 4/9 实施走样 | code review + 守卫（host 外禁 `*Authority` 实例化） | installer/authority 只落 `spica/host/`；写闭包列禁改清单 | 4、9 | 仍在场 |
| P0 | 模型 flip 打碎探针请求形状 | Phase 7 rewiring | Phase 0 #3（client 级，穿越 flip 有效）+ `test_chat_tool_round` 形状断言 | 逐 commit 独立 revert，任一红即回 | 7 | 设计已消解（录制层级已钉 client 层），待实施验证 |
| P1 | Phase 0 golden 时间戳 flaky | 实现窗口使用 ManualGameMemory 或未钉时间戳 | golden 连跑两遍一致性 gate | 提示词硬性禁用 ManualGameMemory + 固定错开时间戳 | 0 | **已修（v2 提示词硬规定）** |
| P1 | Phase 2 白名单缺失必改测试 → 回滚条款必然触发 | scoped key flip | 批准前置爆炸半径 rg | 4 个测试文件已入白名单；负向断言防空转条款 | 2 | **已修（白名单已补全）** |
| P1 | Phase 3 直构 TurnDeps 测试大面积红 / span 名漂移 | contributor 注册机制只挂 from_services | 三个直调测试文件零改动硬 gate + span 名断言 | `__post_init__` galgame 兼容 auto-fill + span 名钉死 | 3 | **已修（机制与 telemetry 已裁决）** |
| P1 | Phase 5 守卫增补打红自家永久 facade / `:577` 机械 flip 静默杀错误路径 / 禁读名单缺项 | deps 单轨化实施 | 5-c0 特征测试 + ALLOWLIST 同 commit 声明 | `llm_ready` 方案 + ALLOWLIST `{deps, visual_job, tts_job}` + 名单补 `llm_client/tool_schemas` | 5 | **已修（三 commit 结构 + 名单已裁决）** |
| P1 | Phase 7 ToolProbeStream cancel/error 语义未定义 / ProviderTraits 泄漏回 runtime | flip 后新代码分支 | 契约专测 + AST 守卫（traits 读禁令） | `.calls` 耗尽后可读契约 + 守卫入 7-c2 | 7 | **已修（契约已裁决 + 守卫入表）** |
| P1 | 双轨超期残留（services/deps 前车之鉴） | 任一 phase 收口不彻底 | Dual-Track 登记表逐项核查（phase 批准前置检查） | 超期 = 阻塞后续 phase 批准；D3 加 6a 弱守卫止血 | 全部 | 仍在场（治理机制加强） |
| P1 | 后续 AI 会话被旧文档引回旧落点 | 文档滞后于 seam 切换 | 每 phase 收口 checklist 含文档 diff | 文档与代码同 commit（迁移原则 3(d)；test-only phase 走 0D） | 3、5、7 | 仍在场 |
| P1 | Phase 2 行为变更外溢（漏改一个调用点 → recent 永远读空） | 三生产调用点未全走 strategy | 对称性测试 + xfail 转绿 + golden_sync | 调用点清单写死在白名单（`stages.py:157` / `memory_commit.py:41` / `chat_engine.py:247`） | 2 | 仍在场（检测面已加厚） |
| P2 | contributor 顺序改变 prompt 语序 | 多 contributor 时代 | full-prompt golden + 显式 `priority` | 单 contributor 期先钉 golden | 3 | 仍在场 |
| P2 | `test_moondream_default_cutover` 15-patch 随 host 改动脆化 | Phase 4/5/6 触碰其 patch 目标 | 该测试列入 4/5/6a/6b 必跑 | 方法名 facade 保位 + Phase 5 白名单含该测试（patch 目标迁移） | 4、5、6a、6b | 已修一半（白名单已补），实施验证 |
| P2 | summarizer/judge 构造签名变更漏改调用点 | Phase 6a | 测试先红后绿 | 先改测试再改码 | 6a | 仍在场 |
| P2 | runtime⇄galgame 包级双向依赖引入隐性环 | Phase 1/3 import 方向 | `test_layering::test_spica_packages_import_cleanly` | 模块级无环论证入 phase 报告；deps 侧函数级懒 import | 1、3 | 设计已消解，待实施验证 |
| P3 | 文档行号漂移、registry 元组期双索引窗口 | 各 phase | 开工前 rg 校准 checklist；4R 独立拆出 | REAL_ARCHITECTURE_MAP 每 phase 末更新 | 各 phase、4R | 已修一半（4R 拆出） |

---

## Decision Log

### 现在做（Y1）

CharacterScope（property 形态）、MemoryScopeStrategy（live-read）、scoped recent、PromptContextContributor（gate=`mode(request)`；galgame 兼容 auto-fill）、assemblies/installer 约定、ReactionScoringPolicy、ToolEntry（独立 4R）、deps 单轨（stages/memory_commit 禁区 + `llm_ready` + 保值 schema-count）、TextModel + BoundModel（6a）、ModelRouter（6b）、ToolCallingModel + ToolProbeStream 契约（7）、ProviderTraits（最小化 + AST 守卫）。

### 等真实需求再做

PersonaRuntime（第二角色包立项时；前置已由 Phase 2 备齐）、ActiveDomainRouter + WindowTarget + PrivacyGate + DomainContextRequest 泛化（co-watch 批准时，Phase 8）、ToolAuthority 对象 + ToolExecutionPolicy（browser/media 批准时，Phase 9）、Anthropic/local adapter（Phase 7 后按需）、game_memory O(n²) schema 手术（独立小刀，随时可插队）。

### 永远不做

TurnPipeline/stage 类化（单一生产形状 + locality + N1 守卫全站在函数式一边——不能证明其非 speculative，故禁）、GameSessionRegistry（同类型并发无用户故事且撞 GPU 约束）、ConversationScope（TurnRequest 已是）、ConfigSnapshot（杀原地突变即达成）、ToolConversation、StructuredOutputModel（直至 provider 保证 + 质量刚需）、ScreenEnvironment、MemoryPort 空钩子扩展、工具垫片重继承基类（mixin 亦仅 nice-to-have 不排期）。

### 保留为 facade（永久）

`retrieve_game_context_node` 别名（D2 守卫）、LLMPort v1 + adapter v1 方法（冻结链用户）、`services` 作 unit-job 参数载体（visual_job/tts_job，D1 ALLOWLIST）、ChatEngine legacy-dict 桥（UI 项目范围外）、`ui/qt_overlay.py:482,959` 显示层 speaker 默认（非身份，永久豁免）。

### 禁止再长新能力的模块

`sync_chain.py`、`stages.py` 冻结区（`call_llm_node` 及 sync-only stages）、`memory/recent.py`（保持哑存储）、`app_host.py`（Phase 4 后禁新增 per-domain 方法）、`retrieve_game_context_node` 别名与 `contribute_context_node` node 本体（D2 行数/AST 约束）、deps `__post_init__` auto-fill（永不长第二项）。

### 人工拍板结果（已定，施工中不得重议）

1. **Phase 顺序**: Phase 0 → Phase 1 → Phase 2 → Phase 3；Phase 4R 可穿插在 1/2/3 之间。
2. **Phase 3 contributor 机制**: 采纳方案 a（`TurnDeps.__post_init__` auto-fill），但限定为 **galgame compatibility auto-fill**；未来 domain 必须通过 assembly 显式注册完整 contributor tuple。
3. **available_tool_schema_count**: 采纳保值方案（bridge 时灌 deps 字段，值逐字节不变）。
4. **session/controller 构造默认值**: 先豁免，不扩大 Phase 2（`agent_assembly.py:170` 仍收编为 scope.py 常量 import）。
5. **D3 时钟**: 接受「6a 收口后 ≤2 个已批生产 phase 内完成 Phase 7；6b 可对调不占时钟」。
6. **Phase 8 预留**: `context.py` / `chat_engine.py` 受控改动进入 Phase 8 白名单，具体设计到 Phase 8 批准时单独评审。
7. **Phase 0D 拆分**: 接受（迁移原则 3(d) 对 test-only phase 的例外成立）。

---

## Open Questions

1. **`_new_summarizer` / `_new_reaction_judge` / `_judge_llm_adapter` 方法名 facade 的删除时机**：Phase 6b 收口后这些方法只剩转发 router 的一行；删除需要连带重构 `test_moondream_default_cutover` 的 patch 形态（患处同 Phase 4/5 的教训）。何时立项、是否值得删，未拍板——默认永久保留直至有明确收益。
2. **多 contributor 时代的 span 命名与观测语义**：单 contributor 期 span 名已钉 `retrieve_game_context_node`；domain #2 到来时是每 contributor 一个 span、还是一个 node span 带 contributor 标签，未裁决——挂 Phase 8 设计一并出。
3. **Phase 8 的 request 落点形态**：泛化 `DomainContextRequest` 槽 vs 纯域 conversation 前缀纪律（或两者组合）——已拍板推迟到 Phase 8 设计时裁决，此处仅登记两个候选与约束（`source` 在 `chat_engine.py:176` 被 del，不可作 gate 输入；system turn 识别依赖域 conversation_id）。

---

## Appendix

### A. Grill findings summary（对抗性评审 → v2 落点）

| Finding | 一句话 | v2 落点 |
|---|---|---|
| P0-1 | Phase 2 白名单漏 4 个裸 key 测试文件，按回滚条款必然回滚 | Phase 2 白名单补全 + 爆炸半径 rg 前置检查 |
| P1-1 | contributor 只挂 from_services 覆盖不了 ~25 处直构 TurnDeps 测试 | `__post_init__` galgame 兼容 auto-fill（拍板 #2） |
| P1-2 | span 名被三处 timing 断言钉死而 v1 未承诺 | 单 contributor 期钉 `retrieve_game_context_node` |
| P1-3 | ManualGameMemory 自动时间戳使 golden 必 flaky | Phase 0 #2 禁用 + 固定错开时间戳 + 连跑两遍 gate |
| P1-4 | available 谓词无公共访问器；测试与既有覆盖大量重复 | Phase 0 #1 公共接口硬规定 + 定位改「补缺口 + 集中背书」 |
| P1-5 | port 级 fake 在 Phase 7 flip 时判据自毁 | Phase 0 #3 钉 client 层 + 拆 a/b 两半 |
| P1-6 | Phase 5 守卫打红自家 facade；`:577` 机械 flip 不可行且无测试；名单缺项 | 5-c0/c1/c2 结构 + `llm_ready` + ALLOWLIST + 全名单 |
| P1-7 | 删薄委托打空 cutover patch 目标且不在白名单 | Phase 5 白名单补 cutover 测试 |
| P1-8 | 身份默认值第二来源（agent_assembly/session/controller）+ frozen-vs-live 未裁决 | 收编/豁免分置 + live-read 裁决 + 补充 rg ledger |
| P1-9 | 域 #2 的 binding 盖章通道无落点归属（context.py/chat_engine.py 被禁改两头堵死） | Phase 8 白名单预留受控改动（拍板 #6） |
| P1-10 | Phase 3 白名单缺 test_layering 而退出条件要求扩它 | Phase 3 白名单补 test_layering |
| P2-1 | Phase 1 诞生 runtime→galgame 首条依赖边未声明 | Phase 1 退出条件补依赖边声明 + 无环论证 |
| P2-2 | gate 检查上收在角例上不是字节等价（active + None 开 span） | node 检查顺序契约保旧语义 + Phase 0 #2(d) 钉基线 |
| P2-3 | Phase 4 三 seam 混装；facade「存在」≠「在路径上」 | 拆 4R + patch 有效性退出条件 |
| P2-4 | Phase 6 三 seam 应拆 | 拆 6a/6b，独立 revert |
| P2-5 | Phase 7 漏检面：mid-stream error / followup cancel / `.calls` 契约 | 7-c0 特征测试 + 契约裁决 + 十项对号表 |
| P2-6 | 6→7 窗口无守卫；traits 禁读仅注释 | 6a 弱守卫 + 7-c2 AST 守卫（含 traits） |
| P2-7 | xfail 写路径未指定，红转绿证明力不足 | Phase 0 #4 硬规定 save_stream_memory + 双 deps 重构 |
| P2-8 | `SqliteMemoryAdapter.recent` 休眠参数可能被误认第四调用点 | D5 记账 + 禁启用 |
| P3-1..4 | 行号/计数漂移、dump() 零消费者、Phase 0 #1 立项语不实、ui 显示层豁免 | 开工 rg 校准 checklist + grill Q1 关闭 + 措辞修正 + 豁免注记 |

### B. Useful rg commands（批准前置 / 收口核验用）

```bash
# Phase 2 爆炸半径（批准前置）
rg -n 'recent_memory\.get_recent\("(default|c1)"\)' tests

# Phase 2 指定身份搜索（全量面）
rg -n 'or "spica"|or "麦"|character_id\s*=\s*"spica"|user_id\s*=\s*"麦"|RecentMemory|recent_memory|append_user_message|load_recent_context_node|clear_memory' spica tests ui hardware

# Phase 2 补充搜索（注解式默认值，退出条件 ledger）
rg -n 'character_id[^=]*=\s*"spica"|user_id[^=]*=\s*"麦"' spica --type py

# Phase 2 退出 grep（单一解析点）
rg -n 'or "spica"|or "麦"' spica

# Phase 1/3 依赖方向核验
rg -n 'from spica\.galgame|import spica\.galgame' spica/runtime spica/core

# Phase 5 services 属性读全量盘点
rg -n 'services\.(tts_adapter|visual_tool|recent_memory|llm_client|tool_schemas|logger)' spica/runtime

# D3 v1 消费者扫描（6a 弱守卫范围核定）
rg -n 'prefers_chat_completions|iter_response_text|create_chat_with_tools|iter_chat_with_tools|complete_text' spica

# Phase 3 facade 引用面
rg -n 'retrieve_game_context_node' tests spica
```

### C. Required test command

```bash
python -m pytest tests -q
```

绝不裸 `pytest`（会递归扫 vendored GPT-SoVITS runtime 直接崩）。所有 phase 的 gate 均以此命令的全量绿为准；本文不声称任何测试已被执行。

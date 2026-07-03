# Spica Long-Term OO Migration Plan

> 版本 v1 · 生成日期 2026-07-03 · 状态：**Phase 0 待批准**
> 来源：三轮只读评估（小刀重构评估 → 长期 OO/Deep-Module 蓝图 → 本计划书），全部基于对
> 7 份权威文档 + runtime/host/galgame/config 全部核心文件 + ports 全层 + 132 个测试文件保护面的直读。
> 本文件是**施工单**，不是愿景文。每个 phase 必须单独批准、单独收口、单独可回滚。
> 测试命令恒为 `python -m pytest tests -q`（绝不裸 `pytest`）。
>
> 配套文件：`README.md`（状态板 + 使用规则）、`PROGRESS.md`（逐 phase 收口日志）。

---

## 0. 迁移目标与四条元规则

**目标**：把蓝图的五个 seam 再契约（LLM provider / host domain assembly / prompt context contributor /
character-memory scope / tool authority）变成一串可单独批准的 phase。终点是五个硬能力：

1. 非 OpenAI provider 只需写一个 adapter；
2. 新 domain 上下文注入不碰 runtime 高危文件；
3. 第二角色接入不产生静默记忆污染；
4. 新 domain 装配不使 AppHost 增长（≤15 行/domain）；
5. act 工具规模化时权限面恒定收敛在 host。

每个 phase 交付的是 **seam**；feature（co-watch/browser）本身不在本计划内——计划只保证 seam 赶在
feature 前就位。

**元规则一：对蓝图的三处显式修正**（已在本计划落实）：

- ① Phase 1 原提示词自相矛盾：`_build_game_context_sections` 引用 `DEFAULT_INTERLOCUTOR_NAME`
  （`stages.py:26,516`）与 `_should_inject_companion`/`_COMPANION_INTENT`（`stages.py:65,369-372`），
  「逐字节搬且只 import json+typing」不可能成立 → 改为**显式 import 白名单 + 搬迁清单 +
  以 prompt 字节 golden 为等价判据**（源码允许最小改写，输出禁止变化）。
- ② contributor 的 gate 签名定死为 `mode(request)`，拒绝 `should_contribute(ctx, deps)`（论证见 §6.2）。
- ③ 多角色数据安全前移：scoped recent 是零数据迁移的最便宜硬爆点修复，**提前到 Phase 2**（论证见 Phase 2）。

**元规则二：防止半新半旧双轨长期残留**（本仓有实证教训：services/deps 双轨从 C4 悬置至今，
`spica/runtime/services.py:9-10`）：

- **寿命上限**——每个双轨点在 §7 登记最长并存 phase 数，超期即阻塞后续 phase 批准；
- **守卫只增**——旧 seam 退役的同一 commit 加 AST/grep 守卫封死旧入口；
- **文档同 commit**——CLAUDE.md/GUARDRAILS 决策树在 seam 切换的同一 commit 更新，
  否则未来 AI 会话会被旧模板引回旧落点；
- **冻结区显名**——sync_chain 一族的「永久 v1」不算双轨，算博物馆，在 §9 Decision Log 单独记账。

**元规则三：phase 退出条件（六项全过才算完成）**：

- (a) `python -m pytest tests -q` 全绿（含该 phase 加强后的守卫）；
- (b) 该 phase 定义的 parity gate 达标（prompt/payload 字节级或断言级）；
- (c) **旧 seam 使用计数归零**——grep/AST 可验证的禁区内零引用；
- (d) 文档（CLAUDE.md/GUARDRAILS 对应条目）与代码同 commit 更新；
- (e) 单 commit 或线性小 commit 序列，`git revert` 干净可回；
- (f) §7 双轨登记表更新（开钟/停钟），并在 `PROGRESS.md` 记收口日志。

**元规则四：必须停止并回滚的情形**：任一守卫测试变红且修复需要「放宽守卫」；prompt/payload parity
出现无法解释的字节差；实施中发现需要修改「禁止修改文件」清单内的文件；实施 agent 提出的「顺手改进」
超出白名单。任一发生 → `git revert` 本 phase 全部 commit，回到上一收口点，重新评审 phase 定义，
而不是现场扩权。

---

## 1. Migration Principles

**为什么不推倒重写**：本仓的核心资产是保护面，不是代码——12 个 AST/语义守卫、golden 锚
（`sync_chain.py` 冻结链 + `test_golden_sync`/`test_turn_contract` 7 形态）、40+ 处测试对模块路径的
import/patch 耦合（7 文件 `from spica.runtime.stages import`、4 处
`patch("spica.runtime.stages.analyze_screen_attachment")`、`test_moondream_default_cutover`
15-patch 驱动 `initialize()`）。重写 = 同时作废被保护物与保护物，且本仓由 AI 会话高频维护，
平行结构是误改温床。

**为什么 aggressive strangler**：strangler 让新旧两侧共存于同一测试面下（facade 保 import/patch 点、
golden 保行为字节），每 phase 独立收口。「aggressive」的含义是**节奏与收口彻底性**：
一个 seam 一个 phase 内完成切换、守卫封旧、文档改向，绝不允许两个 seam 同时处于半迁移态。

**什么叫一个 phase 完成**：元规则三的六项全过。

**什么情况必须停止并回滚**：元规则四。

---

## 2. 修正后的目标架构（14 个 object/interface 的最终形状）

> 记法：【Y1】第一年做；【按需】等真实需求（对应 feature 批准）再做；【不做】不推荐。
> 每项含 facade 兼容与退出条件。

### CharacterScope【Y1, Phase 2】
`@dataclass(frozen=True): character_id: str; user_id: str`，落 `spica/runtime/scope.py`
（不动高危 `context.py`）。host 装配期 resolve-once，挂 `AppHost.character_scope` 与
`TurnDeps.character_scope`。
**facade**：无需——纯增量；18 处 `or "spica"`/`or "麦"`（`app_host.py:432-433` 等 13 处、
`stages.py:183,515`、`memory_commit.py:65`、`chat_engine.py:242`）机械替换。
**退出条件**：`grep 'or "spica"\|or "麦"' spica/` 生产代码仅剩单一解析点（scope 构造处）1 处命中。

### MemoryScopeStrategy【Y1, Phase 2】
具体类（非 Protocol——单实现，立项理由是把 §27① 对称法则从注释纪律变成单一居所）：
`recent_key(request) -> str`（= `scoped_conversation_id(character_id, request.conversation_id)`）、
`ltm_scope(request) -> MemoryScope`（用 `effective_memory_conversation_id`）、
`clear_targets(conversation_id) -> (recent_key, ltm_conversation_id)`。落 `spica/runtime/scope.py`。
**facade**：三个消费点（`stages.py:157` 读、`memory_commit.py:41,64-68` 写、
`chat_engine.py:247,250` 清）改为经 strategy，函数签名全部不变。
**退出条件**：三点全部经 strategy + 一条新对称性测试直接断言「retrieve 与 commit 用同一 `ltm_scope`」。

### PersonaRuntime【按需——第二角色包真实落地时】
不可变 persona 包（id/名字/profile/skill_dir/visual/tts 引用），切角色 = 换引用，废除
`set_interlocutor_name` 的原地突变（`chat_engine.py:218-225`、`agent_assembly.py:180-183`）。
前置（CharacterScope、strategy）已在 Phase 2 备齐。
**facade**：`set_interlocutor_name` 签名不变、内部换实现。
**退出条件**：AppConfig 装配后零突变（可加守卫断言）。

### PromptContextContributor【Y1, Phase 3】
```
Protocol: name: str; priority: int
  mode(request: TurnRequest) -> Literal["active","offline","none"]
  sections(ctx: TurnContext, deps: TurnDeps, mode: str) -> list[str]
```
gate 签名**只收 request**（裁决见 §6.2）。通用 `contribute_context_node` 留在 `stages.py`
（保 N1 守卫覆盖与 patch 面）：先查 `ctx.error`/`ctx.prompt`（今日 `stages.py:538,545` 的通用检查
上收到 node），再逐 contributor 问 `mode`，全 "none" = 字节级 no-op（不开 span）。
注册面：`TurnDeps.context_contributors: tuple[...]`；`from_services` 在 `game_memory` 非 None 时
默认注册 galgame contributor（现有测试字节兼容）。
**facade**：`retrieve_game_context_node = contribute_context_node` 模块级别名永久保留
（7 处测试 import 不动）。
**退出条件**：`orchestrator.py:261`/`sync_chain.py:51` 调新名；Phase 0 全段 prompt golden 字节不变；
N1 扫描含新文件。

### DomainModule / CapabilityInstaller【Y1, Phase 4 立约定，逐 domain 落地】
非基类，是约定：`spica/host/assemblies/<domain>.py :: install(host: AppHost) -> Handle`。
installer 在 host 包内 → 铁律 #7 由包边界保住。
**facade**：AppHost 被搬空的方法留薄委托一个 phase 后删除。
**退出条件（Phase 4）**：reaction 接线全部出 `app_host.py`，AppHost 侧仅剩
`assemblies.reaction.install(self)` 一行级调用。

### ActiveDomainRouter【按需——co-watch 批准时，Phase 8】
`publish(domain, binding: DomainTurnBinding, priority)` / `retract(domain)` /
`current() -> DomainTurnBinding|None`。ChatEngine 的单槽 provider（`chat_engine.py:53`）指向
`router.current`——**ChatEngine 接口零改动即是 facade**。
**退出条件**：galgame 经 router 发布（复用 `companion_controller.py:243-263` 的
publish-last/clear-first 纪律），galgame 全族测试绿。

### ToolAuthority【按需——browser/media 批准时，Phase 9；模式先入文档】
per-domain 类（如 `BrowserAuthority.open_site(site_key, query)`、
`MediaAuthority.playback(cmd: Literal[...])`），只在 `spica/host/` 构造，藏 URL 模板白名单/
参数校验/窗口所有权/事件分发。
**facade**：现有 4 个闭包（`app_host.py:709-730,737-769,771-786,481-503`）不强制改造，
新 act 工具必须走对象形态。
**退出条件**：Phase 9 交付首个 authority 类 + 新守卫（`spica/host/` 之外禁实例化 `*Authority`）。

### ToolExecutionPolicy【按需——Phase 9 与首个真实 policy 同时激活】
`check(name, effect, meta) -> Allow|Deny(reason)`，挂唯一执行入口 `RegistryToolSet.run`
（`tools.py:125-131`，effect 现仅日志）。此前**不建**——choke point 已在，后接是 ~10 行。
**退出条件**：首批 policy（act 忙态互斥/频率闸）有测试。

### TextModel【Y1, Phase 6】
`complete(prompt, *, model) -> str`；`stream(prompt, *, model, state) -> Iterator[str]`
（request dict 在 adapter 内部组装——这就是 depth）。落 `spica/ports/model.py`。
**facade**：LLMPort v1 全保留（冻结链永久用户）；`OpenAICompatibleAdapter` 双实现。
**退出条件**：summarizer/judge 只依赖 TextModel（构造签名改为 `BoundModel`）。

### ToolCallingModel【Y1, Phase 7】
`probe(prompt, tools, *, model, state) -> ToolProbeResult(calls, text)`（非流式）；
`probe_stream(...) -> ToolProbeStream`（`.deltas` 迭代器 + 耗尽后可读 `.calls`——typed 化今日
`iter_chat_with_tools` 的 sink 形态，`ports/llm.py:50-63`）。端点家族选择
（Responses vs Chat Completions）内化进 adapter。
**facade**：v1 方法与 `prefers_chat_completions` 保留给冻结链。
**退出条件**：`tool_round.py`/`orchestrator.py` 零引用 v1 探针方法与 `prefers_chat_completions`
（新 AST 守卫封死）。

### ProviderTraits【Y1, Phase 7，最小化】
adapter 侧 frozen dataclass（流式探针能力/reasoning 词汇/工具方言），供 adapter 内部路由与
management 面展示；**runtime 禁读**（写进契约注释）。
**退出条件**：随 Phase 7 落地即完成。

### ModelRouter【Y1, Phase 6】
host 侧 resolve-once：`for_role("dialogue"|"judge"|"summary") -> BoundModel(adapter, model)`，
收编 `app_host.py:556-598` 三处 fallback。
**facade**：`_new_summarizer`/`_new_reaction_judge` 方法名保留（`test_moondream_default_cutover`
patch 目标），内部改调 router。
**退出条件**：三处 endpoint 决策唯一居所。

### WindowTarget【按需，Phase 8】
frozen dataclass（window_id/match_rule/owner_domain），替换 `app_host.py:680-687` 的裸 5 元组。

### PrivacyGate【按需，Phase 8】
`evaluate(target, purpose) -> WindowSafetyResult`，收编三份安全拷贝
（`ocr_loop.py:149-173`、`session.py:88-100`、`watch_game_screen.py:158-165`）并顺手收窄
已记录的 check→capture race（P1）。单实现立项理由：安全不变量集中化（authority/policy 标准）+
第二消费者（co-watch 截帧）随 Phase 8 到来。
**退出条件**：三处旧拷贝删除、gate 有独立单测、galgame 隐私行为回归绿。

### 明确【不做】（§9 Decision Log 汇总）
TurnPipeline/Stage 类化、GameSessionRegistry（同类型并发）、ConversationScope、ConfigSnapshot、
ToolConversation、StructuredOutputModel、ScreenEnvironment、MemoryPort 空钩子扩展。

---

## 3. Phase Plan（Phase 0–9）

> 通用规则（每 phase 默认继承，不再重复）：全量 gate = `python -m pytest tests -q` 全绿；
> 回滚 = 单 commit（或线性小序列）`git revert`；文档更新与代码同 commit；守卫只增不减不放宽。

---

### Phase 0 — Characterization 保护面（只加测试）

- **目标**：为 Phase 1–7 铺安全网。**零生产代码改动。**
- **为什么排最前**：后续所有 phase 的 parity 判据在此定义；无它则 Phase 1 的「字节等价」无判据。
- **修改文件白名单**：`tests/test_app_host_tool_registration.py`（新）、
  `tests/test_game_prompt_golden.py`（新）、`tests/test_responses_probe_shape.py`（新）、
  `tests/test_recent_memory_scope.py`（新）。
- **禁止修改**：`spica/**`、`memory/**`、`agent_tools/**`、`ui/**`、既有任何测试文件。
- **新增测试内容**：
  1. **AppHost 工具注册元数据**：构造 `AppHost()`（不 `initialize()`），断言 registry 含
     `watch_game_screen`（`intent_gated=False`、available 谓词此刻返回 False）、
     `note_game_observation`（`effect="write"`）、`sing_song`（`effect="act"`、`intent_gated=True`）、
     `inspect_screen`（builtins 注册，带 `compact_output`）。——封堵唯一裸奔的装配面。
  2. **galgame prompt full-section golden**：真实 `GameMemorySqliteAdapter`（tmp 库）喂满
     progress/summaries/buffer/current-line/relations/choices/beats，直调
     `retrieve_game_context_node`，golden 整段注入后 prompt（active 与 offline 两态）。
  3. **Responses probe request shape**：fake Responses client（仿 `test_turn_contract` 的
     `_ToolThenAnswerLLMClient`）录 `create_responses` kwargs，断言 tools 载荷形状、轮次记账、
     无工具时 `tools` 键缺席——补齐 chat 路已有（`test_chat_tool_round`）而 Responses 路缺失的
     形状 golden，Phase 7 的 flip 判据。
  4. **recent 跨角色污染基线**：`@pytest.mark.xfail(strict=True)` 表达**目标行为**——角色 A 以
     conversation_id="default" 写入 recent 后，切 `config.character.character_id="B"` 再经
     `load_recent_context_node` 读同 id，期望为空；今日必失败（`memory/recent.py:12-14` 裸 key）。
     strict xfail：套件保持绿、缺陷有案可查、Phase 2 摘除 xfail 即红转绿，且若有人在 Phase 2 前
     意外「修好」它会强制暴露。**采用 xfail 而非文字说明的理由**：可执行的失败基线比文字说明更能
     防止 Phase 2 走样。
- **允许行为变化**：否。**兼容 facade**：不适用。
- **必跑测试**：四个新测试 + 全量。
- **解锁**：Phase 1/2 可开工。**不完成则禁止**：一切生产代码 phase。

---

### Phase 1 — galgame prompt 段落构建器出走 stages.py

- **目标**：`stages.py` 的 galgame 展示逻辑迁入 domain 包；stages 只留 gate+node。
- **为什么排这里**：最小生产刀，验证 facade+golden 纪律；与 Phase 2 相互独立
  （**如需可与 Phase 2 对调**，二者无依赖）。
- **搬迁清单**（修正后，含蓝图遗漏项）：`_section`、
  `_format_progress/_format_summaries/_format_buffer/_format_relations/_format_choices/_format_beats`、
  `_build_game_context_sections`、`_should_inject_companion`、`_COMPANION_INTENT`、
  `_GAME_CONTEXT_ACTIVE_SUMMARY_LIMIT`（`stages.py:65,72,369-372,375-525`）
  → 新文件 `spica/galgame/prompt_sections.py`。
- **新模块 import 白名单**：`json`、`typing.Any`、
  `spica.conversation.character_loader.DEFAULT_INTERLOCUTOR_NAME`
  （`_build_game_context_sections` 在 `stages.py:516` 引用它）。
  **禁止 import**：`spica.core.events`、`spica.galgame.session`、`spica.runtime.*`、Qt。
  galgame→conversation 是新依赖边但无环（conversation 不反向依赖）。
- **等价判据**：**输出 prompt 字节等价**（Phase 0 golden #2），源码允许最小必要改写
  （import 行、模块 docstring），不承诺「源码逐字节」。
- **留在 stages.py**：`retrieve_game_context_node`、`_game_context_mode`、`_resolve_game_target`、
  `_parse_*`、`_GALGAME_CONVERSATION_PREFIX`、`analyze_screen_attachment` 一带（patch 点）。
- **白名单**：`spica/galgame/prompt_sections.py`（新）、`spica/runtime/stages.py`（删+import）、
  `tests/test_layering.py`（`TRANSFORM_LAYER_FILES` **增加**新文件——只扩域）、
  `CLAUDE.md` §2 表、`docs/DEVELOPMENT_GUARDRAILS.md` §9。
- **禁止修改**：`orchestrator.py`、`sync_chain.py`、`tool_round.py`、`context.py`、`deps.py`、
  `app_host.py`、一切 adapter。
- **行为变化**：否。**facade**：node 与全部 gate 符号原地不动。
- **必跑**：`test_game_prompt_golden` + `test_retrieve_game_context_node` +
  `test_game_context_in_chain` + `test_current_line_injection` + `test_layering` + 全量。
- **解锁**：Phase 3（contributor 包着干净的 sections 模块）。
  **不完成禁止**：Phase 3、任何新 domain 上下文注入需求。

---

### Phase 2 — CharacterScope + scoped recent + MemoryScopeStrategy v1

- **目标**：多角色数据安全的硬爆点在任何角色需求到来前拆除。
- **排序专门论证（为什么这么早）**：
  ① recent 是**纯内存 deque**（`memory/recent.py:10`）——重 key 零数据迁移，是全计划最便宜的
  硬爆点修复；② 污染类缺陷是静默的（A 的近期上下文漏进 B，无异常无日志），越晚越可能在无守卫
  状态下被触发；③ Phase 4 要搬 `app_host.py` 的 reaction 接线，其中 6 处身份默认值
  （`:432-433,489-490,512-513`）——先收敛成 CharacterScope，Phase 4 搬的就是干净代码；
  ④ 与 Phase 1/3 无耦合，不占关键路径。
  **结论：多角色的「数据安全半」必须在 Y1 最前段完成；「运行时切换半」（PersonaRuntime）等
  第二角色包真实立项**——那是 feature，不是爆点。
- **白名单**：`spica/runtime/scope.py`（新：CharacterScope + MemoryScopeStrategy）、
  `spica/runtime/stages.py`（仅 `load_recent_context_node`/`retrieve_long_term_memory_node` 的
  key/scope 构造改经 strategy，`:157,182-186`）、`spica/runtime/memory_commit.py`（`:41,64-68`）、
  `spica/core/chat_engine.py`（`:241-250` clear/list/remember + recent key）、
  `spica/host/app_host.py`（仅 18 处身份默认值机械替换 + `character_scope` 属性）、
  `tests/test_recent_memory_scope.py`（摘 xfail）、`tests/test_memory_commit_scope.py`
  （**预期更新**：其「recent append 保留 raw id」断言随行为变更改为 scoped——行为测试允许改，
  须在 PR 说明）、新对称性测试。
- **禁止修改**：`memory/recent.py`（保持哑存储——key 推导全在 strategy）、
  `adapters/memory/sqlite.py`、`context.py`、`orchestrator.py`、`tool_round.py`。
- **行为变化**：**是，且是本计划唯一的主动行为变更**——recent 桶 key 从裸 `conversation_id` 变
  `{character_id}::{conversation_id}`；`clear_memory` 的 recent/LTM 不对称
  （`chat_engine.py:247` vs `:250`）同步修齐。单角色运行观感不变（同会话读写同 key；
  进程重启本就清空 recent）。
- **facade**：全部公开签名不变。
- **必跑**：memory 五族 + `test_recent_memory_scope`（红转绿）+ golden_sync/turn_contract
  （字节不变）+ 全量。
- **解锁**：第二角色可安全接入（数据层）；Phase 4 搬迁面更干净。
  **不完成禁止**：发布任何多角色功能、PersonaRuntime。

---

### Phase 3 — PromptContextContributor seam

- **目标**：domain 上下文注入从「改三个高危文件」变「新文件+注册」。
- **为什么在此**：Phase 1 已把 sections 模块化；co-watch（domain #2）批准前必须就位。
- **白名单**：`spica/runtime/prompt_context.py`（新：Protocol）、
  `spica/runtime/stages.py`（`contribute_context_node` 通用化 + `retrieve_game_context_node` 别名；
  gate 通用检查上收）、`spica/galgame/context_contributor.py`（新：包 `prompt_sections` + 迁入
  `_game_context_mode`/`_resolve_game_target`/`_parse_*`）、`spica/runtime/deps.py`
  （`context_contributors` 字段 + `from_services` 默认注册规则）、
  `orchestrator.py`/`sync_chain.py`（各 1 行换名）、docs。
- **声明**：`context.py:54-58` 的「gate 代码 untouchable」历史注释所指的语义不变性由
  Phase 0 golden + 别名 facade 承接；两处故意重复的 `galgame::` 字面量**保持不去重**。
- **禁止修改**：`context.py`、`chat_engine.py`、`app_host.py`、`prompt_builder.py`。
- **行为变化**：否（全 "none" 分支保持字节级 no-op：先问遍 mode 再决定开不开 span——
  node 通用检查顺序写进契约注释与测试）。
- **facade**：`retrieve_game_context_node` 别名永久保留（7 处测试 import + 直调方）。
- **必跑**：Phase 0 golden #2 + `test_retrieve_game_context_node` + `test_game_context_in_chain` +
  turn_contract + 全量。
- **解锁**：co-watch/browser 的上下文注入 = domain 内新文件。
  **不完成禁止**：任何第二 domain 的 prompt 注入实现。

---

### Phase 4 — Host assembly 分册 + ReactionScoringPolicy 下沉

- **目标**：AppHost 停止随 domain 增长；policy 与 authority 分层立范。
- **白名单**：`spica/galgame/reaction_scoring.py`（新 policy：judge 调用/冷却状态/
  lexicon mtime 缓存/降级，即 `app_host.py:400-479` 逻辑）、
  `spica/host/assemblies/__init__.py` + `reaction.py`（新）、`spica/host/app_host.py`
  （删搬空方法、加 install 调用、留一版薄委托）、`tests/test_reaction_judge.py`
  （**先改后搬**：`:232` 的 `patch.object(app_host_module.time,...)` 改指 policy 的注入 clock）、
  `spica/plugins/registry.py`（rider：7 元组→`ToolEntry` NamedTuple，读取器 API 不变）、
  docs（GUARDRAILS 新增「新 domain 装配模板」）。
- **禁止修改**：`reaction.py` 引擎本体（scorer seam 签名 `(beat)->ScoreResult` 不动）、
  写闭包（`_request_song`/`_record_game_observation`/`_record_play_history`/beat writer 留 host）、
  `session.py`、`registry.py` 对外 API。
- **行为变化**：否（judge 冷却/降级语义逐断言保持）。
- **facade**：被搬方法名留薄委托一个 phase（`test_moondream_default_cutover` 的
  `patch.object(AppHost, "_new_reaction_judge"...)` 等目标不落空），Phase 5 收口时删。
- **必跑**：reaction 五族（judge/config/wiring/no_comment/proactive）+ Phase 0 测试 #1 +
  `test_registry` + 全量。
- **解锁**：后续任何 domain 按 assemblies 模板接入（AppHost ≤ ~15 行/domain 的预算自此可执行）。
  **不完成禁止**：co-watch/browser 的 host 接线。

---

### Phase 5 — deps 单轨化（stages/memory_commit 禁区版）

- **目标**：结清 C6/C7 债的可结清半：stages 与 memory_commit 只读 deps。
- **白名单**：`spica/runtime/deps.py`（加 `recent` 字段，`from_services`/`from_legacy_services` 接线）、
  `spica/runtime/stages.py`（`:157` recent、`:577` llm 判空、`:603` schema 计数、`:809` visual、
  `:849-866` tts → deps）、`spica/runtime/memory_commit.py`（`:41`）、
  `orchestrator.py:264` 与 `tool_round.py:61`（两处一行 flip）、
  `tests/test_no_dict_config.py`（**加强**：禁读属性名单增补 `tts_adapter/visual_tool/recent_memory`）、
  删 Phase 4 薄委托。
- **禁止修改**：`services.py`（字段保留——`visual_job`/`tts_job` 以参数携带 services 属
  **永久 facade 载体**，见 §7 双轨表）、stage 签名（第三参 `services` 留为惰性参数，守卫禁读）。
- **行为变化**：否（同对象换引用；golden_sync payload 字节钉死）。
- **必跑**：golden_streaming/golden_sync + turn_contract + `test_no_dict_config`（加强后）+ 全量。
- **解锁**：Phase 6/7 的 `deps.model` 有干净落点。**不完成禁止**：Phase 6。

---

### Phase 6 — Model 层 v2 之一（TextModel + ModelRouter，叶子先迁）

- **目标**：turn 外 LLM 消费者（summarizer/judge）脱离 v1；endpoint 决策收敛。
- **白名单**：`spica/ports/model.py`（新：TextModel + BoundModel）、
  `spica/adapters/llm/openai_compatible.py`（**只增** v2 方法，内部复用现路径）、
  `spica/host/model_router.py`（新，收编 `app_host.py:556-598`）、
  `spica/galgame/summarizer.py` + `reaction_judge.py`（构造签名改收 BoundModel）、
  `app_host.py`（`_new_summarizer`/`_new_reaction_judge`/`_judge_llm_adapter` 内部改调 router，
  **方法名保留**）、`tests/test_galgame_summarizer.py`/`test_reaction_judge.py`
  （**预期更新**：mock 形状随构造签名变）、新增 v2 契约测试套件
  （参数化 over adapters——为未来第二 provider 免费复用）。
- **禁止修改**：`tool_round.py`、`orchestrator.py`、`stages.py`、`ports/llm.py`。
- **行为变化**：否。**facade**：LLMPort v1 全保留；adapter 双实现。
- **必跑**：summarizer/judge/reaction 族 + v2 契约套件 + 全量。
- **解锁**：judge/summary 可换任意 provider。
  **不完成禁止**：Phase 7、任何非 OpenAI adapter 动工。

---

### Phase 7 — Model 层 v2 之二（ToolCallingModel，生产链 flip）

- **目标**：`prefers_chat_completions` 与 v1 探针方法退出 runtime；provider #2 硬爆点拆除。
- **白名单**：`ports/model.py`（ToolCallingModel + ToolProbeResult/ToolProbeStream + ProviderTraits）、
  `openai_compatible.py`（实现）、`spica/runtime/deps.py`（`model` 字段；`llm` 保留给冻结链）、
  `tool_round.py`（probe 族改 `deps.model`，STREAM_RESET 语义不动）、
  `orchestrator.py`（`iter_response_text` → `model.stream`）、
  新守卫 `tests/test_no_v1_llm_in_runtime.py`（AST：`orchestrator.py`/`tool_round.py` 禁引用
  `prefers_chat_completions/iter_response_text/create_chat_with_tools/iter_chat_with_tools`；
  `stages.py` 冻结区豁免并注明）、docs（CLAUDE §2 模型层条目改向）。
- **禁止修改**：`stages.py` 的 `call_llm_node`（冻结链永久 v1）、`sync_chain.py`、golden 断言。
- **行为变化**：否（Phase 0 #3 Responses 形状 golden + chat 路既有形状断言 +
  turn_contract 7 形态 + cancellation + no_comment 全部字节钉死）。
- **facade**：v1 Protocol/adapter 方法永久保留（博物馆租金）。
- **必跑**：上述全部 + `test_chat_tool_round` + `test_tool_chain_rounds` + 新守卫 + 全量。
- **解锁**：**非 OpenAI provider = 只写一个 v2 adapter**。
  **不完成禁止**：Anthropic/local adapter 立项。

---

### Phase 8 —【按需触发：co-watch 批准】ActiveDomainRouter + WindowTarget/PrivacyGate

- **目标**：domain #2 的 turn-binding 碰撞点与多窗口安全不变量在 co-watch 动工前就位。
- **白名单**：`spica/host/domain_router.py`（新）、`app_host.py`（`_companion_game_binding`
  改经 router，≤10 行）、`companion_controller.py`（publish/retract 改向 router，纪律不变）、
  `spica/runtime/window.py`（新：WindowTarget）+ `spica/galgame/privacy_gate.py`（新：吸收
  `ocr_loop.py:149-173`/`session.py:88-100` 引用/`watch_game_screen.py:158-165` 三份拷贝）、
  `ocr_loop.py`/`watch_game_screen.py`（改调 gate）、`app_host.py:680-687`（5 元组→WindowTarget）。
- **禁止修改**：`chat_engine.py`（provider 接口零改动）、`session.py` 的锁与 FSM。
- **行为变化**：否（gate 判定逻辑等价迁移；若顺手收窄 P1 race 须单独 commit 并声明）。
- **必跑**：galgame 全族 + `test_watch_game_screen` + 新 gate 单测 + 全量。
- **解锁**：co-watch domain 按预算落地（AppHost ≤15 行、runtime 0 行）。
  **不完成禁止**：co-watch 任何 turn-binding/截帧实现。

---

### Phase 9 —【按需触发：browser/media 批准】ToolAuthority 对象化 + ToolExecutionPolicy 激活

- **目标**：act 规模化的权限与策略基建，与首个 browser/media authority 同 phase 交付。
- **白名单**：`spica/host/authorities/`（新包：首个 `BrowserAuthority`/`MediaAuthority`，
  URL 模板白名单 + 命令枚举）、`tools.py`（`run` 加 policy check ~10 行）、
  `registry.py`（无 API 变化）、新守卫（host 包外禁实例化 `*Authority`）、
  对应工具垫片与 assemblies 文件、config 新 typed 节。
- **禁止修改**：既有 4 个闭包（可后续自愿改造）、`tool_round.py`。
- **行为变化**：新能力增量；既有工具行为字节不变（policy 对 read/write 默认放行）。
- **必跑**：act 纪律断言（仿 `test_sing_song_tool`）+ 工具族 + 全量。
- **解锁**：全部目标能力就绪。**不完成禁止**：任何绕 authority 的浏览器/播放器控制实现。

---

## 4. Phase 0 覆盖确认

四项全部纳入（见 Phase 0 白名单）：AppHost 工具注册元数据 ✅；galgame full-section golden ✅；
Responses probe request shape ✅；recent 跨角色污染基线 ✅（strict xfail 表达目标行为——选 xfail
而非「说明不加」的理由已在 Phase 0 内注明：可执行失败基线 > 文字说明，且 strict 防「意外修好」）。

---

## 5. Phase 0 实现提示词

```text
你在 /home/san/ai_code/Spica-Chatbot 执行《Spica Long-Term OO Migration Plan》Phase 0
（docs/oo_migration/MIGRATION_PLAN.md）。
本 phase 只允许新增测试文件，禁止改动任何生产代码与任何既有测试。
先读 CLAUDE.md §1 铁律 + docs/DEVELOPMENT_GUARDRAILS.md §13，然后【先输出计划并等待确认】：
列出你将新增的 4 个测试文件、每个文件的断言清单、你不会碰的文件。确认后再动手。

新增（只此四件）：
1) tests/test_app_host_tool_registration.py
   构造 AppHost()（不调 initialize），断言 registry：watch_game_screen 存在、intent_gated=False、
   available 谓词此刻返回 False；note_game_observation effect=="write"、intent_gated=False；
   sing_song effect=="act"、intent_gated=True；inspect_screen 已由 builtins 注册且带 compact_output。
2) tests/test_game_prompt_golden.py
   用真实 GameMemorySqliteAdapter（tmp 路径）喂满 progress/summaries/buffer/current-line/
   relations/choices/beats，构造带 game_memory 的 TurnDeps，直调 retrieve_game_context_node，
   对 active 与 offline 两态分别 golden 整段注入后 prompt 文本（存为测试内嵌常量）。
3) tests/test_responses_probe_shape.py
   仿 tests/test_turn_contract.py 的 fake LLM client 形制，录 create_responses 的 kwargs：
   断言有工具时 tools 载荷原样透传、无工具时无 tools 键、轮次与 followup prompt 含 [TOOL_RESULTS]。
4) tests/test_recent_memory_scope.py
   (a) 现状 characterization：不同 conversation_id 的 recent 互不可见（应通过）；
   (b) @pytest.mark.xfail(strict=True)：角色 A 写 recent 后把 config.character.character_id 改为 "B"，
       经 load_recent_context_node 读同一 conversation_id，断言读不到 A 的内容（今日必失败）。

gate：python -m pytest tests -q 全量绿（xfail 计 xfailed 不计 failed）。
禁止：任何 spica/**、memory/**、agent_tools/**、ui/** 改动；任何"顺手"的生产代码修复——
xfail 用例暴露的缺陷由 Phase 2 修，不是本 phase。
收尾报告：新增文件清单、全量真实输出、golden 样本的生成方式说明；
并更新 docs/oo_migration/README.md 状态板与 PROGRESS.md 收口日志。
```

---

## 6. Phase 1 候选（不默认批准）与三项强制修正

**6.1 prompt_sections 搬家矛盾修正**：见 Phase 1 正文——搬迁清单补入
`_should_inject_companion`/`_COMPANION_INTENT`/`_GAME_CONTEXT_ACTIVE_SUMMARY_LIMIT`；
import 白名单为 `json` + `typing.Any` + `DEFAULT_INTERLOCUTOR_NAME`；等价判据从「源码逐字节」
改为「**输出 prompt 字节等价**（Phase 0 golden）」。

**6.2 gate 接口裁决：拒绝 `should_contribute(ctx, deps)`，保持 `mode(request) -> str`**。理由：
① CLAUDE §3 允许的 gate 输入（interaction_mode/conversation_id 前缀/active session/关键词）中，
「active session」已由既有机制**盖章进 request**（`GameTurnBinding` → `game_context_request`，
`chat_engine.py:79-99`）——domain 运行时状态进 gate 的合法通道是 binding 发布，不是 gate 直读；
② 给 gate 开 `(ctx, deps)` 就是给未来 contributor 在**每个普通聊天 turn**上做 DB 读/开 span 的通道，
「none = 字节级 no-op」保证（`stages.py:537-540`）将只能靠自觉维持；
③ `ctx.error`/`ctx.prompt` 检查是 node 级通用逻辑，上收到 `contribute_context_node`，
contributor 永远看不到 ctx 的 gate 期状态。
窄签名是结构性防线，宽签名是纪律性防线——本仓的历史证明结构性防线才活得下来。

**6.3 CharacterScope/scoped recent 提前裁决：采纳，前移为 Phase 2**（原蓝图第 4/7 phase 合并前移）。
论证见 Phase 2 正文。补充：Phase 1 与 Phase 2 **无依赖、可对调**——若多角色风险更急迫，可先批 Phase 2。

---

## 7. 双轨治理登记表

| # | 旧 seam | 新 seam | 最长并存 | 删除/封死条件 | 防再生长守卫或规则 |
|---|---|---|---|---|---|
| D1 | stage 读 `services.*`（`stages.py:157,577,603,809,866`） | `deps.*` | 至 Phase 5 收口 | stages/memory_commit 禁区零引用 | `test_no_dict_config` 属性名单扩充（Phase 5，加强）；`visual_job`/`tts_job` 的 services 参数标注**永久 facade 载体**，禁新增读者（GUARDRAILS 条目） |
| D2 | `retrieve_game_context_node` 直连 galgame | contributor 注册 | 别名**永久**（成本≈0） | 不删；禁往别名/node 加 domain 逻辑 | N1 扫描含新文件；CLAUDE 决策树改为「写 contributor」（Phase 3 同 commit） |
| D3 | LLMPort v1（探针族 + `prefers_chat_completions`） | TextModel/ToolCallingModel | 生产链并存 ≤2 phase（6→7 必须连续排期） | `orchestrator/tool_round` 零 v1 引用 | 新 AST 守卫 `test_no_v1_llm_in_runtime`（Phase 7）；冻结链豁免显式注明 |
| D4 | AppHost 内 reaction 方法 | assemblies + policy | 薄委托 ≤1 phase（4→5 删除） | 委托方法删除、cutover 测试 patch 目标迁移 | 规则：Phase 4 后 `app_host.py` 禁新增 per-domain 方法（GUARDRAILS §3.1 更新 + review checklist） |
| D5 | 裸 recent key | strategy scoped key | **零并存**（Phase 2 一次 flip 三个调用点） | xfail 转绿 + `test_memory_commit_scope` 更新 | 对称性测试常驻；`memory/recent.py` 禁长 key 逻辑（保持哑存储，文档规则） |
| D6 | AppHost 单槽 binding provider | ActiveDomainRouter | Phase 8 一次切换 | galgame 经 router 发布且全族绿 | 规则：`chat_engine.set_game_binding_provider` 禁二次注入（router 是唯一注入者） |
| — | `sync_chain.py` + `call_llm_node` v1 | （无新侧） | **不是双轨，是冻结博物馆** | 永不迁移、永不长新能力 | 既有 F8 冻结注释 + §9 Decision Log 记账 |

---

## 8. 风险登记表

| 级 | 风险 | 触发条件 | 检测方式 | 应对 | 消除责任 phase |
|---|---|---|---|---|---|
| P0 | 守卫静默失守（拆 stages 后 N1 只扫旧文件 / patch 打空命名空间） | Phase 1/3 搬迁遗漏 | `test_layering` 扫描域核对 + patch 点清单复核（4 处 `analyze_screen_attachment`） | 白名单强制含 `test_layering` 扩域；`analyze_*` 一带列禁改 | 1、3 |
| P0 | authority 随 policy 一起下沉出 host 包 | Phase 4/9 实施走样 | code review + 建议守卫（host 外禁 `*Authority` 实例化） | installer/authority 只落 `spica/host/`；写闭包列禁改清单 | 4、9 |
| P0 | 模型 flip 打碎探针请求形状 | Phase 7 rewiring | Phase 0 #3 + `test_chat_tool_round` 形状断言 | 逐消费者独立 commit，任一红即 revert | 7 |
| P1 | 双轨超期残留（services/deps 前车之鉴） | 任一 phase 收口不彻底 | §7 登记表逐项核查（phase 批准前置检查） | 超期 = 阻塞后续 phase 批准 | 全部 |
| P1 | 后续 AI 会话被旧文档引回旧落点（CLAUDE 决策树仍写「仿 retrieve_game_context_node」） | 文档滞后于 seam 切换 | 每 phase 收口 checklist 含文档 diff | 文档与代码同 commit（退出条件 d 项） | 3、5、7 |
| P1 | Phase 2 行为变更外溢（scoped recent 漏改一个调用点 → recent 永远读空） | 三调用点未全走 strategy | 对称性测试 + xfail 转绿 + golden_sync | 调用点清单写死在 phase 白名单（`stages.py:157`/`memory_commit.py:41`/`chat_engine.py:247`） | 2 |
| P2 | contributor 顺序改变 prompt 语序 | Phase 3 多 contributor 时代 | full-prompt golden + 显式 `priority` | 单 contributor 期先钉 golden | 3 |
| P2 | `test_moondream_default_cutover` 15-patch 随 host 改动脆化 | Phase 4/6 触碰其 patch 目标 | 该测试列入两 phase 必跑 | 方法名 facade 保位；顺手降 patch 数（允许的行为测试改造） | 4、6 |
| P2 | summarizer/judge 构造签名变更漏改调用点 | Phase 6 | `test_galgame_summarizer`/`test_reaction_judge` 预期更新清单 | 先改测试（红）再改码（绿） | 6 |
| P3 | 文档行号漂移、registry 元组期双索引窗口 | 各 phase | 收口 checklist | REAL_ARCHITECTURE_MAP 每 phase 末更新 | 各 phase |

---

## 9. Decision Log

- **现在做（Y1）**：CharacterScope、MemoryScopeStrategy、scoped recent、PromptContextContributor
  （gate=`mode(request)`）、assemblies/installer 约定、ReactionScoringPolicy、ToolEntry、
  deps 单轨（stages/memory_commit 禁区）、TextModel、ModelRouter、ToolCallingModel、
  ProviderTraits（最小化）。
- **等真实需求再做**：PersonaRuntime（第二角色包立项时）、ActiveDomainRouter + WindowTarget +
  PrivacyGate（co-watch 批准时）、ToolAuthority 对象 + ToolExecutionPolicy（browser/media 批准时）、
  Anthropic/local adapter（Phase 7 后按需）、game_memory O(n²) schema 手术
  （独立小刀，随时可插队）。
- **永远不做**：TurnPipeline/stage 类化（单一生产形状 + locality + N1 守卫全站在函数式一边——
  不能证明其非 speculative，故禁）、GameSessionRegistry（同类型并发无用户故事且撞 GPU 约束）、
  ConversationScope（TurnRequest 已是）、ConfigSnapshot（杀原地突变即达成）、ToolConversation、
  StructuredOutputModel（直至 provider 保证+质量刚需）、ScreenEnvironment、
  MemoryPort 空钩子扩展、工具垫片重继承基类（mixin 亦仅列 nice-to-have 不排期）。
- **保留为 facade（永久）**：`retrieve_game_context_node` 别名、LLMPort v1 + adapter v1 方法
  （冻结链用户）、`services` 作 unit-job 参数载体、ChatEngine legacy-dict 桥（UI 项目范围外）。
- **禁止再长新能力的模块**：`sync_chain.py`、`stages.py` 冻结区（`call_llm_node` 及
  sync-only stages）、`memory/recent.py`（保持哑存储）、`app_host.py`（Phase 4 后禁新增
  per-domain 方法）、`retrieve_game_context_node` 别名。

---

## 10. 批准状态与后续 grill

**推荐批准**：**Phase 0**（只加测试、零风险、为一切后续定判据）。

**不建议马上批准**：Phase 1/2 待 Phase 0 收口 + 下一轮 grill 后按序批（二者可对调）；
Phase 3–7 逐个批准，禁止打包批；Phase 8/9 挂 feature 批准触发器，本计划不排日期。

**需要下一轮 grill 的问题清单**：

1. Phase 2 的 scoped recent 是否应同时给 `RecentMemory.dump()` 的消费方（若有 UI/调试读者）做兼容检查？
2. `contribute_context_node` 的「全 none 才零 span」在多 contributor 下的观测语义：
   单 contributor active 时 span 名是否保留 `retrieve_game_context_node`（遥测连续性）？
3. ToolProbeStream 的 `.calls` 耗尽后可读契约在取消路径（`STREAM_RESET` 前 cancel）下的语义是否完备？
4. Phase 6 BoundModel 化 summarizer/judge 后，`app_host` 三个 `_new_*` 方法名 facade 何时可删
   （cutover 测试重构的时机）？
5. Phase 2 与 Phase 1 的实际批准顺序（多角色紧迫度 vs 最小刀验证纪律）。

**给对抗性评审模型的 grill 提示词**：

```text
你是 /home/san/ai_code/Spica-Chatbot 的对抗性迁移评审员。输入是
docs/oo_migration/MIGRATION_PLAN.md。只读，证伪导向，逐项攻击：
1) 逐 phase 核对"修改文件白名单 vs 该 phase 声称的目标"：找出任何一个目标无法在白名单内达成的
   phase（例如 Phase 2 的 18 处身份替换是否真的不需要碰 galgame/session.py 的构造默认参数
   character_id="spica"/user_id="麦"——session.py:151-152、companion_controller.py:97-98 在不在清单里？）。
2) 验证 Phase 1 搬迁清单的完备性：grep stages.py 375-525 区间的全部名字引用，确认 import 白名单
   （json/typing/DEFAULT_INTERLOCUTOR_NAME）无遗漏；特别检查 _format_buffer 是否被 [CURRENT_LINE]
   路径之外的调用方引用。
3) 攻击 gate=mode(request) 裁决：构造一个 co-watch 场景使"binding 盖章进 request"不够用
   （如需要按播放器实时状态决定注入粒度），证明该场景是否真的能且应该经 binding 通道解决。
4) 攻击 Phase 2 行为变更：列出所有直接构造 RecentMemory 或绕过 strategy 读写 recent 的测试与
   生产调用点（含 hardware/、ui/），验证"三个调用点"清单是否完备。
5) 攻击双轨表 D3：给出一个在 Phase 6 与 Phase 7 之间插入紧急 feature 的现实场景，评估 v1/v2
   并存被拉长时的具体腐烂路径与守卫缺口。
6) 攻击 Phase 0 #4 的 xfail 设计：strict xfail 在 conftest 的 os.environ 隔离与 config 原地突变
   （chat_engine.set_interlocutor_name）交互下是否可能 flaky。
7) 检查每个 phase 的"必跑测试"是否足以检测该 phase 特有的失败模式，指出至少一个漏检面。
输出：证伪成功清单（带 file:line）、计划需修订条目、以及你认为应该调整的 phase 顺序及理由。
不许泛泛认同；每条结论必须给出可执行的验证步骤。
```

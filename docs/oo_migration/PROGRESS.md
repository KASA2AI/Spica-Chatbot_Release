# OO Migration 收口日志

> 追加式日志：每个 phase 收口（或回滚）时在文末追加一条，禁止改写历史条目。
> 条目模板见下。状态板在 `README.md`。

## 条目模板

```markdown
## Phase N — <名称>（<已收口|已回滚>）
- 日期：YYYY-MM-DD
- commit：<哈希>（回滚则写 revert 哈希 + 原因）
- 实际修改文件：<清单，与计划白名单比对，超出项必须解释>
- 测试：python -m pytest tests -q → <真实结果>；parity gate：<结果>
- 旧 seam 归零验证：<grep/AST 验证命令与结果>
- 文档更新：<CLAUDE.md / GUARDRAILS / 本文件夹的 diff 摘要>
- 双轨表变化：<开钟/停钟了哪一行>
- 遗留/偏差：<无 或 列出并说明去向>
```

---

## Phase 0 — Characterization 保护面（只加测试）（已收口）
- 日期：2026-07-03
- commit：`336811f33c0dca6e39a1eaf6d2b22606ea7f15d9`
- 实际修改文件：`tests/test_app_host_tool_registration.py`、`tests/test_game_prompt_golden.py`、
  `tests/test_responses_probe_shape.py`、`tests/test_recent_memory_scope.py`
  （4 件全部新增，与计划白名单完全一致，零超出；零生产代码 / 既有测试 / 配置改动）
- 测试：`python -m pytest tests -q` → 1117 passed, 1 xfailed, 1 warning, 108 subtests passed；
  parity gate：`tests/test_game_prompt_golden.py` 连续两遍 4 passed，结果一致
  （golden 由 GameMemorySqliteAdapter tmp 路径 + 直写模型对象 + 固定错开时间戳生成，
  未使用 ManualGameMemory；expected 为测试内嵌字面量常量）
- 旧 seam 归零验证：不适用（test-only phase，无 seam 切换）
- 文档更新：无（Phase 0 白名单禁文档改动；本条目与 README 状态板由 Phase 0D 回写）
- 双轨表变化：无（未开钟/停钟）
- 遗留/偏差：
  1. recent 跨角色污染以 strict xfail 记账（`test_recent_isolated_across_characters`），
     归 Phase 2 的 MemoryScopeStrategy 转绿，届时摘 xfail；
  2. 计划书 `MIGRATION_PLAN.md` §Current Approval State 表未同步（不在 Phase 0D 白名单内，
     留待后续获批窗口或人工同步）。

## Phase 1 — galgame prompt_sections 出走 stages（已收口）
- 日期：2026-07-03
- commit：`da8f29b47384ddaaae132542fd63a65cbfb79733`
- 实际修改文件：`spica/galgame/prompt_sections.py`（新增）、`spica/runtime/stages.py`、
  `tests/test_layering.py`、`CLAUDE.md`（仅 galgame 布局清单两行；文末 Agent skills hunk 为
  unrelated WIP，经 `git apply --cached` 单 hunk 选择性 stage 排除在 commit 外）、
  `docs/DEVELOPMENT_GUARDRAILS.md`——与计划白名单完全一致，零超出
- 测试：
  - `python -m pytest tests/test_game_prompt_golden.py -q` → 4 passed
  - `python -m pytest tests/test_retrieve_game_context_node.py tests/test_game_context_in_chain.py
    tests/test_current_line_injection.py tests/test_layering.py -q` → 30 passed, 18 subtests passed
  - `python -m pytest tests -q` → 1117 passed, 1 xfailed, 1 warning, 108 subtests passed
  parity gate：Phase 0 `test_game_prompt_golden.py` active/offline full-section golden 字节等价（零 diff）
- 旧 seam 归零验证：
  - `stages.py` 中 `_section` / `_format_progress` / `_format_summaries` / `_format_buffer` /
    `_format_relations` / `_format_choices` / `_format_beats` / `_build_game_context_sections` /
    `_should_inject_companion` / `_COMPANION_INTENT` / `_GAME_CONTEXT_ACTIVE_SUMMARY_LIMIT` 已全部迁出；
  - `retrieve_game_context_node`、gate（`_game_context_mode` / `_resolve_game_target` / `_parse_*` /
    `_GALGAME_CONVERSATION_PREFIX` / `_OFFLINE_COMMAND_INTENTS`）与 `analyze_screen_attachment`
    patch 点仍在 `stages.py`；
  - `prompt_sections.py` import 白名单经 AST 审计恰为 `json` / `typing.Any` /
    `DEFAULT_INTERLOCUTOR_NAME`（审查曾拦下多余的 `from __future__ import annotations`，已删）。
- 文档更新：`CLAUDE.md`（§2 galgame 布局加 `prompt_sections.py`）与
  `docs/DEVELOPMENT_GUARDRAILS.md`（§9 落点说明 + 必读清单）已随 Phase 1 commit 同 commit 更新；
  本条目与 README 状态板由收口文档回写补记
- 双轨表变化：无
- 依赖边声明：本 phase 诞生 runtime → galgame 首条 import 边
  （`stages.py` → `spica.galgame.prompt_sections`）；模块级无环论证——`spica/galgame/__init__` 只
  re-export 纯数据 models、`prompt_sections` 不回指 `spica.runtime`——由
  `test_layering::test_spica_packages_import_cleanly`（15 包 import）gate 常驻覆盖
- 遗留/偏差：`CLAUDE.md` 文末 Agent skills hunk 是 unrelated WIP，未包含在 Phase 1 commit；
  Phase 2 未执行

## Phase 2 — CharacterScope + scoped recent + MemoryScopeStrategy（已收口）
- 日期：2026-07-03
- commit：`26314a2da7b0c88b4de622aa9d330d43d5cb7224`（前置 plan amendment：`3128d8e`——
  白名单补 `prompt_sections.py` / `test_memory_commit.py` 两缺口 + spy append 爆炸半径 rg）
- 实际修改文件（与修订版白名单完全一致，零超出）：
  - 生产 7 件：`spica/runtime/scope.py`（新增：DEFAULT_CHARACTER_ID + CharacterScope +
    character_scope_from_config + MemoryScopeStrategy）、`spica/runtime/stages.py`、
    `spica/runtime/memory_commit.py`、`spica/core/chat_engine.py`、`spica/host/app_host.py`
    （14 处身份默认值 → `character_scope` property）、`spica/host/agent_assembly.py`（仅一行
    改常量 import）、`spica/galgame/prompt_sections.py`（仅限定范围：身份参数化，未 import
    `spica.runtime.*`）；
  - 测试 8 件：`test_recent_memory_scope.py`（摘 strict xfail，红转绿）、
    `test_memory_commit_scope.py`（recent append 断言改 scoped，隔离性断言保留）、
    `test_cancellation.py` / `test_no_comment_gate.py` / `test_proactive_turn.py` /
    `test_streaming_pipeline.py`（7 处裸 get_recent 改读 scoped 桶，含 2 处负向断言）、
    `test_memory_commit.py`（仅 :89 spy 期望）、`test_memory_scope_strategy.py`（新增 9 用例：
    语义 4 + retrieve/commit 对称 2 + clear 对称 1 + rename live-read 2）；
  - 文档 2 件（与代码同 commit，迁移原则 3(d)）：`CLAUDE.md`（§2 表 recent/scope 两行，
    Agent skills WIP hunk 经单 hunk 选择性 stage 排除）、`docs/DEVELOPMENT_GUARDRAILS.md`
    （§11 旧 P1 裸 key 注记改已修复 + 必读加 scope.py）
- 测试：
  - memory 族 targeted（8 文件）→ 29 passed
  - 白名单 4 测试 + turn_contract → 39 passed
  - `test_game_prompt_golden.py` → 4 passed（身份参数化后字节等价保持）
  - `python -m pytest tests -q` → 1127 passed, 1 warning, 108 subtests passed
    （= 1117 基线 + 9 新增 + 1 xfail 转绿，0 xfailed）
- 旧 seam 归零验证（退出 grep）：
  - `rg -n 'or "spica"|or "麦"' spica` → **零命中**（比计划书「仅剩 scope.py 1 处」更严：
    回退全部经常量，字面量模式在 spica/ 下绝迹）；
  - `rg -n 'character_id[^=]*=\s*"spica"|user_id[^=]*=\s*"麦"' spica --type py` → 仅
    `session.py:151-152` + `companion_controller.py:94-95`（计划书显式豁免），零计划外命中
- 文档更新：见上「文档 2 件」；本条目与 README 状态板由收口文档回写补记
- 双轨表变化：D5（裸 recent key vs strategy scoped key）零并存承诺兑现——三生产调用点
  （stages 读 / memory_commit 写 / chat_engine 清）一次 flip，对称性测试常驻
- 关键设计记录：
  - MemoryScopeStrategy **live-read** `config.character`（调用时现算；`set_interlocutor_name`
    的原地突变即时可见，rename 特征测试常驻）；不给 TurnDeps 挂 scope 字段（拍板保持）；
  - recent read/write/clear **三点对称**（`clear_memory` 的 recent/LTM 不对称已修齐）；
  - LTM `effective_memory_conversation_id`（§27①）语义不变，scope 三元组与旧手拼逐字节同值；
  - `prompt_sections.py` import 白名单 **3→2 收紧**（`json`/`typing.Any`；身份由 stages 侧
    resolve 后参数传入，Phase 1 import 边界原样保持）；
  - `"::"` 单一居所仍是 `scoped_conversation_id`（scope.py 只 import 沿用，adapter 未改，
    未向其他 runtime 模块扩散）
- 遗留/偏差：
  1. `tests/test_memory_pipeline_e2e.py:14` 模块 docstring 尾句（"recent stays on the bare
     conversation_id"）在 Phase 2 后过时——该文件不在白名单且其测试全绿，留后续文档轮处理；
  2. Phase 3+ 未执行。

## Phase 3 — PromptContextContributor seam（已收口）
- 日期：2026-07-03
- commit：`d7865612044ac79dc16a3c1a47adc8edd5203968`（前置 plan amendment：`57b7f3f`——
  docs 白名单逐文件明确 CLAUDE.md + GUARDRAILS §5/§9-4）
- 实际修改文件（与修订版白名单完全一致，10 件，零超出）：
  - 新增 3 件：`spica/runtime/prompt_context.py`（Protocol：name/priority/mode(request)/
    sections(ctx,deps,mode)，typing-only 零 spica 边）、`spica/galgame/context_contributor.py`
    （gate/target 解析自 stages 逐字迁入 + `GalgameContextContributor` 单例）、
    `tests/test_prompt_context_contributors.py`（13 用例守卫）；
  - 修改 7 件：`spica/runtime/stages.py`（gate helpers 删除；通用 `contribute_context_node`
    57 行 + 纯赋值别名）、`spica/runtime/deps.py`（`context_contributors` 字段 +
    `__post_init__` galgame auto-fill，函数级懒 import）、`orchestrator.py` / `sync_chain.py`
    （各 import+调用两行换名）、`tests/test_layering.py`（TRANSFORM_LAYER_FILES 增两新文件，
    只扩域）、`docs/DEVELOPMENT_GUARDRAILS.md`（仅 §5 决策树 + §9 第 4 条改向）、
    `CLAUDE.md`（仅 Phase 3 两个 hunk：§2 表 contributor 行 + §3 表述改向；文末 Agent skills
    WIP hunk 经选择性 stage 排除在 commit 外）
- 测试：
  - `test_game_prompt_golden.py` → 4 passed（字节等价 + (d) span 语义基线）
  - 三直调（retrieve/current_line/reaction_wiring）→ 31 passed, 3 subtests（**零改动**硬 gate）
  - 新守卫 + layering → 17 passed, 15 subtests
  - game_context_in_chain + turn_contract → 12 passed
  - `python -m pytest tests -q` → 1140 passed, 1 warning, 108 subtests passed
    （= 1127 基线 + 13 新守卫用例，零 fail/skip/xfail）
- exit conditions 逐条：① orchestrator/sync_chain 调新名 ✓；② 三直调测试零改动全绿 ✓；
  ③ N1 扫描含两个新文件 ✓；④ CLAUDE/GUARDRAILS 决策树同 commit 改向 ✓
- 双轨表变化：D2 开钟即停——别名**永久保留**（成本≈0，Decision Log「保留为 facade」项），
  防再生长守卫（纯赋值 AST + node 行数上限 65 + span 名钉死 + auto-fill 恰一项）常驻
- 关键设计记录：
  - `PromptContextContributor` Protocol：gate 签名只收 request（结构性防线）；实现方结构性
    满足、不 import Protocol；
  - 注册：`TurnDeps.context_contributors=None` → galgame 兼容 auto-fill（永不长第二项）、
    显式 `()` 关闭（字节级 no-op）、显式 tuple 原样尊重——未来 domain 必须经 assembly 显式注册；
  - node 保留旧 span/timing 名 `retrieve_game_context_node(_ms)`（三处 timing 断言不改自绿）；
  - **prior-error no-op 修正**（重审 P1）：`ctx.error` 检查先于 deps 构造，`(ctx, None, None)`
    直调形态保持旧语义并有 PriorErrorCompatTest 钉死；
  - 异常隔离：`mode()` 抛异常按 none + WARNING（坏 gate 不炸普通聊天、不拖垮同伴 contributor）；
    `sections()` 抛异常 span 保留、注入为空、不炸 turn；
  - runtime→galgame 的模块级 import 边随 gate 迁出而消失（Phase 1 的 stages→prompt_sections
    边移交给 contributor），deps 侧仅函数级懒 import——15 包 import 环检测常驻。
- 遗留/偏差：
  1. `docs/DEVELOPMENT_GUARDRAILS.md` §9 必读列表仍写 `spica/runtime/stages.py(gate)`——
     gate 已迁 contributor，措辞过时（重审 P2；授权范围仅 §5 + §9 第 4 条），留后续文档轮；
  2. `spica/galgame/prompt_sections.py:5-7` docstring「gate half stays in stages.py」过时
     （forbidden 文件）；
  3. `spica/runtime/context.py:56` 注释提及 `stages._GALGAME_CONVERSATION_PREFIX` 过时
     （forbidden 文件）；
  4. 已知契约收窄（如实记录）：`(services=None, deps=None)` 且**无**先行错误的 none-mode
     直调形态，旧实现静默 no-op、新通用 node 需 deps 问 contributors——结构上不可保留
     （零调用方、零测试依赖）；错误路径的对应容忍已保留并钉死；
  5. Phase 4R / 4+ 未执行。

## Phase 4R — registry ToolEntry NamedTuple（微 phase）（已收口）
- 日期：2026-07-03
- commit：`983c8747bfec8325bc39147728d045c8cb4fb37b`
- 实际修改文件（与计划白名单完全一致，零超出）：`spica/plugins/registry.py`（+52/-34）、
  `tests/test_registry.py`（+87，只加断言，既有 3 用例零改动）
- 测试：
  - `tests/test_registry.py` → 10 passed（3 既有 + 7 新增：`ToolEntry._fields` 形状 pin /
    显式元数据读取器 / 未注册名默认值 / 非法 effect ValueError / available False→True 供给
    翻转 + 谓词抛异常隐藏不炸（该分支首次有覆盖）/ 同名重复注册覆盖）
  - `tests/test_app_host_tool_registration.py` → 5 passed（Phase 0 #1 **零改动自绿**——
    该文件当初即为本 phase 设计的公共接口回归判据）
  - 工具族（chat_tool_round / tool_chain_rounds / no_static_tool_schemas / sing_song /
    watch / note）→ 60 passed, 6 subtests（消费方零改动）
  - `rg -n 'entry\[[0-9]\]' spica/plugins/registry.py` → 零命中（7 元解包循环同步归零）
  - `python -m pytest tests -q` → 1147 passed, 1 warning, 108 subtests passed
    （= 1140 基线 + 7 新用例，零 fail/skip/xfail）
- exit conditions 逐条：① registry.py 内元组索引访问消失 ✓（`entry[n]` 与解包循环双 grep
  归零）；② 对外 API 零变化 ✓（八个公共入口签名未动；Phase 0 #1 + 工具族零改动全绿）
- 双轨表变化：无（内部表示重构，无 seam 切换；解锁 Phase 4——消除元组/NamedTuple 双索引窗口）
- 关键设计记录：
  - `ToolEntry` 字段顺序与历史 7 元组一致（`schema, handler, available, intent_gated,
    chainable, compact_output, effect`）——NamedTuple ⊃ tuple，下标语义纵深兼容；
  - `_tools` 类型注解 12 行嵌套 tuple → `dict[str, ToolEntry]`；
  - `register_tool` 关键字构造，schema verbatim / effect 三值校验 / name 解析行为不变；
  - 七处读取访问点全部改命名字段；新元数据必须走命名字段（`_fields` pin 防匿名加宽回潮）；
  - 预扫验证：`_tools` 在 registry.py 之外零引用，内部表示自由更换无隐蔽依赖。
- 遗留/偏差：无；Phase 4 / 5+ 未执行。

## Phase 4 — ReactionScoringPolicy + reaction assembly（已收口）
- 日期：2026-07-03
- commit：`d5dde5770792b4f900a5a54014ea90cc0894e8b3`（前置 plan amendment：`3240fc8`——
  白名单补 `test_reaction_config.py` 缺口 + `test_reaction_judge.py` lexicon seam 迁移范围 +
  三条 facade patch 有效性退出条件 + docs 逐处点名）
- 实际修改文件（与修订版白名单完全一致，7 件，零超出）：
  - 新增 3 件：`spica/galgame/reaction_scoring.py`（ReactionScoringPolicy：judge 调用 / 冷却
    状态 / lexicon mtime 热重载缓存 / 失败降级逐字下沉；依赖全 provider live-read + clock 注入）、
    `spica/host/assemblies/__init__.py`（装配约定 docstring）、`spica/host/assemblies/reaction.py`
    （install + new_reaction_judge / judge_llm_adapter / build_reaction_engine 三本体逐字迁入）；
  - 修改 4 件：`spica/host/app_host.py`（净 -190 行：initialize 两行 → `reaction_assembly.
    install(self)` 一行；五方法改薄委托；三常量与三状态属性迁出；`_lexicon_fallback` 经 rg 证
    零外部引用后随逻辑整体迁入 policy 不留委托）、`tests/test_reaction_judge.py`（clock 注入
    改造 + 常量 import 随迁 + DegradeFallbackTest lexicon patch 迁 policy seam + 新增
    PatchValidityTest 三用例）、`tests/test_reaction_config.py`（热重载/缓存两用例迁直驱
    policy，monkeypatch 点改 `spica.galgame.reaction_scoring.load_reaction_lexicon`，断言值
    逐条保持）、`docs/DEVELOPMENT_GUARDRAILS.md`（§12b 装配模板 + §3.1 禁新增 per-domain
    方法注记）
- 测试：
  - `tests/test_reaction_judge.py` → 31 passed（28 既有 + 3 新 patch 有效性）
  - reaction 全族 + cutover + Phase 0 #1（9 文件）→ 85 passed
  - Phase 3 / golden / turn_contract / layering smoke → 29 passed, 15 subtests
  - `python -m pytest tests -q` → 1150 passed, 1 warning, 108 subtests passed
    （= 1147 基线 + 3 新用例，零 fail/skip/xfail）
- exit conditions 逐条：① reaction 接线全部出 app_host（initialize 仅剩 install 一行；残留 =
  五薄委托 + 计划书明确留 host 的写权限闭包；`_reaction_lexicons`/`_mtimes`/`_judge_last_at`/
  `def _lexicon_fallback` 在 app_host 零命中）✓；② judge 冷却/降级语义逐断言保持（冷却三段 +
  judge.calls==2、fallback lexicon 阈值表、热重载 4→9、reload 计数、窗口读、key/base_url/
  effort 回退树——断言值全部未动全绿）✓；③ **三条 facade patch 有效性常驻**（install 经
  `host._new_reaction_judge()` / `host._build_reaction_engine()`；new_reaction_judge 经
  `host._judge_llm_adapter()`——sentinel 用例 PatchValidityTest 常驻，clock 注入用例不可假绿）✓
- 双轨表变化：**D4 开钟**——AppHost 薄委托（`_reaction_scorer` / `_reaction_lexicon_for` /
  `_new_reaction_judge` / `_build_reaction_engine` / `_judge_llm_adapter`）最长并存 1 个 phase，
  Phase 5-c2 删除并同 commit 迁移 cutover patch 目标
- 审查发现与修复记录：实现首版把 GUARDRAILS §12b 插进了 §12 的 ```text 代码块内部（CommonMark
  中带 info string 的 fence 不能作闭合），致 §12b 被吞成字面文本、悬空 fence 吞掉 §13——重审
  P1 拦下后已修（§12 补闭合 fence、删悬空 fence），全文件 24 fence 成对、`git diff --check` 干净
- 遗留/偏差：无新增；Phase 5+ 未执行（薄委托未删、cutover patch 目标未迁、`test_no_dict_config`
  未扩）。

## Phase 5 — deps 单轨化（stages/memory_commit 禁区版）（已收口）
- 日期：2026-07-03
- commit：`7a352d1ac748b90986494890b300259c1a70a732`（前置 plan amendment：`521f882`——
  白名单补 4 个 memory 族测试缺口 + 5-c2「删委托」改「转长寿 facade」+ 守卫行级临时豁免 +
  `test_responses_probe_shape` 保值等价注记）
- 实际修改文件（与修订版白名单完全一致，11 件，零超出）：
  - 生产 5 件：`spica/runtime/deps.py`（`recent` / `llm_ready` / `available_tool_schema_count`
    三字段 + bridge 灌入，getattr 防御式读取保持旧 `or` 短路对最小 fake 的惰性语义）、
    `stages.py`（`:148` recent、call_llm_node deps-first + `if not deps.llm_ready:`、count、
    `_tts_adapter_name` 收 deps、build_visual/synthesize → `deps.visual`/`deps.tts`，两处过时
    「transitional carrier」注释同步修正）、`memory_commit.py`（`deps.recent`）、
    `orchestrator.py`（`deps.visual`）、`tool_round.py`（**仅** count 一点）；
  - 测试 6 件：`test_llm_client_not_configured.py`（新，5-c0 特征测试：错误路径 code+message
    经真实同步链钉死）、`test_no_dict_config.py`（5-c2 守卫加强）、4 个 memory 族测试
    （仅 `recent=` 注入，断言零改动）
- 测试：
  - `python -m pytest tests -q` → 1152 passed, 1 warning, 108 subtests passed
    （= 1150 基线 + 1 特征测试 + 1 守卫豁免存活性用例）
  - targeted：`test_no_dict_config` + `test_moondream_default_cutover` → 8 passed（cutover
    零改动 gate）；golden_streaming/golden_sync/turn_contract/pipeline_smoke/chat_tool_round/
    watch/responses_probe_shape → 60 passed, 10 subtests；memory 族 + c0 → 30 passed
- exit conditions 逐条：① stages/memory_commit 禁区 AST `services.` 属性读均 `[]` ✓；
  ② `tool_round.py` 的 `services.llm_client` 仅剩 line 36（归 Phase 7-c2）✓；
  ③ `test_no_dict_config` 加强至 8 攻击面、ALLOWLIST 三件（visual_job/tts_job 注明 D1 永久
  facade 载体）、`(tool_round.py, 36, llm_client)` **行级**临时豁免（重审 P1 把 file+attr 级
  收紧为行级：同文件新增第二个读必红；存活性用例反向钉「豁免所指节点消失必红」）✓；
  ④ Phase 4 五薄委托原样保留、`test_moondream_default_cutover` 零改动全绿 ✓
- 双轨表变化：**D1 大幅收窄**——stages/memory_commit 禁区清零；orchestrator/tool_round 仅剩
  计划内遗留（`tool_round.py:36-37` llm_client 归 7-c2；`orchestrator.py:121` /
  `sync_chain.py:43` logger 为 observer 注入链既有参数，禁扩散）；**D4 停钟/改记**由
  `521f882` amendment 落定（长寿 facade，Phase 5 未删除委托，删除不再排期）
- 实施中拦截记录（如实）：`from_services` 初版无条件读三属性，打红白名单外
  `test_turn_deps.py`（5 failures）——根因是旧代码 `or` 短路使属性对最小 fake 惰性；在白名单内
  的 `deps.py` 以 getattr 修复（与相邻 `game_memory` 先例同形，生产值逐字节不变），
  `test_turn_deps` 零改动回绿
- 遗留/偏差：
  1. `tool_round.py:36-37` llm_client 判空 + 守卫行级豁免 → Phase 7-c2 同 commit 结清并删豁免
     （存活性用例强制）；
  2. `orchestrator.py:121` / `sync_chain.py:43` `services.logger` 留 D1 observer 注入链；
  3. `visual_job.py` / `tts_job.py` 为 D1 永久 facade 载体（ALLOWLIST 注明，文件未改）；
  4. Phase 6a+ 未执行（D3 时钟未起跳）。

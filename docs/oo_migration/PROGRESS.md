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

## Phase 6a — TextModel + BoundModel + summarizer/judge 收编（已收口）
- 日期：2026-07-03
- commit：`22505421c37d2b33344791b2f2ae43fabc3b94d8`（前置 plan amendment：`00d4852`——四轮累积：
  6a/6b 白名单按 Phase 4/5 后真实代码重校准（assemblies/reaction.py + test_app_host.py 入白名单）+
  docs 逐文件点名 + v2 契约判据层级/`complete_text` 兼容垫禁令 + 顶部状态表回填）
- 实际修改文件（与修订版白名单完全一致，13 件，零超出）：
  - 新增 3 件：`spica/ports/model.py`（TextModel Protocol + frozen `BoundModel(adapter, model)`，
    公开字段，无 `complete_text` 兼容垫）、`tests/test_text_model_contract.py`（Group A：client 层
    fake 驱动真实 adapter 的 complete Responses/Chat 双路径 + stream 无 tools shape；Group B：最小
    TextModel fake 的绑定/model 注入/禁兼容垫双向结构钉）、`tests/test_no_new_v1_llm_consumers.py`
    （D3 弱守卫：AST 扫 spica/galgame + spica/host 禁 LLMPort/v1 五方法族，存量白名单**空集**，
    liveness 8 形态自测 + v2 面反例 3 形态）；
  - 修改 10 件：`openai_compatible.py`（只增 v2 `complete`/`stream`，内部复用 v1 路径）、
    `summarizer.py` / `reaction_judge.py`（构造签名 `(llm, model)` → `(bound: BoundModel)`，调用点
    `self._bound.complete(prompt)`，LLMPort import 删除）、`app_host.py`（`_new_summarizer` 手工组
    BoundModel + 五处薄委托 docstring「deleted in Phase 5-c2」→「长寿 facade（D4 停钟）」纯注释修正）、
    `assemblies/reaction.py`（`new_reaction_judge` 组 BoundModel——adapter 仍经
    `host._judge_llm_adapter()`，patch-validity pin 不变；模块 docstring 同步修正；
    `judge_llm_adapter()` fallback 树零改动）、`test_app_host.py`（仅 dangling recovery fake 改
    adapter 侧 `complete(self, prompt, *, model)`）、`test_galgame_summarizer.py` /
    `test_reaction_judge.py`（fake 改形 + 构造包 BoundModel + `_llm`/`_model` 私有断言改
    `._bound.adapter`/`._bound.model`，断言语义逐条不变）、`CLAUDE.md`（§2 表加模型 port v2 一行；
    Agent skills WIP hunk 经 `git apply --cached` 单 hunk 选择性 stage 排除在 commit 外）、
    `docs/DEVELOPMENT_GUARDRAILS.md`（§10 模板加第 5 条 TextModel/BoundModel 注记）
- 测试：
  - targeted：summarizer+judge 全族 39 passed；app_host+契约+弱守卫+cutover 26 passed, 14 subtests；
    golden 三件 + turn_contract 16 passed, 4 subtests（字节不变）
  - `python -m pytest tests -q` → 1163 passed, 1 warning, 122 subtests passed
    （= 1152 基线 + 11 新用例【契约 7 + 守卫 4】；subtests 108 + 14【参数化 3 + liveness 8 +
    反例 3】，加法闭合，零 fail/skip/xfail）
- 旧 seam 归零验证：`rg -n "LLMPort|complete_text" spica/galgame/summarizer.py
  spica/galgame/reaction_judge.py` → 零命中；弱守卫空白名单常驻；
  forbidden 五件零 diff：`git diff --stat -- spica/runtime/tool_round.py
  spica/runtime/orchestrator.py spica/runtime/stages.py spica/ports/llm.py
  spica/runtime/deps.py` → 空（probe 族未迁，`stages.py:434,444` 博物馆未碰——D3 范围纪律自证）
- facade / patch 有效性：`test_moondream_default_cutover.py` **零改动 gate 通过**；
  PatchValidityTest sentinel 改经 `judge._bound.adapter` 必达（拦截语义不变，仅字段路径随
  BoundModel 形状调整）；`_new_summarizer`/`_new_reaction_judge`/`_judge_llm_adapter` 方法名与
  构建路径全部保留
- 文档更新：CLAUDE.md §2 + GUARDRAILS §10（与代码同 commit，迁移原则 3(d)）；docstring 附带清理
  6 处逐行点名（app_host `:385,:390,:434,:447,:451` + assemblies/reaction `:14-16`），diff 自证
  纯注释零可执行变更
- 双轨表变化：**D3 开钟**——自 Phase 6a 收口起跳：≤2 个已批生产 phase 内必须完成 Phase 7；
  6b 可与 Phase 7 对调，不占 D3 时钟
- 遗留/偏差：无新增；Phase 7 前生产链（orchestrator/tool_round）照旧 v1（计划内）；
  6b 的「施工前必须裁决」（ModelRouter vs `_judge_llm_adapter` patch-validity，方案 A/B/C）
  仍待批准前裁决。

## Phase 7 — ToolCallingModel 生产链 flip（已收口）
- 日期：2026-07-03
- commit（分段节奏 c0 → review → c1 → review → c2 → review，各自独立可 revert）：
  - **7-c0** `d791550685a96b348d9bf6a6306e5eee0e5d3333`（test-only）：新增
    `tests/test_stream_probe_edges.py`——三类特征 v1 下先绿（mid-stream error 实测信封
    `[status, error]`/无 done/浮出 fallback 异常/两次 create；followup cancel 工具恰一次/
    recent 零 append/流停在检查点；STREAM_RESET preamble 不进 answer 也不进 recent），
    全部 client 级 fake 驱动真实链；
  - **7-c1** `fbc6084dbdb59b26991f331028f9e1bcf49a71f8`：orchestrator 终答流
    `deps.llm.iter_response_text(request, ctx)` → `deps.model.stream(prompt, ctx)`（request
    dict 组装移入 adapter，字节同形）；`TurnDeps.model` 字段 + `__post_init__` auto-fill
    （显式传入不覆盖、`replace()` 身份保持、非 readiness 判据）；新增
    `tests/test_turn_deps_model.py` 五契约；
  - **7-c2** `c7a9e2bc76e91b49b79bc4cc0ff78809ad820b84`：tool_round 探针/followup/chain 全部
    经 `deps.model.probe / probe_stream / stream`（Optional-return 家族信号；lazy
    ToolProbeStream 构造零 client I/O；usage 禁双记账——Responses 经 result.usage 由 runtime
    obs 记、chat 留 adapter 内 `_record_usage`）；`tool_round.py:36` `services.llm_client`
    判空改 `deps.llm_ready`（文案字节不变）；**llm_ready 终局语义** = adapter OR client
    （adapter-only ready，双无才 not ready——契约两用例 + 5-c0 零改动重释为「无任何 LLM
    capability」）；`test_no_dict_config` TEMP_EXEMPT 清空（存活性用例转自动再武装）；
    新守卫 `tests/test_no_v1_llm_in_runtime.py`（十名禁面）+ 弱守卫升八方法族 +
    `tests/test_tool_calling_model_contract.py`（11 契约）；CLAUDE §2 / GUARDRAILS §10 改向；
  - **c2 后守卫加固** `57b900a5ca1cccbcd7f860c4fd315031a6224150`（三轮 review 累积）：
    v1 载体禁面全谱——`deps.llm` 精确属性读（`deps.config.llm.model` 零误伤）、`LLMPort`
    裸名、`.LLMPort` 任意别名属性、`import spica.ports.llm` / `from spica.ports.llm import`
    / `from spica.ports import LLMPort|llm` 三层 import；守卫 5 passed, 35 subtests
    （liveness：十名方法 10 + 载体正向 12 + 合法反例 13）；
    `ports/model.py` / `openai_compatible.py` docstring 后 Phase 7 语义校正（纯字符串）。
- 前置 plan amendments：`03d3961`（Phase 7 施工单重校准：十名禁面/lazy I/O/usage 禁双记/
  Optional-return/ProviderTraits 转 on-demand/分段节奏）+ `c9bae59`（readiness 终局语义 +
  scripts 扫描盲区制度化；同批 `f66a8b6` 修复 reaction_judge_report 6a 断裂）。
- 测试：
  - 全量 `python -m pytest tests -q` → **1191 passed, 1 warning, 160 subtests passed**
    （链路：1168 → c1 +5 → c2 +17/subtests+19 → 加固 +1/subtests+19【1190/141 → 1191/160】，
    逐段对账）；
  - targeted 全绿：ToolCalling/TextModel/turn_deps 契约、runtime v1 守卫（5 passed,
    35 subtests）、c0 stream probe edges（零语义漂移）、chat_tool_round / tool_chain_rounds /
    cancellation / no_comment / turn_contract / golden_streaming / golden_sync /
    Phase 0 #3（`test_responses_probe_shape` 零修改）。
- 旧 seam 归零验证：
  - `orchestrator.py` + `tool_round.py` 十名方法禁面 **零 AST 命中**（守卫常驻）；
  - v1 载体禁面同零：`deps.llm` / `LLMPort` / `.LLMPort` / 全部 import 形态；
  - `services.llm_client` 于 orchestrator/tool_round **归零**（`deps.py` bridge 合法保留）；
  - `test_no_dict_config` TEMP_EXEMPT = 空集；
  - 冻结区未迁自证：`stages.py`（`call_llm_node` 永久 v1 博物馆，不扫不迁）/ `sync_chain.py` /
    `test_responses_probe_shape.py` 零 diff；
  - 树外扫描（scripts/ui/hardware/agent_tools）：`reaction_judge_report.py` 已由 `f66a8b6`
    修复为 BoundModel 形态；`verify_watch_chain.py:38,226` 读 adapter 模块级私有
    `_prefers_chat_completions` 为诊断脚本**显名豁免**（adapter v1 面永久保留）。
- 文档更新：CLAUDE.md §2 模型 port v2 行改向「生产链已 v2」+ 双守卫指名（hunk 选择性 stage
  排除 Agent skills WIP）；GUARDRAILS §10 第 5 条删「与 Phase 7 前生产链」半句 + runtime
  十名禁面注记；`ports/model.py` / `openai_compatible.py` 模块 docstring 后 Phase 7 语义。
- 双轨表变化：**D3 停钟**——自 Phase 6a 收口起跳，Phase 7 在时钟内完成（6a 后第 1 个已批
  生产 phase，≤2 约束满足）。「非 OpenAI provider = 只写一个 v2 adapter」承诺闭环达成。
- 遗留/偏差（全部非阻塞，显名记账）：
  1. `orchestrator.py:10,:109` 两处 docstring/注释性 `deps.llm` 提及（AST 不可见、守卫不红），
     归后续 doc cleanup 轮；
  2. `verify_watch_chain.py` 诊断脚本读 `_prefers_chat_completions`——显名豁免，永久合法；
  3. `reaction_judge_report.py` 默认模型逻辑与生产 judge 疑似历史分歧（脚本
     `summary_model or llm.model` vs 生产 `reaction_judge_model or llm.model`）——非 Phase 7
     范畴，另立项裁决；
  4. Phase 6b 仍未批准/可选（D3 已停钟，不受时钟约束；批准前须先裁决「施工前必须裁决」小节）。

## 审查后小修 — Phase 7 收口后三方深度对抗审查（小刀 A/B/C）
- 日期：2026-07-04
- 背景：Phase 7 收口后由三个独立窗口对抗审查，合并定谳 6 项缺陷（BUG-1..6，全部经独立实测
  复核；无 P0/P1，今日生产零触发路径——host 恒配 client、博物馆零生产调用方）。
- **小刀 A（代码修复）**：BUG-2 `ToolProbeStream` 自引用环——consume 泵改模块级
  `_consume_probe_stream`（零 self 闭包 + exhausted flag 容器），取消弃流恢复 v1 的
  refcount 即时释放语义；回归测试 `test_abandoned_stream_closes_on_refcount_without_gc`
  （gc.disable 隔离 + 结构证：生成器帧不再引用 handle）。BUG-3 `from_services` 判空错位——
  llm 选择改显式 `is not None` 与 `llm_ready` 同源（falsy adapter-only 不再错绑
  `OpenAICompatibleAdapter(None)`）；回归测试 `test_falsey_adapter_only_bundle_binds_the_adapter`。
- **小刀 B（守卫加固，test-only）**：BUG-5 弱守卫镜像 runtime 守卫的包级载体禁面
  （`from spica.ports import LLMPort|llm`、`.LLMPort` 任意 receiver——`ports/__init__`
  re-export 为真实暴露面；liveness 正向 +5 / 反向 +4）。BUG-6 弱守卫与
  `test_no_dict_config` 补扫描根存在性 + 非空断言（防目录迁移后 rglob 空集 vacuous 绿，
  三守卫 liveness 自此对称）。
- **小刀 C（契约显名 + 记账 + 注释清理）**：BUG-1 博物馆契约特征测试
  `tests/test_sync_museum_contract.py` 两态钉死——adapter-only + v1 面通行（7-c2 终局语义
  抵达博物馆 gate 的显名承认）、纯 v2 adapter → `NODE_FAILED`（**契约记录非功能诉求**，
  博物馆只承诺 v1 面 adapter；冻结文件零改动）。BUG-4 `replace(deps, llm/config=…)` 陈旧
  绑定显名——`deps.py` 契约注释 + 特征测试 `test_replace_with_new_llm_keeps_old_binding_by_design`
  + MIGRATION_PLAN 6b「施工前必须裁决」追加。注释清理（纯文字零逻辑）：orchestrator 模块
  docstring 与 `:109` 注释 deps.llm→deps.model 改向、`context.py` 前缀注释
  stages→context_contributor 指向修正。
- 测试：全量 `python -m pytest tests -q` → **1198 passed, 1 warning, 169 subtests passed**
  （链路：收口基线 1191 → A +2 → B +2/subtests+9 → C +3，加法闭合）。
- 未修显名：BUG-7 AST 守卫动态形态逃逸（getattr/importlib/裸载体别名）= 全仓守卫固有边界，
  裁决不修（复合覆盖成立：别名载体调任何 v1 方法仍被方法名禁面抓回）；`from_services` 的
  `memory=… or …` 同族 truthiness 模式（无 ready 错位驱动，记录备裁）；GUARDRAILS §9:184
  `stages.py(gate)` 旧句（不在本轮允许文件，归下次 GUARDRAILS 触碰顺带）；
  `reaction_judge_report` 默认模型分歧（另立项）。

## Phase 6b — ModelRouter 收编 host endpoint 决策（已收口）
- 日期：2026-07-04
- commit：`633c0d2926f89082d68b479d05d74ddf66ed6e9b`（前置裁决 amendment：`118de31`——
  方案 A-ii + BUG-4「重建 deps」规则 + 施工硬约束四条 + 五组测试 gate，施工前落盘）
- 实际修改文件（与白名单完全一致，6 件，零超出）：
  - 新增 2 件：`spica/host/model_router.py`（`ModelRouter(host)`：构造 inert 零读零 I/O、
    duck-typed `Any` 不 import AppHost；`role_model` 三角色回退决策逐字保持历史表达式；
    `for_role` 组 BoundModel——judge 半恒经 `host._judge_llm_adapter()`；`judge_adapter()`
    = key/base_url/reasoning 回退树自 assemblies **逐字迁入**）、`tests/test_model_router.py`
    （7 单测：构造 inert（ExplodingHost）、三回退、summary 绑主 adapter、no-key 共享、
    **router 级 patch-validity**——sentinel 经 `for_role("judge").adapter` 必达）；
  - 修改 4 件：`spica/host/app_host.py`（`__init__` 挂 `self.model_router`；`_new_summarizer`
    改 `for_role("summary")`（None 守卫原样）；`_judge_llm_adapter` 委托体改指
    `router.judge_adapter()`，方法名/patch 语义不变；BoundModel import 随迁移除）、
    `spica/host/assemblies/reaction.py`（`new_reaction_judge` 仅替换组装行为
    `GalgameReactionJudge(host.model_router.for_role("judge"))`，双 guard 原样；
    `judge_llm_adapter` 函数体迁出删除（唯一调用方即 host 委托，已核验）；docstring 改向）、
    `CLAUDE.md`（§2 模型 port 行加 router 唯一居所一句；Agent skills WIP hunk 经单 hunk
    选择性 stage 排除）、`docs/DEVELOPMENT_GUARDRAILS.md`（§10 第 5 条加 router 注记）
- 测试：
  - `tests/test_model_router.py` → 7 passed；零改动硬 gate（reaction_judge + cutover +
    summarizer + app_host）→ 54 passed；守卫组（no_new_v1 + no_getenv + layering）→
    11 passed, 38 subtests；博物馆/deps 契约 → 11 passed
  - 全量 `python -m pytest tests -q` → **1205 passed, 1 warning, 169 subtests passed**
    （= 审查后小修基线 1198 + 7 router 单测，加法闭合）
- 旧 seam 归零验证：`rg -n 'reaction_judge_base_url|judge_api_key|summary_model' spica/host/app_host.py
  spica/host/assemblies/reaction.py` 决策逻辑命中归零（app_host 仅剩委托转发注释；assemblies
  零命中）——三处 endpoint/model 决策唯一居所达成
- facade / patch 有效性：`install` 仍经 `host._new_reaction_judge()`/`_build_reaction_engine()`；
  judge adapter 仍经 `host._judge_llm_adapter()`（`for_role("judge")` 回经委托，调用链单向
  无环）；PatchValidityTest 三 sentinel + JudgeKeySplitTest 六用例 + cutover 15-patch
  **全部零改动全绿**
- 双轨表变化：无新开钟；Open Questions #1（`_new_*` facade 删除时机）维持默认永久保留
- 遗留/偏差：无新增；BUG-4 按裁决未触发（构造期解析，无运行时切换）；遥测对齐仍挂
  真正换模型功能立项时；`reaction_scoring.py:130` log-only model 拷贝维持已挂账。
  **至此计划内全部 Y1 phase（0–7 含 6b）收口**；Phase 8/9 feature-triggered 待立项。

## Phase 8 — ActiveDomainRouter + WindowTarget/PrivacyGate + request 落点泛化（已收口）
- 日期：2026-07-04
- commit（seam 基建获提前批准，按 c0 → review → c1 → review → c2 → review → c3 分段）：
  - **设计裁决 amendment** `b2b9c32`（六项裁决 + 审查五修正：router push 模型/方案 C 组合/
    多 contributor 注册期排序/WindowTarget 纯身份/PrivacyGate 动态入参/sink 异常安全）；
  - **8-c0** `8b83657`（test-only）：`tests/test_domain_binding_contract.py` 五组保护基线
    （request lane 缺口、publish-LAST/clear-FIRST、galgame-only 闭包、watch 5 元组三 None 路、
    system turn 域识别 = conversation_id 且 `source` 结构性不可达 gate）；
  - **8-c1** `cf6c415`：`ActiveDomainRouter`（inert/加锁/priority/平手取最近 + WARNING once/
    no-throw 契约/`current_for` 过滤读）+ `context.py` 泛化（`DomainContextRequest`
    frozen+kw_only、`DomainTurnBinding`、`domain_context_requests` tuple、`MappingProxyType`
    不可变前缀注册表）+ `chat_engine._request` isinstance 三路分派（galgame legacy lane
    字节等价、generic lane、未知形状 fail-open）+ controller sink 两点 best-effort +
    app_host 接线（engine provider 改指 `router.current`，`_companion_game_binding`
    保持 galgame-only 零改动）；
  - **8-c2 白名单 amendment** `c5647d1`（爆炸半径 rg 抓到两个裸 5 元组 provider 必改点，
    实施窗口停工上报后扩权——`test_chat_tool_round.py` / `verify_watch_chain.py` 限定入单）；
  - **8-c2 实现** `e7bbb7f`：`spica/runtime/window.py`（WindowTarget 纯身份值对象 +
    WatchContext NamedTuple）+ `spica/galgame/privacy_gate.py`（唯一评估器：ocr purpose =
    check_safety + OVERLAY_COVERS 逐字迁入、watch purpose = 仅状态门、动态输入逐调用、
    owner_domain/未知 purpose loud ValueError）+ ocr_loop/watch 工具/app_host 等价迁移 +
    `test_privacy_gate.py` 8 用例；`test_ocr_loop.py` 行为等价强至**零改动自绿**。
- 测试：全量 `python -m pytest tests -q` → **1245 passed, 1 warning, 170 subtests passed**
  （链路：8 前基线 1218 → c0 +13 → c1 +19/subtests+1 → c2 +8，逐段对账）；
  `python scripts/verify_watch_chain.py` 离线活体全链通过（WatchContext 形 provider）。
- exit conditions 逐条：galgame 经 router 发布且全族绿 ✓（controller sink 镜像
  publish-LAST/clear-FIRST，exploding sink 无残留）；两处隐私评估逻辑收编进 gate、
  `session.py` 状态集单一居所保留不删 ✓；域 conversation 前缀纪律入 GUARDRAILS（8-c3）✓；
  check→capture race 未收窄挂账声明在档 ✓。
- 行为边界（显名）：**co-watch feature 未实现、未立项**（Phase 8 只交付 seam）；watch
  purpose 不做 check_safety 的**不对称保留**；check→capture race **未收窄，继续挂账**
  （随 co-watch feature 或另立项）；多 contributor telemetry（单 span + metadata）实施
  推迟到 domain #2 contributor 真落地时。
- 文档更新（8-c3 同批）：README 状态板 / MIGRATION_PLAN 状态区 / GUARDRAILS 决策树新正路 /
  CLAUDE.md §2 架构地图两行（Agent skills WIP hunk 照例排除）。

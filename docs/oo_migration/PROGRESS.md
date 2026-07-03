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

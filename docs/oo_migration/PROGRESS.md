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

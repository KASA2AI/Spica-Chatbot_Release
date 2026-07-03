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

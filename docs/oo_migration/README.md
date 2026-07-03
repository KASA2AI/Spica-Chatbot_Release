# OO Migration 跟进文件夹

> 本文件夹是《Spica Long-Term OO Migration Plan》的唯一跟进居所。
> 任何实现窗口（人或 AI 会话）执行迁移 phase 前，必须先读本 README + `MIGRATION_PLAN.md` 对应
> phase 章节；收口后必须回写本 README 状态板 + `PROGRESS.md` 日志。
> 本文件夹只管**迁移工程**；co-watch/browser 等 feature 本身另行立项，不在此记账。

## 文件索引

| 文件 | 用途 |
|---|---|
| `MIGRATION_PLAN.md` | 计划书本体（**v2，唯一执行版本**）：Metadata、Current Approval State、原则、14 个目标 object 终态、Phase 0/0D/1–3/4R/4/5/6a/6b/7–9 施工单、Phase 0 实现提示词、双轨治理、风险登记、Decision Log（含人工拍板结果）、Open Questions、附录 |
| `MIGRATION_PLAN_v1.md` | v1 原文存档（已被 v2 取代，仅供追溯，不作执行依据） |
| `PROGRESS.md` | 逐 phase 收口日志（追加式，格式见该文件模板） |
| `README.md` | 本文件：状态板 + 使用规则 |

## 状态板

> 状态取值：`未批准` / `已批准待施工` / `施工中` / `已收口` / `已回滚`。
> 收口 = 满足计划书「Migration Principles」第 3 条的六项退出条件，并已写 `PROGRESS.md` 条目。
> 本状态板与计划书 §Current Approval State 同步维护（收口时两处同 commit 更新）。

| Phase | 名称 | 前置 | 触发条件 | 状态 | 收口 commit | 备注 |
|---|---|---|---|---|---|---|
| 0 | Characterization 保护面（只加测试） | — | 推荐立即批准（ready for approval） | 已收口 | `336811f33c0dca6e39a1eaf6d2b22606ea7f15d9` | 2026-07-03 收口：4 个新增测试文件；全量 gate `python -m pytest tests -q` → 1117 passed, 1 xfailed, 1 warning, 108 subtests passed；golden 单测连跑两遍均 4 passed 一致。详见 `PROGRESS.md` |
| 0D | Phase 0 文档收口（微 phase） | 0 收口 | Phase 0 收口后 | 已收口 | `2bc96c3d8d5fffb49153a6c4302d1ef66cf971e3` | 2026-07-03 已回写 Phase 0 状态板与 PROGRESS；见 Phase 0D 提交 |
| 1 | galgame prompt_sections 出走 stages | 0 | 批准 | 已收口 | `da8f29b47384ddaaae132542fd63a65cbfb79733` | 2026-07-03 收口：`prompt_sections.py` 从 `stages.py` 出走（gate + node 留守）；Phase 0 golden 字节等价；全量 gate 通过（1117 passed, 1 xfailed）。详见 `PROGRESS.md` |
| 2 | CharacterScope + scoped recent + MemoryScopeStrategy | 0（排在 1 后） | 批准 | 已收口 | `26314a2da7b0c88b4de622aa9d330d43d5cb7224` | 2026-07-03 收口：CharacterScope + MemoryScopeStrategy 落 `spica/runtime/scope.py`；recent 桶改 character scoped（读/写/清三点对称，全计划唯一主动行为变更）；全量 gate 1127 passed。详见 `PROGRESS.md` |
| 3 | PromptContextContributor seam | 0,1 | 批准 | 已收口 | `d7865612044ac79dc16a3c1a47adc8edd5203968` | 2026-07-03 收口：PromptContextContributor seam 落地（方案 a·galgame 兼容 auto-fill）；`retrieve_game_context_node` 永久纯赋值别名；orchestrator/sync_chain 已调新名 `contribute_context_node`；全量 gate 1140 passed。详见 `PROGRESS.md` |
| 4R | registry ToolEntry NamedTuple（微 phase） | 0 | 批准 | 未批准 | — | 可穿插在 1/2/3 之间 |
| 4 | ReactionScoringPolicy + reaction assembly | 0,2,4R | 批准 | 未批准 | — | 含 patch 有效性退出条件 |
| 5 | deps 单轨化（stages/memory_commit 禁区） | 4 | 批准 | 未批准 | — | 三 commit：5-c0/c1/c2；删 Phase 4 薄委托 |
| 6a | TextModel + BoundModel + summarizer/judge 收编 | 5 | 批准 | 未批准 | — | 同 commit 落 D3 弱守卫 |
| 6b | ModelRouter 收编 host endpoint 决策 | 6a | 批准 | 未批准 | — | 可与 Phase 7 对调，不占 D3 时钟 |
| 7 | ToolCallingModel 生产链 flip | 5,6a | 批准（D3：6a 后 ≤2 个已批生产 phase） | 未批准 | — | 三 commit：7-c0/c1/c2 |
| 8 | ActiveDomainRouter + WindowTarget/PrivacyGate + request 落点泛化 | 3,4 | **co-watch feature 批准** | 未批准 | — | 含 context.py / chat_engine.py 受控改动预留 |
| 9 | ToolAuthority + ToolExecutionPolicy | 4,8 | **browser/media feature 批准** | 未批准 | — | 按需触发，不排日期 |

## 使用规则（实现窗口必读）

1. **一次只施工一个 phase**；phase 定义（白名单/禁改清单/gate）以 `MIGRATION_PLAN.md` 为准，
   施工中不得现场扩权——需要改「forbidden files」即停止并回滚（计划书「Migration Principles」第 4 条）。
2. **先计划后动码**：实现窗口先输出「将改哪些文件 / 不碰哪些边界 / 测试计划」，等确认再动。
3. **收口六项**（计划书「Migration Principles」第 3 条）：全量绿 / parity 达标 / 旧 seam 禁区归零 /
   文档同 commit（test-only phase 例外走 Phase 0D，见第 5 条）/ revert 干净 / 双轨表更新。
   全过才能把状态板置「已收口」。
4. **收口回写**：更新本 README 状态板行（状态 + 收口 commit 哈希）+ 在 `PROGRESS.md` 追加条目。
   回滚同样要记（状态置「已回滚」+ 日志写原因）。
5. **守卫只增不减**；测试命令恒为 `python -m pytest tests -q`。
6. **双轨检查**：批准任何新 phase 前，先核对计划书 §7 登记表——有超期双轨则先收口旧账。
7. 本计划与 `CLAUDE.md` §1 铁律冲突时，以铁律为准（计划书设计上无冲突；发现冲突即是计划缺陷，
   停工上报而不是绕过）。

## 背景产物（会话报告，未落盘）

本计划由同一评估会话的三份报告推导而来：① 小刀重构评估（当前架构诊断 + Top3 小刀）；
② 长期 OO/Deep-Module 蓝图（五 seam 结论 + Evidence Map E1–E16 + 三档方案比较）。
①② 的关键证据（file:line）已内联进 `MIGRATION_PLAN.md`，故未单独归档；若需全文可从会话记录导出后
放入本文件夹（建议命名 `BLUEPRINT.md` / `EVALUATION.md`）。

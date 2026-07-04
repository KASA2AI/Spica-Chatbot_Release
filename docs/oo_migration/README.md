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
| 4R | registry ToolEntry NamedTuple（微 phase） | 0 | 批准 | 已收口 | `983c8747bfec8325bc39147728d045c8cb4fb37b` | 2026-07-03 收口：registry 内部 7 元组改 `ToolEntry` NamedTuple；对外 API 零变化（Phase 0 #1 零改动自绿）；全量 gate 1147 passed。详见 `PROGRESS.md` |
| 4 | ReactionScoringPolicy + reaction assembly | 0,2,4R | 批准 | 已收口 | `d5dde5770792b4f900a5a54014ea90cc0894e8b3` | 2026-07-03 收口：评分决策下沉 `reaction_scoring.py`、装配出走 `assemblies/reaction.py`（AppHost 仅剩 install 一行 + 薄委托）；冷却/降级语义逐断言保持；三条 facade patch 有效性常驻；全量 gate 1150 passed。**D4 双轨开钟**（薄委托 ≤1 phase，Phase 5-c2 删）。详见 `PROGRESS.md` |
| 5 | deps 单轨化（stages/memory_commit 禁区） | 4 | 批准 | 已收口 | `7a352d1ac748b90986494890b300259c1a70a732` | 2026-07-03 收口：三 commit 结构内容全落（c0 特征测试 / c1 deps flip / c2 守卫 8 攻击面 + 行级临时豁免）；禁区 AST services 属性读清零；D1 大幅收窄、D4 停钟由 amendment `521f882` 落定（薄委托转长寿 facade 未删）；全量 gate 1152 passed。详见 `PROGRESS.md` |
| 6a | TextModel + BoundModel + summarizer/judge 收编 | 5 | 批准 | 已收口 | `22505421c37d2b33344791b2f2ae43fabc3b94d8` | 2026-07-03 收口（前置 amendment `00d4852`）：TextModel + BoundModel 落 `spica/ports/model.py`；summarizer/judge 脱离 v1 LLMPort 改持 BoundModel（judge adapter 仍经 `host._judge_llm_adapter()`，facade/patch 语义不变）；弱守卫 `test_no_new_v1_llm_consumers` 同 commit 落地（空白名单 + liveness）；全量 gate 1163 passed。**D3 开钟**：自本收口起 ≤2 个已批生产 phase 内必须完成 Phase 7（6b 可与 7 对调，不占 D3 时钟）；6a 未偷做 Phase 7（forbidden 五件零 diff 自证）。详见 `PROGRESS.md` |
| 6b | ModelRouter 收编 host endpoint 决策 | 6a | 批准 | 已收口 | `633c0d2926f89082d68b479d05d74ddf66ed6e9b` | 2026-07-04 收口（前置裁决 amendment `118de31`：方案 A-ii + BUG-4「重建 deps」规则）：role/endpoint 决策唯一居所落 `spica/host/model_router.py::for_role`（summary/judge 模型回退 + judge 独立 endpoint 树逐字迁入）；judge adapter 仍恒经 `AppHost._judge_llm_adapter` patch seam（`for_role("judge")` 回经委托），PatchValidityTest + cutover **零改动**；router 构造 inert、duck-typed 无环；全量 gate 1205 passed, 169 subtests。详见 `PROGRESS.md` |
| 7 | ToolCallingModel 生产链 flip | 5,6a | 批准（D3：6a 后 ≤2 个已批生产 phase） | 已收口 | `57b900a5ca1cccbcd7f860c4fd315031a6224150` | 2026-07-03 收口（前置 amendments `03d3961` + `c9bae59`）：四 commit 链——7-c0 `d791550`（特征测试先行）→ 7-c1 `fbc6084`（orchestrator 终答流经 `deps.model.stream`）→ 7-c2 `c7a9e2b`（tool_round 探针/followup 经 BoundModel ToolCallingModel 面 + llm_ready 终局语义 + TEMP_EXEMPT 清空 + runtime v1 守卫）→ 守卫加固 `57b900a`（v1 载体禁面全谱 + docstring 校正）；runtime 十名禁面 + v1 载体（`deps.llm`/`LLMPort`/`.LLMPort`/三层 import）零命中；`stages.call_llm_node`/`sync_chain` 博物馆未迁；Phase 0 #3 零改动全绿；全量 gate 1191 passed, 160 subtests。**D3 停钟**：时钟内完成（6a 收口后第 1 个已批生产 phase）。2026-07-04 收口后三方对抗审查小修：BUG-2/3 代码修复（小刀 A）+ BUG-5/6 守卫加固（B）+ BUG-1/4 契约显名（C），基线 → 1198 passed, 169 subtests。详见 `PROGRESS.md`（Phase 7 条目 +「审查后小修」条目） |
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

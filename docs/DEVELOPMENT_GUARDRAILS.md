# Spica 开发护栏

> 给**未来的 Claude Code 会话**和人类开发者：怎么在正确落点做最小改动，不乱重构、不绕边界。
> 配套：`docs/FUTURE_FEATURE_PLAYBOOK.md`（按需求查落点）、`docs/ARCHITECTURE_FOR_ALGORITHM_ENGINEERS.md`（全局）、`docs/CODE_REVIEW_REPORT_CURRENT.md`（债务/风险）、`docs/REAL_ARCHITECTURE_MAP.md`（真实路径）。
> 本文规则均经第 1 趟全量测试（`python -m pytest tests -q` → 821 passed）背书。

---

## 1. 本项目的核心边界

Spica = 本地桌面语音陪伴 App（PySide6 overlay + 语音），核心是 galgame 陪玩。架构靠 5 道边界撑住，**全部有 AST/语义守卫测试钉死**：
- **唯一开口路径** `spica/runtime/turn.py::run_turn`——所有「她说话」都收敛到这。
- **UI 无关核心** `spica/`（禁 Qt）vs **GUI** `ui/`（只 Qt）。
- **配置单一入口** `spica/config/`（只它能 `os.getenv`）。
- **ports/adapters/registry**：换引擎=改配置名，不动核心。
- **工具副作用分级** read/write/act，act 权限闭合在 host。

破坏任意一条 = 破坏架构。怎么不破坏，看下面。

---

## 2. 改代码前必须读什么

| 你要做的事 | 必读 |
|---|---|
| 任何改动 | `CLAUDE.md` §1 铁律 + §「📌 新会话必读」10 条硬规则 |
| 加某类需求 | `docs/FUTURE_FEATURE_PLAYBOOK.md` 对应章节 |
| 动 run_turn/orchestrator/tool_round/ChatEngine/AppHost | 本文 §3.1 + `docs/ARCHITECTURE_FOR_ALGORITHM_ENGINEERS.md` §5/§9 |
| 动配置 | 本文 §7 + `docs/CODE_REVIEW_REPORT_CURRENT.md` §11 |
| 动 galgame | `CLAUDE.md` §4 + `docs/GALGAME_COMPANION_PLAN.md` |

---

## 3. 高危文件清单

### 3.1 极高危：改前**必须先写计划**（影响范围 + 修改文件 + 不碰的边界 + 测试计划），等确认再动
```text
spica/runtime/turn.py              # 唯一 emit 路径，全链命脉
spica/runtime/orchestrator.py      # 流式编排 + cancellation 检查点 + Sequencer 有序
spica/runtime/tool_round.py        # 工具轮 + 系统 turn 工具硬关断
spica/runtime/stages.py            # 12 段纯 transform + galgame gate
spica/runtime/context.py / deps.py # TurnRequest/Context/Deps 边界
spica/core/chat_engine.py          # 三入口汇入 run_turn + galgame 绑定 + 记忆管理
spica/host/app_host.py             # 组装根 + galgame/写权限 host 闭包（持权限）；OO 迁移 Phase 4 后
                                   # 禁新增 per-domain 方法——domain 装配走 assemblies（见 §12b）
spica/conversation/prompt_builder.py  # 拼 prompt 的唯一地方
spica/galgame/session.py           # galgame 唯一状态 owner（并发核心）
spica/plugins/registry.py          # 工具/能力注册元数据
spica/config/{schema,manager,env_roster}.py  # 配置解析（改前先 dump 基线）
```

### 3.2 中高危：可改但**必须补/更新测试**，且不得越过 port 边界
```text
spica/adapters/**                  # 各 port 实现；改了补对应 adapter 测试
spica/ports/**                     # 改接口=改所有实现+契约测试
memory/{recent,store,extractor}.py # 记忆实现体；补 test_memory_*
spica/galgame/{ocr_loop,summarizer,reaction,companion_controller}.py  # 补 galgame 测试
ui/controllers/** ui/workers/**    # 补 ui 测试（pytest.importorskip PySide6）
```

### 3.3 低危：允许小步改
```text
docs/**                            # 文档
新增一个隔离的 adapter（藏在已有 port 后，不动调用方）
新增测试（只加不删不放宽）
注释 / 类型标注 / 日志文案
```

---

## 4. 绝对禁止事项（10 条硬规则，与 CLAUDE.md §1 同效）

1. 不绕开 `run_turn` 让 Spica 开口（主动开口走 `stream_system_turn`→run_turn）。
2. `ui/` 不 new LLM/TTS/Memory/VLM 主服务（找 `AppHost` 要）。
3. `spica/` 不 import Qt/PySide。
4. 业务代码不 `os.getenv`（只 `manager.py`/`secrets.py`/`runtime_env.py` 可碰）。
5. 不新建第二套 LLM prompt 链（统一 `prompt_builder` + gated stage 注入）。
6. act 工具不直接 exec/eval/shell/任意路径/任意 URL（动作经 host 闭包白名单面）。
7. OCR 文本不直接成为用户消息（OCR → `GalgameCompanionSession` text stream）。
8. galgame 剧情不污染普通聊天 recent memory（独立 `game_memory` 库）。
9. 不删除/放宽守卫测试来让测试变绿。
10. 不大范围重命名/搬目录，除非先写迁移计划。

**执行规则（落地到具体动作）：**
- 改 `run_turn`/`orchestrator`/`tool_round`/`ChatEngine`/`AppHost` 前**必须先写影响范围**。
- 改配置前**必须检查** `schema.py` / `manager.py` / `env_roster.py` / `app.yaml` / `test_resolved_config_equivalence`，并先 `python scripts/dump_resolved_config.py --out <baseline>`、改完 `--diff` 零差异。
- 改/加工具前**必须说明** `effect`（read/write/act）。
- 新 **act** 工具**必须**走专用 port 白名单动作面，权限留 host 闭包（或明确授权的 adapter）。
- 新 UI 功能**必须**经 bridge/worker/Qt signal，后端线程**不准**直接操作 widget。
- 新 galgame 功能**必须**尊重 `GalgameCompanionSession` 是唯一状态 owner（只调公共方法）。
- 新主动开口**必须**走 `stream_system_turn` → run_turn，不自建播报通道。

---

## 5. 新能力落点决策树

```text
这个需求是……
├─ 换/加一个引擎（LLM/TTS/STT/Visual/Memory provider）？
│    → 写 adapter 实现已有 port → registry.register_*("名字", 工厂) → 改 app.yaml provider 名
│      不动 runtime/ui。（playbook §1/§2/§3/§10）
├─ 让 LLM 能调用一个新能力（查信息/做动作）？
│    → 工具：schema + handler → register_tool(effect=read|write|act)
│      write/act 的执行权限放 host 闭包。（playbook §4/§5）
├─ 要把某些上下文喂进 prompt？
│    → 写 contributor（domain 内新文件 + deps/assembly 注册，仿 spica/galgame/context_contributor.py），
│      gate 用请求字段判断，不跑第二次 LLM。绝不自己拼 prompt。（playbook §6/§19）
├─ 让她主动说话（事件触发）？
│    → 发 ProactiveTurnRequest → ProactiveTurnArbiter → stream_system_turn → run_turn。（playbook §7/§9）
├─ galgame 相关（OCR/选项/总结/反应/履历）？
│    → 经 GalgameCompanionSession 公共方法；OCR 串行、读 snapshot、不进 recent。（playbook §6/§7/§14）
├─ UI 显示/交互/播放？
│    → ui/ controller/worker，消费 RuntimeEvent，Qt signal 跨线程。（playbook §10/§20）
├─ 加配置项？
│    → schema.py 加 typed 字段（带默认）；需 env 则进 env_roster+manager。（playbook §11）
└─ 加记忆类型/字段？
     → MemoryConfig/MemoryScope/store/extractor；galgame 用 game_memory。（playbook §13）
```

---

## 6. 新工具开发模板

```text
1. 写 schema（OpenAI function schema）+ handler。read 工具放 spica/adapters/tools/ 或
   agent_tools/function_tools/<域>/；handler 失败抛 ScreenToolError（被包成 ToolError 信封）。
2. write/act 工具：handler 只做纯转发，真正动作放 host 闭包（app_host.py __init__ 里注册，
   闭包闭合执行权限/白名单/配置）。act 绝不 exec/eval/shell/任意路径。
3. register_tool(schema, handler, *, available=谓词|None, intent_gated=?, chainable=False, effect="read|write|act")
   - available：仅特定状态供给（如陪玩态）。
   - intent_gated：是否经词表预筛（只决定供不供给，绝不劫持消息）。
   - effect：必须如实声明。
   read 工具在 spica/host/builtins.py 注册；write/act 在 AppHost.__init__（要 host 闭包）。
4. 不准绕 registry 直接 dispatch（INVARIANT N5）。
必读：spica/plugins/registry.py、spica/runtime/tools.py、spica/adapters/tools/sing_song.py（act 范例）
测试：test_registry / test_chat_tool_round / test_no_static_tool_schemas + 新工具专属测试（仿 test_sing_song_tool）
```

---

## 7. 新配置开发模板

```text
1. 在 spica/config/schema.py 对应子模型加 typed 字段，带默认值（默认值=旧硬编码值，保证零 diff）。
2. 若要 env override：env 名进 spica/config/env_roster.py，在 manager._env_overrides() 映射。
   绝不在业务代码新开 os.getenv。
3. 消费方从注入的 config/deps.config 读，不自己解析文件。
4. 改解析逻辑前：python scripts/dump_resolved_config.py --out before.json；改完 --diff 必须零差异
   （除非你就是要改某个生效值，那要在 PR 说明）。
必读：spica/config/{schema,manager,env_roster}.py、data/config/app.yaml
测试：test_resolved_config_equivalence / test_no_getenv / test_config_manager / test_env_centralization
注意：song 节是 untyped override dict（D-3a），别误当 typed。
```

---

## 8. 新 UI 功能开发模板

```text
1. UI 只做显示/输入/播放/线程桥/交互。要后端能力就找 AppHost 拿现成服务，不 new 主服务。
2. 后端→UI：发 RuntimeEvent（或经 CompanionEventBridge），UI 用 Qt queued signal 接。
   后端线程绝不直接调 widget 方法（会段错误）。
3. 跨线程启动/拆卸要 marshaling 回 GUI 线程（仿 audio_controller 的 QTimer.singleShot defer、
   qt_overlay 的系统 turn marshal）。
必读：ui/qt_overlay.py、ui/controllers/{chat_stream_controller,companion_event_bridge,audio_controller}.py
测试：test_layering（Qt 隔离）+ 相关 ui 测试（顶部 pytest.importorskip("PySide6")）
```

---

## 9. 新 galgame 功能开发模板

```text
1. 状态变化只经 GalgameCompanionSession 公共方法（start/pause/resume/end/on_ocr_result/
   on_window_lost/on_choice_detected/on_user_reported_choice/on_summary_finished）。不碰它私有字段。
2. OCR 相关：串行「完成后等待」，复用 RapidOCR 单例 + _INFER_LOCK，不双加载模型。
3. 总结/问答读不可变 snapshot（锁内切 list），不持有可变 buffer 引用。
4. 要把游戏上下文进 prompt：经 PromptContextContributor（OO 迁移 Phase 3：galgame gate 在
   spica/galgame/context_contributor.py，prompt 段落构建在 prompt_sections.py，通用 node
   contribute_context_node——别名 retrieve_game_context_node 永久保留——在 stages.py），
   gate 用请求字段，不跑第二次 LLM。
5. OCR 文本绝不进 recent memory / 绝不直接成用户消息。游戏数据写 game_memory 独立库。
必读：spica/galgame/{session,ocr_loop,summarizer,companion_controller,prompt_sections}.py、
      spica/runtime/stages.py(gate)、spica/ports/game_memory.py、spica/adapters/game_memory/sqlite.py
测试：test_galgame_session / test_galgame_summarizer / test_retrieve_game_context_node / test_companion_*
```

---

## 10. 新模型 adapter 开发模板（LLM/TTS/STT/Visual/Memory/OCR/Screen）

```text
1. 已有 port？→ 直接在 spica/adapters/<kind>/ 写新实现，满足 port 方法签名。
   没有合适 port？→ 先在 spica/ports/ 定 port（接口），再写 adapter。
2. 在 registry 注册一个名字（builtins.py register_builtin_adapters 或插件 register()）。
3. 改 data/config/app.yaml 对应 provider 名启用。不动 runtime/ChatEngine。
4. adapter 内部别 import Qt、别 os.getenv（配置从构造参数/注入拿）。
5. turn 外文本型 LLM 消费者（summarizer/judge 一类）不直接依赖 LLMPort v1：走 spica/ports/model.py
   的 TextModel/BoundModel（OO 迁移 Phase 6a，host 侧手工组 BoundModel(adapter, model)）；v1 为冻结链
   保留，spica/galgame+spica/host 禁新增 v1 消费者（test_no_new_v1_llm_consumers 钉），runtime 禁
   v1/provider 家族十名（test_no_v1_llm_in_runtime 钉，OO 迁移 Phase 7）。
必读：spica/ports/<kind>.py、spica/adapters/<kind>/、spica/host/builtins.py、spica/plugins/registry.py
测试：对应 adapter 合同测试（仿 test_phase5_adapters / test_tts_adapters / test_stt_faster_whisper）
```

---

## 11. 新 memory 字段开发模板

```text
1. 调参（条数/预算字符）→ 加 MemoryConfig 字段（schema.py），消费方读 deps.config.memory。
2. 新存储字段 → 改 memory/store.py 表结构 + spica/adapters/memory/sqlite.py + 抽取规则 memory/extractor.py。
   注意命名空间 {character_id}::{conversation_id}（adapters/memory/sqlite.py），别破坏角色隔离。
3. galgame 侧记忆 → 用 game_memory 独立库，不混进角色 MemoryScope。
4. recent/LTM/clear 的 key 与 scope 一律经 spica/runtime/scope.py 的 MemoryScopeStrategy 推导
   （OO 迁移 Phase 2：recent 桶已按 {character_id}::{conversation_id}，旧「裸 conversation_id」
   P1 已修复），不要在调用点手拼 key 或身份默认值。
必读：spica/runtime/scope.py、spica/ports/memory.py、spica/adapters/memory/sqlite.py、memory/、spica/runtime/memory_commit.py
测试：test_memory_commit / test_memory_store / test_recent_memory / test_ltm_cross_restart / test_memory_commit_scope
```

---

## 12. 新 proactive / system turn 功能开发模板

```text
1. 域事件触发 → 造 ProactiveTurnRequest（directive 文本 + source + policy）。
2. 经 ProactiveTurnArbiter.try_speak（drop_if_busy）→ host 把 _start_turn 接到 ChatEngine.stream_system_turn。
3. stream_system_turn 内部走 run_turn（interaction_mode="system"，工具供给硬关断防自激）。
4. 不自建播报通道、不另起 prompt；台词角色化由 compose_system_directive_message + 正常 prompt 完成。
   答案为 NO_COMMENT 哨兵时会被 system_silent 吞掉（不播不写），这是正常行为。
必读：spica/core/proactive.py、spica/core/chat_engine.py(stream_system_turn)、spica/galgame/reaction.py(范例)
测试：test_proactive_turn / test_reaction_wiring / test_no_comment_gate
```

---

## 12b. 新 domain 装配模板（assemblies，OO 迁移 Phase 4 立范）

```text
1. 装配代码落 spica/host/assemblies/<domain>.py：install(host) + 各构建函数；不进 AppHost 方法体
   （AppHost 每 domain 预算 ≤15 行 = 一次 install 调用 + 薄委托）。
2. install() 与构建函数必须经 AppHost 薄委托构建（facade = 唯一构建路径）——patch 有效性测试常驻，
   仿 test_reaction_judge.PatchValidityTest 的 sentinel 用例，防「facade 存在但不在路径上」的假绿。
3. 评分/决策类逻辑下沉 domain 包（仿 spica/galgame/reaction_scoring.py 的 ReactionScoringPolicy）：
   依赖一律 provider live-read（lambda 现读 host 属性，不捕获 bound method/值），时钟等可注入。
4. 写权限闭包（beat writer / song request 等）留 host（铁律 #9），policy/assembly 只经它们转发。
必读：spica/host/assemblies/reaction.py、spica/galgame/reaction_scoring.py
测试：test_reaction_judge（patch 有效性族 + 冷却/降级）+ domain 专属测试
```

---

## 13. 每类改动必须跑哪些测试

| 改动 | 必跑（轻量守卫先跑） | 再跑 |
|---|---|---|
| runtime/turn/orchestrator/tool_round | test_turn_contract, test_layering | test_golden_streaming, test_golden_sync, test_cancellation, 全量 |
| 配置 | test_resolved_config_equivalence, test_no_getenv | test_config_manager, test_env_centralization, `dump_resolved_config --diff` |
| 工具 | test_registry, test_no_static_tool_schemas | test_chat_tool_round, test_tool_chain_rounds, 工具专属测试 |
| UI | test_layering | 相关 ui 测试（importorskip PySide6） |
| galgame | test_galgame_session, test_retrieve_game_context_node | test_galgame_summarizer, test_companion_* |
| 记忆 | test_memory_commit, test_recent_memory | test_memory_store, test_ltm_cross_restart |
| proactive | test_proactive_turn | test_reaction_wiring, test_no_comment_gate |
| 任何改动收尾 | **`python -m pytest tests -q`（全量）** | —— |

命令固定 `python -m pytest tests -q`，**绝不裸 `pytest`**（会扫 vendored GPT-SoVITS 崩）。

---

## 14. 什么情况必须先写设计，不能直接实现

- 动 §3.1 任一极高危文件。
- 新增/修改 turn 的 stage 顺序、cancellation 检查点、play unit/Sequencer 逻辑。
- 新增 port（接口层），或改已有 port 签名。
- act 工具、能起外部进程/占 GPU/写磁盘的能力。
- 跨子系统耦合（如让 galgame 影响普通聊天记忆/prompt）。
- 任何会改「生效配置值」的配置解析改动（先 dump 基线）。
- 大范围重命名/搬目录（先写迁移计划 + 影响范围 + 回滚）。

---

## 15. 什么情况可以小步修 bug

- 改动局限在一个 adapter/widget/worker 内部，不越 port 边界、不改公共签名。
- 有现成测试覆盖，或你能补一个最小复现测试。
- 不碰 §3.1 极高危文件的控制流。
- 修完 `python -m pytest tests -q` 全绿。
> 即便如此，仍要先一句话说清「改哪个文件、为什么、跑了什么测试」。**发现的更大问题只记录，不顺手大修。**

---

## 16. Claude Code 工作流程

### 16.1 先读
`CLAUDE.md` → 本文 → playbook 对应章节 →（需要时）architecture 文档。

### 16.2 先计划
输出：需求理解 / 影响范围 / 推荐落点 / **不会碰的边界** / 测试计划。**等用户确认。**

### 16.3 先列修改文件
明确「将改 A、B；不会动 run_turn/ChatEngine/prompt_builder/MemoryPort（除非已说明）」。

### 16.4 再实现
在确认的落点做**最小改动**。新能力优先 ports/adapters/registry。

### 16.5 再测试
按 §13 跑对应测试 + 全量。配置改动跑 `dump_resolved_config --diff`。

### 16.6 最后报告
改了什么 / 测试结果（贴真实输出）/ 已知遗留 / 没碰的边界。失败如实说。

---

## 17. 常见事故和预防

| 事故 | 根因 | 预防 |
|---|---|---|
| 启动拿到空 API key 永久定格（F19） | 构造期读 env，灌注在后 | 进程入口第一句 `load_secrets()`（test_env_centralization 钉） |
| 后端线程刷 UI → 段错误 | 跨线程直接调 Qt widget | 只发 RuntimeEvent + Qt queued signal |
| 配置改完行为悄悄变了 | 没对账 resolved config | 改前 dump 基线，改完 `--diff` 零差异 |
| recent memory 爆 / 普通聊天被剧情污染 | OCR 文本进了 recent | OCR → session text stream，绝不进 recent |
| 第二套回答链路和主链漂移 | 嫌 run_turn 麻烦另起 LLM 调用 | 一切开口走 run_turn；上下文用 gated stage 注入 |
| act 工具被诱导执行任意命令 | LLM 输出直接落地执行 | 动作经 host 闭包白名单面，LLM 只传参数 |
| 测试变绿但架构腐烂 | 删/放宽守卫测试 | 守卫红了改代码，永不改/删守卫 |
| 大改名断一片 | 没迁移计划就重命名/搬目录 | 先写迁移计划+影响范围，确认再动 |

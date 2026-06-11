# GALGAME_COMPANION_PLAN.md

> Spica Galgame 陪玩系统 —— 完整工程规格与 Phase 0 输入文档。
> 配套 `CLAUDE.md`。`CLAUDE.md` 是每次都读的铁律与架构地图；本文件是完整规格与实现计划。
> **两者冲突时**：`CLAUDE.md` §1「绝对铁律」不可被覆盖；其余细节以本文件明确写出的为准。

---

## 目录

1. 文档目的与使用方式
2. 系统总览与核心原则
3. 架构硬约束
4. 并发与所有权模型
5. 读路径：`retrieve_game_context_node`
6. 记忆系统设计（四类记忆）
7. 截图隐私、overlay 与窗口边界
8. OCR / screen pipeline 复用
9. 数据模型
10. OCR 运行与 stable line
11. Story buffer 定义
12. 崩溃恢复
13. 剧情总结
14. 选项识别
15. 用户问答流程与 conversation_id
16. 状态机与状态映射
17. 平台、启动与窗口匹配
18. OCR 校准流程
19. 人物关系表
20. 自然语言命令
21. 主动吐槽（v1 仅预留）
22. 错误修正与删除
23. R18 与数据边界
24. 实现 Phase 拆解（Phase 0–10）
25. 测试策略
26. v1 成功标准
27. **Phase 0 必须先回答的开放问题**（重要）

---

## 1. 文档目的与使用方式

本文件指导 Claude Code 为 Spica 增加 galgame 陪玩系统。要求：

1. **先输出**开发计划、数据模型、状态机、模块边界、并发模型、测试策略。
2. **不要直接改代码**（Phase 0 除外极小探查）。
3. **不要先做 OCR。** 先用「手动喂文本 → 游戏记忆 → prompt 注入 → `run_turn` 回复」验证主链路。
4. 确认架构边界、回答完 §27 的开放问题后，再进入实现。

这不是给聊天机器人加一个 OCR 功能，而是在 Spica 现有架构上增加一个**新运行模式**：

```
普通聊天模式：
  用户输入 → run_turn → Spica 回复

Galgame 陪玩模式：
  游戏窗口 → OCR 文本流 → 剧情状态/游戏记忆
           → 用户提问 / 选项识别 / 结束总结 → run_turn → Spica 回复
```

---

## 2. 系统总览与核心原则

1. 不破坏现有 `ChatEngine → run_turn` 唯一对话 turn 路径。
2. 不另起第二套 LLM prompt / 回答路径。
3. `spica/` 核心层不 import Qt / PySide / shiboken。
4. 新能力走现有 ports / adapters / registry 风格。
5. UI 负责框选、预览、确认；业务逻辑留在后端 session / domain 层。
6. v1 优先 Ubuntu + Bottles，Windows 只预留接口、不实现。
7. v1 先保证剧情记忆与进度准确，不做主动吐槽。
8. 默认配置的 LLM endpoint 视为支持 R18 内容，v1 不为 R18 设计特殊绕行 / 禁用 / 降级 / 拒答逻辑。

记忆四分（详见 §6）：游戏配置、剧情进度、陪玩共同记忆、用户/角色关系记忆**必须分开存储**，不互相污染。

---

## 3. 架构硬约束

### 3.1 对话路径

一次回复必须继续走：

```
ChatEngine.stream_voice / run_voice → run_turn → runtime stages
→ build_prompt → LLM → RuntimeEvent → UI 播放/显示
```

galgame 系统不得自己拼 prompt、不得自己直接调 LLM、不得重写第二套问答链路。

职责划分：

```
GalgameCompanionSession 负责：
  游戏 session 状态 / OCR loop / stable line / buffer 与未总结行 id /
  游戏进度状态 / 游戏记忆读写 / 选项事件记录

需要 Spica 回复时：
  仍调用 ChatEngine → 仍走 run_turn → 通过 gated stage 注入游戏上下文
```

### 3.2 ports / adapters

新增 port / adapter / domain 层的位置见 `CLAUDE.md` §2「架构地图 / galgame 新增」。**不要**另起 `spica/platform/` 平行体系；平台差异藏在 adapter 后面。UI 相关（框选、预览、OCR 测试展示、确认窗口、重新校准）一律留在 `ui/`，`spica/` 不 import Qt。

---

## 4. 并发与所有权模型

galgame 陪玩本质是并发系统，**必须先定义线程、任务、状态所有权**，否则会出现数据竞争、重复 OCR、UI 跨线程错误、模型推理抢占。

陪玩时可能同时存在：

```
1. Qt UI 主线程
2. OCR loop：截图 + RapidOCR
3. 后台剧情总结：LLM 调用
4. 用户普通提问：run_turn + LLM + TTS
5. 选项识别：截图 + VLM 定位 + OCR 抽字
6. 语音输入 loop：用户语音转文字后进入 run_turn
```

### 4.1 UI 主线程

只负责：展示 overlay / 框选 UI / 截图预览 / OCR 测试结果；接收点击与命令；消费后端 `RuntimeEvent` / `CompanionRuntimeEvent`。

不得执行：OCR 推理、VLM 推理、LLM 总结、长时间窗口扫描、阻塞式截图循环。

所有后端任务产生 UI 更新时，必须 marshal 回 Qt 主线程。

### 4.2 `GalgameCompanionSession` 是唯一状态 owner

它拥有（其他模块**不得直接修改**）：

```
current_game_id / playthrough_id / current_session_id / FSM state /
stable_current_line / pending_line_candidate /
unsummarized_committed_line_ids / last_choice_event_id /
window binding / OCR profile
```

外部只能通过 session 方法提交事件：

```
start() / pause() / resume() / end()
on_ocr_result() / on_window_lost()
on_choice_detected() / on_user_reported_choice()
on_summary_finished()
```

### 4.3 OCR loop 必须串行，不允许重叠

用「完成后等待」模型，不用固定 tick 叠加：

```
while playing:
    run one OCR cycle
    wait interval_seconds
```

原因：RapidOCR 推理可能 >1 秒；OCR 模型实例未必线程安全；并发会抢 CPU/GPU；并发会让 stable line 顺序混乱。

要求：同一 session 内 OCR 推理串行；同一 OCR adapter 实例用锁或单 worker 队列；一次 OCR 超过 1 秒则下一次延后不重叠；可记录 warning `ocr_cycle_ms > interval_seconds`。

### 4.4 OCR / 总结 / 问答 并发策略

允许 OCR loop、后台总结、问答 `run_turn` 并发，但共享数据读写必须安全：

```
1. committed StoryLine 一经确认立即落盘。
2. unsummarized_committed_line_ids 由 session owner 维护。
3. 后台总结启动时切出 immutable snapshot：
   summary_source_line_ids = 当前未总结 committed StoryLine id 列表。
4. pending_current 不进入 summary snapshot。
5. 总结运行期间新进来的 committed StoryLine 不进入本次 summary。
6. 新 committed StoryLine 留给下一次 summary。
7. 用户问答读取 game_context snapshot，不直接读可变 buffer。
8. 用户问答额外由 session owner 原子读取当前 pending_current line，一并注入。
```

禁止：总结任务直接持有可变 buffer 引用；`run_turn` 直接读写 session 内部 list；OCR 线程直接改 UI；多个 summary 对同一批 source_line_ids 重复总结。

### 4.5 资源互斥

```
1. OCR 推理串行。
2. 选项识别与 OCR 不并发。
3. 进入 choice_checking 时请求暂停 OCR loop。
4. 若当前有一个 OCR cycle in-flight，等它跑完。
5. in-flight cycle 完成后，不再启动下一个 OCR cycle。
6. 然后再执行选项截图 / VLM 定位 / OCR 抽字。
7. 后台总结若是远端 LLM，可与 OCR 并发。
8. 后台总结若是本地 GPU LLM，应走单独 ModelJobRunner 排队。
```

v1 可先假设：总结/问答用远端或当前配置 LLM endpoint，OCR 用本地 RapidOCR，两者资源冲突较小。但**代码接口要预留 `JobRunner` / queue**，不要把并发写死。

### 4.6 语音输入与 OCR 输入分流

```
1. 用户语音/文字命令 → ChatEngine/run_turn 或 command intent。
2. OCR 文本 → GalgameCompanionSession 的 text stream。
3. OCR 文本不直接变成用户消息。
4. "暂停陪玩 / 看一下选项 / 结束陪玩" 等命令先由 command router 处理。
5. 用户问剧情问题仍走 run_turn，但 gated stage 注入游戏上下文。
```

---

## 5. 读路径：`retrieve_game_context_node`

新增 gated stage，支持两种模式。

### 5.1 active companion mode（正在陪玩）

满足**任一**条件即启用：

```
1. interaction_mode == "galgame"
2. conversation_id 属于 galgame::<game_id>::playthrough::<playthrough_id>
3. 存在 active GalgameCompanionSession，且该 turn 来自陪玩 UI / 陪玩命令
```

注入：

```
[GAME_PROGRESS]        当前游戏、章节、线路推测、当前场景、上次进度
[CURRENT_GAME_BUFFER]  最近未总结 committed StoryLine、当前 pending_current line
[GAME_RELATIONS]       与当前问题相关的人物关系
[GAME_CHOICES]         最近选项、用户报告的选择、选择结果置信度
[COMPANION_CONTEXT]    同一游戏内的陪玩共同记忆
```

### 5.2 offline game query mode（普通聊天里查进度，无 active session）

触发条件：

```
1. 用户明确提到游戏名 / 别名。
2. 用户用 "昨天那个游戏 / 上次那个 galgame / 最近玩的游戏 / 玩到哪了" 等离线进度查询表达。
3. command router 识别出 intent ∈ {ask_last_progress, ask_game_progress, ask_character_relation}。
```

注入 `[GAME_PROGRESS]` / `[RECENT_GAME_SUMMARIES]` / `[GAME_RELATIONS]` / `[GAME_CHOICES]`；默认**不**注入 `[CURRENT_GAME_BUFFER]` / `[COMPANION_CONTEXT]`，除非用户明确问共同经历。

### 5.3 不允许用额外 LLM 分类做 gate

允许的判定信号：显式 `interaction_mode`、conversation_id 命名空间、active session、command router intent、关键词/别名/最近游戏启发式。

**禁止**：为判断是否注入 game_context 而单独跑一次 LLM 分类（那是第二条 LLM 路径）。

### 5.4 `interaction_mode` 的来源（不是悬空 flag）

```
1. 普通聊天默认 interaction_mode = "chat"。
2. 陪玩中，陪玩 UI / command router 发起的用户问题设 interaction_mode = "galgame"。
3. 普通聊天里问"我昨天玩到哪了"，仍可保持 "chat"，但 command intent = ask_last_progress。
4. run_turn 不靠 interaction_mode 单独判断，必须结合 command intent / conversation_id / active session。
```

建议扩展 `TurnRequest` 或 `TurnContext` metadata：

```json
{
  "interaction_mode": "chat | galgame",
  "command_intent": "ask_last_progress | inspect_choices | ... | null",
  "game_context_request": {
    "mode": "active | offline | none",
    "game_id": "string | null",
    "playthrough_id": "default"
  }
}
```

---

## 6. 记忆系统设计（四类记忆）

四类记忆，明确哪些复用现有系统、哪些是 galgame 新数据。

### 6.1 用户 / 角色关系记忆 —— 复用现有，不重造

沿用现有 `MemoryPort` / `MemoryScope`。例：「用户通常听完 galgame 语音再推进」「用户不喜欢剧透」「用户喜欢慢慢看剧情」。

galgame 只**读取**这类记忆，必要时通过现有 `MemoryPort` 写入，**不新建**一套用户长期记忆系统。

> ⚠️ 见 §27 开放问题①：陪玩切到专属 conversation_id 后，能否仍读到这类记忆，取决于现有 retrieve 的真实 scope 逻辑，必须先读代码确认。

### 6.2 游戏档案记忆 —— `GameProfile`

保存游戏配置（不和剧情混存）：`game_id` / `display_name` / `aliases` / `launch_profiles` / `window_match_rule` / `ocr_profile` / `last_played_at` / `active_playthrough_id` / `proactive_commentary` 设置。按 `game_id` 存。

### 6.3 剧情进度记忆 —— 客观事实

概念上包括：当前章节 / 场景 / 地点 / 线路推测 / 线路置信度 / 线路证据 / 是否确认线路 / 剧情摘要 / 关键台词 / 重要事件 / 未解决伏笔 / 人物关系 / 选项记录 / `last_ocr_anchor`。

**注意**：这是概念分类。实际存储时，人物关系（`CharacterRelation`）和选项记录（`ChoiceEvent`）**分表存储**，不直接嵌入 `GameProgressState`。

### 6.4 陪玩共同记忆 —— `CompanionBeat`

记录同一游戏内用户与 Spica 共同形成的主观经历。例：「用户说『我就知道这个人有问题』」「用户说『这个选择我后悔了』」「用户特别喜欢某角色」「一起吐槽某角色谜语人」。

v1 不做主动吐槽，但**可以记录用户明确表达的共同记忆**。普通聊天默认不检索 `CompanionBeat`，除非用户明确问「我们之前玩这个时说过什么」。绑 `character_id + user_id + game_id`，不跨角色共享。

---

## 7. 截图隐私、overlay 与窗口边界

### 7.1 v1 不承诺离屏窗口捕获

只在以下条件**全部**满足时才 OCR：

```
游戏窗口可见
游戏窗口未被非 Spica 窗口遮挡
Spica overlay 没有覆盖 OCR 区域
目标窗口仍可被可靠识别
```

出现「被遮挡 / 最小化 / 窗口丢失 / 标题变化无法确认 / Wayland 无法安全捕获 / overlay 覆盖 OCR 区域」时，**立即暂停 OCR 并提示用户**。

**禁止承诺**：切到别的软件也能只截游戏内容；被遮挡也能截到游戏内容；Wayland 不给权限也能直接截。

**实际承诺**：Spica 只在确认目标游戏窗口可见且安全时 OCR；一旦不安全立即暂停，绝不误截其他应用。

后续版本可研究（不进 v1）：X11 XComposite / compositor 相关捕获；Wayland portal 窗口捕获；Windows PrintWindow / Windows Graphics Capture / DXGI。

### 7.2 Spica overlay 避让

mss 按屏幕坐标截图，overlay 自身可能污染 OCR 区域。要求：OCR 前检查 overlay 是否覆盖 OCR 区域；覆盖则暂停或提示移动 Spica；UI 提供「避让 OCR 区域」布局策略；校准时记录 OCR 区域并让 overlay 默认不挡它。

### 7.3 推荐窗口模式

推荐**窗口化 / 无边框窗口化**；不推荐独占全屏（overlay 可能无法显示、alt-tab 可能最小化游戏、mss 截图可能不稳、失焦/遮挡暂停策略下体验差）。

### 7.4 焦点与 overlay

OCR 允许条件 = 目标窗口可见 + 未被非 Spica 窗口遮挡 + overlay 没盖住 OCR 区域 + 窗口可可靠识别。**Spica overlay 短暂获得焦点不应必然导致 OCR 暂停**，只要 overlay 不遮挡 OCR 区域、游戏画面仍可见、捕获区域仍安全。

---

## 8. OCR / screen pipeline 复用

仓库已有 screen 工具链（`agent_tools/function_tools/screen/`，含 RapidOCR + Moondream）。**v1 不重复加载两套 OCR / VLM 模型。**

```
1. OCRPort 复用现有 RapidOCR 初始化与配置。
2. inspect_choices 复用现有 screen analysis 的 VLM 定位/画面判断能力。
3. 选项文字抽取走 OCRPort，不让描述型 VLM 直接生成精确选项文字。
4. 若现有 screen 工具暂时无法直接作为 port 复用，写 adapter bridge，不复制模型加载逻辑。
```

选项识别流程：VLM/screen analyzer 判断有无选项 + 给出大致区域 → OCRPort/RapidOCR 从区域抽准确文字 → 保存结构化 `ChoiceEvent`。

**禁止**：OCR adapter 与 `inspect_screen` 各自加载一份 RapidOCR；VLM 直接承担精确 OCR；整帧 OCR 作为主要选项抽取方式。整帧 OCR 只能作低置信度 fallback 并标记低置信度。

> ⚠️ 见 §27 开放问题⑤：现有 screen 工具能否直接当 port 复用、还是需要 bridge，须先读 `agent_tools/function_tools/screen/` 确认。

---

## 9. 数据模型

> 约定：`route_key` 为 v2 多线路并行预留的 key（v1 恒为 `null` 或 `"default"`），已补入相关模型，消除「§14.6 承诺预留但 schema 缺失」的不一致。

### 9.1 GameProfile

```json
{
  "game_id": "string",
  "display_name": "string",
  "aliases": ["string"],
  "created_at": "datetime",
  "updated_at": "datetime",
  "last_played_at": "datetime | null",
  "active_playthrough_id": "default",
  "launch_profiles": {},
  "window_match": {},
  "ocr_profile": {},
  "proactive_commentary": {}
}
```

### 9.2 LaunchProfile

```json
{
  "platform": "linux | windows",
  "launch_type": "desktop_entry | command | exe | manual_bind",
  "launch_target": "string | null",
  "command": "string | null",
  "working_dir": "string | null",
  "enabled": true
}
```

### 9.3 WindowMatchRule

```json
{
  "platform": "linux | windows",
  "title_keywords": ["string"],
  "last_full_title": "string | null",
  "process_name": "string | null",
  "app_id": "string | null",
  "confirmed_once": true
}
```

### 9.4 OCRProfile

```json
{
  "languages": ["ja", "zh"],
  "dialog_text_region": {},
  "speaker_name_region": null,
  "speaker_strategy": "region | parse_from_text | narration_or_unknown",
  "stability_required_count": 2,
  "interval_seconds": 1.0,
  "similarity_threshold": 0.9,
  "raw_cache_retention_days": 7
}
```

### 9.5 OCRRegion

```json
{
  "x_ratio": 0.0,
  "y_ratio": 0.0,
  "w_ratio": 0.0,
  "h_ratio": 0.0,
  "pixel_rect": [0, 0, 0, 0],
  "window_size_at_calibration": [0, 0],
  "last_verified_at": "datetime"
}
```

### 9.6 PlaySession

```json
{
  "session_id": "string",
  "game_id": "string",
  "playthrough_id": "default",
  "route_key": "string | null",
  "started_at": "datetime",
  "ended_at": "datetime | null",
  "state": "active | paused | ended | interrupted | crashed",
  "ocr_line_count": 0,
  "summary_count": 0
}
```

### 9.7 StoryLine

```json
{
  "line_id": "string",
  "session_id": "string",
  "game_id": "string",
  "playthrough_id": "default",
  "speaker": "string | null",
  "text": "string",
  "timestamp": "datetime",
  "source": "ocr | manual",
  "confidence": 0.0,
  "raw_hash": "string",
  "status": "pending_current | committed | discarded"
}
```

### 9.8 StorySummary

```json
{
  "summary_id": "string",
  "game_id": "string",
  "playthrough_id": "default",
  "route_key": "string | null",
  "session_id": "string",
  "source_line_ids": ["string"],
  "summary_zh": "string",
  "key_original_lines": ["string"],
  "characters": ["string"],
  "major_events": ["string"],
  "unresolved_threads": ["string"],
  "route_guess": {},
  "created_at": "datetime",
  "updated_at": "datetime",
  "source": "auto_summary | user_correction | manual_note",
  "revision": 1
}
```

### 9.9 GameProgressState

```json
{
  "game_id": "string",
  "playthrough_id": "default",
  "route_key": "string | null",
  "last_played_at": "datetime",
  "chapter": { "title": "string | null", "confidence": 0.0 },
  "route": {
    "confirmed": false,
    "name": "string | null",
    "confidence": 0.0,
    "evidence": ["string"]
  },
  "location": "string | null",
  "current_scene_summary": "string",
  "major_events": ["string"],
  "unresolved_threads": ["string"],
  "last_ocr_anchor": {
    "speaker": "string | null",
    "text": "string",
    "timestamp": "datetime"
  }
}
```

> v1：`playthrough_id` 恒为 `"default"`，`route_key` 恒为 `null`；`route` 子对象承载「玩家声明 + LLM 推测」的当前线路与置信度。v2 用 `route_key` 做多线路并行 key。

### 9.10 CharacterRelation

```json
{
  "relation_id": "string",
  "game_id": "string",
  "playthrough_id": "default",
  "character_a": "string",
  "character_b": "string",
  "relation_summary": "string",
  "evidence": ["string"],
  "confidence": 0.0,
  "updated_at": "datetime",
  "source": "auto_summary | user_correction"
}
```

### 9.11 ChoiceEvent

```json
{
  "choice_id": "string",
  "game_id": "string",
  "playthrough_id": "default",
  "session_id": "string",
  "timestamp": "datetime",
  "options": [{ "index": 1, "text": "string" }],
  "selected_option_index": null,
  "selected_option_text": null,
  "selection_source": "user_reported | inferred | null",
  "confidence": 0.0,
  "screen_analysis_summary": "string"
}
```

### 9.12 CompanionBeat

```json
{
  "beat_id": "string",
  "game_id": "string",
  "playthrough_id": "default",
  "session_id": "string | null",
  "type": "reaction | joke | user_preference | shared_observation | correction",
  "content": "string",
  "source": "user | spica | auto",
  "created_at": "datetime",
  "scope": { "character_id": "spica", "user_id": "string", "game_id": "string" }
}
```

---

## 10. OCR 运行与 stable line

### 10.1 安全前置检查（每轮 OCR 前）

```
1. 是否存在绑定窗口。
2. 是否能确认窗口仍是目标游戏。
3. 窗口是否可见。
4. 窗口是否被非 Spica 窗口遮挡。
5. Spica overlay 是否覆盖 OCR 区域。
6. 是否疑似无法安全截取。
```

不安全 → 暂停 OCR，状态转 `paused` 或 `window_lost`，UI 提示，不继续截图。

### 10.2 OCR loop

串行执行，完成后等待 1 秒，只在安全前置检查通过时执行；OCR 对白区域；若配置了名字区域则一并 OCR。

### 10.3 stable line 策略

```
1. OCR 得到 raw_speaker / raw_text。
2. 清洗文本。
3. 和上一轮候选比较。
4. 连续 2 次相同或高度相似 → 标记 stable_current_line。
5. stable_current_line 立即写入 StoryLine，status = pending_current。
6. 同一句继续停留，不重复写入。
7. 下一句出现（画面文本变化）→ 把上一句 pending_current 转为 committed。
8. committed 立即持久化，并加入 unsummarized_committed_line_ids。
9. 结束陪玩时，把 pending_current 转为 committed。
10. 崩溃恢复时，pending_current 可保留，或在恢复流程中确认/转正。
```

同时满足：不重复写同一句；崩溃不丢当前正在看的那一句；后台总结默认只总结 committed；即时问答仍可读 pending_current。

> 用户习惯是听完语音再推进，所以 v1 偏保守，宁可慢，也不让半句/错句/重复句污染剧情记忆。

---

## 11. Story buffer 定义

「buffer」不是独立长期模型，而是 derived working set：

```
Story buffer = 自上次成功 StorySummary 以来，尚未被 summary 覆盖的 committed StoryLine 序列。
pending_current 不属于 summary buffer。
```

session 可持有：

```json
{
  "unsummarized_committed_line_ids": ["line_id_1", "line_id_2"],
  "last_summary_id": "summary_id | null",
  "pending_current_line_id": "line_id | null",
  "pending_line_candidate": {}
}
```

关系：StoryLine 是持久化行；buffer 是一组 committed StoryLine id；pending_current 是当前显示的稳定句、不进 summary buffer；`StorySummary.source_line_ids` 记录本次覆盖的 committed 行；一个 committed 行默认只被一个 regular summary 覆盖；结束总结可覆盖整 session 或最近未总结部分。

**用户即时问答上下文** = committed 历史的不可变 snapshot + 当前 pending_current 的一次性原子读取 + 最近已总结 StorySummary + 最近 ChoiceEvent。**不要把 pending_current 塞进 summary snapshot。**

---

## 12. 崩溃恢复

PlaySession 状态：`active / paused / ended / interrupted / crashed`。

dangling session 检测（Spica 启动时）：

```
1. 找到 state=active/paused 且无 ended_at 的 PlaySession。
2. 标记 interrupted 或 crashed。
3. 询问用户是否恢复。
4. 恢复 → 继续使用该 session。
5. 不恢复 → 基于已落盘 committed StoryLine 做一次补总结。
6. pending_current 可提示用户确认是否纳入恢复后的剧情。
```

依赖：stable line 一确认就落盘为 pending_current；committed StoryLine 不能只在内存；summary 触发前的原始行必须能找回。

---

## 13. 剧情总结

### 13.1 触发条件

```
1. unsummarized committed buffer 累计约 2000 字。
2. 用户结束陪玩。
3. 用户提问且当前 buffer 较长。
4. 崩溃恢复时发现有未总结 committed StoryLine。
```

### 13.2 执行方式

游玩中 2000 字总结是**后台任务，默认不阻塞 OCR**；状态保留 `playing`，UI 可显示「正在后台整理剧情」。结束陪玩时的最终总结可进入 `summarizing / ending`。

### 13.3 总结语言

中文总结；保留关键日文原词、人名、专有名词；不乱翻译角色名。

### 13.4 关键台词

不长期保存全部 OCR 原文，只在重要时保存关键台词：告白 / 身份揭露 / 死亡·事故 / 重大伏笔 / 线路分歧 / 角色关系变化 / 用户手动标记。

### 13.5 线路判断

可推测线路，但**必须带置信度，不能把推测写成事实**：

```json
{ "route_guess": "A线", "confidence": 0.68,
  "evidence": ["连续出现A的个人剧情", "上一次选项偏向A"], "is_confirmed": false }
```

确认条件：游戏明确出现线路名 / 用户确认 / 剧情中出现强证据。回答时必须区分「目前确认是 A 线」与「我倾向判断在 A 线，但还没完全确认」。

> 承重墙：**线路/章节以玩家声明为权威，LLM 只看 summary 提议、由用户确认。** galgame 画面常不显示章节号，让 LLM 从 OCR 猜必幻觉。

### 13.6 v1 playthrough 限制

```
v1 假设：同一游戏同一时间只跟踪一个 active playthrough，playthrough_id = "default"。
v1 不保证：用户频繁 SL；同时跑多条线；A线/B线进度并行。
数据结构已预留：playthrough_id、route_key（见 §9）。
v2 再做多周目 / 多线路并行管理。
```

### 13.7 总结失败处理

```
1. 保留所有 source_line_ids。
2. 不从 unsummarized_committed_line_ids 移除这些行。
3. 记录 summary_job 状态 = failed。
4. 允许稍后重试。
5. 下次总结触发时，失败的未总结行可与新 committed 行折叠进一个新 snapshot。
6. 但不得对同一批完全相同 source_line_ids 同时启动多个重复 summary job。
```

---

## 14. 选项识别

### 14.1 v1 触发方式

v1 不做自动选项检测。用户主动说「Spica，看一下选项」，或点未来 UI 按钮/快捷键（v1 可只做语音/文字命令，接口预留按钮和快捷键）。

### 14.2 识别流程

```
1. 用户触发"看一下选项"。
2. 进入 choice_checking。
3. 请求暂停 OCR loop。
4. 若当前有 OCR cycle in-flight，等它完成。
5. 不启动新的 OCR cycle。
6. 检查游戏窗口是否安全可截图。
7. 截取当前游戏窗口可见画面。
8. VLM / screen analyzer 判断有无选项、定位大致区域。
9. RapidOCR 对选项区域运行 OCR。
10. 保存 options[].text。
11. UI 显示识别结果。
12. 记录 ChoiceEvent。
13. choice_checking 结束 → 恢复 playing，允许 OCR loop 继续。
```

整帧 OCR 只能作低置信度 fallback 并标记。选项质量优先依赖 VLM 给出的区域约束，而非无约束全屏 OCR。

### 14.3 选项建议模式

```
"看一下选项"            → 只读选项
"看一下选项并帮我选"     → 无剧透建议
"我想走 A 线，帮我看选项" → 线路建议
```

**默认不要线路建议。**

### 14.4 用户报告选择

存在最近未完成 ChoiceEvent 时：「我刚才选了第二个」→ 关联最近 ChoiceEvent，`selected_option_index = 2`，`selection_source = user_reported`。

不存在最近 ChoiceEvent 时：「我刚才选了原谅她」→ **新建 manual ChoiceEvent**，`options = unknown`，`selected_option_text = "原谅她"`，`selection_source = user_reported`。

用户口头声明优先级最高。**不要因为没先触发「看一下选项」就丢失选择记录。**

### 14.5 选择影响

刚记录时不硬编影响；后续剧情推进后再总结「该选择可能导致…… / 置信度 / 证据」；无法判断保持 unknown。禁止用外部攻略，禁止剧透未来剧情。

---

## 15. 用户问答流程与 conversation_id

### 15.1 陪玩中提问

例：「刚才发生什么了？」「这个角色是谁？」「他和女主什么关系？」「现在是不是进 A 线了？」「这句话什么意思？」

```
1. GalgameCompanionSession 提供当前 game_context：
   GameProgressState / 最近 StorySummary /
   unsummarized committed StoryLine snapshot / pending_current line /
   最近 ChoiceEvent / 相关 CharacterRelation
2. 调用 ChatEngine → 仍走 run_turn → retrieve_game_context_node 注入 → Spica 回复。
```

**必须包含 pending_current line**，否则用户正盯着当前那句问「刚才发生什么」时会漏掉当前显示句。

### 15.2 普通聊天中问进度

「我昨天玩到哪了？」：

```
1. command intent = ask_last_progress / ask_game_progress。
2. 查最近游玩 game_id，默认用最近玩的游戏。
3. 回答时说明："我查的是最近玩的《游戏名》。"
4. 读 GameProgressState + 最近 StorySummary → 通过 run_turn 回复。
```

普通聊天默认不注入 `CompanionBeat`，除非明确问共同经历。

### 15.3 conversation_id 与记忆连续性

游戏专属 conversation_id：`galgame::<game_id>::playthrough::<playthrough_id>`，用于游戏相关问答、陪玩中 Spica 回复、游戏相关 recent context。普通聊天仍用原有 conversation_id（如 `default`）。

```
进入陪玩：
  UI 可切换到 galgame conversation_id；
  陪玩中问剧情写入 galgame conversation_id；
  游戏 OCR 文本不写入 ChatEngine recent memory，而写入 StoryLine / GameMemory。

离开陪玩：
  状态回 game_launched；普通聊天回 default conversation_id；
  游戏进度通过 GameProgressState / StorySummary 查询，不依赖普通 recent memory。

普通聊天查游戏：
  当前 turn 可仍在 default conversation_id；
  retrieve_game_context_node 以 offline query mode 注入进度；
  不切换整个聊天会话。
```

**v1 不把 OCR 剧情文本塞进 ChatEngine recent memory**（否则爆且污染普通聊天）。只把「用户 ↔ Spica 的问答 turn」写入对应 conversation_id 的 recent memory。

> ⚠️ 见 §27 开放问题①：切到 galgame conversation_id 后，现有 long-term retrieve 是否还能取到 Spica 平时关于麦的长期记忆——这是必须先读代码回答的耦合点。

---

## 16. 状态机与状态映射

### 16.1 FSM 状态

```
idle                   没有绑定游戏，没有陪玩
game_launched          游戏已启动/已绑定窗口，但未开始陪玩
calibrating            正在校准 OCR
playing                正在陪玩，OCR 运行中
paused                 陪玩暂停，OCR 停止
window_lost            窗口丢失或不安全，OCR 暂停
choice_checking        正在识别选项
background_summarizing  后台总结中，但 OCR 可继续
summarizing            结束陪玩时总结
ending                 正在结束陪玩
error                  异常
```

### 16.2 正常流程

```
idle → (添加/启动/绑定游戏) → game_launched → (开始陪玩)
→ calibrating(若缺 OCR 区域) → (校准完成) → playing
→ (暂停) paused → (恢复) playing
→ (看一下选项) choice_checking → (识别结束) playing
→ (2000字) background_summarizing(保持 playing 能力) → (完成) playing
→ (结束陪玩) summarizing / ending → (窗口仍开) game_launched
```

### 16.3 window_lost

```
playing → (遮挡/最小化/窗口丢失/overlay 覆盖 OCR 区域) → window_lost
→ (找回并确认安全) → playing
```

**v1 默认不自动 ending。** 原因：用户可能只是临时切窗口；Wine/Bottles 可能短暂重建窗口；用户可能离开电脑，自动 ending 会触发 LLM 总结不一定符合预期；StoryLine 已落盘，不急于自动结束。

策略：window_lost 保持挂起；UI 提示「恢复窗口 / 结束陪玩 / 保存并退出」；v1 不自动总结结束。

### 16.4 暂停 / 结束

暂停：停止 OCR、保留 session、Spica 仍可回答普通问题、不继续截图。

结束：

```
1. 停止 OCR。
2. pending_current 转 committed（除非用户选择丢弃）。
3. flush 当前 unsummarized committed buffer。
4. 总结本次游玩。
5. 更新 GameProgressState。
6. 更新 CharacterRelation。
7. 保存 ChoiceEvent。
8. 保存 CompanionBeat。
9. 标记 PlaySession ended。
10. 窗口仍开 → 回 game_launched。
11. 不自动关闭游戏。
```

### 16.5 FSM ↔ PlaySession.state 映射

FSM state 是运行时状态，`PlaySession.state` 是落盘状态，二者不是同一枚举，必须映射：

| FSM state | PlaySession.state | 说明 |
| --- | --- | --- |
| idle | none | 没有 session |
| game_launched | none / ended | 游戏已绑定但未陪玩 |
| calibrating | active | 已进入陪玩准备流程 |
| playing | active | OCR 可运行 |
| paused | paused | 用户主动暂停 |
| window_lost | paused | 窗口不安全，OCR 暂停，session 未结束 |
| choice_checking | active | 临时识别选项，OCR 暂停/挂起 |
| background_summarizing | active | 后台总结，session 仍 active |
| summarizing | active | 结束前总结中 |
| ending | active → ended | 结束流程中，完成后 ended |
| error | active / paused / crashed | 视异常严重程度 |

启动时发现 `state=active` 且无正常关闭标记 → 标记 `interrupted / crashed` → 进入 dangling session 恢复。

---

## 17. 平台、启动与窗口匹配

### 17.1 平台策略

Ubuntu 优先、Bottles 优先、Windows 只预留接口。

### 17.2 启动方式

```
1. 从 Ubuntu 应用启动项选择（扫描用户应用目录 + 系统应用目录）。
2. 手动输入启动命令。
3. 用户已打开游戏，Spica 只绑定窗口。
```

### 17.3 窗口匹配策略

Wine/Bottles 下进程名和 WM_CLASS 可能不可靠，**不能吹成强匹配**。

```
v1 主路径：标题关键词 + 用户确认。
辅助信号：last_full_title / process_name / app_id / desktop entry id。

流程：
  1. 启动或等待游戏窗口。
  2. 获取候选窗口。
  3. 用 title_keywords 过滤。
  4. process/app_id 仅作辅助排序。
  5. 候选唯一，第一次仍需用户确认。
  6. 候选多个，用户选择。
  7. 找不到，要求用户手动绑定。
```

窗口标题可能随线路/章节变化，所以完整标题只保存为历史参考，不作为唯一规则。

---

## 18. OCR 校准流程

### 18.1 第一次校准

引导语：「请把游戏推进到有正常对白框的画面。接下来需要框选对白区域。如果这个游戏有独立名字框，也建议框选名字区域。名字区域可以跳过，之后会用旁白/未知说话人兜底。」

### 18.2 OCR 区域

```
dialog_text_region：对白区域，必选。
speaker_name_region：名字区域，推荐但可跳过。
```

**不要让没有名字框的游戏无法配置。** 说话人兜底策略（`speaker_strategy`）：名字区域 OCR / 从对白文本解析 / 旁白 / 未知说话人。

### 18.3 区域保存

每个区域同时保存比例坐标和像素坐标（见 §9.5）：比例用于窗口缩放适配，像素用于调试和校验。

### 18.4 OCR 测试

```
1. 截当前游戏窗口可见区域。
2. 裁剪对白区域。
3. 如有名字区域，也裁剪。
4. 调用 OCR。
5. UI 显示截图预览。
6. UI 显示识别文本。
7. 用户确认 / 重新框选 / 手动修正本次识别文本。
```

截图预览和确认 UI 必须在 `ui/` 层，`spica/` 不 import Qt。

---

## 19. 人物关系表

单独维护（不只放剧情总结里），见 §9.10。用于回答：「这个角色是谁？」「他和女主什么关系？」「他们是不是以前认识？」「刚才那个人为什么这么说？」

---

## 20. 自然语言命令

允许自然语言，内部映射固定 intent。v1 核心 intent：

```
add_galgame / launch_game / bind_current_game_window
start_galgame_companion / pause_galgame_companion / resume_galgame_companion / end_galgame_companion
recalibrate_ocr_region / inspect_choices / report_choice_selection
summarize_current_story / ask_last_progress / ask_character_relation
correct_story_summary / delete_story_summary
```

「开始 galgame 陪玩」「开始陪我玩这个」「接着昨天那个游戏」→ 都映射到 `start_galgame_companion`。

没有绑定游戏时，Spica 应询问：1) 启动已有游戏 2) 添加新游戏 3) 绑定已打开窗口；不要直接失败。

---

## 21. 主动吐槽（v1 仅预留）

v1 不实现。只在 `GameProfile` 预留：

```json
{ "proactive_commentary": { "enabled": false, "mode": "off", "output": "text", "frequency": "low" } }
```

后续版本要求：第一次开始某游戏陪玩时询问是否开启；默认低频；默认文字；用户允许才语音；严肃/悲伤/告白/死亡/选项/用户提问/Spica 回答时禁止吐槽；吐槽必须短、不超过一句；只在同一游戏陪玩模式中引用 `CompanionBeat`。

---

## 22. 错误修正与删除

v1 必须支持修正和删除错误总结（OCR 会错、模型总结会错、错误记忆会污染后续回答）。命令：「刚才那段总结错了」「删除上一条剧情总结」「把这个角色关系改一下」。

每条可修改记录保留：`source`（auto_summary / user_correction / manual_note）/ `created_at` / `updated_at` / `revision`。

删除游戏配置时必须询问：1) 只删配置 2) 同时删剧情记忆 3) 取消。**不能默认连剧情一起删。**

---

## 23. R18 与数据边界

默认配置的总结/问答 LLM endpoint 视为支持 R18 内容，v1 不为 R18 做禁用/跳过/特殊规避/降级/默认拒答。R18 内容按普通剧情处理，正常进入 OCR、总结、进度、问答。

```
本地保存：截图临时数据 / raw OCR / StoryLine / OCR 缓存 /
          GameProfile / GameProgressState / ChoiceEvent / CharacterRelation / CompanionBeat
可能发送到 LLM：OCR 文本 / 剧情摘要 / 当前 buffer / pending_current line /
              人物关系 / 选项内容 / 用户问题
```

架构上明确：**截图 / raw OCR 永不离机**；文本总结/问答按当前 LLM 配置处理。首次启用可提示用户这条边界。LLM 总结失败 = 通用异常处理（保留 StoryLine、标记 failed、允许重试、不丢原始行），不针对 R18 特判。

---

## 24. 实现 Phase 拆解（Phase 0–10）

**不要先做 OCR。先验证「游戏记忆 → prompt 注入 → run_turn 回复」这条读路。**

### Phase 0：架构确认（不改代码，或仅极小探查）

输出：ports 列表 / adapters 列表 / session 边界 / 并发模型 / `run_turn` 注入点 / UI 事件通道 / 状态机 / FSM↔PlaySession 映射 / 数据模型 / 测试计划。**必须先回答 §27 的开放问题**，并说明将修改哪些文件、不会碰哪些边界。

### Phase 1：数据模型与存储

实现/设计 §9 全部模型。

### Phase 2：手动喂文本与手动事件路径（不接 OCR）

```
manual_add_story_line(game_id, speaker, text)
manual_flush_summary(game_id)
manual_set_progress_state(...)
manual_add_choice_event(game_id, options, selected_option)
manual_add_companion_beat(game_id, type, content)
```

目标：不用游戏窗口、不用 OCR，也能写入 StoryLine / StorySummary / GameProgressState / ChoiceEvent / CompanionBeat，从而让 Phase 3 完整测试五块注入。

### Phase 3：run_turn 游戏上下文注入

实现 gated stage `retrieve_game_context_node`。验证：active mode 可注入 game context；offline mode 可回答「昨天玩到哪了」；不另起第二条 LLM 路径；普通聊天不被污染；`CompanionBeat` 默认不进普通聊天。

### Phase 4：Session 状态机

实现 §16.1 全部 FSM 状态及流转。

### Phase 5：Ubuntu 启动与窗口绑定

desktop entry 扫描 / 手动 command / manual bind / title_keywords + 用户确认 / overlay 避让 OCR 区域检查。

### Phase 6：OCR 校准与测试 UI 接口

实现后端接口和 UI 调用点，`spica/` 不碰 Qt。

### Phase 7：OCR text stream

串行 OCR loop / 安全前置检查 / 连续 2 次稳定 / 相似度去重 / pending_current StoryLine / committed StoryLine / 立即落盘。

### Phase 8：剧情总结

2000 字后台总结 / summary snapshot / 结束总结 / 崩溃恢复补总结 / 进度状态更新 / 人物关系更新 / 总结失败重试折叠。

### Phase 9：选项识别

用户主动触发 / choice_checking 暂停 OCR 并 drain in-flight cycle / VLM 定位判断 / OCR 提取文字 / ChoiceEvent / 用户报告选择 / manual ChoiceEvent。

### Phase 10：修正 / 删除错误记忆

最小可用 correction / delete。

---

## 25. 测试策略

**命令固定：`python -m pytest tests -q`**（永远不要裸 `pytest`，避免递归扫到 vendored GPT-SoVITS runtime 或第三方目录）。

### 25.1 单元测试

```
1. 模型序列化（GameProfile / PlaySession / StoryLine / StorySummary）。
2. conversation_id 生成：galgame::<game_id>::playthrough::<playthrough_id>。
3. stable line 去重：连续 2 次才 stable / 相似波动合并 / 不重复提交同一句。
4. pending_current → committed 转换。
5. buffer = unsummarized committed StoryLine ids。
6. pending_current 不进入 summary snapshot。
7. summary snapshot 不含快照后新来的行。
8. ChoiceEvent：已有 event 时报告"第二个"；无 event 时报告"原谅她"新建 manual ChoiceEvent。
9. CompanionBeat：可写入 / active mode 可注入 / 普通聊天默认不注入。
10. FSM state → PlaySession.state 映射。
11. choice_checking 暂停 OCR 并等待 in-flight cycle 完成。
12. 总结失败时 source_line_ids 保持 unsummarized，可折叠进下次 summary。
```

### 25.2 run_turn 注入测试（不接真游戏、不接 OCR，用手动数据）

active mode 注入 `[GAME_PROGRESS]` / `[GAME_CHOICES]` / `[COMPANION_CONTEXT]`；offline mode 回答「昨天玩到哪了」；普通聊天默认不注入 game context；`CompanionBeat` 默认不进普通聊天；不出现第二条 LLM prompt 路径。

### 25.3 OCR 去重测试（golden frames / mock）

同一句连续两次才 stable；一个字波动仍合并；文本变化后上一句 committed；当前句 pending_current 崩溃后仍可恢复；pending_current 不进后台 summary snapshot。

### 25.4 崩溃恢复测试

建 active PlaySession → 写若干 committed StoryLine + 一个 pending_current → 不设 ended_at → 重启 → 检测 dangling session → 标记 interrupted/crashed → 可补 summary → pending_current 可恢复或等用户确认。

### 25.5 事件通道测试

后端不直接调用 Qt，只产生事件（`galgame_window_lost` / `galgame_summary_done` / `galgame_stable_line_committed` / `galgame_ocr_preview_ready` / `galgame_choice_detected` 等），UI 层单独消费。

### 25.6 不强制自动化（手动验收项）

真实 Bottles 启动；真实 Wayland 截图；真实 VLM 选项定位质量；真实 R18 剧情总结质量。

---

## 26. v1 成功标准

闭环：添加游戏 → 选启动项 → 确认窗口 → 框选对白区（可选名字区）→ 测试 OCR → 保存配置 → 启动游戏并绑定 → 开始陪玩（检查可见/未遮挡/overlay 未覆盖）→ 串行 OCR → 稳定确认 → pending_current → 变化后 committed → 立即落盘 → 2000 字后台总结 → 看选项（drain OCR）→ 记 ChoiceEvent → 报告选择（含 manual）→ 结束陪玩（pending 转 committed、总结、更新进度/关系/选项、回 game_launched）→ 第二天「我昨天玩到哪了」默认查最近游戏并说明、经 run_turn 回复。

判定标准：

```
1. 不误截其他窗口。
2. 游戏遮挡/最小化/overlay 覆盖 OCR 区域时暂停 OCR。
3. 不重复记录同一句。
4. OCR loop 串行，无重叠推理。
5. pending_current 防崩溃丢当前句。
6. committed StoryLine 立即落盘。
7. 后台总结读不可变 snapshot，不读可变 buffer。
8. pending_current 不进 summary snapshot。
9. 问答读 committed snapshot + 当前 pending_current。
10. 崩溃后能恢复或补总结。
11. 能回答昨天玩到哪里。
12. 能记录选项和用户选择。
13. 没先看选项也能凭口头声明新建 manual ChoiceEvent。
14. 能写入/读取 CompanionBeat，普通聊天默认不注入。
15. 能修正错误总结。
16. 问答仍走 run_turn 唯一路径。
17. 新模块不破坏 spica/ 的 Qt-free 约束。
18. R18 游戏按普通游戏流程处理。
19. window_lost 默认挂起，不自动 ending / LLM 总结。
20. 先手动喂文本验证记忆注入 run_turn，再接 OCR。
21. 测试命令固定为 python -m pytest tests -q。
```

---

## 27. Phase 0 必须先回答的开放问题（重要）

下面几条**靠继续写文档回答不出来，只能读现有代码或在实现中决定**。Claude Code 在 Phase 0 必须逐条给出答案，再进入 Phase 1。

### ① conversation_id × 长期记忆耦合（最高优先，必读代码）

进入陪玩切到 `galgame::<game_id>::playthrough::<playthrough_id>` 这个专属 conversation_id 后，**现有 `MemoryPort` 的 long-term retrieve（喂 `[LONG_TERM_MEMORY]` 段的那条路径）是否还能取到 Spica 平时关于「麦」的长期记忆？**

- 现有 `adapters/memory/sqlite.py` 按 `character_id::conversation_id` 命名空间隔离。若 long-term retrieve 也按 conversation_id 隔离，则切到 galgame conversation_id 会**读不到**角色长期记忆，与 §6.1「galgame 只读取角色记忆」冲突。
- **Phase 0 动作**：读 `spica/adapters/memory/sqlite.py` 的 retrieve / scope 逻辑与 `spica/ports/memory.py` 的 `MemoryScope` 用法，确认长期记忆的真实 key 维度（是按 `character_id` 还是按 `character_id::conversation_id`）。
- **据此定方案**：要么长期记忆按 `character_id` 取（陪玩也能读到角色记忆），要么 retrieve 时显式传角色级 scope，要么 gated stage 用一个不切换主 conversation 的方式取角色记忆。不要在没读代码前假设任何一种。

### ② `route_key` 落点确认

本规格已把 `route_key`（v2 多线路 key）补入 `PlaySession` / `StorySummary` / `GameProgressState`（见 §9），v1 恒为 `null`。Phase 0 确认这是否与 Phase 1 的存储 schema 一致；若决定用别的维度做多线路 key，在 Phase 0 改这里，别让「§13.6 预留」与实际 schema 再次脱节。

### ③ 后台总结进行中又触发一次总结

后台总结在飞时，buffer 又攒到 2000 字（新行不在上一个 snapshot 里），是否起**第二个**并发总结？

- §13.7 只禁了「同一批 source_ids 重复」，没禁「不同批并发」。
- **决定时机**：Phase 8。建议 v1 简单化——同一时间只允许一个 in-flight summary job，新行排队等下一轮；但这是 Phase 8 的实现决策，不在 Phase 0 写死。

### ④ `RuntimeEvent` vs 新 `CompanionRuntimeEvent`

galgame 的 UI 事件（见 §25.5 + 下列）走现有 `RuntimeEvent` 还是新增 `CompanionRuntimeEvent` dataclass？

- **Phase 0 动作**：读现有 `RuntimeEvent` 定义与 Host→UI 桥接方式，判断扩展现有枚举更干净、还是新增并列 dataclass 更干净。无论哪种，必须 Qt-free、后端只 emit、UI 主线程消费。
- 建议事件类型：`galgame_status_changed` / `galgame_ocr_preview_ready` / `galgame_ocr_test_result` / `galgame_stable_line_committed` / `galgame_window_lost` / `galgame_window_recovered` / `galgame_summary_started` / `galgame_summary_progress` / `galgame_summary_done` / `galgame_choice_detected` / `galgame_choice_recorded` / `galgame_error`。

### ⑤ `OCRPort` 能否直接包现有 screen 工具链

`OCRPort` 能否直接复用 `agent_tools/function_tools/screen/` 的 RapidOCR 初始化，还是需要写一个 adapter bridge？

- **Phase 0 动作**：读 `agent_tools/function_tools/screen/` 看 RapidOCR 是怎么初始化/持有的、能否作为 port 注入。
- 目标（硬约束）：**只加载一份 RapidOCR**，galgame OCR 与 `inspect_screen` 共用同一实例/配置，不双加载（见 §8）。

### ⑥（跨平台正交提醒，不阻塞 v1）

跨平台打包/运行（Windows exe、Ubuntu .desktop、Wayland 截图——现在是 mss + Linux-only 代码）与 galgame 陪玩**功能**是两件正交的事。本规格只管功能语义；Windows 实现、Wayland 完整窗口捕获**单独排期**，不在 v1。Phase 0 不必解决，但要在计划里标注它是独立工作流，避免被混进 galgame 功能里一起做。

---

*文档结束。Phase 0 完成（含 §27 逐条回答）后再进入 Phase 1。*

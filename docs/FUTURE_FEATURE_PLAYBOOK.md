# Spica 未来需求 Playbook

> 「需求 → 推荐落点 → 别这样做 → 最小步骤 → 先读什么 → 查哪些边界 → 跑哪些测试 → 风险」。
> 全部用真实路径。配套 `docs/DEVELOPMENT_GUARDRAILS.md`（护栏）。末尾附**可复制的新需求提示词模板**。
> 路径以真实仓库为准；标「不确定」处表示需先读代码确认，不要瞎定。

**通用前置（每项都适用，不再重复）**：先读 `CLAUDE.md` + `docs/DEVELOPMENT_GUARDRAILS.md`；收尾跑 `python -m pytest tests -q`（绝不裸 `pytest`）；先给计划再改码。

---

## 需求 1：新增一个 LLM provider
推荐落点：`spica/adapters/llm/` 写新 adapter 满足 `spica/ports/llm.py::LLMPort`；`spica/host/builtins.py` `registry.register_llm("名字", 工厂)`；`data/config/app.yaml` `llm.provider` 改名。
不应该做：在 runtime/ChatEngine 里加 if-else 分模型；在 adapter 里 `os.getenv` 读 key（key 从注入的 secrets/config 来）；import Qt。
最小步骤：1) 实现 LLMPort 全部方法（`iter_response_text`/`create_responses`/`create_chat_with_tools`/`iter_chat_with_tools`）；2) register_llm；3) 改 app.yaml provider 名 + base_url/model。
先读：`spica/ports/llm.py`、`spica/adapters/llm/openai_compatible.py`、`spica/host/builtins.py`、`spica/config/schema.py(LLMConfig)`。
查边界：LLMPort 签名一致；唯一调用面仍在 adapter（runtime 经 deps.llm）；Anthropic 是 messages API 非 OpenAI 兼容，需独立分支。
跑测试：`test_phase5_adapters`、`test_streaming_tool_probe`、`test_llm_reasoning` + 全量。
风险：**P2**（动 LLM 面，但有 port 契约 + golden 兜底）。

## 需求 2：新增一个 TTS provider
推荐落点：`agent_tools/tts/adapters/` 或 `spica/adapters/tts/` 写 adapter 满足 `spica/ports/tts.py::TTSPort`；`builtins.py` `register_tts("名字", 工厂)`；`data/config/tts.yaml` `provider` 改名。
不应该做：在 ui/ 里直接 new TTS；后端线程直接驱动播放 widget；绕 registry。
最小步骤：1) 实现 TTSPort；2) register_tts；3) 改 tts.yaml provider；4) 预热接 warmup（可选）。
先读：`spica/ports/tts.py`、`agent_tools/tts/manager.py`、`agent_tools/tts/adapters/{dummy,gptsovits_current}.py`、`builtins.py`、`spica/host/warmup.py`。
查边界：TTSPort 签名；tts.yaml 是角色数据文件（路径相对配置目录）；不 import Qt。
跑测试：`test_tts_adapters`、`test_warmup` + 全量。
风险：**P2**。

## 需求 3：新增 faster-whisper 本地 STT（或新 STT 后端）
推荐落点：已有 `spica/adapters/stt/faster_whisper.py` + `SttConfig`（schema.py，`backend`/`model`/`device`/...）。新后端=新 adapter + `SttConfig.backend` 增枝 + AppHost 的 STT 装配处按 backend 选择。
不应该做：在业务码 `os.getenv` 读模型路径（走 SttConfig）；让 STT 直接驱动 UI（经 hardware/respeaker worker + signal）。
最小步骤：1) 写 STT adapter；2) `SttConfig` 加 backend 枝；3) AppHost 按 `config.stt.backend` resolve-once 注入。
先读：`spica/adapters/stt/faster_whisper.py`、`spica/config/schema.py(SttConfig)`、`spica/host/app_host.py`（STT 装配处，约 stt resolve）、`hardware/respeaker/speech_worker.py`。
查边界：STT 经 AppHost 注入（registry 无 register_stt——STT 不在 registry 4 类里，是 AppHost 直接装配，**改前确认装配点**）；yaml-only（铁律 #4）。
跑测试：`test_stt_faster_whisper`、`test_speech_worker_stt` + 全量。
风险：**P2**（STT 装配点需确认）。不确定装配细节时先读 app_host.py 的 stt 段。

## 需求 4：新增一个屏幕观察工具
推荐落点：`spica/adapters/tools/` 写 read 工具（仿 `screen.py`），schema + handler；`builtins.py` register_tool(effect="read")。复用 `agent_tools/function_tools/screen/`（mss 截图 + RapidOCR + Moondream），**绝不上传图片**。
不应该做：新加载一份 OCR/VLM 模型（复用单例 + `_INFER_LOCK`）；把截图发给聊天 LLM。
最小步骤：1) schema + handler（调本地 pipeline）；2) register_tool(available=?, intent_gated=?, effect="read", compact_output=?)；3) 大输出注册 compact_output。
先读：`spica/adapters/tools/screen.py`、`agent_tools/function_tools/screen/`、`spica/adapters/ocr/rapidocr.py`、`spica/plugins/registry.py`。
查边界：本地不上传（INVARIANT N0）；OCR 串行锁；effect=read；不绕 registry。
跑测试：`test_registry`、`test_screen_*`、`test_chat_tool_round` + 全量。
风险：**P2**。

## 需求 5：新增浏览器操控工具（act）
推荐落点：**act 工具** + 专用 port（`spica/ports/` 定 `BrowserControlPort` 白名单动作面）+ adapter + **host 闭包持权限**。仿 `sing_song`（act 范例）。
不应该做：让 LLM 传任意 URL/JS/命令直接执行（act 安全边界是 port 白名单，不是 effect flag）；exec/eval/shell 拼 LLM 字符串。
最小步骤：1) 定 BrowserControlPort（枚举允许的动作：导航到白名单域、点击、读文本…）；2) adapter 实现；3) host 闭包注册 act 工具，闭合权限；4) 失败回 ToolError。
先读：`spica/adapters/tools/sing_song.py`、`spica/host/app_host.py(_request_song)`、`spica/plugins/registry.py`(effect 注释 :107-111)、`spica/ports/game_launcher.py`(白名单 port 范例)。
查边界：**这是高危 act 能力，必须先写设计**（见 guardrails §14）；白名单域/动作枚举；权限不下放给 LLM。
跑测试：新 port/adapter 合同测试 + `test_registry` + 全量。
风险：**P1**（act 操控用户环境；CLAUDE §0 列为未立项候选，需先立项+设计）。

## 需求 6：新增 galgame 选项推荐
推荐落点：选项识别已在 `session.py`（choice 逻辑）；推荐文本经 **gated stage 注入 prompt** → run_turn 回复。选项检测两路径已有（`on_choice_detected`/`on_user_reported_choice`）。
不应该做：让描述型 VLM 直接生成精确选项文字（用 VLM 定位 + RapidOCR 抽字）；为「要不要荐」单跑一次 LLM；自己拼 prompt。
最小步骤：1) session 暴露当前选项快照（公共方法）；2) 扩 `retrieve_game_context_node` 把选项注入 prompt 段；3) run_turn 正常回复。
先读：`spica/galgame/session.py`、`spica/runtime/stages.py`(gate :528)、`spica/galgame/ocr_region.py`、`spica/ports/ocr.py`。
查边界：session 唯一状态 owner；OCR 不双加载；gate 纯请求逻辑不跑 LLM。
跑测试：`test_galgame_session`、`test_retrieve_game_context_node`、`test_current_line_injection` + 全量。
风险：**P2**。

## 需求 7：新增 galgame 主动吐槽策略
推荐落点：已有 `spica/galgame/reaction.py`（吐槽引擎）+ `reaction_judge.py`（评分）；新策略=调 `GalgameConfig` 的 reaction 参数 / reaction_table，或扩 judge 维度。开口仍走 `stream_system_turn`→run_turn。
不应该做：让 reaction worker 直接驱动 Qt/QTimer/QMediaPlayer 跨线程（已知 libQt6Gui 段错误史，必须 marshal 回 GUI 线程）；自建播报通道。
最小步骤：1) 改 `GalgameConfig` reaction_* 字段（schema.py，yaml-only）；2) 或扩 judge prompt/维度；3) 开口经 ProactiveTurnArbiter。
先读：`spica/galgame/reaction.py`、`spica/galgame/reaction_judge.py`、`spica/core/proactive.py`、`spica/config/schema.py(GalgameConfig)`。
查边界：开口走 run_turn(system)；judge 仅评分不产台词；**已知 P2**：judge 无 per-call 超时、OCR/TTS/judge GPU 争用（见 CODE_REVIEW §9）。
跑测试：`test_reaction_engine`、`test_reaction_judge`、`test_reaction_scoring`、`test_reaction_wiring`、`test_reaction_voice_duck` + 全量。
风险：**P2**（reaction 跨线程历史踩坑多）。

## 需求 8：新增视频陪看
推荐落点：**未立项候选**（CLAUDE §0）。复用看屏链路（截屏/VLM）+ 主动吐槽链路（proactive→run_turn）。需先写设计：采帧来源、节流、隐私边界（仿 OCR 截图边界）。
不应该做：把视频帧当 OCR 主路径暴力整帧识别；让帧描述直接成用户消息；自建第二套吐槽/播报链。
最小步骤：1) 先写设计（采帧 port + 节流 + 隐私门）；2) 复用 screen pipeline 看帧；3) 触发经 ProactiveTurnArbiter。
先读：`agent_tools/function_tools/screen/`、`spica/galgame/reaction.py`、`spica/core/proactive.py`、`CLAUDE.md` §4 截图边界。
查边界：隐私（只截授权窗口/区域）；GPU 争用；开口走 run_turn(system)。
跑测试：复用 screen/reaction 测试 + 新采帧 port 测试。
风险：**P1**（新子系统 + 隐私 + GPU，必须先立项+设计）。不确定，需要先读 screen pipeline 与 reaction 链。

## 需求 9：新增桌宠主动日常聊天
推荐落点：复用主动开口：定时/事件 → `ProactiveTurnRequest`（directive=日常话题）→ `ProactiveTurnArbiter`(drop_if_busy) → `stream_system_turn`→run_turn。
不应该做：另起播报定时器直接塞文本到 UI；绕 run_turn；忽略 busy 仲裁（会打断她正在说的话）。
最小步骤：1) 触发源（定时器/idle 检测）造 ProactiveTurnRequest；2) 经现有 arbiter；3) directive 文本即话题种子，台词由正常 prompt 角色化。
先读：`spica/core/proactive.py`、`spica/core/chat_engine.py(stream_system_turn)`、`spica/galgame/reaction.py`(范例)。
查边界：drop_if_busy；interaction_mode="system" 工具硬关断；NO_COMMENT 会被静默吞掉。
跑测试：`test_proactive_turn`、`test_no_comment_gate` + 全量。
风险：**P2**。

## 需求 10：新增 UI 设置项
推荐落点：纯 UI 偏好 → `ui/overlay_config.json` + `ui/overlay_config.py` + `ui/widgets/settings_panel.py`。影响后端行为的 → 走 app.yaml（见需求 11）。
不应该做：把后端配置塞 overlay_config.json；后端线程直接读 UI 控件；ui/ new 后端服务。
最小步骤：1) overlay_config 加字段 + 默认；2) settings_panel 加控件；3) controller 应用。
先读：`ui/overlay_config.py`、`ui/widgets/settings_panel.py`、`ui/qt_overlay.py`。
查边界：overlay_config 只放 UI 偏好；后端经事件/Host 接口。
跑测试：`test_layering` + 相关 ui 测试（importorskip PySide6）。
风险：**P3**。

## 需求 11：新增 app.yaml 配置项
推荐落点：`spica/config/schema.py` 对应子模型加 typed 字段（带默认）；消费方读 `deps.config.<域>`。需 env override 则进 `env_roster.py` + `manager._env_overrides()`。
不应该做：业务码 `os.getenv`；新开散落配置文件；改解析不 dump 基线。
最小步骤：1) schema 加字段（默认=旧值零 diff）；2)（可选）env 名进 roster+manager；3) `dump_resolved_config --out before`，改完 `--diff` 零差异。
先读：`spica/config/{schema,manager,env_roster}.py`、`data/config/app.yaml`、`scripts/dump_resolved_config.py`。
查边界：typed config 单一入口；env 名集中；song 节是 untyped（D-3a）。
跑测试：`test_resolved_config_equivalence`、`test_no_getenv`、`test_config_manager`、`test_env_centralization` + 全量。
风险：**P2**（配置解析改动，必有基线对账）。

## 需求 12：新增角色包 / 多角色
推荐落点：`CharacterConfig`（schema.py，`package_dir`/`character_id`/...）+ `spica/core/character.py::CharacterPackage` + `spica/conversation/character_loader.py`。长期记忆已按 `character_id` 命名空间隔离。
不应该做：忽略 **已知 P1**——recent_memory 仍按裸 conversation_id 未按角色命名空间（`chat_engine.py:235-240` TODO），切角色会串短期上下文，多角色前**必须先修这条**。
最小步骤：1)（前置）recent_memory 按 `scoped_conversation_id` 命名空间化 + 隔离测试；2) 角色包加载/切换；3) prompt/记忆/visual/tts 按角色解析。
先读：`spica/core/character.py`、`spica/conversation/character_loader.py`、`spica/core/chat_engine.py:231-252`、`spica/adapters/memory/sqlite.py`。
查边界：长期记忆 `{character_id}::{conversation_id}`；recent 命名空间化（P1 前置）。
跑测试：`test_character_package`、`test_character_template`、`test_recent_memory`、`test_ltm_cross_restart`、`test_memory_commit_scope` + 全量。
风险：**P1**（触发已知多角色 recent 污染风险，需先写设计）。

## 需求 13：新增记忆类型
推荐落点：角色记忆 → `MemoryConfig`/`MemoryScope`/`memory/store.py`/`extractor.py`/`adapters/memory/sqlite.py`。游戏侧 → `game_memory` 独立库（`adapters/game_memory/sqlite.py`），绝不混进角色 scope。
不应该做：把 galgame 数据写进角色 MemoryScope；让 OCR 文本进 recent；破坏 `{character_id}::{conversation_id}` 命名空间。
最小步骤：1) 选对库（角色 vs game_memory）；2) 加字段/表 + 抽取规则；3) 注入 prompt 经 prompt_builder 段或 gated stage。
先读：`spica/ports/memory.py`、`spica/adapters/memory/sqlite.py`、`spica/ports/game_memory.py`、`spica/adapters/game_memory/sqlite.py`、`memory/extractor.py`。
查边界：角色/游戏两库隔离；§27① effective_memory_conversation_id 语义；后台 JobRunner 写失败静默（已知 P2）。
跑测试：`test_memory_*`、`test_game_memory_adapter`、`test_session_summary` + 全量。
风险：**P2**。

## 需求 14：优化 OCR / VLM pipeline
推荐落点：`agent_tools/function_tools/screen/`（capture/backends/model_manager）+ `spica/adapters/ocr/rapidocr.py` + `spica/adapters/screen/local_moondream.py`。先跑诊断器。
不应该做：双加载模型；破坏串行 `_INFER_LOCK`（RapidOCR 非线程安全）；上传图片。
最小步骤：1) 先跑 `scripts/diag_ocr_providers.py`（疑回落 CPU）/`scripts/verify_watch_chain.py`；2) 在 backend/model_manager 优化；3) 保持单例 + 锁。
先读：`agent_tools/function_tools/screen/{capture,model_manager,backends/}`、`spica/adapters/ocr/rapidocr.py`、`scripts/diag_ocr_providers.py`。
查边界：单例不双加载；OCR 串行；本地不上传；GPU 争用（已知 P2）。
跑测试：`test_ocr_adapter`、`test_rapidocr_backend`、`test_rapidocr_lock`、`test_moondream_model_manager`、`test_screen_analyzer_streaming` + 全量。
风险：**P2**。

## 需求 15：优化唱歌 pipeline
推荐落点：`agent_tools/function_tools/song/`（pipeline/netease/separator/rvc/mixer）。入口仍是 `sing_song` 工具（act）→ host 闭包 → SongRequestEvent → SongWorker。
不应该做：恢复已删的「前置劫持/意图路由/第二 LLM 分类器」；让 LLM 传任意路径/URL；后端线程直接驱动播放 widget。
最小步骤：1) 在 song pipeline 内部优化（搜索/分离/RVC/混音）；2) 控制词快路径在 `ui/controllers/song_controller.py`；3) 唱完播报走 system turn。
先读：`agent_tools/function_tools/song/pipeline.py`、`spica/adapters/tools/sing_song.py`、`spica/host/app_host.py(_request_song)`、`ui/controllers/song_controller.py`、`ui/workers/song_worker.py`。
查边界：act 权限在 host 闭包；song 配置是 untyped override（D-3a）；播放控制走 UI 快路径。
跑测试：`test_sing_song_tool`、`test_song_control_fastpath`、`test_song_config_injection` + 全量。
风险：**P2**。

## 需求 16：优化 ReSpeaker 输入
推荐落点：`hardware/respeaker/`（audio/control/speech_worker）+ `RESPEAKER_*` 配置（`env_roster.RESPEAKER_ENV_MAP` + `respeaker_env_overrides`）。
不应该做：业务码 `os.getenv`（RESPEAKER_* 已在 roster，经 manager）；speech_worker 直接驱动 UI（用 signal）。
最小步骤：1) 改 audio/control；2) 配置经 respeaker_env_overrides；3) worker 用 Qt signal 回 GUI。
先读：`hardware/respeaker/{audio,control,speech_worker}.py`、`spica/config/env_roster.py(RESPEAKER_ENV_MAP)`、`spica/config/manager.py(respeaker_env_overrides)`。
查边界：env 经 config 层；硬件 VAD fallback；半双工现状（她说话时输入暂停）。
跑测试：`test_respeaker_audio`、`test_speech_worker_stt` + 全量。
风险：**P2**。

## 需求 17：增加全双工语音
推荐落点：**未立项，需先写设计**（现状半双工：她说话/唱歌时输入暂停）。涉及 `proactive.py` 的全双工钩子位、voice_input_controller、播放/输入仲裁。
不应该做：直接拆半双工互斥而不处理「她说话时听到自己 TTS」回授；绕 run_turn 的打断逻辑。
最小步骤：1) 先写设计（回声消除/输入仲裁/打断语义）；2) 复用 proactive 全双工钩子位；3) 改输入控制 + 播放打断。
先读：`spica/core/proactive.py`、`ui/controllers/voice_input_controller.py`、`ui/controllers/audio_controller.py`、`hardware/respeaker/`。
查边界：打断走 run_turn cancellation（不另造）；TTS 回授；状态机一致。
跑测试：`test_voice_input*`(若有)、`test_stop_button_interrupt`、`test_cancellation` + 全量。
风险：**P1**（跨子系统仲裁，必须先设计）。不确定，需先读 proactive 钩子位 + 输入控制。

## 需求 18：加性能 telemetry
推荐落点：`spica/runtime/observer.py`（TurnObserver）+ `common/timing.py`；turn 内计时经 `deps.observer`，**不准**直接 `log_timing`（有守卫 `test_no_log_timing`）。诊断脚本 `scripts/play_with_timing.py`/`monitor_resources.sh`。
不应该做：在 stages 里直接打时间日志（绕 observer）；裸 ThreadPool（有 `test_no_raw_threadpool`）。
最小步骤：1) 扩 TurnObserver 的 span/事件；2) 消费方经 deps.observer；3) 报告脚本读输出。
先读：`spica/runtime/observer.py`、`common/timing.py`、`scripts/play_with_timing.py`、`tests/test_turn_observer.py`。
查边界：计时经 observer；并发经 ExecStrategy；不破坏 turn 纯度。
跑测试：`test_turn_observer`、`test_no_log_timing`、`test_no_raw_threadpool` + 全量。
风险：**P2**。

## 需求 19：改 prompt
推荐落点：**唯一**在 `spica/conversation/prompt_builder.py::build_spica_prompt`（`[LONG_TERM_MEMORY]` 等段在此）。游戏上下文段经 `retrieve_game_context_node`(gate) 注入。
不应该做：在别处拼 prompt；为新段单跑一次 LLM 判断；把 OCR 原文堆进 prompt 不截断（注意 `game_buffer_tail_limit` 压体积）。
最小步骤：1) 在 prompt_builder 改/加段；2) 游戏侧上下文走 gate；3) 注意 prompt 体积（曾 28k 字符→6s 首 token）。
先读：`spica/conversation/prompt_builder.py`、`spica/runtime/stages.py(build_prompt + gate)`、`spica/config/schema.py(GalgameConfig.game_buffer_tail_limit)`。
查边界：prompt 单一组装点；gate 纯请求逻辑；体积/首 token 延迟。
跑测试：`test_prompt_builder`、`test_retrieve_game_context_node`、`test_golden_streaming`、`test_golden_sync` + 全量。
风险：**P2**（golden 钉死输出，改了要更新 golden 并确认是有意改变）。

## 需求 20：改播放队列 / 打断逻辑
推荐落点：`spica/runtime/{sequencer,play_unit_splitter,orchestrator}.py`（后端有序释放）+ `ui/controllers/{chat_stream_controller,audio_controller}.py`（前端播放）。打断走 turn `cancelled` Event + `is_turn_cancelled`。
不应该做：手动重排 play unit（有守卫 `test_no_manual_reorder`，必须经 Sequencer）；后端线程直接停 widget；绕 run_turn 的 cancellation。
最小步骤：1) 后端顺序逻辑在 Sequencer/orchestrator；2) 打断设 `cancelled` Event，三检查点短路；3) UI 播放在 controller。
先读：`spica/runtime/sequencer.py`、`spica/runtime/orchestrator.py`、`spica/runtime/context.py(is_turn_cancelled)`、`ui/controllers/audio_controller.py`。
查边界：有序经 Sequencer（不手排）；cancellation 防 ghost；后端不碰 widget。
跑测试：`test_sequencer`、`test_no_manual_reorder`、`test_cancellation`、`test_stop_button_interrupt`、`test_chat_stream_controller_*` + 全量。
风险：**P1**（动核心播放/打断控制流，改前写计划）。

---

## 速查：风险等级口径
- **P0**：会破坏主链路/架构边界，本仓库当前无 P0 需求项。
- **P1**：动核心控制流 / act 操控环境 / 跨子系统仲裁 / 触发已知 P1 风险——**必须先写设计**。
- **P2**：在既有 port/adapter/stage 边界内的扩展——按模板做 + 补测试。
- **P3**：UI 偏好 / 文档 / 隔离小改。

---

# Claude Code 新需求固定提示词模板（可复制）

```text
你现在在 Spica-Chatbot 仓库（本地桌面语音陪伴 App，核心是 galgame 陪玩）。

请先阅读：
- CLAUDE.md（§1 铁律 + 「📌 新会话必读」10 条硬规则）
- docs/DEVELOPMENT_GUARDRAILS.md（高危文件 + 落点决策树 + 各类改动模板 + 该跑哪些测试）
- docs/FUTURE_FEATURE_PLAYBOOK.md 中与本需求相关的章节
- 如需全局背景再读 docs/ARCHITECTURE_FOR_ALGORITHM_ENGINEERS.md（15 分钟快速通道）

本次需求是：
【在这里写需求】

硬约束（违反即破坏架构）：
1. 不准绕开 run_turn 让 Spica 开口（主动开口走 stream_system_turn→run_turn）。
2. 不准在 spica/ import Qt/PySide。
3. 不准在业务代码 os.getenv（配置只走 spica/config 层）。
4. 不准大范围重构 / 重命名 / 搬目录（要动先写迁移计划）。
5. 新能力优先走 ports/adapters/registry，不塞进 runtime/UI/Host。
6. 新 UI 能力必须经 UI bridge/worker/Qt signal，后端线程不碰 widget。
7. 新 act（有副作用）工具不准 exec/eval/shell/任意路径，动作经 host 闭包白名单面。
8. 新 galgame 功能不污染普通聊天 recent memory，且只经 GalgameCompanionSession 公共方法。
9. 不准删除/放宽守卫测试来让测试变绿。
10. 改配置先 dump 基线（scripts/dump_resolved_config.py），改完 --diff 零差异。

请先只输出（不要直接改代码）：
- 需求理解
- 影响范围（会动哪些文件）
- 推荐落点（对照 playbook 的哪一项）
- 不应该碰的文件 / 边界
- 最小实现步骤
- 测试计划（按 guardrails §13 + 收尾 python -m pytest tests -q）
- 可能风险（P0/P1/P2/P3）

等我确认后再实现。实现时在确认落点做最小改动；发现更大问题只记录、不顺手大修。
```

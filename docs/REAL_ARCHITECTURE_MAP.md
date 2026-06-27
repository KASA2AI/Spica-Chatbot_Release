# Spica 当前真实架构地图

> 第 1 趟只读审计产物。路径来自 `git ls-files` + 真实代码阅读 + 7 域 subagent 深读 + 主上下文复读自证。
> ✅ 已核验=读代码/测试确认；🔶=仍需第 2 趟核验。生成日期：2026-06-27。
> 全量测试 821 passed 佐证本图结论。

---

## 1. 一句话项目定义

本地桌面语音角色扮演陪伴应用（PySide6 透明 overlay + 语音对话），核心是 galgame 陪玩；LLM/TTS/记忆/屏幕识别都为「Spica 陪你玩 galgame、记得剧情和共同经历」搭桥。

---

## 2. 目录地图（真实，来自 git ls-files）

```text
webui_qt.py                     # 进程入口（Linux Qt/xcb/输入法/ALSA 垫片）→ ui.qt_overlay.main()
spica/                          # 平台核心（UI 无关，禁 import Qt）
  host/   app_host.py(836) agent_assembly.py builtins.py management.py warmup.py
  core/   chat_engine.py events.py state_machine.py character.py proactive.py
          companion_events.py song_events.py
  conversation/ prompt_builder.py reply_parser.py text_normalizer.py time_context.py
          character_loader.py character_compat.py
  runtime/ turn.py orchestrator.py(518) context.py deps.py stages.py(1088) tool_round.py(334)
          tools.py memory_commit.py exec_strategy.py jobs.py observer.py fold.py sync_chain.py
          llm_stream.py play_unit_splitter.py sequencer.py services.py tts_job.py visual_job.py
  ports/  llm tts visual memory tool stt screen + game_launcher window_locator
          screen_capture ocr game_memory（galgame 五端口）
  adapters/ llm/openai_compatible.py memory/sqlite.py tts/ visual/spica_diff.py
          stt/faster_whisper.py screen/local_moondream.py screen_capture/mss_visible_window.py
          ocr/rapidocr.py window_locator/linux_x11.py game_launcher/linux_desktop.py game_memory/sqlite.py
          tools/{screen,watch_game_screen,note_game_observation,sing_song}.py
  galgame/ session.py companion_controller.py ocr_loop.py text_stream.py summarizer.py
          models.py history.py binding.py ocr_calibration.py ocr_region.py window_match.py
          manual.py reaction.py reaction_judge.py
  config/ schema.py manager.py secrets.py env_roster.py runtime_env.py
  plugins/ registry.py host.py manifest.py
  memory/ （仅 __init__.py — port glue 命名空间，§10 命名陷阱）
agent_tools/
  function_tools/screen/ analyzer capture backends/{moondream,rapidocr} model_manager image_processing schema tool config
  function_tools/song/   pipeline netease separator rvc mixer models intent intent_rules config（intent*→UI 控制词，§6）
  tts/  manager base schemas adapters/{dummy,gptsovits_current} gptsovits/service vendors/GPT-SoVITS-…（vendored 排除）
  visual/ diff_service.py
memory/   recent.py store.py extractor.py control.py  （角色短期/长期记忆实现体）
hardware/respeaker/ audio.py control.py speech_worker.py
ui/   qt_overlay.py overlay_config.py controllers/ workers/ widgets/ models/
data/config/  app.yaml（typed config 唯一 app 载体）tts.yaml visual.yaml *.migrated（退役备份）
scripts/  dump_resolved_config verify_watch_chain diag_ocr_providers migrate_config_p0b reaction_*_report
tests/    ~130 文件（12 AST/语义守卫 + 域测试）；全量 821 passed
```

---

## 3. 启动链路（✅ 已核验）

```text
webui_qt.py::main()
  ├─ _check_linux_qt_xcb_dependency / _configure_linux_alsa_plugins / _configure_linux_input_method
  │     （直写 os.environ：entry 豁免，#10 territory，不在 test_no_getenv 扫描域）
  └─ ui.qt_overlay.main()                     ✅ test_env_centralization.py:136-157 AST 钉首句
        :1348 load_secrets()  ← 首句（铁律#10），先于一切构造
        :1357 QApplication → :1359 OverlayWindow.__init__
                 └─ AppHost()  __init__: resolve_effective_screen_config():142 / song:145（resolve-once）
                 └─ AppHost.initialize() (~84 行纯装配, app_host.py:201-284)
                       :210 ConfigManager().load()（app.yaml + env override；内部再 _ensure_env_loaded）
                       :211 load_secrets()
                       register_builtin_adapters()（builtins.py：LLM/TTS/Visual/Memory/inspect_screen）
                       watch/note/sing_song 在 __init__ 注册（host 闭包持权）
                       ChatEngine（注入 deps）+ reaction/galgame 闭包 + proactive arbiter + management
        :1360 show → :1361 app.exec()（companion sink 已在 exec 前挂）
```

---

## 4. 普通聊天 turn 链路（✅ 已核验 runtime）

```text
UI → ChatEngine.stream_voice()（legacy dict 兼容）→ stream_voice_runtime()
  → orchestrator.stream_voice_events() → run_turn（唯一 emit，turn.py:43-44）
       validate_input → load_recent_context → retrieve_long_term_memory
       → analyze_screen_attachment → retrieve_game_context_node(gate) → build_prompt
       → call_llm / tool_round(probe→exec→followup) → parse_reply
       → save_stream_memory(②cancel 检查) → visual_job ∥ tts_job → Sequencer 按 index 有序
  → RuntimeEvent → chat_engine.py:208 转 legacy dict → ChatStreamController → 播放
同步: ChatEngine.run_voice() = run_turn + fold_events（fold.py）
冻结锚: sync_chain.py（生产零调用方，仅 ~7 测试引用 run_voice_pipeline）
```

---

## 5. 截图 / 看屏链路（✅ 已核验 tooling，本地不上传）

```text
自动: inspect_screen(read,always,intent_gated) → adapters/tools/screen.py
       → agent_tools/function_tools/screen: mss 截图 → RapidOCR(本地,_INFER_LOCK) → Moondream(本地,N0 never uploaded) → JSON 注入
陪玩: watch_game_screen(read,companion-only) → 绑定窗口截图 + Moondream（capture 前查安全态）
手动: 截图按钮 → ui 框选 → pending_screen_attachment → 下一条消息 → analyze_screen_attachment
```

---

## 6. sing_song 链路（✅ 已核验：act 纪律成立）

```text
主 LLM 经 sing_song function call(act,intent_gated)
  → adapters/tools/sing_song.py（纯转发垫片，非空校验）
  → host 闭包 _request_song（app_host.py:673-694：网易云白名单搜索）→ SongRequestEvent（RuntimeEvent 子类）
  → UI 桥 → SongWorker → agent_tools/function_tools/song/pipeline: 搜索/下载→人声分离→Applio/RVC→混音→播放
  → 唱完主动开口（系统 turn）
残余前置规则: 播放控制词快路径（song_controller.py:167-190，is_busy()+conf≥0.9+{暂停/继续/停止/重唱}）
  ↑ 复用 parse_song_control_intent（intent.py/intent_rules.py 迁此，非 orphan；LLM 侧意图分类器已删）
```

---

## 7. galgame 陪玩链路（✅ 已核验 galgame）

```text
GalgameCompanionController.start/stop → GalgameCompanionSession（唯一状态 owner，RLock session.py:168）
  → ocr_loop.py:132-147 串行「完成后等待」泵（RapidOCR 锁）
  → on_ocr_result → StableLineTracker 去重 → _unsummarized_lines（私有 buffer，绝不进 recent）
  → summarizer 后台总结（锁内切不可变 snapshot session.py:463，LLM 锁外跑只读 StoryLine）
  → game_memory 独立库 spica_data/galgame.sqlite3
问答: ChatEngine → run_turn，gate retrieve_game_context_node（stages.py:528，build_prompt 后/LLM 前，
      纯请求逻辑 _game_context_mode:316，none 时 byte no-op，绝不跑 LLM）
事件: CompanionEventBridge 跨线程 → UI（companion_events.py 全 RuntimeEvent 子类）
崩溃恢复: recover_dangling_sessions（summarizer.py:164←app_host.py:763 启动），失败留 ended_at=NULL 幂等重试
ChoiceEvent 两路径: on_choice_detected(OCR) / on_user_reported_choice(用户)
```

---

## 8. proactive / reaction 主动开口链路（✅ 已核验）

```text
域事件 → ProactiveTurnRequest → ProactiveTurnArbiter.try_speak(drop_if_busy, proactive.py:123-133)
  → ChatEngine.stream_system_turn（interaction_mode="system"，工具供给双处硬关断）
  → 同一条 run_turn → 同一条播放管线 → UI 消费 StreamKind.SYSTEM；NO_COMMENT → system_silent 吞掉
galgame 主动吐槽: reaction.py → _speak → 同上 ProactiveTurnArbiter → run_turn
  reaction_judge.py: 独立 JUDGE LLM，仅评分（JudgeVerdict worth/moment/angle），不产用户台词；失败降级 lexicon scorer
```

---

## 9. 配置链路（✅ 已核验）

```text
三载体:
  data/config/app.yaml → ConfigManager.load()（manager.py:97-104）→ AppConfig（Pydantic, 10 节:
    llm/memory/character/stream/galgame/stt/screen/song(untyped)/plugins/max_tool_rounds, schema.py:300-314）
  xiaosan.env → load_secrets() → Secrets（OPENAI/JUDGE key；注：env 实为 override 层，可承载非密钥）
  ui/overlay_config.json → UI 偏好
env override: SCREEN/RESPEAKER 名结构 import env_roster；APP 级 manager 硬编码（手工镜像 + 测试钉名，§见报告 P3）
只 manager/secrets/runtime_env 碰 os.environ（test_no_getenv 守）；解析等价 test_resolved_config_equivalence:49 passed
```

---

## 10. 记忆链路（✅ 已核验）

```text
短期: memory/recent.py::RecentMemory（内存 deque，裸 conversation_id）—— sync 写
长期: memory/store.py → ports/memory.py::MemoryPort → adapters/memory/sqlite.py
      namespace {character_id}::{conversation_id}（:30）—— async JobRunner 写（memory_commit.py:74-82）
游戏记忆: ports/game_memory.py → adapters/game_memory/sqlite.py（独立库，OCR 剧情只进此）
Phase 0 耦合点(✅): galgame turn conversation_id="galgame::…" 但 memory_conversation_id=caller scope；
      effective_memory_conversation_id（context.py:114-120）使 galgame 仍读到角色「default」长期记忆，
      抽取写回 caller scope（§27① 写读对称，memory_commit.py:67 / stages.py:25）
命名陷阱: spica/memory/ 仅 __init__（port 层）；真实实现在根级 memory/
P1: Phase 7 多角色 recent 未按 character 命名空间（chat_engine.py:235-240 TODO）
```

---

## 11. UI 和后端边界（✅ 已核验，无越界）

```text
后端→UI 只经 RuntimeEvent / CompanionEventBridge / worker Qt queued signal；后端线程不碰 widget
UI 不 new LLM/TTS/Memory/Visual，全经 AppHost；不碰 galgame domain 内部（只经 host factory + 事件）
controller/worker: ChatStreamController（消费事件流/推进播放）/ AudioController（QMediaPlayer，teardown defer 防死锁）
  / SongWorker / SpeechWorker(hardware) / CompanionActionWorker / CompanionEventBridge（galgame 事件跨线程）
近期跨线程修复: 46a926b（系统 turn marshal 回 GUI 线程）/ 527c6bc（音频 teardown QTimer.singleShot defer）已并入
```

---

## 12. ports / adapters / registry 边界（✅）

```text
ports/  纯接口: llm tts visual memory tool stt screen + galgame 五端口
adapters/ 每 port 一实现，按平台/provider 命名
registry CapabilityRegistry（plugins/registry.py）register_llm/tts/visual/memory/tool（4 维元数据）
工具元数据: available / intent_gated（纯供给预筛，不劫持）/ chainable / effect(read|write|act)
INVARIANT N5: runtime 经 registry 解析工具，从不重 import 静态 schema（tools.py:15-17）
```

---

## 13. 测试守卫索引（✅ 全量 821 passed）

```text
test_layering          Qt 隔离 + agent 包删除 + RuntimeEvent 仅 facade 产（AST）
test_no_getenv         env 集中，allowlist=3，floor>100（AST）
test_env_centralization #10：qt_overlay.main 首句 load_secrets（AST:136-157）+ runtime_env 垫片 + DeepSeek legacy warn
test_resolved_config_equivalence 配置解析 env>file>default 各 coercion + env 名册 meta-pin（:257-282）
test_turn_contract     turn 事件契约 7 场景
test_no_dict_config / test_no_static_tool_schemas / test_no_manual_reorder /
test_no_raw_threadpool / test_no_log_timing / test_no_comment_gate / test_sqlite_concurrency_pragmas
诊断器: scripts/{verify_watch_chain, diag_ocr_providers, dump_resolved_config, dump_when_frozen}
```

---

## 14. 高风险文件索引

```text
adapters/game_launcher/linux_desktop.py  subprocess.Popen 启游戏（✅ 仅 XDG .desktop，OS 信任）
adapters/window_locator/linux_x11.py     subprocess.run 查窗口（✅ 只读）
adapters/tools/sing_song.py              唯一 act 工具（✅ host 闭包 + 白名单 + ToolError）
adapters/llm/openai_compatible.py        唯一真实 LLM 调用面
galgame/session.py / ocr_loop.py         并发 owner / 串行 OCR（✅ RLock + RapidOCR 锁）
galgame/reaction_judge.py                judge LLM（🔶 无 per-call 超时，P2）
runtime/orchestrator.py + turn.py        唯一 emit + 流式编排
ui/qt_overlay.py                         进程 GUI 入口（✅ #10 首句 load_secrets）
```

---

## 15. 不确定点（喂给第 2 趟）

**本轮已解决（不再不确定）**：
- #10 load_secrets 时序 → ✅ 成立，test_env_centralization AST 钉。
- AppHost 是否变胖 → ✅ 薄（initialize ~84 行，余量是必须留 host 的闭包）。
- galgame reaction 开口 → ✅ 经 stream_system_turn→run_turn；judge 仅评分。
- song intent.py/intent_rules.py → ✅ 非 orphan，迁 UI 控制词快路径。
- Phase 0 耦合点（galgame 切 conversation_id 后能否取角色长期记忆）→ ✅ 能（effective_memory_conversation_id）。

**仍需第 2 趟核验**：
- galgame GPU 争用（OCR/TTS/judge 共享 GPU）是否有 freeze 风险（galgame subagent 仅 30% 置信）。
- reaction_judge 无 per-call 超时是否会阻塞 worker（P2）。
- OCR 隐私门 check→capture 残余 race 是否需收紧（P1，有 mitigation）。
- store.py upsert importance 跨 galgame/chat 是否互顶（P2）。
- game_memory source_line_ids 每次 retrieve 全表扫（O(n²)）大规模影响（P2）。
- ui/qt_overlay.py 行数/职责是否值得拆（量化未做）。

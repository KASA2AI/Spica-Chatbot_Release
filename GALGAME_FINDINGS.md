# GALGAME_FINDINGS — galgame 陪玩系统欠账与事实清单

> 来源:路 B 阶段 2 之前的全项目架构回顾(产物二,2026-06 审定)。
> **纪律:任何一条的状态变化(修复 / 决策 / 否决)时更新本文件;新欠账随发现追加。**
> 分层底图见当轮回顾产物一(会话记录);完整规格见 `docs/GALGAME_COMPANION_PLAN.md`;铁律见 `CLAUDE.md`。

## 欠账清单

| # | 项 | 锚点 | 影响 | 状态 / 处置 |
| --- | --- | --- | --- | --- |
| 1 | **declare_route seam-only**:玩家声明线路无真实调用点(§13.5 承重墙只有"防 LLM 覆写"一半在用) | `spica/galgame/session.py` `declare_route`;调用点仅 `tests/test_session_summary.py` | 玩家确认线路入口悬空 | ⏳ 后续(自然落点 = command intent / UI,阶段 2 不开口子) |
| 2 | **anemoi 同源问题**(产品决策挂起) | 无代码锚点,详见下方真机事实存档 | 玩 anemoi 时角色记忆与游戏剧情可能互相打架 | ⏸ 挂起的产品决策;#4 的记忆分流使风险可控但不消除 |
| 3 | **active=buffer / offline=summaries 语义坑**:active 原不注入 `[RECENT_GAME_SUMMARIES]`,且 `_merge_progress` 不写 `current_scene_summary`(恒空)、总结触发即清 buffer → 总结过的叙事在陪玩 prompt 中只剩 major_events 标题 | `spica/runtime/stages.py` `_build_game_context_sections`;`session.py` `_merge_progress` | 陪玩中问"刚才/上次剧情"断片 | ✅ **阶段 2 已处理**:active 分支追加 `[RECENT_GAME_SUMMARIES]`(limit 2,offline 维持 5),gate 判定未动;守门 `tests/test_retrieve_game_context_node.py::ActiveSummariesInjectionTest`(含 buffer/summaries 防双重注入墙) |
| 4 | **长期记忆读写不对称**:读用 `effective_memory_conversation_id`(§27①),写曾用 raw `conversation_id` → 陪玩中聊出的"麦的事实"会困死在 galgame 命名空间 | 读 `stages.py` `retrieve_long_term_memory_node`;写 `spica/runtime/memory_commit.py` | 退出陪玩后角色记忆检索不到陪玩期间内容 | ✅ **阶段 2 已修**:`memory_commit.py` 写 scope 改 `effective_memory_conversation_id`(普通 turn fallback 逐字节不变);独立守门 `tests/test_memory_commit_scope.py`(3 例,含 recent 隔离不被顺带破坏) |
| 5 | **GPU 资源立场**(v1 已定:对话期间 OCR 照跑) | 见下方真机事实存档 | 阶段 2 起"OCR 采样 + 远端 LLM + 本地 TTS"并发为常态 | ✅ 立场已定(v1 不暂停不降频);协调**预留位归 controller**;**阶段 4 复核(观察待定)**:真机对话期 cycle 300-660ms 为**稳态抖动非累积**(OCR 串行 finish-then-wait 无积压机制);N=2 下 660ms cycle 仍必收停留 ≥1.4s 的台词,风险窗仅"语音播报期间 <1.4s 闪现行"。**让路触发条件写死**:真机出现与语音播报时间相关的漏句才启用(届时 controller 一方法 + host TTS 回调挂接);Moondream 落地时重评 |
| 6 | **stable=2 极端快翻固有边界**;B 方案"完整句旁路"已评估未实施 | `spica/galgame/text_stream.py` `StableLineTracker`(N=2, 0.9) | 极端快翻偶漏句 | ⏳ 可选优化:interval 修复(0.3 透传)后正常快翻不漏;真机复现再上 B 方案(可配置、默认安全、不破 golden frames) |
| 7 | **speaker 解析未适配 `【】`**(LimeLight);OCR 偶截存档槽文字、句末标点小错 | `text_stream.py` `_BRACKET_SPEAKER`/`_PARSE_FROM_TEXT`/`resolve_speaker`;后两条真机观测 | summaries 全 `—:`、名字混台词;将来吐槽认不出对象 | ✅ **speaker 解析已修(2026-06-11,主动吐槽前置)**:真机病灶有两层——limelight profile 是 `strategy="region"`+`speaker_name_region=None`,parse 分支从未启用(1118 行 speaker 0 命中);正则本身也不识 `【】`。修复:① 有序模式表,括号系优先(容忍丢 `】` 18 行/丢 `【` 50 行/闭引号错读 `』]1`/`【大梦 粘连`以空白界名);② region 空结果回退文本解析(持久化 profile 零迁移);③ 前缀路径收紧防旁白假说话人(无空白/句内标点、≤6 字、句中闭引号=旁白嵌引用拒切、纯数字名=时钟拒切)——假说话人会让吐槽认错对象,精度优先。**1118 行真机回放:779 行切出干净说话人、残渣零、假名零**(查珠=杏珠 OCR 误读属名字级噪声,不修);历史脏行明确不迁移。测试 `test_stable_line.py` 23 例(15 新,全部真机原文 golden)。剩余:存档槽误截靠区域微调(不变) |
| 8 | **对话框比例未固化闭环**:校准器能写 `GameProfile.ocr_profile`,但 `controller.start()` 不读它 → 每次手填 `--dialog/--window-id`;PLAN §18 链 B 校准 UI 未做 | `spica/galgame/ocr_calibration.py`(能写);`companion_controller.py` `start()` | 真机使用体验 | ✅ **阶段 3 已闭环**:`start()` 缺省读 profile(显式传参赢、未校准早抛无 dangling)+ `has_calibrated_dialog_region` 下沉;UI 自动弹框选(复用 ScreenshotSelectionOverlay + Phase 6 calibrator/preview),有则静默复用;守门 `ProfileRatiosTest`/`HasCalibratedDialogRegionTest`。**已知限制:dpr≠1 / 混合 dpr 多屏的框选坐标换算未承诺**(`selection_to_physical_rect` 仅均匀 dpr 缩放,X11 dpr=1 恒等) |
| 9 | **双 interval 旋钮未对账**:`OCRProfile.interval_seconds`(默认 1.0,**无消费者**)vs `config.galgame.ocr_interval_seconds`(0.3,真旋钮);另:`galgame::` 前缀字面量三处有意重复(`runtime/context.py` / `stages.py` / `models.game_conversation_id`,护栏优先于去重,值锚测试 `tests/test_turn_context.py::GameTurnBindingTest`) | `spica/galgame/models.py` `OCRProfile`;`spica/config/schema.py` | 迷惑性死字段 / 字面量分散 | ⏳ 后续清理(v2 收编或删除;前缀待 gate 代码允许触碰时统一) |
| 10 | **`recover_dangling_companion_sessions` 无人调用**;§12 "问用户要不要恢复"UI 延后 | `spica/host/app_host.py`;调用点 `ui/qt_overlay.py` `_start_dangling_recovery` | 崩溃残留 dangling session 永不补总结 | ✅ **阶段 3 已接入**:warmup 完成(成功或失败)后由后台 worker 静默补总结,只打日志;"陪玩中直接关闭 Spica"也走这条兜底(closeEvent 后台 stop 等 3s 超时放弃 → 留 dangling → 下次启动补)。ask-user UI 继续延后 |
| 11 | **unsummarized 反查全表扫描**:union 全部 summaries 的 source_line_ids + 扫全部 committed 行;阶段 2 起每个 active turn 都走这条路 | `spica/adapters/game_memory/sqlite.py` `_summarized_line_ids`(注释已写优化方案);port 注记 `spica/ports/game_memory.py` | 长游玩库变大后 turn 延迟上升 | 👀 **观察待定(阶段 4 实测定案)**:真实 adapter、98% 已总结稳态——5k 行(≈5h)25ms / 20k 行(≈20h)105ms / 50k 行(≈50h)270ms,线性 O(总行数) ≈5µs/行(JSON 逐行反序列化为主),仅作用于陪玩中的对话 turn(叠加在秒级 LLM 延迟上)。**决策:接受 40h/200ms 量级,纯观察**;触发优化条件 = 真机长期通关后问答明显变卡,届时按 port 注释做增量标记 / (summary_id, line_id) 索引表 |
| 12 | **demo 群定位**:stream / summary demo 用 tempfile 库(**属预期**,各验 Phase 7/8);companion demo 用真库 + 阶段 2 起带 `--ask` 探针;calibration demo 验 Phase 6 | `galgame_ocr_stream_demo.py` / `galgame_summary_demo.py` / `galgame_companion_demo.py` / `galgame_ocr_calibration_demo.py` | 验收工具,非产品路径 | ℹ️ 现状记录;阶段 3 后逐步被 UI 取代 |
| 13 | **UI 主窗口零接线**:CompanionEventBridge / GalgameController 已建未实例化,仅 demo attach 过 sink | `ui/qt_overlay.py` `_init_companion_ui` / `ui/controllers/galgame_controller.py` | 阶段 3 主体工作 | ✅ **阶段 3 已接线**:🎮 入口(WindowControls,checked 仅事件回写)、选窗 picker、自动校准流、状态 chip、CompanionActionWorker(stop 等重操作绝不在 UI 线程)。attach 时序由结构保证:attach 在 `__init__` 内,而唯一构建 controller 的路径(UI 事件)只能在 `app.exec()` 之后派发 |
| 14 | **`game_conversation_id` 无生产调用点** | `spica/galgame/models.py` | — | ✅ **阶段 2 已启用**:`companion_controller.start()` 发布的 `GameTurnBinding.conversation_id` 用它构造 |
| 15 | **陪玩外记忆不可达(三道门机理)**:① gate none 分支字节级 no-op(stages.py `retrieve_game_context_node`)→ 普通 turn 不读 game memory;② recent 按 conversation_id 隔离(memory_commit append + load_recent)→ 陪玩问答不进 default 短期上下文;③ OCR 剧情行只走 session/text_stream,永不进角色记忆(铁律 §4)——三者各自正确,合成 = Spica 在陪玩外对玩过的游戏完全失忆 | 真机发现;机理锚点同左 | 陪玩外问"我玩的那个游戏"答不上 | ✅ **B 方案已落地(游玩履历桥)**:stop 正常 end 后 + recover 補總結后,把履历卡 upsert 进角色记忆 default scope(`memory_key=galgame_history:<game_id>` 同游戏覆盖、scope=relationship、importance=0.85 不 pinned、≤220 字、"游戏"框架措辞防 #2 同源混淆、§13.5 三档线路措辞,阈值 `history.ROUTE_CONFIDENCE_THRESHOLD=0.6`)。**A 方案(普通 turn 注入履历段)已评估已否决**:须打破"none 分支字节级 no-op"护栏 + golden 全部重基线,对 turn/记忆系统是不可接受的整体风险;B 走现有检索通道,零新注入面。**铁律 #8 口径**:galgame 域只产出卡文本(注入回调,同 emit sink 形制),写动作/落点(store、scope、key)全在 host 闭包——"galgame 对角色记忆只读"维持成立。**两个已接受缝隙**:(a) end 完成后、回调执行前进程被杀 → 该次履历丢失且 recover 不补(session 已 ended;下次玩同游戏即重写);(b) 无 LLM 配置时 recover 直接返回空 → 履历也不写 |
| 16 | **(非 galgame)UI 启动 uuid 致长期记忆跨重启失忆**:主程序 `OverlayWindow.conversation_id` 自 Initial release 起是 `str(uuid.uuid4())`(每次启动随机)→ 所有自动抽取的长期记忆写读都在 `spica::<本次启动uuid>` 孤岛,重启即孤儿——角色长期记忆跨重启实际失忆。uuid 对 recent(进程内 deque)零作用、无任何其他消费点(grep 穷尽 + git 历史证实非有意设计),唯一实际效果就是切碎 silo。**履历卡(#15)是第一个跨启动读回的用例,把它撞了出来**(卡在 `spica::default`,打分健康 2.60/5 项命中,败在 WHERE 扫描集) | `ui/qt_overlay.py`(原 :80);诊断:写 `spica::default` vs 读 `spica::<uuid>` 并排复现 + store 打分复算;检索诊断探针留 debug 级于 `spica/adapters/memory/sqlite.py` `retrieve()` | 长期记忆系统级(非 galgame 局部) | ✅ **A 方案已修**:`conversation_id = "default"`(与 run_voice 默认/remember/§27①/demo/golden/履历卡全链对齐,一行);回归守门 `tests/test_ltm_cross_restart.py`(跨"启动"可达 + uuid 孤岛机理反向钉死)。**旧 uuid 孤岛为遗留死数据**,可选清理:`sqlite3 spica_data/memory.sqlite3 "DELETE FROM memories WHERE conversation_id LIKE 'spica::%-%-%-%-%';"`(uuid 形如含连字符;清理前可先 SELECT 核对) |

| 17 | **(非 galgame)song 前置分流 = 历史遗留"第二条 LLM 路径"**:song 触发不在 function-call 体系——UI 层 `interaction_controller.handle_user_text` 在消息进 chat **之前**用 `route_text`(规则三层 + **独立 LLM 意图分类器** deepseek,song_config.json `llm_fallback.enabled:true`)判定,命中即整条消息劫持进唱歌流程,主 LLM 全程缺席。误触发三源:LISTEN 句中"想听"任意位置匹配 / IDLE 弱信号(句含 唱\|歌\|音乐)调分类器(3s 延迟,陪玩聊 BGM 必中)/ SEARCH 确认流打断 | `ui/controllers/interaction_controller.py` / `agent_tools/function_tools/song/intent_router.py` / `intent_rules.py` | 普通聊天被劫持/延迟;触发层词表既漏又误(watch 同根,真机证实) | ✅ **B1 已修(触发层重构)**:劫持面收窄到确定性意图(SING≥0.9 明确歌名 / 控制词仅流程活跃态 / CONFIRM+SEARCH 仅确认态),LISTEN 句中模式删除,IDLE 弱信号分类器分支删除(CONFIRMING 态 fallback 保留);**明示代价**:句中合法点歌("加班累了,我想听X")落普通对话,需句首形式或明确指令(既有测试钉已按此更新)。watch 侧同期改为**状态供给**(registry `available` 谓词 + `intent_gated=False`,陪玩态无条件进工具集,调不调=LLM 结构化决策;词表删除)。**B2 后续立项**:song 工具化进主 LLM function call,废前置分流与独立分类器(收编第二 LLM 路径)。**P0a(2026-06-11)**:`song_config.json llm_fallback.enabled→false`——配置审查实锤分类器因 F19 时机 bug(SongController 构造早于 `load_secrets()` 的 dotenv 灌注,`DEEPSEEK_API_KEY` 构造时不可见)**从未真正启用过**,关闭只是承认现状;F19 本体已修(main 入口先灌 env,CLAUDE.md 铁律 #10)。**✅ B2 已落地(P2,2026-06-11)**:song 工具化为主 LLM 的 `sing_song` function call(`spica/adapters/tools/sing_song.py` + host 闭包 `_request_song`:同步 netease 搜索→`SongRequestEvent` 经 RuntimeEvent 桥→UI 起 SongWorker→fire-and-acknowledge,确认语由 run_turn 生成带 TTS——F14 写死台词治愈;effect="act" 首个操作类工具;供给=词表预筛 `intent_gated=True`,词表从"劫持判决"降级为"供给预筛")。**净删 1703 行 / 新增 213**:intent_router/intent_llm/trigger 整文件、intent_rules 473→120(只剩控制快路径)、INTENT_CONFIRMING 确认态+pending_song_hint+SONG_PRELUDE 合成 prompt 流+15 处写死台词全灭;控制反馈降 UI 状态 chip。保留:管线本体、playback gate、AudioOwner;控制快路径=播放态 PAUSE/RESUME/CANCEL/RESTART(CHANGE 移交主 LLM)。**AudioOwner 仲裁钉死**:READY 歌曲经 prelude gate 排队等当前 turn 语音播完(`notify_on_current_stream_done`),不抢占(`AudioOwnerArbitrationTest`)。**B1 的"句中点歌"代价被治愈**(主 LLM 理解语境)。守门 `test_sing_song_tool.py`+`test_song_control_fastpath.py`(12 例);唱完静默收尾,"播完了~"主动播报=P3 首用例 |
| 18 | **(系统级)chat_completions 路径自初版起无工具轮**:`tool_round.py` 流式 probe 对 `prefers_chat_completions()`(= base_url 含 "deepseek")客户端**整段跳过**、`call_llm_node` 同分支直接 `complete_chat` ——工具 schemas 走完供给层(`schemas_for_user_text` 正常返回)却**永不进请求体**。后果:deepseek 端点上 **`inspect_screen`/`watch_game_screen` 从未真正可用过**,"她描述画面"全是旧 observation/编造;watch 零触发四轮排查的最终根因。**铁证**:用户真机 TIMING 链含 `llm_chat_stream_create`(仅 chat 回退路径产生)+ `scripts/verify_watch_chain.py` 离线单步(场景 B:站a schemas 在、仅 1 次无 tools 调用)。**方法论教训(四轮失败的原因)**:诊断行与 ToolsFieldTest 全钉在 `call_llm_node`(同步链),而**生产 run_voice/stream_voice 都走 orchestrator → tool_round,sync_chain 生产零调用方**——"测试绿但测错链" | 断点 `spica/runtime/tool_round.py`(原 :50-53);判定 `openai_compatible.py::_prefers_chat_completions`;活体诊断器 `scripts/verify_watch_chain.py`(下次"工具不触发"先跑它) | deepseek 端点全部 function-call 能力静默失效 | ✅ **已修(2026-06)**:① tool_round chat 分支补 chat 工具 probe(`deps.llm.create_chat_with_tools` → tool_calls → 复用同一 `_run_tool_calls`/followup 链);② adapter `create_chat_with_tools` + 纯函数 `to_chat_completions_tools`(Responses 扁平 → `{"type":"function","function":{...}}` 嵌套,可单测);③ `call_llm_node` 同款病灶同期修(chat 工具循环镜像 Responses 循环);④ **无 tools 请求体逐字节不变**显式守门(`{"model","messages","stream"}` / sync `{"model","messages"}`);守门 `tests/test_chat_tool_round.py`(11 例,全部钉在 stream_voice/run_voice_pipeline 真实链上,装配 = verify 脚本场景 B) |
| 19 | **(系统级)Moondream 权重每次重启重下 3.85G**:TTS service `_configure_runtime_cache_dirs` 把 `XDG_CACHE_HOME` 进程级指向 `/tmp/spica_chatbot_cache/xdg`(本意是挡 vendored runtime 的 numba/matplotlib 缓存垃圾),而 huggingface 缓存解析链 `HF_HOME → $XDG_CACHE_HOME/huggingface → ~/.cache/huggingface` 在第二步被连带劫持;TTS 初始化早于 Moondream 懒加载 → 权重全落 `/tmp`,重启即清、下次看屏重下(**同 boot 内重启命中 /tmp 缓存不重下,重启才重下**)。revision 钉死 `2025-06-21` 无漂移,非上游更新问题 | 劫持源 `agent_tools/tts/gptsovits/service.py::_configure_runtime_cache_dirs`;受害加载点 `agent_tools/function_tools/screen/backends/moondream.py::load`(`from_pretrained` 未传 cache_dir,走 HF 默认链) | 每次重启后的首个看屏 turn 卡 ~2 分钟拉 3.85G | ✅ **已修(2026-06-11)**:劫持源头在改 XDG 之前先钉 `HF_HOME=~/.cache/huggingface`(用户已自设则不动,与该函数既有模式一致);已下权重(moondream2 + remote code 伴生的 starmie-v1)从 /tmp 迁入持久缓存;`local_files_only` 离线冒烟命中 `~/.cache/.../snapshots/9a7d402…`。顺带发现:`~/.cache/huggingface/hub/tmprjr186on` 是 2024-05 的 4.1G 孤儿下载暂存,可手动清 |
| 20 | **(系统级)P1:多轮工具循环上生产流式链**(架构审查 F2/F8 清偿):此前生产链(stream_voice→tool_round)硬单轮,`max_tool_rounds` 的多轮循环只活在生产零调用方的 sync 链上——#18 的镜像("能力钉在没人走的链上")。方案 D:工具注册声明 `chainable`(默认 False),**单发工具(watch/note/inspect)代码路径零分叉逐字节不变**(test_chat_tool_round 不改一行全绿为放行硬证);chainable 工具进 round 2..max_tool_rounds 链式 probe(非流式,复用 #18 的 create_chat_with_tools/create_responses + prefetched 通道,不引入流式 tool_call 解析);超限走**优雅强制收尾**(无 tools 流式 followup + "不要再调用工具"尾注 + WARNING + obs `tool_loop_exceeded`,她总开口)。**sync_chain 定位冻结:纯 golden 锚,生产零调用方,不长新能力**,超限保留历史 error 语义(golden 钉),两链差异注释互指。F4 同役:followup 压缩两层化(工具自声明 `compact_output`——inspect 注册其历史压缩器,逐字节等价;8000 字符头尾截断全局兜底,现有工具实测 1-2KB 永不触发)。链式收尾为 prefetched 弹出非流式(明示接受的对价);浏览器立项时复评 | `tool_round.py::_run_chain_rounds`/`_any_chainable`;`plugins/registry.py::register_tool(chainable, compact_output)`;`stages.py::_compact_tool_history_for_prompt`(两层);守门 `tests/test_tool_chain_rounds.py`(8 例,真链装配) | 浏览器操控(P6)与 B2(P2)的硬前置 | ✅ **P1 已落地(2026-06-11)** |
| 21 | **(系统级)P3:turn 发起器——主动开口能力上线**(架构审查 F1/F5 清偿:对话发起权此前 100% 在 UI):`spica/core/proactive.py`(Qt-free)= `ProactiveTurnRequest`(directive/source/conversation_id/policy/ttl,**字段零域假设**,域只撰写 directive 文本)+ `ProactiveTurnArbiter`(纯策略类,回调注入;v1 仅 drop_if_busy,queue_latest+ttl 留位给 P5 吐槽;busy=对话忙 OR 录音段在飞)。系统 turn 走唯一对话路径:`ChatEngine.stream_system_turn` 把 directive 包系统框架文本(`compose_system_directive_message`,单一来源)走既有 `stream_voice` + `interaction_mode="system"`(现成类型化通道,galgame gate 先例)——run_turn/orchestrator/prompt_builder/memory_commit 零分叉;recent memory 存框架后文本自标识系统事件不冒充麦。**自激硬关断**:系统 turn 工具供给恒空(tool_round+stages 同型 gate)——否则"唱完了"播报经词表供给 sing_song 会自己再点歌(设计期自查堵死,测试钉:directive 含唱仍无 tools 字段)。UI 消费:`StreamKind.SYSTEM` + `start_system_turn`,播放/busy/抢占全复用(用户消息恒经 stop_current 抢占自发流);`_on_current_stream_done` 升级多播列表(song prelude 门与 arbiter 恢复点可共存)。**全双工钩子位**:arbiter 收 `VoiceInputGate`(before/after_system_speech,v1 NullGate 零行为)——将来 AEC/输入过滤装进 gate,不回头改发起器(全双工课题在档不动)。首用例:`finish_song_playback` 提交含歌名 directive → 她 run_turn 角色化播报"唱完了~"(非写死台词);陪玩中播报自动带游戏上下文=已接受行为 | `spica/core/proactive.py`;`chat_engine.stream_system_turn`;tool gate `tool_round.py`/`stages.py`;UI `chat_stream_controller.start_system_turn`/`qt_overlay._is_proactive_busy`;守门 `tests/test_proactive_turn.py`(7 例:video 假域/字段无域字样/无 tools 硬证/仲裁/gate)+`ProactiveFinishReportTest` | 吐槽(P5)与视频陪看主动评论的共同前置 | ✅ **P3 已落地(2026-06-11)** |

## P0-P4 关账清点(2026-06-11,架构审查四轮 + 实施五步收口)

**已清偿**:
- F1/F5(对话发起权 UI 独占 / 事件→turn 缺口)→ **P3**(#21,turn 发起器);
- F2/F8(生产链硬单轮 / 多轮活在死链)→ **P1**(#20,方案 D + sync_chain 冻结定位);
- F4(工具输出压缩特判)→ **P1**(两层通用压缩);
- F7(记忆 commit 异常静默)→ **P0a**(jobs.py logger.exception + 测试钉);
- F13/F14/F15/F16/F17(song 前置劫持 / 写死台词第三说话路径 / song env 直读 / 装饰开关 / UI 层测试缺口)→ **B2**(#17,净删 1703 行;顺带歼灭第四说话路径 SONG_PRELUDE 合成 prompt 流);
- F19(dotenv 灌注时机 bug,DEEPSEEK 警告真根因)→ **P0a**(main 入口先灌 env,铁律 #10 + 源码顺序测试钉);
- guard 盲区(no_getenv 死目录静默空扫 / 不覆盖 agent_tools/ui)→ **P0a**(扫 177 文件 + 扫描量下限 + 临时白名单显式记账);
- #18(chat_completions 无工具轮)/#7(speaker 解析【】)/#19(Moondream 每重启重下 3.85G)→ 各自已修(见对应行);
- 日志清理(triage INFO 降级 / httpx 压制 / 播放状态机 event= 双层降级)→ 完成(见日志清理收口节)。

**仍挂账**:
- ~~P0b 配置统一~~ → **已完成,见下方《P0b 关账清点》**(F6/deepseek 双名/app.yaml/白名单清账全清偿);
- **语音全双工**(她外放时麦克风停车,半双工是 Initial release 原始设计):决议**暂不动**;`VoiceInputGate`(proactive.py)已留接口位,将来路线 b(识别结果硬过滤)→d(ReSpeaker 固件 AEC 验证)→c(软件 AEC);
- anemoi 同源(#2,产品决策挂起)/ declare_route seam(#1)/ stable=2 B 方案(#6)/ 存档槽误截(#7 余项)/ interval 死字段 + `galgame::` 前缀统一(#9)/ unsummarized 全表扫描观察(#11,40h/200ms 接受)/ demo 群逐步退役(#12);
- 特判二(watch 是否挂压缩器):继续挂起,压缩机制已通用化,挂上是一行注册参数;
- OCR 名字级噪声(查珠=杏珠误读):**已决议不修**(名字归一化不值得,总结 LLM 能容忍);
- F9(host 渐胖趋势):P3 实际落 UI 侧,host 未再加厚,降为低优先观察。

## P0b 关账清点(2026-06-12,配置统一收官:守门→收编→typed 化→注入→app.yaml 落地→收墙)

**已清偿**:
- **F6(screen 平行 env 配置)**:SPICA_SCREEN_*×15 收编 typed config——env 读取经 manager(coercion 一份:env 侧 manager / file 侧 ScreenConfig validator),旧 json 归并 app.yaml `screen:` 节;
- **deepseek key 双名**:B2 已使 DEEPSEEK_* 成死名(代码零读取),P0b 加 `load_secrets()` 残留 WARNING(不静默);xiaosan.env 两行已删,env_audit legacy 归零;
- **app.yaml 落地**:名义权威首次成为事实载体——screen/song/plugins 三节由 `scripts/migrate_config_p0b.py` 迁入(写前字段级+生效级双断言,旧文件 `*.migrated` 仅回滚备份);三载体 = app.yaml + xiaosan.env + overlay_config.json,tts/visual.yaml 归角色数据文件(D1);
- **guard 收墙**:SCAN_DIRS 补 hardware(RESPEAKER_*×3 收编),临时白名单清零,永久白名单恰好 config 层三件(manager/secrets/runtime_env——GPT-SoVITS env 写垫片挪入 config 层,D3);
- **resolve-once 注入**:AppHost 构造期 resolve screen(8 调用点)/song(worker 链)各一次注入;`_request_song` 的 search limit 单源化;attachment 分析兜底走载体开关(③-B,零接触冻结链,golden 三件套不改一行);
- **配置守门基建(长期资产)**:Layer A `scripts/dump_resolved_config.py`(真机快照:value/source/env_var/env_set 三遍差分归因 + env_audit + --diff exit 1)+ Layer B `tests/test_resolved_config_equivalence.py`(41 语义钉:优先级/coercion 全分支,P0b 全程一字未改);
- **(顺带,非 P0b 立项)RVC cwd 竞态**:TTS pushd 与 Applio chdir 两 vendored 垫片进程级 cwd 互踩(缓存命中连唱触发 rmvpe.pt [Errno 2]),vendored 10 处相对路径锚定 `__file__` + 删 `_applio_context` 的 chdir,正反两向竞态同根拔除(确定性复现+复验)。

**仍挂账**:
- song 节 typed 化(D-3a:deep-merge+voices 开放字典语义,pydantic 化另立项);
- 方案 d:GPT-SoVITS pushd 侧绝对化(挂账观察——RVC 不碰 cwd 后,残余仅 TTS-vs-TTS warmup 启动小窗);
- 管理面 `update_config` save 全量落盘掉注释(D-3d 登记不动,设置中心启用前不触发);
- D6 旧链分支下版本删除(「warn 一个版本」承诺,删时同删 `*.migrated` 三件);
- legacy `tool.py` self-load(冻结链组件,与 sync_chain 同退役);
- 既有挂账原样保持:语音全双工(VoiceInputGate 接口位在)/anemoi/特判二/OCR 名字噪声(决议不修)/interval 死字段/declare_route/stable=2/存档槽/全表扫描/demo 群。

## 外部代码审查处置(2026-06-12,review 9 条)

裁决:#1 watch 隐私违约 + #6 SQLite 并发**现做**(落地后补记);#2/#9/H1 详细挂账见下(照着能改级;#3 即 H1 同条);#4 song 取消/缓存竞态(窗口窄,触发=取消后立刻重点同一首,下次动 pipeline 时改临时目录+原子 rename)与 #8 Moondream preload executor reset 不 shutdown(一行 `executor.shutdown(wait=False)`,下次动 screen 区域顺手)简记挂账;#5 vendored 不可复现=既定形态不动;#7 GPT-SoVITS pushd=已登记(上方「方案 d」)不另立。

### 挂账#2:崩溃恢复漏最后一条 pending 行
- **事实**:`spica/galgame/session.py:406-421 _write_pending_current` 把新稳定行立刻持久化为 `PENDING_CURRENT`(注释即 "crash safety §10.5");提升为 COMMITTED 只有两处——下一条稳定行(`session.py:401`)和正常 `end()`(`session.py:587`,§16.4 step 2)。崩溃恢复 `spica/galgame/summarizer.py:164-205 recover_dangling_sessions` 只读 `unsummarized_committed_story_lines`(`:174`)→ 崩溃点落在「最后一条稳定行已 pending、尚无下一条/未 stop」时,该行留在 DB、永不入总结。
- **影响**:每次崩溃最多丢 1 行(pending 是单槽);该行状态永久停在 pending_current。
- **改法**:① `spica/ports/game_memory.py` + `spica/adapters/game_memory/sqlite.py` 加查询 `pending_current_story_lines(game_id, playthrough_id) -> list[StoryLine]`(SELECT status='pending_current';提升机制 `update_story_line_status` 已有,`ports/game_memory.py:62`);② `recover_dangling_sessions` 在取 committed 行**之前**,对每个 dangling session 先把这些行逐条 `update_story_line_status(line_id, COMMITTED)`——镜像 `end()` 的 §16.4 step 2 语义,恢复路径与正常收尾同构;③ 然后照原逻辑取 unsummarized committed 总结。
- **验证**:测试钉「有 pending 行的 dangling session 恢复后,总结输入包含该行且 DB 状态已提升」+「无 pending 行时行为不变」;现有 summarizer/恢复测试全绿。

### 挂账#9:env 名册与 proxy strip 集合漂移
- **事实**:`spica/config/runtime_env.py:22 _PROXY_ENV_KEYS` 实际 strip 6 项(`all_proxy/ALL_PROXY/http_proxy/https_proxy/HTTP_PROXY/HTTPS_PROXY`),`spica/config/env_roster.py:91 STRIPPED_ENV_VARS` 只登记 3 项大写。Layer B 元钉正则 `"([A-Z][A-Z0-9_]{2,})"` 只抽大写名,小写漏网——这就是它没被钉住的原因。
- **性质**:纯审计/名册完整性,无功能 bug(strip 行为本身有行为测试覆盖)。
- **改法**:① `env_roster.STRIPPED_ENV_VARS` 补全为 6 项(名册如实记录行为);② `runtime_env.py` 删本地 `_PROXY_ENV_KEYS`,改 `from spica.config.env_roster import STRIPPED_ENV_VARS`(单一居所、结构防漂移——P0b 本来纪律);③ 可加结构钉:断言 runtime_env 引用的就是 roster 常量(import 同一性)。
- **验证**:Layer A diff 零变化(STRIPPED 是写侧名册,不参与 resolution,env_audit 不受影响);Layer B 41 钉不动(元钉方向是「源码大写名 ⊆ roster」,roster 增项不破);全绿。

### 挂账 H1:LLM 端点协议判定写死 deepseek 名字(review #3;换 chat-only 端点前必修)
- **事实**:`spica/adapters/llm/openai_compatible.py:262-264 _prefers_chat_completions` = `"deepseek" in base_url and _has_chat_completions(client)`,决定全链 Chat-Completions-first vs Responses-first。生产代码里该判定**仅此一处**(prompt/默认值无模型名写死)。
- **触发条件**:换到 **chat-only OpenAI 兼容端点**(vLLM/Ollama/LM Studio 等,URL 不含 deepseek)时**必修**,否则:工具 probe 直接报错(`tool_round.py:127 create_responses` 无回落)+ galgame 总结永败(`complete_text`,`openai_compatible.py:121`,SummaryError 无限折叠重试)+ 聊天每轮 404 暗税(流式 `:175` 有 404→chat 回落,能用)——症状=**表面能聊天,工具与总结暗坏**。deepseek 现状与 OpenAI 官方(有 Responses API)不受影响。反向坑:URL 任意位置含 "deepseek"(如代理路径)会误判成 chat 模式。
- **改法**:① `LLMConfig`(`spica/config/schema.py:21`)加字段 `api: str = "auto"`("auto" | "responses" | "chat_completions";默认 auto=现行名字判定,保 Layer A/B 零 diff;yaml-only 不开 env 名——若要 env 则按纪律进 roster+manager);② adapter 收下该值:`spica/host/builtins.py:33` 工厂签名加参、`spica/host/app_host.py:200 resolve_llm` 传 `config.llm.api`,实例 `prefers_chat_completions` 显式值优先、auto 走名字判定;③ 流式内部三个模块级函数(`:136 _iter_response_text` / `:240 _fallback_response_text` / `:280 _iter_chat_completion_text` 链)直接收 client,需加可选 prefer 参数由 adapter 实例传入——外部调用面 `tool_round.py:78` / `stages.py:592` / `complete_text`(`:119`)已走实例方法,零改动,单点全链生效。
- **换时附加**:补非 deepseek 域名 chat-only client 测试 fixture(现有测试全钉 deepseek URL 形态);真机三连验证(对话流式为增量出字、工具触发一次、总结无 SummaryError)。

## 真机事实存档(无代码锚点,防失忆)

### anemoi 同源问题(#2,产品决策挂起)
anemoi 是 Spica 的"原型游戏"——游戏内角色与 Spica 人设同源。陪玩 anemoi 时,Spica 的角色长期记忆(她是谁、和麦的关系)与游戏剧情(同源角色的剧情走向)会互相打架:剧情里的"她"和作为陪玩者的"她"指代冲突。**普通游戏无此问题**(剧情角色与 Spica 无关)。待决策方向(均未定):玩 anemoi 时遮蔽部分角色记忆 / prompt 显式声明"游戏里的角色不是你" / 接受混淆作为特性。阶段 2 的记忆分流(剧情入 game memory、角色记忆保持 default scope)使两类记忆物理分开,但 prompt 内仍会同时出现。

### GPU 立场与实测数据(#5)
- 环境:RTX 4090,companion 进程拉起整套栈(GPT-SoVITS TTS warmup、Moondream、RapidOCR CUDA)。
- 实测:OCR cycle 110–169ms @ interval 300ms(GPU 正常、未回落 CPU、未被争用)——曾怀疑 GPU 争用导致漏句,已被实测排除(真因是 interval 未透传,已修)。
- v1 立场:**对话期间 OCR 照跑,不暂停不降频**。理由:对话时玩家常继续点字,暂停采样直接违背"剧情不漏";对话 LLM 在远端不占本地 GPU;本地竞争仅 TTS 合成突发 vs OCR 推理,均百毫秒级可交错。
- 观测护栏:`ocr_cycle_ms exceeded interval` 告警 + `last_cycle_ms` 属性。
- **已核(2026-06-11):GPU 未回落,536-670ms = 对话期叠加争用,v1 立场内。** 权威口径 = 引擎三 session 的 `session.get_providers()`(det/cls/rec 全部 `['CUDAExecutionProvider', 'CPUExecutionProvider']`,CUDA 首位;与 rapidocr 自家 `_verify_providers` 同口径)。诊断器 `scripts/diag_ocr_providers.py`(下次再疑回落直接跑它;旧 `galgame_companion_demo._collect_providers` 打印 `[]` 的原因:跳过所有 `callable(child)`,而 TextDetector/OrtInferSession 均定义 `__call__`,递归第一层即被剪——勿再用)。实测梯度:稳态对话条 OCR 113-123ms(GPU 正常带)/ 整帧 ~164ms(det 有 limit_side_len 缩放,整帧不爆炸)/ GPU 饱和争用 ~277ms / watch 整帧 OCR 抢 `_INFER_LOCK` ~316ms / **双负载叠加 731-832ms(夹住真机 536-670)** / CPU 参照 ~1556ms。机制:她"看屏幕"时 `analyzer.py` 先整帧 `ocr_image`(与 loop 共用 `_INFER_LOCK`,等锁计入 `ocr_cycle_ms`)+ Moondream/TTS 占 GPU。回落签名 = **稳态也 ≥1.5s**;且 providers 在 session 创建时固定,运行中 CUDA 错误会抛错不会静默永久切 CPU——"对话期高、稳态正常"本身即排除回落。遗留小项:warning 文案 "CPU OCR may not hold the interval" 在 GPU 确认在用后属误导,可改中性表述(未改)。
- 升级路径:TTS 卡顿 → 先调大 `config.galgame.ocr_interval_seconds`;若需"说话时让路",落点是 controller 新方法(host 在 TTS 阶段回调挂接),**不**进 ocr_loop / session。

### 其他真机噪声(#7)
- LimeLight 的说话人以 `【名字】` 形式渲染在对白区,当前 `parse_from_text` 正则不识别。
- OCR 偶尔截到存档槽文字(区域比例覆盖到 UI 元素时);句末标点偶有识别错(`。`/`.` 混淆类)。

### 日志清理收口(2026-06-11)
- triage INFO 全部降 DEBUG(降级不删,调 level 找回):`tool_round.py` "stream turn tools offered" / `app_host.py` "watch context" 三行 / `stages.py` "turn tools offered" + "llm path"(同步链孪生);httpx 的 "HTTP Request: … 200 OK" 在 `qt_overlay.py main()` 压到 WARNING。留 INFO:capturing / tts done / CUDA 预加载 / dangling recovered。
- **残谜(未解,眼睛别丢)**:`app_host._companion_watch_context` 的 "watch context" 行按代码每 turn 该被求值 ≥2 次(registry `tool_schemas` 状态过滤 + `schemas_for_user_text` 各一次),真机却只在启动打过一次。已降 DEBUG,排查时 `logging.getLogger("spica.host.app_host").setLevel(logging.DEBUG)` 整行找回;下次陪玩复验时一并看。
- **待办(待真机体感后决定)**:陪玩状态变化(started/stopped/window_lost)终端无日志,仅走 RuntimeEvent → UI 气泡;如需终端可见需新增 INFO,等真机感受"终端没有状态行碍不碍事"后再定加不加、加哪几条。
- **复盘(2026-06-11):"event= 行已降 DEBUG"只修了一半。** `_log_ui_playback_event` 默认确实降了,但包装层 `_log_play_item_event` 自带 `level=logging.INFO` 默认,7 个不传 level 的调用点(play_item_enter / typewriter_start_begin / play_chunk_audio_begin / set_character_image_start 等)每句播放仍以 INFO 刷十几行——真机复验抓出。今日包装层默认同降 DEBUG;两处显式 `level=logging.WARNING`(slow 告警)保留。**教训:核日志级别要核到"最外层包装的默认实参"**,字面 grep `logger.info` 连 `logger.log(level)` 都看不见,更看不见包装默认值的覆盖。

### 持久化 WindowMatchRule 不可直接喂 check_safety(阶段 3 决定记录)
`GameBinder._store_binding` 持久化的规则**从不写 `title_keywords`**(只写 last_full_title /
process_name / app_id / confirmed_once,binding.py)。空关键词规则会让
`title_matches_rule` 恒 False → OCR 永久暂停。因此 `controller.start()` 的焦点匹配规则
**维持自建默认** `WindowMatchRule(title_keywords=[game_id])`,不读 profile.window_match。
若未来想用持久化规则,必须先让某处真正写入 keywords(校准/绑定 UI 收集),再改这条。

## 阶段 3 UI 接入落点速查(实施于阶段 2 之后)

- 入口:`WindowControls` 🎮(`companion_requested`;**checked 仅由 `set_companion_active` 事件回写**,点击 handler 先撤销 Qt 自动翻转再转发)。
- 协调器:`ui/controllers/galgame_controller.py` ——流程编排全走注入回调(set_status / set_companion_active / toast / pick_window / select_region / ask_active_action),离屏可测;**所有失败路径收敛到 `_reset_to_real_state`**(按钮/状态条回真实状态)。
- 线程:`ui/workers/companion_action_worker.py`(QThread 包 callable);bind / 校准 OCR 测试 / start / **stop(终结总结 LLM,数秒~数十秒)** / recover 全部后台;单 action 闸。
- 选窗:binder selection-only 模式(session=None)+ `resolve_selection(window_id, game_id_override)`;首次流 `begin_bind("")` 空规则 → 全员入围 → 必弹 picker(§17.3)。
- 校准:ScreenshotSelectionOverlay 框选 → `selection_to_physical_rect` → calibrator.set_dialog_region + run_ocr_test → 预览确认(关闭预览窗 = 取消,eventFilter)→ confirm → start(读 profile)。
- 关窗:closeEvent 后台 stop 等 3000ms,超时放弃 → dangling → 下次启动 `_start_dangling_recovery` 静默补总结(§12 设计内崩溃等价路径)。

## 阶段 4 收口记档(路 B 封顶,异常路径全检)

**已确认干净收口(逐条核验,不再重查)**:① 选窗后窗口消失 → start 成功后首 cycle 进 WINDOW_LOST(诚实暂停,stop 可用);② 陪玩中窗口彻底关闭 → 同上(§16.3 默认挂起是设计,轮询成本有界);③ OCR cycle 异常被 `_run_loop` 吸收循环不死;④ 后台总结失败折叠(in_flight 复位、行留 buffer、回 PLAYING,有测试);⑤ stop 进行中再点🎮被单 action 闸忽略、关窗走 `shutdown wait(3000)` 超时放弃 → dangling → recover(设计内崩溃等价路径;放弃后 QThread 随进程销毁的 Qt 告警属外观级);⑥ `_cleanup_failed_start` 覆盖完整(早抛在 session 前零清理、晚抛 finalize 无 dangling、履历回调不在 cleanup 路径);⑦ recover 单 session 失败标 interrupted 不再重检,`update_play_session` 自身抛错中断当轮 → 残余下次启动重试(最终一致)。

**脏 dangling 排查结论:当前零路径**——dangling 查询只认 `active/paused`,唯一能产生 `crashed` 的 `mark_error` **零生产调用**(choice_checking 同);**备忘:mark_error/选项识别接入时必须同步扩 dangling 查询**,否则 crashed 残留永不被 recover。

**资源生命周期**:OCR daemon 每 start 一条 stop join(有界);sqlite `with self._connect()` 是**事务作用域非关闭**,实际靠 CPython 引用计数析构(本项目环境无泄漏;PyPy 才是问题);`ThreadJobRunner.submit` 不剪死线程(100h ≈ 2000 个死 Thread 对象,无害,已决定不修);CompanionActionWorker 列表 finished 即剪。

**阶段 4 实修两项**:M1 校准流异常复位(双保险:`galgame_error` 事件腿在 busy+calibrating 时复位 + `_calibrate` worker 对 `set_dialog_region` 返回 False 抛错走 failed 复位——一个治"错误事件已发出",一个治"只返回 False 没发事件");M2 切游戏(🎮 活跃菜单第四项"换个游戏陪玩";A 在 B 的选窗/校准期间保持活跃,中途取消 A 无损;最终 worker 同一 callable 内 `stop(A)`(同步完成总结+履历)→ `start(B)`;controller/session 零新方法)。

**阶段 4 顺手修的潜伏缺陷(线程)**:阶段 3 的 `_run` 曾把 `on_ok`/`_done` 裸闭包连到 worker 信号——闭包无 QObject 线程亲和,AutoConnection 退化 direct,**回调在 worker 线程执行**(真机"能用"属侥幸,离屏测试以段错误暴露)。已改为全部经 controller 绑定方法分发(`_dispatch_worker_ok/fail/done`,GUI 线程 queued),per-worker 回调挂属性随发。

## 游玩履历桥落点速查(#15,实施于阶段 3 之后)

- 卡片生成:`spica/galgame/history.py` —— `build_play_history_card`(纯模板 **v2**:游戏名中英双写《名》（game_id）→ 前置"主人公（男主角）是X"独立句(检索词覆盖 主人公/男主/主角;主角=跨最近 3 条 summary.characters 频次最高,平手取最新条目先列者;判不出省略)→ 进展(§13.5 三档)→ 关系 top2 → 最近剧情 → 日期;**贪心装配硬保 ≤220**:段按优先级追加、放不下整段丢弃)/ `compose_play_history`(读库装配,从没玩出东西 → None)。生成单点,stop 与 recover 共用。**v1 真机教训**:卡内无 主人公/男主/名字/拉丁 token → CJK bigram 零命中被检索过滤;关系段 top1 恰取配角对(雄真-杰)→ 主角全卡未被标注。relations 边频次法判主角被该数据证伪,故用 summary.characters 频次法。检索词守门测试用真实 store 关键词算法(`test_retrieval_keyword_guard`)。
- controller:构造注入 `record_history(game_id, card)` 回调;`stop()` 在 `session.end()` **正常返回后** `_record_play_history_safe`(整段 try/except,best-effort 不挡 stop)。
- host:`AppHost._record_play_history` 闭包持写权限 —— `memory_store.upsert_memory(conversation_id=scoped_conversation_id(char,"default"), scope="relationship", memory_key=f"galgame_history:{game_id}", importance=0.85, memory_type="experience", source="galgame_companion")`;`recover_dangling_companion_sessions` 对恢复涉及的 game_id 去重补写。
- 220 字硬预算依据:prompt 渲染记忆内容经 `_compact_text(·, 220)`(prompt_builder)。
- 检索依赖:store 关键词打分(非 pinned 零命中即过滤)→ 卡文本自带"游戏 / 一起玩 / 游戏名"检索词。

## 阶段 2 缝合层落点速查(实施于本清单审定后)

- `GameTurnBinding` + `GALGAME_CONVERSATION_PREFIX`:`spica/runtime/context.py`(binding 两字段,**不带** memory_conversation_id——该值只在 `_request` 时刻可知,由其从调用方 conversation_id 派生,语义同 §27①)。
- provider 钩子:`ChatEngine.set_game_binding_provider` + `_request` 内自动填(防双包守卫:调用方已是 `galgame::` 会话则不改写);未设/返回 None 时构造逐字节不变。
- 发布快照:`companion_controller.start()` 末发布 / `stop()` 首行清空;`current_game_context()` **无锁读**(stop 持锁跨最终总结 LLM,锁读会卡对话线程数秒)。
- host 单例:`AppHost.companion_controller()` accessor(缓存)+ `_companion_game_binding` 惰性 provider(`initialize()` 末接线——该行无单测,靠 diff + 真机 `--ask` 覆盖)。
- 每个陪玩 turn:`conversation_id = galgame::<game>::playthrough::default`(recent 隔离 + active gate)、`memory_conversation_id = 调用方原会话`(长期记忆读写都连续)。

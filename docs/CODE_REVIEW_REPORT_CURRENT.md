# 当前代码审查报告（第 1 趟只读审计）

> 只读审计，不修代码。风险分级 P0/P1/P2/P3。证据以 `file:line` 给出。
> 生成日期：2026-06-27。审计方式：4 守卫测试 + 6 类反向 grep + 7 域 subagent 深读 + 主上下文对争议点直接复读自证 + 全量测试。
> **本轮架构结论已经全量测试验证：`python -m pytest tests -q` → 821 passed, 108 subtests passed in 15.27s（无 collection error、无 skip、未触发熔断）。** 可信度高，不止于静态阅读。

---

## 1. 总体判断

**当前代码架构健康，无 P0，核心铁律全部成立且大多有真实自动化守卫。** 全量测试 821 passed 佐证。

- 10 条铁律：8 条**是**（多数有 AST 守卫），#2 与 #10 经复读确认成立（#10 还有专门 AST 守卫 `test_env_centralization.py`）。
- 唯一对话路径 `run_turn` 确为唯一 emit 点（turn.py:43-44）；系统 turn 工具供给双处硬关断；cancellation 三检查点防 ghost；galgame 上下文经纯请求逻辑 gated stage 注入（绝不第二条 LLM）；act 工具（sing_song）host 闭包持权纯转发；记忆角色隔离 + galgame 独立库成立；**Phase 0 关键耦合点（galgame turn 仍能读到角色长期记忆、且写回落 caller scope）经代码确认正确**。
- 主要问题是**架构债（P1/P2）与文档漂移（P3）**，非破链：Phase 7 多角色 recent memory 未命名空间化（已挂 TODO）、galgame 几处并发残余 race（有 mitigation）、judge LLM 无 per-call 超时、env 名册 APP 级为「手工镜像 + 测试钉」而非结构复用、README/CLAUDE 若干描述与代码漂移。

---

## 2. 架构上已经做对的地方

- **铁律有 AST 自动化守卫**：`test_layering.py`（Qt 隔离 + agent 包删除 + RuntimeEvent 仅 facade 产）、`test_no_getenv.py`（env 集中，floor>100 防空扫）、`test_env_centralization.py`（#10：AST 钉 `qt_overlay.main()` 首句必须 `load_secrets()`，:136-157）、`test_resolved_config_equivalence.py`（配置解析语义 + env 名册 meta-pin，:257-282）。
- **唯一 emit 路径牢固**：`run_turn`（turn.py:43-44）唯一把 legacy dict 升 RuntimeEvent；orchestrator/stages/tool_round 只 `output_queue.put`；ChatEngine 三入口全汇入 run_turn。
- **类型化 turn 边界**：TurnRequest（context.py:82-114 frozen，输入）/ TurnContext（:196-229，懒填）/ TurnDeps（deps.py:43-89，DI）；`deps.tools.run()` 单点执行。
- **系统 turn 防自激**：`interaction_mode=="system"` 在 tool_round.py:49-57 与 stages.py:592-600 两处清空工具 schema（策略同步）。
- **cancellation 防 ghost**：tool_round.py:284-291（每次 tool.run 前）/ orchestrator.py:336-363（跳过/中断 LLM 流）/ :392-396（跳过 memory commit）；谓词 context.py:127-137 防御式。
- **galgame 状态唯一 owner + 串行 OCR**：`GalgameCompanionSession` RLock（session.py:168）私有状态 + 公共事件 API；OCR loop「完成后等待」串行（ocr_loop.py:132-147）；RapidOCR `_INFER_LOCK`（rapidocr.py:18,33）序列化所有推理。
- **galgame 上下文 gate 是纯请求逻辑**：`retrieve_game_context_node`（stages.py:528）在 build_prompt 后、call_llm 前；`_game_context_mode`（:316）只看 interaction_mode/conversation_id 前缀/request mode；mode=="none" 时 byte-level no-op（不开 observer span，:537-540）；**绝不跑 LLM**（:535）。
- **summary 读不可变 snapshot**：锁内切快照（session.py:463 `list(self._unsummarized_lines)`），传 job，LLM 锁外跑只读 StoryLine，只推进匹配批次。
- **act 工具纪律落地**：sing_song 纯转发 → SongRequestEvent，真动作在 host 闭包 `_request_song`（app_host.py:673-694），网易云白名单、无 shell/exec、失败回 ToolError 信封；无 LLM 绕 registry 路径（INVARIANT N5，tools.py:15-17）。
- **截图本地不上传**：mss 本地截图 + LocalMoondream（INVARIANT N0）+ RapidOCR 本地，零 HTTP 外发。
- **记忆角色隔离结构性成立**：长期记忆 namespace `{character_id}::{conversation_id}`（adapters/memory/sqlite.py:30），`::` 使跨角色串记忆结构上不可能；galgame 独立库 galgame.sqlite3 + 独立端口/schema。
- **UI 严守边界**：不 new LLM/TTS/Memory；全经 AppHost；不碰 galgame domain 内部，只经 host factory + CompanionEventBridge 事件；后端线程不直接动 widget，全 Qt queued signal（近期 46a926b/527c6bc 跨线程修复已并入）。
- **LLM 调用单点**：真实客户端调用全在 adapters/llm/openai_compatible.py，`OpenAI(` 仅 agent_assembly.py:44。

---

## 3. 架构债务

- **[P1] Phase 7 多角色 recent memory 未命名空间化**：`chat_engine.py:235-240` 明写 TODO——recent_memory 仍用裸 conversation_id，未按 character_id 命名空间；recent clear 用裸 id（:247），long-term clear 用 scoped（:250）。当前单角色无害，Phase 7 接入运行时切角色前**必须**修，否则 A 的近期上下文漏进 B。
- **[P1] galgame OCR 隐私门残余 race**：watch/OCR capture 前查 `state in 安全态`（watch_game_screen.py:158-165 / ocr_loop.py:152-173），但 state 无锁读 singleton，check→capture 有 ≤一个 OCR 周期的窗口；理论上可能截到刚被遮挡/切走的窗口。有 mitigation（capture 紧邻 safety check），符合 CLAUDE §4「破坏立即暂停」的意图，但非零 race。**非 P0**（有活跃安全门）。
- **[P2] judge LLM 无 per-call 超时**：reaction_judge.py（约:141）judge 调用只继承 adapter 全局超时，无单次超时；judge 卡住会阻塞 reaction worker（queue.get 超时只管 idle）。对应记忆笔记 2026-06-26 perf-audit 的「judge timeout」仍在。
- **[P2] GPU 争用无显式护栏**：RapidOCR 有 `_INFER_LOCK`，但 OCR(GPU) / TTS(GPU) / judge LLM 之间无显式 GPU 争用调度（galgame subagent 30% 置信，待二次核验）。对应 perf-audit 的「GPU-contention freeze」可能仍在。
- **[P2] 同步链 vs 流式链 tool-overflow 行为不对称**：sync_chain 超限保留 `LLM_TOOL_LOOP_EXCEEDED` 错误（stages.py:688，golden 钉），流式链优雅强制收尾（tool_round.py:256-265）。golden 锁住，任一回归会暴露不对称。
- **[P2] song 节 untyped**：`AppConfig.song: dict[str, Any]`（schema.py:307-312 有意 untyped override dict 叠在 song/config.py DEFAULT_CONFIG），CLAUDE D-3a 挂账。
- **[P2] 长期记忆后台 JobRunner 失败静默**：memory_commit.py:74-82 catch+WARNING 但 turn 正常返回；池过载/崩溃可能静默丢记忆抽取，无重试。
- **[P3] env 名册 APP 级为「手工镜像 + 测试钉」非结构复用**：见 §11 与 §14 末注。
- **[P3/设计] galgame 抽取记忆落角色 default scope**：§27① 有意；可能把游戏特定事实写进角色全域知识（extractor 规则把关）。非 bug，记录取舍。

---

## 4. 高风险文件

| 文件 | 风险点 | 状态 |
|---|---|---|
| spica/adapters/game_launcher/linux_desktop.py | subprocess.Popen 启动游戏 | ✅ 仅扫 XDG `.desktop`，不接 LLM 任意路径；OS 级信任 |
| spica/adapters/window_locator/linux_x11.py | subprocess.run 查窗口 | ✅ 只读系统查询 |
| spica/adapters/tools/sing_song.py | 唯一 act 工具 | ✅ host 闭包持权 + 白名单 + ToolError 信封 |
| spica/adapters/llm/openai_compatible.py | 唯一真实 LLM 调用面 | ✅ 端口化 |
| spica/host/app_host.py（836 行） | 体量大 | ✅ initialize()~84 行装配；余量是必须留 host 的 reaction/galgame 闭包（铁律#8/#9） |
| spica/runtime/stages.py（1088 行） | 体量大 | ✅ ~12 stage+galgame gate，每个紧凑纯函数 |
| spica/galgame/session.py | 并发状态 owner | ✅ RLock + 私有状态 + 公共事件 API |
| spica/galgame/ocr_loop.py + reaction.py + reaction_judge.py | 串行 OCR / 主动吐槽 / judge | ✅ 串行+锁；reaction 经 run_turn；🔶 judge 无 per-call 超时（P2） |

---

## 5. 可能已经变胖的模块

- `app_host.py` 836 行：**非 bloat**。initialize() ~84 行纯接线；体量来自 reaction 评分/判定闭包（:310-444）、galgame session/observation/history 闭包（:564-784）——铁律 #8/#9 要求写/judge 权限留 host 闭包，规则使然。
- `stages.py` 1088 行：~12 typed stage + galgame gate，可接受。
- `chat_engine.py` 282 / `orchestrator.py` 518 / `tool_round.py` 334：合理。
- `ui/qt_overlay.py`：体量较大（OverlayWindow 装配 + 多 controller 接线 + 系统 turn marshaling），但职责是 UI 组装/线程桥，未见越界。可作第 2 趟重点量化。

---

## 6. 重复逻辑 / 历史兼容链

- `sync_chain.py`（61 行）：✅ 冻结，**生产零调用方**——`run_voice_pipeline` 仅 ~7 测试文件引用。生产同步入口是 `run_voice`=run_turn+fold。
- `agent_tools/function_tools/song/intent.py` + `intent_rules.py`：✅ **非 orphan**。LLM 侧意图分类器确已删，但 `parse_song_control_intent` 仍被 `ui/controllers/song_controller.py:167-190` 用于**播放控制词快路径**（gated：is_busy() + confidence≥0.9 + {PAUSE,RESUME,CANCEL,RESTART}）。迁去 UI 控制层，非死代码。
- ChatEngine RuntimeEvent → legacy dict 兼容层（chat_engine.py:208）：UI 仍消费 legacy dict，过渡兼容，合理。

---

## 7. 命名和目录问题

- **[P3] `spica/memory/` 几乎空**（只 `__init__.py`，自述「port glue namespace」），真实实现体在根级 `memory/`。命名易混但有意（spica/memory=port 层，memory/=实现层）。
- **[P3] 退役载体未删**：根级 `config/screen_vision_config.json.migrated`、`data/config/plugins.yaml.migrated`、`song_config.json.migrated` 并存（CLAUDE 称下版本删旧链读取）。

---

## 8. 测试覆盖缺口

全量 821 passed。覆盖图（tests subagent）：runtime/turn(20，优)、config(10，优)、screen(13，良)、galgame(11，良)、reaction(5)、memory(6，偏薄)、adapters(7，偏薄)、ui(3，薄)、stt(2)、song(2)、**tools(1，最薄)**。

缺口：
- **[P2] tools 子系统测试最薄**：仅 `test_tool_chain_rounds.py`；无 live registry→ToolSet→orchestrator 工具解析 e2e（仅有 `test_no_static_tool_schemas` 负向守卫）。
- **[P3] Phase 7 多角色 recent 隔离无正向测试**：`test_recent_memory.py` 验 per-char 桶，但无并发多轮跨角色隔离场景。
- **[P3] run_turn 唯一 emit 仅负向守卫**：test_layering 禁 transform 层 import events，但无「只有 run_turn 产 event」正向证明。
- **[P3] env 名册 APP 级字段映射无直接对账测试**：名级有 meta-pin（test_resolved_config_equivalence:257-282），字段路径映射靠等价测试间接覆盖（见 §11）。

---

## 9. 性能风险

- **[P2] judge LLM 无 per-call 超时**（§3）——perf-audit「judge timeout」仍在。
- **[P2] GPU 争用无显式护栏**（§3）——perf-audit「GPU-contention freeze」可能仍在。
- **[P2] game_memory `source_line_ids` 去重每次 retrieve 全表扫**（adapters/game_memory/sqlite.py 约:335-348）：hand-fed 无碍，OCR 累积上万行将 O(n²)。
- 记忆笔记 perf-audit「empty-reaction」：已部分处理（budget refund reaction.py:668-679），但 NO_COMMENT 处理散落 reaction 引擎 + orchestrator，可读性待整。
- 记忆笔记 perf-audit「cooldown-before-judge」：已成立（reaction.py:587-588 cooldown 在 score gate 前）。

---

## 10. 并发风险

- ✅ cancellation 三检查点防 ghost（见 §2）。
- ✅ 流式 play unit 按 index 严格有序：Sequencer（sequencer.py:24-65 单消费者缓冲乱序）+ orchestrator.py:159-165 按序 emit。
- ✅ galgame OCR loop 串行不重叠 + RapidOCR 锁；summary 读锁内不可变 snapshot，LLM 锁外跑（OCR 可并发续写 buffer，snapshot 隔离）。
- ✅ 崩溃恢复：`recover_dangling_sessions`（summarizer.py:164←app_host.py:763 启动调用）；final summary 失败留 ENDING/ended_at=NULL → 下次启动重检为 dangling，幂等重试。
- **[P1] OCR 隐私门残余 race**（§3）。
- **[P2] JobRunner 失败静默**（§3）；**[P2] CompanionBeat dedupe DB race**（reaction.py:600,614-629 worker 线程读 DB 去重，慢 DB 下后 beat 可能未见前 beat；同进程 SQLite 不太可能）。

---

## 11. 配置风险

- ✅ `test_resolved_config_equivalence.py` 49 passed（含 env>file>default 各 coercion 分支 + roster meta-pin）；`dump_resolved_config.py` Layer A 真机快照守门。
- **[P3] env 名册 APP 级非结构复用**：`manager._env_overrides()`（manager.py:128-196）**硬编码** app 级 env 名（MODEL/OPENAI_BASE_URL/SPICA_*/JUDGE_* 等），未 import `APP_ENV_MAP`；只有 `SCREEN_ENV_MAP`/`RESPEAKER_ENV_MAP` 真正结构 import（manager.py:23）。`APP_ENV_MAP` 是手工镜像（roster:24 注「mirrors ConfigManager._env_overrides」）。**漂移防护靠测试**：`test_roster_covers_every_env_name_in_the_config_layer`（test_resolved_config_equivalence.py:257-282）正则扫 manager/secrets/runtime_env 源，断言每个大写 env 名都在 roster——名级不可静默漂移。→ CLAUDE「结构上无法漂移」对 SCREEN/RESPEAKER 准确，对 APP 级应表述为「测试钉死名 + 等价测试覆盖值」。**低危**，记 P3。
- **[发现] app.yaml 实为 10 节非 8**：llm/memory/character/stream/galgame/**stt**/screen/song/plugins/max_tool_rounds（schema.py:300-314）。CLAUDE/README 写「8 节」漏 stt。→ 文档漂移。
- **[发现] xiaosan.env 非「只装密钥」**：env_roster 定义 APP/SCREEN(15)/RESPEAKER/CACHE/SECRETS 多组 override 名；env=override 层可承载远多于密钥。CLAUDE/README「只装密钥」是约定/推荐内容，非代码强制。→ 措辞需澄清。
- **[P2] song 节 untyped**（§3）。

---

## 12. 记忆污染风险

- ✅ **Phase 0 关键耦合点已澄清（从代码）**：galgame turn 设 `conversation_id="galgame::..."` 但保留 `memory_conversation_id`=caller 原 scope；`effective_memory_conversation_id`（context.py:114-120）= memory_conversation_id or conversation_id；retrieve（stages.py:25）与 commit（memory_commit.py:67）都用 effective → galgame turn **仍能读到** Spica 关于麦的「default」长期记忆，抽取的记忆落回 caller 原 scope（§27① 写读对称）。**隔离正确，耦合点不破。**
- ✅ OCR 剧情文本结构性隔离：session.on_ocr_result → game_memory.add_story_line，绝不进 ChatEngine recent。
- **[P2] upsert importance 跨talk**：同一事实被 galgame turn 与普通 chat 各抽一次时，store.py upsert 按 memory_key 去重，importance 谁高谁留——galgame 抽取可能永久顶高某事实重要度（待二次核验 store.py upsert）。
- **[P3/设计] galgame 抽取落角色 default scope**（§3）。

---

## 13. 安全 / 副作用风险

- ✅ 业务码无 `os.system`/`shell=True`/`eval(`/`exec(`（grep 命中均假阳性：torch model.eval()、Qt app.exec()）。
- ✅ act 工具 sing_song 无任意执行面；game_launcher 仅扫 XDG `.desktop`（OS 级信任），不接 LLM 任意路径/命令。
- ✅ 屏幕识别全本地不上传（INVARIANT N0）。
- 🔶 **[P1] watch_game_screen 隐私门 race**（§3/§10）：低概率，state 时效≤~100ms，有 mitigation。

---

## 14. CLAUDE.md 铁律 vs 当前代码实际

| # | CLAUDE.md 规则 | 代码守卫 | 证据 | 是否成立 | 风险 |
|---|---|---|---|---|---|
| 1 | spica/ 不 import Qt | test_layering.py (AST) | spica/ 全 docstring；真 import 仅 tests/；6 passed | **是** | 低 |
| 2 | 跨 Host→UI 只走 RuntimeEvent dataclass | test_layering N1-final（部分） | companion/song events 全 RuntimeEvent 子类，经独立 sink 发射；guard 钉 stages+conversation 不产 event | **是**（galgame/song 经 CompanionEventBridge，合 dataclass 规则；守卫未覆盖这些发射点但有意设计） | 低 |
| 3 | 唯一对话路径 run_turn（含系统 turn） | test_turn_contract | run_turn 唯一 emit（turn.py:43）；system turn 工具双处硬关断；galgame/reaction/proactive 均经 run_turn | **是** | 低 |
| 4 | 业务码不 os.getenv | test_no_getenv.py (AST) | allowlist=3，临时清零，floor>100；passed | **是** | 低 |
| 5 | Host 必须薄 | 无直接守卫 | initialize()~84 行装配；余量是必须留 host 的闭包 | **是**（薄 by design） | 低 |
| 6 | 测试命令 python -m pytest tests -q | pytest.ini | testpaths=tests,hardware/respeaker | **是** | 低 |
| 7 | 新能力走 ports/adapters/registry | 目录 + INVARIANT N5 | 13 端口 + 对应 adapters；tools 全 registry 驱动 | **是** | 低 |
| 8 | galgame 记忆独立 scope | 独立库/端口/schema | galgame.sqlite3 独立，OCR 不进 recent | **是** | 低 |
| 9 | act 工具经专用 port 白名单 + host 闭包持权 | 无直接守卫 | sing_song 纯转发+host 闭包+ToolError；game_launcher XDG 白名单 | **是** | 低 |
| 10 | 进程入口先 load_secrets() 再构造对象 | **test_env_centralization.py:136-157（AST 钉首句）** | qt_overlay.main() 首句 load_secrets()（:1348），先于 QApplication/AppHost；screen 解析 call-time 读 env 依赖 entry 先灌注 | **是**（复读 + 守卫确认；inner AppHost config.load 在 secrets 后无害——entry 已灌注） | 低 |
| — | env 名册「结构上无法漂移」 | test_resolved_config_equivalence.py:257-282（名级 meta-pin） | SCREEN/RESPEAKER 结构 import；APP 级 manager 硬编码 + 手工镜像 + 测试钉名 | **部分**（名级测试钉；APP 级字段映射非结构复用） | P3 |

---

## 15. 反向 grep 验证

### Qt 泄漏
**结果**：`spica/` 命中全 docstring（spica/__init__.py:6、host/app_host.py:10）。真 `from PySide6 import` 仅 tests/（UI 测试）。**判断**：✅ 无泄漏，test_layering AST 守卫在位且通过。

### agent 包泄漏
**结果**：命中全 provenance 注释（"Moved verbatim from agent/..."）。无真实 import。**判断**：✅ 无泄漏。

### env 直读
**结果**：业务真实命中仅 3 allowlist（secrets/manager/runtime_env）；余为注释、vendored（EXCLUDED_PARTS 排除）、tests。**判断**：✅ 集中收敛，test_no_getenv passed。注：env override 名册 APP 级分散于 manager（见 §11 P3），不违 #4。

### RuntimeEvent 边界
**结果**：定义 core/events.py；唯一产出 run_turn（turn.py:44）；fold.py 消费；chat_engine.py:208 转 legacy dict；companion/song events 子类（独立桥）；stages.py 只 import turn_error_to_legacy_dict（来自 context.py）。**判断**：✅ 边界清晰，pure transform 层不 import events。

### LLM 直接调用点
**结果**：真实客户端调用全在 adapters/llm/openai_compatible.py；`OpenAI(` 仅 agent_assembly.py:44；runtime 经 deps.llm。旁支 reaction_judge 用独立 JUDGE LLM，**仅评分（JudgeVerdict worth/moment/angle），不产用户台词**（galgame subagent 确认，judge 失败降级到 lexicon scorer）。**判断**：✅ 无第二条开口 LLM 链路。

### 任意执行 / act 工具风险
**结果**：业务码无 os.system/shell=True/eval(/exec(（假阳性）。真实 subprocess 仅 game_launcher（Popen 启游戏，XDG .desktop）、window_locator（run 查窗口）。**判断**：✅ 大体安全，act 工具无任意执行面。

---

## 16. 建议的后续治理路线

1. **[P1] Phase 7 前置**：recent_memory 按 `scoped_conversation_id` 命名空间化（chat_engine.py:235-240 TODO）+ 跨角色隔离正向测试。
2. **[P1] galgame OCR 隐私门收紧**：把 state 读 + capture 纳入同一锁/快照，消除残余 race；或显式接受并文档化窗口。
3. **[P2] judge LLM per-call 超时** + GPU 争用调度护栏（OCR/TTS/judge）。
4. **[P2] tools 子系统 e2e 测试**：补 live registry→ToolSet→orchestrator 工具解析。
5. **[P3] env 名册收口（可选）**：让 manager 从 `APP_ENV_MAP` 读名，兑现「结构上无法漂移」；或软化 CLAUDE 措辞为「测试钉死」。
6. **[P3] 文档漂移修订（第 3 趟 CLAUDE 闭环）**：app.yaml 节数（10 非 8）、xiaosan.env 措辞（override 层非仅密钥）、README STT（faster-whisper 非 speech_recognition）、README song intent_router（已删/迁 UI）、README §点歌的 SongIntentRouter 引用。
7. **[P3] 退役 `*.migrated` 清理**（CLAUDE 已计划）。

---

## 17. 测试结果

| # | 命令 | 结果 | 失败 | 熔断 |
|---|---|---|---|---|
| 1 | `pytest tests/test_layering.py tests/test_no_getenv.py -q` | 6 passed, 15 subtests | 无 | 否 |
| 2 | `pytest tests/test_turn_contract.py tests/test_resolved_config_equivalence.py -q` | 49 passed | 无 | 否 |
| 3 | `pytest tests -q -p no:cacheprovider`（全量） | **821 passed, 108 subtests in 15.27s** | 无（无 collection error、无 skip） | 否 |

**本轮架构结论经全量测试验证，可信度高**（不仅静态阅读 + grep）。注：tests subagent 曾预测 2 个硬 PySide6 import 在 headless 会 collection 失败、~15 个 importorskip 跳过；实测本机 conda gptsovits 环境 PySide6/模型齐备，全部运行通过，无跳过。

### 本轮自证复读（绕过 subagent，主上下文直读）
- **#10 load_secrets 时序**：✅ 成立。qt_overlay.main() 首句 load_secrets()，且 test_env_centralization.py:136-157 AST 钉死。host-config subagent 的「P1 违规」是误报（只看到 inner AppHost 顺序）。
- **env_roster 漂移**：✅ 修正为 P3。manager 硬编码 APP 级名但 test_resolved_config_equivalence.py:257-282 名级 meta-pin 守护；SCREEN/RESPEAKER 结构 import。subagent 的「无测试拦截」不准确。

---

## 18. 本轮阅读覆盖清单

- ✅ 根：README / CLAUDE / pytest.ini / webui_qt.py
- ✅ 守卫直读：test_layering / test_no_getenv / test_env_centralization / test_resolved_config_equivalence
- ✅ 自证直读：env_roster.py / manager.py（全）
- ✅ grep 反向验证：6 类全跑
- ✅ Wave-1 subagent：runtime(+chat_engine) / host(+config) / 工具系统 / memory
- ✅ Wave-2 subagent：galgame(+ports/adapters) / ui / tests+scripts
- ✅ 全量测试：821 passed

---

## 19. 附录：分模块审查记录（subagent 回报，主上下文已交叉验证）

### 模块：runtime + chat_engine（runtime-reviewer，高置信，10/10 是）
- **架构事实**：run_turn 唯一 emit（turn.py:43-44）；TurnRequest/Context/Deps 类型化边界；stages 纯 transform 无 emit；orchestrator 纯协调 + Sequencer 有序；tool_round probe→exec→followup + chainable 多轮 + 优雅超限；system turn 工具双处硬关断；cancellation 三检查点；sync_chain 冻结零生产调用方；proactive drop_if_busy→stream_system_turn→run_turn。
- **风险**：无 P0/P1（核心三问全过）。
- **不确定**：streaming probe 双端点分歧、observer「placeholder」、memory commit 静默失败、sync/streaming overflow 不对称、play unit hold gate 测试。
- **行数**：stages 1088 / orchestrator 518 / tool_round 334 / chat_engine 282 / turn 44 / sync_chain 61。

### 模块：host + config（host-config-reviewer，注：含 1 误报已纠）
- **架构事实**：initialize() ~84 行装配（薄）；836 行余量是 reaction/galgame host 闭包（铁律使然）；config 三载体；resolve-once+inject（screen:142 / song:145）；builtins 注册 LLM/TTS/Visual/Memory/inspect_screen，watch/note/sing 在 __init__ 带 host 闭包；app.yaml 10 节；song untyped（schema.py:307-312）。
- **风险**：[误报已纠] #10 时序 → 实为成立（见 §17 自证）；[纠为 P3] env_roster APP 级镜像；reaction lexicon mtime 热重载、余 restart-effective。
- **不确定**：ManagementSurface.save 是否丢注释；song 节是否支持热重载。

### 模块：工具系统（tooling-reviewer，高置信，无 P0）
- **架构事实**：4 工具元数据表（见正文 §2/§6）；register_tool 4 维；intent_gated 纯供给预筛不劫持（router.py:8-13）；sing_song act 安全；无 registry 绕过（N5）；intent.py/intent_rules.py 迁 UI 控制词快路径非 orphan；截图本地不上传（N0）。
- **风险**：P1 watch 隐私门 race；game_launcher XDG OS 信任。
- **不确定**：compact_output 是否脱敏；intent gate 误报率；available() 异常静默吞。

### 模块：memory（memory-reviewer / Haiku，关键点已主上下文复核）
- **架构事实**：recent(deque,裸 id) vs long-term(SQLite,scoped `{char}::{conv}`)；**Phase 0 耦合点正确**（effective_memory_conversation_id 使 galgame 仍读角色记忆，写回 caller scope，§27①）；game_memory 独立库/端口/schema；OCR 不进 recent；spica/memory=port 层，memory/=实现层；sync recent / async long-term。
- **风险**：P1 Phase 7 多角色 recent 未命名空间（chat_engine.py:235-240 TODO）；P2 JobRunner 静默失败；P2 upsert importance 跨talk；P2 source_line_ids O(n²)。

### 模块：galgame（galgame-reviewer，高置信）
- **架构事实**：Session 唯一 owner（RLock，私有态，公共事件 API）；OCR 串行+RapidOCR 锁；OCR 文本不变用户消息；gate retrieve_game_context_node（build_prompt 后/LLM 前，纯请求逻辑，none 时 byte no-op，绝不跑 LLM）；summary 锁内不可变 snapshot；崩溃恢复 recover_dangling_sessions 幂等；reaction 经 run_turn，judge 仅评分降级 lexicon；ChoiceEvent 两路径。
- **风险**：P1 OCR 隐私门 race；P2 judge 无 per-call 超时；P2 GPU 争用（30% 置信）；P1 summary snapshot age / CompanionBeat dedupe DB race。
- **不确定**：judge stall 防护、OCR+TTS+judge GPU 耗尽、dangling 永留 interrupted、CHOICE_CHECKING 反应跳过、Moondream 路径是否也查安全门。

### 模块：UI（ui-reviewer，高置信，全 是，无 P0/P1 越界）
- **架构事实**：#10 qt_overlay.py:1348 load_secrets 首（先于 QApplication:1357/OverlayWindow:1359/exec:1361）；UI 不 new 后端主服务，全经 AppHost；不碰 galgame domain 内部；后端→UI 全 RuntimeEvent/CompanionEventBridge/worker Qt queued signal；sink 在 exec 前挂；跨线程修复 46a926b（系统 turn marshal）/527c6bc（音频 teardown defer）已并入。
- **不确定**：SpeechWorker GUI 回调 marshaling、音频 teardown defer 双 free 窗口、SongWorker fallback load_song_config 是否守 #4、CompanionEventBridge signal 线程亲和。

### 模块：tests + scripts（tests-reviewer）
- **架构事实**：12 守卫表（layering/no_getenv/turn_contract/resolved_config/no_dict_config/no_static_tool_schemas/no_manual_reorder/no_raw_threadpool/no_log_timing/no_comment_gate/env_centralization/sqlite_concurrency_pragmas）；覆盖图见 §8；scripts 9 件诊断/守门/报告（见正文 §13 索引）。
- **缺口/风险**：tools 子系统测试最薄（P2）；registry e2e 部分；Phase 7 多角色隔离正向缺；run_turn 唯一 emit 仅负向。
- **熔断预测**：2 硬 PySide6 import（test_reaction_voice_duck / test_speech_worker_stt）headless 会失败——但本机实测齐备，全过。

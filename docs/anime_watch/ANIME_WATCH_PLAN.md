# Spica 看动漫功能 · 立项计划书（ANIME_WATCH_PLAN）

> 状态：**v2（已吸收对抗性 review）；Phase 0 探针完成、Phase 1 纯逻辑完成（全部 anime 单元/golden 绿 + 全量 pytest 绿）；下一步 Phase 2 adapters，阻塞在先决条件安装（§12）**。
> 按 CLAUDE.md 流程：本计划书 ≠ 动码授权；每个 Phase 开工前仍需按 guardrails §16 列修改文件清单并确认。
> 风险等级：**P1**（act 操控用户环境 + 外部网络依赖）。
> **v2 修订**：吸收 review 的 P0-1…P0-4 / P1-5…P1-12 / P2-13…P2-21，逐条落在下文并在 §14 索引。

---

## 0. 需求与范围

### 0.1 用户故事

- 「spica我想看无职转生第三季第一集」→ Spica 找到资源、下载、用系统默认播放器播放。
- 「spica我想看动漫」→ Spica 问「想看什么」→ 用户说片名 → 同上。
- 「spica我想看xxx最新一集」→ 解析为该番当前最新集（**v1 支持**，D10）。
- 同一集重复请求 / 「再放一遍」/「放吧」→ 本地已有文件直接开播，不重下。

### 0.2 v1 范围内

- 两级来源策略：**主 = bilibili 搬运 space**（默认 `3493112693394137`，可配多个）；**备 = 蜜柑计划**（官方站 RSS 为主，`mikan.tangbai.cc` 镜像备选）。
- 下载后播放（D1：不流播）。
- 完成行为「智能阈值」：快下自动播；慢下先语音播报「下好了，现在看吗」，等确认。
- 本地库索引（去重、磁盘占用统计、超上限提醒、崩溃对账）。
- 「最新一集」相对指代（D10）。

### 0.3 v1 范围外（明确不做）

- **视频陪看**（看画面吐槽）——将来做，v1 **零预埋代码**，仅 §5.8 留文档 hook。
- **整季合集 / batch 种子**（D11）——resolve 阶段直接过滤 batch 条目，只下单集。**注**：B 站「一个 bvid=整季合集分P」不是 batch，adapter 把它展开成 per-part 单集候选（`resolver.part_source_title`，`is_batch=False`），不被 batch 过滤打掉（Phase 1 review #1）。
- 播放进度控制（暂停/快进/看到哪了）——xdg-open 拿不到播放器进程状态，v1 不承诺。
- 追番订阅 / 自动追更、BD 合集整理、弹幕。
- **非剧集内容 / 复杂话数映射**（Phase 1 review #5，resolver 明确不做，安全非匹配而非错配）：OVA/OAD/SP/剧场版/总集篇/特别篇/前传标 `is_special` 不当作「第 N 集」；**クール 偏移不重映射**（13.. 不折算成 01..，按标题原样取号）；**绝对话数不重映射**（「第25话」不折算成 S2E01）。这些 v1.1 再挂账。

---

## 1. 已拍板决策（含机器环境事实与 v2 新增 D10/D11）

| # | 决策点 | 结论 |
| --- | --- | --- |
| D1 | 交付形态 | **下载后播放**，不做流播 |
| D2 | BT 后端 | **qBittorrent（qbittorrent-nox + Web API）**；机器现未安装（先决条件 §12，含 `systemctl enable --now`）；已装的 aria2 留作 port 后备 adapter，v1 不实现 |
| D3 | B 站下载器 | **yt-dlp**（pip 装入现有 gptsovits 环境；纯 Python；ffmpeg 已在位）；版本策略见 §12（滚动升级 + 已知good下限，B 站 extractor 以月为单位破损） |
| D4 | 播放器 | **系统默认**：Linux `xdg-open`（当前 GNOME Totem）/ Windows `os.startfile`；`player_command` 覆盖项（机器有 vlc 兜底 mkv/HEVC） |
| D5 | 完成行为 | **智能阈值**（默认 300s）：快下自动播；慢下先系统 turn 播报等确认 |
| D6 | 磁盘策略 | **不自动删**；占用上限（默认 100GB），超限播报时提醒 |
| D7 | 画质字幕 | **1080p，简体/简繁内封优先**；B 站 1080p 需登录 cookie（secrets，§6） |
| D8 | 蜜柑源 | **可直连官方站** → 官方 `mikanani.me` 为主，`mikan.tangbai.cc` 镜像备选，base_url 列表可配 |
| D9 | 陪看衔接 | 将来做，v1 零预埋（§5.8 仅文档） |
| **D10** | 「最新一集」 | **v1 支持**：映射为「当前已发布最高集」；来源歧义（季度/合集）时不静默选，走候选确认（§5.1） |
| **D11** | 整季合集 | **v1 只单集**：resolve 阶段过滤 batch 种子（§5.3）；补旧番若仅合集可用则 `ANIME_NOT_FOUND`，v1.1 挂账 |

---

## 2. 铁律映射（本功能直接受约束的条款）

| 铁律 | 在本功能的落点 |
| --- | --- |
| #1 spica/ 无 Qt | `spica/anime/`、ports、adapters、assembly 全 Qt-free；worker/播放器 Qt 侧只在 `ui/` |
| #2 跨线程只走 RuntimeEvent | **仅两类跨 host→UI 边界的通道**：① host 闭包 emit `AnimeRequestEvent`（触发下载）；② UI→host 的**状态回流 seam**（P1-6，worker 经注入回调上报进度/完成给 host 持有的 library）。**下载进度事件不跨边界**——它产生于 UI worker、消费于 UI controller，走 Qt signal UI 内部自持（P2-19 修正：v1 早稿把此通道说宽了） |
| #3 唯一对话路径 | 触发 = 主 LLM function call（`watch_anime`）；「下好了」播报 = `ProactiveTurnRequest` → `ProactiveTurnArbiter` → `stream_system_turn` → run_turn。**无**第二套 prompt、**无**前置意图劫持、**无**为消歧单跑 LLM |
| **#5 Host 必须薄** | **（v2 补漏）** 装配走 `spica/host/assemblies/anime.py`（install(host) + 构建函数），检索/回退/查重编排下沉 `spica/anime/coordinator.py`；AppHost 内 ≤15 行（一次 install 调用 + 薄委托）。**不**仿旧 `_request_song` 直接堆进 `AppHost.__init__`（那是 Phase 4 前存量形制，guardrails §3.1/§12b 已废止新增） |
| #9 act 白名单 | 三个专用 port（§3.1）动作面全枚举 + adapter 内是唯一执行/校验点；工具是纯转发垫片；执行权限在 host 闭包；**绝不** exec/eval/shell 拼 LLM 字符串、绝不播放 download_dir 之外或非媒体扩展名的文件（P0-3/P0-4） |
| #10 入口先灌注 | B 站 cookie / qbt 密码走 `xiaosan.env` + `load_secrets()`；依赖 `qt_overlay.main()` 首句（已有 AST 守卫） |
| #11 守卫测试 | 新增 config 节动 resolved-config 基线：**改前 dump，改后 `--diff` 只允许 anime 新增键，零既有值漂移**；Layer B (`test_resolved_config_equivalence`) 同步更新 |

---

## 3. 架构落点

### 3.1 新文件清单

```
spica/ports/
  anime_source.py        # AnimeSourcePort（review #3 契约明确化）：
                         #   search(query) -> [AnimeCandidate]  （匹配数据；B 站合集展开为 per-part 单集）
                         #   materialize(candidate) -> AnimeResource  （只对选中项做最后一公里物化）
  torrent_client.py      # TorrentClientPort 白名单动作面（P0-3 收口）：
                         #   add_magnet(magnet)          -- 强制校验 magnet:?xt=urn:btih: 格式，
                         #                                  非 magnet(含 http(s) torrent URL)一律拒绝
                         #   status(task_id) / cancel(task_id)  -- 强制 category=spica-anime 过滤(P2-20)
                         #   save_dir 不在动作面上——adapter 构造期钉死为 resolve 后的 download_dir
  media_player.py        # MediaPlayerPort 白名单动作面（P0-4 收口）：
                         #   play_file(path) —— adapter 内是唯一执行点，强制：
                         #     Path(path).resolve().is_relative_to(download_dir.resolve())  (非 startswith)
                         #     + 存在性 + is_file() 常规文件 + 媒体扩展名白名单(.mkv/.mp4/.ts/...)
                         #   拒绝 .desktop/.sh/.html 等（种子内文件名由作者控制,是代码执行面）

spica/adapters/anime_source/
  bilibili_space.py      # 主源：space 视频列表检索（WBI 签名）+ yt-dlp 下载；每源超时(§5.2)
  mikan.py               # 备源：官方 RSS 为主 + HTML 兜底(挂账)；base_url 列表轮询；每源超时(§5.3)

spica/adapters/torrent/
  qbittorrent.py         # qBittorrent Web API（localhost）；所有操作强制 category=spica-anime

spica/adapters/media_player/
  system_default.py      # Linux: xdg-open / Windows: os.startfile；player_command 覆盖时用它

spica/adapters/tools/
  watch_anime.py         # act 工具纯垫片（仿 sing_song.py），零业务逻辑

spica/anime/             # 域层（Qt-free，仿 spica/galgame/）
  models.py              # AnimeCandidate / AnimeResource / EpisodeRef / DownloadTask dataclass
  resolver.py            # 「无职转生第三季第一集」/「最新一集」→ EpisodeRef 的确定性解析 + 模糊匹配
  coordinator.py         # 检索/主备回退/查重编排（P0-2：从 host 下沉）+ materialize 选中项；
                         #   注入式 clock+budget_seconds+per_source_timeout+cancelled，出
                         #   resolve_timeout/cancelled outcome；per-source 错误进 errors trail
                         #   不吞 code（review #2/#3/#8，§5.2/5.3/P1-8，纯逻辑不接网络）
  library.py             # 已下载索引（去重/路径/磁盘统计/上限/对账纯逻辑）；JSON 落盘
  playback_policy.py     # 完成行为阈值判定纯函数（P2-16：从 ui controller 下沉,便于单测）

spica/core/
  anime_events.py        # AnimeRequestEvent / AnimeReadyEvent（跨 host→UI；仿 song_events.py）
                         # 注：进度事件不在此(UI 内部 Qt signal,P2-19)

spica/host/assemblies/
  anime.py               # （P0-2）install(host)：装配 coordinator/ports，注册 watch_anime 工具，
                         #        注册 host 持有的写权限闭包(播放校验 + library 唯一写点 + emit)

ui/
  workers/anime_worker.py          # 下载执行（yt-dlp 子进程 / qbt 轮询）；进度 Qt signal；
                                    # 经注入回调向 host library 上报(P1-6);退出时子进程生命周期(P1-9)
  controllers/anime_controller.py  # 消费 AnimeRequestEvent/AnimeReadyEvent、忙态降级、起播、状态 chip
```

### 3.2 修改的既有文件

| 文件 | 改动 |
| --- | --- |
| `spica/config/schema.py` | 新增 `AnimeConfig` typed 节（§6），挂到 `AppConfig` |
| `data/config/app.yaml` | 新增 `anime:` 节（app 级第 11 键）；**`enabled` 默认 false**（P1-12：Phase 4 端到端过后翻 true） |
| `spica/config/secrets.py`（名册侧） | 新增 B 站 cookie + qbt 密码 secret 名（Phase 3 定名） |
| `spica/host/app_host.py` | **仅** ≤15 行：调用 `assemblies/anime.py::install(self)` + 薄委托（P0-2；不新增 per-domain 方法体） |
| `agent_tools/function_tools/router.py` | **仅当选词表路线才改**（P0-1）；本计划选 `intent_gated=False` 状态供给路线 → **不改 router.py**（见 §5.7） |
| `ui/qt_overlay.py` | `AnimeRequestEvent`/`AnimeReadyEvent` 桥接 → controller（仿 SongRequestEvent 桥） |
| 守门 | `tests/test_resolved_config_equivalence.py` 基线更新 + 新增 §10 测试文件 |

**不碰**：`run_turn` / `stages.py` / `orchestrator.py` / `tool_round.py` / `prompt_builder.py` / `ChatEngine`（`stream_system_turn` 现成，零改动）/ `MemoryPort` / `MemoryScope` / recent memory / galgame 全域 / registry 机制本身 / `proactive.py`（P1-5：不实现 `queue_latest` 保留字段，重试在 controller 侧做）/ 冻结链 / v1 `LLMPort` / `domain_router.py`（v1 无 turn binding）。

### 3.3 明确不建的东西（防过度设计）

- **不建** domain turn binding / 不占 conversation 前缀 / 不建新 MemoryScope——v1 就是普通聊天里的一个 act 工具。
- **不建**第二个「确认播放」工具——「放吧」由 LLM 再调 `watch_anime`，闭包命中 library / 「最近完成未播」指针 → 播放分支（§5.5 + P1-11）。
- **不写** recent memory 特例。
- **不改** `proactive.py`（P1-5：忙态重试在 controller 侧，不动 arbiter 语义）。

---

## 4. 数据流

### 4.1 主链路

```
用户:「spica我想看无职转生第三季第一集」
  → run_turn → LLM 调 watch_anime(query="无职转生 第三季", episode=1)   (episode 为 optional,缺省=询问/候选)
  → 工具垫片 → host 写权限闭包（assemblies/anime.py 注册）:
      1. resolver 解析 EpisodeRef（番名规范化 + 季/集抽取；"最新一集"→LATEST 哨兵）
      2. library 查重（含"最近完成未播"指针,P1-11）→ 命中 → 请求 host 播放闭包 play_file → 返回「已开播」
      3. coordinator.resolve（带每源超时+总预算,P1-8）:
         主源 bilibili_space → 命中 → AnimeResource(url)
         未命中/失败/网络错 → 备源 mikan（过滤 batch,D11）→ 磁力 AnimeResource(magnet)
         候选歧义（含 LATEST 季度歧义）→ 返回候选列表由 LLM 向用户确认（绝不静默选最相似,D10/P1-11）
         真彻底没有 → ToolError(ANIME_NOT_FOUND)；网络全挂 → ToolError(ANIME_SOURCE_ERROR)（P1-10 改码）
         已有任务在下 → ToolError(ANIME_DOWNLOAD_BUSY,带当前进度)（进度来自 host 内状态,P1-6）
      4. emit AnimeRequestEvent（fire-and-acknowledge，立即返回工具结果）
  → turn followup：Spica 角色化回应「找到啦，我去下～」
  → UI 桥接事件 → anime_worker 执行下载:
      yt-dlp 子进程 / qbt add_magnet(仅magnet) + 轮询；进度走 UI 内部 Qt signal
      经注入回调向 host library 上报进度/完成（P1-6 seam）
  → 完成 → AnimeReadyEvent → controller 过 playback_policy + 忙态判定（P1-7）:
      耗时 ≤ threshold 且 NOT busy 且 galgame 未活跃 → 直接请求 host 播放闭包 play_file
      否则（慢下 / 忙 / 陪玩中）→ ProactiveTurnRequest → try_speak；
         busy 返回 False → controller 退避重试（P1-5：下载完成状态持久,重试无害）直到成功或用户先开口
```

### 4.2 歧义确认（多轮，走 turn 本身）

```
watch_anime 返回 {"candidates": ["无职转生II 第2クール(01起)", "无职转生II(13起)", ...]}
  → LLM 在 followup 里复述候选 → 用户选 → 下一 turn LLM 带明确参数再调
  （chainable=False：多轮由对话天然承载,不进工具轮循环）
```

---

## 5. 关键设计细节

### 5.1 标题解析（resolver.py）——本功能真正的难点

- 「无职转生第三季第一集」→ `EpisodeRef(title_query, season, episode)`：中文数字（含十一以上）/阿拉伯数字、「第x季/期/部」「第x集/话/話」、罗马数字 Ⅱ/Ⅲ、"S3E1" 全部规范化。**纯确定性代码**，golden cases 钉死（§10 + P2-13 清单）。**`クール` 不做规范化/重映射**——与 §0.3 一致，按标题原样取号（不折算 cour 偏移）。
- **「最新一集」（D10）**：`episode=LATEST` 哨兵；coordinator 从来源（蜜柑 RSS / space 列表）取当前最高集号；**若季度存在歧义（跨季/合集混入）不静默选，走候选确认**（cour/绝对话数编号本身不重映射，见 §0.3——那类是安全非匹配，不是候选确认）。
- 匹配来源条目（B 站视频标题 / 蜜柑发布名如 `[字幕组] Mushoku Tensei S3 - 01 [1080p][简繁内封]`）：规范化后模糊匹配。**季度标记先消费再抽集号**（否则「第3季」的 3 被当集号，Phase 0 实测）；season/episode 从**去掉 subgroup 后的原文**抽取，`[02]` 括号集号也能解析（review #6）；同番不同字幕组的长短名按子串归并（review #4）。
- **置信不足必须走候选确认，禁止静默选最相似**（P1-10）：短 query + 多个不同番、或 ep_pool 跨多个不同番名 → `ambiguous` 出候选，不静默取 best（review #4，`resolver._cluster_by_title`）。
- **匹配不唯一 → 候选列表进工具结果**，由主 LLM 向用户确认。**禁止**为消歧单跑 LLM（铁律 #3）。
- 别名（「无职转生」/「Mushoku Tensei」/「無職転生」）：v1 内置小别名规范化；不建大别名库，不够用挂账。
- **v1 不做的话数映射**见 §0.3（specials 非剧集、クール/绝对话数不重映射，安全非匹配）。

### 5.2 主源：bilibili space（Phase 0 已侦察，见 probes/PHASE0_FINDINGS.md）

- space UID **可配列表**，默认 `["3493112693394137"]`；`bilibili_fallback_search` 默认关（避免搜到不相干投稿）。
- **resolve 结果是 `(bvid, part_index)` 不是单 URL**（Phase 0 实测：搬运号是「一个 bvid = 整季合集分P」，如 `【4K超清】无职转生 第三季 01-02话`，ep1=P1/ep2=P2）；adapter 用 yt-dlp 按 `-I <part>`/`?p=N` 下指定集。matcher 先定位「番+季」合集视频，再把分P列表映射到集号——**集号在标题里位置不固定，不能靠位置解析**。
- **风控重试是硬要求（Phase 0 实测）**：裸调 `-403`；仅 WBI `-352`；**buvid3/buvid4 指纹（`finger/spi`）+ dm_* 指纹参数 + WBI 签名才通，且概率性**（单页 1-3 次重试）。adapter 必须 re-seed buvid 重签重试；**有 cookie 优先带**（既稳风控又解 1080p，D7）。页间加节流。
- 下载 yt-dlp 子进程：**每源网络超时 `source_timeout_seconds`（§6）**、stderr 收集、`.part` 断点续传；无/失效 cookie 时的行为见 §7（区分降清晰度 vs auth 失败 vs 充电专属）。
- **搬运号随时会没是常态**：任何失败 → WARNING + 静默回退蜜柑，不崩 turn。

### 5.3 备源：蜜柑计划

- 官方 `mikanani.me` 为主（Phase 0 实测可直连、RSS 完整）；`mikan.tangbai.cc` 镜像**本机实测不可达（http=000）**，放列表末位或删（D8），`base_url` 列表按序重试；**每站超时 + 总预算（P1-8）**。
- **磁力直接由 RSS 拼，不下 .torrent（Phase 0 确认）**：`enclosure@url` / `Home/Episode/` 里的 40 位十六进制就是 btih（下载 .torrent 算 btih 逐字节验过），`magnet:?xt=urn:btih:<40hex>&dn=<title>`。满足 P0-3 magnet-only、零 SSRF。
- **v1 RSS-only（Phase 0 验证足够，P2-19）**：`RSS/Search?searchstr=<番名>` 返回完整 item（title/enclosure/length/pubDate）；HTML 解析作为后续挂账项，不进 v1。
- 字幕组过滤：1080p + 简体/简繁关键字（`1080|简|CHS|GB` 优先级表）；`preferred_subgroups` 可配（默认空=按规则自动挑）。
- **过滤 batch（D11）**：合集/多文件种子（`[01-12]`、`Fin`、`Complete` 等标记）在 resolve 阶段直接跳过，只取单集条目。

### 5.4 BT 下载与生命周期（qBittorrent）

- `qbittorrent-nox` + Web API（localhost:8080）；类别 `spica-anime`；保存到 `download_dir`（**expanduser 后传绝对路径给守护进程，`~` 不能指望对端展开，schema validator 处理**，P2-14）。
- **只认领 `spica-anime` 类别的任务**，所有 status/cancel 强制类别过滤（P2-20），绝不动用户手动种子。
- 下完停止做种；v1 不做上传管理承诺。
- **生命周期（P1-9，v2 补三条）**：
  1. 退出时 **yt-dlp 子进程**：终止并保留 `.part`（qbt 是外部常驻服务、退出后继续；yt-dlp 是 app 子进程，不能留孤儿）。
  2. **重启后 in-flight 任务一律按「慢下」处理**（start_time 在 worker 内存里、重启后耗时不可知，阈值判定失效）。
  3. **启动对账**：按类别对账「qbt 已完成但 library 未登记」→ **只登记、只播报（走 P1-5 重试），永不 auto-play**（防开机突然弹播放器）。
- 卡死：`stall_timeout_minutes`（默认 30）无进度 → 系统 turn 询问换源/取消。
- qbt 轮询中断连/重启：**重连而非判失败**（P1-10）。

### 5.5 完成行为（D5）与「放吧」闭环

- worker 记录起始时刻；`playback_policy`（纯函数，spica/anime/）判耗时。
- **auto-play 前必须过忙态真值 + galgame 活跃判定（P1-7）**：正在 TTS 说话/唱歌，或 galgame 陪玩进行中（否则播放器窗口弹出会触发 privacy gate 暂停陪玩）→ 降级为播报路径。
- 播报走 `ProactiveTurnRequest`（drop_if_busy）。**`try_speak` busy 返回 False 会静默丢弃、不排队（实测 proactive.py 语义，P1-5）**——「下好了」不可弃，故 controller 消费返回值、**False 则退避定时重试**直到成功或用户先开口。不改 proactive.py。
- 「放吧」**不需要新机制**，但需三重兜底（P1-11）：
  ① 播报 directive **内嵌规范化「标题 第x季 第x集」**（供 LLM 二次调用复原参数）；
  ② closure 维护「**最近完成未播**」指针，query 模糊命中该指针即直接播（应对换说法 / recent deque 滚掉 / app 重启后 recent 为空）；
  ③ `episode` 参数 **optional**（strict schema 里非 required），用户只说片名时不逼 LLM 瞎填集数。

### 5.6 播放（MediaPlayerPort）

- 动作面唯一：`play_file(path)`；**校验下沉 adapter 内部成为唯一执行点**（P0-4c：auto-play 也经 host 播放闭包，不让 UI controller 直调绕开校验）。
- 校验四关（P0-4a/b）：`Path.resolve().is_relative_to(download_dir.resolve())`（**非 startswith**，防 `SpicaAnimeEvil` 无分隔符前缀绕过）+ 存在性 + `is_file()` + **媒体扩展名白名单**（拒 `.desktop/.sh/.html`——种子内文件名由作者控制，是 xdg-open 代码执行面）。
- **library 只登记合并/改名后的最终文件、播放只走 library 路径**（P1-10：钉死「.part 被播放」）。
- Linux `xdg-open`（Totem）；`player_command` 非空时用它（如 vlc）；Windows `os.startfile`（v1 验收只做 Linux）。
- 局限如实：xdg-open 后拿不到播放器进程状态——无「看完了」事件（D6 选「不自动删」原因之一）。

### 5.7 工具注册（watch_anime）——供给路线（P0-1 收口）

- **选定 `intent_gated=False`（状态供给路线，仿 `watch_game_screen`）**：供给纯靠 `available` 谓词（`enabled` 且 controller 已附着），「调不调」是 LLM 按 description 的结构化决策。
- **理由（实测 router.py）**：`_tool_names_for_text` 硬编码只认 `inspect_screen`/`sing_song`；若 `intent_gated=True` 而不改 router.py，工具永远不被供给（一次都触发不了）。更关键：词表只扫当前 user_text，「放吧」「嗯」「好啊」这类纯确认词无「看/番」词命中 → 词表路线下「放吧」闭环必挂。状态供给绕开整个词表脆弱面（这正是 watch_game_screen 走此路的原因）。
- **因此不改 `router.py`**（§3.2 已标注）。
- `available` 谓词 **live-read 且容忍 config 未加载**（P2-15：注册在 assembly 装配期，谓词异常会被 registry 吞掉隐藏工具——不靠碰巧，显式写 try 容错）。
- description 采 CONFIRM_FIRST 风格（仿 sing_song）：用户没给具体片名时不调工具、先问清；参数 `query`（片名+季）+ `episode`（**optional**，含 `"latest"` 取值，P1-11）。
- `chainable=False`；`effect="act"`。
- 失败一律 ToolError 信封（`ANIME_NOT_FOUND` / `ANIME_SOURCE_ERROR` / `ANIME_DOWNLOAD_BUSY` / `ANIME_DISABLED` / `ANIME_RESOLVE_TIMEOUT`），不抛崩 turn。
- 并发 v1 **单飞**：已有任务在下 → `ANIME_DOWNLOAD_BUSY`（带进度，进度读 host 内状态 P1-6），LLM 自然转述「上一集还在下哦，xx%」。

### 5.8 陪看 hook（只留文档，不预埋代码）

将来陪看接入点：① `MediaPlayerPort` 换/增 mpv adapter（`--input-ipc-server` 提供进度与暂停控制）；② 采帧走既有 screen pipeline + `WindowTarget`，新建自己的 privacy gate 实例；③ 吐槽走 ProactiveTurnArbiter。**v1 代码不为此做任何预留**（D9）。

---

## 6. 配置草案（P2-14 补键）

```yaml
# data/config/app.yaml 新增节（AnimeConfig，typed）
anime:
  enabled: false                     # P1-12：Phase 4 端到端过后翻 true
  download_dir: "~/Videos/SpicaAnime"   # validator expanduser→绝对路径(传 qbt 守护进程,P2-14)
  disk_limit_gb: 100                 # D6
  auto_play_threshold_seconds: 300   # D5
  player_command: ""                 # 空 = xdg-open / os.startfile
  bilibili_spaces: ["3493112693394137"]
  bilibili_fallback_search: false
  mikan_base_urls: ["https://mikanani.me", "https://mikan.tangbai.cc"]
  preferred_subgroups: []
  quality: "1080p"                   # D7
  subtitle_preference: ["简体", "简繁"]
  stall_timeout_minutes: 30
  source_timeout_seconds: 15         # P1-8：每源网络超时
  resolve_budget_seconds: 45         # P1-8：resolve 总预算
  qbittorrent_url: "http://127.0.0.1:8080"
  qbittorrent_username: "admin"      # 密码进 secrets（P2-14：用户名是 config）
  qbittorrent_poll_seconds: 5        # P2-14
  ytdlp_format: "bv*[height<=1080]+ba/b[height<=1080]"  # P2-14
```

- secrets（`xiaosan.env`，经 `load_secrets()`）：B 站 cookie、qbt Web API 密码。
  - **cookie 形态（P2-14）**：以 `cookies.txt` 落盘喂 yt-dlp（等于把 secret 落盘成文件）→ 计划书写明其路径与生命周期（放 data/、gitignore、可由用户随时替换）；或 Cookie header 直传（不落盘，Phase 3 定）。
- **纪律**：全走 typed config；不新开 `os.getenv`；改解析前 dump 基线、改完 diff 只含 anime 新增键；Layer B 同步。

---

## 7. 失败模式矩阵（P1-10 改码 + 补行）

| 失败 | 错误码 / 行为 |
| --- | --- |
| B 站 space 无此番/视频被删/风控 | WARNING + 静默回退蜜柑 |
| **蜜柑主备 base_url 全挂（网络）** | **`ANIME_SOURCE_ERROR`（不是 NOT_FOUND——断网≠没这番，P1-10 改码）** |
| 来源可达但确无此番/集 | `ANIME_NOT_FOUND` |
| resolve 超总预算 | `ANIME_RESOLVE_TIMEOUT`（「还没找到，稍后再试」，P1-8） |
| 磁力长时间 0% / 卡进度 | stall_timeout → 系统 turn 询问换源/取消 |
| **磁盘写满**（qbt errored / yt-dlp 写失败） | 事件带错误 → 系统 turn 提示清理（P1-10 补行） |
| **qbt 轮询中断连/重启** | **重连而非判失败**（P1-10 补行） |
| **磁力是合集/多文件种子** | resolve 阶段过滤，不进下载（D11/P1-10） |
| yt-dlp 需登录而无 cookie | 自动降清晰度 + followup 说明 |
| **cookie 过期** | yt-dlp 可能静默降 480p 或报 auth——**两者都识别并播报**（P1-10） |
| **B 站充电/大会员专属** | auth 失败 ≠ 降清晰度，独立播报「这集要充电/大会员」（P1-10） |
| **同名不同季模糊误匹配** | 置信不足**走候选确认，禁止静默选最相似**（P1-10 / §5.1） |
| qbt Web API 连不上 | `ANIME_SOURCE_ERROR`，提示检查 qbittorrent-nox（v1 不自动拉起进程） |
| 播放器打不开文件 | AnimeReadyEvent 带错误 → 系统 turn 提示（player_command=vlc 兜底） |
| `.part` 被播放 | 由「library 只登记最终文件、播放只走 library」结构性杜绝（P1-10） |
| 磁盘超限 | 下载照常 + 完成播报里提醒清理（D6） |

---

## 8. 合规注记（如实，不展开）

搬运号视频与字幕组磁力属版权灰/黑区，B 站下载亦违其 ToS；本功能是用户本地个人自用工具，决定与责任在用户。工程侧对应：来源全部 adapter 化 + base_url/space 可配——**来源天生短命，换源不动 runtime 是本设计核心诉求**。

---

## 9. 不碰的边界（逐条）

- `spica/runtime/{turn,stages,orchestrator,tool_round,prompt_builder}.py`：零改动。
- `ChatEngine`：零改动（`stream_system_turn` 现成）。
- `proactive.py`：零改动（P1-5：不实现 queue_latest，重试在 controller）。
- `MemoryPort` / `MemoryScope` / recent memory：零改动，无新记忆类型。
- galgame 全域：零改动（auto-play 只**读** galgame 活跃状态做忙态判定，不改其状态）。
- registry 机制本身：只调 `register_tool`，不改注册表。
- `router.py`：零改动（P0-1：走状态供给路线，§5.7）。
- 冻结链 `sync_chain.py`、v1 `LLMPort`、`domain_router.py`：不碰。

---

## 10. 测试计划（补 P0-1/P1-5/P1-9/P2-16 缺口）

自动化（不接真网络，全 mock）：

- `test_anime_resolver`：标题解析 golden cases（中文数字含十一以上/罗马数字/S3E1/「第x话」/**「最新一集」LATEST**/剧场版·OVA·SP/总集篇 x.5/cour 集数偏移/绝对话数 vs 季内话数/v2 修正版后缀/第0话·前传）+ 来源条目模糊匹配 golden（真实风格发布名样本）+ 歧义→候选列表 + **置信不足不静默选**（P2-13 全清单）。
- `test_anime_library`：去重、已完成命中→播放分支、磁盘统计与上限、**崩溃对账纯逻辑**（mock qbt 状态；对账函数归 Phase 1，P2-17）、**对账补登记项只登记不 auto-play**（P1-9）。
- `test_anime_source_fallback`：主源失败→备源、**网络全挂→SOURCE_ERROR（非 NOT_FOUND）**、真没有→NOT_FOUND、base_url 轮询、**batch 条目被过滤**（D11）、每源超时+总预算（P1-8）。
- `test_watch_anime_tool`：注册元数据（effect="act"/chainable=False/**intent_gated=False**）、**available 谓词容忍 config 未加载不抛**（P2-15）、ToolError 各错误码、BUSY 单飞带进度、垫片纯转发、**episode optional**（P1-11）。
- `test_watch_anime_supply`：**状态供给路线**——enabled 且 controller 附着才供给；「放吧」纯确认词也能供给（P0-1 核心回归）。
- `test_media_player_port`：路径白名单（download_dir 内/外、**前缀无分隔符绕过 `SpicaAnimeEvil`**、软链 realpath、**非媒体扩展名 `.desktop/.sh` 拒绝**、`.part` 拒绝）（P0-4）。
- `test_torrent_client`：**add_magnet 拒非 magnet（含 http torrent URL）**（P0-3）、status/cancel 强制类别过滤（P2-20）。
- `test_anime_playback_policy`：**阈值判定纯函数**（快/慢分支）+ 忙态降级 + galgame 活跃降级（P1-7；从 controller 下沉便于测，P2-16）。
- `test_anime_events`：`AnimeRequestEvent`/`AnimeReadyEvent` 经 `register_event` 解码 round-trip（P2-16）。
- `test_anime_completion_retry`：**busy 丢弃→退避重试→最终播报**（P1-5）。
- `ui` 测试（`pytest.importorskip PySide6`）：`anime_worker`（子进程生命周期/退出保留 .part，P1-9）、`anime_controller`（事件消费→policy→起播/降级）（P2-16 / guardrails §3.2 要求）。
- 既有守门：`test_registry` / `test_resolved_config_equivalence`（基线更新）/ `test_no_getenv` / 全量 `python -m pytest tests -q`。

手动验收（真机）：真实 space 检索命中率、真实蜜柑磁力下载一集、真实 Totem/vlc 播放、慢下播报→「放吧」闭环、cookie 高清下载、「最新一集」解析、退出/重启生命周期。

---

## 11. Phase 拆解（每个 Phase 结束跑全量测试）

| Phase | 内容 | 出口标准 |
| --- | --- | --- |
| **0 探针** ✅ | 一次性脚本（不进 spica/）验证：space API 可爬性+命名规律；蜜柑官方站 RSS 结构+磁力覆盖；qbt Web API 走通 add/status/cancel。**样本入库前脱敏**（cookie/buvid/WBI key 不进 git，P2-21） | **mikan/bilibili 探针已跑，样本+结论存 probes/（PHASE0_FINDINGS.md）；qbt 探针就绪待安装。开放问题 1/2/3/6 已答，4/5 待安装/手动** |
| **1 纯逻辑** ✅ | `spica/anime/` models + resolver + coordinator（编排骨架，源用 mock）+ library（含**对账纯逻辑**，P2-17）+ playback_policy + ports 定义 + 全部 golden/单元测试 | **完成：models/resolver/coordinator/library/playback_policy + 3 ports；全部 anime 单元/golden 测试绿（真实样本验证）；全量 pytest 绿** |
| **2 adapters** | bilibili_space / mikan（RSS-only）/ qbittorrent / system_default 四 adapter（合同测试 mock 网络层） | mock 合同测试绿；真机脚本各跑通一次 |
| **3 工具+装配** | watch_anime 垫片 + `assemblies/anime.py`（install + 写权限闭包）+ anime_events + AnimeConfig/secrets + 基线 diff + **enabled 默认 false** | 工具经 registry 状态供给、供给测试绿、ToolError 全路径、config 零漂移；**真机此时不触发下载**（P1-12） |
| **4 UI+完成行为** | anime_worker / controller / 事件桥 / playback_policy 接线 / 忙态降级 / 系统 turn 播报 + 重试 / 生命周期 | 真机端到端：语音点片→下载→自动播 & 慢下播报→「放吧」→播；**过后翻 enabled=true**（P1-12） |
| **5 打磨** | 磁盘提醒、崩溃对账接线、stall 处理、cookie 生命周期、文档收尾（CLAUDE.md §0 立项状态更新） | 手动验收清单全过 |

顺序呼应 CLAUDE.md §5：**先手喂样本把纯逻辑跑绿（Phase 1），最后才接最脏最飘的真实来源（Phase 2+）**。

---

## 12. 先决条件（Phase 0 前用户侧准备）

```bash
sudo apt install qbittorrent-nox
sudo systemctl enable --now qbittorrent-nox    # P2-18：装而不常驻→高频 SOURCE_ERROR
# 首次启动设 Web UI 用户名/密码（密码进 xiaosan.env,用户名进 app.yaml）
pip install yt-dlp                             # 装进现行 gptsovits 环境
# yt-dlp 版本策略（P2-18）：滚动升级（B 站 extractor 以月为单位破损），
#   requirements 记已知good下限；破损归 §7「yt-dlp 需登录/降清晰度」或新增一行,升级即修复。
# B 站 cookie 届时按 Phase 3 说明放 xiaosan.env / data/cookies.txt
```

---

## 13. 开放问题（Phase 0 状态见 probes/PHASE0_FINDINGS.md）

1. ✅ **已答**：搬运号 = 一个 bvid = 整季合集分P；resolve 出 `(bvid, part)`；集号标题位置不固定（§5.2）。
2. ✅ **已答**：匿名可爬但概率性风控，需 buvid 指纹 + dm 参数 + WBI + 重试；cookie 优先带（§5.2）。
3. ✅ **已答**：官方 RSS 完整带磁力（infohash 直拼），v1 RSS-only 足够（§5.3）。
4. ⏳ **阻塞安装**：qbt 免密 vs 密码——`probe_qbt.py` 就绪，待用户装 qbittorrent-nox 后跑。
5. ⏳ **待手动验收**：Totem 对 `1080p HEVC-10bit + ASS 内封` mkv 兼容性——倾向 `player_command` 默认 vlc，下到真集后验。
6. ✅ **已答**：蜜柑 RSS 按 pubDate 倒序取最新集（先按番+季过滤再取最大集号）；季度歧义走候选确认（§5.1/D10）。

---

## 14. v2 修订索引（review 发现 → 落点）

| 发现 | 落点 |
| --- | --- |
| P0-1 供给机制断裂 + router.py 缺失 | §5.7（选 intent_gated=False 状态供给，不改 router.py）+ §10 test_watch_anime_supply |
| P0-2 照抄废止的 host 装配形制 + 漏铁律#5 | §2（补 #5 行）+ §3.1（assemblies/anime.py + coordinator.py）+ §3.2（app_host ≤15 行） |
| P0-3 torrent 动作面泄漏 save_dir/任意 URL | §3.1 torrent_client（save_dir 移出面 + magnet-only 校验）+ §10 test_torrent_client |
| P0-4 play_file 三洞（前缀/扩展名/绕校验） | §5.6（is_relative_to + 扩展名白名单 + adapter 唯一执行点）+ §10 test_media_player_port |
| P1-5 drop_if_busy 语义写反 + 丢播报无补救 | §5.5（controller 消费返回值退避重试，不改 proactive.py）+ §10 test_anime_completion_retry |
| P1-6 缺 UI→host 状态回流 seam | §2 铁律#2 + §3.1（host 持 library 唯一写点，worker 注入回调上报）+ §5.4 |
| P1-7 auto-play 打断唱歌/触发 privacy gate | §5.5（auto-play 前过忙态+galgame 活跃判定）+ §10 test_anime_playback_policy |
| P1-8 闭包联网检索阻塞 turn 无超时 | §6（source_timeout/resolve_budget）+ §5.2/§5.3 + §7 RESOLVE_TIMEOUT |
| P1-9 生命周期三洞 | §5.4（yt-dlp 子进程/重启按慢下/对账不 auto-play） |
| P1-10 错码 + 缺六行 | §7（改码 + 补行） |
| P1-11 「放吧」参数漂移无兜底 | §5.5（directive 内嵌规范化标题 + 最近完成未播指针 + episode optional） |
| P1-12 Phase 3 合入即半成品可触发 | §3.2/§6（enabled 默认 false）+ §11（Phase 4 后翻 true） |
| P2-13 resolver golden 缺形态 | §5.1 + §10 test_anime_resolver 清单 |
| P2-14 config 缺键 + cookie 落盘形态 | §6（补键 + cookie 生命周期） |
| P2-15 available 谓词构造期时序 | §5.7（live-read + 容忍未加载不抛） |
| P2-16 测试缺 ui/供给/重试/对账 | §10（补齐）+ playback_policy 下沉便于测 |
| P2-17 Phase1 测试 vs Phase5 实现错位 | §11（对账纯逻辑归 Phase 1，接线归 Phase 5） |
| P2-18 qbt 未常驻 + yt-dlp 版本策略 | §12（systemctl enable + 版本策略） |
| P2-19 进度事件通道说宽 | §2 铁律#2（进度 UI 内部 Qt signal，不跨边界）+ §5.3 RSS-only |
| P2-20 cancel/status 未按类别 scope | §3.1 torrent_client（强制 category 过滤） |
| P2-21 探针样本脱敏 | §11 Phase 0 |

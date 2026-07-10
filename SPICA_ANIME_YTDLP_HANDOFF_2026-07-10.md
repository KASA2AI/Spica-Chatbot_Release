# Spica Anime Watch Bilibili 下载重连修复交接报告

## 基本信息

- 日期：2026-07-10
- 仓库：`/home/san/ai_code/Spica-Chatbot`
- 状态：实现与验证已完成，尚未提交
- 原始症状：anime-watch 使用 B 站源时，第一次下载长期极慢；关闭并重启 Spica 后，再请求同一集通常会很快完成。
- 目标剧集：`无职转生第三季第一集`
- 实时解析 locator：`BV1oAM36EE9p:1`

## 结论

根因不是找源失败，而是首次 yt-dlp 提取到的 B 站 CDN/PCDN 连接可能持续低速或出现短期网络错误。旧 worker 只启动一次 yt-dlp，没有机器可读进度、低速检测或外层 extractor 重启；重启 Spica 后会重新提取下载 URL，并利用 `.part` 续传，因此表现为第二次突然恢复。

当前实现会在同一个 Spica worker 生命周期中重新启动整个 yt-dlp extractor，无需重启应用：

- 默认低速阈值：`512 KiB/s`
- 连续低速窗口：`15 秒`
- 最多自动重连：`2 次`
- 第三次仍低速：不再终止，显示降级状态并继续下载
- 第三次遇到可重试网络错误并退出：正式失败
- 低速与网络错误共享同一个两次重连预算

## 深度 review 后续修复（同日）

本轮针对重连实现的深度 review 又修复了四个边界问题：

- Windows 非 UTF-8 codepage：正式 argv 现在固定为
  `python -m yt_dlp --ignore-config --encoding utf-8 ...`，父进程继续按 UTF-8
  解码。真实 yt-dlp `2026.07.04` 子进程在 CP936 stdio 下验证过中文路径字节流，
  不再因 `after_move:filepath` 变成 `�` 而误报不可信路径。
- Windows/DNS 网络错误：typed classifier 新增 `WinError 10053/10054/10060/
  10061/11001`、`getaddrinfo failed`、`Unable to download JSON metadata`、
  `Unable to download API page` 和 `IncompleteRead`。这些测试使用 `--no-warnings`
  下仍可见的 fatal `ERROR:` 形态，不依赖会被抑制的 optional warning。
- 损坏进度：带 `SPICA:` 前缀但 JSON/字段损坏的记录会重置低速窗口；普通日志
  不重置。负值、非有限值和错误字段类型视为损坏，合法的未知总大小
  `total_bytes=None` 以及 `total=0` 保留。
- 取消交接：attempt 前已可见取消时不 Popen；取消落在不可中断 Popen 内时，
  Popen 返回后由 worker 注册、终止并 reap。Popen 与 terminate/wait/kill 全部在
  `_proc_lock` 外；这里只保证返回后的接管，不宣称 Popen 与取消完全原子。

### Standards 提交门禁收口

- UTF-8 正式 argv 测试不再直接调用 `_ytdlp_argv()`；现在通过
  `AnimeDownloadWorker.execute()` 注入 Popen，并从真实调用边界捕获 argv。
- kill timeout 测试不再 monkeypatch `_start_process_reaper`；受控顽固进程在
  第一次 kill 后继续存活，实际 daemon reaper 再次 kill 并成功 wait，测试只观察
  lifecycle error、kill/wait/reap、worker 返回和 `_proc` 所有权释放。
- controller 降级清理测试不再读 `_degraded_requests`；现在通过 worker
  signals、可见 status、`in_flight_state()` 与 host closures 验证合法完成路径。
- controller shutdown 测试不再动态替换 FakeWorker 的
  `cancel/wait/force_kill`，而是通过 `AnimeController` 启动真实
  `AnimeDownloadWorker`，只在 Popen 系统边界注入 terminate-resistant 受控进程，
  观察最终 terminate/kill/reap 和 QThread 退出。
- **任务级 waiver（用户明确批准）：**现有确定性 yt-dlp 生命周期测试
  保留对 `_run_ytdlp_attempt()` 的调用，并在所有权交接断言中读取 `_proc`。
  这是本任务的显式例外，不把该私有 seam 升格为公开 API，也不形成 repo-wide
  先例；若不带该 waiver，不应声称这些测试严格符合公共 seam 规则。
- `Duplicated Code` 仍只作为判断性 smell 记录；本轮没有重构稳定的生产清理路径。

## 已实现内容

### yt-dlp CLI contract

位置：`ui/workers/anime_worker.py`

- `--ignore-config` 紧跟 `python -m yt_dlp`
- 紧接 `--encoding utf-8`，与父进程 UTF-8 pipe 解码形成明确协议
- 显式启用 `--progress`
- JSON 进度模板包含 `format_id`，缺失时允许 JSON `null`
- `--progress-delta 1`
- `--socket-timeout` 使用 `AnimeConfig.source_timeout_seconds`
- `--retries 1`，由外层重连补充整体容错
- 显式 `--part --continue`
- 保持固定 argv、`shell=False`、输出路径约束和最终媒体路径复验

### 连续低速策略

位置：`spica/anime/download_health.py`

- Qt-free 独立策略模块
- 按相邻 `downloaded_bytes / monotonic 时间差` 计算每个区间速率
- 只有连续低速累计达到 15 秒才触发
- 任一区间恢复到阈值即清零连续低速时长
- `format_id`、`tmpfilename` 变化时重置
- `status=finished`、字节回退、时间回退、坏数据、新 attempt 时重置
- 损坏的 `SPICA:` 记录重置，普通非进度日志不破坏连续低速语义
- ffmpeg 合并阶段不参与低速判断

### 外层重连与错误分类

位置：`ui/workers/anime_worker.py`

- 内部 typed failure kind，不从翻译后的 UI 文案反推错误类型
- 会员、登录、视频不可用、本地磁盘/合并错误均为终止错误
- timeout、连接中断、DNS、HTTP `403/404/408/416/425/429/5xx`、TLS unexpected EOF 等为可重试网络错误
- Windows socket errno、`getaddrinfo failed`、fatal JSON/API 下载错误和
  `IncompleteRead` 同样进入共享的外层重连预算
- 已覆盖 yt-dlp 真实文案：
  - `premium members only`
  - `may be deleted or geo-restricted`
- 总耗时从第一次 attempt 前开始计算，避免重连后被误判为“快速下载”并自动播放

### stdout 与进程生命周期

位置：`ui/workers/anime_worker.py`

- stdout 只能由一个 reader thread 读取
- reader 在 `finally` 投递 EOF sentinel
- 正常结果必须同时满足“进程退出 + EOF sentinel”
- 主循环只消费 queue，不直接 drain pipe
- 主动终止流程：`terminate -> bounded wait -> kill -> bounded wait`
- pipe close 在独立 daemon closer 中执行，避免 `TextIOWrapper.close()` 等 reader 内部锁而卡死主线程
- reader cleanup 使用单一总 deadline，不让多次 join 重复累加预算
- kill 后仍未 reap 的进程移交独立 daemon reaper，避免 worker `deleteLater()` 后失去 owner
- 取消发生在重连信号边界时不会启动下一进程
- attempt 开始前已知取消不 Popen；Popen 中取消在句柄返回后完成所有权交接和
  有界清理，锁内只读写取消状态/进程所有权

### Controller 与 typed config

位置：

- `ui/controllers/anime_controller.py`
- `spica/config/schema.py`
- `data/config/app.yaml`

内容：

- 新增 `AnimeConfig.ytdlp_min_rate_kib_per_second: float = 512.0`
- 配置为 `0` 可关闭低速自动重连
- controller 注入现有 `source_timeout_seconds` 和新阈值
- 独立 `reconnecting(request_id, used, maximum, reason)` signal
- 独立 `degraded(request_id)` signal
- UI 文案使用“当前连接过慢/中断，正在重新连接 1/2”，不承诺“换节点”
- 重连与降级状态不触发主动播报；原 `stalled` 主动播报行为保持不变
- 降级状态不会被后续普通 progress 立即覆盖
- controller shutdown 默认总等待预算已覆盖 worker 最坏清理时间

## 本任务涉及文件

生产代码：

- `spica/anime/download_health.py`（新增）
- `ui/workers/anime_worker.py`
- `ui/controllers/anime_controller.py`
- `spica/config/schema.py`（仅 AnimeConfig 局部）
- `data/config/app.yaml`（仅 anime 模板注释局部）

测试：

- `tests/test_anime_download_health.py`（新增）
- `tests/test_anime_worker.py`
- `tests/test_anime_controller.py`
- `tests/test_anime_config.py`

未修改 anime resolver、Bilibili source adapter、qBittorrent adapter、AppHost、assembly、媒体播放器或 yt-dlp 安装包。

深度 review 的功能边界修复轮只追加修改：

- `ui/workers/anime_worker.py`
- `tests/test_anime_worker.py`
- 本交接书

最终 Standards 收口轮只追加修改：

- `tests/test_anime_controller.py`
- 本交接书

该收口轮没有修改生产代码、配置或 download-health 策略。

## 验证结果

### 深度 review 新增精确定向测试

```bash
python -m pytest tests/test_anime_worker.py -k "cli_contract_forces_utf8_output or utf8_output_survives_non_utf8_child_stdio or windows_socket_errors_are_retryable or network_failure_restarts_extractor or terminal_failures_win_over_network or corrupt_progress_resets or regular_log_keeps or unknown_or_zero_total_keeps or attempt_cancelled_before_start or cancel_during_popen or process_wait_does_not_hold or kill_timeout_returns_lifecycle_error" -q
```

结果：`39 passed, 57 deselected`

覆盖真实 yt-dlp/CP936 中文字节流、正式 argv、Windows/DNS/API/IncompleteRead
外层重连、终止分类优先级、损坏进度 reset、普通日志/未知总大小控制，以及取消
发生在 attempt 前和 Popen 内的两条生命周期保证。

### Standards controller 边界精确定向测试

```bash
python -m pytest tests/test_anime_controller.py::test_degraded_status_survives_progress_then_ready_clears_visible_state tests/test_anime_controller.py::test_shutdown_force_kills_and_reaps_terminate_resistant_extractor -q
```

结果：`2 passed`

覆盖：降级进度在合法 ready 序列后恢复公开完成状态；controller 通过真实
worker 及 Popen 边界受控进程，对 terminate-resistant extractor 执行
kill/reap 并等待 QThread 退出。

### 确定性低速主回归

```bash
python -m pytest tests/test_anime_worker.py::test_ytdlp_low_speed_reconnects_then_second_attempt_succeeds -q
```

结果：`1 passed`

覆盖：首进程持续 `256 KiB/s`，达到窗口后被终止；第二进程使用相同 argv 和 `.part` 续传成功；只产生一个成功结果；总耗时从 attempt 1 开始。

### Anime 相关测试

```bash
python -m pytest tests/test_anime_download_health.py tests/test_anime_worker.py tests/test_anime_controller.py tests/test_anime_config.py tests/test_anime_assembly_persistence.py tests/test_watch_anime_tool.py tests/test_watch_anime_supply.py -q
```

结果：`217 passed`

### 架构守卫

```bash
python -m pytest tests/test_layering.py tests/test_no_getenv.py tests/test_resolved_config_equivalence.py -q
```

结果：`56 passed, 15 subtests passed`

### 最新全量测试

```bash
python -m pytest tests -q
```

结果：`1906 passed, 177 subtests passed, 1 warning`

唯一 warning 是既有 librosa `importlib-resources` deprecation warning，与本任务无关。

额外检查：

- `git diff --check`：通过
- 修改模块 `compileall`：通过
- 深度 review 后续的最终两轴 code review：见本轮收尾结果（固定点
  `b6fc2d9be718d1f066ac6ee37d1ab2d467215206`，task-scoped working-tree diff）

### 此前真实 B 站 smoke（深度 review 后续未重跑）

使用正式 `AnimeDownloadWorker`、现有 cookie 文件路径和默认重连参数，下载到 `/tmp`，不写正式媒体库、不启动播放器。

结果：

- locator：`BV1oAM36EE9p:1`
- 同一 worker 生命周期内自动网络重连：`1/2`、`2/2`
- 第三次 attempt 成功
- 输出大小：`170,823,969 bytes`
- 总耗时：`42.84 秒`
- error：`None`
- 未重启 Spica/worker 所在进程
- smoke 临时目录已清理

这次真实 smoke 实际遇到了两次可重试网络错误，因此确认“重新执行 B 站 extractor + 续传”在真实源上有效。

### 真实 subprocess 生命周期复核

- 正常 parent 退出、孙进程持 stdout `1.5s`：worker 等到真实 EOF，耗时 `1.52s`，不截断尾部
- cancel 后孙进程继续持 stdout `7s`：worker 在 `0.57s` 有界返回，符合单一 `0.5s` reader cleanup budget
- controller 默认关停总等待 `3s`，可覆盖上述 worker 清理路径

## 配置快照说明

施工前基线：`/tmp/anime-ytdlp-before.json`

本任务有意新增的 resolved leaf 只有：

```text
app.anime.ytdlp_min_rate_kib_per_second = 512.0 (default)
```

快照中同时出现的 `app.tts.enabled` 和 `SPICA_SCREEN_ENABLED` env 状态变化
不属于本 anime 修复；它们已进入固定点 commit
`b6fc2d9be718d1f066ac6ee37d1ab2d467215206`，不是当前未提交工作区的混合 WIP。

## 工作区保护说明

当前工作区不是干净状态。之前的 TTS/STT/看屏/song/self-check 改动已进入
`b6fc2d9be718d1f066ac6ee37d1ab2d467215206`，不再是当前未提交 WIP。

相对当前固定点，以下原混合文件现只剩 anime 任务改动：

- `spica/config/schema.py`
- `data/config/app.yaml`

工作区还有与 anime 任务无关的未跟踪文件
`spica-architecture-review-2026-07-10.md`；本任务未修改它，也不应将它混入后续提交。

后续 staging 时不要使用无差别的 `git add -A`。应逐文件、逐 hunk 检查，只提交目标范围；在提交前再次运行 `$code-review`。

本任务未创建 commit，也未 stage 文件。commit list：无（本任务仍是未提交 WIP）。

## 残余风险

- 未建立独立 process group。极端情况下，如果 yt-dlp parent 已退出、异常孙进程
  永久继承并持有 stdout，自然完成路径会永久等不到 EOF，可能永久占住 anime
  controller 的 single-flight；后续下载请求会因已有 active worker 被拒绝/丢弃。
- 本轮没有加入 EOF timeout：那会截断合法尾部，破坏当前“进程退出 + EOF”完成
  contract，也无法可靠区分正常尾部与异常继承 writer。
- 实际低速检测发生在下载阶段，此时通常尚未进入 ffmpeg 合并，因此该风险不阻断本修复。
- 若未来要求强制清理整个进程树，应单独设计跨 Linux/Windows 的 process-group/job-object adapter，不应在本 bugfix 中临时扩域。

## 建议后续动作

1. 在提交前检查 task-scoped diff，避免混入无关的未跟踪架构报告。
2. 提交前再次检查本报告记录的 task-scoped working-tree diff；当前仍未 stage。
3. 若需要完整 UI 验收，可启动 overlay 后请求同一集，观察状态文案和最终播放/播报策略；核心真实下载链已 smoke 通过。
4. 不需要再次修改 resolver、Bilibili source 或 qBittorrent 路径，除非出现新的独立证据。

## Suggested Skills

- `$code-review`：提交前审查 task-scoped working-tree diff，隔离无关文件。
- `$tdd`：若继续调整阈值、预算、错误分类或进程生命周期，先补行为红测。
- `$diagnosing-bugs`：若真机仍出现首轮慢速或未重连，采集真实 yt-dlp JSON progress/tail 后再定位。
- `$handoff`：若下一会话仍未提交，更新本报告中的测试结果和工作区状态。

## 下一会话建议提示词

```text
请先阅读 SPICA_ANIME_YTDLP_HANDOFF_2026-07-10.md 和仓库 AGENTS.md。
本次 anime B站低速重连修复已经实现并通过全量/真实 smoke，但本任务仍是未提交 WIP。
请只审查并整理 anime task-scoped diff；不要修改或混入未跟踪的 `spica-architecture-review-2026-07-10.md`。如需提交，先展示 staged diff 和测试结果，未经明确授权不要 commit。
```

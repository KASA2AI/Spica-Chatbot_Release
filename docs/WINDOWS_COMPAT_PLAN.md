# Spica Windows 兼容分段计划（W 系列）

## Metadata

- **Version**: v2.1
- **Date**: 2026-07-04
- **Supersedes**: v1 / v2（同日；均未曾入库，本文原地替换。v2 = v1 + 红队对抗报告全部 findings 收编，见下方对照表；v2.1 = v2 + 终审三条小修，见对照表末尾 F1–F3）
- **Status**: W0 侦察 complete（read-only，无代码改动、无 commit）；红队对抗审查 complete（read-only）；**W1 已批准（2026-07-05 审批窗口：HEAD 仍 `9acfbe6`、基线复测 1256 passed/1 warning/177 subtests 一致、全部代码锚点复校准零漂移；A1/A3/A8 裁决见 §0）；W2-a 已批准（2026-07-05 审批窗口：HEAD `c10133d`、基线 1293 passed/1 warning/177 subtests；裁决四条：①check_imports REQUIRED 全绿为硬 gate、PREFLIGHT 仅 WARN 留痕（PyAudio 属 PREFLIGHT）②`AppHost.initialize()` deferred 到 W2，W2-a 只做 import 闭包 smoke ③`docs/windows_bootstrap.md` 须回填真实 Windows smoke 留痕后才算 complete ④onnxruntime CPU 包在清单显式列出，不依赖 rapidocr 传递拉取）；W2 及以后各段均未批准**
- **基线**: HEAD `9acfbe6`，`python -m pytest tests -q` = **1256 passed, 1 warning, 177 subtests**（v1 制定窗口实测；红队窗口 read-only 未复核，按此采信）
- **Scope**: 让 Spica 在 Windows 上可运行，且有一个 typed config 开关（`platform.os`）切换 Linux/Windows 装配；主开发机保持 Linux，Windows 机只作验收靶机；**Linux 行为字节级不变**（开关默认 auto、auto 在 Linux 折算 linux 时，全量测试与 resolved config 零波动——沿用 OO 迁移的等价纪律）。
- **Non-goals**（全部明确排除，不在任何 W 段内实现）:
  - 独占全屏窗口采集（E3：沿用「窗口化/无边框窗口化」隐私边界，独占全屏另立项）；
  - per-port 平台覆盖、platform 的 env override（E6：只做 `platform.os` 一个 typed key；单 port 切换以后另立项）；
  - ReSpeaker 作为 Windows 默认输入（E4：通用麦优先，ReSpeaker 降为 W3b 可选真机项）；
  - Windows 机上提交代码（E5：Windows 只 pull + smoke）；
  - Windows 全量 pytest 承诺（E5：第一阶段只 smoke，全量策略 W5 定案）；
  - 开始菜单/.lnk/注册表扫描式游戏发现（E2：后置）；
  - 非 NVIDIA GPU 支持（E1：CUDA/TRT 与 Linux 一致）。
- **重要声明**：本文是计划，不代表已实现架构。file:line 引用是计划制定/红队复核时（HEAD `9acfbe6`）的阅读锚点，**每段开工前须重新校准**。本文不声称任何测试已被执行（基线复核除外）；所有测试 gate 均为应执行命令 `python -m pytest tests -q`（**绝不裸 `pytest`**，会递归扫 vendored GPT-SoVITS 崩溃）。**每个 W 段的实施授权只能来自审批窗口的裁决；本文不自我批准任何段，不含占位 commit hash。**
- 形制参照：`docs/oo_migration/MIGRATION_PLAN.md`。注：OO 迁移 v2 的 Non-goals 明文排除 Windows——本 W 系列是**独立新立项**，不占用 OO 计划的任何双轨时钟。

### v2 变更记录（红队 findings 收编对照）

| Finding | 内容 | v2 落点 |
|---|---|---|
| P1-1 | L3 原地重构撞 `tests/test_companion_bridge.py:111-114` 钉测，gate (a) 必破 | 裁决=方案 (a)：旧函数原名原签名保留，新函数另名只换调用点 → §5-W1 内容 6 重写 |
| P1-2 | Windows 基础 env bootstrap 无归属段 | 新增 **W2-a** 独立可批小步 → §5-W2-a |
| P1-3 | W3 白名单缺注入链两文件；effective_os 无持久居所 | 新增 §3.6 居所裁决（AgentServices 字段，W1 白名单补 `spica/runtime/services.py`）；W3 白名单补 `spica/host/app_host.py` + `ui/qt_overlay.py` |
| P2-1 | gate (d) 只钉单屏，多屏路径无守卫 | gate (d) 扩为单屏 + 合成双屏（横排/纵排各一）dpr=1 共三组 golden → §5-W1 gate |
| P2-2 | fold 对未知宿主未定义 | §3.2 明文 auto+未知 → fold 内 raise；Layer B 加 `("auto","darwin")` raises 钉 → §3.4 |
| P2-3 | 通用麦 fatal 错误分类不在契约里（无麦重试风暴） | W3-a 契约加 FATAL markers 条款 → §4.3 |
| P2-4 | A3 备选 B 与 W1 白名单矛盾 | A3 推荐裁 A；裁 B 则 W1 白名单须补 app_host.py → §0-A3 |
| P2-5 | L5 引入 TMPDIR/TEMP/TMP 隐性 env 影响 | §5-W1 内容 7 加实施注记（基线前确认 gettempdir()==/tmp；env_roster 注释性记录）+ 风险 R10 |
| P3-1 | Windows launcher 照抄 shlex.split 吃反斜杠 | W2 规格明写：`command` 整串传 Popen，绝不 POSIX shlex → §5-W2 内容 2 |
| P3-2 | webrtcvad Windows wheel 覆盖不稳 | W3-a 选型前置「实测装得上再定」→ §4.3 / A4 |
| P3-3 | windows 分支 rebase 冲突频率被低估 | §7 明文「整体重建为默认动作，不解冲突」 |
| P3-4 | E4 主链到 W3 只闭环 dummy TTS | 已声明的降级，维持原文（W3 smoke 明写、W4 真值复验） |
| P3-5 | W2 期 screen 节 moondream/cuda 噪声 | W2 验收配置 commit 加 `screen.enabled: false` → §5-W2 |
| P3-6 | W2 smoke 首项的日志无来源 | W1 内容加「选线日志行」→ §5-W1 内容 3 |
| P3-7 | 命名债（RESPEAKER_END_SILENCE_SECONDS 调通用麦、ReSpeaker* 异常名） | §4.2 留档为已知命名债，功能可行不改名 |
| F1（v2.1 终审） | `AgentServices.effective_platform` 无默认值会打红大量直接构造该 dataclass 的既有测试（如 `test_turn_contract.py:209`），破 W1 gate (a) | 明确 `effective_platform: str = "linux"` 带默认 + 理由 → §0-A8 / §3.6 |
| F2（v2.1 终审） | 「UI 只传原始 int」表述有歧义，可能诱导改 provider 签名致 int 扩散过 controller/ocr_loop/privacy_gate | L2 精确化：provider 契约与下游签名全部零改动，int 只作 adapter 方法入参 → §2.1-L2 / §5-W1 内容 5 |
| F3（v2.1 终审） | gate (d) 列举三个构型却写「共两组」，照文施工会漏测一个方向 | 全文统一为「共三组」（gate (d) / R2 / §9） |

---

## Current Approval State

> 状态取值：`ready for approval` / `ready after dependency` / `not approved` / `optional`；`complete` 为收口后回填。
> 批准纪律：**一次只施工一个段，逐段批准，禁止打包批。**

| 段 | 名称 | 前置 | 机器 | 状态 |
|---|---|---|---|---|
| W0 | 侦察（read-only） | — | Linux | **complete**（无代码改动，无 commit） |
| W1 | platform 开关 + 三工厂 + Linux 可测 seam 债（L1–L5）+ Windows stub adapter + L3/L4 几何新函数 | W0 | 全 Linux，落 main | **approved**（2026-07-05 审批窗口：A1=agent_assembly.py 居所、A3=方案 A port 方法、A8=`AgentServices.effective_platform: str = "linux"`；白名单/gate 按 §5-W1 原文零修订通过；锚点复校准零漂移） |
| W2-a | Windows 基础 env bootstrap（requirements 清单 + import 冒烟） | W1 | 清单产出 Linux；装机+冒烟 Windows | **approved**（2026-07-05 审批窗口：HEAD `c10133d`、基线 1293 passed/1 warning/177 subtests；裁决见 Metadata Status 行；complete 以 `docs/windows_bootstrap.md` 回填真实 Windows 留痕为准） |
| W2 | Windows 真 WindowLocator + 原生 launcher + L3/L4 真机验收 | W1, W2-a | 实现/单测 Linux（落 main），验收 Windows（windows 分支） | ready after dependency |
| W3 | 入口 `.ps1`/`.bat` + 通用麦采集缝 + 软件断句 + WMF 播放验证 | W1（缝可与 W2 并行实现，验收需 W2-a 的 Windows 环境就位） | 实现 Linux，验收 Windows | ready after dependency |
| W3b | ReSpeaker Windows 真机（可选） | W3 | Windows 真机 | optional（不阻塞主线） |
| W4 | 重型运行时（TTS/RVC/Moondream/RapidOCR CUDA+TRT/faster-whisper CUDA） | W2, W3 | 几乎全 Windows+GPU | not approved |
| W5 | docs 收口 + Windows 全量 pytest 策略定案 + windows 分支合流定案 | W2–W4 | Linux | not approved |

---

## 0. 需求理解 / 影响范围 / 推荐落点 / 不碰边界 / 测试计划（总览）

**需求理解**：项目已是 ports/adapters 形制，三个平台 port（window_locator / screen_capture / game_launcher）接口面平台中立（`spica/ports/window_locator.py:28,80,86`；`spica/ports/screen_capture.py:37-38`；`spica/ports/game_launcher.py:5-7` 甚至明文预留了 windows 分支）。Windows 兼容的正路 = **第二套 adapter + 装配期选线 + 一个 typed config 开关**，不是 fork 代码、不是到处 `if os.name`。W0 侦察证实真正的泄漏只有少数几处且集中（§2 L1–L9）。

**影响范围**（全系列合计，逐段白名单见 §5）：`spica/config/schema.py`（+PlatformConfig）、`spica/host/agent_assembly.py`（fold + 三工厂 + 选线日志）、`spica/runtime/services.py`（effective_platform 字段，§3.6）、`data/config/app.yaml`（注释模板行）、新增 `spica/adapters/window_locator/windows_win32.py` + `spica/adapters/game_launcher/windows_native.py`、`ui/qt_overlay.py`（:336/:401/:415 三处 seam；W3 期 :162/:274-275 接线邻域）、`ui/controllers/galgame_controller.py`（新几何函数）、`spica/config/runtime_env.py`（:21）、`spica/config/env_roster.py`（仅注释记录）、`spica/host/app_host.py`（W3 recorder 构造/持有）、`hardware/`（采集缝注入）、`ui/controllers/voice_input_controller.py`（:163 注入点）、`requirements-windows-base.txt` + Windows 入口脚本、W4 的两处 OCR 预载文件、docs。

**推荐落点**：开关走 guardrails §7 配置模板（typed schema、yaml-only）；工厂走 §10 adapter 模板 + `build_ocr_adapter` 先例（`spica/host/agent_assembly.py:69` 定义、`:205-207` 使用、`tests/test_build_ocr_adapter.py` 测试形制）；音频缝走注入（`SpeechWorker` 已有 `stt_port` 注入先例，`hardware/respeaker/speech_worker.py:33-44`）。

**不碰的边界**（全系列恒定）：`run_turn` / orchestrator / `ChatEngine` / `prompt_builder` / registry 机制 / MemoryPort / 守卫测试（不删不放宽）/ **既有非守卫测试同样不删不改（除非某段白名单显式列入并说明）** / 不新起 `spica/platform/` 平行树 / 不大范围改名搬目录 / port 面不加 hwnd/xid 等平台字段 / `spica/` 不 import Qt / 业务码不 `os.getenv`。

**测试计划**：每段 gate 见 §5；Linux 守卫测试对照表见 §9；Windows 侧唯一 gate 是逐段 smoke 清单（§6）。

**待审批窗口裁决的点**（本文只给推荐，不定案；**A1/A3/A8 已于 2026-07-05 W1 审批窗口定案**，A2/A4/A5/A6/A7 归各自段（W2/W3/W4）的审批窗口，本轮明确未裁）：
- **A1【已裁决 2026-07-05：ACCEPT 推荐案——`agent_assembly.py` 内】** fold/工厂居所：`agent_assembly.py` 内（与 `build_ocr_adapter` 全套先例同居所——定义 `:69`、装配点使用 `:205-207`、测试形制 `tests/test_build_ocr_adapter.py`；且该文件本就因 L1 在 W1 白名单内，`agent_assembly.py` 不在 guardrails §3.1 极高危清单）vs 新增 `spica/host/platform_select.py`（拒：为一个纯函数 + 三个工厂另起新居所，徒增 import 面与「第二个选线居所」的发现成本，违背铁律 #7「走现有风格」）；
- **A2** Win32 API 绑定方式：ctypes 直调 user32（推荐，零新依赖）vs pywin32；
- **A3【已裁决 2026-07-05：ACCEPT 方案 A——port 方法，精确形态照 §5-W1 内容 5（F2）：provider 与下游签名零改动，native int 只作 adapter 方法入参】** overlay id 格式化下沉的形态：**推荐裁 A**——port 加平台中立方法 `format_native_window_id(native: int) -> str`（附契约测试；全仓对三 port 零 `isinstance` 调用、runtime_checkable Protocol 加方法不炸任何现有 fake——红队复核确认）。备选 B（host 装配期注入 formatter 闭包给 UI）**必须**同步把 `spica/host/app_host.py` 列入 W1 白名单（P2-4），且 A 已有 UI 直达先例（`ui/qt_overlay.py:413` 读 `host.services.window_locator_adapter`）；
- **A4** 软件断句选型：webrtcvad 帧级 VAD + 尾静音模型（推荐，**前置条件：W3-a 内先实测 Windows py3.11 wheel 装得上**——py-webrtcvad 是 C 扩展，wheel 覆盖不稳，可能需 webrtcvad-wheels fork，P3-2）vs RMS 能量阈值（零依赖备选）vs push-to-talk（UI 兜底，任何 VAD 方案的失败回退）；
- **A5** W3 是否新增 typed `stt.mic_backend`（`auto|respeaker|generic`，yaml-only）供显式覆盖，还是纯按 platform 折算（推荐先加：与 SttConfig 同域、零 env）；
- **A6** Windows 入口脚本落点：`scripts/windows/run_spica.ps1|.bat`（推荐）vs 仓库根；
- **A7** W4 预载 DLL 变体的触发条件：先验证「conda PATH + ORT 自身 DLL 发现」是否足够，不足才写 `*/bin/*.dll` + `os.add_dll_directory` 变体（推荐顺序）；
- **A8【已裁决 2026-07-05：ACCEPT 推荐案——`AgentServices.effective_platform: str = "linux"` 带默认值（F1 硬要求，审批窗口实测 24 个测试文件直接构造 `AgentServices(...)`）】** effective_os 持久居所（P1-3 的 W1 半边，**W1 批准时必须一并裁定，否则 W3 必越权**）：**推荐 `AgentServices` 新字段 `effective_platform: str = "linux"`（必须带默认值，理由见 §3.6——F1）**（`spica/runtime/services.py:14`；fold 在 `build_agent_services` 内一次、字段随 services 全局可达，UI/host 消费走 `host.services` 既有先例，且避免 W1 触碰 §3.1 极高危的 `app_host.py`）。备选：AppHost 属性（fold 上提到 `AppHost.initialize()`，W1 白名单改补 `app_host.py`）。

---

## 1. 固定前提（用户已拍板，只安排怎么做，不再讨论要不要）

| # | 裁决 |
|---|---|
| E1 | Windows 用 conda env；TRT EP 与所有 CUDA 推理与 Linux 一致（W4 后置到 Windows+GPU 真机） |
| E2 | Windows v1 以 `manual_bind` 为主 + 手填 exe 为次级；开始菜单/.lnk 扫描后置。**不消费 `LaunchResult.pid`** 做 crash 检测，沿用窗口枚举/绑定判断游戏存在 |
| E3 | 沿用当前隐私边界：只支持「窗口化/无边框窗口化」的可见窗口采集；独占全屏 = non-goal |
| E4 | Windows v1 优先「通用麦克风」路径保证「说话→STT→回答→TTS」主链可验；ReSpeaker 降为 W3b 可选真机项（若做：接受 Zadig/WinUSB + libusb backend + 6ch WASAPI 真机 gate）。注：主链的 TTS 环在 W3 允许 dummy/预生成 wav 闭环，真嗓在 W4 复验（P3-4，已声明的分步降级） |
| E5 | Windows 机第一阶段只 pull + smoke，不在 Windows 提交；xiaosan.env 不入库、Windows 本机手配；不承诺 Windows 全量 pytest（W5 定案）；单独建 windows 分支 |
| E6 | 只做一个 typed config：`platform.os = auto \| linux \| windows`，默认 auto，Linux 零波动；不做 per-port override、不加 env override |

红队最小修正清单（P1-1/P2-2/P2-4/P2-1/P1-3-W1 半边/P2-5/P1-2）已全部内建到 §3/§5 对应段落；W3 白名单修订（P1-3 的 W3 半边）已在 §5-W3 落实。

---

## 2. W0 侦察结论摘要（证据锚点，开工前复核）

### 2.1 泄漏清单（L1–L9）

| # | 位置 | 内容 | 修复归属 |
|---|---|---|---|
| L1 | `spica/host/agent_assembly.py:197-204` | `LinuxDesktopGameLauncher()` / `LinuxX11WindowLocator()` / `MssScreenCapture()` 硬编码构造，无选线工厂 | **W1**（核心项） |
| L2 | `ui/qt_overlay.py:336` | `hex(int(self.winId()))` 在 UI 层现造 X11 hex 形态 overlay id，喂 `linux_x11.py:126` 焦点豁免 | **W1** 机制（格式化下沉 adapter：provider 仍返回 `str \| None`，native int 只作 adapter 方法入参——精确形态见 §5-W1 内容 5，F2；Linux 字节等价）；W2 验 HWND 行为 |
| L3 | `ui/controllers/galgame_controller.py:44-52` | `selection_to_physical_rect` 均匀 dpr 且不减多屏原点（注释自承 KNOWN LIMITATION）——**语义变化不是纯搬运**。旧语义被 `tests/test_companion_bridge.py:111-114` 钉死（含 dpr=2.0 与 0→1 回退用例） | **W1** 新函数另名 + 换调用点（P1-1 裁决 (a)，见 §5-W1 内容 6）；**W2** 真机 per-monitor DPI 验收 |
| L4 | `ui/qt_overlay.py:415` | wmctrl 物理坐标直接喂 `QGuiApplication.screenAt`（期望逻辑坐标） | 同 L3（W1 重构 / W2 验收） |
| L5 | `spica/config/runtime_env.py:21` | `DEFAULT_RUNTIME_CACHE_ROOT = Path("/tmp/spica_chatbot_cache")` 硬编码 POSIX /tmp | **W1**（改 `tempfile.gettempdir()` 基；Linux 解析值不变——**有条件**：TMPDIR 未设时，见 P2-5 注记 §5-W1 内容 7；Layer A `--diff` 自证——`runtime_env.py:25-28` 的 resolve 函数与 Layer A 共享） |
| L6 | `spica/galgame/binding.py:40-43` | X11 专属 reason_code（`WMCTRL_MISSING`/`WAYLAND_UNSUPPORTED`）映射进 domain。**已核实降级安全**：`binding.py:82` 未知 code 走 `.get(..., ["retry", "manual_bind", "cancel"])` 默认 | **W2**（仅加 Windows code 的可选条目，不动结构） |
| L7 | `linux_x11.py:186-192` → 持久化 | `app_id` 承载 WM_CLASS 值进 `WindowMatchRule`；`window_match.py:57` 仅 +0.2 tiebreak，降级安全 | **W2** 记录不修（Windows adapter 填自己的 app_id 语义即可） |
| L8 | `spica/local_runtime/tts/model_imports.py:32-49` + `tts/driver.py:74,93-100` | pushd/sys.path 残留 glue（vendored 根因）；热路径 cwd-free 结论仅 Linux 验证过；vendored text frontend 有 `os.name=="nt"` 分支 | **W4** |
| L9 | 文案级 | `hardware/respeaker/control.py:106-110` udev 建议、`hardware/respeaker/audio.py:245` / `webui_qt.py:27` conda 路径文案 | **W3/W5** 顺带 |

### 2.2 语义变化（需按 E 前提安排的设计点）

- 启动：Bottles→原生。`launch_type` 白名单（`spica/galgame/models.py:95`：`desktop_entry|command|exe|manual_bind`）中 `desktop_entry` 是 Linux-only（`linux_desktop.py:109-115` 扫 `.desktop`）；E2 裁决 Windows v1 = `manual_bind` 主 + `exe` 次，`desktop_entry` 在 Windows 明确返回不支持。`LaunchProfile.platform` 字段已存在（`models.py:94`）但现 adapter 从不 branch。**不消费 pid**（E2；`ports/game_launcher.py:37` 的 pid 本就无消费方，rg 零命中——红队复核）。**Windows `command` 解析注意**：`linux_desktop.py:104-106` 用 POSIX `shlex.split`，照抄会吃掉 `C:\game\a.exe` 的反斜杠（P3-1，规格见 §5-W2）。
- 窗口探针：wmctrl/xprop EWMH（`linux_x11.py:77,98,142,152,163`）→ Win32 `EnumWindows/GetWindowRect/GetForegroundWindow/IsIconic`；X11 XID hex 比较（`linux_x11.py:44-51`）→ HWND。上层判定设计（标题关键词主判据 `window_match.py:46-49`、遮挡=纯矩形相交+黑帧启发 `privacy_gate.py:85-93` + `ocr_region.py:60-80`）**平台无关，全部保留**。
- 输入法：ibus→Windows TSF 原生，无需存在（`webui_qt.py:57-59` 已 `sys.platform` 守卫）。ALSA 同理（`webui_qt.py:93-94`）。
- DPI 体制：X11 dpr=1 常态 → Windows per-monitor DPI（L3/L4 因此升级为必修）。**注**：X11 全 dpr=1 时（含多屏）logical==physical，新旧几何数学恒等——「零 diff 与修多屏 bug 互斥」已被红队证伪；真正的约束是旧语义有钉测（P1-1）+ 多屏等价须有 golden（P2-1）。
- mss：跨平台（Windows 走 GDI），`spica/adapters/screen_capture/mss_visible_window.py` **不需要新 adapter**；Wayland 黑帧兜底分支在 Windows 成为无害死代码。

### 2.3 重型环境（W4 专属）

- `agent_tools/function_tools/screen/backends/rapidocr.py:81-112`：in-process CUDA 预载 glob `*/lib/*.so*`（:107）+ `ctypes.CDLL(RTLD_GLOBAL)`（:112）——Windows 上 glob 天然空 → 已有 best-effort 静默跳过；
- `spica/local_runtime/ocr/rapidocr_trt_runtime.py:50,57,87`：显式预载 `libnvinfer.so.10` 等 **Linux .so 名**——Windows 唯一「非字面一致」处；`classify_load_status`（:107 起）的诚实回退链（det/rec 必须真 trt 才算 OK）必须保留；
- GPT-SoVITS vendored（nt 分支转正）、RVC 独立 env、Moondream CUDA 硬绑（`spica/local_runtime/vision/moondream_hf.py:48-52,60-64`，fail-loud 成 ToolError 信封不崩 turn）、faster-whisper CUDA（`spica/adapters/stt/faster_whisper.py:43`）。

### 2.4 干净负结果（防重查；标 ✚ 者为红队复核新增）

- 全仓 posix-only API（os.fork/fcntl/setsid/preexec_fn/signal/pwd/grp/resource/termios）**实测零命中**；路径处理压倒性 pathlib，入口不依赖 cwd；
- UI 层唯一平台泄漏就是 L2 一行：无 X11BypassWindowManager、无 X11 shape 直调（点击穿透是 `setMask(QRegion)`，`ui/qt_overlay.py:1217-1239`）、无托盘/全局热键/字体路径；
- Wayland portal/pipewire/grim 全仓零使用——「portal 授权无 Windows 等价」的担忧不适用；
- 播放全走 Qt Multimedia（`ui/controllers/audio_controller.py:16,114-122,181-210`）、录音全走 PyAudio/PortAudio（`hardware/respeaker/audio.py:240-247`），皆跨平台栈；PyAudio/pyusb 皆惰性加载（`audio.py:240-247`、`control.py` 构造期 importlib）→ 模块导入干净 ✚；
- xiaosan.env / app.yaml 解析全 pathlib 仓库根锚定（`spica/config/secrets.py:19-25`、`manager.py:108-111`），可移植；`qt_overlay.py:1419` main 首句 `load_secrets()`（铁律 #10）在位；
- `webui_qt.py` 三个 Linux helper（:10-11 xcb、:57-59 IM、:93-94 ALSA）已 `sys.platform != "linux"` 短路，Windows 直通 `qt_main()`；
- GameProfile 持久化为不透明 JSON blob 且 `from_dict` 忽略未知 key（`spica/adapters/game_memory/sqlite.py:190`、`models.py:62-64`）——加 Windows 字段零 migration；
- 无守卫测试钉死 agent_assembly 的具体 adapter 构造；tests 中仅 `test_moondream_default_cutover.py:106` 引用 `build_agent_services` 且是 mock 掉 ✚——三工厂替换 L1 不碰守卫；
- **platform 节不需要动 manager.py** ✚：`ConfigManager.load()` = merge 后整体 `AppConfig.model_validate`（`manager.py:97-104,217-219`）；`test_resolved_config_equivalence.py:181` 在加入默认 auto 的 PlatformConfig 后两侧同默认，保持绿；
- roster meta-pin（`test_resolved_config_equivalence.py:257-282`）只扫 manager/secrets/runtime_env 三文件的带引号大写名，W1 无新名 → 天然绿 ✚（但注意：它**扫不到 stdlib 内部 env 读取**，L5 的 gettempdir 属「绕过」而非「满足」，见 P2-5 注记）；
- 全仓对三平台 port 零 `isinstance` 调用 → A3-A 给 runtime_checkable Protocol 加方法不炸任何现有 fake ✚；
- `AppHost.initialize()` 在缺 GPU/TTS/faster-whisper 时能走完 ✚：warmup 全 best-effort（`spica/host/warmup.py:15-30` STT 预热失败只报不阻塞）+ `FasterWhisperAdapter` 构造零负载（`faster_whisper.py:59-68` 双检锁懒加载）——W2 smoke 的存活前提是基础 import 栈在（→ W2-a）；
- §3.5 干净 import 测试真能拦 windll ✚：模块级 `ctypes.windll` / `from ctypes import windll` 在 Linux import 即炸 → 测试必红，机制覆盖 W2 真实现；
- dummy TTS adapter 存在（`agent_tools/tts/adapters/dummy.py`）→ W3 smoke 前提在 ✚；
- 「全注释解析为 {}」推论对「只加注释行」仍成立（app.yaml 现已有 live 节，注释行不改解析）✚。

---

## 3. 平台开关设计（W1 核心）

### 3.1 schema

`spica/config/schema.py` 的 `AppConfig`（:354-370）新增：

```python
class PlatformConfig(BaseModel):
    # yaml-only（铁律 #4：不进 env_roster，roster meta-pin 天然绿——
    # tests/test_resolved_config_equivalence.py:257 不需要任何改动）。
    # Literal 使 typo 启动即炸（仿 GalgameConfig.reaction_mode 先例 schema.py:95）。
    os: Literal["auto", "linux", "windows"] = "auto"
```

`data/config/app.yaml` 只加注释模板行（全注释行不改解析，红队复核确认）：

```yaml
# platform:
#   os: auto        # auto | linux | windows（auto = 装配期按 sys.platform 折算）
```

### 3.2 fold 纯函数 + 装配期 resolve-once

```python
def fold_platform(os_cfg: str, host_platform: str) -> str:
    """折算生效平台。纯函数，无 sys 读取，Layer B 可注入双向钉。
    - os_cfg 为显式 "linux"/"windows" -> 原样返回（不看 host_platform；
      这也是未知宿主上的唯一逃生口）。
    - os_cfg == "auto"：host_platform=="linux" -> "linux"；
      host_platform=="win32" -> "windows"；
      其余（darwin/cygwin/msys/...）-> **raise（fail loud）**——绝不静默折算，
      防止 mac/cygwin 上拿到 wmctrl 路径的静默坏行为（P2-2）。
    - os_cfg 非法值在 schema Literal 层已炸，此处不重复防御。"""
```

- **`ConfigManager.load()` 全程保留 `"auto"`，绝不折算**——否则 `tests/test_resolved_config_equivalence.py:181` 的 `load() == AppConfig()` 等式在含 platform 节的默认对象上会红。folding 只发生在装配期一次（`build_agent_services` 内调用 `fold_platform(config.platform.os, sys.platform)`）——resolve-once + 注入，仿 screen/song 先例。
- 居所：`agent_assembly.py`（A1 已裁决 2026-07-05：agent_assembly.py）。`sys.platform` 不是 env，不触铁律 #4；读取点收敛为装配期这一处——**W3 及以后的消费者一律读 §3.6 的持久居所，禁止二次读 `sys.platform`**。

### 3.3 三工厂

仿 `build_ocr_adapter`（`agent_assembly.py:69` + `tests/test_build_ocr_adapter.py`）：

```python
def build_window_locator(effective_os: str) -> WindowLocatorPort: ...
def build_screen_capture(effective_os: str) -> ScreenCapturePort: ...
def build_game_launcher(effective_os: str) -> GameLauncherPort: ...
```

- linux 分支返回今天的三个类**字节等价**（`LinuxX11WindowLocator` / `MssScreenCapture` / `LinuxDesktopGameLauncher`），替换 `agent_assembly.py:197-204` 的硬编码构造（L1）；
- windows 分支：W1 返回 stub（见 §5-W1），W2 切真类；`screen_capture` 两分支同为 `MssScreenCapture`（mss 跨平台，不新写 adapter，W2 只验收）；
- 未知/非法 effective_os：fail loud（区别于 build_ocr_adapter 的降级——平台选错不该静默回退，标注在工厂 docstring）；
- **选线日志行**（P3-6）：装配处以 INFO 记一行 `platform resolved: os_cfg=%s host=%s effective=%s lanes=window_locator/%s screen_capture/%s game_launcher/%s`——W2 smoke 首项的判据来源。

### 3.4 守卫落点（Layer A / Layer B / 工厂单测三层）

- **Layer B**（`tests/test_resolved_config_equivalence.py`，机器无关、入库）：只用**注入平台值**钉——
  `fold_platform("auto","linux") == "linux"`；`fold_platform("auto","win32") == "windows"`；
  `fold_platform("windows","linux") == "windows"`（显式值不看宿主）；
  **`fold_platform("auto","darwin") raises`（P2-2 新增钉）**；
  非法 os_cfg 由 schema Literal 层的 fail-loud 测试盖住。
  **绝不读真机 `sys.platform`**。「auto→本机」的断言只放 Layer A（per-machine、gitignored）或工厂单测的 monkeypatch。
- **Layer A**（`scripts/dump_resolved_config.py`）：改前 `--out` 留基线；改后 `--diff` **只允许出现新增 `platform` 节**（`dump_resolved_config.py:90` dump 的是 `app_config.model_dump()`，新节呈现为 ADDED 行——红队复核确认；这是唯一允许的加法 diff，PR 说明后重建基线）。**基线操作前置检查见 §5-W1 内容 7（P2-5）**。
- **工厂单测**（新 `tests/test_build_platform_adapters.py`，仿 `test_build_ocr_adapter.py:16-19` 的 default-zero-diff 钉）：`build_*("linux")` 逐一 `assertIsInstance` 今天的三个类；`build_*("windows")` 返回 windows lane；非法值 raise。**「Layer A 零 diff」是必要不充分——工厂单测才是「Linux 仍构造 Linux adapter」的真守卫**，两者同为 W1 退出条件。

### 3.5 Windows adapter import 纪律

- Windows adapter 模块**必须在 Linux 干净 import**：win32 API（ctypes user32 调用）只在方法内懒加载，或工厂在分支内懒 import 具体类；模块级 `ctypes.windll` 在 Linux import 即炸，守卫可拦（红队复核确认机制有效）；
- 新增守卫测试：「Windows 模块在 Linux 干净 import + 实例化 + `enumerate_windows()` 返回优雅 reason_code」（import 本身即是检查）；
- 这保证 Linux 全量测试面覆盖 windows lane 的可 import 性，W2 后的真实现同样受此约束。

### 3.6 effective_os 持久居所（A8——已随 W1 批准裁定 2026-07-05：AgentServices 字段案；P1-3）

- **推荐**：`AgentServices` 新字段 **`effective_platform: str = "linux"`**（`spica/runtime/services.py:14`）。fold 在 `build_agent_services` 内执行一次后写入该字段；AppHost 持有 services，UI 侧消费走 `host.services.effective_platform`（先例：`ui/qt_overlay.py:413` 已直读 `host.services.window_locator_adapter`）。理由：与 fold/工厂同居所、注入路径既有、**W1 无需触碰 §3.1 极高危的 `app_host.py`**。
- **默认值是硬要求（F1）**：15+ 测试文件直接构造 `AgentServices(...)` 并依赖尾部字段默认（如 `tests/test_turn_contract.py:209`、`tests/test_streaming_pipeline.py:148`、`tests/test_pipeline_smoke.py:147`）——无默认值的新字段会整片打红既有测试，直接破 W1 gate (a)。默认 `"linux"` 是 **legacy/测试构造默认**，与该 dataclass 既有「tests/legacy callers leave it None」尾部字段惯例同构（`services.py` 各尾部字段注释）；选 `"linux"` 而非 `None` 是为了消费方免 None 分支、且与现全仓行为等价。**生产路径 `build_agent_services` 总是写入 fold 后的真实值，绝不依赖默认**；新字段追加在 dataclass 末尾（尾部已全为默认字段，排序合法）。
- 备选：AppHost 属性（fold 上提到 `AppHost.initialize()`）——若审批改裁此项，W1 白名单以 `spica/host/app_host.py` 替换 `spica/runtime/services.py`。
- 消费纪律：W3 的 recorder 选择器、以及未来任何平台判定，一律读此居所；**全仓 `sys.platform` 生产读取点保持唯一**（`webui_qt.py` 三个已守卫 helper 除外，它们在 config 层之前运行）。

---

## 4. 音频采集缝设计（W3 核心）

### 4.1 现状（file:line）

- 录音唯一路径：`SpeechWorker.run()` 硬 import 调用 `record_respeaker_channel0_hardware_vad(should_stop=…, on_speech_start=…, end_silence_seconds=…)`（`hardware/respeaker/speech_worker.py:5-10,56-60`）；
- 断句 = ReSpeaker **硬件 VAD**（`Tuning.is_voice()`）+ 尾静音模型（`audio.py:25` `DEFAULT_END_SILENCE_SECONDS=0.9`，env 覆盖 `RESPEAKER_END_SILENCE_SECONDS` 已在 roster）；
- 流参数：6 声道 16k int16 开流、抽 channel 0（`audio.py:14,250-265`）；设备自动匹配要求 ≥6 通道 + 关键词（`audio.py:289`）——**通用麦没有硬件 VAD、没有 6ch**，这不是换实现是换语义；
- STT 契约：`SpeechToTextPort.transcribe(pcm, *, sample_rate=16000)` 消费「单段 VAD 切好的 16-bit mono PCM」（`spica/ports/stt.py:25-31`）——**缝只要产出同构 PCM 段，STT 及其下游零改动**；
- 注入先例已在：`SpeechWorker(self, stt_port=…)`（`speech_worker.py:33-44`；构造点 `ui/controllers/voice_input_controller.py:163`）；**host→UI 接线先例**：`ui/qt_overlay.py:274-275` `set_stt_port(self.host.stt_adapter)`——recorder 走同型接线（P1-3）。

### 4.2 缝落点（不往 hardware/respeaker 塞 if）

- **recorder 注入**：`SpeechWorker` 增加 `recorder=None` 构造参数（同 `stt_port` 形制），`run()` 调注入的 recorder；默认（None）保持现 import 路径 → **Linux 字节等价**；
- **recorder 契约**（与 `record_respeaker_channel0_hardware_vad` 同构，`speech_worker.py:56-60` 调用面零改动）：`(should_stop, on_speech_start, end_silence_seconds) -> bytes`（16k mono int16；空 bytes/异常语义沿用现 `ReSpeakerNoSpeechError`/`ReSpeakerRecordingCancelled` 家族，通用 backend 抛同名/同构异常）；
- **通用麦 backend**：新 `hardware/audio_input/generic_mic.py`（或 `hardware/microphone.py`，施工时定）：PyAudio 默认输入设备、16k mono int16 开流 + 软件断句（§4.3）；
- **接线链**（P1-3 修订）：`AppHost`（构造并持有 recorder，按 §3.6 的 `effective_platform` +（A5 若批准）`stt.mic_backend` 选 backend）→ `ui/qt_overlay.py` 接线（:274-275 邻域，同 `set_stt_port` 形制）→ `voice_input_controller.py:163` → `SpeechWorker`；Linux 默认 respeaker、Windows 默认 generic；
- ReSpeaker 现函数不动，作为可选 backend 保留（W3b 真机 gate 后 Windows 也可选它）；
- **已知命名债（P3-7，留档不改名）**：`RESPEAKER_END_SILENCE_SECONDS` 将顺带调通用麦的尾静音；generic backend 抛 `ReSpeaker*` 名异常——功能可行，是刻意选择（调用面零改动优先）；改名属大范围重命名，另立项。

### 4.3 软件断句（W3 单列设计小步 W3-a，A4 待裁决）

现端点判定绑硬件 VAD，通用麦没有——这是 W3 主要工作量：

| 方案 | 依赖 | 评估 |
|---|---|---|
| **webrtcvad 帧级 VAD + 尾静音模型**（推荐） | 新增 py-webrtcvad（C 扩展） | 10/20/30ms 帧、16k mono int16 正好是现流格式；「speech 起始→`on_speech_start`；连续静音 ≥ `end_silence_seconds` → 收段」可 1:1 复刻硬件 VAD 循环语义。**前置：Windows py3.11 wheel 实测装得上再定**（P3-2；不行则评估 webrtcvad-wheels fork，仍不行落备选） |
| RMS 能量阈值 | 零新依赖 | 阈值对环境噪声敏感，需真机调参；作为 webrtcvad 不可用时的备选 |
| push-to-talk | 零依赖（UI 键） | 确定性最高；**作为任何 VAD 方案的失败回退**保留在设计里，不作默认 |

W3-a 产出（全部过审批后才进 W3 主体施工）：
1. 选型裁决（含 wheel 实测结果留痕）；
2. 断句参数与 `end_silence_seconds` 语义对齐说明；
3. 注入式单测方案（合成 PCM 帧序列 → 断句边界断言，不依赖真麦克风）；
4. **fatal 错误分类条款（P2-3）**：`hardware/respeaker/speech_worker.py:13-21` 的 `FATAL_SPEECH_ERROR_MARKERS` 是字符串匹配——generic backend 的「设备不可用」类错误**必须**复用现有 marker 字样或同步扩充 markers（`speech_worker.py` 在 W3 白名单内，合法可改），否则无麦时 `is_fatal_speech_error` 返回 False → 语音循环无限重试风暴。

---

## 5. 分段表

> 每段固定六栏：内容 / 机器 / 白名单 / gate 与退出条件 / 不碰边界 / 测试。白名单外的文件一律不动；实施中发现需越白名单 → 停下回审批窗口，不现场扩权。

### W0 侦察（complete）

read-only 完成：泄漏清单 L1–L9、负结果、开关/分段草案（本文 §2 收编）。无代码改动、无 commit。红队对抗审查亦 read-only 完成，findings 已收编（Metadata 对照表）。

### W1 platform 开关 + 三工厂 + seam 债（L1–L5）+ Windows stub（全 Linux，落 main）

- **内容**：
  1. `PlatformConfig` 进 schema（§3.1）+ app.yaml 注释模板行；
  2. `fold_platform` 纯函数（§3.2，含 auto+未知宿主 raise——P2-2）+ 装配期 resolve-once + **`AgentServices.effective_platform` 持久居所**（§3.6/A8）；
  3. 三工厂替换 `agent_assembly.py:197-204` 硬编码（L1，§3.3）+ **选线日志行**（P3-6）；
  4. Windows stub adapter：`windows_win32.py`（`enumerate_windows()` 返回 `ok=False, reason_code="WIN32_LOCATOR_PENDING"` 一类优雅值）、`windows_native.py`（`manual_bind` 直接可用——`linux_desktop.py:90-91` 已证平台中立；`exe`/`command` W1 返回 ok=False 待 W2；`desktop_entry` 永久返回不支持，E2）；
  5. L2 机制：overlay id 格式化下沉 adapter（A3 推荐 port 方法形态）。**精确形态（F2，防签名扩散）**：provider 契约 `Callable[[], str | None]`（`galgame_controller.py:77`）与 `check_safety(..., overlay_window_id: str | None)`（`ports/window_locator.py:85-86`）**全部零改动**；`qt_overlay.py:336` 的 lambda 改为调 `window_locator_adapter.format_native_window_id(int(self.winId()))` 后返回其 str 结果（UI 直达 `host.services.window_locator_adapter` 先例 :413）——**native int 只作为 adapter 方法入参存在，绝不传过 controller/ocr_loop/privacy_gate**。X11 lane 的 `format_native_window_id` 返回 `hex(native)` 同串 → 字节等价；
  6. **L3/L4 几何（P1-1 裁决 (a)）**：`selection_to_physical_rect` **原名原签名保留不动**——`tests/test_companion_bridge.py:111-114` 继续钉它，该测试文件**不入白名单、不修改**；新纯函数**另名**（施工时定名，如 `selection_to_physical_screen_rect`；语义：per-screen dpr + 多屏 origin 折算），**只换 `ui/qt_overlay.py:401` 调用点**；旧函数 docstring 标注 deprecated（删除另立后续小步，不在 W 系列内）。`:415` 的 `screenAt` 修复同批：physical→screen 匹配改为按各屏物理几何比对的新纯函数。两个新函数配注入式（合成 dpr/origin）单测；
  7. L5：`runtime_env.py:21` 改 `tempfile.gettempdir()` 基。**实施注记（P2-5）**：`gettempdir()` 读 `TMPDIR/TEMP/TMP`——stdlib 内部 env 读取，roster meta-pin（只扫带引号大写名）**扫不到**，属「绕过」而非「满足」，须在 `spica/config/env_roster.py` 加注释性记录；**dump 基线前先跑 `python -c "import tempfile;print(tempfile.gettempdir())"` 确认输出 `/tmp`**（TMPDIR 被设的启动环境会让 cache_root 漂移 → Layer A 出现白名单外 diff → 强制回滚，见 R10）；
  8. 守卫三层（§3.4，含 darwin raises 钉）+「Windows 模块 Linux 干净 import」测试（§3.5）。
- **机器**：全 Linux，全部落 main。
- **白名单**：`spica/config/schema.py`；`data/config/app.yaml`（仅注释行）；`spica/host/agent_assembly.py`；`spica/runtime/services.py`（仅 `effective_platform` 字段——A8 推荐案；若审批改裁 AppHost 属性则以 `spica/host/app_host.py` 替换本行）；新建 `spica/adapters/window_locator/windows_win32.py`、`spica/adapters/game_launcher/windows_native.py`（+ 各自 `__init__.py` re-export）；`spica/ports/window_locator.py`（A3 裁 A 时：仅新增方法不改既有签名）+ `spica/adapters/window_locator/linux_x11.py`（format 方法归所）；`ui/qt_overlay.py`（:336 / :401 / :415 三处）；`ui/controllers/galgame_controller.py`（新增函数；**:44-52 旧函数除 docstring 外不动**）；`spica/config/runtime_env.py`（:21）；`spica/config/env_roster.py`（仅注释记录，零名册变化）；`tests/` 新增（工厂/fold/几何/import 干净）与 `tests/test_resolved_config_equivalence.py`（只加不改）。**`tests/test_companion_bridge.py` 明确不在白名单内。**
- **gate 与退出条件**（全部满足才算收口）：
  (a) `python -m pytest tests -q` 全绿且既有 1256 项零波动（含 `test_companion_bridge.py:111-114` 原样通过）；
  (b) `scripts/dump_resolved_config.py --diff` **仅**新增 `platform` 节（前置：gettempdir()==/tmp 确认留痕；PR 说明后重建基线）；
  (c) 新工厂单测绿：`build_*("linux")` 构造今天的三个类（zero-diff 钉）+ fold 四枚注入钉（含 darwin raises）；
  (d) 几何 golden：**单屏 dpr=1、合成双屏横排 dpr=1、合成双屏纵排 dpr=1，共三组**（P2-1/F3），新函数输出与旧语义逐字节相等（X11 全 dpr=1 时数学恒等，零真机依赖）；
  (e) 守卫测试零改动（`test_layering`/`test_no_getenv`/`test_env_centralization`/roster meta-pin 天然绿）；
  (f) 线性小 commit，`git revert` 干净可回。
- **不碰边界**：三 port 的现有方法签名（A3 只加不改）；`linux_x11.py` 探针逻辑；`mss_visible_window.py`；`manager.py`（platform 无 env、validate 整体走 model_validate 无需改——红队复核确认）；env_roster 名册（零新增名）；`tests/test_companion_bridge.py`；全部 §0「不碰的边界」恒定项。
- **测试**：§9 表 W1 行 + 收尾全量。

### W2-a Windows 基础 env bootstrap（P1-2，独立可批小步）

- **内容**：
  1. 产出 `requirements-windows-base.txt`（或 environment.yml，施工时定）：以 `AppHost.initialize()` + UI 启动的**真实 import 闭包**为准盘点（至少 PySide6、openai、httpx、pydantic、yaml、python-dotenv、numpy、Pillow、mss、rapidocr_onnxruntime+onnxruntime、faster-whisper 及其依赖——**以实测 import 报错清单迭代，不凭记忆定版**）；现有 `requirements-rvc.txt`/`requirements-screen.txt`/`requirements-stt.txt` 只覆盖专项，无主清单（红队 ls 复核）；
  2. import 冒烟脚本 `scripts/windows/check_imports.py`：逐包 import + 版本打印，任何失败非零退出；
  3. Windows 机 conda env 创建 + 安装留痕（命令与版本记录进验收日志——E5：Windows 不产生 commit，留痕文档在 Linux 侧提交）。
- **机器**：清单产出与脚本在 Linux（落 main）；装机 + 冒烟在 Windows。
- **白名单**：新建 `requirements-windows-base.txt`、`scripts/windows/check_imports.py`、验收留痕文档（docs/ 或收口日志）。
- **gate 与退出条件**：(a) Linux 全量零波动（纯新增文件）；(b) Windows 机 `python scripts/windows/check_imports.py` 全绿留痕。
- **不碰边界**：一切生产代码。
- **测试**：无新 Linux 测试；收尾全量确认零波动。

### W2 Windows 真 WindowLocator + 原生 launcher + L3/L4 真机验收

- **内容**：
  1. `WindowsWin32WindowLocator` 真实现（A2 推荐 ctypes user32）：`EnumWindows`+`GetWindowTextW`+`IsWindowVisible`（枚举，`window_id=str(hwnd)` 十进制）、`GetWindowRect`（几何，物理像素）、`GetForegroundWindow`（焦点）、`IsIconic`（最小化）、`GetWindowThreadProcessId`（pid 供候选展示）；`check_safety` 与 X11 同构：geometry gone→`WINDOW_GONE`、Iconic→minimized、前台窗口标题关键词 + overlay id（HWND int，经 W1 下沉的格式化）豁免。**manual_bind-first 仍依赖枚举+遮挡+焦点，此范围不可缩水**；
  2. `WindowsNativeGameLauncher` 真实现：`exe` 直启 `Popen`（fire-and-forget，同 `linux_desktop.py:41,96` 形制，不消费 pid——E2）；**`command` 类型整串传 `Popen`（Windows CreateProcess 原生解析命令行），绝不照抄 `linux_desktop.py:104-106` 的 POSIX `shlex.split`——会吃掉 `C:\game\a.exe` 的反斜杠（P3-1）**；`manual_bind` 已可用；`desktop_entry` 明确不支持；
  3. `binding.py:40-43` 加 Windows reason_code 可选条目（未知 code 已安全降级 `binding.py:82`，此项仅为更好的 UI 提示）；
  4. L3/L4 真机验收：per-monitor DPI（100%/150% 混合）+ 多屏下框选→OCR 区域对齐；**mss 端到端 OCR 正确性 gated 在 L3/L4，两者同批验收**；
  5. windows 分支验收配置 commit：`ocr.provider: rapidocr`（CPU，schema 内置默认 `schema.py:349`；GPU EP 归 W4）+ **`screen.enabled: false`（P3-5：免 moondream_hf/cuda 在无 GPU 验证期的 ToolError 噪声——该路径 fail-loud 不崩 turn，关掉只为验收日志干净）** + `stt.device: cpu`/`compute_type: int8`（供 W3 沿用）。
- **机器**：实现+注入式单测（伪 Win32 调用层）在 Linux 落 main；行为验收在 Windows（windows 分支，§7）。
- **白名单**：`spica/adapters/window_locator/windows_win32.py`、`spica/adapters/game_launcher/windows_native.py`、`spica/host/agent_assembly.py`（windows 分支 stub→真类）、`spica/galgame/binding.py`（仅 `_UNAVAILABLE_OPTIONS` 新条目）、`tests/` 新增；windows 分支另有验收配置 commit（不回 main）。
- **gate 与退出条件**：(a) Linux 全量零波动；(b) Windows smoke W2 清单（§6.1）全勾；(c) DPI 混合场景验收记录（哪些倍率/屏数组合过了）写进收口日志。
- **不碰边界**：port 签名；linux_x11/mss adapter；`window_match.py`/`privacy_gate.py`（平台无关判定层）；`GalgameCompanionSession` 只经公共方法。
- **测试**：§9 表 W2 行 + 收尾全量（Linux）。

### W3 入口脚本 + 通用麦采集缝 + 软件断句 + WMF 播放验证

- **内容**：
  1. **W3-a（先行设计小步，单独过审批）**：软件断句选型裁决（§4.3，A4，含 webrtcvad wheel 实测）+ recorder 契约定稿（含 P2-3 fatal markers 条款）；
  2. 采集缝落地（§4.2）：`SpeechWorker` recorder 注入（默认 None=现路径，Linux 字节等价）、通用麦 backend（16k mono int16）、**host 侧选择器与接线（P1-3）**：`AppHost` 按 `services.effective_platform` +（A5 若批准）`stt.mic_backend` 构造并持有 recorder → `ui/qt_overlay.py`（:274-275 邻域，同 `set_stt_port` 形制）接线 → `voice_input_controller.py:163` 注入；A5 配置项按 guardrails §7 模板走（Layer A 基线流程同 W1）；
  3. Windows 入口 `scripts/windows/run_spica.ps1|.bat`（A6）：conda env python 路径参数化（E1）、无 ibus/无 ALSA（`webui_qt.py` 已守卫）、工作目录定位仓库根后 `python webui_qt.py`；`run_ibus.sh` 不动（Linux 专属启动器）；
  4. WMF 播放验证：`QMediaPlayer` 播 TTS 输出格式 wav（用预生成 wav / dummy TTS adapter——`agent_tools/tts/adapters/dummy.py` 已有——验证播放链；**真 TTS 合成归 W4**）+ song 用 mp3/wav 各一首验证；
  5. faster-whisper Windows 沿用 W2 验收配置的 `stt.device: cpu`/`int8`；CUDA 归 W4；
  6. L9 文案顺带：`audio.py:245`/`webui_qt.py:27` conda 文案去本机化。
- **机器**：缝+backend+断句单测（合成 PCM，mock PyAudio）在 Linux 落 main；断句真机调参 + 播放/回环验收在 Windows。
- **白名单**：`hardware/respeaker/speech_worker.py`（recorder 参数 + 必要时 FATAL markers 扩充）、新建 `hardware/audio_input/`（通用 backend + 断句）、`spica/host/app_host.py`（recorder 构造/持有——P1-3 修订）、`ui/qt_overlay.py`（:274-275 邻域接线——P1-3 修订）、`ui/controllers/voice_input_controller.py`（:163 注入点）、`spica/config/schema.py`（仅当 A5 批准 `stt.mic_backend`）、新建 `scripts/windows/`（入口脚本）、`hardware/respeaker/audio.py` + `webui_qt.py`（仅 L9 文案行）、`tests/` 新增。
- **gate 与退出条件**：(a) Linux 全量零波动（默认 recorder=None 路径字节等价）；(b) 断句单测（合成帧）绿；(c) Windows smoke W3 清单（§6.2）全勾；(d) 若 A5 批准配置项：Layer A `--diff` 仅新增该键 + Layer B 语义钉。
- **不碰边界**：`SpeechToTextPort`（`ports/stt.py`）零改动；`record_respeaker_channel0_hardware_vad` 本体零改动；run_turn/播放控制流。
- **测试**：§9 表 W3 行 + 收尾全量（Linux）。

### W3b（可选）ReSpeaker Windows 真机

- **内容**：Zadig/WinUSB 驱动绑定 + libusb backend（`control.py:104-105`）+ pyusb 设备发现 + **6ch WASAPI 开流真机 gate**（`audio.py:250-265`；WASAPI 可能只暴露 2ch 处理端点 → 6ch 打不开则本项失败关闭，不影响主线）；设备关键词对 WASAPI 命名的适配（`audio.py:289`）或 `RESPEAKER_INPUT_DEVICE_INDEX` 兜底。
- **机器**：Windows 真机 + ReSpeaker 硬件。
- **gate**：硬件 VAD 断句在 Windows 走通一次完整回环；失败则记录关闭，通用麦仍是 Windows 默认（E4）。
- **不碰边界**：通用麦路径；Linux ReSpeaker 路径。

### W4 重型运行时（Windows + GPU，conda）

- **内容**：
  1. **开工前环境探底 checklist**（先行小步）：conda env 装 torch-CUDA / ctranslate2 / onnxruntime-gpu / TensorRT 的版本矩阵探明并记录（E1 前提；基础栈已由 W2-a 就位）；
  2. OCR 预载 seam（唯一「非字面一致」处，A7 顺序）：`backends/rapidocr.py:107` 的 `*/lib/*.so*` glob 在 Windows 天然空→静默跳过（代码已 best-effort）；先验证 conda PATH + ORT 自身 DLL 发现是否足以拉起 CUDA EP，不足才加 `*/bin/*.dll` + `os.add_dll_directory` 变体；`rapidocr_trt_runtime.py:87` 的 `libnvinfer.so.10` 显式名 → Windows 等价 DLL 名或 no-op；**`classify_load_status` 诚实回退链保留**（det/rec 必须真 trt 才算 OK，否则如实回 CUDA/CPU）；
  3. GPT-SoVITS：vendored `os.name=="nt"` 分支转正验证（`tts/driver.py:93-100` 注释所指的 text frontend cwd 分支）；pushd/cwd（`model_imports.py:32-49`、`driver.py:74`）Windows 行为验证；L5 修后的 cache 落点验证；vendored 代码**原则不 patch**，确需小修须单独记录并保持可回滚；
  4. Moondream（CUDA 硬绑在 N 卡上即满足；验收配置把 `screen.enabled` 切回 true）、faster-whisper `device=cuda float16` 切回、RVC 独立 conda env Windows 等价（`rvc/driver.py:141-148` 的 `worker_python` 参数已支持指向独立 env）；
  5. 诊断脚本：`scripts/verify_watch_chain.py` / `scripts/diag_ocr_providers.py` 在 Windows 跑通。
- **机器**：几乎全 Windows+GPU；Linux 侧只允许预载 seam 小改（改动文件当次跑 OCR 相关测试 + 全量零波动）。
- **白名单**：`agent_tools/function_tools/screen/backends/rapidocr.py`（仅预载函数）、`spica/local_runtime/ocr/rapidocr_trt_runtime.py`（仅 preload 函数族）、windows 分支验收配置 commit（provider/device/screen.enabled 切回 GPU 值）；vendored 目录默认零改动。
- **gate 与退出条件**：(a) Linux 全量零波动；(b) Windows smoke W4 清单（§6.3）全勾；(c) TRT 若真机不可达：如实降级 CUDA EP 并记录（不算失败，回退链本就是设计——但须在收口日志写明 det/rec/cls 各自实际 EP）。
- **不碰边界**：`build_ocr_adapter` 选择逻辑；OCR 双路径共线设计（path A/B 同 provider）；vendored 大树。
- **测试**：§9 表 W4 行 + 收尾全量（Linux）。

### W5 docs 收口 + 定案

- **内容**：
  1. CLAUDE.md §2 架构地图加 platform 选线行、§0 状态行更新；
  2. `docs/DEVELOPMENT_GUARDRAILS.md` §5 决策树加「平台相关改动」分支、§7/§10 模板补 platform 注意项；`docs/FUTURE_FEATURE_PLAYBOOK.md` 新增「需求 21：新增平台支持/平台相关改动」条目；
  3. Windows 验收清单文档化（§6 收编成 docs 正式件）+ 各段收口日志汇总；
  4. **Windows 全量 pytest 策略定案**：`pytest.ini:3-5` testpaths 含 `hardware/respeaker` → 裁决「保证收集期可 import」vs「平台 collect_ignore」；POSIX 夹具清单（`tests/test_game_launcher_adapter.py:55,76-89` 的 /opt、`tests/test_tts_slim_manifest.py:223,261` 的 /home/san——均为喂 fake runner 的字符串，Windows 语义待逐条核）逐条定去留；已有 Windows 路径正测（`test_tts_slim_manifest.py:163` `D:\x`、`test_trt_per_stage_ep.py:44` `C:\models`）作为基础；
  5. windows 分支合流/退役定案（§7）；L9 剩余文案清理；`selection_to_physical_rect` deprecated 旧函数的删除是否立项（连同 `test_companion_bridge.py:111-114` 钉测迁移）在此一并裁决。
- **机器**：Linux。
- **gate**：文档与代码同 commit；全量零波动。

---

## 6. Windows smoke 清单（逐段可勾选；E5：这是 Windows 侧唯一 gate，必须逐条留痕）

### 6.0 W2-a smoke（环境 bootstrap）

- [ ] conda env 按 `requirements-windows-base.txt` 创建完成，命令与版本留痕；
- [ ] `python scripts/windows/check_imports.py`：**REQUIRED 全绿**；PREFLIGHT（PyAudio）失败仅 WARN 留痕，不阻塞收口；
- [ ] import 闭包 smoke：`python -c "from spica.host.app_host import AppHost; import ui.qt_overlay; print('ok')"`（`AppHost.initialize()` 是 W2 gate，不在本段）；
- [ ] PySide6 platform smoke：`QApplication([]).platformName()` 输出 `windows`；
- [ ] 以上输出原文回填 `docs/windows_bootstrap.md` §3。

### 6.1 W2 smoke（窗口/启动/采集几何）

- [ ] conda python 启动：`load_secrets()` 灌注 → `AppHost.initialize()` 完成，**选线日志行**（§3.3/P3-6）显示 effective platform = windows、三 lane 选中 windows；
- [ ] overlay 正常显示：透明 + 置顶 + 无边框 + 点击穿透（`setMask` 打洞区域可穿透，控件区可点）；
- [ ] 打开记事本（窗口化）→ 窗口枚举列出候选（含标题、hwnd）；
- [ ] `manual_bind` 绑定记事本 → 绑定成功事件到 UI；
- [ ] 手填 exe 路径启动一个程序（`exe` launch_type，路径含反斜杠与空格各测一次——P3-1 佐证）→ 窗口出现并可绑定；
- [ ] 框选 OCR 区域（100% DPI 单屏）→ OCR loop 出字（CPU provider）且区域对齐；
- [ ] **per-monitor DPI 验收**：150%（或本机实际倍率）+ 双屏各一次框选 → 区域对齐无偏移（L3/L4 gate）；
- [ ] 遮挡触发暂停：用另一窗口盖住绑定窗口 / Spica overlay 盖住 OCR 区域 → session 暂停并提示；焦点离开 → 同理；
- [ ] 最小化绑定窗口 → `WINDOW_MINIMIZED` 类暂停路径触发；
- [ ] 关闭绑定窗口 → `WINDOW_GONE` 路径触发。

### 6.2 W3 smoke（入口/语音回环——TTS 允许 dummy/预生成 wav）

- [ ] `scripts/windows/run_spica.ps1`（或 `.bat`）冷启动成功（参数化 conda python）；
- [ ] 通用麦克风：说一句中文 → 软件断句收段（尾静音 ≈ `end_silence_seconds` 语义）→ faster-whisper（cpu/int8 配置）转写正确；
- [ ] **无麦/禁用麦场景**：拔掉或禁用输入设备 → 报致命错误停止循环，**不进入无限重试**（P2-3 gate）；
- [ ] 转写文本进 `run_turn` → LLM 回答返回；
- [ ] 回答经 TTS 播放链播出（本段允许 dummy TTS / 预生成 wav 验证 `QMediaPlayer`+WMF；真嗓归 W4）；
- [ ] song 播放：一首 wav + 一首 mp3 经 song 播放器出声（WMF 编解码验证）；
- [ ] 中文输入法（TSF）在输入框可用（无 ibus 依赖佐证）；
- [ ] 打断：她说话时按停止 → 播放停止无 ghost 音频。

### 6.3 W4 smoke（重型 runtime，全链真值）

- [ ] `python scripts/diag_ocr_providers.py`：CUDA EP 拉起（TRT 若不可达：det/rec/cls 实际 EP 如实记录，回退链工作）；
- [ ] `python scripts/verify_watch_chain.py` 全链通过；
- [ ] GPT-SoVITS：合成一句真语音 wav 并播出（首句延迟记录）；
- [ ] faster-whisper `cuda/float16`：转写回环恢复 GPU；
- [ ] Moondream：`inspect_screen` 对真桌面出描述（`screen.enabled` 切回 true）；
- [ ] galgame 全链：绑真游戏（窗口化）→ OCR（GPU）→ 总结/吐槽至少各触发一次 → 记忆写回可查；
- [ ] RVC/sing_song（若独立 env 就绪）：点歌全链出声；未就绪则显式记录跳过；
- [ ] **完整语音回环真值版**：说话→STT(CUDA)→run_turn→GPT-SoVITS 真嗓→播出（W3 清单的真 TTS 复验）。

---

## 7. 分支与 git 工作流 + 回滚

- **main**：W1/W2-a 全部、W2/W3 的实现与单测（Linux 可 import、全量绿）、W5 文档——即**所有功能代码走 main 小步落**，每步全量绿；
- **windows 分支**：main 的验收快照 + **验收配置 commit**（Windows 机的 app.yaml 本机值：W2 期 `ocr.provider: rapidocr` + `screen.enabled: false` + `stt.device: cpu`；W4 期切回 GPU 值——在 **Linux 侧**提交到 windows 分支，永不回 main；解决「app.yaml 入库而 Windows 机需本机值」的 pull 冲突问题）+ 验收期热修（同样 Linux 侧提交）；
- **同步策略（P3-3）**：main 的 app.yaml 是活跃调参文件，验收配置 commit 也改它 → rebase 几乎必冲突。**默认动作 = 整体重建，不解冲突**：丢弃旧 windows 分支 → 从 main 最新点重开 → 重打验收配置 commit（该 commit 内容固定，重打成本≈零）。禁止在 windows 分支上积累与 main 的解冲突历史；
- **Windows 机**：只 `git pull` windows 分支 + 手配 xiaosan.env（不入库，E5）+ 跑 §6 smoke；**不产生任何 commit**；
- **回滚**：每段线性小 commit，`git revert` 干净可回；W1 revert 后须重建 Layer A 基线；windows 分支可整体丢弃重建，零合并债；
- **合流定案（W5）**：验收配置 commit 永久留在 windows 分支或转为 docs 里的「Windows 机配置说明」，分支退役与否 W5 裁决。

**必须停止并回滚的情形**（沿用 OO 计划纪律）：任一守卫测试变红且修复需要放宽守卫；**任一既有非守卫测试变红且修复需要修改该测试而其不在当段白名单**（P1-1 教训的一般化）；Layer A 出现白名单外的 diff；实施中发现需动「不碰边界」清单内文件；实施会话提出的「顺手改进」超出当段白名单。任一发生 → revert 本段全部 commit，回审批窗口重议段定义。

---

## 8. 开放风险表

| # | 风险 | 缓解 |
|---|---|---|
| R1 | windows 分支漂移/合并债 | 功能代码全走 main；分支只装验收配置+快照；**冲突默认整体重建不解冲突**（P3-3）；验收配置 commit 内容固定可重打 |
| R2 | per-monitor DPI 几何真机仍错位（L3/L4 是全仓最少既有测试覆盖的坐标路径） | W1 三组注入式 golden（单屏 + 双屏横排 + 双屏纵排，P2-1/F3）先行；W2 与 mss 端到端同批验收；失败回退方案 = 限定单屏+100% DPI 的验收口径（E3 窗口化约束下影响可控），并记录为已知限制 |
| R3 | webrtcvad Windows wheel 装不上 / 通用麦软件断句质量（噪声环境误切/漏切） | W3-a 前置 wheel 实测（P3-2，不行评估 webrtcvad-wheels fork）；RMS 备选；push-to-talk 兜底常备（A4）；尾静音参数真机调参 |
| R4 | WMF 编解码（song 的 mp3 链） | W3 smoke 显式覆盖 wav+mp3；失败则 song 侧转码为 wav 输出（song pipeline 内部已产 WAV，`app.yaml:136` `separator.output_format: WAV`、`:158` `rvc.export_format: WAV`） |
| R5 | GPT-SoVITS vendored 的 nt 分支质量未知 | W4 允许失败降级 dummy TTS 并如实记录；vendored 原则不 patch，小修单独记录可回滚 |
| R6 | Windows conda CUDA 版本矩阵（torch/ctranslate2/onnxruntime-gpu/TRT 互相牵制） | W2-a 先立基础栈；W4 开工前环境探底 checklist 先行，矩阵探明前不动代码 |
| R7 | pytest 在 Windows 收集期崩（`pytest.ini:3-5` testpaths 含 hardware/respeaker） | 第一阶段 Windows 根本不跑 pytest（E5，smoke 唯一 gate）；W5 定案 collect 策略 |
| R8 | 6ch WASAPI 开流失败（W3b） | W3b 是 optional，失败关闭不影响主线（E4） |
| R9 | 杀软/权限干扰（EnumWindows/截屏/首次网络） | 低风险；smoke 首项启动检查覆盖；出现即记录到验收日志 |
| R10 | L5 改 gettempdir() 后，设了 TMPDIR/TEMP/TMP 的启动环境使 `runtime_cache.cache_root` 漂移 → Layer A 白名单外 diff → 强制回滚（P2-5） | dump 基线前确认 `gettempdir()==/tmp` 并留痕；env_roster 注释性记录该 stdlib 隐性读取；`SPICA_RUNTIME_CACHE_DIR` 显式覆盖仍在 roster 可用 |

---

## 9. 附：每段该跑哪些 Linux 守卫测试（对照 guardrails §13）

| 段 | 轻量守卫先跑 | 再跑 | 收尾 |
|---|---|---|---|
| W1 | `test_resolved_config_equivalence`、`test_no_getenv`、`test_layering`、`test_env_centralization` | `test_config_manager`、新 `test_build_platform_adapters`（含 fold 四钉：linux/win32/显式值/darwin-raises）、新几何 golden（单屏 + 双屏横排 + 双屏纵排三组）、新「Windows 模块干净 import」测试、**`test_companion_bridge`（须原样绿——P1-1 哨兵）**、**既有 `AgentServices` 直接构造测试面须原样绿（F1 哨兵，如 `test_turn_contract`）**、`scripts/dump_resolved_config.py --diff` | `python -m pytest tests -q` |
| W2-a | —（纯新增文件） | — | `python -m pytest tests -q`（确认零波动） |
| W2 | `test_layering`、`test_no_getenv` | 新 Windows adapter 注入式单测、`test_game_binder`、`test_window_locator_match`、`test_galgame_session` | 同上 |
| W3 | `test_layering`、`test_no_getenv`（+若 A5：`test_resolved_config_equivalence` + `--diff`） | 新断句/recorder 单测（含 fatal markers 用例——P2-3）、`test_speech_worker_stt`、`test_stt_faster_whisper`、`test_respeaker_audio`、`test_companion_bridge` | 同上 |
| W4 | `test_no_getenv`、`test_layering` | `test_rapidocr_backend`、`test_rapidocr_lock`、`test_ocr_adapter`、`test_build_ocr_adapter`、`test_trt_per_stage_ep` | 同上 |
| W5 | 全部轻量守卫 | 文档所引测试名逐一存在性核对 | 同上 |

> 命令恒为 `python -m pytest tests -q`。Windows 侧不跑全量（E5），§6 smoke 清单即 Windows gate。

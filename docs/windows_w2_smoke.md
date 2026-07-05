# W2 Windows 真机 smoke 留痕(§6.1 验收记录)

> 归属:`docs/WINDOWS_COMPAT_PLAN.md` §5-W2 / §6.1。
> 裁决(2026-07-05 审批窗口,严格口径):**W2 状态 = partial / hardware-blocked,不标 complete。**
> Linux 实现 commit `5f6acc6` 有效;Windows 真机 smoke 9/10 PASS;多屏 + 混合 DPI 条目因硬件条件(单物理屏)未测,BLOCKED。
> 不阻塞 W3 审批/施工;**W4/W5 开工前必须补跑多屏/混合 DPI 验收,或另开审批窗口显式 waiver**(见 §8 挂账 W2-DPI-MULTISCREEN)。

## 1. 执行环境

- 执行日期:2026-07-05
- 机器:Windows 10 Enterprise LTSC 2021 `10.0.19044` / conda `25.11.1` / spica-win python `3.11.15`
- 屏幕拓扑:**单屏 4K(3840×2160)@ 200% 缩放**(per-monitor effective DPI 192)——本机仅一块物理屏
- 仓库:main HEAD `5f6acc6`(W2 实现);验收配置 commit `13a0cbe`(**windows 分支,不回 main**:`ocr.provider=rapidocr` / `screen.enabled=false` / `stt.device=cpu`,`compute_type=int8`)
- 纪律:Windows 只 pull + smoke,main 零 commit(E5);本文档由 Linux 侧规范化原始回传草稿后提交

## 2. §6.1 逐条结果(9 PASS / 1 BLOCKED)

| # | 条目 | 结果 | 证据摘要 |
|---|---|---|---|
| 1 | conda python 启动:load_secrets → AppHost.initialize() 完成,选线日志 | **PASS** | `platform resolved: os_cfg=auto host=win32 effective=windows lanes=window_locator/windows_win32 screen_capture/mss game_launcher/windows_native` |
| 2 | overlay 透明+置顶+无边框+点击穿透 | **PASS**(人工确认) | 穿透区/控件区行为正常 |
| 3 | 记事本(窗口化)→ 枚举列出候选(标题、hwnd) | **PASS** | 枚举 11 个可见带题窗口(中文标题正常);UI 候选列表含记事本 |
| 4 | manual_bind → 绑定成功事件到 UI | **PASS**(用 QQ 窗口) | 记事本 CJK 标题撞既有 `guess_game_id_from_title` 限制(见 §5,非 W2 回归);QQ(拉丁标题)全流程走通 |
| 5 | 手填 exe 启动(反斜杠+空格各一次)→ 窗口出现可绑定 | **PASS** | `C:\Windows\System32\notepad.exe` 与 junction 空格路径 `...\A B\notepad.exe`(`exe` 与 `command` 整串两式)均启动且窗口被枚举——P3-1 佐证;UI 级人工确认 |
| 6 | 框选 OCR(单屏)→ OCR loop 出字且区域对齐 | **PASS**(实测本机 200% DPI,严于清单的 100%) | UI 框选 QQ 文字区出字;几何零偏移自动化佐证见 §4 |
| 7 | **per-monitor DPI:150%(或本机实际倍率)+ 双屏各一次** | **BLOCKED(硬件)** | 拆分:「本机实际倍率」半条 = 单屏 200% **PASS**(§4);「双屏/混合 DPI」半条 = 本机单屏**无法执行**,挂账 W2-DPI-MULTISCREEN |
| 8 | 遮挡触发暂停(他窗盖住 / overlay 盖 OCR 区 / 焦点离开) | **PASS**(人工确认) | 暂停并提示 |
| 9 | 最小化 → WINDOW_MINIMIZED 暂停路径 | **PASS** | adapter smoke + UI 级均过 |
| 10 | 关闭 → WINDOW_GONE 路径 | **PASS** | 同上 |

## 3. adapter 级真机 smoke:16/16 PASS

真 Win32(无 fake)驱动 `WindowsWin32WindowLocator` + `WindowsNativeGameLauncher`:

- 枚举 11 个可见带题窗口(hwnd 十进制、pid、中文标题正常);
- exe 直启 notepad → pid 匹配枚举到的窗口;geometry 物理像素 `x=92 y=281 w=862 h=427`;
- 安全链五路径全部真机命中:无关前台→`WINDOW_NOT_FOCUSED`;前台标题 keyword 命中→ok;overlay 十进制 hwnd 豁免→ok;SW_MINIMIZE→`WINDOW_MINIMIZED`;WM_CLOSE→`WINDOW_GONE`;关闭后 geometry=None——与 Linux fake 契约测试(`tests/test_windows_adapters.py`)逐条对应;
- P3-1:junction 制造的空格+反斜杠路径,`exe`(list-of-one)与 `command`(整串 `'"C:\...\A B\notepad.exe"'`)均成功 CreateProcess 且窗口枚举到;
- manual_bind ok 零 spawn;desktop_entry 明确不支持。
- (方法注:裸复制 notepad.exe 到外部目录会因 MUI 资源不解析而静默退出——用目录 junction 指向 System32 制造空格路径,是场景搭建技巧,与 adapter 无关。)

### AppHost headless smoke

`load_secrets()` → `AppHost.initialize()` 完成;`services.effective_platform=windows`;三 adapter = `WindowsWin32WindowLocator` / `WindowsNativeGameLauncher` / `MssScreenCapture`;app 退出码 0。

## 4. L3/L4 几何验证(单屏 200% DPI):零偏移

tkinter 置顶窗口显示唯一 token → 生产链 `locator.get_window_geometry(物理px)` → `mss.grab(该 rect)` → `RapidOcrAdapter.recognize`(CPU):token `ALIGN8842` 逐字读回,**几何零偏移**(OCR 2.46s 含模型加载)。

这验证了 W2 实现 docstring 报备的前提:Qt6 生产进程 per-monitor-DPI-aware 下 `GetWindowRect` 返回物理像素,与 mss 坐标空间一致——**在单屏 200% 场景成立**。W1 几何 seam 无 bug,`ui/qt_overlay.py` 未动(停止条款未触发)。

## 5. BLOCKED 项与挂账边界(写精确,防补验跑偏)

**已验证**(本机可执行的全部):单屏高倍率 per-monitor 缩放(200%,严于清单示例 150%)下,枚举/几何/焦点/最小化/关闭/OCR 对齐全链成立。

**未验证**(硬件 BLOCKED,单物理屏无法执行):
1. **多屏拓扑**:虚拟桌面跨屏原点/负坐标下 `GetWindowRect` 与 mss 的坐标空间一致性;
2. **异构 per-monitor DPI**(混合缩放,如主屏 200% + 副屏 100%):窗口跨屏/落在非主屏时的几何换算。

**补验成本低**:任意第二块显示器(电视 HDMI 亦可)+ 主副屏不同缩放率即构成完整场景,预计 15 分钟(§6.1 条目 7 补跑 + 本文档回填)。**不要**用「单屏 150%」替代补验——单屏缩放已被 200% 覆盖,补验对象只是多屏拓扑与混合 DPI。

## 6. 既有缺口记录(非 W2 回归)

**CJK 窗口标题无法推断 game_id**:`guess_game_id_from_title`(`spica/galgame/companion_controller.py`,早于 W1/W2)只取标题开头拉丁段,纯 CJK 标题(如「无标题 - 记事本」、日文原版 galgame)返回空 → UI 报「纯非拉丁标题暂需后续支持」并复位,优雅拒绝不崩。**平台无关既有限制,Linux 同样触发**(已在 Linux 侧代码核实)。`GameBinder.resolve_selection` 已支持显式 game_id,缺的是 UI 输入口——**建议单独小步立项**(撞项目核心用例:日文 galgame),不归 W2。

## 7. 其他观察(均与计划 §2.4 预期一致)

- CPU OCR 节奏:`ocr_cycle_ms=718~1218 exceeded interval_ms=600` WARNING 间歇出现——串行「完成后等待」循环自我调节(设计行为,无重叠),galgame 秒级文本节奏可用;W4 GPU EP 后预计消失;
- TTS warmup 预期失败:`No module named 'torch'`(W4 重型未装),best-effort 只报不阻塞;
- faster-whisper cpu/int8 真机可用:模型加载 3907ms、warmup ok(W3 前置好消息);
- RapidOCR 无 CUDA 提示噪声:rapidocr_onnxruntime 1.4.4 行为,实际推理走 CPU 正常,W4 换 GPU EP 后消失;
- Windows 工作区噪声(不入库):autocrlf 行尾伪 diff + 本机 run_ibus.sh 既有 diff;pull 后残留的旧 `tests/test_windows_stub_adapters.py` untracked 副本已移出。

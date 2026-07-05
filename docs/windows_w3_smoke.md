# W3 Windows 真机 smoke 留痕(§6.2 验收记录)

> 归属:`docs/WINDOWS_COMPAT_PLAN.md` §5-W3 / §6.2。
> 裁决(2026-07-05 审批窗口):**W3 = complete。** §6.2 交互 8/8 PASS + 自动化层全绿;
> 断句/转写真人链路在实际运行配置 **cuda/float16** 下验收(口径说明见 §6);真嗓 GPT-SoVITS 归 W4(§7)。
> W2 的 W2-DPI-MULTISCREEN 挂账不受本段影响,仍在 §8 追踪。

## 1. 执行环境

- 执行日期:2026-07-05(自动化层由 Windows 侧脚本驱动;§6.2 交互项由操作者真机执行确认)
- 机器:Windows 10 Enterprise LTSC 2021 `10.0.19044` / conda `25.11.1` / spica-win python `3.11.15`
- 仓库:main HEAD `7bded62`(W3 实现);本次在 main 默认配置下运行(`screen.enabled=true`、`stt.device=cuda`——见 §6/§8)
- 纪律:Windows 只 pull + smoke,main 零 commit(E5);Windows 不跑 pytest(R7),逻辑层用原生 Python 脚本驱动真实代码(形制同 W2 adapter 级真机 smoke);本文档由 Linux 侧规范化原始回传草稿后提交

## 2. 依赖 + check_imports 门(REQUIRED 14/14)

- `pip install -r requirements-windows-base.txt` → exit 0;新增 **webrtcvad-wheels 2.0.14**(PyAudio 0.2.14 已在 W2-a 装通,satisfied),其余 satisfied;
- `python scripts\windows\check_imports.py` → **RESULT: OK,exit 0**。REQUIRED 14/14:PySide6 6.11.1 / openai 2.44.0 / httpx 0.28.1 / pydantic 2.13.4 / PyYAML 6.0.3 / python-dotenv 1.2.2 / numpy 1.26.4 / Pillow 11.3.0 / mss 10.2.0 / rapidocr-onnxruntime 1.4.4 / onnxruntime 1.27.0 / faster-whisper 1.2.1 / **PyAudio 0.2.14 / webrtcvad-wheels 2.0.14**;PREFLIGHT 段为空(W3 起 PyAudio 升 REQUIRED);providers = `['AzureExecutionProvider', 'CPUExecutionProvider']`。

## 3. W3 逻辑层原生 smoke:21/21 PASS

原生 Windows Python 驱动真实代码(与 Linux 测试面同构,另加 Linux 给不了的真 .pyd 证据):

- **[1] import 闭包**:AppHost + `resolve_mic_backend` + `ui.qt_overlay` + `generic_mic`;帧几何 320 samples / 640 bytes;
- **[2] `resolve_mic_backend` fold**:auto+windows→generic、auto+linux→respeaker、显式覆盖、未知值 raise;
- **[3] webrtcvad-wheels 原生 .pyd 契约**:import 名 `webrtcvad`;silence→False / voiced→True;10/20/30ms 帧接受;25ms 报错;
- **[4a] 断句状态机(fake VAD)**:收段 / pre-roll / 句中短停不切 / NoSpeech / Cancelled / on_speech_start 一次且异常被吞;
- **[4b] 真 webrtcvad 驱动断句循环**:合成 utterance 有界收段、正常终止——W3-a 遗留的「fork 原生扩展在 Windows 真能跑」假设就此闭合;
- **[5] fatal 信封(P2-3)**:开流失败→「无法打开麦克风」FATAL;缺 PyAudio→既有 FATAL;句中短读→非 fatal(transient)。

## 4. AppHost.initialize() 端到端(headless):COMPLETED + 选线日志

`load_secrets()` → `AppHost().initialize()` 走完,真实日志:

```
platform resolved: os_cfg=auto host=win32 effective=windows lanes=window_locator/windows_win32 screen_capture/mss game_launcher/windows_native
mic backend resolved: cfg=auto platform=windows effective=generic
initialize() COMPLETED. effective_mic_backend = 'generic'
```

真机 win32 下 mic backend 折算 generic 且 host 装配完成(重型 torch/CUDA 加载 deferred 到 best-effort warmup,不阻塞 init,符合 §2.4)。

## 5. §6.2 交互项:8/8 PASS(操作者真机执行,`run_spica.ps1` 启动 GUI)

| # | 条目 | 结果 | 备注 |
|---|---|---|---|
| 1 | run_spica.ps1 冷启动,overlay 起来 | **PASS** | LLM 文字对话通 |
| 2 | 通用麦说中文 → 断句 → faster-whisper 转写 | **PASS** | 参数与运行口径见 §6 |
| 3 | 无麦/禁用麦 → 致命错误停循环、不无限重试(P2-3) | **PASS** | 逻辑另有自动化佐证 §3[5] |
| 4 | 转写文本进 run_turn → LLM 回答返回 | **PASS** | 依赖 ② |
| 5 | 回答经播放链播出(预生成 wav / dummy TTS,QMediaPlayer+WMF) | **PASS** | 验的是播放链,非真嗓(§7) |
| 6 | song 播放 wav + mp3(WMF 编解码) | **PASS** | 播现成文件,torch 无关 |
| 7 | 中文输入法 TSF 可用 | **PASS** | 输入框打中文正常 |
| 8 | 打断:她说话时按停止 → 无 ghost 音频 | **PASS** | 对播放链音频验证 |

## 6. 断句/转写参数与运行口径(审批裁决,2026-07-05)

**实际参数(留痕定稿):**
- `vad_aggressiveness = 2`(代码默认 `DEFAULT_VAD_AGGRESSIVENESS`;生产路径无覆盖入口,Windows 机跑干净 `7bded62`,值由代码事实确定);
- `end_silence_seconds = 0.9`(`RESPEAKER_END_SILENCE_SECONDS` 未设,默认值生效);
- 运行配置 `stt.device = cuda` / `compute_type = float16`(main 默认 app.yaml)。

**cpu/int8 口径裁决(理由全文):**
- §6.2 #2 的核心验收对象是 **Windows 通用麦采集 + webrtcvad 软件断句 + faster-whisper 转写 + 文本进 run_turn** 这条链路本身;
- 该完整真人链路已在实际运行配置 **cuda/float16** 下 PASS;
- **cpu/int8 是 W2/W3 计划中的可移植验收建议口径,不作为本次 W3 complete 的硬 gate**;
- cpu/int8 已 **warmup OK**(W2 smoke 佐证:模型加载 3907ms、warmup 通过),证明该口径引擎可加载;若未来在无 GPU 机器验收,可按该口径重跑,不影响本机 W3 收口;
- 真嗓 GPT-SoVITS 归 W4,W3 只验 dummy/预生成 wav 播放链(§7)。

## 7. 真嗓边界(W4-gated,记清防误解)

GPT-SoVITS 真嗓需 torch/CUDA = **W4,本机未装**(实测 `torch: ModuleNotFoundError`,provider=`gptsovits_current`)。§6.2 ⑤ 只验播放链(wav);**对话中听不到 Spica 说真嗓属预期设计,等 W4,非 W3 回归**。TTS warmup 失败为 best-effort 只报不阻塞(§2.4 既证)。

## 8. 观察(均非 W3 回归)

- 本次在 main 默认配置跑(`screen.enabled=true` / `stt.device=cuda`):smoke 有效性不受影响;若真机长跑要日志干净,建议沿用 W2 windows 分支验收配置(`screen.enabled=false` / `stt.device=cpu` / `ocr.provider=rapidocr`,commit `13a0cbe`);
- Windows 工作区 autocrlf 伪 diff(build_release.sh / run_ibus.sh / dump_when_frozen.sh / monitor_resources.sh / tuning.py)+ `.claude/` untracked——与 W2 同,非本次改动,未触碰、不入库;
- 环境唯一副作用:spica-win 装入 webrtcvad-wheels 2.0.14(W3 预期依赖)。

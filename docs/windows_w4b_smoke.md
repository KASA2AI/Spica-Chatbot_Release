# W4-b §6.3 Windows 真机 smoke 留痕(重型运行时验收)

> 归属:`docs/WINDOWS_COMPAT_PLAN.md` §5-W4(W4-b)/ §6.3。
> 机器:Windows 10 Enterprise LTSC 2021 `10.0.19044` / RTX 4090 / driver 596.49。仓库 HEAD `84ee4b3`。
> env:`spica-win-heavy`(从 committed `requirements-windows-heavy.txt` 复现)。Windows 零 commit(E5);代码修复带回 Linux 提交。
> 状态:**A/B/D/E PASS(A/E 经修复);C(TTS真嗓)待单 env pin 清单;F/G/H 待真人/依赖。W4-b 非 complete。**

## 0. 头条:真机抓出 preload 真实 bug(已修复复验)

**我 ship 在 `84ee4b3` 的 Windows preload 是坏的。** gate-1 之所以绿,是因为 gate-1 探针用了 `import torch`(会加载整套 cuDNN-9);而实际 ship 的 preload(`find_spec` 定位 + 只 `WinDLL` 强载 `cudnn64_9.dll`)**不足**:

- **根因**:cuDNN 9 拆成「派发器 `cudnn64_9.dll` + 7 个后端 DLL(graph/ops/cnn/adv/heuristic/engines×2)」,派发器在 `cudnnCreate` 时按需 dlopen 那 7 个 sibling,**而这个内部加载不吃 `os.add_dll_directory` 目录**。只强载派发器 → EP init 时找不到 `cudnn_graph64_9.dll` → **`0xC0000409` fail-fast 硬崩**(绕过 Python except,`build_engine_with_fallback` 都接不住)。两个 preload 函数(CUDA 后端 + TRT 运行时)同缺陷。
- **根因诊断**(`diag_cudnn.py`,三策略隔离子进程各建 ORT CUDA session):`prod_preload`(只载 cudnn64_9)→ `0xC0000409` 崩;`force_family`(强载整套 8 个)→ returncode 0、CUDA EP 真 init;`import_torch`(gate-1 机制)→ 绿。
- **为什么 unit mock 没测出**:gate-1 单测 mock 了 `WinDLL` 不真加载 DLL,且假树只放了 `cudnn64_9.dll`——测不到 cuDNN 内部按需 dlopen 的真机行为。这正是真机 gate 的价值。
- **修复**:两个 `_preload_*_windows` 从「只强载 `cudnn64_9.dll`」改「强载整套 `_WIN_CUDNN_DLLS`(8 个,best-effort 存在即载,派发器在先)」,落点仍在两个 preload 函数内(不扩白名单),仍不 import torch、仍 env-free。**Linux 回归钉**(`tests/test_windows_gpu_preload.py`)补「断言全家族强载、非只派发器」,防回退。

## 1. 验收矩阵(修复后)

| 项 | 结论 | 证据 |
|---|---|---|
| A OCR GPU | ✅ **修复后 PASS** | `8 cuDNN-9 DLLs resident` → det/cls/rec 全 CUDAExecutionProvider,steady **124ms**,exit 0;`rapidocr_trt_ep` 0xC0000409 硬崩消失、优雅降级 CUDA(TRT 引擎另因 nvinfer 自己的 builder-resource sibling 同类问题 build 失败,但被 `build_engine_with_fallback` 接住 → 判据「TRT 降 CUDA 不算失败」可接受) |
| B watch chain | ✅ PASS | `verify_watch_chain.py` 整链通,`answer='画面上是个女孩。'`,LLM 2 次调用(probe 带 tools + stream 无 tools)。尾部 `WinError 32` 是诊断脚本 `TemporaryDirectory` 清理时 SQLite 连接未关的跨平台 nit,场景已跑完,非生产/非 W4-b |
| C GPT-SoVITS 真嗓 | ⛔ BLOCKED | 依赖闭包深(soundfile→librosa→jieba→gradio→pyopenjtalk);无 pin 装时 `g2p_en` 顶 numpy 2.4.6 已拉回。→ 待 §3 单 env pin 清单 |
| D faster-whisper CUDA | ✅ PASS | `device=cuda compute_type=float16` 加载 3.9s + 推理 222ms;ct2 自带 cuDNN,不受 preload bug 影响 |
| E Moondream inspect_screen | ✅ **修复后 PASS** | 装 `transformers 4.50 / accelerate / einops` + app 装 `MoondreamHfProvider` seam + 下 ~1.8GB 权重 → CUDA 出描述、无 ToolError、ok=true。(日文问句致输出退化 = 模型/prompt 质量,生产走英文 `DEFAULT_SCREEN_PROMPT`,另记非管线故障) |
| F galgame 全链 | ⛔ 待真人 | 依赖组件(OCR GPU/E)已绿;需跑 app + 真游戏窗 |
| G RVC/sing_song | ⏭️ 待装 | 单 env pin 清单(§3)装齐 RVC 层后由你点歌听声 |
| H 完整语音回环 | ⛔ 待真人 | STT(D)通;TTS 腿待 C |

## 2. 关键依赖发现(定 §3 清单用)

heavy env 按 committed `requirements-windows-heavy.txt` 建出后,**只覆盖 GPU-EP 替换**;真机 `dep_probe.py` 探得缺:
- screen/Moondream:`transformers accelerate einops`(**本次已并入 requirements-windows-heavy.txt** → E 转绿);
- TTS/RVC:`soundfile librosa numba scipy pyworld cn2an jieba pypinyin g2p_en pytorch_lightning` + gradio 闭包 + pyopenjtalk(→ §3 单 env pin 清单,从 Linux gptsovits 导出)。

修复后 env `pip check`:仅 `onnxruntime` dist 名 ×2(已知可接受)+ `py3langid 需 numpy>=2`(无害,split_lang 走 fast-langdetect 不碰它);numpy **1.26.4** 守住。完整 freeze 见 carryback `heavy_pip_freeze_final.txt`(供 §3 定 pin)。

## 3. 后续(非本次修复窗口)

1. **单 env pin 清单**(§windows_heavy_install.md §3):从 Linux gptsovits 导出 TTS+RVC 完整 pin,numpy<2 守卫,`--no-deps` 装 numpy≥2 声明者 → 解 C/G,并让 Windows 一个 env 复现 Linux 全栈;
2. **TRT 真活(可选低优)**:强载 nvinfer builder-resource DLL(`nvinfer_builder_resource_sm89_10.dll` 等)让 TRT 引擎 build 不降级——非默认 provider;
3. **Moondream 日文 prompt 输出退化**:模型/prompt 质量,另记;
4. **`verify_watch_chain.py` Windows teardown nit**:跨平台脚本小修,另记不阻塞。

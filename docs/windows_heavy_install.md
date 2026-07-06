# Windows 重型运行时安装草案(W4-b:单一 app env)

> 归属:`docs/WINDOWS_COMPAT_PLAN.md` §5-W4(W4-b)。依据:W4-a 探底(`docs/windows_w4_probe.md`)+ W4-b gate 1(2026-07-06,heavy 组合 CUDA+TRT EP 真 init 全绿)+ W4-b §6.3 真机 smoke(`docs/windows_w4b_smoke.md`)。
> 状态:**install 草案 + gate-1/§6.3 已验部分**;完整 TTS+RVC pin 清单待从 Linux 工作 env 导出(见 §3)。
> 纪律:不碰 `requirements-windows-base.txt` 语义;`data/config/app.yaml`(main)不改,GPU 验收配置只进 windows 分支(P3-3)。

## 1. 单一 app env(镜像生产 Linux `gptsovits` env,numpy 1.26.4)

**整个 app —— OCR/STT/TTS/screen,连 RVC/唱歌 —— 跑在一个 conda env。** 这不是取舍,是照抄 Linux 生产现状:

- Linux `gptsovits` env 实测 numpy `1.26.4`(<2),里面 LLM/PySide6/OCR(rapidocr+onnxruntime-gpu)/STT(faster-whisper)/TTS(GPT-SoVITS)/**RVC(audio-separator 0.44.2 + faiss + torchcrepe/torchfcpe)** 全在;
- `rvc/driver.py:141` `python = worker_python or sys.executable` + `song/config.py:44` `worker_python: None` → **RVC worker 默认用同一解释器,不 subprocess 到第二个 env**;`applio`/`spica-clean-py311` 是历史调研 env,非生产路径;
- **`audio-separator` 的 `numpy>=2` 是元数据声明,runtime 不强制**:在 numpy 1.26.4 下 `import audio_separator` + `Separator` load 均 OK,`pip check` 标红但 runtime 绿——与 `onnxruntime` dist-name 红同类。故此前「RVC 必须 numpy≥2 独立 env」是 clean-env spike(为 pip-check-green 选 numpy 2.x)的产物,**不是硬约束**,已纠正。

> Windows wheel 面已探明(W4-a §2):RVC 关键 7 包 + torch cu124 全有 py3.11 win_amd64 wheel。

## 2. 安装顺序(GPU-EP 核 + screen 层,gate-1/§6.3 已验)

```powershell
# 0) 从正式 spica-win 克隆(验收期先 clone,过了再决定是否原地升级 spica-win)
conda create --clone spica-win -n spica-win-heavy -y

# 1) CPU onnxruntime -> GPU 构建(两个 dist 冲突,必须先卸)+ screen/Moondream 层
conda run -n spica-win-heavy python -m pip uninstall -y onnxruntime
conda run -n spica-win-heavy python -m pip install -r requirements-windows-heavy.txt
#    注:tensorrt-cu12 偶发 transient sha256 失败 -> 重试一次再判真失败

# 2) torch/torchaudio 走 cu124 index(绝不能裸 pip install torch)
conda run -n spica-win-heavy python -m pip install torch==2.5.1 torchaudio==2.5.1 --index-url https://download.pytorch.org/whl/cu124
```

**验收判定(真 session init,非静态 provider 清单):**
- numpy 保持 `1.26.4`(被顶即停);torch `2.5.1+cu124`、`cuda.is_available()=True`;
- ORT CUDA EP 真 active(§6.3 修复后 124ms 全绿);TRT 若不可达降级 CUDA(可接受);
- Moondream `inspect_screen` 上 CUDA 出描述(§6.3 E 项已验,需 transformers/accelerate/einops——已在清单);
- `pip check` 的 `onnxruntime` dist-name 红 = 已知不洽(runtime 绿),**不修**;
- 进程内 DLL 定序由 preload 代码承担(**整套 cuDNN-9 家族**先、`nvinfer_10.dll` 后——见 §6.3 根因,W4-b 已实装两个 preload 函数)。

## 3. TTS + RVC 完整依赖 —— 单 env pin 清单(follow-on,从 Linux gptsovits 导出)

**做法(避免逐包裸装的 numpy 陷阱):** 从生产 `gptsovits` env 的 `pip freeze` 裁出实际 import 闭包,pin 死成一份 Windows 单 env 清单,**numpy==1.26.4 守卫**,对 `audio-separator`/`g2p_en` 等声明 `numpy>=2` 的包用 `--no-deps`/constraints 装(元数据红、runtime 绿)。

> **为什么必须 pin**:§6.3 smoke 实测——无 pin 装文本前端时 `g2p_en`(`numpy>=1.13.1` 无上界)把 numpy 顶到 2.4.6,违反 GPT-SoVITS 的 numpy<2,当场破坏 TTS。所以不能在验收机逐包猜装。

- 覆盖面:GPT-SoVITS 音频核(soundfile/librosa/numba/scipy/pyworld)+ 文本前端(jieba/pypinyin/cn2an/g2p_en/split_lang…)+ vendored `inference_webui.py` 顶层 import 的 web-UI 闭包(gradio 等)+ 日文 g2p `pyopenjtalk`(Windows 编译易碎,单独留意);+ RVC 层(`requirements-rvc.txt` 的包,同 env);
- vendored GPT-SoVITS 的 `os.name=="nt"` 分支转正、pushd/cwd、cache 落点 = §6.3 真嗓验收项,**vendored 零 patch**(R5);
- 产出后并入本文件 §2 或另立 `requirements-windows-app.txt`(施工时定)。

## 4. §6.3 已验 / 待验矩阵(细节见 `docs/windows_w4b_smoke.md`)

| A OCR-GPU | B watch | C TTS真嗓 | D whisper | E Moondream | F galgame | G RVC | H 语音回环 |
|---|---|---|---|---|---|---|---|
| ✅ 修复后绿 | ✅ | ⛔ 待 §3 清单 | ✅ | ✅ | ⛔待真人 | ⏭️待 §3 装 | ⛔待真人 |

## 5. 明确不做

- 不把本文件任何包放进 `requirements-windows-base.txt`(base 保持 CPU 可装);
- 不在 main 改 `data/config/app.yaml`(GPU 验收配置 = windows 分支专属 commit,`13a0cbe` 形制重打:`ocr.provider` 切 `rapidocr_trt_ep`/`rapidocr_ort`、`screen.enabled: true`、`stt.device: cuda`/`float16`——随 §6.3 定);
- 不 patch vendored;不动 `build_ocr_adapter` 选择逻辑与 OCR 共线设计。

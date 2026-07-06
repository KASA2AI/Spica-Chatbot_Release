# Windows 重型运行时安装草案(W4-b:主 heavy env + RVC 独立 env)

> 归属:`docs/WINDOWS_COMPAT_PLAN.md` §5-W4(W4-b)。依据:W4-a 探底(`docs/windows_w4_probe.md`)+ W4-b gate 1 真机验证(2026-07-06,heavy 组合 CUDA+TRT EP 真 init 全绿)。
> 状态:**install 草案 + gate-1 已验部分**;GPT-SoVITS/TTS 依赖面待 W4-b Windows 真嗓验收时以实测迭代(vendored 零 patch 原则,R5)。
> 纪律:一切安装不碰 `requirements-windows-base.txt` 的语义;`data/config/app.yaml`(main)不改,GPU 验收配置只进 windows 分支(P3-3)。

## 1. 双 env 结构(numpy 冲突,平台无关——Linux 实证 + W4-a 复核)

| env | numpy 侧 | 装什么 | 用途 |
|---|---|---|---|
| **主 heavy env**(spica-win 升级或其 clone) | `<2`(1.26.4) | base 清单 + `requirements-windows-heavy.txt` + torch/torchaudio cu124(+ GPT-SoVITS 依赖,待实测定稿) | app 本体:OCR GPU / STT CUDA / TTS 真嗓 / Moondream |
| **`spica-win-rvc`**(独立) | `>=2`(2.4.6) | torch trio cu124 + `requirements-rvc.txt` | RVC/song 变声 worker(`rvc/driver.py::worker_python` 指向此 env 的 python.exe) |

> 为什么分:audio-separator 强制 `numpy>=2`,GPT-SoVITS 前向约束 `numpy<2`——同 env 不可调和(`docs/LOCAL_RUNTIME_RVC_ENV_REALITY.md`)。Windows wheel 面已探明:RVC 关键 7 包 + torch cu124 全有 py3.11 win_amd64 wheel(W4-a §2)。

## 2. 主 heavy env 安装顺序(gate-1 已验)

```powershell
# 0) 基于正式 spica-win 克隆(验收期建议先 clone,验收过再决定是否原地升级 spica-win)
conda create --clone spica-win -n spica-win-heavy -y

# 1) CPU onnxruntime -> GPU 构建(两个 dist 冲突,必须先卸)
conda run -n spica-win-heavy python -m pip uninstall -y onnxruntime
conda run -n spica-win-heavy python -m pip install -r requirements-windows-heavy.txt
#    注:tensorrt-cu12 偶发 transient sha256 失败 -> 重试一次再判真失败

# 2) torch/torchaudio 走 cu124 index(绝不能裸 pip install torch)
conda run -n spica-win-heavy python -m pip install torch==2.5.1 torchaudio==2.5.1 --index-url https://download.pytorch.org/whl/cu124
```

**验收判定(gate-1 形制,真 session init 而非静态 provider 清单):**
- numpy 保持 `1.26.4`(被顶即停);
- torch `2.5.1+cu124`、`torch.cuda.is_available()=True`;
- ORT CUDA EP 真 active;det/rec TRT 真 active、cls CUDA(per-stage 语义与 Linux golden 一致);
- `pip check` 的 `onnxruntime` dist-name 红 = 已知不洽(runtime 绿),**不修**;
- 生产进程内的 DLL 定序由 preload 代码承担(`cudnn64_9.dll` 先、`nvinfer_10.dll` 后——`backends/rapidocr.py` / `rapidocr_trt_runtime.py` 的 Windows 分支,W4-b 实装)。

## 3. GPT-SoVITS / TTS(主 heavy env,待实测定稿)

- vendored GPT-SoVITS 沿 Linux golden 依赖面(numpy<2 侧),Windows 差异集中在:`os.name=="nt"` 分支转正验证(`tts/driver.py`)、pushd/cwd 行为、cache 落点——均为 W4-b §6.3 验收项,**vendored 原则零 patch**,确需小修单独记录可回滚(R5);
- 依赖清单不在本草案预写死:**以 Windows 真机 import 报错清单实测迭代**(W2-a 的清单纪律),定稿后另立 `requirements-windows-tts.txt` 或并入本文件 §2。

## 4. RVC 独立 env(`spica-win-rvc`)

```powershell
conda create -n spica-win-rvc python=3.11 -y
# torch trio 先装(cu124 index)——requirements-rvc.txt 头注的既定顺序
conda run -n spica-win-rvc python -m pip install torch==2.5.1 torchaudio==2.5.1 torchvision==0.20.1 --index-url https://download.pytorch.org/whl/cu124
conda run -n spica-win-rvc python -m pip install -r requirements-rvc.txt
```

- 接线:song 侧 `rvc/driver.py` 的 `worker_python` 参数指向 `...\envs\spica-win-rvc\python.exe`(seam 已在,零代码改动);
- 验收:§6.3「RVC/sing_song 若独立 env 就绪则点歌全链出声;未就绪显式记录跳过」;
- Linux 参照:`docs/LOCAL_RUNTIME_RVC_INSTALL.md`(smoke 步骤同构迁移)。

## 5. 明确不做

- 不把本文件任何包放进 `requirements-windows-base.txt`(base 保持 CPU 可装);
- 不在 main 改 `data/config/app.yaml`(GPU 验收配置 = windows 分支专属 commit,`13a0cbe` 形制重打:`ocr.provider` 切 `rapidocr_trt_ep` 或 `rapidocr_ort`、`screen.enabled: true`、`stt.device: cuda`/`float16`——具体值随 §6.3 验收定);
- 不 patch vendored;不动 `build_ocr_adapter` 选择逻辑与 OCR 共线设计。

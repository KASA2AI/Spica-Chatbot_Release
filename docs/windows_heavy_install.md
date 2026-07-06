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

## 3. TTS + RVC 单 env 清单（已产出 draft,待 Windows 验证）

从生产 `gptsovits` env 的 `pip freeze`(259 包,numpy 1.26.4)导出,产出两文件:
- **`requirements-windows-app.txt`** —— TTS + RVC 顶层包(可读、pin 到 gptsovits 版本);
- **`constraints-windows-app.txt`** —— 全量锁(240 行,剔除 torch trio/nvidia-cu12/triton/pyopenjtalk/@直引),pin 每个传递依赖 + `numpy==1.26.4`,防任何 transitive 顶 numpy(§6.3 smoke 那个 `g2p_en` 坑)。

**两处 Windows 替换(均 drop-in,import 名不变):**
- `pyopenjtalk` → **`pyopenjtalk-plus==0.4.1.post8`**:Linux 的 pyopenjtalk 0.4.1 **无 win_amd64 wheel**(探针实测),-plus fork 有 cp311 wheel 且 top_level=`pyopenjtalk`;
- `audio-separator==0.44.2`:**单独 `--no-deps` 装**(声明 numpy≥2 是元数据、runtime 绿——gptsovits 单 env 实证)。

**安装顺序(基于 clone 的单 heavy env):**

```powershell
# 前置:§2 已把 base + heavy(GPU-EP + screen 层)+ torch/torchaudio cu124 装好
# 1) TTS+RVC 顶层,带 constraints 锁(numpy 守在 1.26.4、transitive 全钉 gptsovits 版)
conda run -n spica-win-heavy python -m pip install -c constraints-windows-app.txt -r requirements-windows-app.txt
# 2) audio-separator 单独 --no-deps（绕过其 numpy>=2 元数据声明）
conda run -n spica-win-heavy python -m pip install -c constraints-windows-app.txt --no-deps audio-separator==0.44.2
# 3) 守卫复查
conda run -n spica-win-heavy python -c "import numpy; assert numpy.__version__=='1.26.4', numpy.__version__; print('numpy held', numpy.__version__)"
```

> **wheel 面已探明(Windows py3.11)**:soundfile/numba/scipy/gradio/pytorch-lightning/faiss-cpu/torchcrepe/torchfcpe/pedalboard/soxr/audio-separator 全有 wheel;唯 pyopenjtalk 无 → -plus fork 顶。**只有 audio-separator 需 --no-deps**(`ml_dtypes` 的 numpy≥2 是 py3.13 条件依赖、py3.11 不触发;`g2p_en` 无上界,constraints 钉住即可)。

- vendored GPT-SoVITS 的 `os.name=="nt"` 分支转正、pushd/cwd、cache 落点 = §6.3 真嗓验收项,**vendored 零 patch**(R5);
- **draft 状态**:两文件与本 recipe 待 Windows 装机 + `service.py` import 闭包 + 真嗓合成验证后定稿(C/G 项)。

## 4. §6.3 已验 / 待验矩阵(细节见 `docs/windows_w4b_smoke.md`)

| A OCR-GPU | B watch | C TTS真嗓 | D whisper | E Moondream | F galgame | G RVC | H 语音回环 |
|---|---|---|---|---|---|---|---|
| ✅ 修复后绿 | ✅ | ⛔ 待 §3 清单 | ✅ | ✅ | ⛔待真人 | ⏭️待 §3 装 | ⛔待真人 |

## 5. 明确不做

- 不把本文件任何包放进 `requirements-windows-base.txt`(base 保持 CPU 可装);
- 不在 main 改 `data/config/app.yaml`(GPU 验收配置 = windows 分支专属 commit,`13a0cbe` 形制重打:`ocr.provider` 切 `rapidocr_trt_ep`/`rapidocr_ort`、`screen.enabled: true`、`stt.device: cuda`/`float16`——随 §6.3 定);
- 不 patch vendored;不动 `build_ocr_adapter` 选择逻辑与 OCR 共线设计。

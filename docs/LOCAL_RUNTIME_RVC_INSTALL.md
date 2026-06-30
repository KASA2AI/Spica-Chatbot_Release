# RVC / Song Voice-Conversion — Install Baseline（RVC Isolation Step3.1）

> 配套：`requirements-rvc.txt`（本文档的依赖清单）、`docs/LOCAL_RUNTIME_CLEAN_ENV_SPIKE.md`（实测来源）、`docs/LOCAL_RUNTIME_RVC_ENV_REALITY.md`（冲突分析）。
> **这是 RVC/separator worker 的独立运行环境安装说明，不是主 Spica env，不是 GPT-SoVITS/TTS env。**
> 依据：clean env `spica-clean-py311`（Python 3.11.15，RTX4090 / driver 555 / CUDA 12.5 / cu124）实测——`pip check` 干净、RVC 产出 wav、separator provider OK。

---

## 1. 这套环境是什么 / 不是什么

| | |
| --- | --- |
| ✅ 是 | RVC 推理（Applio 推理子集）+ 人声分离（audio-separator）的**独立 env**，**numpy 2.x 侧** |
| ❌ 不是 | 主 Spica env；不是 GPT-SoVITS/TTS env |
| ❌ 不是 | Applio full requirements（webui/training/gradio5/numpy2.4.4/transformers5——推理不需要） |
| ❌ 不是 | 当前 golden `gptsovits` env 的安装说明（那是手工违约态、pip-check 红、不可复现，见 §5） |

**为什么独立**：RVC 实测可跑在 numpy 2.x；`audio-separator` 又**要求** numpy≥2；而 GPT-SoVITS **要求** numpy<2。把 RVC/separator 拆进自己的 numpy-2.x env，与 TTS 的 numpy<2 彻底解耦——这个 env `pip check` 干净，比 golden 单 env 更稳。

---

## 2. Linux 安装（已验证）

```bash
conda create -n spica-rvc-py311 python=3.11 -y
conda activate spica-rvc-py311
python -m pip install --upgrade pip

# 1) torch 三件套：走 CUDA wheel 官方 index（匹配 golden 的 cu124）。单独装，不进 requirements-rvc.txt。
python -m pip install torch==2.5.1 torchaudio==2.5.1 torchvision==0.20.1 \
    --index-url https://download.pytorch.org/whl/cu124

# 2) 其余依赖：可换国内镜像（清华/USTC）。USTC 曾出现 IncompleteRead，清华更稳。
python -m pip install -r requirements-rvc.txt \
    -i https://pypi.tuna.tsinghua.edu.cn/simple

# 3) 一致性自检（应当干净）
python -m pip check
```

> 镜像可选：去掉 `-i ...` 走默认 PyPI，或换 USTC `http://pypi.mirrors.ustc.edu.cn/simple/ --trusted-host pypi.mirrors.ustc.edu.cn`。
> `pyopenjtalk` 在本 env **不需要**（那是 TTS 的）；`pyncm`（网易云下载）不在镜像、也不在推理路径，按需从 pythonhosted 装。

## 3. Windows 安装

```text
TODO: verify on Windows with CUDA wheel / faiss-cpu / onnxruntime-gpu / audio-separator.
  - torch cu124 wheel: pip install ... --index-url https://download.pytorch.org/whl/cu124 (win32 wheel)
  - faiss-cpu / onnxruntime-gpu / pedalboard / soxr: confirm Windows wheels exist for py3.11
  - rvc/lib/predictors/f0.py uses multiprocessing -> Windows must use 'spawn' (worker concern, Step3.2)
  - rvc/lib/platform.py already handles win32 (ASIO) -- not on the inference path
NOT yet supported. Do not claim Windows works.
```

---

## 4. 已验证版本（clean env 实测，requirements-rvc.txt 与此一致）

| 包 | 版本 | 用途 |
| --- | --- | --- |
| python | 3.11.15 | — |
| torch / torchaudio / torchvision | 2.5.1+cu124 / 2.5.1+cu124 / 0.20.1+cu124 | 单独走 cu124 index |
| numpy | **2.4.6** | numpy 2.x 侧（audio-separator 要 ≥2） |
| scipy / librosa / soundfile / soxr | 1.17.1 / 0.10.2 / 0.13.1 / 1.1.0 | 音频 |
| transformers | 4.50.0 | contentvec/hubert 加载 |
| faiss-cpu | 1.14.2 | spica.index 检索 |
| torchcrepe / torchfcpe | 0.0.24 / 0.0.4 | 音高 |
| onnxruntime-gpu | 1.26.0 | separator + provider |
| audio-separator | 0.44.2 | 人声分离（强制 numpy≥2） |
| python-dotenv / noisereduce / pedalboard / psutil | 1.2.2 / 3.0.3 / 0.9.23 / 7.2.2 | Applio 推理链模块级依赖 |
| tensorboard / wget / beautifulsoup4 | 2.21.0 / 3.2 / 4.15.0 | Applio core 模块级 import 拉入 |

> 版本以 clean env 实测为准，不为美观改动。RVC 的 `tensorboard`/`wget`/`beautifulsoup4` 是 `core.py` 模块级 import 链拉进来的（推理本身不用，但**必须存在**才能 import——TTS-B1 同型教训）。

---

## 5. 为什么不能用当前 `gptsovits` env 当安装说明

- golden `gptsovits` env 是 **GPT-SoVITS + Applio + separator + UI 全塞一起**的手工态：numpy 压在 1.26.4（喂 GPT-SoVITS），**违反** audio-separator 的 numpy≥2 → `pip check` 红、无 lockfile、不可复现。
- 按 golden freeze 重建会带本机路径（如 `PyAudio @ file:///...`）+ 违约约束，不可作为安装说明。
- **正确做法**：TTS 用自己的 numpy<2 env + 自己的 requirements；RVC/separator 用本文档的 numpy-2.x env + `requirements-rvc.txt`。两套分开。

---

## 6. Smoke test（安装后验证）

### A. import smoke
```bash
python - <<'PY'
import torch, torchaudio, numpy, librosa, soundfile, soxr, transformers, faiss
import onnxruntime, torchcrepe, torchfcpe, noisereduce, pedalboard, dotenv
print("import ok | numpy", numpy.__version__, "| torch", torch.__version__)
PY
```

### B. RVC smoke（验证入口 + spike 结果）
- 入口：`agent_tools/function_tools/song/rvc.py::infer_spica_vocal` → Applio `core.py::infer_spica_vocal`。
- 输入：一段已分离的短 vocal wav（spike 用 `static/generated_song/cache/separated/*/vocals.wav` 截 ~8–15s；不下载新歌、不跑网易云）。
- 资源：RVC model `spica_200e_57000s.pth`、`spica.index`、`rmvpe.pt`、contentvec——全部从 `Applio/` 加载。
- **spike 结果**：在本 env（numpy 2.4.6）下产出 `rvc.wav` 638KB，`infer_spica_vocal` 返回输出路径，无报错。
- 验证脚本可参考 scratchpad 的 `clean_env_smoke.py`（不入库；含本机路径）。**不要求完全自动化**，但每次重建 env 后应跑一次确认产出 wav。

### C. separator smoke
```bash
python - <<'PY'
import onnxruntime as ort
print("providers:", ort.get_available_providers())   # 期望含 CUDAExecutionProvider
from audio_separator.separator import Separator
print("audio-separator import ok")
PY
```
- spike 结果：providers = `['TensorrtExecutionProvider', 'CUDAExecutionProvider', 'CPUExecutionProvider']`，import OK。

### D. pip check
```bash
python -m pip check          # 目标：No broken requirements found（本 env 应当干净）
```

---

## 7. 边界（硬性）

- `requirements-rvc.txt` **只用于 RVC/separator worker env**，不是主 Spica env、不是 GPT-SoVITS/TTS env。
- **不要**把 `requirements-rvc.txt` / numpy 2.x 装进当前 golden `gptsovits` env（会打断 GPT-SoVITS 的 numpy<2）。
- GPT-SoVITS 仍需**自己的** TTS env / requirements（numpy<2 侧；含多语言 g2p + 可能的系统库 mecab-ko，见 spike 文档）。
- **不要**用 Applio full requirements 替代本文件。
- RVC worker（未来 Step3.2）**应使用本 env**；worker 仍必须只通过 **JSON request + 磁盘 wav** 与主进程通信（不 import `spica` 主包、不依赖 Qt/ChatEngine/run_turn）。
- 本轮**未**实现 worker / Port / subprocess / slim；只固化安装基线。

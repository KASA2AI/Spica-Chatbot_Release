# RVC Environment Reality Check（Step3.0 基线快照）

> 配套：`docs/LOCAL_RUNTIME_PLAN.md`、`docs/LOCAL_RUNTIME_TTS_SLIM_B1.md`。
> **本文是事实记录，不是安装说明。** 把当前「手工混合但可跑」的 `gptsovits` env 固化为可追溯基线，避免后续升级/实验弄坏后无法还原。
> 采集日期：2026-06-30（开发机 `gptsovits` conda env，python 3.11.15）。**本轮零环境改动、零装包/卸载、零 pip 修复。**

---

## 1. 当前结论摘要

- 当前 **不是** TTS env + RVC env 两套；是 **GPT-SoVITS + Applio/RVC + audio-separator + Spica UI 全在同一个 `gptsovits` conda env**。
- 它 **既不是干净的 GPT-SoVITS env，也不是干净的 Applio env**——是这台开发机上**手工调出来的可运行状态**。
- **RVC 当前可运行**（Step1 已实测：一次真实 RVC 推理跑通，bit-level 输出正常）。
- 但 **`pip check` 不通过（rc=1）**；**无 lockfile、无 conda export、无可复现安装脚本**。
- **当前状态不能视为可复现安装方案**——只能当「经验基线」。
- 后续隔离工作目标（顺序）：
  1. **先 same-env subprocess** 清掉主进程污染（sys.modules/sys.path/env）；
  2. **再 independent RVC env** 解决可复现性（复刻当前可工作的推理子集，**非** Applio full requirements）；
  3. **最后 slim runtime** 作为独立 env/runtime 的内容清单（裁掉 ~9.3G 训练垃圾）。

> raw `pip freeze`（258 包）只存于开发机 scratchpad，**未入库**（含机器专属路径，见 §8 / 文末）。

---

## 2. 当前关键包版本（实测，非推测）

| 包 | 版本 | 站队 |
| --- | --- | --- |
| python | 3.11.15 | — |
| torch | 2.5.1 | 两边之间（均可用） |
| torchaudio | 2.5.1 | — |
| torchvision | 0.20.1 | — |
| **numpy** | **1.26.4** | **GPT-SoVITS 侧（<2）** |
| scipy | 1.17.1 | 一致 |
| **librosa** | **0.10.2** | **GPT-SoVITS 侧** |
| soundfile | 0.13.1 | 一致 |
| faiss-cpu | 1.14.2 | ~Applio |
| **transformers** | **4.50.0** | **GPT-SoVITS 上界** |
| **gradio** | **4.44.1** | **GPT-SoVITS 侧（<5）** |
| onnxruntime-gpu | 1.26.0 | （CPU 包 `onnxruntime` **未装**） |
| audio-separator | 0.44.2 | 分离步 |
| torchcrepe | 0.0.24 | Applio |
| torchfcpe | 0.0.4 | Applio |
| ffmpeg-python | 0.2.0 | — |
| pyncm | 1.8.1 | 下载步 |
| pyopenjtalk | 0.4.1 | GPT-SoVITS |
| PySide6 | 6.11.1 | Spica UI |

**关键观察：每一个冲突轴上，当前 env 都站 GPT-SoVITS 那边**（numpy 1.x / librosa 0.10.2 / gradio 4.x / transformers 4.50），**与 Applio requirements 完全相反**。

---

## 3. `pip check` 结果 → **失败（exit code = 1）**

```
# 真实风险（数值冲突，但当前用到的代码路径未触发）
audio-separator 0.44.2 has requirement numpy>=2, but you have numpy 1.26.4
audio-separator 0.44.2 has requirement rotary-embedding-torch<0.7.0,>=0.6.1, but you have 0.8.9

# 声明缺失依赖（属于 audio-separator 的“其它分离模型”，当前用的 UVR-MDX ONNX 路径不需要）
audio-separator 0.44.2 requires beartype / diffq / julius / ml-collections /
                 onnx-weekly / onnx2torch-py313 / samplerate, which are not installed

# 假阳性（包名差异）：装的是 onnxruntime-gpu，提供同一 runtime
faster-whisper 1.2.1 requires onnxruntime, which is not installed
rapidocr-onnxruntime 1.4.4 requires onnxruntime, which is not installed
```

| 类别 | 条目 | 性质 |
| --- | --- | --- |
| **实际风险** | audio-separator 要 numpy≥2（env=1.26.4）、rotary-embedding-torch<0.7（env=0.8.9） | 复现/升级风险；当前分离路径仍能跑 |
| **当前未触发** | audio-separator 缺 beartype/diffq/julius/… | 属其它分离模型；UVR-MDX ONNX 不需要 |
| **假阳性/包名差异** | faster-whisper / rapidocr 要 `onnxruntime` | 已装 `onnxruntime-gpu`，runtime 实际可用 |

> **未修、未让 resolver 重装。** pip check 红本身就是「这是脆弱手工态」的证据。

---

## 4. GPT-SoVITS / Applio / 当前 env 对比

| package | current gptsovits env | GPT-SoVITS expected | Applio requirements | RVC inference imports? | conflict level |
| --- | --- | --- | --- | --- | --- |
| python | 3.11.15 | 3.x | 3.x | — | none |
| torch | 2.5.1 | (unpinned) | ==2.7.1+cu128 | yes | soft |
| torchaudio | 2.5.1 | (unpinned) | ==2.7.1+cu128 | yes | soft |
| torchvision | 0.20.1 | — | ==0.22.1+cu128 | no | not_imported_by_inference |
| **numpy** | **1.26.4** | **<2.0** | **==2.4.4** | yes | **hard_on_paper** → runtime_compatible_in_current_env |
| scipy | 1.17.1 | (unpinned) | ==1.17.1 | yes | none |
| **librosa** | **0.10.2** | **==0.10.2** | **==0.11.0** | yes | soft → runtime_compatible_in_current_env |
| soundfile | 0.13.1 | (unpinned) | ==0.13.1 | yes | none |
| **transformers** | **4.50.0** | **>=4.43,<=4.50** | **==5.4.0** | yes (contentvec) | **hard_on_paper** → runtime_compatible_in_current_env |
| **gradio** | **4.44.1** | **<5** | **==5.50.0** | **no** | **not_imported_by_inference** |
| faiss-cpu | 1.14.2 | — | ==1.13.2 | yes | soft |
| torchcrepe | 0.0.24 | — | (req) | yes | none |
| torchfcpe | 0.0.4 | — | (req) | yes | none |
| onnxruntime(-gpu) | 1.26.0 | — | (audio-separator) | **no** | not_imported_by_inference（分离器才用） |
| audio-separator | 0.44.2 | — | (分离步) | no | hard_on_paper（pip check 红）→ 分离步可跑，仍是复现风险 |
| pyncm / PySide6 / pyopenjtalk | 1.8.1 / 6.11.1 / 0.4.1 | — / UI / GPT-SoVITS | — | no | none |

**逐条解释（重点）：**
- **numpy** 1.26.4：满足 GPT-SoVITS（<2），违反 Applio 声明（==2.4.4），但 **RVC 推理实测在 1.26 跑通**；audio-separator 声明 numpy≥2 → pip check 红，分离步仍能跑。
- **transformers** 4.50.0：满足 GPT-SoVITS 上界，违反 Applio 声明（==5.4.0），但 **contentvec/hubert 实测在 4.50 能加载**。
- **gradio** 4.44.1：Applio 声明 5.50，但 **RVC 推理根本不 import gradio**（最大纸面冲突 moot）。
- **audio-separator**：pip check 红（numpy / rotary-embedding-torch），当前 UVR-MDX ONNX 路径能跑，**仍是复现风险**。

---

## 5. 当前为什么能跑（结论）

```
当前能跑，不代表 requirements 天然兼容。
当前能跑 = GPT-SoVITS-dominant env + RVC 推理子集向后兼容旧版本 + 没碰 Applio webui/training。
```

- **不能按 Applio full requirements 重建**——那会装 numpy2 / gradio5 / transformers5.4 / torch2.7，**直接打断 GPT-SoVITS**（numpy<2 / transformers≤4.50 / gradio<5）。
- **不能按 GPT-SoVITS requirements 直接认为 RVC 全部可用**——RVC 还需 faiss-cpu / torchcrepe / torchfcpe / audio-separator（不在 GPT-SoVITS requirements 里），且 audio-separator 声明 numpy≥2 会和 resolver 打架。
- 三大纸面冲突的真实状态：gradio **不在推理路径**；numpy / transformers **推理实测在 GPT-SoVITS 旧版本上跑通**（Applio 的激进 pin 是 webui/training 全集级别，不是推理必需）。
- **当前应作为「经验基线」，而不是「正式安装说明」。**

---

## 6. 下一步路线修正

```
Step3.0  env reality snapshot                    ← 本文（基线，不改环境）
Step3.1  same-env subprocess worker              ← 先用同一个 gptsovits Python 跑 subprocess
Step3.2  independent RVC env (current subset)    ← 复刻当前可工作推理子集，非 Applio full requirements
Step3.3  isolated slim RVC runtime               ← runtime 内容裁成 slim（~620M，去 ~9.3G 训练垃圾）
```

- **Step3.1**：worker 用同一个 `gptsovits` Python，**先解决 `sys.modules` / `sys.path` / env 污染主进程**的问题；**不立即处理第二套 env**。
- **Step3.2**：再做独立 RVC env；**独立 env 应复刻当前可工作的推理子集版本**（torch2.5.1 / numpy1.26.4 / transformers4.50 / librosa0.10.2 / faiss1.14.2 / torchcrepe0.0.24 / torchfcpe0.0.4 / scipy1.17.1 / soundfile0.13.1），**不应直接照 Applio full requirements**。
- **Step3.3**：再把 runtime 内容裁成 slim（内容清单见 Step1 证据：core.py + rvc/{configs,infer,lib} + rvc/train/process/{model_blender,model_information} + RVC .pth + index + contentvec + rmvpe）。

> 体积裁剪（曾经设想的「第一刀」）**降级为 Step3.3 的内容清单**——因为真正的 P0 是隔离/复现，不是体积。

---

## 7. 风险登记

| 级别 | 风险 |
| --- | --- |
| **P0** | 当前 env 不可复现（无 lock / 无 conda export / 无安装脚本） |
| **P0** | `pip check` 失败 |
| **P0** | in-process Applio import 污染主进程 sys.modules（+4472 模块）/ sys.path（+3 含 /tmp 临时目录）/ env（KMP/CUDA 变量） |
| **P1** | 包升级可能静默打断 RVC 或 GPT-SoVITS（pip check 已红，约束本就被违反） |
| **P1** | audio-separator 依赖不匹配（numpy≥2 / rotary-embedding-torch） |
| **P2** | 未来 Windows 安装未知（torch+cu / faiss / audio-separator / torchcrepe wheel；f0.py multiprocessing 需 spawn） |
| **P2** | 独立 RVC env 尚未验证 |
| **P3** | Applio full requirements 对「仅推理」用途有误导性（是 webui+training 全集 + 激进 pin） |

---

## 8. 禁止误用（硬性）

- **不要**把当前 `pip freeze` 当成正式 requirements。
- **不要**用 Applio full requirements 直接重建 RVC env（会装 numpy2/gradio5/transformers5.4 → 打断 GPT-SoVITS）。
- **不要**自动升级 numpy / transformers / gradio。
- **不要**用 `pip install -U` 批量更新当前 env（pip check 已红，resolver 重算会动违反约束的包）。
- **不要**让 subprocess 模式静默 fallback 到 in-process。
- **不要**让 worker `import spica` 主包。
- raw `pip freeze` 含机器专属路径（如 `PyAudio @ file:///home/conda/feedstock_root/...`），**只留 scratchpad，不入库**；如需入库须先清洗成无本机路径/无 token 的摘要（即本文档的版本表）。

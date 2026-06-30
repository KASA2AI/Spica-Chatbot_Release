# Clean-Room Env Reproducibility Spike（RVC Isolation Step3.0b）

> 配套：`docs/LOCAL_RUNTIME_RVC_ENV_REALITY.md`（golden env 基线）、`docs/LOCAL_RUNTIME_PLAN.md`。
> **目的**：用全新 conda env 模拟用户从零安装，验证 Spica/GPT-SoVITS/RVC 的最小运行依赖能否复现。**golden `gptsovits` env 全程未改、只作对照。**
> 采集：2026-06-30，Python 3.11.15，RTX 4090 / driver 555 / CUDA 12.5。**未改 production 代码、未改 golden env。**

---

## 1. 结论摘要

- **RVC 推理：可复现 ✅**——干净 env 里跑通了一次真实 RVC 推理（短 vocal → `rvc.wav` 638KB），而且**跑在 numpy 2.4.6 上**（RVC 对 numpy 版本不敏感）。
- **separator：可复现 ✅**——`audio-separator` + `onnxruntime-gpu` import 通过，GPU provider = TensorRT/CUDA/CPU。
- **GPT-SoVITS：不可平凡复现 ❌**——卡在多语言 g2p 依赖树（`jieba`/`pypinyin`/`cn2an`/`opencc` 中文、`g2pk2`/`ko_pron`/mecab-ko 韩文、`g2p_en`、`ToJyutping` 粤语）+ ASR（`funasr`/`modelscope`/`ctranslate2`）+ 训练相邻（`pytorch-lightning`/`peft`/`x-transformers`），**全在 inference_webui 模块级 import**，且 GPT-SoVITS 钉 `numpy<2`。
- **核心冲突定位**：numpy 之争**只在 GPT-SoVITS(<2) vs audio-separator(≥2) 之间**。**RVC 本身两边都行**（实测 2.4.6 通过）。
- **重大洞见**：clean env（RVC+separator 用 numpy 2.x）**`pip check` 通过（rc=0）**；golden 之所以 `pip check` 红，正是因为它把 numpy 压在 1.26.4 喂 GPT-SoVITS、违反了 audio-separator 的 ≥2。→ **独立 env 不只是清污染，更是直接化解 numpy 冲突**：RVC/song env 用 numpy 2.x（pip-check 干净）、TTS env 用 numpy<2，各自自洽。

---

## 2. 实测命令摘要（按阶段）

```bash
conda create -n spica-clean-py311 python=3.11 -y          # sibling of gptsovits
# torch 匹配 golden 的 cu124（pytorch 官方 index，非镜像）
pip install --no-cache-dir torch==2.5.1 torchaudio==2.5.1 torchvision==0.20.1 \
    --index-url https://download.pytorch.org/whl/cu124      # 但它带进 numpy 2.4.4（未钉）
# 核心子集，numpy 压回 golden 的 1.26.4（清华镜像）
pip install --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple \
    numpy==1.26.4 scipy==1.17.1 librosa==0.10.2 soundfile==0.13.1 \
    transformers==4.50.0 gradio==4.44.1 faiss-cpu==1.14.2 \
    onnxruntime-gpu==1.26.0 torchcrepe==0.0.24 torchfcpe==0.0.4 ffmpeg-python==0.2.0 PySide6==6.11.1
pip install pyopenjtalk==0.4.1                              # 源码编译，成功
pip install audio-separator==0.44.2                        # ★ 把 numpy 1.26.4 强升到 2.4.6
pip install python-dotenv noisereduce pedalboard psutil    # RVC/Applio 模块级缺口
pip install tensorboard wget beautifulsoup4                # RVC core.py→launch_tensorboard 模块级缺口
```

镜像：torch 走 pytorch 官方 index（慢但只此一次）；其余走**清华 TUNA**（USTC 出现 IncompleteRead 断流 + 缺 pyncm，已换）。

## 3. 关键包版本（clean env 最终态）

`python 3.11.15` · torch **2.5.1+cu124** · numpy **2.4.6**（被 audio-separator 升上去）· scipy 1.17.1 · librosa 0.10.2 · transformers 4.50.0 · gradio 4.44.1 · faiss-cpu 1.14.2 · onnxruntime-gpu 1.26.0 · torchcrepe 0.0.24 · torchfcpe 0.0.4 · pyopenjtalk 0.4.1 · audio-separator 0.44.2 · PySide6 6.11.1。env 体积 **7.7G**。

## 4. 安装失败 / 调整记录

| 现象 | 处置 |
| --- | --- |
| `torch` 带进 `numpy 2.4.4`（未钉 numpy） | 核心批次显式钉 `numpy==1.26.4` 压回 |
| **`audio-separator==0.44.2` 强升 `numpy 1.26.4→2.4.6`**（声明 `numpy>=2`） | 这是**核心冲突的实锤**：单 env 装不下 GPT-SoVITS(<2)+audio-separator(≥2) |
| RVC import 链缺 `python-dotenv` / `noisereduce` / `pedalboard` / `tensorboard` / `wget` / `beautifulsoup4` | 逐一补齐（grep 模块级 import 精确定位，非乱试） |
| GPT-SoVITS import 缺 `psutil`→`jieba`→…（多语言 g2p 全树） | 子集不够；需 GPT-SoVITS 全 requirements（含韩文 mecab 系统库） |
| `pyncm==1.8.1` USTC/清华/默认 index 都没有 | 非推理依赖（网易云下载），跳过；golden 来自 pythonhosted 直链 |
| USTC 镜像 IncompleteRead 断流 | 换清华 TUNA |

## 5. `pip check` 结果 → **通过（rc=0，No broken requirements found）**

**与 golden 相反**：golden `pip check` 红（audio-separator 要 numpy≥2 但被压 1.26.4）；clean env 让 audio-separator 拿到 numpy 2.4.6 → 干净。这恰好证明：**冲突的根在 numpy 轴，谁让步谁 pip-check 干净**。

## 6. smoke 结果

| 类 | 结果 | 证据 |
| --- | --- | --- |
| **A imports** | 除 `pyncm`(镜像缺) 全部 OK | torch/numpy/faiss/onnxruntime/audio_separator/torchcrepe/torchfcpe/PySide6/pyopenjtalk/gradio/transformers… |
| **B GPT-SoVITS** | ❌ 卡 `jieba`（多语言 g2p 模块级树，子集装不全） | `inference_webui` import 链需中/韩/英/粤 g2p + ASR + 训练相邻库 |
| **C RVC** | ✅ **跑通**，产出 `rvc.wav` 638KB，**numpy 2.4.6 下成功** | infer_spica_vocal 全链：contentvec/rmvpe/faiss-index/RVC-model 全加载 |
| **D separator** | ✅ import OK，provider = TensorRT/CUDA/CPU | onnxruntime-gpu 1.26.0 |

## 7. 必答结论

```
当前 Spica 能否从 clean env 复现？
  - RVC：能（独立装得起来，且 numpy 2.x 也能跑）。
  - separator：能。
  - GPT-SoVITS：不能平凡复现——需要其完整多语言 requirements（含 mecab-ko 系统库 + ASR 重依赖），
    且属 numpy<2 一侧。
  - 三者塞进单 env（=golden）：不能干净复现——GPT-SoVITS(<2) 与 audio-separator(≥2) 的 numpy 冲突；
    golden 只能靠压 numpy<2 + 违反 audio-separator 声明（→pip-check 红）这种手工违约态存活。

卡在哪里？
  - 不是模型路径、不是 CUDA、不是系统库（RVC/separator 都通了）。
  - 是 ① 依赖完整性（子集远不够，GPT-SoVITS/Applio 大量模块级依赖）
       ② 依赖版本冲突（numpy<2 vs ≥2）
       ③ 缺正式 requirements / install script（项目没有可复现安装脚本）。

same-env subprocess 还是下一步吗？
  - 仍可作为 Step3.1（先清进程污染、低风险），但本 spike 把独立 env 的理由从"清污染"升级为
    "化解 numpy 冲突"——独立 RVC/song env 用 numpy 2.x 是 pip-check 干净的，反而比 golden 更稳。
  - 必须补 requirements / install script：是。这是复现的前置，不分单 env / 独立 env。
```

## 8. 对路线的影响

| 问题 | 结论 |
| --- | --- |
| clean env 能否作为用户环境基础 | RVC/song 子集：能（且 pip-check 干净）。全 Spica（含 TTS）：需补 GPT-SoVITS 全多语言 requirements，未完成 |
| 是否继续单 env | 单 env 是 golden 的脆弱违约态；**不建议作为分发基础** |
| 是否仍建议独立 RVC env | **更建议了**——独立 env 让 RVC/separator 用 numpy 2.x（pip-check 干净），与 TTS 的 numpy<2 彻底解耦 |
| 独立 RVC env 该怎么建 | **不是 Applio full requirements，也不是 golden freeze**；而是 = torch2.5.1+cu124 + RVC 推理链实测依赖（contentvec via transformers、faiss、torchcrepe/torchfcpe、noisereduce、pedalboard、soxr、python-dotenv、tensorboard、wget、bs4）+ audio-separator（song 步），numpy 2.x 即可 |

## 9. 风险登记（更新）

| 级别 | 风险 |
| --- | --- |
| P0 | 无正式 requirements / install script → 任何 env 都不可复现（单 env 或独立 env 都要先补） |
| P0 | golden 单 env 是 numpy 违约态（pip-check 红），升级即可能崩 |
| P1 | GPT-SoVITS 多语言 g2p 树含**系统库**（mecab-ko 等），clean 复现需系统依赖，不只是 pip |
| P1 | GPT-SoVITS/Applio 大量**模块级** import（jieba/tensorboard/launch_tensorboard…）→ 子集装不全，必须补全树 |
| P2 | `pyncm` 不在 USTC/清华镜像（网易云下载，非推理；需 pythonhosted 直链） |
| P2 | 镜像稳定性（USTC IncompleteRead）→ 安装脚本应允许换源 |

## 10. 下一步建议

1. **先补 requirements / install script**（P0 前置）：
   - `requirements-tts.txt`（= GPT-SoVITS 全 requirements，numpy<2 侧，含系统依赖说明 mecab 等）；
   - `requirements-rvc.txt`（= 本 spike 实测的 RVC 推理链 + audio-separator，numpy 2.x 侧）；
   - 标注镜像可换、torch 走官方 cu124 index。
2. **Step3.1 same-env subprocess**（清进程污染）仍可先做、低风险。
3. **Step3.2 independent RVC env**（本 spike 已证明 pip-check 干净、numpy 解耦）——用 `requirements-rvc.txt` 建。
4. **Step3.3 slim runtime** 作为独立 env 的内容清单。

> 本 spike 不实现 slim / subprocess / 安装包；只验证复现性 + 定位卡点。

## 11. 边界确认

- golden `gptsovits` env 全程**未改**（numpy 仍 1.26.4，pip-check 仍按原态）。
- clean env `spica-clean-py311` 是独立实验 env（7.7G），失败未污染 golden。
- 未改 production 代码 / SongPipeline / TTS driver / RVC invocation / config / Applio / GPT-SoVITS。
- 未删除/移动任何模型。
- raw 安装日志含本机路径，留 scratchpad，不入库；本文档只含版本 + 结论（无本机路径）。

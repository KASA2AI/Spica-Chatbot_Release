# LOCAL_RUNTIME_PLAN.md

> Spica 第二阶段（工程化 / 本地推理重置）的**阶段宪法**。
> 配套 `CLAUDE.md`（每次都读的铁律与架构地图）与 `docs/DEVELOPMENT_GUARDRAILS.md`（落点与护栏）。
> 本文件的地位等同 `GALGAME_COMPANION_PLAN.md`：是这一阶段的**完整规格**，跨会话保持一致性。
> **冲突时**：`CLAUDE.md` §1「绝对铁律」不可被覆盖；本阶段的新约定以本文件明确写出的为准。
>
> **使用方式**：本阶段每一刀（每个模型）都新开一个 Claude Code 会话。每个会话**开头必须先读**：
> `CLAUDE.md` → `docs/DEVELOPMENT_GUARDRAILS.md` → 本文件（§3 硬约束 + §6 parity + §8 红线 + 当刀对应章节）。
> 不重复完整规格，只在提示词里指向本文件对应 §。
>
> **代码坐标核实纪律**：本文件中出现的具体文件路径 / 函数名 / 类名（如 `agent_assembly.py::build_agent_services`、`analyzer.py` 的 `import ocr_image`、`RapidOcrAdapter`）是基于某时点仓库阅读写下的。**每刀进入实现前，CC 必须先 `grep` 核实这些坐标仍然准确**，再据以落点。坐标若有出入，以真实代码为准、但处置方向（§9 / §11）不变。

---

## 目录

1. 文档目的与本阶段的真实目标
2. 名词与现状坐标（先认清已有什么 + 两条 OCR 路径）
3. 架构硬约束（本阶段不可破坏）
4. `local_runtime/` 目录结构
5. provider 命名规范
6. parity harness 规格（本阶段头号基建）
7. manifest、engine 与 `.gitignore` 规格
8. 红线清单（non-negotiables，每刀都适用）
9. 四模型难度评级与处置
10. 每刀固定工作流程
11. 第一刀：OCR 的具体范围
12. 配置层与守卫的衔接
13. 跨平台构建纪律（Windows 后置，但构建脚本前置跨平台）
14. Phase 拆解与排期
15. 开放问题（每刀进入前需确认）

---

## 1. 文档目的与本阶段的真实目标

### 1.1 一句话目标（措辞已校正）

本阶段**不是**「把模型 TRT 化」。本阶段是：

> **把本地推理能力从第三方工程依赖中抽离，形成 Spica 自己的 Local Runtime 层；能 TRT 的 TRT，不能 TRT 的先 ONNX Runtime / CTranslate2 / 原生 runtime 封装，最终对上层暴露统一 Port。**

TRT 是**手段**，不是目的。本阶段的奖品是**「摆脱 vendored 第三方工程依赖 + 固定部署边界」**，加速是顺带。

### 1.2 为什么这样定义（关系到 triage）

- 复杂部署依赖（vendored GPT-SoVITS 运行时、Applio、CUDA torch、transformers）的根源是「在跑原版 PyTorch 推理代码」。一旦导出 ONNX、用 ONNX Runtime 跑，这堆依赖大部分可以扔掉——**ONNX Runtime 单独就能达成主目标，且天然跨平台，为第三阶段 Windows 化铺路**。
- 因此分层是：**第一层 ONNX 化（真正的奖品，可移植、去依赖）→ 第二层 TRT（可选加速，套在 ONNX 之上）**。
- 这个分层的意义是**降险**：四个模型里有几个 TRT 化极难（自回归 / VLM），就算它们 TRT 失败，**用 ONNX Runtime / CTranslate2 仍拿到了「摆脱依赖」这个主目标**。**绝不把整阶段押在「全部 TRT 成功」上。**

### 1.3 本阶段不做什么

- **不在本阶段做 Windows 运行时 adapter**（window locator / capture / audio 的 Windows 实现）——整体后置到第三阶段（§13）。
- **不把点歌 / RVC（song）纳入本阶段主线**——标为 optional，不得阻塞主程序工程化（§9）。
- **不重构 `run_turn` / `orchestrator`**——本阶段零触碰核心控制流（§3）。前一轮已确认：核心链路不急、且 TRT 化不需要它先减重（模型在 port 后面，换引擎不动核心）。

---

## 2. 名词与现状坐标（先认清已有什么 + 两条 OCR 路径）

> 进入实现前必须认清：很多「该做的抽象」**已经存在**，本阶段是延续，不是从零造。**但 OCR 不是只有一条路径——这点我那版宪法漏了，本节补上。**

### 2.1 port / adapter / 实现体 / registry

- **port**（`spica/ports/`）= 接口契约。本阶段相关四个：`TTSPort`、`ScreenAnalysisPort`、`OCRPort`、`STTPort`。主链（`run_turn`/`orchestrator`/`AppHost`）只认这些 port。
- **adapter**（`spica/adapters/`）= 薄壳，把 port 接口翻译成对底层 pipeline 的调用。现有：`adapters/ocr/rapidocr.py`（即 `RapidOcrAdapter`）、`adapters/screen/local_moondream.py`、`adapters/stt/faster_whisper.py`、`adapters/tts/`。
- **实现体（现状所在）**：`agent_tools/function_tools/screen/`（RapidOCR backend + Moondream backend + analyzer + capture + model_manager）、`agent_tools/tts/vendors/GPT-SoVITS-...`（vendored 运行时）。
- **registry**（`spica/plugins/registry.py`）：`CapabilityRegistry`，注册 llm/tts/visual/memory/tool。**注意：STT 不在 registry 4 类里，是 `AppHost` 直接装配**。

### 2.2 OCR 当前有**两条**路径（第一刀必须同时处理，不能只覆盖一条）

这是本阶段第一刀最容易踩空的地方。当前 OCR 调用**不止 galgame 一条**：

```
路径 A（已封装，戴 OCRPort 帽子）：
  galgame OCR loop → OCRPort → RapidOcrAdapter → 底层 RapidOCR
  装配点：spica/host/agent_assembly.py::build_agent_services()，其中硬编码构造 RapidOcrAdapter

路径 B（未封装，绕过 OCRPort）：
  inspect_screen / manual screen analysis
    → agent_tools/function_tools/screen/analyzer.py 内 **直接 import ocr_image**
    → 绕过 OCRPort，直连底层 OCR
```

**后果（为什么必须一起处理）**：如果第一刀只把路径 A 切到新 runtime / 新 provider，**路径 B 仍跑老代码** —— 同一套 OCR 在生产里**实现分叉**：galgame 走新引擎、inspect_screen 走老 `ocr_image`。这既让 parity 只验了一半，也违背「OCR 推理实现单一来源」的工程化目标。**第一刀必须把路径 B 也收到 `OCRPort` / 新 factory 后面**，消除这个不一致（§11.1）。

> 这正是用户最初「工具该不该分层」的直觉**真实成立**的地方——成立在路径 B（inspect_screen 直 import），不在路径 A（galgame 已封装）。

### 2.3 装配点（既定，不再列为开放问题）

galgame OCR 的 provider 装配点是**确定的**：`spica/host/agent_assembly.py::build_agent_services()`，当前经 `build_ocr_adapter(...)` 按 typed config 选择 `OCRPort` adapter。

**第一刀的落点（已落地）**：在 `agent_assembly.py` 新增 `build_ocr_adapter(...)` 工厂，按配置 provider 名选择实现。schema built-in default 仍是 `rapidocr`（无配置文件 / 极限回滚 fallback），repo production default 已由 `data/config/app.yaml` 切到 `rapidocr_ort`，`fallback_provider` 仍是 `rapidocr`。新 provider（`rapidocr_ort` / `rapidocr_trt_ep`）均经此 factory 接入。**（进入后续刀前 grep 核实函数名与配置坐标仍准确。）**

### 2.4 关键认知

OCR 路径 A 已经三层封装（主链 →(`OCRPort`)→ adapter →(实现体)），路径 B 没有。本阶段**不是「把工具搬进某文件夹做分层」**（路径 A 已分好），而是：① 在 `local_runtime/` 写新推理实现，戴现有 port 帽子；② **顺手把路径 B 收到 port 后面**统一两条路径。`agent_tools` vs `spica/adapters` 的物理布局不一致属 P3，**不在本阶段单独搬目录**——实现体迁移由各刀在 `local_runtime/` 自然完成（§4）。

---

## 3. 架构硬约束（本阶段不可破坏）

1. **不新建第二套 port。** 上层认的是现有 `spica/ports/`（`TTSPort`/`ScreenAnalysisPort`/`OCRPort`/`STTPort`）。`local_runtime/` **只放推理实现 + 导出 + 构建脚本**，新实现戴现有 port 帽子。**绝不在 `local_runtime/` 下另起 `port.py` 当新契约**——那等于第二套接口层，违背「不另起平行体系」（`CLAUDE.md` 铁律 #7）。
2. **零触碰核心控制流。** `run_turn` / `orchestrator` / `stages` / `ChatEngine` 一行不改。TRT/ONNX 细节只活在 adapter 与 `local_runtime/`，上层永远不知道底层引擎是什么。
3. **`spica/local_runtime/` 是生产 runtime 代码，禁 `os.getenv` / `os.environ`。** `local_runtime/` 在 `spica/` 下，受 `CLAUDE.md` 铁律 #4 与 `test_no_getenv` AST 守卫管辖。**生产 runtime 代码（含 `device.py` 的 CUDA/TRT/ORT 探测）一律不读 env**，必须通过：① 注入的 typed config（`spica/config`）；② 函数参数；③ `import` 探测（如尝试 `import tensorrt` / 查 ORT providers）；④ `subprocess` 查询（如 `nvidia-smi`）；⑤ `platform` 模块。**只有 `scripts/local_runtime/` 下的 CLI 脚本**（不在 `spica/` 下、不受守卫管）**可以读 env**，但其 env 名必须**集中说明**（脚本顶部或 `doctor.py` 统一列出），且**绝不破坏 `test_no_getenv`**（守卫只扫 `spica/`，CLI 在 `scripts/` 故天然不触发——但仍不得把读 env 的代码塞回 `spica/local_runtime/`）。
4. **业务码不 `os.getenv`**（铁律 #4，§3.3 是它在本层的具体化）。新增配置走 `spica/config` 层（typed config 或 env_roster），不在 `local_runtime` 散落读 env。
5. **新能力走现有 ports / adapters / registry 风格**（`CLAUDE.md` 铁律 #7）。
6. **旧实现保留当 fallback**（§8），strangler-fig，验证通过前不删。
7. **测试命令固定 `python -m pytest tests -q`**（裸 `pytest` 会扫 vendored GPT-SoVITS 崩；这条在 vendored 删除前不变，§12）。

---

## 4. `local_runtime/` 目录结构

新建 `spica/local_runtime/`，作为**所有本地推理实现 + 导出 + 构建脚本**的统一新家。结构（先落 OCR，其余刀逐步填）：

```text
spica/local_runtime/
  __init__.py
  device.py            # GPU/CUDA/TRT/ORT 探测（禁 os.getenv，走 import/subprocess/platform/typed config，§3.3）
  errors.py            # 本层错误码（英文，§8.3）
  manifest.py          # manifest 解析/校验（§7）
  parity/              # parity harness（模型无关核心 + 可插拔比较器，§6）
    __init__.py
    harness.py         # run two providers on fixed inputs → comparison report
    report.py          # parity report 数据结构 + 序列化（gate 的依据）
    comparators.py     # text_diff / audio_diff（按模型插）
  ocr/                 # 第一刀
    __init__.py
    rapidocr_runtime.py   # 新推理实现（先 ONNX RT，后挂 TRT EP）
    rapidocr_build.py     # 构建/缓存脚本入口（跨平台写法）
  tts/                 # 第二刀（后续）
    ...
  vision/              # Moondream 隔离（后续，大概率不 TRT）
    ...
  stt/                 # faster-whisper：大概率零代码改动（§9），目录可空置或仅放说明
```

构建/校验脚本统一放（**这些在 `scripts/` 下，不在 `spica/`，故可读 env，但 env 名集中说明，§3.3**）：

```text
scripts/local_runtime/
  doctor.py            # 环境自检：CUDA / TRT / ORT / 驱动 / 模型存在（env 名在此集中说明）
  export_onnx.py       # 通用导出入口（按模型分派）
  build_trt.py         # 通用构建入口（ONNX → engine，目标机现编）
  verify_parity.py     # 跑 parity harness 产出报告（gate 的执行器）
  benchmark.py         # 旧 vs 新 耗时
```

adapter 落点不变：新实现的薄壳 adapter 仍在 `spica/adapters/<kind>/`（如 `adapters/ocr/rapidocr_ort.py`），戴现有 port 帽子。

---

## 5. provider 命名规范

- 现有 provider 名**保持原样**，作为 fallback：TTS = `gptsovits_current`；OCR = `rapidocr`（即现有 `RapidOcrAdapter`，进入前 grep 核实）；STT = `faster_whisper`。
- 新实现注册**新 provider 名**，与旧并存，app.yaml 切换。命名约定（英文）：
  - `rapidocr_ort` —— ONNX Runtime（CPU/CUDA EP）。**第一刀的主交付**；现已是 repo production OCR default，用来演练 provider seam / Path A+B 默认切换（不代表 OCR runtime dependency reduction 已彻底完成）。
  - `rapidocr_trt_ep` —— ONNX Runtime 的 TensorRT Execution Provider。**第一刀的第二步增强，仍 experimental**（§11.1）；真机 preflight 可用，但 cold cache / first new shape build 接近 70 秒，暂缓切默认。
  - `gptsovits_trt` —— GPT-SoVITS 新 runtime（TRT 稳定段 + ONNX RT 自回归段）。
  - `moondream_hf` —— Moondream 原样隔离（仍拖 transformers）。
- **fallback 配置形态**（落点见各刀，示意）：
  ```yaml
  ocr:
    provider: rapidocr_ort           # repo production default；trt_ep 暂缓
    fallback_provider: rapidocr      # legacy fallback 保留
  ```
- **命名即契约**：provider 名一旦写进 manifest / 配置 / 测试，不随意改名（改名属搬目录类高危，`CLAUDE.md` 铁律 #10）。

---

## 6. parity harness 规格（本阶段头号基建）

> parity harness 是本阶段**第一刀就要搭起来、四刀共用**的基建，也是质量回退的**唯一防线**。
> **现有 golden 测试钉的是事件流 / 结构，不是真模型的音质 / OCR 准确度**——切 TRT/ORT 后她声音变难听、OCR 错字变多，golden 照样全绿。没有 parity，质量回退**静默发生**。

### 6.1 parity gate（红线，措辞已按真实工程修订）

**本阶段铁律（与 `CLAUDE.md` §1 同效）：**

```
第一刀内部执行顺序：先 parity harness（含旧 vs 旧自验），后新 runtime 接入。

允许：实现 experimental provider（如 rapidocr_ort）+ 测试 factory（build_ocr_adapter），
      并让 parity harness 直接调用新实现做对比。

但在 parity 报告产出且达标之前，禁止：
  ① 切默认 provider（生产默认仍走旧实现）；
  ② 删除旧 provider；
  ③ 移除 fallback；
  ④ 让生产默认链路走新实现。

parity 报告是上述 ①②③④ 的唯一 gate。报告不存在或不达标 → 四者全禁。
```

**与旧版的区别（重要）**：旧版写「没有 parity 报告不准**注册**新 provider」，这卡死了流程（要跑新实现才能出报告）。修订版把 gate 从「注册」挪到「**切默认 / 删旧 / 移 fallback / 让生产默认走新实现**」——注册一个 experimental provider 本身无害（它不被默认选中），真正危险的是「让生产默认走没验证过的实现」和「删掉退路」。锁后者，放开前者。

**当前 OCR 状态（Runtime Cutover Rehearsal Step 3 / 3.1）**：`rapidocr_ort` 已通过默认切换 rehearsal，repo production default 为 `ocr.provider: rapidocr_ort`，`ocr.fallback_provider: rapidocr`。schema built-in default 仍保持 `OcrConfig().provider == "rapidocr"`，用于无配置文件 / 极限回滚。`rapidocr_trt_ep` 仍 experimental；虽已确认可实际跑 TensorRT EP，但 cold cache / first new shape build 接近 70 秒，需 cache/prewarm strategy 与真实 galgame parity 后再评估默认切换。

### 6.2 harness 设计（模型无关核心 + 可插拔比较器）

harness 核心是「**在固定参考输入集上跑两个 provider，产出对比报告**」，模型无关。每模型的**比较器**可插拔：

- **OCR**：`text_diff` —— 逐字符比对识别文本，per-input pass/fail + 聚合准确率 delta。
- **TTS**（后续刀）：`audio_diff` —— mel / waveform 误差（max / mean）；音质 MOS 不现实，逐波形/mel 差异 + 人耳抽检。
- **Screen/VLM**（后续刀）：`text_diff` 答案文本比对。

### 6.3 参考输入集

- 固定一组参考输入（**golden reference set**），版本化、可复现。
- **CI 用 mock / synthetic，绝不用真截图**（§6.5）。OCR 的 CI 参考集是合成图 / 固定 stub，不是真 galgame 截图。
- **真机** parity 的参考集可用真实素材（覆盖正常对白 / 名字框 / 长句 / 标点 / `【】` 等已知边界），但只在手动验收阶段用，不进 CI、不进 git。

### 6.4 parity 报告（gate 的依据）

报告（结构化、可被脚本判定 pass/fail）至少含：

- 每条输入：old 输出、new 输出、是否一致 / 误差值。
- 聚合：一致率 / 平均误差 / 最大误差。
- 耗时：每次推理 old vs new（ms）。
- 判定：pass / fail（按阈值，§15 待定每模型阈值）。

报告产物落 `artifacts/parity/<model>_<timestamp>.json`（**不进 git**，§7.3）。

### 6.5 parity 测试与守卫（CI **绝不**依赖真资源）

**红线**：parity 的 CI 测试**一律 mock / synthetic，绝不依赖真 galgame 截图 / 真 GPU / 真 OCR 模型**。理由：CI（以后迁 GitHub-hosted）根本没 GPU，依赖真模型直接没法跑。

- `test_parity_harness`：harness 自身正确性（用合成数据：两个相同 stub provider → 差异≈0、报告字段完整）。
- `test_ocr_comparator`（第一刀）：OCR `text_diff` 比较器在合成样本上的判定逻辑（含一致 / 错字 / 多字 / 少字）。
- **真机 parity**（真 OCR + 真 GPU + 真 TRT/ORT）= **手动验收项**（像真机 OCR 一样不自动化），但**报告必须产出并归档**（`artifacts/parity/`）作为 §6.1 gate 的证据。

---

## 7. manifest、engine 与 `.gitignore` 规格

### 7.1 不提交 engine，提交 build system

> **TRT serialized engine 不跨平台、不跨 GPU 架构**（不开硬件兼容模式时连架构都不跨）。Linux 4090 编的 `.engine` 在用户 Windows 别的卡上根本加载不了。

**红线**：

- **永远发 ONNX（可移植）+ 在用户/目标机现场编译 TRT**。**绝不发预编译 engine**（除非作为「已知完全相同环境」的可选快路径）。
- **engine / plan / onnx artifact / timing cache 一律不进 git**（大文件 + 不可移植），见 §7.3 的 `.gitignore`。
- 「让别人拉下来自己 ONNX → TRT」= 实际在**交付一套构建系统**（比「写个打包脚本」重）：托管、`doctor.py` 检测 GPU/TRT/CUDA 并优雅失败、版本兼容矩阵。

### 7.2 manifest 字段（英文）

`models/manifest.yaml`（或每模型 `export_config.yaml`），每个模型条目至少：

```yaml
models:
  rapidocr_det:                      # model_id（英文）
    source: <path-to-source-or-onnx>
    onnx: artifacts/onnx/rapidocr_det.onnx
    engine_cache_dir: artifacts/trt/
    precision: fp16                  # fp16 | int8 | fp32
    dynamic_shapes:                  # min / opt / max
      input: [[1,3,32,32],[1,3,48,320],[1,3,48,1280]]
    checksum: <sha256-of-onnx>
    min_cuda: "12.x"
    min_tensorrt: "10.x"
    gpu_arch_hint: "sm_89"           # 仅提示；真实由目标机探测
```

engine 缓存 key 必须含：**os + gpu_arch + tensorrt_version + cuda_version + precision + shape_profile + model_checksum**。任一变化即失效重编。ONNX Runtime TensorRT EP 的 engine cache 同理（model / ORT / TRT / 硬件变化即失效）——OCR 这刀的 TRT EP 步骤优先用 ORT 的 TRT EP + engine cache + timing cache（session 创建从分钟级降到秒级），大概率**不用手动 `trtexec`**。

### 7.3 `.gitignore` 规则（本阶段必加）

第一刀建立 `local_runtime` 时，**必须**在 `.gitignore` 加入以下规则（artifact 全部不进 git）：

```gitignore
artifacts/onnx/
artifacts/trt/
artifacts/parity/
artifacts/benchmarks/
*.engine
*.plan
*.timing.cache
```

### 7.4 「摆脱」的是代码不是权重

「摆脱 vendored」摆脱的是**vendored 运行时代码**，不是模型权重。ONNX 是从特定权重（如 GPT-SoVITS `v2pro-20250604`）导出的，**manifest / 文档必须钉死导出自哪个版本权重**——换版本架构变了，导出脚本会崩。

---

## 8. 红线清单（non-negotiables，每刀都适用）

每一刀提示词都引用本节。违反任一条即破坏本阶段架构：

1. **不新建第二套 port**；新实现戴现有 `spica/ports/` 帽子（§3.1）。
2. **零触碰 `run_turn` / `orchestrator` / `stages` / `ChatEngine`**（§3.2）。
3. **`spica/local_runtime/` 生产 runtime 代码禁 `os.getenv` / `os.environ`**；探测走 typed config / 参数 / `import` / `subprocess` / `platform`；只有 `scripts/` 下 CLI 可读 env 且 env 名集中说明（§3.3）。
4. **parity gate**：允许 experimental provider + 测试 factory；但无 parity 报告，**不切默认、不删旧、不移 fallback、不让生产默认走新实现**（§6.1）。
5. **旧 provider 保留当 fallback**，真机 parity 过前**不删旧实现**（strangler-fig）。
6. **engine / onnx / timing cache 不进 git**（§7.3 的 `.gitignore`）；只发 ONNX + 目标机现编，绝不发预编译 engine（§7.1）。
7. **manifest 记 os / gpu_arch / tensorrt / cuda / precision / shape_profile / checksum**（§7.2）。
8. **构建脚本跨平台写法**（路径、GPU/CUDA/TRT 探测不假设 Linux）——这是**写法纪律**，不是做 Windows adapter（§13）。
9. **第一刀必须统一两条 OCR 路径**（路径 A galgame + 路径 B inspect_screen），不能只覆盖 galgame（§2.2 / §11.1）。
10. **不删/不放宽守卫测试**（`test_layering` / `test_no_getenv` / `test_resolved_config_equivalence` / `test_registry` 等）来让测试变绿。
11. **不大范围搬目录/重命名**（`agent_tools` vs `adapters` 布局不一致属 P3，本阶段不单独整理；实现体迁移由各刀在 `local_runtime/` 自然完成）。
12. **同一刀只动一个模型**，验证通过再下一刀（用户定的节奏）。
13. **不同时 Windows 化 + TRT 化同一模块**（两个变量一起变，出问题分不清根因）。

### 8.3 错误码（英文，本层统一）

`local_runtime/errors.py` 定义，surface 经现有 `ToolError` / `OCRPort` 契约信封（每刀确认映射）：

```
LOCAL_RUNTIME_MODEL_NOT_FOUND
LOCAL_RUNTIME_ONNX_MISSING
LOCAL_RUNTIME_ENGINE_BUILD_FAILED
LOCAL_RUNTIME_DEVICE_UNSUPPORTED       # 无 CUDA / TRT / 驱动不符
LOCAL_RUNTIME_INFERENCE_FAILED
LOCAL_RUNTIME_PARITY_FAILED
```

---

## 9. 四模型难度评级与处置

| 模型 | 现状实现 | 适合 TRT？ | 本阶段处置 | 刀序 |
|---|---|---|---|---|
| **RapidOCR** | `rapidocr_onnxruntime`（**已是 ONNX**），有 `_INFER_LOCK` 全局推理锁 + CUDA lib preload；**两条调用路径**（§2.2） | 中（ORT 的 TRT EP 即可） | **第一刀**。`rapidocr_ort` 已切为 repo production OCR default，用于 provider seam / Path A+B default cutover rehearsal；legacy `rapidocr` 仍是 fallback。`rapidocr_trt_ep` 为第二步增强且仍 experimental，因 cold cache / first new shape build 约 70 秒，暂缓默认切换。 | 1 |
| **GPT-SoVITS TTS** | 直接 import vendored `GPT_SoVITS.inference_webui`，改 `sys.path` + pushd | 部分（vocoder/decoder 适合；**AR semantic 段难**：动态长度 / 采样 / KV cache） | **第二刀**，且**分段**：阶段 A 脱 vendor 不 TRT（在 `local_runtime` 重写最小推理图，输出 parity 一致）→ 阶段 B 分模块导 ONNX → 阶段 C 只 TRT 稳定段（vocoder/decoder），**AR 段停在 ONNX RT**。最高价值、最难。 | 2 |
| **faster-whisper STT** | `spica/adapters/stt/faster_whisper.py` + `SttConfig`（**已抽离**，模型常驻 / 懒加载 / 单 worker） | **不建议** | **大概率零代码改动**。CTranslate2 已是优化 runtime，性价比高于自导 Whisper 到 TRT。本阶段只**确认它在概念上纳入 local_runtime 伞下**（保持 CTranslate2 不动），可能仅文档登记。**注意：此模型的抽离前几轮已完成（STT 端点判定调参那条线就在它上面），勿当成待做项重做。** | 3（确认 no-op） |
| **Moondream 屏幕理解** | `transformers.AutoModelForCausalLM` + `trust_remote_code=True`（VLM，自定义前向在 HF 仓库 remote code 里） | **难** | **最后一刀**。第一阶段**先不 TRT、连 ONNX 都先别碰**：原样包成 `moondream_hf` runtime 隔离掉，接受它暂时仍拖 transformers。**它是低频用**（选项定位 / 画面判断），拖的依赖不值得第一阶段研究级精力去拔。**TRT 与否的最终决定，延到它这一刀、实际去 HF 仓库看 `modeling_*.py` 自定义前向逻辑多少再拍板**（大概率结论：隔离 + 不 TRT；可选评估换更易导出的小 VLM）。 | 4 |
| **RVC / song（点歌翻唱）** | 网易云搜索/下载 + 伴奏分离 + Applio/RVC + 混音 | 不建议第一阶段 | **砍出主线**，标 optional，不阻塞主程序工程化。TRT 投入产出比最低（高难度 + 非核心）。 | 主线外 |

**刀序总览**：OCR → GPT-SoVITS（分 A/B/C 子阶段）→ faster-whisper（确认 no-op）→ Moondream（隔离）。RVC 不进主线。

---

## 10. 每刀固定工作流程

> 沿用前几轮已跑顺的「提示词 → CC 出计划 → 你确认 → 实现 → 验证 → 下一刀」循环，本阶段在「出计划」这一步加重。

1. **先读**：`CLAUDE.md` → `docs/DEVELOPMENT_GUARDRAILS.md` → 本文件（§3 + §6 + §8 + 当刀章节）。
2. **先 grep 核坐标**：核实本文件给的代码坐标（函数/类/文件名）仍准确（文件顶部「代码坐标核实纪律」）。
3. **先确认装配点**：galgame OCR 装配点已定（§2.3 = `agent_assembly.py::build_agent_services` 硬编码 `RapidOcrAdapter`）；其余刀的装配点进入前读代码确认。
4. **先出计划，不改代码**：输出 需求理解 / 影响范围 / 推荐落点 / 不碰的边界 / 最小步骤 / 测试计划 / 风险（P0–P3）。**第一刀的计划必须先把 parity harness 设计讲清，等确认后才碰模型**（§6.1）。
5. **parity-first 实现**：先搭/复用 parity harness（旧 vs 旧自验，合成数据）→ 再做新推理实现（experimental provider + factory，可被 harness 调用）→ 用 parity 比对新旧 → 报告达标 → 才切默认 / 才动 fallback（§6.1）。
6. **验证**：parity 报告归档（`artifacts/parity/`）+ `python -m pytest tests -q` 全量 + 守卫绿 + 配置改动 `dump_resolved_config --diff`（若碰配置层）。
7. **放行核对**（人工）：`git diff` 范围是否如计划、`spica/` 核心零改动、守卫绿、parity 报告存在且达标、旧 provider fallback 仍在、`.gitignore` 已加（第一刀）。
8. **下一刀**。

---

## 11. 第一刀：OCR 的具体范围

### 11.1 本刀交付的本质：一条可复用的机制（不只是「OCR 变快」）

第一刀用 RapidOCR（四个里唯一**本身已是 ONNX**、最低难度、最高频、最易验证）把**整条机制**跑通并打磨好，后面三刀套用。**本刀重心是 runtime 抽离 + 统一两条路径，TRT EP 是第二步，不一次承诺。**

1. `spica/local_runtime/` 新家建起（`device.py` / `errors.py` / `manifest.py` / `parity/` + `ocr/` 落地）。`device.py` 探测**禁 os.getenv**（§3.3）。
2. **`.gitignore` 加 artifact 规则**（§7.3）。
3. **parity harness 搭起并自验**（合成数据，两个相同 stub provider ≈0 差异，证明基建对）——**先于**新 runtime 接入。
4. **新增 `build_ocr_adapter(...)` factory**（在 `agent_assembly.py`），按 provider 名选择；schema/factory fallback default 保持 `rapidocr`，repo production default 由 `data/config/app.yaml` 选择 `rapidocr_ort`（§2.3）。
5. **统一两条 OCR 路径**（§2.2 红线 #9）：
   - 路径 A（galgame）：经新 factory 选 provider。
   - 路径 B（inspect_screen / `analyzer.py` 直 `import ocr_image`）：**把它的 OCR 调用也收到 `OCRPort` / 新 factory 后面**，不再直连底层。**inspect_screen 的对外行为不变**（只是 OCR 调用换了内部来源），其余 screen analyzer 逻辑（VLM 定位 / 画面判断）不动。
6. **`rapidocr_ort`（ONNX Runtime）作为第一刀主交付**：把 RapidOCR 推理抽进 `local_runtime/ocr/rapidocr_runtime.py`，新 adapter 戴 `OCRPort` 帽子，经 factory 接入；现已作为 repo production default 覆盖 Path A+B 的默认链路。
7. **parity 验证**：固定参考集（CI 合成 / 真机真图分离），跑旧 `rapidocr` vs 新 `rapidocr_ort`，逐字比对 + 耗时，报告归档。
8. **`rapidocr_trt_ep`（TRT EP）第二步增强**：已完成真机 preflight，但 cold cache / first new shape build 接近 70 秒；在 cache/prewarm strategy 与真实 galgame parity 过关前，**不切默认**。
9. **切默认的 gate**：`rapidocr_ort` 的 repo production default cutover 已完成；之后若考虑 `rapidocr_trt_ep`，仍需单独 parity / latency / cache gate。**旧 `rapidocr` 保留 fallback**（§6.1 / 红线 #4/#5）。
10. 构建脚本（`rapidocr_build.py` / `scripts/local_runtime/*`）按**跨平台写法**（§13）。

### 11.2 本刀不碰

- ❌ `agent_tools/function_tools/screen/` 的 **Moondream backend**（与 OCR 无关，留到第四刀）；`analyzer.py` 里**只改 OCR 调用来源**（路径 B 收进 port），不动 VLM/画面判断逻辑。
- ❌ TTS / STT / song 任何代码。
- ❌ `run_turn` / `orchestrator` / `stages` / `ChatEngine` / `spica/` 核心控制流。
- ❌ 配置解析逻辑——除非 OCR provider 选择确实需要碰配置层；若需要，**改前 `dump_resolved_config --out before`、改完 `--diff`**。
- ❌ 不删旧 `rapidocr` provider（fallback）。
- ❌ 不在 `spica/local_runtime/` 写任何 `os.getenv`（§3.3）。

### 11.3 本刀测试（CI 全部 mock/synthetic，不依赖真资源）

- `test_parity_harness` / `test_ocr_comparator`（新增，合成数据，§6.5）。
- `test_local_runtime_ocr_contract`（新 adapter 满足 `OCRPort`）。
- `test_build_ocr_adapter`（factory：默认 `rapidocr` 零 diff；按名选 experimental provider）。
- `test_local_runtime_manifest`（manifest 解析/校验，若本刀引入 manifest）。
- 守卫回归：`test_layering` / `test_no_getenv`（**确认 `local_runtime` 没新引入 os.getenv**）/（若走 registry）`test_registry` /（若碰配置）`test_resolved_config_equivalence`。
- **路径统一回归**：补一个测试证明 inspect_screen / 路径 B 的 OCR 现在也经 `OCRPort` / factory（不再直 `import ocr_image` 绕过）。
- 真机 parity（真 OCR + 真 GPU + 真 ORT/TRT engine）= 手动验收，报告归档作 gate。
- 收尾：`python -m pytest tests -q` 全量。

---

## 12. 配置层与守卫的衔接

- 新增本地推理配置（provider 名、precision、模型路径等）走 `spica/config`：UI 无关的运行配置进 typed config（`schema.py` 加字段，默认=旧值零 diff）或 env_roster；**绝不业务码 `os.getenv`**（§3.3）。
- **`runtime_env.py` 的退役（TTS 刀的事，先记不做）**：`runtime_env.py` 是三个 `os.getenv` allowlist 文件之一，**为 vendored GPT-SoVITS 运行时的 env 写而存在**。等第二刀真把 vendored TTS 删掉，`runtime_env.py` 可能可移除——但这会动到配置守卫（`test_no_getenv` 的 allowlist floor、`test_env_centralization`）。**这是第二刀（TTS）收尾的衔接点，不在第一刀（OCR）范围**，先登记。
- **裸 `pytest` 限制的解除（同样 TTS 刀）**：现在必须 `python -m pytest tests -q`，正是因为裸 `pytest` 会扫 vendored GPT-SoVITS 崩。vendored 删除后此限制可评估解除——**同属第二刀收尾，第一刀不动**。

---

## 13. 跨平台构建纪律（Windows 后置，但构建脚本前置跨平台）

> **关键区分**：Windows 运行时 adapter（window locator / capture / audio）**整体后置第三阶段**；但**构建脚本的跨平台写法从第一刀就要做**。两者不是一回事。

- **后置（第三阶段做）**：`window_locator/windows_win32.py`、Windows screen capture（DPI/黑屏/多显示器）、Windows 音频输入（无 ReSpeaker 时软件 VAD + faster-whisper 走主路径，ALSA→WASAPI/PyAudio）。这些是**整个 adapter 层**，本阶段不碰。
- **前置（第一刀就做，纪律性）**：构建/编译脚本（`build_trt.py` / `device.py` / engine cache key）里——路径不写死 `/`（用 `pathlib`）、GPU/CUDA/TRT 探测不假设 Linux（不写死 `nvidia-smi` 路径假设、用 `platform` 分支）、cache key 带 os+arch（§7.2）。这几乎免费（只是写法意识），但能让第三阶段 Windows 化时这套构建脚本**直接复用、不重写**。
- 注意 §3.3：探测**禁 os.getenv** 与「跨平台写法」并行——跨平台探测走 `platform`/`subprocess`/`import`，不是走 env。
- 理由：TRT engine 不跨平台（§7.1），「用户自己 ONNX→TRT」的脚本若第一刀写成 Linux-only，第三阶段得整套重写。**构建跨平台 ≠ 做 Windows 适配**——前者是写法习惯，后者是后置的 adapter 工程。

---

## 14. Phase 拆解与排期

| 子阶段 | 内容 | 关键交付 | gate |
|---|---|---|---|
| **2.1 OCR（第一刀）** | parity harness 搭起 + 统一两条 OCR 路径 + RapidOCR 抽进 `rapidocr_ort`（TRT EP 第二步） | `local_runtime/{device,errors,manifest,parity,ocr}` + `build_ocr_adapter` factory（schema fallback default 仍 rapidocr）+ 路径 B 收进 port + 新 adapter（戴 `OCRPort`）+ `.gitignore` artifact 规则 + 构建脚本（跨平台写法）+ fallback 配置；repo production default 已切 `rapidocr_ort` | `rapidocr_ort` default cutover 已完成；`rapidocr_trt_ep` 默认需另过 cache/prewarm/parity gate |
| **2.2 GPT-SoVITS（第二刀）** | 阶段 A 脱 vendor 不 TRT → B 分模块导 ONNX → C 只 TRT 稳定段 | `local_runtime/tts/*` + `gptsovits_trt` provider + parity（mel/waveform）；收尾处理 `runtime_env.py` 退役 + 裸 pytest 限制 | 每子阶段各自 parity 过 |
| **2.3 faster-whisper（第三刀）** | 确认 no-op，纳入概念伞 | 文档登记，保持 CTranslate2 不动 | 确认无行为变化 |
| **2.4 Moondream（第四刀）** | 原样隔离成 `moondream_hf`，TRT 与否当刀再决 | `local_runtime/vision/*` + `moondream_hf` provider；隔离 transformers 依赖 | 隔离后行为 parity 一致 |
| **主线外** | RVC/song 标 optional | 不阻塞主程序 | — |
| **第三阶段** | Windows 运行时 adapter | window locator / capture / audio 的 Windows 实现 | 本文件外，另立计划 |

---

## 15. 开放问题（每刀进入前需确认）

每刀进入实现前，对应问题需先有答案（不要瞎定）：

1. **parity 阈值**：OCR 的一致率 / 误差判定阈值定多少为 pass？TTS 的 mel/waveform 误差阈值？（§6.4，每模型单独定，第一刀先定 OCR 的）
2. **golden reference set**：CI 用的合成 OCR 样本怎么造（覆盖一致/错字/多字/少字）？真机用的真实素材集（正常对白 / 名字框 / 长句 / 标点 / `【】` 边界）放哪、不进 git？（§6.3）
3. **ONNX 模型托管**：Git LFS / HF Hub / release artifact？（§7.1，影响「用户拉下来」的实际流程）
4. **GPT-SoVITS 权重版本**：导出钉死哪个版本（如 `v2pro-20250604`）？（§7.4）
5. **Moondream remote code 体量**：到第四刀实际读 HF 仓库 `modeling_*.py`，自定义前向逻辑多少 → 决定隔离 / ONNX / 换模型（§9）。

> **已从开放问题移除**（已在正文给定）：OCR provider 装配点（§2.3 已定为 `agent_assembly.py::build_agent_services`）；TRT EP 是否一次到位（§5/§11 已定为「先 ort 后 trt_ep」分步，不留作开放承诺）。

---

> **本阶段的核心不是「加速」，是「去第三方工程依赖 + 固定部署边界」。** 抓住这条，TRT / Windows / 打包安装后面才会真的变简单。
> 每刀小步、验证一个再做下一个、旧实现留 fallback、parity 先行、两条 OCR 路径一起统一——这套纪律与本仓库 C 硬化、galgame Phase 0 一脉相承。

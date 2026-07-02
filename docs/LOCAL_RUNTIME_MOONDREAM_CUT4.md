# Moondream local_runtime 隔离（Cut 4）收尾文档

> 父规划：`docs/LOCAL_RUNTIME_PLAN.md`。姊妹文档：`docs/LOCAL_RUNTIME_TTS_SLIM_B1.md`（GPT-SoVITS）、`docs/LOCAL_RUNTIME_RVC_SLIM_B1.md`（Applio/RVC）；OCR 两刀（rapidocr_ort / rapidocr_trt_ep）为同型前例。
> **状态：Cut 4 完成。`moondream_hf` provider 已实现，真机 parity PASS（BIT_IDENTICAL）。Runtime Cutover Rehearsal Step 1 已把 repo production default 切到 `moondream_hf`；schema built-in default 仍是 `moondream_local`，legacy `MoondreamBackend` 保留为 fallback。**
> 未做：ONNX / TensorRT / Photon / 换模型 / Windows / env / installer（见 §7）。

关键文件：

- 隔离运行时：`spica/local_runtime/vision/__init__.py`、`spica/local_runtime/vision/moondream_hf.py`
- install-hook 缝：`agent_tools/function_tools/screen/backends/moondream_runtime.py`
- 缝接入点：`agent_tools/function_tools/screen/model_manager.py`（backend-load 一层）
- factory：`spica/host/agent_assembly.py::build_moondream_provider`
- host 装配：`spica/host/app_host.py`（OCR install 之后的 Moondream install hook）
- parity：`scripts/local_runtime/verify_moondream_parity.py`（prepare / worker / compare / import-check）
- 测试：`tests/test_moondream_runtime.py` / `tests/test_build_moondream_provider.py` / `tests/test_moondream_hf_backend.py` / `tests/test_moondream_parity_harness.py`（另在 `tests/test_moondream_model_manager.py` 加 3 条 `_validate_config` 测试）

---

## 1. 本刀目标

这是「**原样隔离 + local_runtime/vision provider**」，**不是 slim、不是性能优化**：

- ❌ 不是加速（推理路径不变）。
- ❌ 不是裁剪（无 manifest、无 build slim 脚本；模型代码仍经 `trust_remote_code` 从 HF 取，权重在 HF cache）。
- ❌ 不是 ONNX / TensorRT / Photon / 换模型（全部不做）。
- ✅ **是**把现有 Moondream（屏幕视觉理解）推理实现从 `agent_tools/.../backends/moondream.py` 隔离进 `spica/local_runtime/vision/`，包成 `moondream_hf` provider，戴现有 `ScreenAnalysisPort` 帽子——架构对齐 + 依赖归位，与 OCR / TTS / RVC 三刀同一 local_runtime 布局。

不变量（用户批准的红线）：

- 继续使用 transformers + torch + `AutoModelForCausalLM.from_pretrained`。
- `revision` 固定 **`2025-06-21`**。
- `from_pretrained` 参数零 diff：`model_id`、`revision`、`trust_remote_code=True`、`device_map={"": "cuda"}`、`torch_dtype` 行为全部原样；**不加 `cache_dir`**、不加任何 determinism 设置。
- `spica/local_runtime/vision/` 内 env-free（无 `os.getenv` / `os.environ`）。

---

## 2. 架构落点

**范式** = OCR cut 1 的 factory + install-hook + experimental-provider + parity-gate + legacy-fallback，缝落在 **manager 的 backend-load 一层**（不新增第二套 port——`moondream_hf` 仍戴现有 `ScreenAnalysisPort` 帽子，`inspect_screen` / `watch_game_screen` 两个消费方链路不变）：

```
AppHost.initialize()
  └─ build_moondream_provider(self.screen_config.provider)     # spica/host/agent_assembly.py
       ├─ "moondream_local"（默认）→ None → host 不装 provider
       ├─ "moondream_hf"           → MoondreamHfProvider()
       └─ unknown                  → WARNING + fallback（默认回 moondream_local → None）
  └─ provider 非 None 才 set_active_moondream_provider(provider)

MoondreamModelManager.load()                                   # agent_tools/.../model_manager.py
  └─ load_moondream_backend(self._config)                      # moondream_runtime.py 缝
       ├─ 无 active provider → MoondreamBackend.load(config)   # legacy，精确原样（零 diff 默认）
       └─ 有 active provider → provider.load(config)           # → MoondreamHfBackend（逐字搬迁）
```

要点：

- **零 diff 默认（P0）**：provider 为 `moondream_local` 时 factory 返回 `None`，host 什么都不装，缝里 `MoondreamBackend.load(config)` 与刀前完全同签名、同参数、同顺序。
- **`moondream_hf` 路径**由 host（配置 `screen.provider: moondream_hf` 时）或 parity 脚本安装 active provider 后生效；`MoondreamHfBackend` = legacy `MoondreamBackend` 的 load/query/_query_model + `_torch_dtype`/`_result_to_text` **逐字搬迁**，唯一差异是 provider 门槛收 `moondream_hf`（+类名）。
- **provider 校验放行只在两处**（用户 Decision 3）：manager `_validate_config` 放行 `{moondream_local, moondream_hf}`；新 backend 自身门槛收 `moondream_hf`。legacy `MoondreamBackend.load` 的 `provider != "moondream_local"` 检查**未动**——`moondream_hf` 由缝路由到新 backend，不会进 legacy。
- **单例与串行语义保持**：`get_moondream_manager` 按 config 签名的进程级单例、`_infer_lock` 串行、不双加载全部未动（缝在 manager 内部，对外语义不变）。
- **schema 零改动**：`ScreenConfig.provider` 本就是自由 str，未加 `fallback_provider` 字段（fallback 只是 factory 默认参数），`test_resolved_config_equivalence` 不受影响。
- **核心控制流零触碰**：`run_turn` / `orchestrator` / `stages` / `ChatEngine` / `analyzer` / prompt 链一行未改。

---

## 3. parity 验证（legacy vs moondream_hf）

**姿态（用户 Decision 1）**：这是搬迁刀，不是导出刀。承重的等价保证是 CI 纯净的三件——import preflight + 缝零 diff 测试 + **代码等价**（AST 级钉死 `MoondreamHfBackend` 各方法与 legacy 字节等价，仅归一 provider 字面量 + 类名两处预期差异）；真机 GPU parity 是再上一道保险。bit-identical 优先，若 CUDA/采样致逐字节不一致则允许「结构等价 + 归一化文本相似度 ≥ 0.98」兜底——**绝不为追 bit-identical 改 `from_pretrained` 或加 determinism 设置**。

```bash
# 1. 生成固定合成桌面图 + 两侧 spec + 路径/import preflight（-B 子进程真 import moondream_hf）。无 GPU。
python scripts/local_runtime/verify_moondream_parity.py prepare

# 2. 两侧各自独立子进程走完整生产路径 analyze_screen_image_local（同图同 prompt 同 seed，OCR 关闭以隔离 Moondream）。
python scripts/local_runtime/verify_moondream_parity.py worker --spec artifacts/parity/moondream_cut4/spec_legacy.json
python scripts/local_runtime/verify_moondream_parity.py worker --spec artifacts/parity/moondream_cut4/spec_hf.json

# 3. 对比文本 + 结构 + 缝路由，出表 + verdict + report。无 GPU。
python scripts/local_runtime/verify_moondream_parity.py compare
```

**为什么必须独立子进程**：`get_moondream_manager` 缓存进程级单例，HF 模型 + transformers remote code 常驻 `sys.modules` / VRAM——一个进程先跑 legacy 再跑 hf 会复用第一次的模块与模型，测不到第二侧。

**真机结果**（gptsovits env，真实 moondream2，报告 `artifacts/parity/moondream_cut4/parity_report.json`）：

| 指标 | 值 |
| --- | --- |
| verdict | **PASS** |
| text_verdict | **BIT_IDENTICAL** |
| similarity | **1.0000** |
| structural_equal | True（同 observation schema / engine 标签 / error 形状） |
| no_errors | True |
| seam_routed | True（legacy 侧 installed=None，hf 侧 installed=MoondreamHfProvider——缝真实生效） |
| both_nonempty | True |

符合预期：`moondream_hf` 就是 legacy `from_pretrained` 路径逐字搬迁，同 seed 下拿到了最强结果。parity artifact（图片 / spec / result / report）全在 `artifacts/parity/`，**gitignored，不入库**。

---

## 4. 测试结果

```
python -m pytest tests -q
1071 passed, 1 warning, 108 subtests passed
```

（基线 RVC 收尾时 1035，本刀 +36；1 warning 为既有 librosa deprecation。）

守卫单独复跑：

```
python -m pytest tests/test_layering.py tests/test_no_getenv.py tests/test_resolved_config_equivalence.py -q
47 passed, 15 subtests passed
```

**未放宽任何守卫**：`test_layering` / `test_no_getenv` / `test_resolved_config_equivalence` 原样在位；`spica/local_runtime/vision/` grep 复核零 env 读取。新增测试覆盖：

- `test_build_moondream_provider.py` — factory：默认/`moondream_local` → None；`moondream_hf` → provider；unknown → fallback + warning；blank → None。
- `test_moondream_runtime.py` — 缝零 diff：无 provider 时 `MoondreamBackend.load` 被以**同一 config 原样**调用；有 provider 时路由 + legacy 不被碰；另有 manager 穿缝集成（fake torch/transformers）。
- `test_moondream_hf_backend.py` — AST 代码等价（load/query/_query_model + 两 helper vs legacy）+ 行为（provider 门槛、`from_pretrained` 钉参：model_id / revision `2025-06-21` / trust_remote_code / device_map / torch_dtype、CUDA 不可用报错）。
- `test_moondream_parity_harness.py` — compare 判定逻辑（bit-identical / normalized / similar / divergent / 结构不符 / 有错 / 缝未路由 / 双空 / 缺输出），CI 纯净无 GPU。
- `test_moondream_model_manager.py` — +3 条 `_validate_config`（放行两值、拒绝 unknown）。

---

## 5. 明确未做事项（不在本刀范围）

```
Cut 4 当时不切默认（已由 §8 的 Runtime Cutover Rehearsal Step 1 切到 moondream_hf）
不删 legacy MoondreamBackend
ONNX / TensorRT / Photon（不做）
Windows dependency spike（不做）
Windows adapter（不做）
env / install / package（不做）
TTS / RVC / song（不碰）
```

---

## 6. 风险 / 挂账

1. **install hook 是进程全局态**：`moondream_runtime.py` 的 `_ACTIVE_MOONDREAM_PROVIDER` 为模块级全局（与 OCR 缝同型）。测试/脚本用完必须 `reset_active_moondream_provider()`（现有测试与 parity worker 已遵守此纪律）。
2. **删 legacy 需另立刀**：`moondream_hf` 已过 parity，`screen.provider` 默认值已由 §8 单独切换；删除 legacy backend 仍属行为面变更，需单独 plan → 确认 → 实现 → 验收。
3. **真正下半场**（Windows dependency spike、可复现 env、切默认、删 vendored、installer）均不在本刀范围，见 `docs/LOCAL_RUNTIME_PLAN.md` 后续章节，各自立项。

---

## 7. 当前完成定义

> **Moondream Cut 4 is complete when the `moondream_hf` provider loads the verbatim `from_pretrained` path from `spica/local_runtime/vision/`, the default `moondream_local` path is byte-level zero-diff through the seam, and real-machine parity (legacy vs hf, same image/prompt/seed, full production path) passes.**

三条均已满足（§2 零 diff 默认 + 缝测试、§3 真机 BIT_IDENTICAL、§4 全量/守卫绿）。**Moondream Cut 4 完成——LOCAL_RUNTIME_PLAN 四刀（OCR / TTS / RVC / Moondream）全部收齐。**

## 8. Runtime Cutover Rehearsal Step 1

- `data/config/app.yaml` 的 live `screen.provider` 已切到 `moondream_hf`，repo production default 会在 host initialize 时安装 `MoondreamHfProvider`。
- `ScreenConfig.provider` 的 schema built-in default 保持 `moondream_local`：无配置文件 / 极限回滚场景仍落 legacy seam。
- `moondream_local` 和 legacy `MoondreamBackend` 继续保留；显式配置 `moondream_local` 时 host 不安装 provider，manager seam 仍走 legacy fallback。
- 未移动旧源码；未做 OCR / TTS / RVC / Windows / installer。

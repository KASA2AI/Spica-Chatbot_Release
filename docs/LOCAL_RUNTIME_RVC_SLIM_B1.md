# RVC (Applio) Slim Runtime — B1 收尾文档

> 父规划：`docs/LOCAL_RUNTIME_PLAN.md`。姊妹文档：`docs/LOCAL_RUNTIME_TTS_SLIM_B1.md`（GPT-SoVITS 同型裁剪）。
> 相关 env 线（暂停展开，留作依赖隔离/Windows/安装包参考）：`docs/LOCAL_RUNTIME_RVC_ENV_REALITY.md`、`docs/LOCAL_RUNTIME_CLEAN_ENV_SPIKE.md`、`docs/LOCAL_RUNTIME_RVC_INSTALL.md` + `requirements-rvc.txt`。
> **状态：功能完成，parity bit-identical，未接 production、未切默认。** 原始 Applio 仍是 fallback。

提交链（全部已 push 到 `origin/main`）：

| commit | 内容 |
| --- | --- |
| `2fd0e32` | RVC slim manifest + dry-run planner（Step1） |
| `59ad09c` | true build + import preflight（Step2A） |
| `d3fa122` | original-vs-slim wav-to-wav parity（Step2B） |

关键文件：
- 清单：`data/config/rvc_slim_manifest.yaml`
- 纯逻辑：`spica/local_runtime/rvc/slim_manifest.py`（**复用 TTS B1 的 generic glob/path-safety/size helpers**）
- builder：`scripts/local_runtime/build_rvc_slim.py`（dry-run 默认 + `--build` 真机 + `--force` + import preflight）
- parity：`scripts/local_runtime/verify_rvc_slim_parity.py`（prepare / worker / compare，独立子进程）
- 测试：`tests/test_rvc_slim_manifest.py` / `tests/test_build_rvc_slim.py` / `tests/test_rvc_slim_parity.py`

---

## 1. 结论摘要

- **RVC slim 功能完成。** Applio/RVC **12G → `artifacts/rvc_slim` 618M**，只保留「当前 spica 推理 load-bearing」的 runtime。
- **slim artifact 由脚本生成，不提交**（gitignored）；**原始 Applio 仍作为 fallback**，且字节未改动。
- **slim 与原始 Applio 在验证样本上输出 bit-identical**（同 sha256、RMSE 0）。
- **import preflight PASS**（独立 `-B` 子进程，无 `.pyc` 漏出）。
- **当前未接 production**（rvc.py invocation / SongPipeline / sing_song 全未改）、**未做 subprocess**、**未做 independent env**、**未做 Windows / install package**（见 §9 延期项）。

---

## 2. 体积结果

| 指标 | 值 |
| --- | --- |
| 原始 Applio | **~12G** |
| RVC slim artifact | **618M**（`du -sh`） |
| build_report total_bytes | **646,863,997** |
| build_report total_gb | **0.6024** |
| copied files | **47** |
| on-disk files | **48**（含 `build_report.json`） |

分类（build_report.categories）：

| category | 文件/大小 |
| --- | --- |
| runtime_model_embedder | 2f / **360.8MB**（contentvec pytorch_model.bin + config.json） |
| runtime_model_pitch | 1f / **172.8MB**（rmvpe.pt） |
| character_model | 1f / **52.7MB**（spica_200e_57000s.pth） |
| character_index | 1f / **30.1MB**（spica.index） |
| runtime_python | **35f / 0.3MB** |
| config | **5f / 0.2MB** |
| license | 2f / ~0MB |

体积 12G → 0.60G（≈ 20×）。

---

## 3. 保留内容（keep-list）

- `core.py`
- `rvc/configs/`（config.py + 24000/32000/40000/48000.json）
- `rvc/infer/`（infer.py / pipeline.py）
- `rvc/lib/`（utils + algorithm + predictors + tools + platform/zluda；含 tts_voices.json）
- `rvc/train/process/model_blender.py`、`rvc/train/process/model_information.py`
- `rvc/models/embedders/contentvec/`（pytorch_model.bin + config.json）
- `rvc/models/predictors/rmvpe.pt`
- character pack：`logs/spica/spica_200e_57000s.pth`、`logs/spica/spica.index`
- license / config 文件

> **重点（TTS-B1 同型陷阱）**：`rvc/train/process/` 下的 `model_blender.py` 与 `model_information.py` 虽在 train 目录，但被 `core.py` 的**模块级 import** 需要，因此必须保留。RVC Slim Step1 的 import-graph 实测（sys.modules diff）抓出了它们；import preflight（§6）再次以真实 import 兜底。

keep 是**白名单**：未匹配 keep 的文件一律不复制（11G 训练/scratch 因此被丢弃，无需逐条 exclude）。**不**用宽 `rvc/train/**` exclude（会 shadow 上面两个 must-keep → planner 会 abort）。

---

## 4. 排除内容（大头）

- 训练数据：`logs/*/sliced_audios`（5G）、`sliced_audios_16k`（573M）、`extracted`（1.4G）、`eval`、`f0_voiced`
- 训练 checkpoint：`logs/*/G_*.pth`、`D_*.pth`、多 epoch `*e_*s.pth`（除 active 200e）
- 未用 vocoder：`rvc/models/pretraineds/**`（1.3G，**推理零加载**——HiFi-GAN 在代码 + RVC .pth 内）
- 备用音高：`rvc/models/predictors/fcpe.pt`（42M，用 rmvpe）
- webui：`tabs/**`、`assets/**`、`app.py`
- 开发 scratch：`Applio/spica/`（fanal_output + 测试歌 + 测试输出，837M）、cache/outputs/temp
- `**/__pycache__/**`、`**/*.pyc`

---

## 5. build 命令

```bash
# dry-run（默认安全，不复制）
python scripts/local_runtime/build_rvc_slim.py \
  --manifest data/config/rvc_slim_manifest.yaml --character spica

# true build（显式 --build；已有 output 默认拒绝覆盖，--force 才覆盖）
python scripts/local_runtime/build_rvc_slim.py \
  --manifest data/config/rvc_slim_manifest.yaml --character spica --build --force
```

- **dry-run 默认**；**真机复制必须显式 `--build`**。
- **默认拒绝覆盖**已有 output（`BUILD ABORTED: ... use --force`）；覆盖必须显式 **`--force`**。
- artifact 输出到 `artifacts/rvc_slim`，**gitignored，不提交**。
- 安全：staging + atomic rename + 失败 rollback；source Applio **只读**；source/output realpath 双向 containment；required 缺失/keep 零命中/required 被 exclude shadow/output 非 gitignore → 全 abort。

---

## 6. import preflight

- 独立 **`-B` 子进程**（`build_rvc_slim.py --import-root <base>`），从 `artifacts/rvc_slim/base` 当 Applio root 复刻 `rvc.py::_load_core` exec `core.py`，触发全 RVC 推理 import 链。
- 目的：防止 module-level import 漏文件（TTS-B1 的 `tools.assets` 教训）。
- `-B`：不写字节码——否则 import 会在 staged base 写 `__pycache__/*.pyc`（在 copy/report 之后、rename 之前），漏进 artifact 且违反 manifest 的 `__pycache__` exclude。
- **本轮结果：PASS**（首次即过，keep-list 完整，无缺失模块）；**0 个 `.pyc`**；`build_report.import_preflight.status = PASS`。

---

## 7. parity 结果（original Applio vs slim）

```
Input         : cached separated 12s vocal sample（无下载、无 song pipeline、无网易云）
output original: 958444 B          output slim: 958444 B
length         : 479200  vs  479200   (equal)
sample_rate    : 40000   vs  40000
RMSE           : 0.000e+00          (gate <= 1e-3, noise floor 6.6e-4)
max_abs_diff   : 0.000e+00
mel_mean_db    : 0.0000
sha256         : identical  (d1e886d7...)
Gate           : PASS
Result         : BIT-IDENTICAL
```

- **比「噪声底内相等」更强**：相同字节 = 相同声音（RVC 推理确定性 + slim 是 sha256 校验过的字节级拷贝 + 同 input/params/seed）。
- 隔离：original 与 slim 各自**独立子进程**（rvc.py `_load_core` 全局缓存 core 模块、Applio import 树常驻 sys.modules，一进程装不下两 root）。
- wav / parity_report / artifacts 全 **gitignored，不提交**；原始 Applio 字节未改动。

---

## 8. 测试状态

```
python -m pytest tests -q
1035 passed, 1 warning, 108 subtests passed
```
（RVC slim 三测试文件：manifest 纯逻辑 + dry-run/true-build/import-check + parity harness；synthetic only，不依赖真实 Applio/torch/GPU/faiss。）

---

## 9. 明确延期项（**不在 RVC slim B1 范围**）

```
production wiring（接 rvc.py / driver）
SongPipeline switch（切默认走 slim）
subprocess worker（VoiceConversionPort）
independent RVC env（用 requirements-rvc.txt，numpy 2.x）
Windows compatibility
installer / package
dependency isolation
clean env rebuild
RVC slim artifact distribution
```

以上之后各自立项（plan → 确认 → 实现 → 验收），不属于当前 slim B1。

---

## 10. 当前完成定义

> **RVC slim B1 is complete when the project can regenerate `artifacts/rvc_slim` from the original Applio tree, pass import preflight, and produce bit-identical RVC wav output against the original Applio path on the verified parity sample.**

三条均已满足（§2 重建、§6 import preflight PASS、§7 bit-identical parity）。**RVC slim B1 完成。**

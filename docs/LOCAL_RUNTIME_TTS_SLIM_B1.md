# GPT-SoVITS Slim Runtime（B1 / Form A）收尾文档

> 父规划：`docs/LOCAL_RUNTIME_PLAN.md`（§B1）。本文是 **B1 落地收尾**：what / how / 结果 / 边界 / 剩余风险。
> **状态：功能完成，parity 验证 bit-identical，默认未切。** slim runtime 尚未接入 production driver，原始 vendored 仍是 fallback。

提交链（全部已 push 到 `origin/main`）：

| commit | 内容 |
| --- | --- |
| `d89c458` | manifest 骨架（`tts_slim_manifest.yaml` + `slim_manifest.py` 纯逻辑） |
| `81b6244` | dry-run planner（`build_tts_slim.py`，默认 dry-run） |
| `fe713e1` | inp_refs 打包 + loud-failure 统一 |
| `1333fec` | 真机 build 能力（execute_build + atomic staging + sha256 + character.yaml + build_report.json） |
| `2f5d35b` | 修 `tools/*.py` keep-list parity gap + parity harness（本步） |

关键文件：
- 配置/清单：`data/config/tts_slim_manifest.yaml`（数据驱动 keep/exclude + character pack + validation）
- 纯逻辑：`spica/local_runtime/tts/slim_manifest.py`（synthetic-tree 可测，无 torch/GPU）
- builder：`scripts/local_runtime/build_tts_slim.py`（dry-run planner + `--build` 真机 build）
- parity：`scripts/local_runtime/verify_tts_slim_parity.py`（prepare / import-check / worker / compare）
- 测试：`tests/test_tts_slim_manifest.py` / `tests/test_build_tts_slim.py` / `tests/test_tts_slim_parity.py`

---

## 1. B1 目标

B1 **不是**性能工作，而是**打包/裁剪**工作：

- ❌ 不是加速（推理路径不变）。
- ❌ 不是去 torch（仍依赖 torch/transformers，见 §9）。
- ❌ 不是 ONNX / TensorRT（那是 B2 / B3，仍冻结）。
- ✅ **是**把 GPT-SoVITS 这个 **~39G 的 vendored 工程**裁剪成一个**可打包、可分发的 slim runtime（~1.36G）**——只保留「当前 spcia v2ProPlus 日文推理路径」实际加载的代码与模型，剥掉日志、Windows 便携 python、训练/降噪/ASR/分离工具、未用的 v3/v4 与多 epoch 权重。

形态选择（见 PLAN §B1）：本轮走 **Form A（裁剪式）**——在原 vendored 上做 keep/exclude 拷贝，**不改 vendored 代码、不重写模型定义**。Form B（提取式）/ Form C（重写式，禁止）不在本轮。

---

## 2. 当前结果

| 指标 | 值 |
| --- | --- |
| 原始 vendored | **~39G**（logs 12G + Windows runtime 7.4G + tools asr/uvr5 4.8G + v3v4/多 epoch 权重 + 模型） |
| slim runtime | **1.3567 GB**（`total_bytes = 1,456,705,852`） |
| └ base | **~1.1 GB**（`base_bytes = 1,119,301,023` ≈ 1067.4 MB） |
| └ character pack（spcia） | **~322 MB**（`character_bytes = 337,404,829`） |
| copied files | **194**（base 166 / license 2 / character_gpt 1 / character_sovits 1 / character_reference 8 / character_inp_refs 16）+ 2 generated（character.yaml、build_report.json） |
| `inp_refs_packed` | **16**（4 emotion × 4 wav） |
| keep-list | 含 `tools/*.py`（顶层模块级 import）+ `tools/i18n/**`；heavy tools 子目录（asr/uvr5/AP_BWE_main）仍 exclude |

体积从 ~39G → ~1.36G（≈ 29×），且 parity bit-identical（§5）。

---

## 3. build 命令

```bash
python scripts/local_runtime/build_tts_slim.py \
  --manifest data/config/tts_slim_manifest.yaml \
  --out artifacts/tts_slim \
  --character spcia \
  --build
```

说明：

- **默认 dry-run**：不带 `--build` 只做规划（枚举 source、应用 keep/exclude、解析 character pack、估体积、跑守卫），**不复制任何文件、不建目录**。
- **`--build` 才真实复制**（显式 opt-in，避免误触发 1.36G 拷贝）。
- **output 必须 gitignored**：build 用 `git check-ignore` 断言 `artifacts/tts_slim` 被忽略，否则拒绝运行（防 slim artifact 误入库）。
- **staging + atomic rename**：先拷进同盘 sibling staging 目录，逐文件 sha256，生成 character.yaml + build_report.json，最后 `os.rename` 原子发布；任何异常 `rmtree` staging 回滚，final 只在 rename 时出现 → **失败绝不留半成品**。
- **默认拒绝覆盖**已存在 output（`refusing to clobber`），重建需先 `rm -rf artifacts/tts_slim`。
- **原始 vendored 只读**：build 只读 source，零写入（symlink 不 follow，source/target realpath containment）。

---

## 4. parity 验证流程

```bash
# 1. 生成两侧 spec（original / slim）+ 静态 preflight（路径存在、slim base 完整、inp_refs glob 隔离）。无 GPU。
python scripts/local_runtime/verify_tts_slim_parity.py prepare

# 2. import preflight：独立子进程真 import slim base 的 inference_webui，缺模块即报、阻断 parity。
python scripts/local_runtime/verify_tts_slim_parity.py import-check artifacts/tts_slim/base

# 3. 两侧各自在独立子进程合成 4 JA × 4 emotion，存 wav。
python scripts/local_runtime/verify_tts_slim_parity.py worker <scratch>/spec_original.json
python scripts/local_runtime/verify_tts_slim_parity.py worker <scratch>/spec_slim.json

# 4. 逐对 audio_metrics 对比，出表 + verdict + report。无 GPU。
python scripts/local_runtime/verify_tts_slim_parity.py compare
```

（`<scratch>` 默认 `artifacts/parity/tts_slim_stepd`，gitignored。）

**为什么必须独立子进程**：vendored `GPT_SoVITS.inference_webui` 用**模块级全局模型状态**（`vq_model` / `sv_cn_model` / `model_version` 等），且在 **import 时**按 cwd（`now_dir`）加载 BERT / cnhubert / sv。`sys.modules` 按模块名缓存——一个进程里先 import 原 root、再 import slim root，第二次会**复用第一次的模块 + 已加载模型**，等于「用原 base 的代码/模型跑 slim 的权重」，测不到 slim base。所以 **original 和 slim 必须各自独立子进程**，cwd/root 各指向自己的树，互不污染。

**weight.json 卫生**：`change_*_weights` 会往 root 写 `./weight.json`。parity 对**原 vendored** 的 weight.json 做 snapshot + restore（跑完字节级还原，vendored 不变）；**slim** 的 weight.json 创建后保留（作为 slim base 可写性证据）。

---

## 5. parity 结果

- 样本：**4 条日文样本 × 4 emotion = 16 clip**，固定 seed 1234，同权重/同 ref/同 inp_refs/同 tts_params。
- **16/16 bit-identical**（wav 文件 sha256 全等）。
- **max RMSE = 0.000e+00**（gate ≤ 1e-3，A1/A2/A3 噪声底 6.6e-4）。
- **max mel diff = 0.000**。
- **length equal = true**（16/16 长度全等）。
- `inp_refs` glob 语义保持：`reference/<emotion>/refs/*.wav` 只命中 4 个 inp_refs，不含主 ref。
- `character.yaml` 全 **pack-relative**（无 `/home`、无 `spica_data`、无 `..`），可迁移。

> 为什么是 0 而非 ~6.6e-4：slim 文件是 sha256 校验过的**字节级拷贝** + 同 seed + 同 GPU，两个独立进程跑出完全一致的输出。6.6e-4 是 A1 self-vs-self 的最坏噪声底；slim-vs-original 直接拿到了最强结果——bit-identical，等价性无可争议。

**B1 step1 教训**：第一次 parity 曾 BLOCKED——keep-list 漏了 `tools/assets.py`（`inference_webui.py:128` 的**模块级 import**）。step1 的 load-path 实测只追了「合成时的文件 open / torch.load」，看不到 **Python import 图**，所以漏了一个被 import 但不被 get_tts_wav 使用的 UI-asset 模块。修复 = keep 加 `tools/*.py` + 加 import preflight（独立子进程真 import，缺模块即阻断），重建后 parity 通过。**这就是 parity 闸的价值：build sha256 全对，但运行时缺一个模块——只有真机 parity 兜得住。**

---

## 6. slim runtime 结构

```text
artifacts/tts_slim/                  # gitignored build artifact，不入库
  base/                              # 共享、角色无关的 runtime（= gptsovits_root）
    config.py
    GPT_SoVITS/                      # 推理代码 + pretrained（BERT/cnhubert/sv/fast_langdetect）+ ja 文本资产
    tools/                           # tools/*.py（assets/my_utils/...）+ tools/i18n/**
    weight.json                      # 运行时生成（P0，见 §9）
  characters/
    spcia/                           # 单角色 pack（换角色只加 pack，不重建 base）
      GPT_weights/spcia-e25.ckpt
      SoVITS_weights/spcia_e12_s1932.pth
      reference/<emotion>/<main_ref>.wav        # 主参考
      reference/<emotion>/<prompt>.txt          # prompt 文本
      reference/<emotion>/refs/*.wav            # inp_refs（独立子目录，保 glob 隔离）
      character.yaml                            # 自包含、全 pack-relative
  build_report.json                 # totals + 逐文件 size/sha256 + license/writable 警告 + parity=PENDING
```

base / character pack 物理分离，对应 manifest 的 `runtime_base` vs `character_packs`。

---

## 7. keep / exclude 原则

**keep**（load-confirmed）：
- base runtime 代码：`config.py`、`GPT_SoVITS/**/*.py`。
- **`tools/*.py`**：顶层 tools 模块——因为 `inference_webui` 在**模块级** import `tools.assets`、`module/data_utils.py` import `tools.my_utils`。
- `tools/i18n/**`：i18n。
- pretrained（实测加载）：`chinese-roberta-wwm-ext-large`（BERT）、`chinese-hubert-base`、`sv/pretrained_eres2netv2w24s4ep4.ckpt`、`fast_langdetect/lid.176.bin`。
- 日文文本资产：`text/ja_userdic/**`、`text/opencpop-strict.txt`。

**exclude**：
- 体积大头：`logs/**`、`runtime/**`（Windows 便携 python）、`tools/asr/**`、`tools/uvr5/**`、`tools/AP_BWE_main/**`。
- 未用权重：`v3/v4` 预训练、`s2Gv2ProPlus` base、`s1v3`、多 epoch / 中间权重；中英 g2p（`G2PWModel`/`cmudict`，`language_profile: ja_only`）。
- webui / api / notebook / dataset / docker。

**归属纪律**：
- **character 权重**（gpt/sovits）属 **character pack**，不进 base（manifest 把 `GPT_weights*`/`SoVITS_weights*` 从 base exclude，pack 显式拉取）。
- **inp_refs** 属 **character pack**，不进 base（独立 `refs/` 子目录，保 `glob(refs/*.wav)` 只命中 inp_refs）。

> ⚠️ 边界：`tools/*.py` 只取**顶层** .py（glob 不跨 `/`），不会把 `tools/asr/*.py` 等子目录 .py 捞进来。换语言（zh/en）需重新跑 keep-list 实测并恢复对应 g2p 资产。

---

## 8. loud-failure 规则

「配置声明且主合成路径会实际使用的依赖，缺失必须 blocking error（dry-run 非 0、无成功 plan）」——避免 build 出「sha256 都对但缺资源」的 slim。

**blocking（缺失即 `BuildAbort`）**：
- base keep glob（每条 `runtime_base.keep` 必须匹配 ≥1 源文件）。
- character **GPT 权重** / **SoVITS 权重**。
- 每个 emotion 的**主 ref audio**（`ref_audio_path`）。
- **`prompt_text_path`**（file 形式；内联 `prompt_text` 不查文件）。
- **`inp_refs_path`**（目录不存在 / 目录空无音频）。

**warning（不阻断）**：
- **license attribution missing**（仅进 `build_report.licenses.missing` + 打 WARNING）。

---

## 9. 剩余风险 / TODO

1. **`weight.json` P0**：slim base 当前**必须可写**（`change_*_weights` 在 base root 写 `./weight.json`）。装到 Program Files / `/opt` / 只读 app bundle 会**失败**。未来需重定向到 user-data/cache 目录或预生成并去掉运行时写。manifest `writable_paths` 已标 P0。
2. **license attribution**：4 个 pretrained 模型目录（chinese-hubert-base / chinese-roberta-wwm-ext-large / fast_langdetect / sv）**缺自带 LICENSE/README/NOTICE**（只有顶层 GPT-SoVITS LICENSE 被拷进 base）。**发布前必须补** third-party attribution。当前 warning-only，不阻断。
3. **仍依赖** torch / transformers / pyopenjtalk / librosa / soundfile（slim 只裁 vendored 树，没裁 python 依赖）。manifest `env_dependencies` 已列。
4. **B2 ONNX**：只有当 torch/transformers 成为安装包体积/分发瓶颈时再做。**仍冻结。**
5. **B3 TensorRT**：optional，不是主线。**仍冻结。**

---

## 10. 当前边界

- slim runtime **尚未接入 production driver**（`GptSovitsV2ProDriver` / `service.py` 未改）。
- **未设为默认**（生产仍走原 vendored，`data/config/tts.yaml` 未改）。
- **original vendored 仍是 fallback**，且字节未改动。
- **artifact 不提交**（`artifacts/tts_slim/`、wav、build_report.json、parity_report 全 gitignored）。
- 入库的只有：**manifest / builder / parity harness / tests / 本文档**。

> 接入 driver / 切默认是后续独立立项（需 plan → 确认 → 实现 → 真机验收），不在 B1。

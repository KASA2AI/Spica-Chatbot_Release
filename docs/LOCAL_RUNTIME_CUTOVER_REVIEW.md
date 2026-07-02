# Runtime Cutover 阶段 Review 报告

> 生成于本轮会话收尾。origin/main = `06d2243`。父规划：`docs/LOCAL_RUNTIME_PLAN.md`；姊妹收尾：`LOCAL_RUNTIME_TTS_SLIM_B1.md` / `LOCAL_RUNTIME_RVC_SLIM_B1.md` / `LOCAL_RUNTIME_MOONDREAM_CUT4.md`。
>
> **状态：TTS 39G + RVC 12G 两棵 vendored 大树均已移出仓库、运行时脱离原树；OCR/Moondream 无 vendored 树、已切新 provider 默认。独立 env / numpy 拆分 / Windows / 可复现安装延期到打包阶段。**

---

## 1. 阶段目标

逐模块减少对仓内 vendored 第三方大树的**运行时依赖**，把生产链切到隔离后的 `spica/local_runtime/` slim 产物，并把 vendored 大树移出仓库（可逆 backup），固定部署边界。**奖品 = 摆脱 vendored 大树，不是提速。**

**诚实边界**：slim 是裁剪后的 vendored 子集，仍依赖 torch/transformers 等 python 包（装在 conda env）；**不是「运行时零 vendored 源码」**。

---

## 2. 本轮 commits（全部已 push origin/main）

| commit | 内容 |
|---|---|
| `5ea631d` | test: `tests/conftest.py` per-test `os.environ` 隔离——修 xiaosan.env dotenv 泄漏导致的测试隔离缺陷 |
| `5411f6d` | **TTS 接 `tts_slim` + 39G GPT-SoVITS 树移出仓** |
| `f8d4a95` | RVC Phase 1A：subprocess execution seam（worker/driver/config/pipeline/tests） |
| `04db545` | RVC 硬化：execution_mode 校验 + 统一错误信封 + result.json 原子写 |
| `78ecaf5` | RVC 修复：非零 exit 即失败（不被 ok result 覆盖） |
| `2f3155f` | RVC Phase 1B：默认切 subprocess（主进程脱离 Applio +4472 污染） |
| `e4f0125` | UI：唱歌状态文案人格化 |
| `06d2243` | **RVC 接 `rvc_slim`（生产 applio_root/model/index → slim）** |
| —（无 commit） | **Applio 12G 树移出仓**（整目录 gitignored、0 tracked → `mv` 对 git 无感） |

codex 早先提交（本轮审查过、并发现+修复其连带的测试隔离缺陷）：`7bf106b`(screen→moondream_hf) / `eef5194`(ocr→rapidocr_ort) / `c61d65c`(guard test) / `7fada42`(docs)。

---

## 3. 逐模块状态

| 模块 | provider / runtime | vendored 树 | parity | 真机 smoke |
|---|---|---|---|---|
| **OCR** | `rapidocr_ort` 默认；Path A(galgame)+B(inspect_screen) 统一 `OCRPort` | 无树（pip `rapidocr_onnxruntime`） | 25 张真图 smoke（codex，match 1.0） | — |
| **Moondream** | `moondream_hf` 默认 | 无树（transformers + HF remote code） | BIT_IDENTICAL（早期会话） | — |
| **TTS** | `tts_slim`（base + spcia pack，1.4G） | **39G 已移出仓** ✅ | 16/16 逐字节 rmse=0 | App 语音四情绪 ✅ |
| **RVC** | subprocess 隔离 + `rvc_slim`（618M） | **12G 已移出仓** ✅ | 逐字节 rmse=0 / bit_identical | App sing_song 全链 ✅ |

---

## 4. 达成的 endgame

- **≈51G vendored 源码移出仓库** → `../spica_vendor_backup/`：`GPT-SoVITS-v2pro-20250604-nvidia50`(39G) + `Applio`(12G)。
- 运行时不再依赖仓内 vendored 大树：TTS off `artifacts/tts_slim`、RVC off `artifacts/rvc_slim`。
- **fresh-process 实测**：两棵**原树路径**已不可加载 / 不在 import 路径（TTS：对**原 39G root** 的 import-check FAILED；RVC：`Applio/core.py` 不存在），而生产 TTS/RVC 仍正常出声。**注**：slim base 里仍含 `GPT_SoVITS` 包并可正常 import——此处指**原 39G 树路径**不可用，不是 `GPT_SoVITS` 包本身不能 import。

---

## 5. 验证证据（7 项闭环，每块逐项过）

1. **parity 重跑**（不吃旧记录）：TTS slim vs 39G、RVC slim vs 12G，均逐字节相同（RMSE 0、长度相等、sha256 一致）。
2. **生产路径真机 smoke**：TTS 四情绪 App 语音、RVC App sing_song 全链（搜/下/分离/RVC(subprocess)/混音/播放）——用户确认通过。
3. **legacy fallback**：TTS `git checkout` 回 vendored 出声；RVC parity 原侧跑 12G + 移后 backup 路径出声。
4. **移树到仓外 backup、不硬删**。
5. **fresh-process**：**原树路径**不可加载 / 不在 import 路径（slim base 的 `GPT_SoVITS` 包仍可正常 import），生产 off slim 仍出声、父进程零 Applio 污染。
6. **全量 `python -m pytest tests -q` 绿**：**1103 passed**（含每步复验）。
7. **失败只回滚本块**：backup `mv` 移回 / config revert。

---

## 6. 关键发现与修复

### 6.1 测试隔离缺陷（codex cutover 的连带 red，本轮定位+修复）
- **现象**：全量从 1071 green → 3 red（screen config 测试）。
- **根因**（runtime trace 坐实）：`ConfigManager.load() → _ensure_env_loaded() → load_dotenv(xiaosan.env)` 把开发机本地 `SPICA_SCREEN_*`（含 `SPICA_SCREEN_PROVIDER=moondream_hf`）**永久灌进全局 `os.environ`**，污染后续测试；旧值=`moondream_local` 时恰好=断言期望被掩盖，codex 切 `moondream_hf` 后暴露。**机器相关**（干净 checkout / CI 不复现）。
- **修**：`tests/conftest.py` autouse 每测试 snapshot/restore `os.environ`（`5ea631d`），对 xiaosan.env 取值免疫。1103 全绿。

### 6.2 RVC subprocess 错误处理硬化（codex review 的阻塞点）
- `execution_mode` 非法值原会静默回落 in_process → 改为只接受 `in_process`/`subprocess`，其它 `ValueError`（切默认后配置拼错不会静默丢隔离）。
- 所有 subprocess 失败路径（timeout / 启动失败 / 非零 exit / 缺·坏·partial result.json / ok=false / ok=true 但 wav 缺）→ 统一 `RuntimeError`，含 returncode(或 timeout/异常类型) + timeout_sec + result_path + wav_exists + stdout/stderr tail。
- worker `result.json` **原子写**（临时文件 + `os.replace`）+ **非零 exit 即失败**（不被 ok result 覆盖）。成功判据 = result.json 存在+可解析+`ok is True`+output wav 存在。
- （`04db545` + `78ecaf5`）

---

## 7. 延期到打包 / Windows 阶段（已记录，本阶段不做）

- 独立 RVC env / numpy 拆分 / `requirements-rvc.txt` / 跨平台可复现安装（`spica-clean-py311` 已验证可用：py3.11 / numpy 2.4.6 / torch 2.5.1+cu124，RVC+separator 依赖齐）。
- **separator 子进程化**（audio-separator numpy≥2 与 GPT-SoVITS numpy<2 的冲突仍残留在主 env——移 RVC 未解此项）。
- `runtime_env.py` 退役、裸 `pytest` 限制解除。
- **`README.md` 已过期，进入打包前必须改**：`README.md:142` 仍称默认 TTS vendor 根目录是 39G `GPT-SoVITS-…`；`README.md:439` 仍称点歌前需本地补齐仓内 `agent_tools/function_tools/song/Applio`。与当前 runtime 不符——生产已走 `artifacts/tts_slim` / `artifacts/rvc_slim`（或未来稳定 runtime artifact），不是仓内大树。
- `data/config/rvc_slim_manifest.yaml:26` + `scripts/local_runtime/verify_rvc_slim_parity.py:73`（TTS manifest 同型）默认 `source` 仍指仓内 `song/Applio`——**移后默认 rebuild / 重跑 original-vs-slim parity 命令会失败**（找不到原树），需 `--source ../spica_vendor_backup/…` 或改 manifest（gate #6 记录，未改）。
- slim artifacts 从 gitignored `artifacts/` 迁到稳定 runtime 目录。
- `build_release.sh`（引用了已移走的树；现是工作区 WIP 删除态）。

---

## 8. 回滚

| 对象 | 回滚方式 |
|---|---|
| TTS 树 | `mv ../spica_vendor_backup/GPT-SoVITS-… agent_tools/tts/vendors/` + `data/config/tts.yaml` 指回（tts.yaml 已 committed 到 slim，回滚需改指 backup 绝对路径） |
| RVC 树 | `mv ../spica_vendor_backup/Applio agent_tools/function_tools/song/Applio`（秒级）。若需把配置指回：**只 patch app.yaml 的 song 三行**（过滤 patch / `git add -p`）或在 fresh branch 上操作——**切勿整文件 `git checkout data/config/app.yaml`**，会误回你的 galgame WIP。 |
| RVC 执行 | config `rvc.execution_mode: in_process` 回同进程 legacy（in_process 保留可选） |

> **⚠ 热切限制**：`agent_tools/function_tools/song/rvc.py:12-13` 的全局 `_CORE_MODULE` 缓存加载一次即复用——**同一进程内**先加载 slim 再把 `execution_mode` 改回 in_process 指 backup **未必真重载 backup core**。legacy / 回滚验证必须在 **fresh process / 重启 App** 下进行，不要指望运行中改配置就干净切回。

---

## 9. 风险 / 挂账

- **单 gptsovits env 仍 pip-check 红**（GPT-SoVITS numpy<2 vs audio-separator numpy≥2，靠压 numpy<2 将就）——脆弱但能跑；归打包阶段整体解。
- **slim 在 gitignored `artifacts/`**：fresh clone / 发布仓需本地有 slim 产物才出声（`README.md:439` 已声明发布仓不含 Applio）——归打包阶段。
- **移后 rebuild / 重跑 original-vs-slim parity 需指 backup**（manifest source 未改）。
- **`infer_spica_vocal` 默认 `applio_root` 仍指已移走的树**（`agent_tools/function_tools/song/rvc.py:29`）：生产链 `pipeline.py:212` 显式传 slim root 所以无事，但**脚本 / 手动调用漏传 `applio_root` 会直接炸**（找不到原树）。后续硬化：强制 `applio_root` 必填 或 默认从 resolved song config 取 slim。
- 工作区仍有**既有 WIP**（app.yaml galgame 调参、CLAUDE.md、speech_worker.py、run_ibus.sh、overlay_config.json、build_release.sh 删除 + `.agents/`/`AGENTS.md`/`docs/agents/`/`docs/codex-handoff-summary.md`）——非本阶段产物，全程未动。

---

## 10. 清理记录（本轮收尾）

- **删**：session scratchpad（本轮验证脚本 / 套件日志 / config 基线，约 11M）；本轮 parity scratch `artifacts/parity/{tts_slim_stepd, rvc_slim_step2b}`（约 11M）。
- **保留**：`artifacts/{tts_slim, rvc_slim}`（生产 slim 产物）、`../spica_vendor_backup/*`（回滚源）、更早会话/codex 的 `artifacts/parity/{ocr_galgame_step3_2, moondream_cut4, *_20260629/30*.json}`（非本轮，保守未删）。

# Windows 基础环境 bootstrap（W2-a 留痕文档）

> 归属：`docs/WINDOWS_COMPAT_PLAN.md` §5-W2-a / §6.0。
> 状态：**W2-a bootstrap complete。** 真实 Windows smoke 已于 2026-07-05 在 Windows 10 Enterprise LTSC 2021（10.0.19044）真机执行并回填 §3；REQUIRED 全绿、import 闭包 smoke `ok`、`platformName()` = `windows`。

## 1. 范围与 gate（W2-a 审批裁决）

- W2-a 只做**基础环境 bootstrap + import 闭包 smoke**，不做 W2/W3/W4 的任何实现或验收。
- `python scripts/windows/check_imports.py`：**REQUIRED 全绿是硬 gate**；PREFLIGHT（PyAudio）失败仅 WARN 留痕，不阻塞 W2-a 收口。
- **`AppHost.initialize()` 明确 deferred 到 W2**，不属于 W2-a hard gate；本段只做 import 闭包 smoke（下面第 5 步）。
- Windows 机**只 pull + smoke，不产生任何 commit**（E5）；全部留痕由 Linux 侧写入本文档提交。
- `xiaosan.env`（secrets）在 Windows 本机手工配置，**不入库**。import 闭包 smoke 不依赖它（import 期零 env 读取，铁律 #4/#10）。

## 2. Windows 机执行步骤

在仓库根目录（pull 到 HEAD 含本文件的 main）：

```powershell
conda create -n spica-win python=3.11
conda activate spica-win
python -m pip install -r requirements-windows-base.txt
python scripts/windows/check_imports.py
python -c "from spica.host.app_host import AppHost; import ui.qt_overlay; print('ok')"
python -c "from PySide6.QtWidgets import QApplication; a=QApplication([]); print(a.platformName())"
```

逐条预期：

| 步骤 | 预期 |
|---|---|
| `pip install -r requirements-windows-base.txt` | REQUIRED 段全部安装成功；PyAudio 装失败可继续（PREFLIGHT） |
| `check_imports.py` | `RESULT: OK`；providers 行至少含 `CPUExecutionProvider`；PyAudio 若 WARN 记录原文 |
| import 闭包 smoke | 打印 `ok`（只验证 import 闭包，不调用 `AppHost.initialize()`——那是 W2 gate） |
| PySide6 platform smoke | 打印 `windows` |

可选留痕（不是 gate）：

```powershell
python scripts/dump_resolved_config.py --out <本机路径>
```

执行后把以下三份输出**原文**贴回 Linux 施工窗口，由 Linux 侧回填 §3：

1. `python -m pip freeze` 输出；
2. `check_imports.py` 完整输出；
3. import 闭包 smoke 与 `platformName()` 输出。

## 3. 真实留痕

> 执行日期：2026-07-05。机器：Windows 10 Enterprise LTSC 2021 `10.0.19044` / conda `25.11.1` / spica-win python `3.11.15`。
> 仓库 HEAD：`c326d91`（含 W2-a artifact）。Windows 只 pull + smoke，本机不 commit（E5）；留痕由 Linux 侧提交。

### 3.1 conda env 创建 + pip install 留痕

命令：

```
conda create -n spica-win python=3.11 -y
conda run -n spica-win python -m pip install -r requirements-windows-base.txt
```

结果：exit 0，REQUIRED 12 包 + PREFLIGHT PyAudio 全部安装成功。

```
Successfully installed Pillow-11.3.0 PyAudio-0.2.14 PySide6-6.11.1 PySide6_Addons-6.11.1
PySide6_Essentials-6.11.1 PyYAML-6.0.3 Shapely-2.1.2 annotated-types-0.7.0 anyio-4.14.1
av-18.0.0 certifi-2026.6.17 click-8.4.2 colorama-0.4.6 ctranslate2-4.8.1 faster-whisper-1.2.1
filelock-3.29.5 flatbuffers-25.12.19 fsspec-2026.6.0 h11-0.16.0 hf-xet-1.5.1 httpcore-1.0.9
httpx-0.28.1 huggingface-hub-1.22.0 idna-3.18 jiter-0.16.0 mss-10.2.0 numpy-1.26.4
onnxruntime-1.27.0 openai-2.44.0 opencv-python-4.11.0.86 protobuf-7.35.1 pyclipper-1.4.0
pydantic-2.13.4 pydantic-core-2.46.4 python-dotenv-1.2.2 rapidocr-onnxruntime-1.4.4
shiboken6-6.11.1 six-1.17.0 sniffio-1.3.1 tokenizers-0.23.1 tqdm-4.68.3
typing-extensions-4.16.0 typing-inspection-0.4.2
```

**版本钉版复核**：全部落在 `requirements-windows-base.txt` 区间内，零漂移。要点：`numpy 1.26.4`（`<2` 前向约束守住）、`onnxruntime 1.27.0`（`<2` CPU 包）、`Pillow 11.3.0`（`<12`，比 Linux 的 10.4.0 高但同区间）。

无关告警（非 W2-a 阻塞项）：`ERROR: pip's dependency resolver ... pipx 1.7.1 requires platformdirs>=2.1, which is not installed.` —— pipx 属 base 全局工具、不在 W2-a 清单内，spica-win 环境本身安装成功、无冲突。（`pipx/userpath/argcomplete` 出现在下方 freeze 系 base 工具串场，与 REQUIRED gate 无关。）

pip freeze（spica-win，摘录 W2-a 相关项 + 全量）：

```
annotated-types==0.7.0
anyio==4.14.1
argcomplete==3.6.2
av==18.0.0
certifi==2026.6.17
click==8.4.2
colorama==0.4.6
ctranslate2==4.8.1
distro==1.9.0
faster-whisper==1.2.1
filelock==3.29.5
flatbuffers==25.12.19
fsspec==2026.6.0
h11==0.16.0
hf-xet==1.5.1
httpcore==1.0.9
httpx==0.28.1
huggingface_hub==1.22.0
idna==3.18
jiter==0.16.0
mss==10.2.0
numpy==1.26.4
onnxruntime==1.27.0
openai==2.44.0
opencv-python==4.11.0.86
packaging==26.0
pillow==11.3.0
pipx==1.7.1
protobuf==7.35.1
PyAudio==0.2.14
pyclipper==1.4.0
pydantic==2.13.4
pydantic_core==2.46.4
PySide6==6.11.1
PySide6_Addons==6.11.1
PySide6_Essentials==6.11.1
python-dotenv==1.2.2
PyYAML==6.0.3
rapidocr-onnxruntime==1.4.4
shapely==2.1.2
shiboken6==6.11.1
six==1.17.0
sniffio==1.3.1
tokenizers==0.23.1
tqdm==4.68.3
typing-inspection==0.4.2
typing_extensions==4.16.0
userpath==1.9.2
```

### 3.2 check_imports 输出

命令：`conda run -n spica-win python scripts/windows/check_imports.py`

```
python == 3.11.15 (win32)

[REQUIRED]
OK    PySide6 == 6.11.1
OK    openai == 2.44.0
OK    httpx == 0.28.1
OK    pydantic == 2.13.4
OK    PyYAML == 6.0.3
OK    python-dotenv == 1.2.2
OK    numpy == 1.26.4
OK    Pillow == 11.3.0
OK    mss == 10.2.0
OK    rapidocr-onnxruntime == 1.4.4
OK    onnxruntime == 1.27.0
OK    faster-whisper == 1.2.1

[PREFLIGHT]
OK(p) PyAudio == 0.2.14

[onnxruntime providers]
['AzureExecutionProvider', 'CPUExecutionProvider']

RESULT: OK (all REQUIRED imports green)
```

gate：REQUIRED 全绿；PREFLIGHT PyAudio 也绿（未触发 WARN）；providers 含 `CPUExecutionProvider`（符合 §6.0 预期）。`AzureExecutionProvider` 是 onnxruntime CPU 包的默认桩 EP（非 GPU），无 CUDA/TRT——正是 CPU 包应有形态（裁决④得证）。

### 3.3 import 闭包 smoke + platformName 输出

命令：`conda run -n spica-win python -c "from spica.host.app_host import AppHost; import ui.qt_overlay; print('ok')"`

```
ok
```

命令：`conda run -n spica-win python -c "from PySide6.QtWidgets import QApplication; a=QApplication([]); print(a.platformName())"`

```
windows
```

（`AppHost.initialize()` 是 W2 gate，本段只做 import 闭包 smoke，未调用。）

### 3.4 可选 dump_resolved_config 留痕

未执行（可选、非 gate）：本机尚未配置 `xiaosan.env`（secrets），为保持验收日志干净暂缓；如需可在配 secrets 后补跑 `python scripts/dump_resolved_config.py --out <本机路径>`。

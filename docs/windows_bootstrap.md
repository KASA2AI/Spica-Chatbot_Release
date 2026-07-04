# Windows 基础环境 bootstrap（W2-a 留痕文档）

> 归属：`docs/WINDOWS_COMPAT_PLAN.md` §5-W2-a / §6.0。
> 状态：**模板已就绪，等待 Windows 侧 smoke 输出回填。§3 真实留痕未回填前，不得宣布 W2-a complete。**

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

## 3. 真实留痕（待回填）

> 回填前本节保持占位；回填时注明执行日期与机器（OS 版本 / conda 版本）。

### 3.1 conda env 创建 + pip install 留痕

（待回填：命令与关键输出、`pip freeze` 全文或摘录）

### 3.2 check_imports 输出

（待回填：完整输出，含 providers 行与 PyAudio WARN（如有））

### 3.3 import 闭包 smoke + platformName 输出

（待回填）

### 3.4 可选 dump_resolved_config 留痕

（待回填 / 标注未执行）

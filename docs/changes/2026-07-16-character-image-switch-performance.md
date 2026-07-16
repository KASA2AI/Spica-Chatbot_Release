# 角色图片切换与透明点击区域性能优化

日期：2026-07-16

## 背景与现象

角色表情切换期间出现了明显的 Qt GUI 停顿，生产日志记录到：

```text
set_character_image_slow ... duration_ms=568.95
set_character_image_slow ... duration_ms=931.03
```

日志对应的角色资源是 2048×2048 RGBA PNG，单文件约 1.15MB，解码后约占 16MiB。图片切换与字幕打字机都运行在 GUI 线程，因此该停顿会同时暂停字幕计时器。

## 根因

图片切换调用链为：

1. `ui/controllers/chat_stream_controller.py` 通过 `QTimer.singleShot(0)` 将切换安排到下一轮 GUI 事件循环。
2. `ui/qt_overlay.py:set_character_image()` 同步读取图片并调用 `_layout_overlay()`。
3. `_layout_overlay()` 调用 `_update_click_through_mask()`。
4. `_character_hit_region()` 将缩放后的角色图转换成 `QImage`。
5. `_alpha_hit_region()` 通过 Python 双层循环逐像素读取 alpha，并对每行的不透明区段反复执行 `QRegion.united()`。

该路径复杂度为 O(width×height)，主要耗时集中在单个 GUI/Python 线程。原图和缩放图即使命中缓存，透明点击区域仍会重复计算。

优化前的分段测量：

| 环节 | 耗时 |
|---|---:|
| PNG 首次解码 | 约 69～100ms |
| 平滑缩放 | 约 3～7ms |
| QImage 转换 | 约 0.5～1ms |
| Python alpha 扫描与区域合并 | 约 579～1229ms |

## 实现方案

### 1. 等价的向量化 Alpha Mask

`_alpha_hit_region()` 现在执行以下步骤：

1. 将源图转换为字节顺序稳定的 `QImage.Format_RGBA8888`。
2. 使用 NumPy 读取 alpha 通道，并保持原有 `alpha > 8` 判断。
3. 构造二值 alpha 图，通过 `QImage.createAlphaMask()`、`QBitmap` 和 `QRegion` 交给 Qt 原生路径生成区域。
4. 将 7px 点击边距拆成水平、垂直两阶段扩展，把最多 225 次平移组合降为 30 次，同时保持与旧实现相同的方形膨胀结果。
5. 按原逻辑裁剪到图片边界，再平移到角色图在窗口中的实际位置。

### 2. 有界透明区域缓存

新增最多 64 项的 LRU 透明点击区域缓存。缓存值使用图片局部坐标，窗口或标签移动时只需要 `translated()`。

缓存键覆盖：

- 图片绝对路径
- 标签宽高
- UI 缩放
- 角色缩放
- 最终 pixmap 宽高
- alpha 阈值
- 点击边距

图片或尺寸变化会产生新的键；相同图片和尺寸会直接复用区域。交互式窗口缩放期间仍保留原有矩形快速路径。

### 3. 图片缓存内存边界

- 原始 `QPixmap` LRU：最多 24 项
- 缩放后 `QPixmap` LRU：最多 64 项
- 透明点击区域 LRU：最多 64 项

这避免长时间遍历大量表情时缓存持续增长。再次设置当前已显示图片时增加快速返回，跳过重复布局和 Mask 更新。

## 性能验证

使用生产角色图 `bs3_sp1_base51_face001_002.png`，对旧算法和新算法生成的完整 `QRegion` 做等价比较：

| 显示尺寸 | 旧算法 | 新算法中位数 | 加速 | 区域结果 |
|---|---:|---:|---:|---|
| 584×584 | 585.94ms | 4.54ms | 129.2× | 完全相等 |
| 700×700 | 718.34ms | 5.70ms | 125.9× | 完全相等 |

新算法最小测量分别为 3.75ms 和 4.49ms。以上数据针对原先的主要热点 `_alpha_hit_region()`；首次出现一张新图片时仍包含约 69～100ms 的 PNG 解码成本，后续复用由 LRU 缓存覆盖。

## 测试与验证

新增 `tests/test_qt_overlay_character_mask.py`，覆盖：

- 新旧算法在阈值边界、分离图形和不同原点下生成完全相等的 `QRegion`
- 全透明与全不透明图片
- 相同图片和尺寸的区域缓存命中
- 标签移动时复用局部缓存并正确平移
- A→B→A 图片切换时的缓存隔离与复用
- 交互式 resize 的矩形快速路径
- LRU 容量与最近使用顺序

本机验证结果：

```text
定向 unittest：6 tests passed
角色 Mask + ChatStreamController 相关回归：14 tests passed
compileall：PASS
git diff --check：PASS
```

定向测试命令：

```powershell
$env:QT_QPA_PLATFORM='offscreen'
$env:PYTHONPATH=(Get-Location).Path
.\.venv\Scripts\python.exe tests\test_qt_overlay_character_mask.py -q
.\.venv\Scripts\python.exe -m compileall -q ui\qt_overlay.py tests\test_qt_overlay_character_mask.py
git diff --check -- ui\qt_overlay.py tests\test_qt_overlay_character_mask.py
```

## 涉及文件

- `ui/qt_overlay.py`
- `tests/test_qt_overlay_character_mask.py`
- `docs/changes/2026-07-16-character-image-switch-performance.md`

## 后续观察

- 在真实桌面会话中继续观察 `set_character_image_done` 与 `set_character_image_slow`。
- 首次出现新图片的剩余成本主要是 PNG 解码；后续若仍需压缩冷路径，可考虑预载下一播放单元的图片。
- Mask 仍在 GUI 线程提交给窗口系统，但主要的逐像素 Python 热点已经移除。

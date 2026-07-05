# W4-a Windows GPU 环境探底留痕(+ W2-DPI 补验/waiver)

> 归属:`docs/WINDOWS_COMPAT_PLAN.md` §5-W4 内容 1(升格为独立小步 W4-a)。
> 裁决(2026-07-05 review):**ALLOW W4-a only;W4-b 未批准**——生产代码/tests/requirements 零改动,重型安装只进新建 probe env 或 clone env,不污染正式 spica-win,vendored 零触碰。
> 状态:**Linux 侧探针完成(§2);Windows 侧 A–E 段已回填(§4,2026-07-05 真机)。探底证据完整,建议 W4-a 收口 + W4-b 放行 + W2-DPI waiver——裁决权归审批窗口,本文档不自标 complete、不自批 W4-b、不自落 §8 waiver。**

## 1. 参照矩阵(Linux golden env,2026-07-05 实测)

E1 前提:CUDA/TRT 与 Linux 一致——Windows 矩阵以 **cu124 家族**对齐为目标。

| 组件 | Linux golden 版本 | 备注 |
|---|---|---|
| GPU / driver | RTX 4090 / 555.42.06 | Windows 机型号待 P0 回填 |
| torch / torchaudio | 2.5.1+cu124 / 2.5.1 | index-url download.pytorch.org/whl/cu124 |
| ctranslate2 | 4.7.2 | Windows spica-win 已是 4.8.1 且 CUDA 已通(W3 smoke) |
| onnxruntime-gpu | 1.26.0 | Windows base 装的是 CPU 包 1.27.0(替换策略见 §4) |
| TensorRT | tensorrt-cu12 10.16.1.11(pip) | Linux 经 in-process preload 验证(det/rec→TRT,cls→CUDA) |
| cuDNN / cuBLAS | nvidia-cudnn-cu12 9.1.0.70 / cublas 12.4.5.8(pip) | **Linux 的 CUDA 库全走 pip 包,非系统 Toolkit**;Windows torch wheel 惯例是 DLL 打包在 torch\lib——P2 回填确认 |
| numpy 约束 | TTS/主 env `<2`(1.26.4);RVC env `==2.4.6` | 双 env 是 Linux 实证结论(audio-separator 强制 ≥2) |

## 2. Linux 侧 wheel 存在性探针(F 段,2026-07-05 已完成)

方法:`pip download <pkg>==<ver> --platform win_amd64 --python-version 3.11 --only-binary=:all: --no-deps`(仅探 wheel 存在性,零安装;同 W3-a webrtcvad 探针法)。

| 包(requirements-rvc.txt 钉版) | 结果 |
|---|---|
| faiss-cpu==1.14.2 | ✅ `faiss_cpu-1.14.2-cp311-cp311-win_amd64.whl` |
| torchcrepe==0.0.24 | ✅ `py3-none-any`(纯 py) |
| torchfcpe==0.0.4 | ✅ `py3-none-any`(纯 py) |
| audio-separator==0.44.2 | ✅ `py3-none-any`(纯 py;numpy≥2 强制源) |
| pedalboard==0.9.23 | ✅ `pedalboard-0.9.23-cp311-cp311-win_amd64.whl` |
| soxr==1.1.0 | ✅ `soxr-1.1.0-cp311-cp311-win_amd64.whl` |
| onnxruntime-gpu==1.26.0 | ✅ `onnxruntime_gpu-1.26.0-cp311-cp311-win_amd64.whl` |
| torch==2.5.1+cu124(顺带) | ✅ `torch-2.5.1+cu124-cp311-cp311-win_amd64.whl`(pytorch cu124 index 直查) |

**结论:7/7(+torch)全有 py3.11 win_amd64 wheel,RVC 独立 env 的 Windows 移植无 wheel 缺失类 blocker。**

## 3. Windows 侧执行稿(A–E 段;操作者逐条执行,输出贴回 §4)

> PowerShell 逐条跑,不用 cmd 的 `&` 串联。全部重型安装只进 `spica-win-probe` / `spica-win-heavy-probe`。

### A. 第 0 项:W2-DPI-MULTISCREEN 补验(优先;无第二屏则如实写明,走 waiver 路径)

接第二块显示器(电视 HDMI 亦可),设**主屏 200% + 副屏 100%(或 150%)**混合 DPI,依次留痕:
1. 屏幕拓扑与各屏缩放率(`mss` 的 monitors 列表或系统显示设置截述);
2. 绑定窗口放**主屏**:框选 OCR 区域 → 对齐验证;
3. 绑定窗口放**副屏**:框选 OCR 区域 → 对齐验证;
4. 副屏 token 对齐法(W2 §4 同法):tkinter 置顶窗口显示唯一 token 置于副屏 → `locator.get_window_geometry`(物理 px)→ `mss.grab(该 rect)` → RapidOCR 读回 → 记录零偏移/具体偏移量。

### B. GPU 底座(纯读)

```powershell
nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv
where.exe cudnn64*.dll
where.exe cublas64*.dll
$env:CUDA_PATH
nvcc --version
```
预期:driver ≥ 551.61(cu124 下限);cudnn/cublas 的 `where` 结果回答「W3 的 ct2-CUDA 到底从哪找到 DLL」(系统 Toolkit vs wheel 自带 vs PATH 注入)。

### C. 现有 spica-win 现状(纯读)

```powershell
conda run -n spica-win python -c "import ctranslate2 as c; print(c.__version__, c.get_cuda_device_count())"
conda run -n spica-win python -c "import ctranslate2, os; d=os.path.dirname(ctranslate2.__file__); print(d); print([f for f in os.listdir(d) if f.endswith('.dll')])"
conda run -n spica-win python -m scripts.local_runtime.doctor
conda run -n spica-win python -m pip freeze
conda run -n spica-win python -m pip check
```
预期:ct2 4.8.1 + cuda_device_count ≥1(W3 已证,此处正式留痕);pip check 应 clean(W2-a/W3 清单自洽)。

### D. 干净 probe env(torch → ort-gpu → TRT 三级阶梯,每级留痕)

```powershell
conda create -n spica-win-probe python=3.11 -y
conda run -n spica-win-probe python -m pip install torch==2.5.1 torchaudio==2.5.1 --index-url https://download.pytorch.org/whl/cu124
conda run -n spica-win-probe python -c "import torch; print(torch.__version__, torch.version.cuda, torch.cuda.is_available(), torch.cuda.get_device_name(0))"
conda run -n spica-win-probe python -c "import torch; x=torch.rand(2048,2048,device='cuda'); print((x@x).sum().item())"
conda run -n spica-win-probe python -m pip install onnxruntime-gpu==1.26.0
conda run -n spica-win-probe python -c "import onnxruntime as o; print(o.__version__, o.get_available_providers())"
conda run -n spica-win-probe python -m scripts.local_runtime.doctor
conda run -n spica-win-probe python -m pip install tensorrt-cu12==10.16.1.11
conda run -n spica-win-probe python -c "import tensorrt; print(tensorrt.__version__)"
conda run -n spica-win-probe python -c "import tensorrt_libs, os; print(os.path.dirname(tensorrt_libs.__file__)); print([f for f in os.listdir(os.path.dirname(tensorrt_libs.__file__)) if f.endswith('.dll')])"
conda run -n spica-win-probe python -c "import onnxruntime as o; print(o.__version__, o.get_available_providers())"
conda run -n spica-win-probe python -m pip freeze
conda run -n spica-win-probe python -m pip check
```
预期:torch `2.5.1+cu124 12.4 True <GPU名>` + matmul 出数;providers 先含 `CUDAExecutionProvider`,装 TRT 后再查是否出现 `TensorrtExecutionProvider`;tensorrt_libs 目录列出 `nvinfer*.dll`(即 `rapidocr_trt_runtime.py` 的 `libnvinfer.so.10` Windows 等价名——直接喂 W4-b preload 设计)。**TRT EP 不出现不算失败**(§5-W4 gate (c):如实降级 CUDA EP 记录)。

### E. spica-win clone 升级探针(关键,不能跳)

目的:验证 base env 升级重型依赖是否破坏 W2-a/W3 清单,尤其 **CPU onnxruntime 1.27.0 → onnxruntime-gpu 1.26.0** 的替换。

```powershell
conda create --clone spica-win -n spica-win-heavy-probe -y
conda run -n spica-win-heavy-probe python -m pip uninstall -y onnxruntime
conda run -n spica-win-heavy-probe python -m pip install onnxruntime-gpu==1.26.0
conda run -n spica-win-heavy-probe python -c "import onnxruntime as o; print(o.__version__, o.get_available_providers())"
conda run -n spica-win-heavy-probe python -m scripts.local_runtime.doctor
conda run -n spica-win-heavy-probe python -m pip freeze
conda run -n spica-win-heavy-probe python -m pip check
```
纪律:若卸 CPU 包后 rapidocr/faster-whisper 的 metadata 依赖(pip check)报红但 runtime import 可用,**如实记录,不修正式安装**。

## 4. Windows 侧回填(2026-07-05 真机;spica-win 零污染,重型只进 probe/clone env)

> 机器:Windows 10 Enterprise LTSC 2021 `10.0.19044` / conda `25.11.1` / HEAD `b206f51`。原始回传草稿 `w4a-windows-probe-log.md`(untracked,不入库)。

### 4.A W2-DPI-MULTISCREEN → 无第二屏,走 waiver(建议,待审批窗口显式落定)

`mss` monitors 实测**仅一块物理屏**(index0=虚拟包围盒、index1=唯一物理屏,同尺寸同原点):

```
monitor count: 2
  0 {'left':0,'top':0,'width':3840,'height':2160}
  1 {'left':0,'top':0,'width':3840,'height':2160,'is_primary':True,'name':'Generic PnP Monitor'}
```

**Waiver 建议**:风险边界 = 多屏拓扑(跨屏原点/负坐标)+ 异构 per-monitor DPI 坐标空间**未验**;不阻塞 W4 理由 = W4-b 改动面(OCR preload / TRT runtime)**不触碰几何路径**、§6.3 W4 smoke 单屏可完成、单屏 200% 全链零偏移已在 W2 §4 证过;挂账 **W2-DPI-MULTISCREEN 顺延 W5 前置**(有第二屏时补 §6.1 条目 7,约 15 分钟)。**§8 挂账行的 waiver 落定权归审批窗口,本文档只给建议。**

### 4.B GPU 底座

```
nvidia-smi: NVIDIA GeForce RTX 4090, driver 596.49, 24564 MiB
where cudnn64*.dll  -> C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v12.8\bin\cudnn64_8.dll
where cublas64*.dll -> C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v12.8\bin\cublas64_12.dll
CUDA_PATH = C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v12.8   nvcc: release 12.8, V12.8.61
```

- driver **596.49 ≥ 551.61**(cu124 下限)✓;GPU = RTX 4090 24GB(同 Linux golden);
- 本机装了**系统 CUDA Toolkit v12.8**(与 Linux golden「全 pip 包、无系统 Toolkit」不同),系统 cuDNN 是 **8.x**;但重型栈不依赖它(见 4.C:ct2/torch wheel 自带 cuDNN 9,filename 版本后缀 8/9 不冲突)。

### 4.C spica-win 现状(纯读)

```
ctranslate2 4.8.1   cuda_device_count = 1
ct2 dir DLLs: ['ctranslate2.dll', 'cudnn64_9.dll', 'libiomp5md.dll']
```

**关键**:ct2 4.8.1 wheel **自带 `cudnn64_9.dll`**——W3 的 STT-CUDA 就是靠 ct2 包内自带 cuDNN 9 跑起来的,不依赖系统 8.x。自包含,解释了 W3 之谜。

doctor(正常运行):`onnx_providers=[Azure,CPU]`、`cuda_ep=false tensorrt_ep=false tensorrt_importable=false nvidia_driver=true`——spica-win 是 CPU onnxruntime,符合预期。pip check:仅 pipx/platformdirs base 全局工具串场(W2-a 同款观察),spica-win 核心包无冲突。

### 4.D probe env 三级阶梯(`spica-win-probe`,py3.11)

**① torch 2.5.1+cu124**:`cuda 12.4  is_available()=True  RTX 4090`;matmul(2048² @ device=cuda)= 2146373248.0(真 GPU 计算)✓。(裸 env 无 numpy → torch 报无害 NumPy 警告;下步 ort-gpu 拉入 numpy 2.4.6。)

**② onnxruntime-gpu 1.26.0** — CUDA EP **真实 init 对照**(同 env 仅导入顺序不同,W4-b 定序关键证据):

```
WITHOUT torch:  Error loading onnxruntime_providers_cuda.dll -> cudnn64_9.dll MISSING (Error 126)
                Failed to create CUDAExecutionProvider (require cuDNN 9.* + CUDA 12.* in PATH)
                ACTIVE = ['CPUExecutionProvider']              CUDA_ACTUALLY_ACTIVE: False
WITH torch first:  ACTIVE = ['CUDAExecutionProvider','CPUExecutionProvider']   CUDA_ACTUALLY_ACTIVE: True
```

→ **CUDA EP 硬需 cuDNN 9 在 DLL 搜索路径;`import torch` 经 `os.add_dll_directory` 注册 `torch\lib`(自带 cuDNN 9)使同进程 ORT CUDA EP 真 init 成功。`get_available_providers()` 是静态编译清单,不等于真 init。**

**③ TensorRT(tensorrt-cu12==10.16.1.11)**:安装**首次 sha256 不匹配失败 → 重试成功**(transient 下载损坏,非确定性 blocker,W4-b 安装文档记「装失败先重试」);`tensorrt_libs` DLLs 含 **`nvinfer_10.dll`**(= `libnvinfer.so.10` 的 Windows 目标名,直喂 W4-b preload)+ nvinfer_plugin_10 / nvonnxparser_10 + 各 sm 架构 builder resource。TRT/CUDA EP **真实 init**(import torch 补 cuDNN9 + `os.add_dll_directory(tensorrt_libs)` 补 nvinfer 后建 session):

```
TRT-session ACTIVE = ['TensorrtExecutionProvider','CUDAExecutionProvider','CPUExecutionProvider']
TRT_ACTUALLY_ACTIVE: True    CUDA_ACTUALLY_ACTIVE: True
```

→ **本机全 GPU 链(CUDA + TRT EP)实测可达**,条件是进程内 cuDNN9(torch)+ nvinfer(tensorrt_libs)都上 DLL 搜索路径。probe env key freeze:`torch==2.5.1+cu124 / torchaudio==2.5.1+cu124 / onnxruntime-gpu==1.26.0 / numpy==2.4.6`。

### 4.E clone 升级探针(CPU onnxruntime → onnxruntime-gpu)

`conda create --clone spica-win -n spica-win-heavy-probe` → 卸 `onnxruntime 1.27.0`(CPU)→ 装 `onnxruntime-gpu 1.26.0`。

**pip check(替换后):metadata 红、runtime 绿**——`faster-whisper`/`rapidocr-onnxruntime` 声明依赖发行名 `onnxruntime`(未安装),但 `rapidocr_onnxruntime OK / faster_whisper OK / onnxruntime 1.26.0` runtime import 全可用。按纪律如实记录不修正式安装;W4-b 在 heavy 清单记该已知不洽,不倒灌 base。numpy **保持 1.26.4 未被顶**(`<2` 侧守住)。key freeze:`onnxruntime-gpu==1.26.0 / rapidocr-onnxruntime==1.4.4 / faster-whisper==1.2.1 / ctranslate2==4.8.1 / numpy==1.26.4 / PySide6==6.11.1 / webrtcvad-wheels==2.0.14 / PyAudio==0.2.14`。

**关键:torch-less clone 里 CUDA EP「列出但 init 失败」**(拿 rapidocr 自带 cls onnx 真建 session):

```
build providers: ['Tensorrt','CUDA','CPU']    # 静态编译清单
[E] Error loading onnxruntime_providers_cuda.dll -> cudnn64_9.dll missing (Error 126)
ACTIVE = ['CPUExecutionProvider']    CUDA_ACTUALLY_ACTIVE: False
```

→ **裸 CPU→GPU 替换不足以得到可用 CUDA EP**:onnxruntime-gpu 1.26 需 cuDNN 9 在 DLL 搜索路径,本机系统只有 cuDNN 8.x(filename 不同名不冲突但也不满足),故 fallback CPU。补 cuDNN 9 的路子:heavy env 含 torch(`torch\lib` 自带 + add_dll_directory 注册)／`nvidia-cudnn-cu12` pip 包／把 ct2 自带的 `cudnn64_9.dll` 目录上 DLL 搜索路径。**注意:cudnn64_9.dll 在 clone 里其实已存在(ct2 包内),但不在 ORT 的 DLL 搜索路径 → 存在≠可发现**,故必须显式注册目录。

## 5. env 策略推荐 + W4-b ready 裁决建议(§4 实测定稿)

### 5.1 版本矩阵(Windows 实测 vs Linux golden)

| 组件 | Windows 实测 | Linux golden | 判读 |
|---|---|---|---|
| GPU / driver | RTX 4090 / **596.49** | RTX 4090 / 555.42.06 | 同卡;Win driver ≥551.61 ✓ |
| torch / torchaudio | 2.5.1+cu124 / 2.5.1 | 2.5.1+cu124 / 2.5.1 | 对齐;cuda 12.4、matmul 出数 |
| ctranslate2 | 4.8.1(spica-win 现装) | 4.7.2 | Win 更新,cuda_device_count=1 已通 |
| onnxruntime-gpu | 1.26.0 | 1.26.0 | 对齐 |
| TensorRT | 10.16.1.11(`nvinfer_10.dll`) | 10.16.1.11 | 对齐;EP 真 init |
| cuDNN | 系统 8.x + **torch/ct2 自带 9**(有效 9) | pip 9.1.0.70 | 有效 cuDNN 9 来自 wheel 自带,非系统 |
| numpy | spica-win 1.26.4(`<2`);裸 probe 2.4.6 | 主 `<2`(1.26.4) / RVC 2.4.6 | spica-win 天然在 `<2` 侧 |

### 5.2 env 策略三裁(§5-W4 内容 1 三问)

1. **RVC 独立 env(`spica-win-rvc`, numpy 2.x)**:numpy 冲突平台无关,Windows wheel 7/7 齐备(§2),`rvc/driver.py::worker_python` seam 已支持。无 blocker。
2. **spica-win 可升级为主重型 env(TTS/OCR/STT)**:**可行**。numpy 1.26.4 本在 `<2` 侧;§4.E 实测卸 CPU-ort 换 gpu 后 numpy 未被顶、rapidocr/faster-whisper runtime 全 OK。
3. **onnxruntime CPU→GPU 替换**:**采纳「卸 CPU 包装 gpu 包」**。已知不洽 = pip check metadata 红(`onnxruntime` dist 名未满足)但 runtime 正常 → W4-b 在 **heavy 清单**如实记录,**不倒灌 base**、不改生产 requirements。

### 5.3 W4-b 设计硬输入(preload 定序——本次最重要产出)

onnxruntime-gpu 的 **CUDA/TRT EP 硬依赖两组 DLL 在进程搜索路径**,缺则「provider 列出但 init 失败 → 静默回落 CPU」(doctor `cuda_ep=true` 是静态编译清单,**非真 init**):

- **cuDNN 9**(`cudnn64_9.dll`):来源可选 torch\lib(import torch 自动注册)/ nvidia-cudnn-cu12 / ct2 包内目录;
- **nvinfer**(`nvinfer_10.dll`):`os.add_dll_directory(tensorrt_libs 目录)`。

→ **W4-b 的 rapidocr preload 必须:建任何 onnxruntime CUDA/TRT session 前,把 cuDNN9 目录 + tensorrt_libs 目录注册进 DLL 搜索路径。** `nvinfer_10.dll` 即 `rapidocr_trt_runtime.py` 中 `libnvinfer.so.10` 的 Windows 目标名。这**坐实了计划 A7 的「不足才加 add_dll_directory 变体」分支从「可能」变「确定必要」**,但落点仍在既有白名单(两个 preload 函数内),不扩白名单。

### 5.4 TensorRT 可达性

**可达**:`nvinfer_10.dll` 在位,TRT EP 实测 init 成功。W4-b 可按 Linux golden 走 det/rec→TRT、cls→CUDA(cls 图不支持 TRT 属 Linux 已知,Windows 沿用);`classify_load_status` 诚实回退链保留。安装注意 `tensorrt-cu12` 有 transient sha256 失败风险 → 装失败先重试。

### 5.5 W2-DPI-MULTISCREEN

本机单屏 → waiver(§4.A)。风险边界=多屏/异构 DPI 坐标未验;不阻塞 W4(改动面不碰几何);挂账顺延 W5。**§8 落定权归审批窗口。**

### 5.6 W4-b ready 判据核对(建议放行,裁决归审批窗口)

- [x] §4 A–E 回填完毕(A waiver / B / C / D 三级全绿 / E 替换验证);
- [x] 版本矩阵定稿(§5.1);
- [x] env 策略三裁落字(§5.2);
- [x] W2-DPI → waiver 显式建议(§4.A / §5.5);
- [x] W4-b 最重要设计输入(preload 定序)已产出(§5.3)。

### 5.7 施工窗口 review 附注(供 W4-b 审批参考,两条)

1. **A7 悬念已判定为「必须动 preload」**:计划原设想「conda PATH + ORT 自身发现或许够」,§4.D/§4.E 确定性证否——裸发现找不到 cuDNN 9。W4-b **必然**在 `rapidocr.py` / `rapidocr_trt_runtime.py` 预载函数内加 DLL 目录注册(在白名单内,不扩面)。附带 W4-b 一个设计选择:preload 是显式 `import torch`(把 OCR 耦合 torch),还是更外科地 `add_dll_directory` 定位 cuDNN9 目录(heavy env 里 ct2 自带该 dll,无需 import torch)——**倾向后者**,归 W4-b 定。
2. **一个未闭合缝(非 W4-a blocker,列为 W4-b 第一道 gate)**:探底是**分件验证**——裸 probe 证了 torch+ort-gpu(numpy 2.4.6),clone 证了 ort-gpu 无 torch(numpy 1.26.4)。但**「clone + 补 torch + numpy 保持 1.26.4」这个真正的 heavy env 未作为单一环境端到端组装并跑一次真 OCR CUDA session**。逻辑链(torch 带 cuDNN9→EP 可 init;numpy 1.26.4 与 ort-gpu/torch 2.5.1 共存)是硬推理但非实测。**建议 W4-b 第一步先组装该 heavy env 真 init 验证,再动 preload 代码。**

### 5.8 W4-b 白名单草案(供下一审批窗口)

`agent_tools/function_tools/screen/backends/rapidocr.py`(仅预载函数,加 DLL 目录注册)、`spica/local_runtime/ocr/rapidocr_trt_runtime.py`(仅 preload 函数族,nvinfer Windows 名 + tensorrt_libs 目录)、新建 requirements-windows-heavy / -tts / -rvc 清单、windows 分支 GPU 验收配置 commit(`13a0cbe` 形制重打:provider/device/screen.enabled 切 GPU 值)、`tests/` 新增;vendored 零改动;`build_ocr_adapter`/共线设计/port 签名不碰。

---

> 探底副作用:新建 conda env `spica-win-probe`、`spica-win-heavy-probe`——**正式 `spica-win` 未动**,二者可 review 后 `conda env remove` 清理。仓库零 commit。

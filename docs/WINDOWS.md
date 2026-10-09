# Windows 原生安装与验收

本页对应独立桌宠版。桌宠、Qwen 语音识别、GPT-SoVITS、屏幕识别与 RVC 使用同一个 Windows Python 环境；无需四态生态的共享核心、QQ 节点、手机网关或机器人服务。Home 默认关闭。

## 1. 选择一个环境

使用 64 位 Python 3.11。已有可用环境就直接激活；新用户只创建一次：

```powershell
conda create -n spica python=3.11 -y
conda activate spica
```

进入项目目录。以下脚本使用当前 `python`，不会另外创建虚拟环境。默认只打印计划；`--install` 才安装，`--check` 只核对当前环境。

文字聊天及云端 ASR：

```powershell
python scripts/windows/setup_environment.py --profile base --install
```

NVIDIA GPU、本地语音及唱歌：关闭使用该环境的 Spica 进程，再执行：

```powershell
python scripts/windows/setup_environment.py --profile full
python scripts/windows/setup_environment.py --profile full --install
python scripts/windows/setup_environment.py --profile full --check
```

完整配置使用 PyTorch 2.6.0 / CUDA 12.4、Transformers 4.57.6、Tokenizers 0.22.2、Accelerate 1.12.0、NumPy 1.26.4、Qwen-ASR 0.0.6 和 Silero VAD 6.0.0。Linux 的主环境旧版 Transformers 与独立 Qwen 环境保持独立的平台标记。

已有 GPU ONNX Runtime 的环境再次安装时仍选择 `full`；`base --install` 会拒绝覆盖已有 GPU 提供程序。

CPU `onnxruntime` 与 `onnxruntime-gpu` 提供同名模块，不能共存。完整安装最后移除 CPU/DirectML 包并重新安装 GPU 包；后续手工安装依赖时也要检查这一点。RapidOCR、Silero 的元数据仍可能要求 CPU 包名，`pip check` 中这类报告不能通过把 CPU 包装回来解决。`audio-separator` 使用项目已有的 NumPy 1.26 兼容组合，以 `--no-deps` 安装。运行检查及真实推理是额外的验收步骤。

准备 `ffmpeg`、`ffprobe`，确保它们在 PATH 中。安装脚本不下载大模型、不安装驱动或系统服务，不设置开机自启，也不控制设备。

## 2. 先跑通文字

Git 副本已经有 `data/config/app.yaml` 模板。首次使用可以显式选择静态示例、关闭尚未安装的可选功能，并保存服务商配置：

```powershell
python scripts/setup_desktop.py --configure-text --model YOUR_MODEL_ID --api-base https://YOUR_PROVIDER/v1
```

此选项会改变当前角色选择及语音、Home、屏幕等开关。已有配置的用户直接在设置中调整。`--write-config` 仅用于 app.yaml 不存在的情况，始终拒绝覆盖已有文件。两个入口都会真正导入示例，生成桌宠可加载的安装副本。

启动后在设置中保存 API Key。密钥只在本机 `xiaosan.env`，不会进入角色包或 Git。

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\windows\run_spica.ps1 -CondaEnv spica
```

也可以明确指定现有解释器，路径支持空格、中文和方括号：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\windows\run_spica.ps1 -PythonExe "C:\path to env\python.exe"
```

启动器会定位项目目录，并为主进程和 Python 工作进程启用 UTF-8。

## 3. 复用本机模型和角色

角色通过设置中的“导入角色文件夹”导入。程序复制角色资源到本项目的 `data/runtime/characters`，运行状态和记忆属于本项目。不要覆盖整个旧项目的配置或数据库。

模型权重、角色卡和素材不随代码安装。已有 Qwen 模型无需重新下载：

```powershell
python scripts/setup_qwen_asr.py --model-dir "D:\models\Qwen3-ASR-1.7B" --device cuda --write-config
```

Windows 上这个命令核对并使用当前 Python，不创建 `.venv-qwen-asr`。设置中的“本地识别 Python”可留空；CPU 使用 `float32`，支持的 NVIDIA GPU 使用 `bfloat16`。只有显式加 `--download` 才下载模型。

GPT-SoVITS 的公共权重放在 `artifacts/tts_slim/base/GPT_SoVITS/pretrained_models`；角色自己的 GPT/SoVITS 权重与参考录音随角色包。RVC 的公共 ContentVec/RMVPE 模型属于 `artifacts/rvc_slim/base/rvc/models`。具体字段见[声音配置](README.md#voice)及[角色包参考](CHARACTER_PACKS.md)。

Moondream 使用模型 ID `vikhyatk/moondream2` 和固定版本 `2025-06-21`，保留 Hugging Face 的标准缓存结构：

```powershell
python -c "from huggingface_hub import snapshot_download; snapshot_download('vikhyatk/moondream2', revision='2025-06-21', allow_patterns=['*.py','*.json','*.txt','model.safetensors'])"
```

这个模型的自定义代码带有多层相对导入。当前 Transformers 的普通本地目录加载方式可能遗漏间接模块；本轮验收采用上述模型 ID / 固定版本 / 标准缓存方式，没有修改模型代码或 Transformers。不要把手工展开的目录直接填作此模型的 ID。

网易云登录由用户完成。重用模型不等于复制登录态。可选 Cubism 依赖见 [Cubism](CUBISM_PACKS.md)，Home 的硬件、灯控与实际闹钟验证见 [Home](HOME.md)。

## 4. Windows 实现边界

- 布局、音量、唤醒词等配置使用 Windows 文件锁、文件身份与私有 ACL；拒绝目录联接和多硬链接的受管文档。Linux 继续使用 POSIX 锁及权限。
- 事务会保留原始字节用于恢复。Windows 不声称拥有 POSIX 目录 fsync 的持久化保证，保存成功时可能记录 `DOCUMENT_DURABILITY_UNCONFIRMED`。
- 游戏记忆数据库每次操作后关闭连接，避免文件被一直占用。
- Qwen 工作进程先初始化数学库，再开始读取父进程管道，避免 Windows 启动死锁；关闭、超时仍由所属桌宠回收。
- Linux Bash 数据库恢复演练只在 Linux 执行。跨平台 SQLite 迁移、备份及恢复辅助程序的测试仍在 Windows 执行。

## 5. 运行验收

```powershell
python scripts/windows/setup_environment.py --profile full --check
$env:PYTHONUTF8 = "1"
$env:QT_QPA_PLATFORM = "offscreen"
python -m pytest tests -q
```

环境检查只验证版本、导入和 GPU 可用性，不等于实际合成、录音、播放或硬件已经通过。实际验收还应覆盖文字回复、角色/换装、设置重启保持、真实 ASR/TTS、屏幕识别、退出后工作进程回收。Home 的灯、显示器和整夜唤醒须按本机硬件另行验证。

测试完成后，在启动桌宠的终端清除 `QT_QPA_PLATFORM`（`Remove-Item Env:QT_QPA_PLATFORM`），或另开终端，避免使用无窗口的测试平台。此次结果见[Windows 验收记录](WINDOWS_ACCEPTANCE_20261009.md)。

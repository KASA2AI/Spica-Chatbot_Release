**简体中文** · [English](README.en.md) · [日本語](README.ja.md) · [返回主页](../README.md)

# 配置与自定义：照着样例做一遍

先跑通文字聊天，再换角色、加声音和改对话框。下面命令都在**项目根目录**执行，使用已激活的 `spica` 环境；尚未安装基础依赖请先看[主页](../README.md)。

[启动](#setup) · [导入](#import) · [角色卡与立绘](#character) · [声音](#voice) · [动态立绘](#animation) · [对话框](#dialogue) · [修改本机设置](#settings)

<a id="setup"></a>
## 1. 先启动文字聊天

新建 UTF-8 的 `xiaosan.env`，填入自己的 key。本例使用 [DeepSeek 兼容 API](https://api-docs.deepseek.com/)，key 从[控制台](https://platform.deepseek.com/)获取；也可按自己的服务修改模型与地址。

```dotenv
OPENAI_API_KEY=REPLACE_WITH_YOUR_API_KEY
```

在 `data/config/app.yaml` 合并以下字段，保留其他设置。先关闭语音等可选功能，避免首次启动依赖尚未下载的模型。

```yaml
llm:
  provider: openai_compatible
  model: deepseek-v4-flash
  base_url: https://api.deepseek.com
  reasoning_effort: none
  system_turn_reasoning_effort: none
character:
  interlocutor_name: Friend
  dialog_display_language: zh
  package_dir: null
  profile_override: null
tts:
  enabled: false
stt:
  backend: qwen_asr
  mic_backend: generic
  model: models/stt/Qwen3-ASR-1.7B
  device: cpu
  compute_type: float32
  language: zh
  warmup_on_startup: false
screen:
  enabled: false
anime:
  enabled: false
song:
  enabled: false
```

首次使用可安装自带的小型静态示例再启动；已有角色的用户只运行最后一行：

```bash
python -c "from pathlib import Path; from spica.host.character_packages import import_character_folder; from spica.config.manager import ConfigManager; p = import_character_folder('Desktop-Packs/Characters/Examples/static', Path('data/runtime/characters')); ConfigManager().update({'character': {'package_dir': p.package_root, 'profile_override': None}}); print(p.name)"
python -X utf8 webui_qt.py
```

输入一句话，有文字回复即完成。字幕 `zh` 为中文、`ja` 为日文；这不是语音语言开关。普通麦克风保留 `mic_backend: generic`。密钥只放 `xiaosan.env`；若存在旧 `MODEL`、`OPENAI_BASE_URL` 或 `SPICA_USER_NAME` 环境项，先清理冲突值。

<a id="import"></a>
## 2. 使用现成角色与样式

1. 下载[角色资源包](https://pan.baidu.com/s/1RDoz_iRvFJdB71Aia3EPaw?pwd=j2u6)，提取码 **`j2u6`**，先解压 ZIP。
2. 齿轮设置 → **导入角色文件夹** → 选择直接包含 `meta.json` 的目录。
3. 需要配套对话框时，在样式区另外导入直接包含 `style.json` 的目录。
4. 等待导入成功 → **重启程序**。角色与样式独立选择，都在重启后生效。

建议源包放在 `Desktop-Packs/Characters/`、`Desktop-Packs/Dialogue-Styles/`；安装副本由程序保存在 `data/runtime/`。选择具体包，不选择 ZIP 或集合上一级。这个纯桌宠版不使用手机／机器人背景；要改桌面对话底图，见[对话框](#dialogue)。

<a id="character"></a>
## 3. 制作自己的角色卡与立绘

复制[静态示例](../Desktop-Packs/Characters/Examples/static/)为 `Desktop-Packs/Characters/my-character/`，先保留原结构：

```text
my-character/
├── meta.json
├── persona.md
├── worldbook.md
└── visuals/
    ├── idle.png
    └── happy.png
```

用开源 [CCEditor](https://lenml.github.io/CCEditor/?lang=zh)（[源码](https://github.com/lenML/CCEditor)）编写角色卡，也可直接编辑 `persona.md`。外部卡这样转入：

| 内容 | 填到哪里 |
| --- | --- |
| 名字 | `meta.json` 的 `name`（列表名称）和 `char_name`（角色称呼） |
| 身份、性格、语气、对话例句 | `persona.md` |
| 世界观、人物关系 | `worldbook.md`；保留 `worldbook_file` 引用 |
| 独立角色身份 | `slug` 改为 `my-character`；同一角色升级保持不变 |

`persona.md` 的最小起点：

```markdown
你是 {{char}}，正在桌面陪 {{user}} 聊天。
性格温和、好奇，回答简洁自然，偶尔开一句轻松的玩笑。
称呼使用 {{user}}，不要沿用原故事主人公的名字。
```

SillyTavern / CCEditor 的 PNG 卡或 V2/V3 JSON 需要提取上述文本，不能直接改名为 `meta.json` 导入。

用同尺寸、同人物位置的透明 PNG 替换 `idle.png`、`happy.png`。添加表情时先在 `visuals.sprites` 声明路径，再在当前服装的 `emotions` 里引用 ID，例如 `"happy": ["happy"]`；支持 `happy/angry/sad/surprised`。新服装复制 `costumes` 中的一项，改 `id`、`label`、`default_sprite`，并补齐对应图片映射。

所有资源路径相对角色包，使用 `/`，不写绝对路径或 `..`，文件名不要只靠大小写区分。保存后重新导入并重启；更多字段见[角色包参考](CHARACTER_PACKS.md)。

<a id="voice"></a>
## 4. 加入声音（可选）

| 用途 | 所需模型 |
| --- | --- |
| 角色讲话 | 成套 GPT-SoVITS **v2Pro / v2ProPlus**：GPT `.ckpt`、SoVITS `.pth`、参考 WAV |
| 麦克风识别 | `Qwen3-ASR-1.7B`，整份模型目录 |
| 唱歌 | RVC v2 `.pth`，可附配套 `.index`；与讲话模型分开 |

**本机环境只安装一次。** 完成基础安装后，NVIDIA / Python 3.11 使用以下顺序；保留 NumPy 1.26.x，不要把独立 RVC 环境的 `requirements-rvc.txt` 混装进来。


Windows 完整语音环境使用同一个 Python，先执行 `python scripts/windows/setup_environment.py --profile full --install`，再用 `--check` 核对；详见 [Windows 原生指南](WINDOWS.md)。下面的手工安装顺序保留给 Linux 主环境；Linux 的 Qwen 仍使用独立工作环境。

```bash
python -m pip uninstall -y onnxruntime
python -m pip install -r docs/requirements/requirements-windows-heavy.txt
python -m pip install torch==2.5.1 torchaudio==2.5.1 torchvision==0.20.1 --index-url https://download.pytorch.org/whl/cu124
python -m pip install -c docs/requirements/constraints-windows-app.txt -r docs/requirements/requirements-windows-app.txt
python -m pip install -c docs/requirements/constraints-windows-app.txt --no-deps audio-separator==0.44.2
```

确认 `ffmpeg -version` 能运行。共享语音权重与 STT 模型不放进角色包，缺少时分别从 [GPT-SoVITS](https://huggingface.co/lj1995/GPT-SoVITS) 和 [Qwen3-ASR 模型仓库](https://huggingface.co/Qwen/Qwen3-ASR-1.7B)下载；已有完整文件可跳过：

```bash
python -c "from huggingface_hub import snapshot_download; snapshot_download('lj1995/GPT-SoVITS', local_dir='artifacts/tts_slim/base/GPT_SoVITS/pretrained_models', allow_patterns=['chinese-hubert-base/*', 'chinese-roberta-wwm-ext-large/*', 'sv/*', 's1v3.ckpt', 'v2Pro/*'])"
python -c "from huggingface_hub import snapshot_download; snapshot_download('Qwen/Qwen3-ASR-1.7B', local_dir='models/stt/Qwen3-ASR-1.7B')"
```

**角色声线单独准备。** 将作者提供的成套模型和约 3～10 秒干净参考录音放入 `voice/`，在原 `meta.json` 顶层追加 `tts`，不要用这段替换整个文件：

```json
{
  "tts": {
    "engine": "gptsovits",
    "model_version": "v2ProPlus",
    "gpt": "voice/gpt.ckpt",
    "sovits": "voice/sovits.pth",
    "target_language": "日文",
    "reference": {
      "audio": "voice/reference.wav",
      "text": "今日は一緒にお話しできて、うれしいです。",
      "language": "日文"
    }
  }
}
```

模型版本填真实版本，`text` 换成录音逐字原文；`日文/中文/英文` 是固定枚举。当前聊天语音以日文为主，改这个字段不会自动切换整条聊天语言。模型制作见 [GPT-SoVITS](https://github.com/RVC-Boss/GPT-SoVITS)。

最后把本机 `tts.enabled` 改为 `true`，重新导入角色并重启，测试一句短话。要用 GPU 识别，将 `stt.device` / `compute_type` 改为 `cuda` / `float16`，识别中文用 `language: zh`。唱歌还需共享 RVC 权重和本机 `song.enabled: true`，包内字段见[声音与 RVC 参考](CHARACTER_PACKS.md#加入角色声音)。

<a id="animation"></a>
## 5. 制作动态立绘

复制[眼部动态示例](../Desktop-Packs/Characters/Examples/eye-rig/)后改 `slug` 和角色卡。它使用 `spica-eye-rig`，支持眨眼和视线跟随，不能直接导入 Cubism 的 `.model3.json` / `.moc3`。

1. 替换 `model/open.png`、`closed.png`：完整透明原图，尺寸、姿势和人物位置相同，只改变眼部。
2. 编辑 `model/character.eyerig.json`：`canvas` 填原图尺寸，`gaze.origin` 设在两眼之间；按自己的图片重画 `eyes` 的眼眶、虹膜和闭眼曲线，不能套用示例坐标。
3. 保留 `pack_format: 2`、`renderer: eye-rig` 和 `eye_rigs` 映射，导入并重启，观察眨眼、视线与换装。

所有坐标以原图左上角为原点；眼部 `bounds` 在两张图中必须完全不透明。标定字段及多服装示例见[动态包参考](CHARACTER_PACKS.md#live2d-包当前支持的是眼部原画动画)。

<a id="dialogue"></a>
## 6. 制作对话框

复制 [`Desktop-Packs/Dialogue-Styles/sana/`](../Desktop-Packs/Dialogue-Styles/sana/)为 `Desktop-Packs/Dialogue-Styles/my-style/`，保留这两个部分：

```text
my-style/
├── style.json
└── images/
    ├── surface.png
    ├── nameplate.png
    └── tail.png
```

| 想改什么 | 修改哪里 |
| --- | --- |
| 列表里的名称 | `style_id` 改为 `my-style`，修改 `name` 和 `author` |
| 对话底板、姓名底板 | 替换 `surface.png`、`nameplate.png`；保留透明通道 |
| 按钮颜色、文字颜色 | `colors.accent`、`colors.text`，例如 `#E94E8B` |
| 字号、间距、透明度 | `text.size`、`layout.left/right`、`layout.surface_opacity` |
| 文末动画 | 替换 `tail.png`，同步修改 `tail` 的网格参数 |

Sana 示例尾标是 **5 列 × 4 行、20 帧、每帧 50ms**，从左到右逐行播放。自己的图集须能整除行列数；静态图把 `columns/rows/frames` 都改成 `1`。`placement: corner` 放右下角，`inline` 放文字末尾。装饰字可以画进底图，实际角色名字由程序绘制。

设置里单独导入该文件夹并重启。按钮功能和设置排版保持原样。图片须为声明的 `images/` 下 PNG，单图 ≤8 MiB、单边 ≤4096、总像素 ≤800 万；完整范围见[对话框参考](DIALOGUE_STYLES.md)。

## 7. 移除与分享

下拉列表右侧 **× → 确定移除** 只移除列表项，源包和记忆保留，以后可重新导入。**导出分享包**不包含程序保存的私人记忆；**导出个人存档**用于迁移自己的记忆。导出到新的文件夹，再压缩 ZIP；分享前检查角色卡里是否手写了私人内容，并注明素材来源。称呼、角色和样式修改后重启生效。

<a id="settings"></a>
## 8. 修改本机设置

在桌宠设置中切换两个分页：

- **角色与外观**：角色、服装、对话框、称呼、缩放、透明度和音量。
- **应用设置**：API 地址、模型、推理强度、语音识别，以及语音、屏幕理解、唱歌、追番和陪玩插话。设备、预热和恢复默认值在“高级选项”中。

填写 API Key 后点击 **保存密钥**，其余修改点击 **保存应用设置 → 重启程序**。密钥留空会保留原值；测试连接只检查模型列表，不发送聊天。首次未配置 API Key 时会自动打开应用设置。

应用设置保存在本机 `data/config/app.yaml`，密钥在 `xiaosan.env`，切换角色会保留它们，分享包不携带密钥。旧环境变量或旧配置文件覆盖某项时，该项会显示来源并禁止保存冲突值；清理对应来源后重启。恢复默认值只修改此页表单，仍需保存，不会清除角色、记忆或密钥。浏览器配置中心已退役。

## 遇到问题

- **没立绘**：确认导入的是含 `meta.json` 的那一级，图片映射存在，已完成重启。
- **没声音**：检查成套模型、参考原文、共享权重及 `tts.enabled`；导入成功不等于实际合成成功。
- **称呼或模型不更新**：检查旧环境变量覆盖；保存后重启。
- **Windows 路径报错**：本机 YAML 用 `C:/Spica/models/file`，包内始终用相对路径；别把 ZIP 当文件夹导入。

## 语音识别：本地 Qwen3-ASR / 百炼云端

两种方式任选一种，在 **设置 → 应用设置 → 语音识别** 中切换，保存后重启生效。

### 显存充足：本地识别

使用 **Qwen3-ASR-1.7B**。识别录音不上传；模型放在角色包之外。
请用安装 Spica 的 Python 运行下面的命令，主环境需要已经安装可用的 PyTorch：

```bash
python scripts/setup_qwen_asr.py --device cuda --download --write-config
```

Linux 上该脚本创建 `.venv-qwen-asr`，复用主环境 PyTorch，并将 Qwen 的依赖安装到
独立环境。Windows 上先完成 [单环境安装](WINDOWS.md)，此脚本只检查并复用当前 Python，不创建另一个环境。`--download` 下载模型；
`--write-config` 只更新 app.yaml 的本地 ASR 配置，不修改密钥和角色。两个参数均可省略。
Windows 和 Linux 使用同一命令；不支持 CUDA 的设备可将 `cuda` 改为 `cpu`，CPU 使用 float32。

如果已有模型，可加 `--model-dir "模型完整目录"` 并省略 `--download`。
如果手动配置，填写“本地模型目录”；Windows 的“本地识别 Python”留空以使用当前解释器，Linux 填独立环境的 `bin/python`。程序不会在聊天时自动下载模型。

### 显存较小：云端识别

1. 安装主环境的 `docs/requirements/requirements-stt.txt`，不需要运行上面的本地安装脚本。
2. 在应用设置中填写并保存 **百炼语音识别 API Key**。
3. 选择 **百炼 Qwen 云端**，按密钥所属服务地域选择北京或新加坡。
4. 保持云端模型 `qwen3-asr-flash`，保存应用设置，然后重启。

云端模式会将有效录音发送到所选百炼服务，可能产生服务商费用；不加载本地识别模型。
启动预热、自检不上传录音，也不代表已验证云端账户可用。本地识别失败不会自动转云端。

### 麦克风与音箱

点击“刷新音频设备”，选择输入麦克风和输出音箱，保存后重启。
“系统默认”跟随操作系统；指定设备断开则报错，不自动改用别的设备。
普通 USB/系统麦克风在两个平台均可使用；只有使用 ReSpeaker 硬件 VAD 时才选择该选项。

旧 Whisper/Google 配置会读取为本地 Qwen 默认配置；请重新填写模型目录及对应平台的 Python，
或明确选择云端。密钥保存在本机 xiaosan.env，不放在 app.yaml 或角色包内。


## 独立桌面整合后的入口

- `scripts/setup_desktop.py --install`：在 `.venv-desktop` 安装基础依赖；不下载语音模型。
- `--write-config --model 模型ID [--api-base 服务地址]`：只在 app.yaml 不存在时写入初始文字配置，
  使用仓库小型示例角色；已有用户不运行这个选项。密钥在设置中保存。
- `--configure-text --model 模型ID [--api-base 服务地址]`：导入静态示例、选择它并关闭可选功能，适用于仓库已有配置模板的首次安装。
- `scripts/setup_qwen_asr.py`：Windows 复用当前环境，Linux 保留独立 Qwen 环境；`--download` 才下载模型，`--write-config` 才改 ASR 配置。
- [语音唤醒](VOICE_WAKE.md)：可选小模型、呼叫后的短接话窗口、完全禁麦。
- [Home](HOME.md)：相机/区域、专用音箱、传感器/灯、闹钟、平台电源配置与真实验收。
- [Cubism](CUBISM_PACKS.md) 与 [Q版素材字段](CHARACTER_PACKS.md)：每个角色有自己的表现资源。

主窗口点击“收起为桌宠”即可切换，悬浮控件可展开；模式随本机偏好保存。
点击有角色交互，三击可手动唤醒语音，摇晃持续到松手后恢复；每 30–45 秒在未使用角色时
按素材权重播放待机。不是必须停止电脑键鼠才算角色空闲。

“日常对话语音”和总语音开关分开；日常无声仍按句子逐步显示，并保持麦克风偏好。
记忆原话立即保存；自动整理默认关闭，开启后按10轮本人对话或4000估算token积累，空闲再整理。
模型由你在设置中填写，沿用聊天 API 服务；记忆页可查询、修订、删除和手动整理。
旧记忆迁移用 `scripts/migrate_memory.py --help` 查看预览/备份选项，不直接拷贝私人数据库进公开仓库。

本机文字通知示例：

```bash
python scripts/notify_desktop.py --title '任务完成' --message '构建已通过。'
```

仅投递给同一用户、同一安装的正在运行的桌面；`queued` 是排队回执，不代表本人已看到。
不触发 LLM、语音、工具或 QQ，桌面关闭时不会自动启动。没有跨端待处理中心或召回入口。

当前架构：`Qt UI → AppHost/功能装配 → ChatEngine → run_turn → ports/adapters`。
Home 通过本机呈现控制器进入同一回复/播放链；实际呈现与后台资源释放分别回执。
角色包、模型、配置和私人数据分别存放，平台由 `platform.os: auto` 选择；公共功能只维护一份。

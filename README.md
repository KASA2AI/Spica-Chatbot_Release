**简体中文** · [English](README.en.md) · [日本語](README.ja.md)

# Spica Chatbot · 纯桌宠配置与角色包制作教程

Spica 是透明桌面上的语音角色伙伴：可以打字或说话、切换角色与服装、陪玩 galgame、一起看番和唱歌。角色的人设、立绘、声线与记忆可以组成独立文件夹；对话框外观单独选择。

本仓库是 Windows／Linux 上的纯桌宠版，包含多角色、立绘差分、眼部动画、独立对话框样式和角色记忆。没有 Mobile、机器人、Hub 或运动控制接口；制作兼容角色包不需要配置这些服务。

[项目主页与演示](https://www.acgkasa.me/) · [角色包格式详解](docs/CHARACTER_PACKS.md) · [对话框格式详解](docs/DIALOGUE_STYLES.md)

本教程的推荐配置与外部链接核对于 **2026-09-08**。README 有三种语言，不代表桌面对话与语音已经支持三种输出语言：当前聊天语音链以日语为主，字幕可选中文或日文。

## 目录

1. [先决定需要哪些模型](#1-先决定需要哪些模型)
2. [安装运行环境](#2-安装运行环境)
3. [填写 API 和本机配置](#3-填写-api-和本机配置)
4. [第一次启动与文件位置](#4-第一次启动与文件位置)
5. [在哪里制作角色卡怎样填写](#5-在哪里制作角色卡怎样填写)
6. [放置立绘并填写 metajson](#6-放置立绘并填写-metajson)
7. [配置角色说话声音](#7-配置角色说话声音)
8. [配置麦克风识别与唱歌](#8-配置麦克风识别与唱歌)
9. [制作 Live2D眼部动态包](#9-制作-live2d眼部动态包)
10. [背景与对话框样式](#10-背景与对话框样式)
11. [导入切换移除和记忆](#11-导入切换移除和记忆)
12. [验收排错与发布](#12-验收排错与发布)

## 1. 先决定需要哪些模型

**第一次建议先跑通“键盘聊天 + 静态示例”，然后加声音，最后加动态立绘和唱歌。** 没有声线也能制作角色卡和立绘包。下表中的几种“模型”职责不同，不能互相替代。

| 部分 | 推荐起点 | 放在哪里／填什么 | 是否每个角色都要一份 |
| --- | --- | --- | --- |
| 聊天 LLM | `deepseek-v4-flash`，关闭 thinking 以便及时接话 | `app.yaml` 的 `llm`，API key 在 `xiaosan.env` | 否，角色共用服务 |
| 麦克风 STT | faster-whisper 的 `large-v3-turbo`，CTranslate2 格式 | `stt.model` 指向完整模型目录 | 否 |
| 角色 TTS | GPT-SoVITS **v2ProPlus**；已有 v2Pro 成套声线也可用 | 角色包中的 GPT `.ckpt` + SoVITS `.pth` + 参考音频 | 是，若需要独立声线 |
| 唱歌 RVC | RVC v2 推理模型 `.pth`，可附配套 `.index` | 角色包 `singing/`，与 TTS 的 `.pth` 分开 | 可选 |
| 静态立绘 | 透明 PNG，按服装和表情整理 | `visuals/` 与 `meta.json` 的映射 | 是 |
| 眼部动画 | `spica-eye-rig`，睁眼／闭眼原图 + 标定 JSON | `model/`，`pack_format: 2` | 可选 |
| 屏幕理解 | 项目已有 RapidOCR / Moondream 适配器 | 本机 `screen` / `ocr` 配置 | 可选、共享 |

DeepSeek 官方当前提供上述模型 ID 和兼容 API，密钥从[官方控制台](https://platform.deepseek.com/)获取；请求地址见[官方快速入门](https://api-docs.deepseek.com/)。这里的选型用于桌面交互，不是效果排名。也可以使用自己的兼容服务，但应确认流式输出与工具调用都可用。

桌面显示、STT 和 TTS 在本机运行。选择远端 LLM 时，聊天文本、需要的角色卡／记忆，以及屏幕识别后参与对话的文字或描述会发给该服务；不要把“本地屏幕识别”理解成整条对话完全离线。

## 2. 安装运行环境

### 2.1 基础环境：先跑键盘聊天

准备 Git、Conda 和 **Python 3.11**。Windows 使用 PowerShell 或 Anaconda Prompt；Linux 使用终端。以下命令都在项目根目录运行，后续每次启动先 `conda activate spica`。

```bash
git clone https://github.com/KASA2AI/Spica-Chatbot_Release.git
cd Spica-Chatbot_Release
conda create -n spica python=3.11 -y
conda activate spica
python -m pip install --upgrade pip
python -m pip install -r requirements-windows-base.txt
python scripts/windows/check_imports.py
```

Linux 普通麦克风需要 PortAudio 开发依赖；Ubuntu／Debian 可先安装：

```bash
sudo apt-get install build-essential python3-dev portaudio19-dev ffmpeg
```

虽然基础依赖文件名带 `windows`，其中的 Python 基础包也用于桌面运行。Linux 无法编译 PyAudio 时先补系统依赖。**普通桌面麦克风填写 `mic_backend: generic`**；Linux 的 `auto` 会走 ReSpeaker 路径，并不等于自动选择任意 USB 麦克风。

### 2.2 NVIDIA GPU、语音与唱歌的完整依赖

先完成基础安装，再执行下面的顺序。此组合沿用仓库已有 Windows GPU 安装配方；它固定 Python 3.11、NumPy 1.26.x 与 PyTorch CUDA 12.4 组件，不要随意混装另一套 CUDA／NumPy 版本。Windows 的 `jieba_fast` 源码编译需要 MSVC Build Tools 的 C++ 工具；Linux 需要编译器。

```bash
python -m pip uninstall -y onnxruntime
python -m pip install -r requirements-windows-heavy.txt
python -m pip install torch==2.5.1 torchaudio==2.5.1 torchvision==0.20.1 --index-url https://download.pytorch.org/whl/cu124
python -m pip install -c constraints-windows-app.txt -r requirements-windows-app.txt
python -m pip install -c constraints-windows-app.txt --no-deps audio-separator==0.44.2
python -c "import numpy, torch; print(numpy.__version__, torch.__version__, torch.cuda.is_available())"
```

检查输出应包含 NumPy `1.26.4`、PyTorch `2.5.1+cu124`，NVIDIA 环境的 CUDA 可用性为 `True`。这只是依赖检查，后面仍需实际合成一句音频。CPU 用户可先停留在基础文字模式，不必为了立绘安装 TensorRT 或语音权重。

`onnxruntime` 与 `onnxruntime-gpu` 提供同名模块，GPU 安装后不要再次安装基础清单把 CPU 包覆盖回来。`audio-separator` 在该锁定环境下采用单独 `--no-deps` 安装；不要用一次无约束升级把 NumPy 改成 2.x。

语音处理需要系统可找到 `ffmpeg`，`ffmpeg-python` 只是 Python 封装，不包含可执行程序。终端运行 `ffmpeg -version` 确认。看番另需 qBittorrent（Web UI）和 VLC，先聊天的用户可以稍后配置。

## 3. 填写 API 和本机配置

### 3.1 密钥文件

在项目根目录新建 UTF-8 的 **`xiaosan.env`**，确认不是 `xiaosan.env.txt`。第一项填写自己的 DeepSeek API key；其他项可留空：

```dotenv
OPENAI_API_KEY=REPLACE_WITH_YOUR_DEEPSEEK_API_KEY
JUDGE_API_KEY=
BILIBILI_COOKIE=
QBITTORRENT_PASSWORD=
```

变量名仍叫 `OPENAI_API_KEY`，因为本项目统一通过兼容适配器读取它；使用 DeepSeek 时填写的就是 DeepSeek key。`JUDGE_API_KEY` 是可选的陪玩判断模型 key，未填写时使用主 key。不要把 key 填进角色卡、`meta.json`、截图或分享包。

### 3.2 应用配置示例

用编辑器打开 **`data/config/app.yaml`**。新用户可按下面配置启动文字模式；已有配置的用户只合并对应字段，保留自己的其他设置。YAML 使用空格缩进，不用 Tab；`none` 是字符串值，`null` 才是空值。

```yaml
llm:
  provider: openai_compatible
  model: deepseek-v4-flash
  base_url: https://api.deepseek.com/v1
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
  backend: faster_whisper
  mic_backend: generic
  model: spica_data/models/faster-whisper-large-v3-turbo
  device: cpu
  compute_type: int8
  language: zh
  warmup_on_startup: false

screen:
  enabled: false
anime:
  enabled: false
song:
  enabled: false
```

| 字段 | 该怎样填写 |
| --- | --- |
| `llm.model` | 接口实际支持的模型 ID；本例是 `deepseek-v4-flash`。不是声线文件名。 |
| `llm.base_url` | API 基址，不是网页版聊天链接；不要再追加 `/chat/completions`。 |
| `reasoning_effort` | 本例 `none` 对 DeepSeek 关闭 thinking；`default` 不发送该控制参数。换供应商时按其支持情况配置。 |
| `system_turn_reasoning_effort` | 主动开口的独立档位，`null` 继承主档位；本例同样用 `none`。 |
| `interlocutor_name` | 用户希望被怎样称呼；日后可在设置中修改，重启生效。 |
| `dialog_display_language` | `zh` 显示中文译文，`ja` 显示日文原句；这里没有 `en` 选项。 |
| `character.package_dir` | 由导入器写入的**安装目录**，不要填源包、ZIP 或模型文件。下面的首次启动命令会填写它。 |
| `tts.enabled` | 先用 `false`；配置好声线与依赖后改成 `true` 并重启。 |
| `stt.language` | 用户对麦克风说的语言，例如 `zh`／`ja`／`en`，与角色发音语言无关。 |

`MODEL`、`OPENAI_BASE_URL`、`REASONING_EFFORT`、`SPICA_USER_NAME`、`SPICA_SKILL_DIR`、`SPICA_CHARACTER_PROFILE` 等旧环境项会覆盖 YAML。推荐让 `xiaosan.env` 只保存所需密钥，把普通设置交给 YAML／设置页。若称呼保存提示被 `SPICA_USER_NAME` 覆盖，先移除启动环境或 dotenv 的旧项，重启后再保存，不能只反复点击保存。

## 4. 第一次启动与文件位置

### 4.1 没有角色模型时，先用仓库的小型示例

确认已经保存上面的文字模式配置。以下命令安装仓库自带的静态小图示例并保存选择，不需要下载完整 Spica 声线或原作立绘。它会改变本机的角色选择；已有角色的用户直接使用设置页即可。

```bash
python -c "from pathlib import Path; from spica.host.character_packages import import_character_folder; from spica.config.manager import ConfigManager; p = import_character_folder('Desktop-Packs/Characters/Examples/static', Path('data/runtime/characters')); ConfigManager().update({'character': {'package_dir': p.package_root, 'profile_override': None}}); print(p.name)"
python webui_qt.py
```

成功后应看到示例角色和对话框，可以输入一句话。基础测试阶段不要打开麦克风、屏幕识别或唱歌。示例图是用于说明格式的小型原创示意图，不是完整 Spica／Sana 美术素材。

### 4.2 认识目录，避免把模型解压错位置

```text
Spica-Chatbot_Release/
├── xiaosan.env
├── data/config/app.yaml
├── data/config/tts.yaml
├── Desktop-Packs/
│   ├── Characters/
│   │   ├── Examples/static/
│   │   ├── Examples/eye-rig/
│   │   └── my-character/
│   └── Dialogue-Styles/
│       ├── spica/
│       ├── sana/
│       └── my-style/
├── artifacts/tts_slim/base/
├── artifacts/rvc_slim/base/
├── spica_data/models/faster-whisper-large-v3-turbo/
├── data/runtime/characters/<slug>/<revision>/
├── data/runtime/character_states/<slug>/
└── data/runtime/dialogue_styles/<style_id>/<revision>/
```

`Desktop-Packs/` 保存可编辑和分享的**源文件夹**；`data/runtime/characters/` 保存导入器维护的副本；`character_states/` 保存角色运行偏好与相关私人状态。实际记忆还可能使用共享 SQLite 数据库，迁移时优先用“导出个人存档”，不要只复制一个目录就假定所有记忆完整。

Chatbot 保留旧默认资产兼容：内置角色卡在 `spica_data/Spica_skill/`，旧立绘／参考音频在 `spica_data/diffs/`、`spica_data/voice/`，旧声线在 `artifacts/tts_slim/characters/spcia/`，旧歌声在 `artifacts/rvc_slim/characters/spica/`。导入新角色后，使用 `data/runtime/characters/` 中的独立安装副本。新制作的源包统一放在 `Desktop-Packs/`；保留旧目录不表示新角色还要复制一套素材到那里。内置角色的旧数据库仍沿用 `spica_data/`，不要直接删除。 旧 TTS 路径中的 `spcia` 是实际保留的历史拼写，具体权重文件以 `data/config/tts.yaml` 为准。

已有[项目资产包下载入口](https://pan.baidu.com/s/1EFq7t8Lxcy9kDNL7MzU1gg?pwd=nzjy)，提取码 `nzjy`。不同批次压缩包布局可能不同，先查看最外层再解压：**角色／样式集合 ZIP 不等于共享引擎和 STT 安装包**。如果 ZIP 内直接是 `Characters/`、`Dialogue-Styles/`，将它们放到 `Desktop-Packs/`；若已经有 `Desktop-Packs/` 这一层，则合并到项目根目录。不要形成重复嵌套目录。

完整 Spica／Sana 权重和大图不包含在 Git 克隆中。小型 `Examples/` 和样式示例会随源码提供。你也可以完全使用自己有权使用的素材制作角色。

### 4.3 本地配置中心

基础清单已包含配置中心依赖；若是在旧环境中单独使用配置中心，可以先补装下面的专用依赖。

```bash
python -m pip install -r requirements-config-studio.txt
python scripts/config_studio.py --port 8765
```

这是独立启动的本机浏览器设置工具，默认只监听 `127.0.0.1:8765`。需要手动打开时加 `--no-open-browser`，按终端提示使用一次性启动授权；端口被占用时改为 `--port 8767`。在页面语言菜单选择 **中文 / English / 日本語**。语言切换只改变界面说明，不改变配置键和值。保存后重启桌宠载入；使用完毕在终端按 `Ctrl+C` 关闭配置中心。配置中心的浏览器语言不会改变角色说话语言。

## 5. 在哪里制作角色卡，怎样填写

### 5.1 推荐开源编辑网站

推荐使用 [CCEditor 在线编辑器](https://lenml.github.io/CCEditor/?lang=zh)，[源码与许可](https://github.com/lenML/CCEditor)公开，支持多版本角色卡。已有 SillyTavern 用户也可在其[角色管理界面](https://docs.sillytavern.app/usage/characters/)编辑。这里推荐的是**角色卡编辑工具**，不是要求安装另一套聊天前端。

可以从作者授权的社区卡开始，也可以在编辑器中新建原创角色。无论来源如何，先检查人设文本，再导出 JSON。若拿到的是内嵌角色卡的 PNG，先在编辑器中打开，再导出或复制文本字段。**Spica 当前不直接读取 SillyTavern PNG 卡或完整 V2／V3 JSON；不能把它改名为 `meta.json` 就导入。**

### 5.2 从外部卡填到 Spica

新建 `Desktop-Packs/Characters/my-character/`。建议先复制 [静态样例](Desktop-Packs/Characters/Examples/static/)，再修改内容。CCEditor／SillyTavern 的字段按下表整理；V2／V3 JSON 中正文通常位于 `data` 对象，旧版可能在顶层。

| 外部卡字段 | Spica 中的位置 | 填写建议 |
| --- | --- | --- |
| `name` | `meta.json` 的 `name`、`char_name` | 列表名称可含版本描述，`char_name` 写角色实际称呼。 |
| `description` | `persona.md` 的身份／经历 | 明确是谁、与用户关系、已知背景，不堆无关原作百科。 |
| `personality` | `persona.md` 的性格／行为 | 写可观察的说话习惯和反应，例如担心时会如何回应。 |
| `scenario` | `persona.md` 的当前关系／场景 | 改成桌面陪伴场景，移除不适合长期聊天的一次性剧情限制。 |
| `mes_example` | `persona.md` 的对话示例 | 用 `{{user}}`、`{{char}}` 写几轮简短例子。 |
| `first_mes`／备用开场白 | 可选地改写成风格示例 | 不会自动作为应用启动问候执行。 |
| `character_book`／世界书 | 手动提炼为 `worldbook.md`，在清单声明 | 当前是普通世界观文本，不执行关键词触发、深度插入或正则脚本。 |
| 作者、出处、许可 | `README.md`、`LICENSE`、`sources.tsv` | 记录原作者和素材来源，按许可保留。 |

不要把酒馆预设的系统提示、越狱模板、扩展脚本或所有格式控制指令整包搬过来。本项目已经管理日语回复、翻译、表情和工具调用；角色卡主要描述“这个角色是谁、怎样说话”。

### 5.3 可以直接改写的 persona.md

文件使用 UTF-8，建议以具体事实和少量例句开始，再通过实际聊天调整：

```markdown
# 身份
你是 {{char}}，住在桌面上的原创角色，正在陪 {{user}} 聊天。
你们是熟悉的朋友，能自然接续之前的普通聊天。

# 性格与语气
好奇、温和，偶尔轻轻吐槽。关心对方时先回应具体感受。
不替 {{user}} 编造动作、台词或人生经历。
日常回答简短自然，遵守应用的日语语音和字幕格式。

# 喜好与关系
喜欢星空和热茶。把 {{user}} 当作平等的朋友。
不知道的事情坦诚说明，不把推测当作共同记忆。

# 对话风格示例
{{user}}：今天有点累。
{{char}}：お疲れさま。少し休んで、お茶でも飲もうか。
{{user}}：你记得我们昨天说过什么吗？
{{char}}：覚えていることから、一緒に振り返ってみよう。
```

`worldbook.md` 可以先写一小段：

```markdown
{{char}} 把自己的桌面小屋叫作星光小屋。
旅人是 {{user}} 的旧称呼，两者指同一个人。
```

下面的清单把 `旅人` 列入 `user_aliases`，载入时会替换成当前用户称呼。优先直接写 `{{user}}`；旧称呼替换是按文本执行，避免把过短的常用字加入别名列表。

角色卡可由 `SKILL.md`、`self.md`、`persona.md` 组成，至少需要一个。最简单的作者包只用 `persona.md`；这些文件是人设文字，不是 Codex 技能或可执行脚本。`self.md` 中手写的背景也会随分享包导出，不会自动当成私人数据库内容剔除。

## 6. 放置立绘并填写 meta.json

### 6.1 源包结构

```text
my-character/
├── meta.json
├── persona.md
├── worldbook.md
├── README.md
├── LICENSE
├── visuals/
│   ├── idle.png
│   └── happy.png
├── voice/
│   ├── gpt.ckpt
│   ├── sovits.pth
│   └── reference.wav
└── singing/
    ├── model.pth
    └── model.index
```

其中 `voice/`、`singing/` 是后续可选项；本节先只用角色卡、世界书和两张图片。未准备好的声音块直接省略，不能填一个不存在的占位路径。

### 6.2 一份完整、可用的静态清单

保存为 `meta.json`，注意 JSON 不支持注释、尾逗号或单引号：

```json
{
  "pack_format": 1,
  "slug": "my-character",
  "version": "1.0.0",
  "name": "Hoshi",
  "char_name": "ホシ",
  "user_aliases": [
    "旅人"
  ],
  "worldbook_file": "worldbook.md",
  "visuals": {
    "renderer": "sprite",
    "default_costume": "casual",
    "sprites": {
      "idle": "visuals/idle.png",
      "happy": "visuals/happy.png"
    },
    "costumes": [
      {
        "id": "casual",
        "label": "Casual",
        "default_sprite": "idle",
        "emotions": {
          "happy": [
            "happy"
          ]
        }
      }
    ]
  }
}
```

| 字段 | 含义／修改规则 |
| --- | --- |
| `pack_format` | 静态包为 `1`，眼部动态包为 `2`。 |
| `slug` | 稳定身份 ID，推荐小写英文、数字和连字符，例如 `hoshi`。同一角色升级保持不变，独立角色使用新 ID。 |
| `version` | 作者的展示版本，例如 `1.0.1`；资源变化也会产生新的安装版本。 |
| `name`／`char_name` | 列表名称／实际发言人名称与 `{{char}}` 替换值。 |
| `worldbook_file` | 可选世界书路径；没有文件就删除此字段。 |
| `sprites` | 自定义图片 ID → 包内相对路径。ID 是下面映射要引用的键。 |
| `default_costume` | 必须等于某一项 `costumes[].id`。 |
| `default_sprite` | 必须引用 `sprites` 中已经声明的 ID，不能直接填 PNG 路径。 |

路径统一使用 `/`，相对 `meta.json` 所在目录，不写 `C:\...`、`/home/...`、`../...` 或符号链接。文件名与 ID 不要只靠大小写区分；这会在 Windows 上冲突。重命名图片后必须同步改清单。

### 6.3 立绘具体怎么放

1. 使用透明背景 PNG，人物外侧保留真正的 alpha 透明度。白色背景不是透明背景。
2. 同一套差分保持**画布尺寸、人物位置、缩放与脚底基线一致**，只改变表情或手势；否则切换时人物会跳动。
3. 普通静态图导入时会等比缩放、居中靠下放入 `1024×1024` 透明画布。不要把 UI 背景或对话框画进人物图。
4. 日常、校服、睡衣等可按 `visuals/casual/`、`visuals/school/` 分类；中景、近景也可作为不同服装项。目录名本身不会触发识别，仍需要 JSON 映射。
5. 支持 `happy`、`angry`、`sad`、`surprised` 四组情绪。先做默认图和开心图即可，缺少的情绪会回到当前服装默认图。

### 6.4 增加一套服装或更多表情

放入 `visuals/school/idle.png` 和 `visuals/school/happy.png`；在原 `sprites` 对象追加 `"school-idle": "visuals/school/idle.png"`、`"school-happy": "visuals/school/happy.png"`，然后在原 `costumes` 数组中追加：

```json
{
  "id": "school",
  "label": "School",
  "default_sprite": "school-idle",
  "emotions": {
    "happy": [
      "school-happy"
    ]
  }
}
```

要默认校服，把 `default_costume` 改成 `school`。更多情绪同理：先放 PNG、声明图片 ID，再在 `emotions` 中引用。支持为同一情绪写多个 ID。手势和三位表情编号的精细导演映射属于可选高级项，见[详细格式](docs/CHARACTER_PACKS.md)，初次制作不需要复制整套 Spica 差分规则。

## 7. 配置角色说话声音

### 7.1 两个模型究竟是哪两个

本项目的角色语音包支持 **GPT-SoVITS v2Pro／v2ProPlus**。推荐新声线先按 **v2ProPlus** 流程准备，已有成套 v2Pro 模型则保留真实版本。

- GPT `.ckpt`：语音语义模型，在清单里填 `tts.gpt`。
- SoVITS `.pth`：该声线对应的声音生成模型，填 `tts.sovits`。
- 参考 WAV 与逐字对应的文本：告诉合成器具体声线和语气，填 `tts.reference`。

成套模型来自声线作者，或按 [GPT-SoVITS 官方项目](https://github.com/RVC-Boss/GPT-SoVITS)用有权使用的录音训练。Spica 负责推理，不内置训练界面。不要把 RVC `.pth`、训练中的任意检查点、LoRA 或其他版本模型改后缀冒充；`model_version` 也不是转换器。

没有训练模型时，可以从上游同版本基础模型和参考音频开始做效果试验；这不保证达到某个角色微调声线的相似度。以实际听到的发音和稳定性决定是否使用。

### 7.2 共享推理权重

角色的两个文件不能替代共享 GPT-SoVITS 运行依赖。保留仓库自带 slim 引擎源码，在其 `pretrained_models/` 下补齐完整基础目录。可以使用项目资产包，也可从[上游模型仓库](https://huggingface.co/lj1995/GPT-SoVITS)下载 v2Pro 所需部分：

```bash
python -c "from huggingface_hub import snapshot_download; snapshot_download('lj1995/GPT-SoVITS', local_dir='artifacts/tts_slim/base/GPT_SoVITS/pretrained_models', allow_patterns=['chinese-hubert-base/*', 'chinese-roberta-wwm-ext-large/*', 'sv/*', 's1v3.ckpt', 'v2Pro/*'])"
```

```text
artifacts/tts_slim/base/GPT_SoVITS/pretrained_models/
├── chinese-hubert-base/                 (complete model folder)
├── chinese-roberta-wwm-ext-large/       (complete model folder)
├── sv/pretrained_eres2netv2w24s4ep4.ckpt
├── s1v3.ckpt
└── v2Pro/
    ├── s2Gv2Pro.pth
    └── s2Gv2ProPlus.pth
```

两个 `chinese-*` 目录是该引擎共用依赖，日语角色也不要随意删掉。若使用基础模型试验，可把相应 `s1v3.ckpt` 与 `s2Gv2ProPlus.pth` **复制进自己的角色包**再按实际文件名引用。训练好的角色包则使用作者提供的对应文件。

### 7.3 参考音频与清单

准备约 **3～10 秒**、单人、干净、无背景音乐的参考语音，先用 WAV。`text` 必须写这段音频实际说出的原文，不是中文释义，也不是希望角色未来说的话。下面的日文只是示例，必须替换成你自己的录音原文。

在已有 `meta.json` 的顶层追加以下 `tts` 字段，保留原来的身份、立绘等字段：

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
      "text": "今日は一緒にお話しできて、とてもうれしいです。",
      "language": "日文"
    },
    "parameters": {
      "top_k": 5,
      "top_p": 1.0,
      "temperature": 1.0,
      "speed": 1.0
    }
  }
}
```

`model_version` 填真实的 `v2Pro` 或 `v2ProPlus`。`reference.language` 描述参考音频语言，`target_language` 描述合成语言，字段值是固定的 `日文`／`中文`／`英文`，英文、日文 README 中也不能翻译这些枚举值。本项目当前聊天语音以日文为主；仅把此处改成 `英文` 不会把整个聊天系统切到英语。

`top_k/top_p/temperature` 先沿用示例，`speed` 先用 `1.0`。一开始只用一个稳定参考，确认会说话后再调参数。

情绪参考可选：在 `tts` 内增加 `emotions`，内容例如：

```json
{
  "happy": {
    "audio": "voice/happy.wav",
    "text": "会えてうれしいです。",
    "language": "日文"
  },
  "sad": {
    "audio": "voice/sad.wav",
    "text": "もう少しだけ、ここにいてください。",
    "language": "日文"
  }
}
```

上例中的两个 WAV 必须实际存在，原文也必须对应自己的录音。未声明的情绪复用主参考。每个参考还可添加 `additional_audio` 数组作为附加音频，不需要时省略。

最后将本机 `app.yaml` 的 `tts.enabled` 改为 `true`，重新导入修改后的角色并重启，测试一条短日文。导入只做资源与格式校验，不会提前合成；“导入成功”不代表模型已经实际发声。

## 8. 配置麦克风识别与唱歌

### 8.1 麦克风识别 STT

使用[转换为 CTranslate2 格式的 large-v3-turbo](https://huggingface.co/dropbox-dash/faster-whisper-large-v3-turbo)，下载完整目录：

```bash
python -c "from huggingface_hub import snapshot_download; snapshot_download('dropbox-dash/faster-whisper-large-v3-turbo', local_dir='spica_data/models/faster-whisper-large-v3-turbo')"
```

目录应包含 `model.bin`、`config.json`、`tokenizer.json` 等实际模型文件，不是 Git LFS 指针。已有完整下载可直接复用，不必重复下载。项目使用 [faster-whisper](https://github.com/SYSTRAN/faster-whisper)，不能把任意 Whisper `.pt` 文件当作相同目录。

在 `stt` 中设置：NVIDIA 用 `device: cuda`、`compute_type: float16`；CPU 用 `device: cpu`、`compute_type: int8`。模型准备好后可将 `warmup_on_startup` 改成 `true`，降低第一次说话的加载等待。中文输入填 `language: zh`，日文填 `ja`，英文填 `en`。选择系统默认的输入／输出设备，再进行短句识别测试。

### 8.2 RVC 唱歌声线

讲话模型和歌声模型是两套模型。准备 [Applio／RVC](https://github.com/IAHispano/Applio)可推理的 RVC v2 `.pth`，放到 `singing/model.pth`；有匹配的索引就放到 `singing/model.index`。把下面的 `rvc` 加到 `meta.json` 顶层：

```json
{
  "rvc": {
    "engine": "rvc_v2",
    "model": "singing/model.pth",
    "index": "singing/model.index",
    "index_rate": 0.5,
    "transpose": 0,
    "protect": 0.33
  }
}
```

没有 `.index` 时删除 `index` 字段，不要引用不存在的文件。`transpose` 是半音移调，不是语速；`index_rate` 和 `protect` 先沿用示例。还需要本机共享 RVC 引擎、HuBERT／RMVPE 等基础权重与歌曲分离依赖，项目资产包的 `artifacts/rvc_slim/base/` 用于这些共享文件。

本机 `song.enabled` 改为 `true` 后重新导入并重启，才能使用该角色歌声。省略 `rvc` 会关闭该角色的唱歌能力；角色包不会自动打开本机已经关闭的功能。API key、STT、显卡选项和 Python 路径始终留在本机配置中。

## 9. 制作 Live2D／眼部动态包

**当前支持的是 `spica-eye-rig` 原画眼部动画**：鼠标视线跟随、平滑眨眼、差分切换后恢复动态默认图。它不是 Cubism 播放器，不能直接导入 `.model3.json`／`.moc3`，也不包含完整骨骼、物理或自动口型系统。

先复制仓库的 [eye-rig 示例](Desktop-Packs/Characters/Examples/eye-rig/)，导入确认原示例能动，再换自己的图：

```text
my-character-live2d/
├── meta.json
├── persona.md
├── worldbook.md
└── model/
    ├── character.eyerig.json
    ├── open.png
    └── closed.png
```

沿用静态包的 `slug`、名称、角色卡、世界书和可选声音，把 `pack_format` 与整个 `visuals` 替换成下面的片段；不要删除顶层其他必填字段：

```json
{
  "pack_format": 2,
  "visuals": {
    "renderer": "eye-rig",
    "default_costume": "casual",
    "sprites": {
      "idle": "model/open.png"
    },
    "eye_rigs": {
      "idle": "model/character.eyerig.json"
    },
    "costumes": [
      {
        "id": "casual",
        "label": "Casual",
        "default_sprite": "idle"
      }
    ]
  }
}
```

### 9.1 准备原画

`open.png` 与 `closed.png` 是**同尺寸、同姿势、同画布的完整 RGBA 人物图**，只改变眼睛开合。不要把闭眼图裁成小贴片。两张图中被标定的眼部矩形必须完全不透明；透明背景留在人物外侧。画布每边不超过 4096 像素。

### 9.2 按自己的图片标定

直接打开示例的 [character.eyerig.json](Desktop-Packs/Characters/Examples/eye-rig/model/character.eyerig.json) 编辑。示例坐标只适用于它自己的 `200×200` 小图；换人物后必须重新测量。

| 字段 | 如何填写 |
| --- | --- |
| `canvas.width/height` | 两张完整原图的实际像素尺寸。 |
| `textures.open/closed` | 相对**眼部 JSON 所在目录**的路径，本例为 `open.png`／`closed.png`。 |
| `gaze.origin` | 视线基准 `[x,y]`，通常位于两眼之间。 |
| `gaze.maximum_offset` | 最大移动 `[dx,dy]`，上限分别 32、16 像素；先从小值调整。 |
| `gaze.strength/smoothing_ms` | 跟随强度与平滑毫秒数；先沿用示例 `0.7`／`95`。 |
| `blink` | 眨眼时长、初始等待、随机间隔，先保留样例参数。 |
| `eyes[].bounds` | `[x,y,width,height]`，完整包住眼睛与睫毛；每个矩形宽高不超过 512。 |
| `eyes[].iris` | 虹膜**左右 x 边界** `[left_x,right_x]`，不是二维位置。 |
| `eyes[].contour` | 从左到右的 `[x,上眼睑y,下眼睑y,睫毛上沿y]`，至少 3 个采样点。 |
| `eyes[].closedLine` | 闭眼曲线 `[x,y]`，至少 2 点，左右覆盖睁眼轮廓。 |

所有坐标从完整原图左上角计算，x 向右、y 向下，不能填屏幕坐标或百分比。曲线的 x 必须递增，各点落在 `bounds` 内，满足睫毛上沿 ≤ 上眼睑 ≤ 下眼睑，并为虹膜左右移动留空间。校验失败时按报错改标定，不要靠扩大矩形掩盖透明区域。

多套服装可各有一个默认图和 rig；在 `eye_rigs` 中为对应图片 ID 建立映射。没有 rig 的表情仍可作为静态差分出现。动态包的角色卡、称呼替换和记忆规则与静态包相同。

## 10. 背景与对话框样式

### 10.1 角色场景背景

可选地在角色包加入 `backgrounds/room.png` 和 `ui/settings-cg.png`，然后在 `meta.json` 顶层合并：

```json
{
  "backgrounds": {
    "room": "backgrounds/room.png"
  },
  "settings_background": "ui/settings-cg.png",
  "scenes": {
    "mobile": {
      "background": "room",
      "background_position": [
        0.5,
        0.5
      ]
    },
    "robot": {
      "background": "room",
      "background_position": [
        0.5,
        0.5
      ]
    }
  }
}
```

Chatbot 为格式兼容保留并可导出这些字段，但纯桌宠不会显示手机／机器人背景，也不会连接对应设备。不要因此为 Chatbot 添加 Hub 地址或手机接口。桌面对话框的背景与配色使用下面的独立样式包配置。

### 10.2 独立制作对话框

复制 `Desktop-Packs/Dialogue-Styles/spica/` 或 `sana/` 作为结构参考；替换成自己的美术后，修改 `style_id` 和名称。图片和 JSON 组成一个单独文件夹：

```text
my-style/
├── style.json
└── images/
    ├── surface.png
    ├── nameplate.png
    └── tail.png
```

最小动画样式 `style.json`：

```json
{
  "format": "spica-dialogue-style",
  "format_version": 1,
  "style_id": "my-style",
  "name": "Hoshi Pink",
  "version": "1.0.0",
  "author": "Your name",
  "images": {
    "surface": "images/surface.png",
    "tail": "images/tail.png"
  },
  "colors": {
    "accent": "#E94E8B"
  },
  "tail": {
    "placement": "corner",
    "columns": 5,
    "rows": 4,
    "frames": 20,
    "frame_ms": 50,
    "width": 28,
    "height": 28
  }
}
```

`surface.png` 是透明底板，`nameplate.png` 是可选姓名底图；若要使用后者，在 `images` 中追加 `"nameplate": "images/nameplate.png"`。角色的实际名字由应用绘制，别把固定角色名烘焙到底板里；“Character Name”这类装饰字可以画在底图中。

本例 `tail.png` 必须是 **5 列 × 4 行，共 20 帧**的图集，按先行后列的阅读顺序逐行播放。每帧 50 ms，循环约 1 秒；像素尺寸必须整除行列数。Sana 原作空白帧可以保留。只有静态尾标时把行、列、帧数都改为 `1`。

`placement: corner` 放在对话区右下角，`inline` 放在文字末尾。句末标记在文字展示完成后出现；连续分句也保留可见停顿。颜色与内边距可通过 `colors/text/layout` 调整，完整键和范围见 [JSON Schema](docs/dialogue-style.schema.json)。所有样式图片必须是 `images/` 下声明的 PNG，单图不超过 8 MiB、单边不超过 4096、总像素不超过 800 万。

样式只改变外观、文字和局部布局；按钮功能与设置结构沿用桌面现有 UI，不执行包内脚本。角色导入不会替你选择同名样式，需要单独导入／选择。

## 11. 导入、切换、移除和记忆

### 11.1 日常使用

1. 打开齿轮设置，点击 **导入角色文件夹**，选择直接含 `meta.json` 的那一级目录。
2. 等待“导入完成”的提示；大型语音包会复制模型，需要预留源包和安装副本的磁盘空间。
3. 在 **对话框样式** 中按需导入含 `style.json` 的目录。
4. 填写希望角色怎样称呼你，按回车或离开称呼框保存。
5. 点击 **重启程序**。角色、称呼与样式在重启后载入。对话中也可点击，程序会先结束当前播放并等待后台安全退出。

已安装的角色／样式可以在下拉列表选择。服装切换通常立即生效，但当前一轮回复与语音未结束时会暂时禁用，避免后续分句恢复旧服装。

### 11.2 从列表移除

展开角色或样式列表 → 点击对应行右侧 **×** → **确定移除**。取消不会改变选择；内置默认项没有 ×。

这一步移除列表条目，**保留源包、安装资源和角色记忆，不回收磁盘空间**。移除当前选中的角色或样式时，当前对话继续使用已加载资源，下次重启恢复该项的内置 Spica 默认值。需要时再次手动导入原文件夹，就能恢复列表条目。

### 11.3 分享与个人存档

先导入并重启到需要导出的角色，再在设置中选择：

| 入口 | 包含什么 | 适用场景 |
| --- | --- | --- |
| **导出分享包** | 角色卡、声明的立绘／rig／声音、世界书、作者资料，不含应用生成的个人存档 | 向社区分享自己可分发的角色 |
| **导出个人存档** | 上述资源，另含该角色长期记忆、近期普通聊天、陪玩共同记忆和服装选择 | 自己迁移到另一台机器 |

两种导出都指定一个**尚不存在的新文件夹名称**。对话框样式独立携带。压缩导出的文件夹即可生成 ZIP，接收者先解压，再在设置中选择文件夹。不要把整个项目或 `data/runtime/` 直接压缩公开。

同一角色升级保持 `slug` 不变，已有本机记忆会继续保留；个人存档仅在该角色尚无本机存档时初始化，不会强制覆盖另一台机器已有的记忆。静态与动态版本若共用 `slug`，也会共用同一角色身份。需要全新记忆身份时用新 `slug`。

## 12. 验收、排错与发布

### 12.1 作者自己先检查一遍

- 从源包导入成功；重启后看到正确名字、默认服装与人设。
- 改用户称呼并重启，下一句按新名字称呼，包含该名字的语音也能播放。
- 普通／开心等图片切换时位置一致；动态图能眨眼、视线幅度合适。
- 有声线时先播短句，再测较长句和不同情绪；不要只看“导入成功”。
- 样式尾标能连续播放；输入、发送、设置和重启按钮仍正常。
- 分享包中没有自己的聊天、密钥和设备配置，图片、模型与原作者许可齐全。

### 12.2 常见问题

| 现象 | 优先检查 |
| --- | --- |
| 401／鉴权失败 | `OPENAI_API_KEY` 是否属于当前 `base_url` 的供应商，dotenv 是否在项目根目录。 |
| 404／模型不存在 | 使用 API 模型 ID 与基址，不要填写网页版 URL 或不再提供的模型别名。 |
| 设置已改却还是旧值 | 是否重启，以及进程环境／dotenv 是否还覆盖 YAML。 |
| 没声音但有字幕 | `tts.enabled`、角色是否声明 `tts`、两个权重是否配套、参考原文是否正确；文字示例本来无声。 |
| 不显示立绘 | 是否选到含 `meta.json` 的目录，默认 ID 是否存在，图片路径和大小写是否匹配。 |
| 动态图导入失败 | 检查尺寸、眼部不透明矩形、虹膜余量与曲线坐标；不要提交 Cubism 文件。 |
| 首句慢 | 先分清 LLM 等待、STT 首次下载、TTS 预热和中文翻译等待；模型落盘后再测，DeepSeek 可用 `none`。 |
| Linux 麦克风报 ReSpeaker 错误 | 普通桌面输入使用 `mic_backend: generic`。 |
| CUDA／DLL 错误 | Python 环境、NVIDIA 驱动与锁定依赖是否匹配，是否混装 CPU／GPU onnxruntime。 |
| 样式损坏 | 重新导入完好的原始样式包修复，再重启。 |
| 移除后磁盘没有变小 | 这是列表移除，资源与记忆刻意保留。 |
| ZIP 导入失败 | 先解压，选择具体角色／样式目录，不选择 ZIP 或集合上一级。 |

### 12.3 继续配置其他能力

看番在 `anime` 中配置 qBittorrent Web UI 地址、播放器和下载目录；密码放 `xiaosan.env`，B 站 Cookie 仅在需要时填写。屏幕观察在 `screen/ocr` 中选择本机适配器；先确认文字聊天和声音工作，再开启重型视觉模型。不同 GPU、输入长度与模型版本的延迟和显存不同，不能用某一次测试值当最低硬件保证。

### 12.4 许可与上游

Spica 自身使用 [LICENSE](LICENSE) 中的 **Source-Available 许可**，并非无条件再分发的开源许可；项目角色、声线和原作素材各有使用范围。为自己原创的社区包注明作者、来源与适用许可，使用第三方素材时先核对其分发条件。本教程不改变任何已有许可。

感谢 [GPT-SoVITS](https://github.com/RVC-Boss/GPT-SoVITS)、[Applio](https://github.com/IAHispano/Applio)、[faster-whisper](https://github.com/SYSTRAN/faster-whisper)、[RapidOCR](https://github.com/RapidAI/RapidOCR)、[Moondream](https://github.com/vikhyat/moondream) 和 [yt-dlp](https://github.com/yt-dlp/yt-dlp)。它们按各自的许可提供。角色卡编写字段参考 [SillyTavern 官方说明](https://docs.sillytavern.app/usage/core-concepts/characterdesign/)，Spica 实际可导入字段以[角色 JSON Schema](docs/character-pack.schema.json)为准。

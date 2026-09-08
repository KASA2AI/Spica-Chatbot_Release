**简体中文** · [English](README.en.md) · [日本語](README.ja.md) · [返回主页](../README.md)

# 配置与自定义：照着样例做一遍

先跑通文字聊天，再换角色、加声音和改对话框。下面命令都在**项目根目录**执行，使用已激活的 `spica` 环境；尚未安装基础依赖请先看[主页](../README.md)。

[启动](#setup) · [导入](#import) · [角色卡与立绘](#character) · [声音](#voice) · [动态立绘](#animation) · [对话框](#dialogue) · [配置中心](#studio)

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
| 麦克风识别 | CTranslate2 格式的 `large-v3-turbo`，整份模型目录 |
| 唱歌 | RVC v2 `.pth`，可附配套 `.index`；与讲话模型分开 |

**本机环境只安装一次。** 完成基础安装后，NVIDIA / Python 3.11 使用以下顺序；保留 NumPy 1.26.x，不要把独立 RVC 环境的 `requirements-rvc.txt` 混装进来。

```bash
python -m pip uninstall -y onnxruntime
python -m pip install -r docs/requirements/requirements-windows-heavy.txt
python -m pip install torch==2.5.1 torchaudio==2.5.1 torchvision==0.20.1 --index-url https://download.pytorch.org/whl/cu124
python -m pip install -c docs/requirements/constraints-windows-app.txt -r docs/requirements/requirements-windows-app.txt
python -m pip install -c docs/requirements/constraints-windows-app.txt --no-deps audio-separator==0.44.2
```

确认 `ffmpeg -version` 能运行。共享语音权重与 STT 模型不放进角色包，缺少时分别从 [GPT-SoVITS](https://huggingface.co/lj1995/GPT-SoVITS) 和 [faster-whisper 模型仓库](https://huggingface.co/dropbox-dash/faster-whisper-large-v3-turbo)下载；已有完整文件可跳过：

```bash
python -c "from huggingface_hub import snapshot_download; snapshot_download('lj1995/GPT-SoVITS', local_dir='artifacts/tts_slim/base/GPT_SoVITS/pretrained_models', allow_patterns=['chinese-hubert-base/*', 'chinese-roberta-wwm-ext-large/*', 'sv/*', 's1v3.ckpt', 'v2Pro/*'])"
python -c "from huggingface_hub import snapshot_download; snapshot_download('dropbox-dash/faster-whisper-large-v3-turbo', local_dir='spica_data/models/faster-whisper-large-v3-turbo')"
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

<a id="studio"></a>
## 8. 本地配置中心（可选）

```bash
python -m pip install -r docs/requirements/requirements-config-studio.txt
python scripts/config_studio.py --port 8765
```

浏览器默认地址为 `127.0.0.1:8765`；加 `--no-open-browser` 可手动打开，按终端提示使用一次性启动授权，退出按 `Ctrl+C`。界面支持 **中文 / English / 日本語**；语言切换只改变界面说明，不改变配置键和值。Linux 可保存；Windows 配置中心目前只读，角色／样式／称呼用桌宠设置修改，其余编辑 YAML / `xiaosan.env` 后重启。

## 遇到问题

- **没立绘**：确认导入的是含 `meta.json` 的那一级，图片映射存在，已完成重启。
- **没声音**：检查成套模型、参考原文、共享权重及 `tts.enabled`；导入成功不等于实际合成成功。
- **称呼或模型不更新**：检查旧环境变量覆盖；保存后重启。
- **Windows 路径报错**：本机 YAML 用 `C:/Spica/models/file`，包内始终用相对路径；别把 ZIP 当文件夹导入。

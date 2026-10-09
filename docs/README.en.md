[简体中文](README.md) · **English** · [日本語](README.ja.md) · [Home](../README.en.md)

# Setup and customization: start from a working example

Start with text chat, then add characters, voices, and styles. Run commands from the **project root** in the activated `spica` environment. Install the base dependencies from the [main README](../README.en.md) first.

[Startup](#setup) · [Import](#import) · [Cards & artwork](#character) · [Speech](#voice) · [Animation](#animation) · [Dialogue styles](#dialogue) · [Change local settings](#settings)

<a id="setup"></a>
## 1. Start with text chat

Create UTF-8 `xiaosan.env` with your API key. This example uses the [DeepSeek compatible API](https://api-docs.deepseek.com/); get a key from its [console](https://platform.deepseek.com/). For another service, change the model and address accordingly.

```dotenv
OPENAI_API_KEY=REPLACE_WITH_YOUR_API_KEY
```

Merge these fields into `data/config/app.yaml`, keeping other settings. Optional features start disabled so missing local models do not block your first conversation.

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

For a first launch, install the bundled small static example. If you already have a character, run only the final line:

```bash
python -c "from pathlib import Path; from spica.host.character_packages import import_character_folder; from spica.config.manager import ConfigManager; p = import_character_folder('Desktop-Packs/Characters/Examples/static', Path('data/runtime/characters')); ConfigManager().update({'character': {'package_dir': p.package_root, 'profile_override': None}}); print(p.name)"
python -X utf8 webui_qt.py
```

Send a message and check for a text reply. Subtitle language is `zh` for Chinese or `ja` for Japanese, not the speech language. Keep `mic_backend: generic` for an ordinary microphone. Store keys only in `xiaosan.env`; remove conflicting legacy `MODEL`, `OPENAI_BASE_URL`, or `SPICA_USER_NAME` environment settings.

<a id="import"></a>
## 2. Use an existing character and style

1. Download the [character asset pack](https://pan.baidu.com/s/1RDoz_iRvFJdB71Aia3EPaw?pwd=j2u6), code **`j2u6`**, and extract the ZIP.
2. Open the gear settings → **导入角色文件夹** → select the folder directly containing `meta.json`.
3. Import a matching dialogue style separately, selecting its folder containing `style.json`.
4. Wait for import to finish, then click **重启程序** (Restart). Character and style selections are independent and apply after restart.

Keep editable sources under `Desktop-Packs/Characters/` and `Desktop-Packs/Dialogue-Styles/`; the app manages installed copies in `data/runtime/`. Select the individual pack, not a ZIP or collection folder. This desktop app does not display mobile/robot backgrounds; customize the [dialogue surface](#dialogue) instead.

<a id="character"></a>
## 3. Create a card and character artwork

Copy the [static example](../Desktop-Packs/Characters/Examples/static/) to `Desktop-Packs/Characters/my-character/`, keeping this layout:

```text
my-character/
├── meta.json
├── persona.md
├── worldbook.md
└── visuals/
    ├── idle.png
    └── happy.png
```

Write a card in the open-source [CCEditor](https://lenml.github.io/CCEditor/?lang=en) ([source](https://github.com/lenML/CCEditor)), or edit `persona.md` directly. Transfer external card content as follows:

| Content | Destination |
| --- | --- |
| Name | `meta.json`: `name` for the list, `char_name` for the character |
| Identity, personality, voice, example dialogue | `persona.md` |
| World and relationships | `worldbook.md`; keep its `worldbook_file` reference |
| Independent character identity | Set `slug` to `my-character`; keep it stable for upgrades |

A small `persona.md` starting point:

```markdown
You are {{char}}, keeping {{user}} company on their desktop.
You are gentle and curious. Reply naturally and briefly, with occasional light humor.
Address the user as {{user}}, not as the original story's protagonist.
```

Extract text from SillyTavern / CCEditor PNG cards or V2/V3 JSON. Renaming a card to `meta.json` does not convert it.

Replace `idle.png` and `happy.png` with transparent PNGs using the same canvas size and character position. Add each expression to `visuals.sprites`, then reference its ID in the outfit's `emotions`, such as `"happy": ["happy"]`. Supported emotions: `happy/angry/sad/surprised`. For another outfit, copy one `costumes` entry, change its `id`, `label`, and `default_sprite`, and add its image mappings.

Use pack-relative paths with `/`, without absolute paths or `..`; filenames must not differ only by case. Save, reimport, and restart. See the [character reference](CHARACTER_PACKS.md) for additional fields.

<a id="voice"></a>
## 4. Add speech (optional)

| Purpose | Required models |
| --- | --- |
| Speaking voice | A matching GPT-SoVITS **v2Pro / v2ProPlus** pair: GPT `.ckpt`, SoVITS `.pth`, reference WAV |
| Microphone recognition | The complete `Qwen3-ASR-1.7B` model directory |
| Singing | RVC v2 `.pth`, optionally its matching `.index`; separate from TTS |

**Install the local environment once.** After base installation, use this order for NVIDIA / Python 3.11. Keep NumPy 1.26.x; do not install the separate-worker `requirements-rvc.txt` into this environment.


On Windows, use `python scripts/windows/setup_environment.py --profile full --install` in the existing app environment, then run it with `--check`. See the [Windows guide](WINDOWS.md). The manual commands below describe the Linux main environment; Linux keeps Qwen in a separate worker environment.

```bash
python -m pip uninstall -y onnxruntime
python -m pip install -r docs/requirements/requirements-windows-heavy.txt
python -m pip install torch==2.5.1 torchaudio==2.5.1 torchvision==0.20.1 --index-url https://download.pytorch.org/whl/cu124
python -m pip install -c docs/requirements/constraints-windows-app.txt -r docs/requirements/requirements-windows-app.txt
python -m pip install -c docs/requirements/constraints-windows-app.txt --no-deps audio-separator==0.44.2
```

Check that `ffmpeg -version` works. Shared speech weights and STT models stay outside character packs. Download missing files from [GPT-SoVITS](https://huggingface.co/lj1995/GPT-SoVITS) and the [Qwen3-ASR model repository](https://huggingface.co/Qwen/Qwen3-ASR-1.7B); skip these downloads if the complete files already exist:

```bash
python -c "from huggingface_hub import snapshot_download; snapshot_download('lj1995/GPT-SoVITS', local_dir='artifacts/tts_slim/base/GPT_SoVITS/pretrained_models', allow_patterns=['chinese-hubert-base/*', 'chinese-roberta-wwm-ext-large/*', 'sv/*', 's1v3.ckpt', 'v2Pro/*'])"
python -c "from huggingface_hub import snapshot_download; snapshot_download('Qwen/Qwen3-ASR-1.7B', local_dir='models/stt/Qwen3-ASR-1.7B')"
```

**Prepare each character's voice separately.** Put the author's matching model pair and a clean reference recording of roughly 3–10 seconds in `voice/`. Add `tts` at the top level of the existing `meta.json`; this fragment is not a replacement for the whole file:

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

Use the actual model version and replace `text` with the recording's exact transcript. `日文/中文/英文` are fixed enum values. Chat speech currently centers on Japanese; changing this field alone does not switch the conversation language. For training, see [GPT-SoVITS](https://github.com/RVC-Boss/GPT-SoVITS).

Set local `tts.enabled` to `true`, reimport the character, restart, and test one short sentence. For GPU recognition use `stt.device: cuda` and `compute_type: float16`; set `language: zh`, `ja`, or `en` for the user's speech. Singing also needs shared RVC weights and local `song.enabled: true`; see the [voice and RVC fields](CHARACTER_PACKS.md#加入角色声音).

<a id="animation"></a>
## 5. Create animated artwork

Copy the [eye animation example](../Desktop-Packs/Characters/Examples/eye-rig/), then change its `slug` and card. Its `spica-eye-rig` format supports blinking and gaze tracking; native Cubism uses a separate optional [pack format](CUBISM_PACKS.md), not eye-rig files.

1. Replace `model/open.png` and `closed.png` with complete transparent images of identical size, pose, and placement. Change only the eyes.
2. Edit `model/character.eyerig.json`: set `canvas` to the image size and `gaze.origin` between the eyes. Recalibrate each eye's bounds, iris, and open/closed curves for your artwork; do not reuse the sample coordinates.
3. Keep `pack_format: 2`, `renderer: eye-rig`, and the `eye_rigs` mapping. Import, restart, and check blinking, gaze, and outfit changes.

Coordinates start at the original image's top-left corner. Each eye's `bounds` must be fully opaque in both images. See the [animation reference](CHARACTER_PACKS.md#live2d-包当前支持的是眼部原画动画) for calibration fields and multiple outfits.

<a id="dialogue"></a>
## 6. Create a dialogue style

Copy [`Desktop-Packs/Dialogue-Styles/sana/`](../Desktop-Packs/Dialogue-Styles/sana/) to `Desktop-Packs/Dialogue-Styles/my-style/`, keeping:

```text
my-style/
├── style.json
└── images/
    ├── surface.png
    ├── nameplate.png
    └── tail.png
```

| Change | Edit |
| --- | --- |
| List identity | Set `style_id` to `my-style`; update `name` and `author` |
| Dialogue and name backgrounds | Replace `surface.png` and `nameplate.png`, retaining transparency |
| Button and text colors | `colors.accent` and `colors.text`, such as `#E94E8B` |
| Font size, spacing, opacity | `text.size`, `layout.left/right`, `layout.surface_opacity` |
| Sentence animation | Replace `tail.png` and match the `tail` grid settings |

The Sana example uses **5 columns × 4 rows, 20 frames, 50ms per frame**, read left to right, row by row. Image dimensions must divide evenly by the grid. For a static marker, set `columns/rows/frames` to `1`. `placement: corner` uses the bottom-right corner; `inline` follows the text. Decorative lettering can be part of the artwork; the app renders the actual character name.

Import this folder separately in settings and restart. Button behavior and settings layout stay the same. Declare PNGs under `images/`, each ≤8 MiB, ≤4096 pixels per side, and ≤8 million pixels total. See the [style reference](DIALOGUE_STYLES.md) for all fields.

## 7. Remove and share packs

**× → 确定移除** removes a list entry while preserving sources and memory; reimport it later to restore it. **导出分享包** exports without app-saved private memory; **导出个人存档** includes personal memory for migration. Export to a new folder, then ZIP it. Check for private text written into the card and credit asset sources before sharing. Restart after changing the user name, character, or style.

<a id="settings"></a>
## 8. Change local settings

The desktop settings window has two tabs:

- **角色与外观 (Character & appearance)**: characters, costumes, dialogue styles, your name, scale, transparency, and volume.
- **应用设置 (Application)**: API address, model, reasoning effort, speech recognition, speech output, screen understanding, singing, anime, and companion reactions. Device, warmup, and defaults are under **高级选项 (Advanced)**.

Enter an API key and click **保存密钥 (Save key)**. For other changes, use **保存应用设置 (Save application settings) → 重启程序 (Restart)**. An empty key field keeps the existing key. The connection test checks the model list without sending a chat message. With no API key configured, the application tab opens automatically on startup.

Application preferences stay in local `data/config/app.yaml`, and keys stay in `xiaosan.env`. Switching characters preserves them; shared packs do not contain keys. Fields controlled by legacy environment variables or files show their source and reject conflicting saves; remove that override and restart. Restoring defaults only changes this page's draft and still requires saving; characters, memory, and keys are preserved. The browser configuration tool has been retired.

## Troubleshooting

- **No artwork**: select the folder containing `meta.json`, check image mappings, and finish the restart.
- **No speech**: check matching models, reference transcript, shared weights, and `tts.enabled`. Import success does not prove synthesis success.
- **Old name or model**: check environment overrides and restart after saving.
- **Windows path errors**: use `C:/Spica/models/file` in local YAML, relative paths inside packs, and extract ZIPs before importing.

## Qwen3-ASR: local / cloud

Local ASR uses **Qwen3-ASR-1.7B**. Windows reuses the single app environment
prepared by the [Windows setup](WINDOWS.md); leave the ASR Python setting empty.
Linux retains a separate Qwen worker environment. For Linux, prepare Python 3.11
with matching PyTorch, then run:

```bash
python -m pip install -r docs/requirements/requirements-qwen-asr.txt
python -c "from huggingface_hub import snapshot_download; snapshot_download('Qwen/Qwen3-ASR-1.7B', local_dir='models/stt/Qwen3-ASR-1.7B')"
```

In **设置 → 应用设置**, choose 本地 Qwen3-ASR-1.7B, set 本地模型目录 and
本地识别 Python to the downloaded directory and that environment's Python
executable (Linux: `bin/python`; Windows uses the app Python). CPU uses
`float32`; a supported NVIDIA GPU can use `bfloat16` or `float16`.
Save and restart. The application does not download models while chatting.

For cloud ASR, no local Qwen environment or weights are needed. Save the
**百炼语音识别 API Key**, select **百炼 Qwen 云端**, choose the matching account
region, save and restart. Valid utterances are uploaded to that service and
may incur charges; startup/self-check does not upload audio or verify billing.
There is no automatic fallback from local to cloud.

旧 Whisper/Google 配置会读取为本地 Qwen 默认配置；请重新配置模型目录（Windows 使用当前 Python），
或明确选择云端。密钥保存在本机 xiaosan.env，不放在 app.yaml 或角色包内。
普通 USB/系统麦克风在两个平台均可使用；只有使用 ReSpeaker 硬件 VAD 时才选择该选项。


[Current optional Home and standalone desktop configuration (中文)](README.md#独立桌面整合后的入口) · [Home setup](HOME.md) · [Local/cloud ASR](README.md#语音识别本地-qwen3-asr--百炼云端)

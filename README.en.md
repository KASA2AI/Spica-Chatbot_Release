[简体中文](README.md) · **English** · [日本語](README.ja.md)

# Spica Chatbot · Desktop pet setup and character creation

Spica is a character companion on a transparent desktop overlay. Type or speak, switch characters and outfits, play visual novels together, watch anime, or request a song. Persona, artwork, voice, and optional memory travel in a character folder; dialogue appearance is selected separately.

This is the standalone Windows/Linux desktop pet. It includes multiple characters, expression sprites, eye animation, independent dialogue styles, and character memory. It does not provide Mobile, robot, Hub, or motion-control interfaces; compatible character packs require none of those services.

[Project website and demos](https://www.acgkasa.me/) · [Character format reference](docs/CHARACTER_PACKS.md) · [Dialogue style reference](docs/DIALOGUE_STYLES.md)

Configuration recommendations and external links were checked on **2026-09-08**. Three README languages do not mean three supported conversation output languages: the current conversation voice pipeline is primarily Japanese, with Japanese or Chinese dialogue text.

## Contents

1. [Choose the components you need](#1-choose-the-components-you-need)
2. [Install the environment](#2-install-the-environment)
3. [Configure the API and application](#3-configure-the-api-and-application)
4. [First launch and folder locations](#4-first-launch-and-folder-locations)
5. [Create and fill in a character card](#5-create-and-fill-in-a-character-card)
6. [Place sprites and write metajson](#6-place-sprites-and-write-metajson)
7. [Add a speaking voice](#7-add-a-speaking-voice)
8. [Microphone recognition and singing](#8-microphone-recognition-and-singing)
9. [Create an animated eye pack](#9-create-an-animated-eye-pack)
10. [Backgrounds and dialogue styles](#10-backgrounds-and-dialogue-styles)
11. [Import switch remove and preserve memory](#11-import-switch-remove-and-preserve-memory)
12. [Verification troubleshooting and sharing](#12-verification-troubleshooting-and-sharing)

## 1. Choose the components you need

**Start with keyboard chat and the static example, then add speech, animation, and finally singing.** A character can have a persona and sprites without voice weights. The different kinds of “model” below have separate jobs.

| Component | Suggested starting point | Configuration / location | Separate copy per character? |
| --- | --- | --- | --- |
| Conversation LLM | `deepseek-v4-flash`, thinking disabled for prompt replies | `app.yaml` → `llm`; key in `xiaosan.env` | No |
| Microphone STT | faster-whisper `large-v3-turbo`, CTranslate2 format | Complete model directory selected by `stt.model` | No |
| Speaking TTS | GPT-SoVITS **v2ProPlus**; an existing matched v2Pro voice also works | GPT `.ckpt`, SoVITS `.pth`, reference recording in the character pack | Yes, for a distinct voice |
| Singing RVC | RVC v2 inference `.pth`, optionally its matching `.index` | `singing/`; separate from the TTS `.pth` | Optional |
| Static sprites | Transparent PNGs, grouped by outfit and expression | `visuals/` and manifest mappings | Yes |
| Eye animation | `spica-eye-rig`: open/closed artwork and calibration JSON | `model/`, `pack_format: 2` | Optional |
| Screen understanding | Existing RapidOCR / Moondream adapters | Local `screen` / `ocr` settings | Optional, shared |

Get a DeepSeek key from its [official console](https://platform.deepseek.com/); the current model ID and compatible API are documented in the [official quick start](https://api-docs.deepseek.com/). This is a practical desktop setup example, not a model ranking. Other compatible services need working streaming and tool calling.

Rendering, STT, and TTS run locally. If you configure a remote LLM, it receives conversation text, relevant persona/memory, and any recognized screen text or descriptions used in the conversation. Local screen recognition does not make the entire conversation offline.

## 2. Install the environment

### 2.1 Base environment for keyboard chat

Install Git, Conda, and **Python 3.11**. Use PowerShell/Anaconda Prompt on Windows or a terminal on Linux. Run commands from the repository root; activate the environment again with `conda activate spica` in each new terminal.

```bash
git clone https://github.com/KASA2AI/Spica-Chatbot_Release.git
cd Spica-Chatbot_Release
conda create -n spica python=3.11 -y
conda activate spica
python -m pip install --upgrade pip
python -m pip install -r requirements-windows-base.txt
python scripts/windows/check_imports.py
```

On Ubuntu/Debian, install the system dependencies needed to compile microphone support:

```bash
sudo apt-get install build-essential python3-dev portaudio19-dev ffmpeg
```

The base Python dependency list is also used for desktop execution on Linux, despite its filename. If PyAudio cannot compile, install the system headers first. Set **`mic_backend: generic` for an ordinary desktop microphone**. On Linux, `auto` selects the ReSpeaker path; it does not automatically select an arbitrary USB microphone.

### 2.2 Full NVIDIA GPU, speech, and singing dependencies

Complete the base installation before running these commands in order. This follows the repository's existing Windows GPU recipe: 64-bit Python 3.11, NumPy 1.26.x, and PyTorch CUDA 12.4 components. Windows uses pure-Python `jieba` without requiring MSVC for Chinese segmentation; an existing `jieba_fast` installation still takes priority. Linux needs a compiler for `jieba_fast`. Japanese uses the pinned [pyopenjtalk-plus](https://github.com/tsukumijima/pyopenjtalk-plus), which provides Windows wheels and a bundled dictionary.

```bash
python -m pip uninstall -y onnxruntime
python -m pip install -r requirements-windows-heavy.txt
python -m pip install torch==2.5.1 torchaudio==2.5.1 torchvision==0.20.1 --index-url https://download.pytorch.org/whl/cu124
python -m pip install -c constraints-windows-app.txt -r requirements-windows-app.txt
python -m pip install -c constraints-windows-app.txt --no-deps audio-separator==0.44.2
python -c "import numpy, torch; print(numpy.__version__, torch.__version__, torch.cuda.is_available())"
```

The check should show NumPy `1.26.4`, PyTorch `2.5.1+cu124`, and CUDA availability `True` on a supported NVIDIA setup. This checks dependencies; an actual speech sample is still required later. CPU users can remain in the basic text mode without installing TensorRT or voice weights just to display sprites.

`onnxruntime` and `onnxruntime-gpu` provide the same Python module. Reinstalling the base list after the GPU replacement can overwrite the GPU build. The locked environment installs `audio-separator` separately with `--no-deps`; an unconstrained upgrade to NumPy 2.x can break the speech stack.

Audio processing needs the actual `ffmpeg` executable on PATH. The `ffmpeg-python` package does not include it. Check with `ffmpeg -version`. Anime playback additionally uses qBittorrent with Web UI and VLC; these can wait until basic chat works.

## 3. Configure the API and application

### 3.1 Secret file

Create a UTF-8 file named **`xiaosan.env`** in the repository root, not `xiaosan.env.txt`. Replace the first value with your own DeepSeek API key; the other entries can remain empty:

```dotenv
OPENAI_API_KEY=REPLACE_WITH_YOUR_DEEPSEEK_API_KEY
JUDGE_API_KEY=
BILIBILI_COOKIE=
QBITTORRENT_PASSWORD=
```

The variable is called `OPENAI_API_KEY` because the application uses a common compatible adapter. For this example, its value is a **DeepSeek** key. `JUDGE_API_KEY` optionally supplies a separate visual-novel reaction judge; otherwise the main key is used. Keys do not belong in personas, manifests, screenshots, or shared packs.

### 3.2 Application configuration

Open **`data/config/app.yaml`**. New users can start with this text-mode configuration. Existing users should merge the relevant fields while retaining their other settings. YAML uses spaces, not tabs. `none` here is a string; `null` is the empty value.

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

| Field | What to enter |
| --- | --- |
| `llm.model` | An API model ID, such as `deepseek-v4-flash`, not a character voice filename. |
| `llm.base_url` | The API base URL, not the provider's chat website. Do not append `/chat/completions`. |
| `reasoning_effort` | `none` disables DeepSeek thinking in this adapter; `default` omits the control parameter. Check support when switching providers. |
| `system_turn_reasoning_effort` | Separate setting for proactive/system turns. `null` inherits the main setting; this example uses `none`. |
| `interlocutor_name` | What the character should call you. Later edit it in settings and restart. |
| `dialog_display_language` | `zh` shows Chinese translations; `ja` shows Japanese original text. There is no `en` option here. |
| `character.package_dir` | The **installed directory** written by import. Do not put a source folder, ZIP, or model file here. The first-run command below fills it in. |
| `tts.enabled` | Start with `false`; set `true` after preparing a voice and dependencies, then restart. |
| `stt.language` | The microphone user's language, such as `zh`, `ja`, or `en`; separate from the character's speech language. |

Old environment entries such as `MODEL`, `OPENAI_BASE_URL`, `REASONING_EFFORT`, `SPICA_USER_NAME`, `SPICA_SKILL_DIR`, and `SPICA_CHARACTER_PROFILE` override YAML. Keep secrets in `xiaosan.env` and ordinary preferences in YAML/settings where practical. If a name save reports an `SPICA_USER_NAME` override, remove the old process/dotenv entry, restart, and save again.

## 4. First launch and folder locations

### 4.1 Start without a large character asset download

Save the text-mode configuration above first. These commands install the tiny static example already in the repository and select it. They do not need the original Spica voice or full artwork. They change your local character selection; existing users can use settings instead.

```bash
python -c "from pathlib import Path; from spica.host.character_packages import import_character_folder; from spica.config.manager import ConfigManager; p = import_character_folder('Desktop-Packs/Characters/Examples/static', Path('data/runtime/characters')); ConfigManager().update({'character': {'package_dir': p.package_root, 'profile_override': None}}); print(p.name)"
python -X utf8 webui_qt.py
```

You should see the sample character and a dialogue box, ready for a typed message. Leave microphone input, screen recognition, and singing off during this first check. Example images are small original diagrams for learning the format, not the full Spica/Sana artwork.

**Starting on Windows:** keep using the same Python 3.11 environment. After activating it, run `python -X utf8 webui_qt.py`, or use the existing PowerShell launcher:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\windows\run_spica.ps1
```

The default Conda environment is `spica`. Append `-CondaEnv your-environment-name` to select another environment. To use an existing interpreter directly, replace the following example path with its actual location:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\windows\run_spica.ps1 -PythonExe "C:\Miniconda3\envs\spica\python.exe"
```

The launcher selects the project directory and enables UTF-8; paths may contain Chinese characters, spaces, and square brackets. Restart in settings preserves the interpreter and launch arguments. Character packs, dialogue styles, and eye animation use the same formats described below. In local YAML settings, write Windows absolute paths as `C:/Spica/models/file` or enclose backslash paths in single quotes. A player command with spaces can be written as `player_command: '"C:\Program Files\VideoLAN\VLC\vlc.exe" --play-and-exit'`.

### 4.2 Understand the directories before extracting assets

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

`Desktop-Packs/` holds editable, shareable **source folders**. `data/runtime/characters/` holds managed installations. `character_states/` holds character preferences and related private state. Memory can also use shared SQLite databases; use **Export personal save** for migration rather than assuming one folder contains everything.

Chatbot retains legacy built-in assets: persona in `spica_data/Spica_skill/`, sprites/reference audio in `spica_data/diffs/` and `spica_data/voice/`, speech weights in `artifacts/tts_slim/characters/spcia/`, and singing weights in `artifacts/rvc_slim/characters/spica/`. Imported characters use independent installations in `data/runtime/characters/`. Put newly authored source packs in `Desktop-Packs/`; you do not need duplicate copies in the legacy folders. Built-in legacy databases still use `spica_data/`; do not delete that directory wholesale. The legacy TTS directory really is spelled `spcia`; use `data/config/tts.yaml` for the exact weight filenames.

The existing [project asset download](https://pan.baidu.com/s/1EFq7t8Lxcy9kDNL7MzU1gg?pwd=nzjy) uses extraction code `nzjy`. Archive layouts differ between releases: inspect the top level first. A **character/style collection ZIP does not install the shared engines or STT model**. Put top-level `Characters/` and `Dialogue-Styles/` inside `Desktop-Packs/`; if the archive already contains `Desktop-Packs/`, merge that folder at the repository root. Avoid nesting the same directory twice.

Git clones do not contain the large Spica/Sana voice weights and artwork. Small `Examples/` and style examples are included. You can also build everything from assets you are entitled to use.

### 4.3 Local Config Studio

The base requirements already include Config Studio. If you are adding it to an older environment separately, install its dedicated requirements first.

```bash
python -m pip install -r requirements-config-studio.txt
python scripts/config_studio.py --port 8765
```

This separate browser-based configuration tool binds only to `127.0.0.1:8765` by default. Add `--no-open-browser` to open it manually and use the one-time bootstrap grant printed by the terminal. Use `--port 8767` if the port is occupied. Choose **中文 / English / 日本語** in the page's language menu. The language switch changes presentation text only; it does not change configuration keys or values. On Linux, save and restart the desktop pet. **Config Studio is currently read-only on Windows**; document writes and isolated self-checks are not yet supported there. On Windows, import/select characters and styles and change your name in the desktop settings; edit other options in `data/config/app.yaml` / `xiaosan.env` and restart. Press `Ctrl+C` in the terminal to stop Config Studio when finished. The browser UI language does not select the character's speech language.

## 5. Create and fill in a character card

### 5.1 Recommended open-source editor

Use [CCEditor online](https://lenml.github.io/CCEditor/?lang=en), with [source and license](https://github.com/lenML/CCEditor) available. Existing SillyTavern users can use its [character editor](https://docs.sillytavern.app/usage/characters/). These are tools for authoring a card; running another chat frontend is not a Spica requirement.

Start with your own character or a community card whose author allows your intended use. Read the text before exporting JSON. For an embedded PNG card, open it in the editor and export/copy its text fields. **Spica does not directly import SillyTavern PNG cards or whole V2/V3 JSON cards. Renaming one to `meta.json` does not convert it into a Spica pack.**

### 5.2 Map card fields into a Spica folder

Create `Desktop-Packs/Characters/my-character/`, preferably by copying the [static example](Desktop-Packs/Characters/Examples/static/). In V2/V3 exports, content is usually under `data`; older exports may store it at the top level. Transfer it as follows:

| Card field | Spica destination | How to adapt it |
| --- | --- | --- |
| `name` | `meta.json`: `name`, `char_name` | List label versus the actual character name. |
| `description` | Identity/history in `persona.md` | Explain who the character is and their relationship with you. Keep relevant facts. |
| `personality` | Behavior and voice in `persona.md` | Describe observable responses and habits, not only adjectives. |
| `scenario` | Current relationship/setting in `persona.md` | Adapt to ongoing desktop companionship; remove incompatible one-shot constraints. |
| `mes_example` | Short dialogues in `persona.md` | Use `{{user}}` and `{{char}}` consistently. |
| `first_mes` / alternate greetings | Optional examples of speaking style | They do not automatically run as the application's startup greeting. |
| `character_book` / lorebook | Summarize into a declared `worldbook.md` | Plain background text; keyword activation, insertion depth, and regex scripts are not executed. |
| Author/source/license | `README.md`, `LICENSE`, `sources.tsv` | Preserve attribution and the applicable asset permissions. |

Do not copy an entire Tavern system preset, jailbreak template, or extension script. Spica already manages response language, translation, expression metadata, and tools. The persona should mainly define identity, relationships, and speaking behavior.

### 5.3 A persona.md template to adapt

Save UTF-8 text. Begin with concrete facts and a few examples, then refine it through actual conversations:

```markdown
# Identity
You are {{char}}, an original character living on the desktop and talking with {{user}}.
You are familiar friends and can continue previous ordinary conversations.

# Personality and voice
Curious, gentle, and occasionally playful. Respond to specific feelings when offering support.
Do not invent {{user}}'s actions, dialogue, or life history.
Keep everyday replies natural and brief. Follow the application's Japanese speech and subtitle format.

# Preferences and relationship
You enjoy stars and hot tea. Treat {{user}} as an equal friend.
Be honest about uncertainty; do not present guesses as shared memories.

# Speaking examples
{{user}}: I'm a little tired today.
{{char}}: お疲れさま。少し休んで、お茶でも飲もうか。
{{user}}: Do you remember what we discussed yesterday?
{{char}}: 覚えていることから、一緒に振り返ってみよう。
```

A small `worldbook.md` is enough initially:

```markdown
{{char}} calls the little desktop home Star Cottage.
旅人 is an old name for {{user}}; both refer to the same person.
```

The manifest below declares `旅人` in `user_aliases` and replaces it with your current name when loading. Prefer `{{user}}` in new text. Alias replacement is textual, so avoid very short common words that could also appear inside unrelated words.

A persona may use `SKILL.md`, `self.md`, and `persona.md`, with at least one present. A new pack normally needs only `persona.md`. These are character text, not executable scripts or Codex skills. Handwritten background in `self.md` is also included in a share export; it is not automatically treated as private database memory.

## 6. Place sprites and write meta.json

### 6.1 Source pack layout

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

`voice/` and `singing/` are optional later additions. For now, use the persona, worldbook, and two images. Omit voice blocks entirely until their files are ready; do not enter nonexistent placeholder paths.

### 6.2 Complete static manifest

Save this as `meta.json`. JSON does not allow comments, trailing commas, or single-quoted strings:

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

| Field | Meaning / editing rule |
| --- | --- |
| `pack_format` | `1` for static packs, `2` for eye-animation packs. |
| `slug` | Stable identity. Prefer lowercase letters, digits, and hyphens, e.g. `hoshi`. Keep it for upgrades; use a new ID for an independent character. |
| `version` | Author's display version, e.g. `1.0.1`. Asset changes also produce a new installation revision. |
| `name` / `char_name` | List label / actual speaker name and `{{char}}` replacement. |
| `worldbook_file` | Optional worldbook path. Remove the field if there is no file. |
| `sprites` | Your image IDs mapped to relative paths; later mappings refer to these IDs. |
| `default_costume` | Must equal one of the `costumes[].id` values. |
| `default_sprite` | An ID already declared in `sprites`, not a PNG path. |

Paths use `/` relative to `meta.json`. Do not use drive paths, `/home/...`, `../...`, or symlinks. Names and IDs must not differ only by letter case because Windows cannot reliably distinguish them. Update the manifest whenever a file is renamed.

### 6.3 Prepare the actual artwork

1. Use PNGs with a real transparent alpha channel outside the character. A white background is not transparency.
2. Keep the **same canvas, character position, scale, and foot baseline** across expressions in one view. Otherwise the character jumps when switching images.
3. Import fits ordinary static sprites proportionally onto a `1024×1024` transparent canvas, centered horizontally and aligned to the bottom. Keep dialogue UI and scene backgrounds out of the sprite.
4. Organize outfits under paths such as `visuals/casual/` and `visuals/school/`. Medium and close views can also be separate outfit entries. Folder names alone do not create mappings.
5. The four emotion keys are `happy`, `angry`, `sad`, and `surprised`. Start with idle and happy; missing emotion artwork falls back to that outfit's default image.

### 6.4 Add an outfit or more expressions

Add `visuals/school/idle.png` and `visuals/school/happy.png`. Add `"school-idle": "visuals/school/idle.png"` and `"school-happy": "visuals/school/happy.png"` to the existing `sprites` object, then append this object to the existing `costumes` array:

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

Set `default_costume` to `school` to start in that outfit. More emotions follow the same sequence: file, sprite ID, then emotion mapping. An emotion can reference multiple IDs. Detailed pose/three-digit expression mappings are optional; see the [format guide](docs/CHARACTER_PACKS.md). A first pack does not need all of Spica's expression rules.

## 7. Add a speaking voice

### 7.1 Which two models do you need?

Character speech supports **GPT-SoVITS v2Pro/v2ProPlus**. Start new voice preparation with **v2ProPlus**, or retain the actual version of an existing matched v2Pro set.

- GPT `.ckpt`: the speech semantic model, selected by `tts.gpt`.
- SoVITS `.pth`: the matching voice generator, selected by `tts.sovits`.
- Reference WAV and its verbatim transcript: the voice/style example in `tts.reference`.

Obtain a matched set from the voice author, or train using recordings you can use with the [official GPT-SoVITS project](https://github.com/RVC-Boss/GPT-SoVITS). Spica performs inference and does not include a training UI. An RVC `.pth`, arbitrary training checkpoint, LoRA, or incompatible version cannot be converted by changing its extension or `model_version`.

For an initial experiment without fine-tuning, you can try the corresponding upstream base pair with a reference recording. This does not guarantee the similarity of a character-specific trained voice; listen to pronunciation and stability before adopting it.

### 7.2 Shared inference weights

The character pair does not replace GPT-SoVITS's shared dependencies. Retain the bundled slim engine source and install the complete base model folders below it. Use the project asset archive or download the v2Pro-related files from the [upstream model repository](https://huggingface.co/lj1995/GPT-SoVITS):

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

The `chinese-*` directories are shared engine dependencies even for Japanese characters. For a base-model experiment, copy `s1v3.ckpt` and `s2Gv2ProPlus.pth` **inside your character pack** and reference their actual filenames. For a trained voice, use the author's matching pair instead.

### 7.3 Reference recording and manifest

Start with a clean, single-speaker **3–10 second WAV**, without background music. `text` is exactly what that recording says, not a translation or a request for future output. Replace the Japanese sample text below with your recording's actual transcript.

Merge this `tts` field into the top level of the existing `meta.json`, retaining its identity and visual fields:

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

Set the real `model_version`, `v2Pro` or `v2ProPlus`. `reference.language` describes the recording; `target_language` describes synthesis. The accepted strings are literally **`日文` / `中文` / `英文`**, even when following the English or Japanese README. The current chat voice is primarily Japanese; changing this field to `英文` does not switch the whole conversation pipeline to English.

Keep `top_k`, `top_p`, and `temperature` at the example values initially and start with `speed: 1.0`. Use one stable reference before tuning multiple emotions.

Optionally add an `emotions` object inside `tts`:

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

Those WAVs must exist and their transcripts must match your own recordings. Unspecified emotions reuse the main reference. A reference can additionally contain an `additional_audio` array; omit it when unnecessary.

Finally set local `app.yaml` → `tts.enabled: true`, import the edited character again, and restart. Test a short Japanese sentence. Import validates resources and structure without synthesizing audio; success at import is not proof that the voice has been heard successfully.

## 8. Microphone recognition and singing

### 8.1 Speech recognition

Download the complete [CTranslate2 large-v3-turbo model](https://huggingface.co/dropbox-dash/faster-whisper-large-v3-turbo):

```bash
python -c "from huggingface_hub import snapshot_download; snapshot_download('dropbox-dash/faster-whisper-large-v3-turbo', local_dir='spica_data/models/faster-whisper-large-v3-turbo')"
```

The directory should contain real `model.bin`, `config.json`, `tokenizer.json`, and related files, not Git LFS pointers. Reuse a complete existing download. This application uses [faster-whisper](https://github.com/SYSTRAN/faster-whisper), so an arbitrary Whisper `.pt` is not a substitute for this directory.

Set `stt.device: cuda` and `compute_type: float16` for NVIDIA, or `device: cpu` and `compute_type: int8` for CPU. Once the files are ready, `warmup_on_startup: true` reduces first-use model loading. Set `language` to what **you** speak: `zh`, `ja`, or `en`. Select the correct system input/output devices and test a short utterance.

### 8.2 RVC singing voice

Speech and singing use separate models. Put an [Applio/RVC](https://github.com/IAHispano/Applio)-compatible RVC v2 inference model at `singing/model.pth` and its matching index, if supplied, at `singing/model.index`. Merge this top-level field into the manifest:

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

Without an index, remove the `index` field rather than referencing a missing file. `transpose` is pitch shift in semitones, not speaking speed. Keep the initial `index_rate` and `protect` values. The local shared RVC engine, HuBERT/RMVPE weights, and song-separation dependencies are also needed; shared files from the asset archive live under `artifacts/rvc_slim/base/`.

Set local `song.enabled: true`, import, and restart. Omitting `rvc` disables singing for that character. A pack cannot enable a feature that the local application has disabled. API keys, STT, GPU selection, and Python paths always stay in local configuration.

## 9. Create an animated eye pack

**The supported animation format is `spica-eye-rig`**, using original artwork for mouse gaze following, smooth blinking, and restoration of the animated default after expression changes. It is not a Cubism player: `.model3.json` and `.moc3` cannot be imported, and full skeletal physics or automatic lip sync are not provided.

Copy the [eye-rig example](Desktop-Packs/Characters/Examples/eye-rig/) and verify its original animation before replacing images:

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

Keep the static pack's identity, persona, worldbook, and optional voice. Replace `pack_format` and the entire `visuals` object with this fragment; retain all other required top-level fields:

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

### 9.1 Prepare the artwork

`open.png` and `closed.png` are **complete RGBA character images with the same size, pose, and canvas**; only the eyes change. Do not crop the closed image into an eye patch. Calibrated eye rectangles must be fully opaque in both images; transparency belongs outside the character. Canvas dimensions must not exceed 4096 pixels on either side.

### 9.2 Calibrate your own images

Edit the example's [character.eyerig.json](Desktop-Packs/Characters/Examples/eye-rig/model/character.eyerig.json). Its coordinates only fit its own `200×200` demonstration image; replacing the character requires new measurements.

| Field | What to measure |
| --- | --- |
| `canvas.width/height` | Actual dimensions of both complete images. |
| `textures.open/closed` | Paths relative to the **rig JSON's directory**; here `open.png` and `closed.png`. |
| `gaze.origin` | Reference point `[x,y]`, usually between the eyes. |
| `gaze.maximum_offset` | Maximum movement `[dx,dy]`, capped at 32 and 16 pixels respectively. Start small. |
| `gaze.strength/smoothing_ms` | Strength and smoothing duration; begin with `0.7` and `95`. |
| `blink` | Blink duration, initial delay, and random interval; keep the sample initially. |
| `eyes[].bounds` | `[x,y,width,height]` covering the eye and lashes, up to 512 pixels per dimension. |
| `eyes[].iris` | Iris **left/right x boundaries**, `[left_x,right_x]`, not a two-dimensional position. |
| `eyes[].contour` | Left-to-right samples `[x,upper_lid_y,lower_lid_y,lash_top_y]`, at least three. |
| `eyes[].closedLine` | Closed-eye `[x,y]` curve, at least two points covering both ends of the open contour. |

Use pixels from the complete image's top-left corner: x increases rightward and y downward. Do not use screen coordinates or percentages. Curve x values must increase, points must remain within `bounds`, and lash top ≤ upper lid ≤ lower lid. Leave room beside the iris for movement. Fix the geometry reported by validation rather than expanding bounds into transparent areas.

Different outfits can map their default sprite IDs to different rigs. Expression images without rigs remain static. Animated and static packs use the same persona, name substitution, and memory rules.

## 10. Backgrounds and dialogue styles

### 10.1 Character scene backgrounds

Optionally add `backgrounds/room.png` and `ui/settings-cg.png`, then merge these top-level manifest fields:

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

Chatbot preserves and can export these fields for package compatibility, but the desktop pet does not render Mobile/robot backgrounds or connect to those devices. They do not require adding a Hub address or mobile interface. Configure desktop dialogue backgrounds and colors with the independent style pack below.

### 10.2 Author a separate dialogue style

Copy `Desktop-Packs/Dialogue-Styles/spica/` or `sana/` as a structural reference. Replace the artwork, change `style_id` and the display name, and keep the style in its own folder:

```text
my-style/
├── style.json
└── images/
    ├── surface.png
    ├── nameplate.png
    └── tail.png
```

A minimal animated `style.json`:

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

`surface.png` is the transparent dialogue background. `nameplate.png` is optional; add `"nameplate": "images/nameplate.png"` under `images` to use it. The application draws the actual character name, so do not bake a fixed speaker name into the background. Decorative text such as “Character Name” can be part of the image.

This example needs a **5-column × 4-row, 20-frame** `tail.png` atlas, played left to right and then top to bottom. At 50 ms per frame, a cycle lasts about one second. Image dimensions must divide evenly by the grid. Deliberate blank frames from Sana's animation may remain. For a static marker, set columns, rows, and frames to `1`.

`placement: corner` anchors the marker at the dialogue corner; `inline` follows the text. It appears after text presentation completes, with a visible pause between consecutive sentences. `colors`, `text`, and `layout` configure appearance; see the [JSON Schema](docs/dialogue-style.schema.json). Declared PNGs must be under `images/`, at most 8 MiB each, no dimension over 4096, and no more than eight million pixels.

A style changes presentation and local layout while retaining existing button behavior and settings structure. It does not execute scripts. Importing a character does not automatically select a similarly named dialogue style.

## 11. Import, switch, remove, and preserve memory

### 11.1 Everyday operation

1. Open the gear settings and choose **导入角色文件夹** (Import character folder). Select the directory directly containing `meta.json`.
2. Wait for the import-completed message. Large voice packs copy weights, so leave disk space for both source and installation.
3. Under **对话框样式** (Dialogue style), optionally import a directory containing `style.json`.
4. Enter the name you want the character to use for you; press Enter or leave the field to save.
5. Click **重启程序** (Restart). Character, name, and style changes take effect after restart. Restart during a conversation stops current playback and waits for background work to exit safely.

Previously imported characters and styles are available in their dropdowns. Outfit changes normally apply immediately, but are temporarily disabled until the current reply and audio finish so later sentences cannot restore an old outfit.

### 11.2 Remove a list entry

Expand the character/style list → click **×** on that row → **确定移除** (Confirm removal). Cancel keeps the selection. Built-in defaults do not have a cross.

Removal **preserves the source, installed resources, and memory; it does not free disk space**. Removing the saved selection keeps current playback using its loaded resources and restores that setting's built-in Spica default on the next restart. Manually import the original folder later to restore the entry.

### 11.3 Share packs and personal saves

Import and restart into the character you want to export, then choose:

| Action | Contents | Intended use |
| --- | --- | --- |
| **导出分享包** (Export share pack) | Persona, declared sprites/rigs/voices, worldbook, author files; no application-generated personal save | Share a character whose assets you can distribute |
| **导出个人存档** (Export personal save) | Those resources plus character long-term memory, recent ordinary chat, shared visual-novel memories, and outfit | Move your character state to another machine |

Both actions need a **new destination folder that does not already exist**. Carry dialogue styles separately. ZIP the exported folder; recipients extract it and import the concrete character/style folder. Do not publish an archive of the whole project or `data/runtime/`.

Keep `slug` unchanged for an upgrade to retain local memory. A personal save initializes a character only when no local save already exists; it does not forcibly replace another machine's existing memories. Static and animated variants sharing a `slug` share the same identity. Use a new slug when you need an independent memory identity.

## 12. Verification, troubleshooting, and sharing

### 12.1 Check your pack before sharing

- Import the source folder, restart, and verify its name, default outfit, and persona.
- Change your name and restart; check that the next reply uses it and sentences containing it still produce speech.
- Check that expression switches do not move the character; verify blink and gaze range on animated packs.
- For voice packs, test a short sentence, a longer sentence, and different emotions. Import success alone is insufficient.
- Check the animated tail, input, send, settings, and restart controls.
- Ensure a share export contains no private chats, keys, or device configuration, and has the relevant asset permissions and attribution.

### 12.2 Troubleshooting

| Symptom | First checks |
| --- | --- |
| 401 / authentication error | Key belongs to the provider at `base_url`; dotenv is in the repository root. |
| 404 / unknown model | Use a supported API model ID and base URL, not a chat website or retired alias. |
| Saved setting still looks old | Restart and check for old process/dotenv overrides. |
| Text but no speech | `tts.enabled`, the pack's `tts` block, matched weights, and accurate reference transcript. Text examples are intentionally silent. |
| No sprite | Correct folder, valid default IDs, file paths, and case. |
| Eye-pack validation fails | Canvas size, opaque eye bounds, iris margin, and curve coordinates. Cubism files are not supported. |
| Slow first reply | Distinguish LLM waiting, first STT download, TTS warmup, and Chinese translation waiting; retest after downloads/warmup. DeepSeek can use `none`. |
| Linux ReSpeaker error | Use `mic_backend: generic` for a normal microphone. |
| CUDA / DLL error | Python environment, driver, pinned dependencies, and conflicting CPU/GPU ONNX Runtime packages. |
| Corrupt installed style | Reimport an intact original style folder to repair it, then restart. |
| Disk usage unchanged after removal | List removal deliberately retains assets and memory. |
| Cannot import ZIP | Extract it first; choose the actual character/style directory, not the collection's parent. |

### 12.3 Other optional features

Configure qBittorrent Web UI, the player, and download location under `anime`; keep its password and any needed Bilibili cookie in `xiaosan.env`. Select local screen adapters through `screen/ocr`. Enable heavyweight vision after keyboard chat and speech work. Latency and VRAM vary with hardware, models, and input length; one deployment's measurements are not minimum hardware guarantees.

### 12.4 License and upstream projects

Spica uses the **Source-Available license** in [LICENSE](LICENSE), not an unrestricted open-source redistribution license. Project characters, voices, and original-game artwork have their own permitted uses. State authorship, sources, and license for your own community pack and check third-party redistribution terms. This guide changes no licenses.

Thanks to [GPT-SoVITS](https://github.com/RVC-Boss/GPT-SoVITS), [Applio](https://github.com/IAHispano/Applio), [faster-whisper](https://github.com/SYSTRAN/faster-whisper), [RapidOCR](https://github.com/RapidAI/RapidOCR), [Moondream](https://github.com/vikhyat/moondream), and [yt-dlp](https://github.com/yt-dlp/yt-dlp), under their respective licenses. See [SillyTavern's official character-design guide](https://docs.sillytavern.app/usage/core-concepts/characterdesign/) for card-authoring concepts, and the [Spica character schema](docs/character-pack.schema.json) for the fields this application actually imports.

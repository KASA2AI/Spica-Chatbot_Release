[简体中文](README.md) · **English** · [日本語](README.ja.md)

# Spica

**A local desktop voice roleplay companion.** The character is Spica (辻倉朱比華) — a transparent desktop avatar you talk to by voice in real time. She chats with you, plays galgames alongside you, watches anime with you, and sings for you. Everything runs on your own machine; your screen is never uploaded.

> 👉 **Homepage: [www.acgkasa.me](https://www.acgkasa.me/)** — demo videos, debug logs, and video tutorials.

---

## ✨ Features

- **🎙️ Voice conversation** — local speech recognition + character voice synthesis; talk to Spica in real time (typing works too).
- **🎮 Galgame companion** — Spica watches you play, reads the current story (OCR), reacts along with you, and remembers the moments you shared.
- **📺 Watch anime together** — find a show → download (magnet / Bilibili) → play, with Japanese–Chinese bilingual subtitles.
- **🎵 Singing** — ask Spica to sing in her own voice.
- **👀 Screen watching** — recognize on-screen content from a screenshot (local OCR + vision model, **never uploaded**).
- **💬 Speaks up on her own** — she starts a conversation at the right moments.
- **🖥️ Desktop overlay** — a transparent PySide6 avatar layer with multiple outfit variations (school uniform / casual wear / pajamas …).
- **Cross-platform** — runs on both Linux and Windows.

---

## 🚀 Installation

### 1. Requirements

- **Python 3.11**
- **NVIDIA GPU recommended** (voice synthesis / speech recognition / screen OCR run smoother on GPU; CPU-only works but is slow)
- OS: Linux or Windows 10/11

### 2. Clone

```bash
git clone https://github.com/KASA2AI/Spica-Chatbot_Release.git
cd Spica-Chatbot_Release
```

### 3. Install dependencies

Use a dedicated virtual environment (conda / venv). Install for your platform:

```bash
# base + speech recognition + screen recognition
pip install -r requirements-stt.txt
pip install -r requirements-screen.txt

# Windows users: see the split files below
#   requirements-windows-base.txt   base runtime
#   requirements-windows-app.txt    voice synthesis + singing (with constraints-windows-app.txt)
#   requirements-windows-heavy.txt  heavy GPU runtime
```

> Voice synthesis / singing use a fairly heavy local runtime and take a lot of space to install — a machine with a GPU is recommended.

### 4. Download the model asset pack (large files not in the repo)

**The engine source code (GPT-SoVITS / RVC) is already in the repo — `git clone` gets it.** This step only fills in the large files that don't belong in git: model weights, the speech-recognition model, TTS reference audio, and avatar art. Download the model asset pack and extract it at the project root; the weights drop into the right engine directories automatically:

```bash
# run inside Spica-Chatbot_Release/
unzip spica_full_assets_*.zip
```

After extraction the layout should look like this:

```text
Spica-Chatbot_Release/
  artifacts/
    tts_slim/
    rvc_slim/
  spica_data/
    models/
    voice/
    diffs/
```

Do NOT extract the archive into `spica_data/` or `artifacts/`, or you'll get a nested `spica_data/spica_data/...` and the program won't find the models.

The pack contains (all large files that aren't in git):

| Content | Extract to | ~Size |
| --- | --- | ---: |
| Voice-synthesis engine weights + Spica's voice (GPT-SoVITS slim) | `artifacts/tts_slim/` | ~1.4 GB |
| Singing engine weights + Spica's singing voice (RVC slim) | `artifacts/rvc_slim/` | ~620 MB |
| Speech-recognition & other models | `spica_data/models/` | ~1.6 GB |
| TTS reference audio | `spica_data/voice/` | ~12 MB |
| Spica avatar variations | `spica_data/diffs/` | ~720 MB |

You can quickly check everything landed correctly:

```bash
test -f artifacts/tts_slim/base/GPT_SoVITS/pretrained_models/chinese-hubert-base/pytorch_model.bin
test -f artifacts/rvc_slim/base/rvc/models/predictors/rmvpe.pt
test -f spica_data/models/faster-whisper-large-v3-turbo/model.bin
test -d spica_data/voice/happy
test -d spica_data/diffs
```

`artifacts/trt/` does not need to be downloaded or uploaded; it's a machine-local TensorRT engine/timing cache tied to your GPU architecture and CUDA/TensorRT/ONNXRuntime versions, regenerated locally when needed.

> 📦 **Model asset pack download:** [Baidu Netdisk](https://pan.baidu.com/s/1GKmnKMEtkQq_b1aSmdTqQw?pwd=m8ee), extraction code: `m8ee`

### 5. External programs (as needed)

- **Watching anime** needs: [qBittorrent](https://www.qbittorrent.org/) (with the Web UI enabled), [ffmpeg](https://ffmpeg.org/), [VLC](https://www.videolan.org/)
- **Microphone array** (optional): ReSpeaker (a regular microphone is used if none is connected)

### 6. Configure secrets

Create a `xiaosan.env` in the project root and fill in as needed:

```env
OPENAI_API_KEY=your LLM API key         # required: for conversation
JUDGE_API_KEY=                          # optional: falls back to the key above
BILIBILI_COOKIE=                        # optional: for Bilibili downloads when watching anime
QBITTORRENT_PASSWORD=                   # optional: for magnet downloads when watching anime
```

> `xiaosan.env` is already in `.gitignore` and will not be committed.

### 7. Run

```bash
python webui_qt.py
```

Feature toggles (enable anime / singing / screen recognition, audio device, GPU, etc.) live in `data/config/app.yaml`.

---

## 🙏 Acknowledgements

Spica stands on these excellent open-source projects:

- [GPT-SoVITS](https://github.com/RVC-Boss/GPT-SoVITS) — voice synthesis
- [Applio / RVC](https://github.com/IAHispano/Applio) — singing voice conversion
- [faster-whisper](https://github.com/SYSTRAN/faster-whisper) — speech recognition
- [RapidOCR](https://github.com/RapidAI/RapidOCR) — on-screen text recognition
- [Moondream](https://github.com/vikhyat/moondream) — visual understanding
- [yt-dlp](https://github.com/yt-dlp/yt-dlp) — video download

All components are the property of their respective authors; please comply with their individual licenses.

---

## 📄 License

_(License TBD — please fill in. Note the individual licenses of the components referenced above.)_

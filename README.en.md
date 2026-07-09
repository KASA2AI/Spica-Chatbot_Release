[简体中文](README.md) · **English** · [日本語](README.ja.md)

# Spica

**A local desktop voice roleplay companion.** The character is Spica (辻倉朱比華) — a transparent desktop avatar you talk to by voice in real time. She chats with you, plays galgames alongside you, watches anime with you, and sings for you. Everything runs on your own machine; your screen is never uploaded.

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

### 4. Download engines and models (not in the repo)

The voice-synthesis / singing engines (the GPT-SoVITS and RVC runtime code + weights) are large, and together with the recognition models and avatar art they are **not shipped with the code repo**. Download them separately and extract into the matching directories — **if any of these packs is missing, the corresponding feature won't start**:

| Content | Extract to | ~Size |
| --- | --- | ---: |
| Voice-synthesis engine + Spica's voice (GPT-SoVITS slim) | `artifacts/tts_slim/` | ~1.4 GB |
| Singing engine + Spica's singing voice (RVC slim) | `artifacts/rvc_slim/` | ~620 MB |
| Speech-recognition & other models | `spica_data/models/` | ~1.6 GB |
| TTS reference audio | `spica_data/voice/` | ~12 MB |
| Spica avatar variations | `spica_data/diffs/` | ~720 MB |

> 📦 **Download link:** _(to be filled — cloud drive / Release asset / HuggingFace)_

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

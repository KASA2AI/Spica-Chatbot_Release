[简体中文](README.md) · **English** · [日本語](README.ja.md)

# Spica Chatbot

**A voice companion living on your desktop.** Transparent character artwork, live conversation, galgame companionship, anime watching, and singing—with your own characters, voices, and dialogue styles.

[Website & demos](https://www.acgkasa.me/) · [Setup & customization](docs/README.en.md) · [Character assets](https://pan.baidu.com/s/1RDoz_iRvFJdB71Aia3EPaw?pwd=j2u6)

## ✨ Features

- **Conversation**: typing, microphone input, proactive replies, and character memory.
- **Multiple characters**: import cards, outfits, expressions, and voices; restart to switch.
- **Animated artwork**: gaze tracking and blinking with the original eye artwork.
- **Dialogue styles**: separate packs for colors, backgrounds, and animated sentence markers.
- **Desktop activities**: galgame companionship, screen understanding, anime, and singing.

A desktop-only app for Windows / Linux. Speech and screen recognition run locally; when using a remote chat model, conversation text, relevant character cards, and memory are sent to the configured service.

## 🚀 Get started

Use **Python 3.11, Git, and Conda**. An NVIDIA GPU is recommended for speech and heavier features.

```bash
git clone https://github.com/KASA2AI/Spica-Chatbot_Release.git
cd Spica-Chatbot_Release
conda create -n spica python=3.11 -y
conda activate spica
python -m pip install --upgrade pip
python -m pip install -r docs/requirements/requirements-windows-base.txt
```

On Ubuntu / Debian, install `build-essential python3-dev portaudio19-dev ffmpeg` before the Python dependencies. The base list also supports Linux.

Follow [first-time setup](docs/README.en.md#setup) to configure the API and start with the small text-chat example. See [speech setup](docs/README.en.md#voice) for additional dependencies and models. Once configured, launch with:

```bash
conda activate spica
python -X utf8 webui_qt.py
```

Windows also supports `powershell -ExecutionPolicy Bypass -File .\scripts\windows\run_spica.ps1`; its default Conda environment is `spica`.

## 📦 Download characters and styles

[Baidu character asset pack](https://pan.baidu.com/s/1RDoz_iRvFJdB71Aia3EPaw?pwd=j2u6) · Access code: **`j2u6`**

Extract the ZIP first. Keep character sources in `Desktop-Packs/Characters/` and styles in `Desktop-Packs/Dialogue-Styles/`. In settings, import the character folder containing `meta.json` and the style folder containing `style.json` separately, then click **重启程序** (Restart).

Large weights and complete character artwork are not included in Git. Character packs do not replace shared speech-engine or recognition models. Small [static](Desktop-Packs/Characters/Examples/static/) and [animated](Desktop-Packs/Characters/Examples/eye-rig/) examples are included.

## 📖 Documentation

- [Setup & customization](docs/README.en.md): startup, character cards, voices, animated artwork, and dialogue styles.
- [Character format reference](docs/CHARACTER_PACKS.md) · [Dialogue style reference](docs/DIALOGUE_STYLES.md) — Chinese.
- [Dependency lists](docs/requirements/): base, GPU, speech, and optional features.

## License

The code uses a [Source-Available license](LICENSE). Character artwork, voices, and other third-party assets retain their own licenses. Credit authors and sources when sharing a pack.

Thanks to [GPT-SoVITS](https://github.com/RVC-Boss/GPT-SoVITS), [Applio](https://github.com/IAHispano/Applio), [faster-whisper](https://github.com/SYSTRAN/faster-whisper), [RapidOCR](https://github.com/RapidAI/RapidOCR), [Moondream](https://github.com/vikhyat/moondream), and [yt-dlp](https://github.com/yt-dlp/yt-dlp).

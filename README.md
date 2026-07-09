**简体中文** · [English](README.en.md) · [日本語](README.ja.md)

# Spica

**本地运行的桌面语音角色扮演陪伴应用。** 角色是 Spica（辻倉朱比華）——一个透明桌面立绘 + 实时语音对话的伙伴，能陪你聊天、一起玩 galgame、一起看番、给你唱歌。全部在你自己的机器上跑，屏幕内容不上传。

---

## ✨ 功能

- **🎙️ 语音对话** —— 本地语音识别 + 角色语音合成，和 Spica 实时语音聊天（也支持打字）。
- **🎮 galgame 陪玩** —— Spica 看着你玩，识别当前剧情、陪你吐槽、记住你们一起玩过的经历。
- **📺 一起看番** —— 找番 → 下载（磁力 / B 站）→ 播放，支持中日双语字幕。
- **🎵 点歌唱歌** —— 让 Spica 用她自己的声线给你唱歌。
- **👀 看屏幕** —— 截图识别桌面内容（本地 OCR + 视觉模型，**绝不上传**）。
- **💬 主动开口** —— 她会在合适的时候主动跟你说话。
- **🖥️ 桌面 overlay** —— PySide6 透明立绘覆盖层，多套立绘差分（校服 / 私服 / 睡衣 …）。
- **跨平台** —— Linux 与 Windows 均可运行。

---

## 🚀 安装

### 1. 环境要求

- **Python 3.11**
- 建议 **NVIDIA GPU**（语音合成 / 语音识别 / 屏幕 OCR 走 GPU 更流畅；纯 CPU 也能跑但慢）
- 操作系统：Linux 或 Windows 10/11

### 显存占用实测（2026-07-09）

测试环境：NVIDIA GeForce RTX 4090 24GB，使用 `nvidia-smi` 约 0.2 秒间隔采样。测试前桌面/系统基线约 `2193 MiB`，下表记录 Spica 相关进程树的显存峰值。

| 功能 | 显存峰值 |
| --- | ---: |
| TTS GPT-SoVITS 四情绪 warmup | `2020 MiB` |
| STT faster-whisper warmup | `2080 MiB` |
| OCR RapidOCR full-frame | `1114 MiB` |
| 屏幕理解 OCR + Moondream HF | `5284 MiB` |
| 点歌分离 audio-separator | `2768 MiB` |
| RVC subprocess 20s | `2140 MiB` |
| 全重型链路叠加峰值（TTS + STT + OCR/Moondream + 点歌分离 + RVC） | `11116 MiB` |

整卡最高 used 为 `13340 MiB`；扣除测试前基线后，Spica 本次测得的软件进程树峰值约 `10.9 GiB`。LLM 对话 / 总结 / 吐槽 judge 默认走远端 OpenAI-compatible 接口，本地基本不占显存；看番下载与播放器主要依赖 qBittorrent / VLC，未计入 Python CUDA 进程峰值。

### 2. 拉代码

```bash
git clone https://github.com/KASA2AI/Spica-Chatbot_Release.git
cd Spica-Chatbot_Release
```

### 3. 装依赖

建议用独立虚拟环境（conda / venv）。按你的平台装：

```bash
# 基础 + 语音识别 + 屏幕识别
pip install -r requirements-stt.txt
pip install -r requirements-screen.txt

# Windows 用户另见下面的分文件（base / app / heavy）：
#   requirements-windows-base.txt   基础运行
#   requirements-windows-app.txt    语音合成 + 唱歌（配合 constraints-windows-app.txt）
#   requirements-windows-heavy.txt  GPU 重型运行时
```

> 语音合成 / 唱歌 用到较重的本地运行时，安装体量较大，建议在有 GPU 的机器上装。

### 4. 下载引擎包与模型（不在仓库里）

语音合成 / 唱歌引擎（GPT-SoVITS、RVC 的运行时代码 + 权重）体积大，和识别模型、立绘差分一起**都不随代码仓库分发**。请单独下载并解压到对应目录——**这几个包缺任何一个，对应功能就起不来**：

| 内容 | 解压到 | 约大小 |
| --- | --- | ---: |
| 语音合成引擎 + Spica 声线（GPT-SoVITS slim） | `artifacts/tts_slim/` | ~1.4 GB |
| 唱歌引擎 + Spica 歌声（RVC slim） | `artifacts/rvc_slim/` | ~620 MB |
| 语音识别等模型 | `spica_data/models/` | ~1.6 GB |
| TTS 参考音频 | `spica_data/voice/` | ~12 MB |
| Spica 立绘差分 | `spica_data/diffs/` | ~720 MB |

> 📦 **下载地址：** _（待填 —— 网盘 / Release 附件 / HuggingFace 链接）_

### 5. 外部程序（按需）

- **一起看番** 需要：[qBittorrent](https://www.qbittorrent.org/)（开启 Web UI）、[ffmpeg](https://ffmpeg.org/)、[VLC](https://www.videolan.org/)
- **麦克风阵列**（可选）：ReSpeaker（不接则用普通麦克风）

### 6. 配置密钥

在项目根目录建一个 `xiaosan.env`，按需填写：

```env
OPENAI_API_KEY=你的大模型 API key      # 必填：对话用
JUDGE_API_KEY=                          # 可选：不填则复用上面的 key
BILIBILI_COOKIE=                        # 可选：看番走 B 站下载时用
QBITTORRENT_PASSWORD=                   # 可选：看番走磁力下载时用
```

> `xiaosan.env` 已在 `.gitignore` 中，不会被提交。

### 7. 运行

```bash
python webui_qt.py
```

功能开关（是否启用看番 / 唱歌 / 屏幕识别、语音设备、GPU 等）在 `data/config/app.yaml` 里调。

---

## 🙏 致谢

Spica 站在这些优秀开源项目之上：

- [GPT-SoVITS](https://github.com/RVC-Boss/GPT-SoVITS) —— 语音合成
- [Applio / RVC](https://github.com/IAHispano/Applio) —— 歌声变声
- [faster-whisper](https://github.com/SYSTRAN/faster-whisper) —— 语音识别
- [RapidOCR](https://github.com/RapidAI/RapidOCR) —— 屏幕文字识别
- [Moondream](https://github.com/vikhyat/moondream) —— 视觉理解
- [yt-dlp](https://github.com/yt-dlp/yt-dlp) —— 视频下载

各组件版权归原作者所有，请遵守其各自的许可证。

---

## 📄 许可

_（License 待定 —— 请补充。注意上述引用组件各自的许可证条款。）_

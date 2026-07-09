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

### 4. 下载模型与立绘（不在仓库里）

模型权重和立绘差分体积较大，**不随代码仓库分发**，请单独下载：

| 内容 | 解压到 |
| --- | --- |
| 语音 / 识别模型包 | `spica_data/models/` |
| Spica 立绘差分包 | `spica_data/diffs/` |

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

**简体中文** · [English](README.en.md) · [日本語](README.ja.md)

# Spica Chatbot

**住在桌面上的语音角色伙伴。** 透明立绘、实时聊天、galgame 陪玩、一起看番和唱歌，也可以换成你自己的角色、声线与对话框。

[主页与演示](https://www.acgkasa.me/) · [配置与自定义教程](docs/README.md) · [角色资源包](https://pan.baidu.com/s/1RDoz_iRvFJdB71Aia3EPaw?pwd=j2u6)

## ✨ 功能

- **聊天与陪伴**：打字、麦克风对话、主动开口和角色记忆。
- **多角色**：角色卡、服装差分、独立声线打包导入，重启切换。
- **动态立绘**：眼部原画动画，支持视线跟随与眨眼。
- **自定义对话框**：独立样式包、配色、底图和动态句末标记。
- **桌面互动**：galgame 陪玩、屏幕理解、看番与点歌唱歌。

适用于 Windows / Linux，保留纯桌宠功能。语音与屏幕识别在本机运行；使用远端聊天模型时，参与对话的文本、角色卡及记忆会发给所配置的服务。

## 🚀 开始使用

准备 **Python 3.11、Git、Conda**；语音与重型功能建议使用 NVIDIA GPU。

```bash
git clone https://github.com/KASA2AI/Spica-Chatbot_Release.git
cd Spica-Chatbot_Release
conda create -n spica python=3.11 -y
conda activate spica
python -m pip install --upgrade pip
python -m pip install -r docs/requirements/requirements-windows-base.txt
```

Linux 的 Ubuntu / Debian 用户先安装 `build-essential python3-dev portaudio19-dev ffmpeg`，再装 Python 依赖。基础清单同样适用于 Linux。

首次使用按[启动配置](docs/README.md#setup)填写 API 与本机设置，先用小型示例跑通文字聊天；语音依赖和模型见[声音配置](docs/README.md#voice)。配置完成后，每次启动运行：

```bash
conda activate spica
python -X utf8 webui_qt.py
```

Windows 也可使用 `powershell -ExecutionPolicy Bypass -File .\scripts\windows\run_spica.ps1`；默认 Conda 环境为 `spica`。

## 📦 角色与对话框下载

[百度网盘角色资源包](https://pan.baidu.com/s/1RDoz_iRvFJdB71Aia3EPaw?pwd=j2u6) · 提取码：**`j2u6`**

下载后先解压。建议角色源包放在 `Desktop-Packs/Characters/`，样式放在 `Desktop-Packs/Dialogue-Styles/`。设置里分别导入含 `meta.json` 的角色文件夹、含 `style.json` 的样式文件夹，再点击 **重启程序**。

大模型权重和完整角色素材不随 Git 下载；角色包也不能替代共享语音引擎与识别模型。仓库自带可直接试用的[静态示例](Desktop-Packs/Characters/Examples/static/)和[动态示例](Desktop-Packs/Characters/Examples/eye-rig/)。

## 📖 文档

- [配置与自定义教程](docs/README.md)：从启动到角色卡、声音、动态立绘和对话框。
- [角色包字段参考](docs/CHARACTER_PACKS.md) · [对话框字段参考](docs/DIALOGUE_STYLES.md)
- [依赖清单](docs/requirements/)：基础、GPU、语音和可选功能分开存放。

## 许可

代码使用 [Source-Available 许可](LICENSE)。角色美术、声线及其他第三方素材遵循各自许可；分享自制包时请注明作者与来源。

感谢 [GPT-SoVITS](https://github.com/RVC-Boss/GPT-SoVITS)、[Applio](https://github.com/IAHispano/Applio)、[faster-whisper](https://github.com/SYSTRAN/faster-whisper)、[RapidOCR](https://github.com/RapidAI/RapidOCR)、[Moondream](https://github.com/vikhyat/moondream) 与 [yt-dlp](https://github.com/yt-dlp/yt-dlp)。

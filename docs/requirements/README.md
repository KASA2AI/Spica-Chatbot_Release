# Dependency lists / 依赖清单

安装命令从项目根目录执行，见[中文教程](../README.md#voice) / [English](../README.en.md#voice) / [日本語](../README.ja.md#voice)。按用途选择，**不要把所有清单装进同一个环境**。

| File | Purpose / 用途 |
| --- | --- |
| [requirements-windows-base.txt](requirements-windows-base.txt) | Desktop base, also used on Linux / 桌宠基础环境 |
| [requirements-windows-heavy.txt](requirements-windows-heavy.txt) | NVIDIA GPU runtime / GPU 运行依赖 |
| [requirements-windows-app.txt](requirements-windows-app.txt) | TTS and singing in the main environment / 主环境语音与唱歌 |
| [constraints-windows-app.txt](constraints-windows-app.txt) | Version constraints for the app list; use `-c` / 版本约束，不单独安装 |
| [requirements-stt.txt](requirements-stt.txt) | Optional STT dependency subset / 语音识别依赖子集 |
| [requirements-screen.txt](requirements-screen.txt) | Optional screen-recognition subset / 屏幕识别依赖子集 |
| [requirements-rvc.txt](requirements-rvc.txt) | Separate RVC worker environment only, NumPy 2.x / 仅限独立 RVC 环境 |

Full desktop speech setup uses **base → heavy → CUDA PyTorch → app with constraints**, followed by the separate `audio-separator --no-deps` command in the tutorial. Keep the main environment on NumPy 1.26.x.

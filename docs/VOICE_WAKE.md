# 角色名唤醒与语音输入

唤醒检测和句子识别是两件事。唤醒使用本地 sherpa-onnx 小模型，只检测你配置的称呼；
Qwen3-ASR 则在开始对话后转写句子。选择云端 ASR 不会把待机时的环境音持续上传。

## 第一次准备

1. 按 [安装与配置](README.md) 配好麦克风，以及本地 Qwen3-ASR 或百炼云端识别。
2. 在桌面应用的 Python 环境安装可选依赖：

   ```bash
   python -m pip install -r docs/requirements/requirements-wake.txt
   python -m nltk.downloader averaged_perceptron_tagger cmudict
   ```

3. 下载 [sherpa 官方中英文唤醒模型](https://github.com/k2-fsa/sherpa-onnx/releases/download/kws-models/sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20.tar.bz2)。
   解压到项目的 `models/wake-word/`，保留压缩包内的完整目录名。
   最终应存在 `models/wake-word/sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20/`，其中至少有：
   - `encoder-epoch-13-avg-2-chunk-16-left-64.int8.onnx`
   - `decoder-epoch-13-avg-2-chunk-16-left-64.onnx`
   - `joiner-epoch-13-avg-2-chunk-16-left-64.int8.onnx`
   - `tokens.txt`、`en.phone`
4. 打开设置的“角色与外观”，勾选“呼叫角色名字来唤醒”。可修改当前角色的中文或英文读法。
   每个角色分别保存唤醒词；运行时不自动下载模型。缺少模型、依赖或设备时会显示原因并停止监听。

## 平时怎样用

- 呼叫当前角色，等待一句回应结束，再讲话。回应成功后有约 8 秒接话时间。
- 在时间内开始讲话，可以把这句话说完；说完后的回答结束，再获得下一次接话时间。
- 没说话就自动收麦，返回仅监听角色名。说“结束对话”“先聊到这里”也会结束本次交流。
- 说“关闭麦克风”，或在设置中勾选“完全禁用麦克风”，会同时关闭录音和唤醒监听。
  取消禁麦设置，或明确点击麦克风按钮后才恢复。
- 唤醒词本身不是工具指令；唤醒回应走同一角色对话链，并禁止供给操作工具。
- 不播放日常语音时，回应文字完整显示且窗口可见后才开启接话。隐藏的文字不能视作已回应。
- 麦克风与播放采用半双工；不能承诺一边播放一边识别打断。

专名读音由 `hardware/audio_input/keyword_pronunciations.json` 提供补充，其他词使用中英文读音转换。
Sana 等少量既有读音带有原设备上校准的变体；这不代表其他房间与麦克风的真实命中率，近音词也可能误触发。
本轮独立版已做软件回归，仍需在实际设备上验证称呼、距离和音量。

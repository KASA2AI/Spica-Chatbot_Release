# 可选原生 Live2D 角色包

本桌面版支持 `pack_format: 3`，与静态和 eye-rig 包独立加载。默认角色不加载原生库。
需要 Python 3.11、可用的 OpenGL 驱动、桌面会话和自己的 Cubism 3 角色资源。
2026-10-09 已在 Windows / Python 3.11 / live2d-py 0.7.0.4 验证真实原生渲染、换装与 Q 版切换；Linux 原生 SDK 不属于这次 Windows 实机验收。

Windows x64 在现有桌面环境安装：

```powershell
python -m pip install --no-deps -r docs/requirements/requirements-cubism.txt
```

Linux x86_64 使用项目当前锁定的对应 wheel：

```bash
python -m pip install --no-deps PyOpenGL==3.1.10
python -m pip install --no-deps https://github.com/EasyLive2D/relive2d/releases/download/v0.7.0.4/live2d_py-0.7.0.4-cp311-cp311-manylinux_2_27_x86_64.manylinux_2_28_x86_64.whl
```

在设置中导入角色文件夹，再重启。缺少运行库或加载失败时保留备用图及错误提示，
聊天和设置仍可使用。导入不会执行模型或自动安装 SDK。

## 制作同类型角色

先按 [角色包教程](CHARACTER_PACKS.md) 准备角色卡和声音，再放入 Cubism 3 的 `.model3.json`、`.moc3` 及其全部引用。最小目录：

```text
my-character/
  meta.json
  persona.md
  visuals/previews/classic.png
  live2d/bindings.json
  live2d/director.json
  live2d/classic/Character.model3.json
  live2d/classic/Character.moc3
  live2d/classic/textures/texture_00.png
  live2d/classic/motions/idle.motion3.json
```

`meta.json` 示例（声音按原教程添加）：

```json
{
  "pack_format": 3, "slug": "my-character", "version": "1.0.0",
  "name": "我的角色", "char_name": "角色名",
  "visuals": {
    "renderer": "cubism", "cubism": "live2d/bindings.json",
    "sprites": {"classic": "visuals/previews/classic.png"},
    "default_costume": "classic",
    "costumes": [{"id": "classic", "label": "默认", "default_sprite": "classic"}]
  }
}
```

`live2d/bindings.json` 示例：

```json
{
  "format": "spica-cubism", "version": 1,
  "director": "live2d/director.json",
  "models": {
    "classic": {
      "model": "live2d/classic/Character.model3.json",
      "idle": "idle",
      "motions": {"idle": {"group": "Idle", "index": 0, "kind": "idle"}},
      "parameters": {
        "eyes": ["ParamEyeLOpen", "ParamEyeROpen"],
        "mouth": ["ParamMouthOpenY"],
        "gaze_x": [{"id": "ParamEyeBallX", "scale": 0.2}],
        "gaze_y": [{"id": "ParamEyeBallY", "scale": -0.15}]
      },
      "scale": 1.0, "offset": [0, 0], "physics": true
    }
  }
}
```

把参数 ID 换成**自己模型的真实 ID**。`group`、`index` 对应 `.model3.json` 的 `FileReferences.Motions` 分组和从零开始的位置，空分组写 `""`。每个服装 ID 必须有一个模型绑定。

常用项：

| 配置 | 用法 |
| --- | --- |
| `motions` | 稳定动作 ID → 分组、序号、`kind`、`cooldown` 秒数；待机 `idle` 循环，`gesture` / `skill` 单次播放 |
| `expressions` | 自定义表情 ID → 模型声明的 `.exp3.json` 表情名称；`neutral_expression` 是默认名称 |
| `idle_motions` | 表情 ID → `motions` 中的循环待机 ID；短动作结束后进入对应情绪待机，未配置时仍使用 `idle` |
| `effects` | 特效 ID → 包内参数动画 `.motion3.json`；保留时间曲线，最长 30 秒，末尾淡出 |
| `parameters.blink` | 默认 `auto`；动作自带眨眼时填 `motion`，避免重复眨眼 |
| `parameters.fixed` | 固定参数，例如造型部件开关；每帧最后应用，不受动作覆盖 |
| `physics` | 动作已包含头发、衣物等烘焙物理时设 `false`，避免双重运动 |
| `scale` / `offset` | 模型构图缩放、位置；通过预览检查帽子、头部与动作范围 |

所有路径使用包内相对路径和 `/`，禁止 `..`、符号链接、Windows 重名路径。模型清单中的纹理、表情、动作、物理等引用会一起校验、复制和导出，不需要手动列进 `meta.json`。数据包不执行 Python，也不携带 DLL 或 Cubism Core；运行库由应用环境安装。

相同服装的变体可以在 `bindings.json` 顶层分组，例如 `"costume_groups": {"法袍": ["classic", "hatless"]}`。成员使用原有服装 ID，各造型保留自己的模型和动作；组内显示名使用 `meta.json` 的服装 `label`，保存的选择仍是原 ID。不要直接混用未经验证的不同模型动作。

## 写导演规则

不需要 LLM 或脚本。先写空规则验证待机，再逐条加动作：

```json
{
  "format": "spica-cubism-director", "version": 1, "hold_seconds": 0.8,
  "rules": [{
    "id": "thanks", "keywords": ["谢谢", "ありがとう", "thank you"],
    "exclude": [], "signals": {"thanks": 3}, "threshold": 3,
    "expression": "smile", "motions": ["happy", "happy-wave"], "priority": 1
  }]
}
```

对应模型的 `expressions.smile` 和候选动作必须指向实际资源；缺少的候选自动跳过，不会切到其他服装。`motion` 仍支持单个动作，`motions` 可以列多个候选，在真正播放时轮换并检查冷却；同一技能意图的变体共用冷却，同角色换装也不会重置技能冷却。每个关键词计 4 分，`signals` 用已有文字分析结果乘权重；达到 `threshold` 后先比较 `priority`，再比较分数。没有命中时保留当前情绪待机。`exclude` 命中会跳过该规则，施法等强动作应加入中日英的否定、引用和讨论词。

`kind: "skill"` 还要求关键词出现在未被否定、未嵌入转述引号的台词中，单靠情绪分数不会触发。整句外包的台词引号可以保留；具体角色的讨论用语仍用 `exclude` 补充。

导演在后台为每句话准备指令；画面只执行正在呈现的句子。相同意图连续出现不会机械重播，动作冷却在真正开始表演时计算，停止和换装会清理待执行指令。口型跟随当前 QMediaPlayer 的实际播放位置；无声、暂停、停止时闭嘴，唱歌只使用管线提供的人声轨。

声明 `tts.inference_profile: "megumin_d"` 的 D 语音方案会等待完整回复、一次合成并连续播放，字幕和导演仍按句切换。切换点根据音频停顿和句子长度估算，并非逐字强制对齐；不会切割或重新合成语音。关闭语音时仍按原有方式分句显示，语音失败时也会继续显示剩余字幕。

正常说完会保留情绪，让短动作自然结束后进入对应待机。导演顶层的 `idle_return_seconds` 控制空闲多久再淡回默认，默认 20 秒；新台词会取消这个计时。停止会立即清除口型、特效和待执行动作，保留当前表情。待机应使用轻微循环的运动，表情渐变通过 `.exp3.json` 的 `FadeInTime` / `FadeOutTime` 调整。

## 生成备用图并分享

将模型和绑定准备好后，在项目根目录执行：

```powershell
python -m ui.cubism_preview "Desktop-Packs/Characters/my-character" --output "Desktop-Packs/Characters/my-character/visuals/previews" --check-actions
```

工具使用桌面同一个渲染器，逐套保存 `<服装ID>.png`，并可试启动所有绑定动作。把 `meta.json` 中的备用图路径对应到这些文件。先导入、重启试用，再从设置导出分享包或压缩整个角色文件夹；分享前保留素材来源说明，个人记忆使用单独的个人存档导出。

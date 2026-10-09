# 制作桌宠角色包与眼部动态包

角色包是一个可搬走的文件夹。程序从 `meta.json` 找到角色卡、立绘与声音，复制到本机安装目录，再在重启时载入。格式与桌面版 Spica 的 `pack_format: 1/2` 兼容。

本仓库只运行本地桌宠。没有 Mobile、机器人、Hub 或运动控制入口。旧包中的 `scenes`、背景和设置页 CG 可以保留并随导出携带，桌面透明覆盖层不使用这些端侧背景。

## 先试用，再替换自己的素材

1. 运行 `python webui_qt.py`，打开齿轮设置。
2. 点击 **导入角色文件夹**，选择 [静态样例](../Desktop-Packs/Characters/Examples/static) 或 [眼部动态样例](../Desktop-Packs/Characters/Examples/eye-rig) 对应的文件夹。这两份样例自带小型示意图，不需要角色语音模型。
3. 等待导入完成，点击设置底部的 **重启程序**。重启后角色卡、立绘和输入框称呼一起切换。
4. 复制一份样例到 `Desktop-Packs/Characters/my-character/`，修改名称、角色卡与素材，再导入该目录。已有角色包也可以从任意本地目录导入。

选择的是**含 `meta.json` 的目录**，不是 JSON 文件、上一级 `Desktop-Packs/Characters`、语音模型文件或 ZIP。收到 ZIP 时先解压。导入后可移走原目录；不要删除 `data/runtime/characters/` 中仍在使用的安装副本。

展开角色下拉列表，点击已导入项右侧的 **×** → **确定移除**，即可从列表移除该版本。源包、安装资源和私人记忆保留；以后手动导入原文件夹即可恢复。移除当前选择后，当前运行不受影响，重启回到内置 Spica；其他角色和对话框样式的选择保持不变。内置默认角色没有移除按钮。

## 静态角色：最小结构

```text
my-character/
  meta.json
  persona.md
  visuals/
    idle.png
    happy.png
```

`meta.json`：

```json
{
  "pack_format": 1,
  "slug": "my-character",
  "version": "1.0.0",
  "name": "我的角色",
  "char_name": "小星",
  "visuals": {
    "default_costume": "casual",
    "sprites": {
      "idle": "visuals/idle.png",
      "happy": "visuals/happy.png"
    },
    "costumes": [{
      "id": "casual",
      "label": "私服",
      "default_sprite": "idle",
      "emotions": {"happy": ["happy"]}
    }]
  }
}
```

`persona.md` 用 UTF-8 保存，例如：

```markdown
你是 {{char}}，正在桌面上陪 {{user}} 聊天。
性格温和、好奇，回答简洁自然。不要把自己描述成 Spica。
```

也可以使用 `SKILL.md`、`self.md`、`persona.md` 的组合，至少保留其中一个角色卡文件。这里的角色卡只提供人设文本，不执行脚本。

| 字段 | 用法 |
| --- | --- |
| `slug` | 稳定角色身份。升级和静态／动态版本若需延续记忆，保持相同 ID；独立角色使用新 ID。 |
| `version` | 作者填写的展示版本。实际安装版本由声明文件的内容摘要确定。 |
| `name` / `char_name` | 设置列表名称 / 对话显示及角色卡中的称呼。 |
| `user_aliases` | 可选旧主人公称呼列表，如 `["旧名字"]`；载入时替换成用户当前设置的称呼。 |
| `worldbook_file` | 可选包内世界观文本路径，例如 `worldbook.md`。 |
| `visuals.sprites` | 立绘 ID → 包内图片路径。无需使用 Spica 的原文件名。 |
| `visuals.costumes` | 服装列表，每套设置自己的默认立绘和情绪映射。 |

情绪映射支持 `happy`、`angry`、`sad`、`surprised`。每项可写多个立绘 ID，缺省使用该服装默认图。同一景别的 PNG 应保持画布与角色位置一致；静态图导入时等比缩放、居中靠下放入 1024×1024 透明画布。不同景别可以做成不同服装。

添加第二套服装时，在 `sprites` 中声明新图片，在 `costumes` 中追加一个不同 `id` 的对象。切换服装立即生效并保存；当前回复和播放尚未结束时会暂时禁用切换，以免后续分句恢复旧服装。

细分表情沿用桌面差分导演：声明 `visuals.rules_file`，规则文件包含 `hand_poses` 与 `expressions`，再在服装中填写 `expression_sprites`，例如 `{"normal": {"000": "idle"}, "index_finger": {"004": "explain"}}`。支持 `normal/arms_crossed/index_finger` 三种手势；表情 ID 为三位数字，并且必须同时存在于规则和图片清单中。可从现有完整角色包复制规则后按自己的素材调整。

## Live2D 包：当前支持的是眼部原画动画

本版所称的动态包使用 **`spica-eye-rig` 局部原画动画**，支持鼠标视线跟随、平滑眨眼、静态差分切换和回复结束后恢复动态默认图。没有 Cubism 渲染器，不能直接播放 `.model3.json` / `.moc3`；修改扩展名也不能转换模型。

动态样例的完整结构：

```text
my-character-live2d/
  meta.json
  persona.md
  model/
    character.eyerig.json
    open.png
    closed.png
```

角色身份、角色卡、声音与记忆字段和静态包相同。把 `pack_format` 改为 `2`，`visuals` 改为：

```json
{
  "renderer": "eye-rig",
  "default_costume": "casual",
  "sprites": {"idle": "model/open.png"},
  "eye_rigs": {"idle": "model/character.eyerig.json"},
  "costumes": [{"id": "casual", "label": "私服 · 动态", "default_sprite": "idle"}]
}
```

准备两张**同尺寸、同姿势、同画布**的完整 RGBA 原图：`open.png` 睁眼，`closed.png` 闭眼。只改变眼部，不改变人物轮廓与透明区域。眼部标定矩形在两张图中都必须完全不透明。不要把闭眼图裁成单独的眼睛贴片。

从样例的 [character.eyerig.json](../Desktop-Packs/Characters/Examples/eye-rig/model/character.eyerig.json) 修改标定。其坐标以完整原图左上角为 `(0, 0)`，x 向右、y 向下，单位为原图像素；不是屏幕坐标、百分比或裁剪后的坐标。

| 字段 | 含义与限制 |
| --- | --- |
| `format` / `version` | 固定为 `"spica-eye-rig"` / `1`。 |
| `canvas.width/height` | 两张原图的实际尺寸，均为 1～4096。 |
| `textures.open/closed` | 相对眼部 JSON 所在目录的路径。睁眼图必须和该立绘在 `sprites` 中引用的文件一致。 |
| `gaze.origin` | 视线中心 `[x, y]`，通常在两眼之间。 |
| `gaze.maximum_offset` | 最大跟随幅度 `[dx, dy]`；分别不超过 32、16 像素，并留足眼白空间。 |
| `gaze.strength` / `smoothing_ms` | 跟随强度 0～1 / 平滑时间 20～1000 ms；建议先用样例值再小幅调整。 |
| `blink.duration_ms` | 一次眨眼的持续时间，80～2000 ms。 |
| `blink.initial_delay_ms` | 第一次眨眼前的等待，0～60000 ms。 |
| `blink.interval_ms` | 两次眨眼之间的随机间隔范围，两个整数均在 1000～60000 ms。 |
| `eyes` | 1～2 个眼睛标定对象。 |
| `eyes[].bounds` | `[x, y, width, height]`，覆盖整个眼部及睫毛，宽高不超过 512，不能越出原图。 |
| `eyes[].iris` | 虹膜左右边界 **`[left_x, right_x]`**，不是 `[x, y]`。 |
| `eyes[].contour` | 从左到右的 `[x, upper_y, lower_y, lash_y]` 采样点，表示上眼睑、下眼睑、睫毛上沿。至少 3 点，满足 `lash_y ≤ upper_y ≤ lower_y`。 |
| `eyes[].closedLine` | 闭眼曲线的 `[x, y]` 采样点，至少 2 点，左右必须覆盖睁眼轮廓。 |

两组曲线的 x 每步至少递增 0.5 像素，所有点都在对应 `bounds` 中；虹膜左右还需为 `maximum_offset[0]` 留出空间。新画师需要按自己的图重新标定，不能直接套用 Spica 的坐标。

多服装动态包可以给每个默认立绘各写一个 rig，通过 `eye_rigs` 映射关联；没有 rig 的表情图仍作为静态差分显示。待本轮回复结束或中断收尾完成，桌面恢复当前服装的动态默认图。窗口隐藏时动画暂停，显示后恢复；DPI、透明点击区域和缩放沿用桌宠显示逻辑。

导入会校验 JSON、路径、尺寸和眼部不透明区域。错误不会改变当前选择。更新纹理或标定后重新导入并重启；导出的分享包会自动包含 rig 引用的两张纹理。

## 加入角色声音

静态与动态样例省略 `tts`，所以使用文字模式。加入自己的 GPT-SoVITS 声线时，在 `meta.json` 顶层追加：

```json
{
  "tts": {
    "engine": "gptsovits",
    "model_version": "v2ProPlus",
    "gpt": "voice/gpt.ckpt",
    "sovits": "voice/sovits.pth",
    "reference": {
      "audio": "voice/reference.wav",
      "text": "与参考音频逐字对应的原文",
      "language": "日文"
    },
    "target_language": "日文",
    "parameters": {"top_k": 5, "top_p": 1.0, "temperature": 1.0, "speed": 1.0}
  }
}
```

把对应权重和参考音频放入 `voice/`。支持现有 `v2Pro/v2ProPlus` 模型；本机需安装对应 GPT-SoVITS 引擎和共享基础权重。导入只验证文件、后缀、非空与音频可读性，不会加载权重执行推理；模型兼容性需要实际发声验收。

`tts.emotions` 可以按四情绪提供不同的 `{audio, text, language}`，缺省复用主参考；每个参考项还可写 `additional_audio: ["voice/refs/01.wav"]`。语言字段支持 `日文/中文/英文`，但只控制合成器；聊天输出语言仍由应用对话规则管理，不能仅修改此字段就假定输出语言随之改变。

可选 RVC 歌声字段：

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

省略 `tts` 使用文字模式，省略 `rvc` 关闭该角色的唱歌能力。角色包不能打开本机已关闭的 TTS 或唱歌开关。推理引擎、STT 模型、Python 路径、设备选择和 API 密钥属于本机环境，不放进角色包。

## 分享、个人存档与升级

导入并重启到目标角色后，在设置中选择导出方式，并指定一个**尚不存在的新文件夹名称**：

- **导出分享包**：角色卡、清单内的图片、rig、声音及作者资料，排除程序生成的个人存档。作者亲自写进角色卡的内容仍会保留。
- **导出个人存档**：额外包含该角色的长期记忆、近期普通聊天、陪玩共同记忆与当前服装，适合自己迁移到另一台机器。

要发送 ZIP，可以压缩导出的文件夹；接收者解压后在设置中导入。不要直接压缩整个项目或 `data/runtime/`。对话框样式是单独选择、单独导入的包，见 [对话框制作说明](DIALOGUE_STYLES.md)。

导入同一 `slug` 的新版不会覆盖本机已有记忆。个人存档只在该角色尚无本机存档时初始化；升级会保留仍有效的服装选择。已安装版本存于 `data/runtime/characters/<slug>/<revision>/`，角色状态存于 `data/runtime/character_states/<slug>/`。旧内置 Spica 的模型、角色卡和两个 SQLite 数据库继续沿用 `spica_data/` 位置。

称呼、角色选择、对话框样式各自保存，重启生效。聊天中点击重启会中断当前播放并等待后台安全退出，按钮显示等待状态，完成后自动重启。

## 文件约束与排错

- 所有引用使用包内相对路径和 `/`。禁止绝对路径、`..`、符号链接、Windows 保留设备名、仅大小写不同的重名路径。
- 角色、立绘等 ID 使用 ASCII 字母、数字、`_`、`-`、`.`，首字符为字母或数字；版本更新时 ID 的大小写保持一致。
- 只有清单引用的文件及标准角色卡、`README.md`、`LICENSE`、`sources.tsv` 会随包复制。引用新文件时也要更新清单。
- 出现“眼部动画原图尺寸与标定不一致”：检查两张完整原图尺寸和 `canvas`；出现“眼部动画区域必须不透明”：检查两张图中完整 `bounds` 的 alpha。
- 旧包有自定义脚本、Cubism 模型或外部绝对路径时，需要按本文转换为受支持的声明式资源；角色卡不会安装程序插件或更改工具权限。
- 称呼保存提示被 `SPICA_USER_NAME` 覆盖时，从自己的启动环境或 dotenv 中移除旧设置，重新启动后再保存；落盘失败不会假报成功。

完整角色清单约束见 [character-pack.schema.json](character-pack.schema.json)，眼部几何约束见 [eye_rig.py](../spica/core/eye_rig.py)。发布自制包时，在 `LICENSE` 或 `sources.tsv` 写明素材作者、来源与许可；本项目代码许可不替代素材原作者的许可。


## Q 版桌宠与本机通知

完整界面顶部点击“收起为桌宠”；悬浮小人上停留会显示说话、摸头和展开按钮。
单击播放角色交互，左右连续摇晃触发眩晕，三击请求唤醒；松手后恢复。
桌宠共用当前对话和麦克风许可，完全禁麦时三击不会打开麦克风。
“小／中／大”按所在屏幕高度的10%／15%／20%适配。

默认 Spica 自带既有生成素材（约51 MiB）。其他角色不会借用 Spica 素材；未配套时
显示该角色静态立绘。角色自己的 `meta.json` 在 `visuals` 下声明：

```json
{"floating": "floating/animation.json"}
```

动画文件是 WebP/GIF，`animation.json` 最少声明 `canvas`、`fps`、`poster` 和
`states.idle`；每项写 `file`、实际编码帧数 `frames`、实际总时长 `duration_ms`。
完整示例见 `spica_data/Spica_skill/floating/animation.json`；所有引用必须在包内。
桌宠配置、动画和静态姿势随角色包一起导入/导出，路径逃逸、帧数或时长不符会拒绝安装。
内置旧格式角色卡通过 `floating_config_path` 引用同一数据格式；新包使用 `visuals.floating`。

`idle_actions` 是可选加权随机动作；默认未与 Spica 交互且未对话时每30–45秒选择一次。
不以是否正在操作其他应用判断角色空闲；说话、拖动、菜单或正在呈现回复会中止动作。

本机任务结束后可调用当前安装目录中的脚本：

```bash
python scripts/notify_desktop.py --title "任务完成" --message "相关测试已通过。"
```

该接口仅发送文字，不调用模型、不播放声音、不接受命令。Spica 未运行会返回
`unavailable`，不会启动应用。`queued` 表示已进入本机显示队列，不表示本人看见。
通知被设置遮挡、电脑无新输入时保留；展示后有新输入，再留8秒阅读时间。
可通过“看完了”提前收起。队列有界、仅当前进程有效，不提供跨端或永久待处理中心。

可选原生 Live2D 的安装和制作见 [Cubism 角色包](CUBISM_PACKS.md)。

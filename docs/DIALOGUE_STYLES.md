# 制作桌面对话框样式包

样式只改变桌宠对话框、角色名栏、输入区颜色及句末动画，按钮功能与设置排版保持一致。角色、声音和私人记忆由角色包独立管理。

在设置中点击 **导入样式文件夹**，选择含 `style.json` 的目录；导入完成后点击 **重启程序**。下拉框可以切回内置 Spica 外观。导入错误保留当前选择；安装副本损坏时可重新导入完好的原包修复。

点击样式下拉列表中对应行右侧的 **×** → **确定移除**，可移除已导入项。源包和安装资源保留，以后手动导入原文件夹即可恢复。移除当前选择后，重启恢复内置 Spica 对话框；角色选择与记忆保持不变。内置默认样式不能移除。

```text
my-style/
  style.json
  images/
    surface.png
    nameplate.png
    tail.png
```

最小 `style.json`：

```json
{
  "format": "spica-dialogue-style",
  "format_version": 1,
  "style_id": "my-style",
  "name": "我的对话框",
  "version": "1.0.0",
  "author": "作者名",
  "images": {
    "surface": "images/surface.png",
    "tail": "images/tail.png"
  },
  "colors": {"accent": "#E94E8B"},
  "tail": {
    "placement": "corner",
    "columns": 5,
    "rows": 4,
    "frames": 20,
    "frame_ms": 50,
    "width": 28,
    "height": 28
  }
}
```

`surface` 为透明对话底图，`nameplate` 是可选角色名底图，`tail` 为透明动画图集。上面的示例需要一张 5 列 × 4 行的雪花图集；如果只有静帧，使用 `columns: 1`、`rows: 1`、`frames: 1`。图片尺寸必须整除网格尺寸，动画按从左到右、从上到下顺序播放，`frames` 不超过格子总数。句末原作空白帧可以保留。

`placement: "inline"` 将尾标放在文字末尾，`"corner"` 放在对话区右下角。`frame_ms` 为每帧毫秒数；打字完成后出现动画，并保留可见的句末停顿。

可配置 `colors`（文字、姓名、描边、按钮强调色等）、`text`（字号、行高、描边宽度）、`layout`（左右边距、姓名栏、底图透明度）。完整字段和范围见 [dialogue-style.schema.json](dialogue-style.schema.json)，实际包可参考 [`Desktop-Packs/Dialogue-Styles/spica`](../Desktop-Packs/Dialogue-Styles/spica) 和 [`Desktop-Packs/Dialogue-Styles/sana`](../Desktop-Packs/Dialogue-Styles/sana)。

“Character Name”“Mashiroiro Symphony SANA”等原作装饰字在底图中，实际角色名仍由程序绘制；自制包可以把自己的装饰画入图片。布局值控制对话区域内部，不能新增脚本、控制按钮行为或开启远端接口。

所有图片仅允许 `images/` 下声明的 PNG，相对路径不允许外跳或符号链接。Sana 示例取自用户本地提供的游戏素材；自制包请替换为自己可使用的素材并标明来源。

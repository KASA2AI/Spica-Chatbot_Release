# 桌宠资源包

Windows 和 Chatbot 使用相同的角色包与对话框样式格式。

```text
Desktop-Packs/
  Characters/          # 角色卡、立绘、眼部动画、角色语音模型
    Examples/          # 可直接导入的静态与眼部动画示例
  Dialogue-Styles/     # 对话框底图、姓名栏、配色、动态尾标
```

- **导入角色**：设置 → 导入角色文件夹，选择 `Characters/<角色>/` 中包含 `meta.json` 的那一级。
- **导入样式**：设置 → 对话框样式 → 导入样式文件夹，选择 `Dialogue-Styles/<样式>/` 中包含 `style.json` 的那一级。
- 导入完成后点击 **重启程序**；角色与样式可分别选择。

拿到资源 ZIP 后解压到本目录；ZIP 内的 `Characters/` 和 `Dialogue-Styles/` 与上述目录对应。不要直接选择 ZIP 或总目录进行导入。
完整角色源包和上传用 ZIP 留在本机；私人聊天记忆仍保存在程序的 `data/runtime/` 中。

制作教程：[角色与眼部动态包](../docs/CHARACTER_PACKS.md) · [对话框样式包](../docs/DIALOGUE_STYLES.md)。

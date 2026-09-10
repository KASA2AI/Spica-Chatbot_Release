"""Local desktop character and dialogue-style settings."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QObject, Slot
from PySide6.QtWidgets import QFileDialog, QMessageBox

from spica.core.character_memory import export_character_save
from spica.conversation.character_loader import normalize_interlocutor_name
from spica.host.character_packages import export_character_folder
from ui.workers.character_package_worker import CharacterPackageWorker


class CharacterSettingsController(QObject):
    def __init__(self, window, panel):
        super().__init__(window)
        self.window = window
        self.panel = panel
        self.worker = None
        self._style_operation = False
        panel.character_import_requested.connect(self.import_folder)
        panel.character_remove_requested.connect(self.remove_character)
        panel.dialogue_style_import_requested.connect(self.import_style_folder)
        panel.dialogue_style_remove_requested.connect(self.remove_style)
        panel.dialogue_style_changed.connect(self.select_style)
        panel.character_changed.connect(self.select)
        panel.character_export_requested.connect(self.export_folder)
        panel.interlocutor_name_changed.connect(self.save_interlocutor_name)
        self.refresh()

    def save_interlocutor_name(self, name) -> bool:
        if self.panel.settings_busy:
            self.panel.interlocutor_name_status.setText("设置正在保存，请完成后再修改称呼。")
            return False
        surface = getattr(self.window.host, "management_surface", None)
        if surface is None:
            return False
        try:
            surface.write_config({
                "character": {"interlocutor_name": normalize_interlocutor_name(name)},
            })
        except (OSError, ValueError) as exc:
            self.panel.interlocutor_name_status.setText(f"称呼保存失败：{exc}")
            return False
        self.refresh()
        return True

    def refresh(self):
        surface = getattr(self.window.host, "management_surface", None)
        if surface is None:
            self.panel.set_character_busy(True)
            self.panel.character_status.setText("角色管理暂不可用。")
            return
        config = surface.read_config()
        character = config["character"]
        selected_style = config.get("dialogue_style", {}).get("package_dir")
        self.panel.set_dialogue_styles(surface.list_dialogue_styles(), selected_style)
        if getattr(self.window.host, "dialogue_style_error", None):
            self.panel.dialogue_style_status.setText("启动时未能加载样式，暂用 Spica 对话框。请重新导入样式后重启。")
        selected = character["package_dir"]
        self.panel.set_characters(surface.list_characters(), selected)
        name = normalize_interlocutor_name(character["interlocutor_name"])
        self.panel.set_interlocutor_name(name)
        self.panel.interlocutor_name_status.setText(
            f"已保存「{name}」，重启桌宠后生效。"
            if name != self.window.interlocutor_name
            else "修改称呼后需重启桌宠。"
        )

    def import_folder(self):
        # Native Windows file dialogs can block Qt timers. Keep dialogue and
        # eye animation running while the user browses character resources.
        source = QFileDialog.getExistingDirectory(
            self.window, "导入角色文件夹",
            str(Path(__file__).resolve().parents[2] / "Desktop-Packs" / "Characters"),
            QFileDialog.Option.ShowDirsOnly | QFileDialog.Option.DontUseNativeDialog,
        )
        if source:
            surface = self.window.host.management_surface
            self._start(lambda: surface.import_character(source), "正在导入角色，请等待完成提示后再重启。大型角色包可能需要几分钟…")

    def select(self, path: str):
        surface = self.window.host.management_surface
        self._start(lambda: surface.select_character(path), "正在选择角色…")

    def import_style_folder(self):
        source = QFileDialog.getExistingDirectory(
            self.window, "导入对话框样式文件夹（包含 style.json）",
            str(Path(__file__).resolve().parents[2] / "Desktop-Packs" / "Dialogue-Styles"),
            QFileDialog.Option.ShowDirsOnly | QFileDialog.Option.DontUseNativeDialog,
        )
        if source:
            self._start(lambda: self.window.host.management_surface.import_dialogue_style(source),
                        "正在导入对话框样式…", style=True)

    def select_style(self, path):
        self._start(lambda: self.window.host.management_surface.select_dialogue_style(path),
                    "正在保存对话框样式…", style=True)

    def remove_character(self, path: str):
        self._confirm_removal(path)

    def remove_style(self, path: str):
        self._confirm_removal(path, style=True)

    def _confirm_removal(self, path: str, *, style: bool = False):
        if self.worker is not None:
            return
        combo = self.panel.dialogue_style_box if style else self.panel.character_box
        index = combo.findData(path)
        if index < 0:
            return
        dialog = QMessageBox(self.window)
        dialog.setWindowTitle("确定移除")
        dialog.setText(f"确定从列表移除「{combo.itemText(index)}」？")
        dialog.setInformativeText("保留原始包和记忆，以后可重新导入。\n移除当前选择后，重启恢复内置 Spica。")
        confirm = dialog.addButton("确定移除", QMessageBox.ButtonRole.AcceptRole)
        cancel = dialog.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        dialog.setDefaultButton(cancel)
        dialog.exec()
        accepted = dialog.clickedButton() is confirm
        dialog.deleteLater()
        if accepted:
            surface = self.window.host.management_surface
            operation = surface.remove_dialogue_style if style else surface.remove_character
            self._start(lambda: operation(path), "正在从列表移除…", style=style)

    def export_folder(self, personal: bool):
        host = self.window.host
        package = host.character_package if host else None
        if package is None or package.manifest is None:
            self.panel.character_status.setText("请选择已导入的角色并重启后导出。")
            return
        destination, _ = QFileDialog.getSaveFileName(
            self.window, "导出到新文件夹", package.character_id,
            options=QFileDialog.Option.DontUseNativeDialog,
        )
        if not destination:
            return
        selected_costume = self.window.selected_costume

        def operation():
            memory = (
                export_character_save(
                    package, host.services, selected_costume=selected_costume
                )
                if personal
                else None
            )
            export_character_folder(package, Path(destination), memory=memory)
            return {
                "message": "个人存档已导出。"
                if personal
                else "分享包已导出，不含私人记忆。"
            }

        self._start(operation, "正在导出角色…")

    def _start(self, operation, message, *, style=False):
        if self.panel.settings_busy:
            return
        self.panel.set_character_busy(True)
        self._style_operation = style
        status = self.panel.dialogue_style_status if style else self.panel.character_status
        status.setText(message)
        self.worker = CharacterPackageWorker(operation, self)
        self.worker.completed.connect(self._complete)
        self.worker.start()

    @Slot(object)
    def _complete(self, event):
        self.worker.deleteLater()
        self.worker = None
        self.panel.set_character_busy(False)
        result = event.data
        status = self.panel.dialogue_style_status if self._style_operation else self.panel.character_status
        status.setText(
            (
                result.get("message")
                or f"已选择 {result.get('name', '')}，重启桌宠后生效。"
            )
            if result["ok"]
            else f"操作未完成：{result['error']}"
        )
        self.refresh()

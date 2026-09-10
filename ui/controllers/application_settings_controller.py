"""Wire the desktop application page to the existing management surface."""

from __future__ import annotations

from PySide6.QtCore import QObject, QUrl, Slot
from PySide6.QtGui import QDesktopServices

from ui.workers.character_package_worker import CharacterPackageWorker


class ApplicationSettingsController(QObject):
    def __init__(self, window, panel):
        super().__init__(window)
        self.window = window
        self.panel = panel
        self.page = panel.application_page
        self.worker = None
        self._operation = ""
        self._restart_after_save = False
        self.page.save_requested.connect(self.save)
        self.page.test_requested.connect(self.test_connection)
        self.page.secret_save_requested.connect(self.save_secret)
        self.page.open_config_requested.connect(self.open_config_directory)
        self.refresh()

    @property
    def surface(self):
        return self.window.host.management_surface

    def refresh(self):
        # Reopening settings preserves drafts. Disk changes refresh on save;
        # package writes cannot own any of the application page's fields.
        if self.worker is not None or self.page.patch() or self.page.has_unsaved_secret():
            return
        try:
            self.page.load_snapshot(self.surface.read_application_settings())
        except Exception:
            self.page.status.setText("无法读取应用设置，请检查本机配置文件后重试。")
            self.page.save_button.setEnabled(False)

    def _start(self, operation, kind, message):
        if self.panel.settings_busy:
            return
        self._operation = kind
        self.panel.set_application_busy(True)
        self.page.status.setText(message)

        def safe_operation():
            try:
                return operation()
            except ValueError as exc:
                return {"ok": False, "error": str(exc)}
            except Exception:
                return {"ok": False, "error": "操作未完成，请检查配置文件和目录是否可读写后重试。"}

        self.worker = CharacterPackageWorker(safe_operation, self)
        self.worker.completed.connect(self._complete)
        self.worker.start()

    def save(self):
        patch = self.page.patch()
        if not patch:
            self.page.status.setText("应用设置没有未保存的修改。")
            return
        self._start(lambda: {"snapshot": self.surface.write_application_settings(patch)}, "save", "正在保存应用设置…")

    def save_secret(self, slot, value):
        if not value.strip():
            self.page.status.setText("请输入新密钥；留空会保留现有密钥。")
            return
        self._secret_slot = slot
        self._start(lambda: {"snapshot": self.surface.write_application_secret(slot, value)}, "secret", "正在保存密钥…")

    def test_connection(self):
        llm = self.page.values()["llm"]
        key = self.page.api_key.text()
        self._start(lambda: self.surface.test_application_connection(
            base_url=llm["base_url"], model=llm["model"], api_key=key,
        ), "test", "正在测试连接…")

    @Slot(object)
    def _complete(self, event):
        self.worker.deleteLater()
        self.worker = None
        result = event.data
        self.panel.set_application_busy(False)
        if result["ok"]:
            if self._operation == "secret":
                # Saving a credential must not discard unsaved form edits.
                draft = self.page.patch()
                editor = self.page.api_key
                editor.clear()
                self.page.load_snapshot(result["snapshot"])
                for section, fields in draft.items():
                    for key, value in fields.items():
                        self.page._set_value(self.page.fields[f"{section}.{key}"], value)
                self.page._update_stt_controls()
                message = "密钥已保存，重启后生效。" + ("表单修改仍需保存。" if draft else "")
            elif self._operation == "save":
                self.page.load_snapshot(result["snapshot"])
                message = "应用设置已保存，点击下方“重启程序”后生效。"
            else:
                message = result["message"]
            self.page.status.setText(message)
        else:
            self.page.status.setText(result["error"])
        restart = self._restart_after_save and result["ok"]
        self._restart_after_save = False
        if restart:
            self.window.restart_application()

    def prepare_restart(self):
        if self.worker is not None:
            return False
        if self.page.has_unsaved_secret():
            self.panel.tabs.setCurrentWidget(self.page)
            self.page.status.setText("有尚未保存的密钥，请先点击“保存密钥”，或清空输入以保留原值。")
            return False
        if self.page.patch():
            self._restart_after_save = True
            self.save()
            return False
        return True

    def open_config_directory(self):
        if self.page._snapshot is not None:
            if not QDesktopServices.openUrl(QUrl.fromLocalFile(self.page._snapshot["config_directory"])):
                self.page.status.setText("未能打开配置文件夹，请检查系统文件管理器。")

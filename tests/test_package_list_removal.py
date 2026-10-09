from __future__ import annotations

from support.filesystem import symlink_or_skip
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from spica.config.manager import ConfigManager
from spica.host.management import ManagementSurface
from test_character_folder_import import folder  # noqa: F401
from test_dialogue_styles import style_folder  # noqa: F401


@pytest.fixture(params=["character", "dialogue_style"])
def packages(request, tmp_path, monkeypatch):
    monkeypatch.setattr(ConfigManager, "_ensure_env_loaded", lambda self: None)
    monkeypatch.delenv("SPICA_USER_NAME", raising=False)
    kind = request.param
    source = request.getfixturevalue("folder" if kind == "character" else "style_folder")
    builtin = tmp_path / "builtin"
    builtin.mkdir()
    (builtin / "meta.json").write_text('{"slug":"spica","name":"Spica"}')
    surface = ManagementSurface(
        registry=None, plugin_host=None, characters_root=tmp_path / "runtime",
        config_manager=ConfigManager(tmp_path / "app.yaml"), builtin_character_dir=builtin,
    )
    install = getattr(surface, f"import_{kind}")
    install(source)
    path = surface.read_config()[kind]["package_dir"]
    return SimpleNamespace(
        kind=kind, source=source, surface=surface, path=path,
        install=install, remove=getattr(surface, f"remove_{kind}"),
        select=getattr(surface, f"select_{kind}"),
        listing=getattr(surface, f"list_{kind}s"),
        builtin=str(builtin) if kind == "character" else None,
    )


def test_remove_selected_keeps_assets_memory_and_can_be_imported_again(packages, tmp_path):
    p = packages
    surface = p.surface
    memory = tmp_path / "runtime/character_states/sana/recent.json"
    memory.parent.mkdir(parents=True)
    memory.write_text('{"memory":"keep me"}')
    source_file = p.source / ("sana.png" if p.kind == "character" else "images/tail.png")
    installed_file = Path(p.path) / source_file.relative_to(p.source)
    image = source_file.read_bytes()
    surface.write_config({"character": {"interlocutor_name": "伞"}})
    other = "dialogue_style" if p.kind == "character" else "character"
    other_settings = surface.read_config()[other]

    result = p.remove(p.path)
    assert result["restart_required"]
    assert surface.read_config()[p.kind]["package_dir"] is None
    assert surface.read_config()[other] == other_settings
    assert not any(item["dir"] == p.path for item in p.listing())
    assert source_file.read_bytes() == installed_file.read_bytes() == image
    assert memory.read_text() == '{"memory":"keep me"}'
    restarted = ManagementSurface(
        registry=None, plugin_host=None, characters_root=surface.characters_root,
        config_manager=ConfigManager(surface.config_manager.config_path),
        builtin_character_dir=surface.builtin_character_dir,
    )
    assert not any(item["dir"] == p.path for item in getattr(restarted, f"list_{p.kind}s")())
    with pytest.raises(ValueError, match="请先导入"):
        p.select(p.path)

    p.install(p.source)
    p.install(p.source)
    assert surface.read_config()[p.kind]["package_dir"] == p.path
    assert sum(item["dir"] == p.path for item in p.listing()) == 1
    assert memory.read_text() == '{"memory":"keep me"}'


def test_remove_other_version_preserves_selection_and_settings(packages):
    p = packages
    manifest = p.source / ("meta.json" if p.kind == "character" else "style.json")
    value = json.loads(manifest.read_text())
    value["version"] = "2.0"
    manifest.write_text(json.dumps(value))
    p.install(p.source)
    before = p.surface.read_config()
    assert before[p.kind]["package_dir"] != p.path
    assert not p.remove(p.path)["restart_required"]
    assert p.surface.read_config() == before
    assert len(p.listing()) == 2  # builtin + the second version


def test_remove_rejects_builtin_source_and_symlink_escape(packages, tmp_path):
    p = packages
    before = p.surface.read_config()
    for path in (p.builtin, p.source):
        with pytest.raises(ValueError):
            p.remove(path)
    installed = Path(p.path)
    outside = tmp_path / "outside" / installed.name
    outside.parent.mkdir()
    installed.rename(outside)
    symlink_or_skip(installed, outside, target_is_directory=True)
    with pytest.raises(ValueError):
        p.remove(installed)
    assert p.surface.read_config() == before
    assert list(outside.parent.iterdir()) == [outside]


def test_failed_config_save_rolls_back_removal_and_failed_reimport_stays_removed(packages, monkeypatch):
    p = packages
    before = p.surface.read_config()

    def fail_save(*_):
        raise OSError("save failed")

    with monkeypatch.context() as patch:
        patch.setattr(p.surface.config_manager, "save", fail_save)
        with pytest.raises(OSError, match="save failed"):
            p.remove(p.path)
    assert p.surface.read_config() == before
    assert any(item["dir"] == p.path for item in p.listing())
    p.remove(p.path)
    with monkeypatch.context() as patch:
        patch.setattr(p.surface.config_manager, "save", fail_save)
        with pytest.raises(OSError, match="save failed"):
            p.install(p.source)
    assert p.surface.read_config()[p.kind]["package_dir"] is None
    assert not any(item["dir"] == p.path for item in p.listing())


def test_popup_cross_confirms_without_selecting_and_manual_import_restores(packages, monkeypatch):
    pytest.importorskip("PySide6")
    import time
    from PySide6.QtCore import QTimer, Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox, QWidget
    from ui.controllers.character_settings_controller import CharacterSettingsController
    from ui.widgets.package_combo_box import REMOVABLE_ROLE
    from ui.widgets.settings_panel import SettingsPanel

    p = packages
    p.select(p.builtin)
    app = QApplication.instance() or QApplication([])
    window = QWidget()
    window.resize(500, 900)
    window.host = SimpleNamespace(management_surface=p.surface)
    window.interlocutor_name = "麦"
    panel = SettingsPanel(window)
    panel.resize(480, 880)
    controller = CharacterSettingsController(window, panel)
    combo = panel.character_box if p.kind == "character" else panel.dialogue_style_box
    activations, confirmations = [], []
    combo.activated.connect(activations.append)
    answer = "取消"
    original_exec = QMessageBox.exec

    def confirm(dialog):
        confirmations.append(dialog.text())
        button = next(button for button in dialog.buttons() if button.text() == answer)
        QTimer.singleShot(0, button.click)
        return original_exec(dialog)

    monkeypatch.setattr(QMessageBox, "exec", confirm)

    def settle():
        app.processEvents()
        deadline = time.monotonic() + 5
        while controller.worker is not None and time.monotonic() < deadline:
            app.processEvents()
            # Let the Python file-copy worker acquire the GIL on Windows too.
            time.sleep(0.01)
        assert controller.worker is None

    def click_row(row, *, cross):
        panel.scroll_area.ensureWidgetVisible(combo)
        combo.showPopup()
        app.processEvents()
        rect = combo.view().visualRect(combo.model().index(row, 0))
        pos = rect.center()
        pos.setX(rect.right() - 12 if cross else rect.left() + 20)
        QTest.mouseClick(combo.view().viewport(), Qt.MouseButton.LeftButton, pos=pos)
        settle()

    try:
        window.show()
        app.processEvents()
        assert not combo.itemData(0, REMOVABLE_ROLE)
        assert combo.itemData(combo.findData(p.path), REMOVABLE_ROLE)
        before = p.surface.read_config()
        click_row(combo.findData(p.path), cross=True)
        assert len(confirmations) == 1
        assert not activations
        assert p.surface.read_config() == before
        assert combo.currentData() == p.builtin

        answer = "确定移除"
        click_row(combo.findData(p.path), cross=True)
        assert len(confirmations) == 2
        assert not activations
        assert combo.count() == 1 and combo.currentData() == p.builtin
        assert p.surface.read_config() == before

        monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *_: str(p.source))
        getattr(panel, f"{p.kind}_import_requested").emit()
        settle()
        assert combo.count() == 2 and combo.currentData() == p.path
        click_row(0, cross=False)
        assert activations == [0]
        assert len(confirmations) == 2
        assert p.surface.read_config()[p.kind]["package_dir"] == p.builtin
    finally:
        if controller.worker is not None:
            settle()
        window.close()
        window.deleteLater()
        app.processEvents()

from __future__ import annotations

from support.filesystem import symlink_or_skip
import asyncio
import json
from pathlib import Path
import shutil
import ssl

from PIL import Image
import pytest

from spica.config.manager import ConfigManager
from spica.host.dialogue_styles import import_dialogue_style, load_dialogue_style, selected_dialogue_style
from spica.host.management import ManagementSurface


@pytest.fixture
def style_folder(tmp_path):
    source = tmp_path / "source"
    (source / "images").mkdir(parents=True)
    Image.new("RGBA", (900, 220), (250, 220, 240, 90)).save(source / "images/surface.png")
    Image.new("RGBA", (40, 20), (255, 100, 160, 200)).save(source / "images/tail.png")
    (source / "style.json").write_text(json.dumps({
        "style_id": "test", "name": "Test style", "images": {
            "surface": "images/surface.png", "tail": "images/tail.png"},
        "tail": {"columns": 2, "rows": 1, "frames": 2},
    }))
    return source


def management(tmp_path):
    return ManagementSurface(registry=None, plugin_host=None,
        config_manager=ConfigManager(config_path=tmp_path / "app.yaml"), characters_root=tmp_path / "runtime")


def test_concurrent_settings_updates_keep_both_changes(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor, TimeoutError
    import threading

    monkeypatch.setattr(ConfigManager, "_ensure_env_loaded", lambda self: None)
    monkeypatch.delenv("SPICA_USER_NAME", raising=False)
    surface = management(tmp_path)
    surface.write_config({"character": {"interlocutor_name": "Old"}})
    blocked, release = threading.Event(), threading.Event()
    save = surface.config_manager.save

    def paused_save(config, *args, **kwargs):
        if threading.current_thread().name.endswith("_0"):
            blocked.set()
            assert release.wait(3)
        return save(config, *args, **kwargs)

    monkeypatch.setattr(surface.config_manager, "save", paused_save)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(surface.write_config, {"dialogue_style": {"package_dir": "new-style"}})
        try:
            assert blocked.wait(2)
            second = pool.submit(surface.write_config, {"character": {"interlocutor_name": "New"}})
            try:
                second.result(timeout=0.2)
            except TimeoutError:
                pass
        finally:
            release.set()
        first.result(); second.result()
    result = surface.read_config()
    assert result["character"]["interlocutor_name"] == "New"
    assert result["dialogue_style"]["package_dir"] == "new-style"


def test_failed_settings_replace_preserves_previous_config(tmp_path, monkeypatch):
    surface = management(tmp_path)
    surface.write_config({"dialogue_style": {"package_dir": "old-style"}})
    path = surface.config_manager.config_path
    before = path.read_bytes()

    def fail_replace(*_):
        raise OSError("configuration replacement failed")

    monkeypatch.setattr(Path, "replace", fail_replace)
    with pytest.raises(OSError, match="configuration replacement failed"):
        surface.write_config({"dialogue_style": {"package_dir": "new-style"}})
    assert path.read_bytes() == before
    assert not list(path.parent.glob(".app-*"))


@pytest.mark.parametrize("damaged", ["images/tail.png", "style.json"])
def test_reimport_repairs_a_damaged_installed_style(style_folder, tmp_path, damaged):
    surface = management(tmp_path)
    surface.import_dialogue_style(style_folder)
    selected = surface.read_config()["dialogue_style"]["package_dir"]
    (Path(selected) / damaged).write_bytes(b"broken")
    surface.import_dialogue_style(style_folder)
    loaded = selected_dialogue_style(selected, tmp_path / "runtime/dialogue_styles")
    assert loaded.revision == load_dialogue_style(style_folder).revision
    assert surface.read_config()["dialogue_style"]["package_dir"] == selected


def test_failed_style_repair_restores_installation_and_selection(style_folder, tmp_path, monkeypatch):
    surface = management(tmp_path)
    surface.import_dialogue_style(style_folder)
    before = surface.read_config()
    selected = Path(before["dialogue_style"]["package_dir"])
    tail = selected / "images/tail.png"
    tail.write_bytes(b"damaged")
    rename = Path.rename

    def fail_install(path, destination):
        if path.name == "package" and Path(destination) == selected:
            raise OSError("replacement failed")
        return rename(path, destination)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "rename", fail_install)
        with pytest.raises(OSError, match="replacement failed"):
            surface.import_dialogue_style(style_folder)
    assert surface.read_config() == before
    assert tail.read_bytes() == b"damaged"
    surface.import_dialogue_style(style_folder)
    assert load_dialogue_style(selected).revision == load_dialogue_style(style_folder).revision


def test_import_is_independent_restart_selection_and_copies_only_artwork(style_folder, tmp_path):
    surface = management(tmp_path)
    surface.write_config({"character": {"interlocutor_name": "麦"}})
    before = surface.read_config()
    (style_folder / "secret.txt").write_text("private")
    result = surface.import_dialogue_style(style_folder)
    after = surface.read_config()
    assert result["restart_required"]
    assert before["dialogue_style"]["package_dir"] is None
    assert before["character"] == after["character"]
    root = Path(after["dialogue_style"]["package_dir"])
    assert not (root / "secret.txt").exists()
    assert surface.import_dialogue_style(style_folder) == result
    shutil.rmtree(style_folder)
    loaded = selected_dialogue_style(str(root), tmp_path / "runtime/dialogue_styles")
    assert loaded.style.name == "Test style"
    surface.select_dialogue_style(None)
    assert surface.read_config()["dialogue_style"]["package_dir"] is None
    # Saving a new selection cannot mutate the running startup snapshot.
    assert loaded.style.name == "Test style"


@pytest.mark.parametrize("failure", ["escape", "symlink", "corrupt", "grid", "script", "pixel_limit"])
def test_invalid_style_never_changes_selection(style_folder, tmp_path, failure, monkeypatch):
    surface = management(tmp_path)
    value = json.loads((style_folder / "style.json").read_text())
    if failure == "escape":
        value["images"]["tail"] = "../outside.png"
    elif failure == "symlink":
        image = style_folder / "images/tail.png"
        image.rename(tmp_path / "outside.png")
        symlink_or_skip(image, tmp_path / "outside.png")
    elif failure == "corrupt":
        (style_folder / "images/tail.png").write_bytes(b"not png")
    elif failure == "grid":
        value["tail"]["columns"] = 3
    elif failure == "pixel_limit":
        monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 10)
    else:
        value["script"] = "alert(1)"
    (style_folder / "style.json").write_text(json.dumps(value))
    with pytest.raises((OSError, ValueError)):
        surface.import_dialogue_style(style_folder)
    assert surface.read_config()["dialogue_style"]["package_dir"] is None
    assert surface.list_dialogue_styles() == [{"name": "Spica · 原有对话框", "dir": None}]


def test_styled_widgets_keep_tail_completion_and_input_hit_targets(style_folder):
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication, QWidget
    from ui.widgets.dialogue_box import TintedDialogueBox
    from ui.widgets.dialogue_style_art import DialogueStyleArt
    from ui.widgets.input_panel import InputPanel

    app = QApplication.instance() or QApplication([])
    parent = QWidget(); parent.resize(900, 220)
    box = TintedDialogueBox(parent); box.setGeometry(0, 0, 900, 153)
    footer = InputPanel(parent); footer.setGeometry(0, 153, 900, 67)
    # A hollow community frame still needs working text and input hit targets.
    Image.new("RGBA", (900, 220), (0, 0, 0, 0)).save(style_folder / "images/surface.png")
    loaded = load_dialogue_style(style_folder)
    art = DialogueStyleArt(loaded.root, loaded.style)
    box.set_style(art); footer.set_style(art)
    parent.show(); app.processEvents()
    for sentence in ("第一句。", "第二句也要显示结束标记。"):
        box.set_dialogue_text(sentence); box.set_typing_active(True)
        assert not box.tail.isVisible()
        box.show_tail(); app.processEvents()
        assert box.tail.isVisible() and box.tail.timer.isActive()
        assert len(box.tail._frames) == 2
    assert footer.hit_region().contains(footer.input.geometry().center())
    assert box.hit_region().contains(box.text_label.geometry().center())
    parent.close(); app.processEvents()
    assert not box.tail.timer.isActive()


def test_settings_import_persists_selection_without_changing_running_widgets(style_folder, tmp_path, monkeypatch):
    pytest.importorskip("PySide6")
    import time
    from types import SimpleNamespace
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication, QFileDialog, QWidget
    from ui.controllers.character_settings_controller import CharacterSettingsController
    from ui.widgets.dialogue_box import TintedDialogueBox
    from ui.widgets.settings_panel import SettingsPanel

    app = QApplication.instance() or QApplication([])
    window = QWidget()
    surface = management(tmp_path)
    window.host = SimpleNamespace(management_surface=surface, dialogue_style=None)
    window.interlocutor_name = "麦"
    window.dialogue = TintedDialogueBox(window)
    panel = SettingsPanel(window)
    controller = CharacterSettingsController(window, panel)
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *_: str(style_folder))
    panel.dialogue_style_import_requested.emit()
    deadline = time.monotonic() + 5
    while controller.worker is not None and time.monotonic() < deadline:
        app.processEvents(); QTest.qWait(10)
    assert controller.worker is None
    selected = surface.read_config()["dialogue_style"]["package_dir"]
    assert selected is not None and panel.dialogue_style_box.currentData() == selected
    assert "重启" in panel.dialogue_style_status.text()
    assert window.dialogue.style_art is None
    assert selected_dialogue_style(selected, tmp_path / "runtime/dialogue_styles").style.style_id == "test"
    window.close(); app.processEvents()

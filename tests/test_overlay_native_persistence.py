"""User preferences must survive a restart on the actual host platform."""

import json

from ui.overlay_config import (
    load_dialogue_box_visible,
    load_dialogue_opacity,
    save_dialogue_box_visible,
    save_dialogue_opacity,
)


def test_overlay_preferences_persist_and_preserve_unrelated_fields(tmp_path):
    path = tmp_path / "overlay.json"
    path.write_text(json.dumps({"user_note": "保留我的设置"}), encoding="utf-8")
    backup_root = tmp_path / "backups"

    assert save_dialogue_opacity(0.65, path, backup_root=backup_root)
    assert save_dialogue_box_visible(False, path, backup_root=backup_root)
    assert load_dialogue_opacity(path) == 0.65
    assert load_dialogue_box_visible(path) is False
    assert json.loads(path.read_text(encoding="utf-8"))["user_note"] == "保留我的设置"

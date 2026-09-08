"""Portable eye animation resources and their native desktop display."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageDraw
import pytest

from agent_tools.visual.diff_service import VisualDiffService
from spica.host.character_packages import (
    export_character_folder, import_character_folder, prepare_character_package,
)


@pytest.fixture
def eye_folder(tmp_path):
    folder = tmp_path / "角色包 眼部动画"
    model = folder / "model"
    model.mkdir(parents=True)
    opened = Image.new("RGBA", (200, 200))
    draw = ImageDraw.Draw(opened)
    draw.rectangle((40, 20, 160, 190), fill=(220, 190, 160, 255))
    closed = opened.copy()
    draw.polygon([(62, 80), (70, 70), (100, 70), (130, 70), (138, 80), (130, 95), (100, 96), (70, 95)], fill="white")
    draw.rectangle((86, 70, 110, 95), fill="red")
    ImageDraw.Draw(closed).line([(62, 85), (100, 88), (138, 85)], fill="black", width=3)
    opened.save(model / "open.png")
    closed.save(model / "closed.png")
    rig = {
        "format": "spica-eye-rig", "version": 1,
        "canvas": {"width": 200, "height": 200},
        "textures": {"open": "open.png", "closed": "closed.png"},
        "gaze": {"origin": [100, 82], "maximum_offset": [6, 3], "strength": 1},
        "eyes": [{"bounds": [60, 60, 80, 50], "iris": [86, 110],
                  "closedLine": [[62, 85], [100, 88], [138, 85]],
                  "contour": [[62, 80, 84, 78], [70, 70, 95, 66], [100, 70, 96, 66],
                              [130, 70, 95, 66], [138, 80, 84, 78]]}],
    }
    (model / "eye.json").write_text(json.dumps(rig))
    (folder / "persona.md").write_text("Test character", encoding="utf-8")
    meta = {
        "pack_format": 2, "slug": "eyes", "version": "1", "name": "Eyes", "char_name": "Eyes",
        "visuals": {"renderer": "eye-rig", "sprites": {"idle": "model/open.png"},
                    "eye_rigs": {"idle": "model/eye.json"}, "default_costume": "casual",
                    "costumes": [{"id": "casual", "label": "Casual", "default_sprite": "idle"}]},
    }
    (folder / "meta.json").write_text(json.dumps(meta))
    return folder


def test_eye_package_survives_source_removal_and_export(eye_folder, tmp_path):
    package = prepare_character_package(
        import_character_folder(eye_folder, tmp_path / "installed"), data_root=tmp_path / "state"
    )
    shutil.rmtree(eye_folder)
    visual = VisualDiffService(package.visual_config_path)
    assert visual.current_default_sprite_id() == "idle"
    sprite = visual.resolve_expression_image("casual", "normal", "000")
    rig_path = Path(visual.config["eye_rigs"][str(sprite)])
    assert rig_path.is_file() and (rig_path.parent / "closed.png").is_file()
    with Image.open(sprite) as image:
        assert image.size == (200, 200)  # Native coordinate system on desktop.
    public = Path(package.package_root) / ".presentation"
    with Image.open(public / "sprites/idle.png") as image:
        assert image.size == (1024, 1024)
    export_character_folder(package, tmp_path / "export")
    reimported = import_character_folder(tmp_path / "export", tmp_path / "another-install")
    assert reimported.manifest == package.manifest
    assert (Path(reimported.package_root) / "model/closed.png").read_bytes() == (rig_path.parent / "closed.png").read_bytes()


@pytest.mark.parametrize("failure", ["escape", "missing", "geometry", "size", "transparent", "old_format"])
def test_invalid_eye_package_is_rejected_before_installation(eye_folder, tmp_path, failure):
    path = eye_folder / "model/eye.json"
    rig = json.loads(path.read_text())
    if failure == "escape":
        rig["textures"]["closed"] = "../../outside.png"
    elif failure == "missing":
        rig["textures"]["closed"] = "absent.png"
    elif failure == "geometry":
        rig["eyes"][0]["contour"][1][0] = 62
    elif failure == "size":
        Image.new("RGBA", (100, 100), "white").save(eye_folder / "model/closed.png")
    elif failure == "transparent":
        Image.new("RGBA", (200, 200)).save(eye_folder / "model/closed.png")
    else:
        meta_path = eye_folder / "meta.json"
        meta = json.loads(meta_path.read_text()); meta["pack_format"] = 1
        meta_path.write_text(json.dumps(meta))
    path.write_text(json.dumps(rig))
    with pytest.raises(ValueError):
        import_character_folder(eye_folder, tmp_path / "installed")
    assert not list((tmp_path / "installed").glob("*/*/meta.json"))


def test_calibration_changes_package_revision(eye_folder, tmp_path):
    first = import_character_folder(eye_folder, tmp_path / "installed")
    path = eye_folder / "model/eye.json"
    rig = json.loads(path.read_text()); rig["gaze"]["strength"] = .3
    path.write_text(json.dumps(rig))
    second = import_character_folder(eye_folder, tmp_path / "installed")
    assert first.revision != second.revision


def test_desktop_loads_eye_animation_and_keeps_its_hit_pixmap(eye_folder, tmp_path, isolated_runtime_config):
    pytest.importorskip("PySide6")
    from PySide6.QtCore import QEvent
    from PySide6.QtGui import QImage
    from PySide6.QtWidgets import QApplication
    from ui.qt_overlay import OverlayWindow
    from ui.widgets.character_sprite import PresenceFrame, REST_FRAME

    app = QApplication.instance() or QApplication([])
    package = prepare_character_package(
        import_character_folder(eye_folder, tmp_path / "installed"), data_root=tmp_path / "state"
    )
    with patch.object(OverlayWindow, "_init_backend", lambda self: None):
        window = OverlayWindow()
    try:
        window.visual_tool = VisualDiffService(package.visual_config_path)
        window._load_default_character()
        window.show(); app.processEvents()
        view = window.character_label
        motion = view._eye_motion
        assert motion is not None and view._eye_timer.isActive()
        view._eye_timer.stop()
        original_key = view.pixmap().cacheKey()

        def pixels():
            image = view.grab().toImage().convertToFormat(QImage.Format.Format_RGBA8888)
            return bytes(image.constBits())

        motion.gaze[:] = [-1, 0]; left = pixels()
        motion.gaze[:] = [1, 0]; right = pixels()
        assert left != right
        motion.openness = 0; closed = pixels()
        motion.openness = .5; half = pixels()
        assert closed != half and closed != right
        assert view.pixmap().cacheKey() == original_key
        # A Windows monitor/DPI change need not resize the logical window.
        # Refresh raster density while preserving the current eye animation.
        original_geometry = view.geometry()
        next_dpr = window.devicePixelRatioF() + .5
        with patch.object(window, "devicePixelRatioF", return_value=next_dpr), patch.object(
            view, "set_sprite", wraps=view.set_sprite,
        ) as refresh:
            app.sendEvent(window, QEvent(QEvent.Type.DevicePixelRatioChange))
            # QLabel.pixmap() adapts to the real test monitor's DPR; inspect
            # the new raster passed to it for the simulated monitor instead.
            assert refresh.call_args.args[0].devicePixelRatioF() == next_dpr
            assert view.geometry() == original_geometry
            assert view._eye_motion is motion
            assert motion.gaze == [1, 0] and motion.openness == .5
        # Resizing keeps the calibration bound to the same original and crop.
        window.resize(window.width() + 80, window.height() + 40)
        assert view._eye_motion is motion
        view.set_presence_frame(PresenceFrame(sprite_alpha=0))
        assert not view._eye_timer.isActive()
        view.set_presence_frame(REST_FRAME)
        assert view._eye_timer.isActive()
        view.hide(); assert not view._eye_timer.isActive()
        view.show(); assert view._eye_timer.isActive()
        # A static/direct pixmap cancels the old eye animation entirely.
        view.setPixmap(view.pixmap())
        assert view._eye_motion is None and not view._eye_timer.isActive()
    finally:
        window.close(); window.deleteLater(); app.processEvents()


@pytest.mark.parametrize("ending", ["complete", "stop", "error", "replacement", "static_costume"])
def test_eye_animation_resumes_after_directed_reply(eye_folder, tmp_path, isolated_runtime_config, ending):
    pytest.importorskip("PySide6")
    from types import SimpleNamespace
    from PySide6.QtWidgets import QApplication
    from ui.models.stream import StreamKind
    from ui.models.stream_unit import StreamUnitState
    from ui.qt_overlay import OverlayWindow

    meta_path = eye_folder / "meta.json"
    meta = json.loads(meta_path.read_text())
    meta["visuals"]["sprites"]["reply"] = "model/closed.png"
    meta["visuals"]["costumes"][0]["emotions"] = {"angry": ["reply"]}
    if ending == "static_costume":
        meta["visuals"]["sprites"]["static_idle"] = "model/open.png"
        meta["visuals"]["costumes"].append({
            "id": "static", "label": "Static", "default_sprite": "static_idle",
            "emotions": {"angry": ["reply"]},
        })
        meta["visuals"]["default_costume"] = "static"
    meta_path.write_text(json.dumps(meta))
    package = prepare_character_package(
        import_character_folder(eye_folder, tmp_path / "installed"), data_root=tmp_path / "state"
    )
    app = QApplication.instance() or QApplication([])
    with patch.object(OverlayWindow, "_init_backend", lambda self: None):
        window = OverlayWindow()
    try:
        window.visual_tool = VisualDiffService(package.visual_config_path)
        window._load_default_character()
        window.agent = SimpleNamespace()
        window._init_chat_stream_controller()
        controller = window.chat_stream_controller
        controller.typewriter_controller = SimpleNamespace(start=lambda *a, **kw: None, stop=lambda: None)
        window.show(); app.processEvents()
        view = window.character_label
        animated = ending != "static_costume"
        assert (view._eye_motion is not None) == animated
        assert view._eye_timer.isActive() == animated
        reply = window.visual_tool.resolve_expression_image(window.selected_costume, "normal", "013")
        controller.streaming_mode = True
        controller.stream_done = True
        controller.stream_pending_units = {
            index: StreamUnitState(
                index=index, display_text=text, cue={"image_path": str(reply)},
                text_ready=True, audio_ready=True, visual_ready=True,
            )
            for index, text in enumerate(("第一句。", "第二句。"))
        }
        controller._pump_stream_playback()
        app.processEvents()  # Sprite application is deliberately deferred past audio start.
        assert Path(view._identity).stem == "reply"
        assert view._eye_motion is None
        controller._mark_text_finished()
        app.processEvents()
        assert controller.current_unit.index == 1
        assert Path(view._identity).stem == "reply"  # Keep the director's image between segments.
        if ending == "stop":
            window._on_stop_requested()
        elif ending == "error":
            controller._handle_stream_error("test error", StreamKind.CHAT)
        elif ending == "replacement":
            with patch.object(window, "set_character_image", wraps=window.set_character_image) as changes:
                controller.stop_current()
                controller._reset_playback_state(streaming=True)
                controller.stream_done = True
                controller.stream_pending_units = {0: StreamUnitState(
                    index=0, display_text="新的回复。", cue={"image_path": str(reply)},
                    text_ready=True, audio_ready=True, visual_ready=True,
                )}
                controller._pump_stream_playback()
                app.processEvents()
                app.processEvents()
                assert [Path(call.args[0]).stem for call in changes.call_args_list] == ["reply"]
            controller._mark_text_finished()
        else:
            controller._mark_text_finished()
        app.processEvents()
        assert not controller.streaming_mode and not controller.playback_active
        app.processEvents()  # Run the idle restore queued by the terminal callback.
        view.finish_sprite_transition()
        if animated:
            assert view._eye_motion is not None, "director reply left the character permanently static"
            assert view._eye_timer.isActive()
            assert Path(view._identity).stem == "idle"
        else:
            assert view._eye_motion is None and not view._eye_timer.isActive()
            assert Path(view._identity).stem == "reply"  # Static costumes keep their final expression.
    finally:
        window.close(); window.deleteLater(); app.processEvents()

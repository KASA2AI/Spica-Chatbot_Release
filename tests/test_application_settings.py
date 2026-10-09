"""Application settings exercise the real config owner and Qt signal boundary."""

from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import yaml

from spica.config.manager import ConfigManager
from spica.config.secrets import load_secrets
from spica.host.management import ManagementSurface


@pytest.fixture
def settings(tmp_path, monkeypatch):
    repo = tmp_path / "desktop"
    repo.mkdir()
    env = repo / "xiaosan.env"
    env.write_text("# local credentials\nOPENAI_API_KEY='old-local-key'\nUNRELATED='keep me'\n", encoding="utf-8")
    config = repo / "app.yaml"
    config.write_text(yaml.safe_dump({
        "character": {"package_dir": "packages/megumin", "interlocutor_name": "伞"},
        "dialogue_style": {"package_dir": "styles/sana"},
        "llm": {"model": "old-model"}, "song": {"enabled": True},
    }), encoding="utf-8")
    monkeypatch.setattr(ConfigManager, "_ensure_env_loaded", staticmethod(lambda: None))
    from spica.config.env_roster import consumed_env_names
    for name in consumed_env_names():
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr("agent_tools.function_tools.song.config.DEFAULT_CONFIG_PATH", repo / "absent-song.json")
    monkeypatch.setattr("agent_tools.function_tools.screen.config.DEFAULT_CONFIG_PATH", repo / "absent-screen.json")

    def environment(inherited=None):
        return load_secrets(with_environment_snapshot=True, inherited_environment=inherited or {},
                            repo_env_path=env, parent_env_path=tmp_path / "parent.env", prime_process=False)

    owner = environment()
    surface = ManagementSurface(registry=Mock(), plugin_host=Mock(), config_manager=ConfigManager(config),
                                characters_root=repo / "characters", secrets_provider=lambda: owner)
    return SimpleNamespace(surface=surface, owner=owner, environment=environment, config=config, env=env)


def test_application_save_preserves_pack_selection_and_does_not_freeze_process_overrides(settings, monkeypatch):
    surface = settings.surface
    surface.write_application_settings({"llm": {"model": "new-model"}, "tts": {"enabled": False}})
    # A name edit still consults the running environment, but must not copy it to YAML.
    monkeypatch.setenv("MODEL", "stale-running-model")
    surface.write_config({"character": {"interlocutor_name": "新称呼"}})
    saved = yaml.safe_load(settings.config.read_text())
    assert saved["llm"]["model"] == "new-model"
    assert saved["character"]["package_dir"] == "packages/megumin"
    assert saved["dialogue_style"]["package_dir"] == "styles/sana"
    assert saved["tts"]["enabled"] is False
    assert saved["character"]["interlocutor_name"] == "新称呼"
    assert "old-local-key" not in json.dumps(surface.read_application_settings())
    before = settings.config.read_bytes()
    with pytest.raises(ValueError, match="角色包"):
        surface.write_application_settings({"character": {"package_dir": "wrong"}})
    assert settings.config.read_bytes() == before


def test_environment_and_legacy_overrides_cannot_report_false_save_success(settings, monkeypatch):
    settings.env.write_text(settings.env.read_text() + "MODEL=env-model\n", encoding="utf-8")
    state = settings.surface.read_application_settings()
    assert state["values"]["llm"]["model"] == "env-model"
    assert state["overrides"]["llm.model"] == "MODEL"
    before = settings.config.read_bytes()
    with pytest.raises(ValueError, match="MODEL"):
        settings.surface.write_application_settings({"llm": {"model": "new-model"}})
    assert settings.config.read_bytes() == before
    legacy = settings.config.parent / "song_config.json"
    legacy.write_text('{"enabled": false}', encoding="utf-8")
    monkeypatch.setattr("agent_tools.function_tools.song.config.DEFAULT_CONFIG_PATH", legacy)
    assert settings.surface.read_application_settings()["values"]["song"]["enabled"] is False
    with pytest.raises(ValueError, match="song_config.json"):
        settings.surface.write_application_settings({"song": {"enabled": True}})


def test_secret_save_and_restart_reloads_dotenv_without_stale_process_values(settings, monkeypatch):
    settings.env.write_text(settings.env.read_text() + "OPENAI_BASE_URL=https://old.example/v1\n", encoding="utf-8")
    owner = settings.environment({"PATH": "/test/bin", "UNCHANGED": "value"})
    monkeypatch.setenv("OPENAI_API_KEY", "old-local-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://old.example/v1")
    new_key = "new-key-with-'quote-and-\\slash"
    owner.write_local_secret("openai_api_key", new_key)
    assert owner.secrets.openai_api_key == "old-local-key"
    assert owner.refresh().secrets.openai_api_key == new_key
    assert "UNRELATED='keep me'" in settings.env.read_text()
    assert settings.env.read_text().count("OPENAI_API_KEY=") == 1
    settings.env.write_text(settings.env.read_text().replace("https://old.example/v1", "https://new.example/v1"), encoding="utf-8")
    restart_env = owner.restart_environment()
    assert restart_env == {"PATH": "/test/bin", "UNCHANGED": "value"}
    restarted = settings.environment(restart_env)
    assert restarted.secrets.openai_api_key == new_key
    assert restarted.environment_snapshot.get("OPENAI_BASE_URL") == "https://new.example/v1"


@pytest.mark.parametrize("value", ["bad\nMODEL=injected", "${UNRELATED}"])
def test_secret_invalid_roundtrip_keeps_original_file(settings, value):
    before = settings.env.read_bytes()
    with pytest.raises(ValueError):
        settings.owner.write_local_secret("openai_api_key", value)
    assert settings.env.read_bytes() == before


def test_inherited_secret_override_and_failed_publication_preserve_existing_secret(settings, monkeypatch):
    before = settings.env.read_bytes()
    owner = settings.environment({"OPENAI_API_KEY": "inherited-key"})
    with pytest.raises(ValueError, match="启动环境"):
        owner.write_local_secret("openai_api_key", "new-key")
    assert settings.env.read_bytes() == before
    monkeypatch.setattr(Path, "replace", Mock(side_effect=OSError("write denied")))
    with pytest.raises(OSError):
        settings.owner.write_local_secret("openai_api_key", "new-key")
    assert settings.env.read_bytes() == before
    assert not list(settings.env.parent.glob(".secret-*"))


def test_connection_check_does_not_chat_save_or_disclose_provider_errors(settings, monkeypatch):
    client = Mock()
    client.models.list.return_value = SimpleNamespace(data=[SimpleNamespace(id="selected-model")])
    constructor = Mock()
    constructor.return_value.__enter__ = Mock(return_value=client)
    constructor.return_value.__exit__ = Mock(return_value=False)
    monkeypatch.setattr("openai.OpenAI", constructor)
    before = settings.config.read_bytes(), settings.env.read_bytes()
    result = settings.surface.test_application_connection(base_url="https://example.invalid/v1", model="selected-model")
    assert "连接成功" in result["message"]
    assert constructor.call_args.kwargs["timeout"] == 8.0
    assert constructor.call_args.kwargs["max_retries"] == 0
    client.chat.completions.create.assert_not_called()
    client.responses.create.assert_not_called()
    client.models.list.side_effect = RuntimeError("provider echoed old-local-key")
    with pytest.raises(ValueError) as raised:
        settings.surface.test_application_connection(base_url=None, model="selected-model")
    assert "old-local-key" not in str(raised.value)
    assert (settings.config.read_bytes(), settings.env.read_bytes()) == before


@pytest.fixture
def qapp():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def _wait_for_controller(qapp, controller):
    from PySide6.QtTest import QTest
    deadline = time.monotonic() + 3
    while controller.worker is not None and time.monotonic() < deadline:
        qapp.processEvents()
        QTest.qWait(5)
    assert controller.worker is None


def test_tabs_keep_appearance_controls_and_application_defaults_do_not_touch_disk(settings, qapp):
    from ui.widgets.settings_panel import SettingsPanel
    panel = SettingsPanel()
    page = panel.application_page
    page.load_snapshot(settings.surface.read_application_settings())
    assert [panel.tabs.tabText(i) for i in range(panel.tabs.count())] == ["角色与外观", "应用设置", "记忆", "Home 状态"]
    assert page.patch() == {}  # Localized language labels must still serialize to language codes.
    selected = []
    panel.voice_volume_changed.connect(selected.append)
    panel.voice_volume_slider.setValue(53)
    assert selected == [0.53]
    before = settings.config.read_bytes()
    page.restore_defaults()
    assert page.patch()["llm"]["model"] != "old-model"
    assert settings.config.read_bytes() == before
    assert "character" not in page.patch()
    assert "dialogue_style" not in page.patch()
    page.api_key.setText("discard-this-draft")
    page.discard_changes()
    assert page.patch() == {}
    assert page.api_key.text() == ""
    assert settings.config.read_bytes() == before
    assert not any(key.startswith("hub.") for key in page.fields)
    panel.deleteLater()


def test_app_save_blocks_other_settings_and_restart_saves_draft(settings, qapp):
    from PySide6.QtWidgets import QWidget
    from ui.widgets.settings_panel import SettingsPanel
    from ui.controllers.application_settings_controller import ApplicationSettingsController
    window = QWidget()
    window.host = SimpleNamespace(management_surface=settings.surface)
    window.restart_application = Mock()
    panel = SettingsPanel(window)
    controller = ApplicationSettingsController(window, panel)
    page = panel.application_page
    page.fields["llm.model"].setText("saved-before-restart")
    assert not controller.prepare_restart()
    assert not panel.character_import_button.isEnabled()
    assert not panel.name_input.isEnabled()
    _wait_for_controller(qapp, controller)
    window.restart_application.assert_called_once()
    assert yaml.safe_load(settings.config.read_text())["llm"]["model"] == "saved-before-restart"
    assert page.patch() == {}
    assert panel.character_import_button.isEnabled()
    page.fields["llm.model"].setText("unsaved-draft")
    controller.save_secret("openai_api_key", "another-local-key")
    _wait_for_controller(qapp, controller)
    assert page.patch() == {"llm": {"model": "unsaved-draft"}}
    assert settings.owner.refresh().secrets.openai_api_key == "another-local-key"
    assert "another-local-key" not in page.status.text()
    page.api_key.setText("not-saved-yet")
    assert not controller.prepare_restart()
    assert panel.tabs.currentWidget() is page
    window.deleteLater()


def test_application_page_scrolls_without_changing_model(settings, qapp):
    from PySide6.QtCore import QPoint, QPointF, Qt
    from PySide6.QtGui import QWheelEvent
    from ui.widgets.settings_panel import SettingsPanel
    panel = SettingsPanel()
    panel.resize(360, 560)
    panel.application_page.load_snapshot(settings.surface.read_application_settings())
    panel.tabs.setCurrentIndex(1)
    panel.show()
    qapp.processEvents()
    page = panel.application_page
    combo = page.fields["llm.reasoning_effort"]
    bar = page.scroll_area.verticalScrollBar()
    assert bar.maximum() > 0
    wheel = QWheelEvent(QPointF(5, 5), QPointF(5, 5), QPoint(), QPoint(0, -120),
                        Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.NoScrollPhase, False)
    qapp.sendEvent(combo, wheel)
    assert bar.value() > 0
    assert page.patch() == {}
    assert panel.scroll_area.verticalScrollBar().value() == 0
    panel.hide()
    panel.deleteLater()


def test_cloud_selection_requires_a_separate_private_key_and_preserves_drafts(settings, qapp):
    from PySide6.QtWidgets import QWidget
    from ui.widgets.settings_panel import SettingsPanel
    from ui.controllers.application_settings_controller import ApplicationSettingsController
    before = settings.config.read_bytes()
    with pytest.raises(ValueError, match="百炼"):
        settings.surface.write_application_settings({"stt": {"backend": "qwen_cloud"}})
    assert settings.config.read_bytes() == before
    window = QWidget()
    window.host = SimpleNamespace(management_surface=settings.surface)
    panel = SettingsPanel(window)
    controller = ApplicationSettingsController(window, panel)
    page = panel.application_page
    page.fields["stt.backend"].setCurrentIndex(page.fields["stt.backend"].findData("qwen_cloud"))
    page.asr_key.setText("private-asr-key")
    page.api_key.setText("unrelated-unsaved-chat-key")
    controller.save_secret("dashscope_api_key", page.asr_key.text())
    _wait_for_controller(qapp, controller)
    assert not page.asr_key.text()
    assert page.api_key.text() == "unrelated-unsaved-chat-key"
    assert page.patch() == {"stt": {"backend": "qwen_cloud"}}
    settings.surface.write_application_settings(page.patch())
    state = settings.surface.read_application_settings()
    assert state["asr_key_configured"] and state["values"]["stt"]["backend"] == "qwen_cloud"
    assert "private-asr-key" not in json.dumps(state)
    assert "private-asr-key" not in settings.config.read_text()
    assert settings.owner.refresh().secrets.openai_api_key == "old-local-key"
    restarted = settings.environment(settings.owner.restart_environment())
    assert restarted.secrets.dashscope_api_key == "private-asr-key"
    window.deleteLater()


@pytest.mark.parametrize("backend", ["faster_whisper", "google"])
def test_saving_new_qwen_path_migrates_legacy_base_before_applying_edit(settings, qapp, backend):
    from ui.widgets.application_settings_page import ApplicationSettingsPage
    raw = yaml.safe_load(settings.config.read_text())
    raw["stt"] = {"backend": backend, "model": "old-whisper-weights", "compute_type": "int8"}
    settings.config.write_text(yaml.safe_dump(raw))
    page = ApplicationSettingsPage()
    page.load_snapshot(settings.surface.read_application_settings())
    page.fields["stt.model"].setText("/downloaded/Qwen3-ASR-1.7B")
    precision = page.fields["stt.compute_type"]
    precision.setCurrentIndex(precision.findData("float16"))
    assert "backend" not in page.patch()["stt"]
    saved = settings.surface.write_application_settings(page.patch())
    actual = settings.surface.config_manager.load().stt
    assert actual.backend == "qwen_asr"
    assert actual.model == saved["values"]["stt"]["model"] == "/downloaded/Qwen3-ASR-1.7B"
    assert actual.compute_type == "float16"
    assert yaml.safe_load(settings.config.read_text())["stt"]["backend"] == "qwen_asr"
    page.deleteLater()


def test_device_refresh_preserves_unplugged_selection_and_works_without_microphone_stack(settings, qapp, monkeypatch):
    from PySide6.QtWidgets import QWidget
    from ui.widgets.settings_panel import SettingsPanel
    from ui.controllers.application_settings_controller import ApplicationSettingsController
    window = QWidget()
    window.host = SimpleNamespace(management_surface=settings.surface)
    panel = SettingsPanel(window)
    controller = ApplicationSettingsController(window, panel)
    page = panel.application_page
    selected = "[\"ALSA\",\"Desk Mic\",1]"
    page.update_device_choices("stt.input_device", [("默认", ""), ("Desk", selected)])
    page.fields["stt.input_device"].setCurrentIndex(1)
    monkeypatch.setattr("hardware.audio_input.devices.list_input_devices", Mock(side_effect=RuntimeError("no driver")))
    monkeypatch.setattr("PySide6.QtMultimedia.QMediaDevices.audioOutputs", lambda: [
        SimpleNamespace(id=lambda: b"speaker", description=lambda: "桌面音箱")])
    controller.refresh_devices()
    _wait_for_controller(qapp, controller)
    assert page.values()["stt"]["input_device"] == selected
    assert page.fields["tts.output_device_id"].findData(b"speaker".hex()) >= 0
    assert "麦克风列表不可用" in page.status.text()
    settings.surface.write_application_settings(page.patch())
    assert settings.surface.config_manager.load().stt.input_device == selected
    window.deleteLater()


def test_settings_inventory_loads_off_gui_thread_without_losing_draft(settings, qapp):
    import threading
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QWidget
    from ui.widgets.settings_panel import SettingsPanel
    from ui.controllers.application_settings_controller import ApplicationSettingsController
    window = QWidget()
    window.host = SimpleNamespace(management_surface=settings.surface)
    panel = SettingsPanel(window)
    controller = ApplicationSettingsController(window, panel, autoload=False)
    page = panel.application_page
    page.load_snapshot(settings.surface.read_application_settings())
    page.fields["llm.model"].setText("keep-this-draft")
    entered, release = threading.Event(), threading.Event()
    def read():
        entered.set()
        assert release.wait(3)
        return {"characters": []}
    characters = SimpleNamespace(read_snapshot=read, apply_snapshot=Mock())
    try:
        controller.refresh_async(characters)
        assert entered.wait(2)
        responsive = []
        QTimer.singleShot(0, lambda: responsive.append(True))
        qapp.processEvents()
        assert responsive and controller.worker is not None
    finally:
        release.set()
        _wait_for_controller(qapp, controller)
    assert page.patch() == {"llm": {"model": "keep-this-draft"}}
    characters.apply_snapshot.assert_called_once()
    window.deleteLater()


def test_missing_api_key_opens_application_tab_on_startup(settings, qapp, monkeypatch):
    from ui.qt_overlay import OverlayWindow
    settings.env.write_text("", encoding="utf-8")
    owner = settings.environment()
    host = SimpleNamespace(management_surface=settings.surface)
    monkeypatch.setattr(OverlayWindow, "_init_backend", lambda window: setattr(window, "host", host))
    window = OverlayWindow(loaded_secrets=owner)
    window.show()
    qapp.processEvents()
    assert window.settings_panel.isVisible()
    _wait_for_controller(qapp, window.application_settings_controller)
    assert window.settings_panel.tabs.currentWidget() is window.settings_panel.application_page
    assert "尚未配置" in window.settings_panel.application_page.api_key_status.text()
    window.hide()
    window.deleteLater()


def test_chatbot_has_no_ecosystem_configuration_entry(settings):
    with pytest.raises(ValueError, match="角色包"):
        settings.surface.write_application_settings({"hub": {"enabled": True}})
    with pytest.raises(ValueError, match="不支持"):
        settings.surface.write_application_secret("hub_bearer_token", "not-supported")
    assert "hub" not in settings.surface.read_application_settings()["values"]


def test_cloud_recognition_keeps_language_but_disables_local_model_controls(settings, qapp):
    from ui.widgets.settings_panel import SettingsPanel
    panel = SettingsPanel()
    page = panel.application_page
    page.load_snapshot(settings.surface.read_application_settings())
    backend = page.fields["stt.backend"]
    backend.setCurrentIndex(backend.findData("qwen_cloud"))
    assert page.fields["stt.language"].isEnabled()
    assert not page.fields["stt.model"].isEnabled()
    backend.setCurrentIndex(backend.findData("qwen_asr"))
    assert page.fields["stt.language"].isEnabled()
    assert page.patch() == {}
    panel.deleteLater()


def test_secret_tainted_connection_is_redacted_and_cannot_probe_a_fallback_endpoint(settings, monkeypatch):
    settings.env.write_text(settings.env.read_text() + 'OPENAI_BASE_URL="https://example.invalid/${OPENAI_API_KEY}"\n', encoding="utf-8")
    state = settings.surface.read_application_settings()
    assert "old-local-key" not in json.dumps(state)
    assert not state["connection_test_available"]
    probe = Mock()
    monkeypatch.setattr("spica.adapters.llm.openai_compatible.check_model_connection", probe)
    with pytest.raises(ValueError, match="密钥插值"):
        settings.surface.test_application_connection(base_url=None, model="model")
    probe.assert_not_called()


@pytest.mark.parametrize("operation", ["test_connection", "save_secret"])
def test_config_errors_never_display_credentials(settings, qapp, monkeypatch, operation):
    from PySide6.QtWidgets import QWidget
    from ui.widgets.settings_panel import SettingsPanel
    from ui.controllers.application_settings_controller import ApplicationSettingsController
    probe = Mock()
    monkeypatch.setattr("spica.adapters.llm.openai_compatible.check_model_connection", probe)
    window = QWidget()
    window.host = SimpleNamespace(management_surface=settings.surface)
    panel = SettingsPanel(window)
    controller = ApplicationSettingsController(window, panel)
    raw = yaml.safe_load(settings.config.read_text())
    raw["tts"] = {"enabled": "old-local-key"}
    settings.config.write_text(yaml.safe_dump(raw), encoding="utf-8")
    before = settings.env.read_bytes()
    if operation == "test_connection":
        controller.test_connection()
    else:
        controller.save_secret("openai_api_key", "replacement-key")
    _wait_for_controller(qapp, controller)
    assert "old-local-key" not in panel.application_page.status.text()
    assert "replacement-key" not in panel.application_page.status.text()
    assert "tts.enabled" in panel.application_page.status.text()
    assert settings.env.read_bytes() == before
    probe.assert_not_called()
    window.deleteLater()


def test_effective_override_allows_unrelated_saves_without_freezing_environment(settings, monkeypatch):
    raw = yaml.safe_load(settings.config.read_text())
    raw["llm"]["model"] = None
    settings.config.write_text(yaml.safe_dump(raw), encoding="utf-8")
    settings.env.write_text(settings.env.read_text() + "MODEL=env-model\n", encoding="utf-8")
    assert settings.surface.read_application_settings()["values"]["llm"]["model"] == "env-model"
    result = settings.surface.write_application_settings({"tts": {"enabled": False}})
    assert result["values"]["tts"]["enabled"] is False
    monkeypatch.setenv("MODEL", "env-model")
    settings.surface.write_config({"character": {"interlocutor_name": "新称呼"}})
    saved = yaml.safe_load(settings.config.read_text())
    assert saved["llm"]["model"] is None
    assert saved["tts"]["enabled"] is False
    assert saved["character"]["interlocutor_name"] == "新称呼"
    assert settings.surface.read_application_settings()["values"]["llm"]["model"] == "env-model"


@pytest.mark.parametrize("existing", [True, False])
def test_secret_save_keeps_dependent_dotenv_references(settings, existing):
    prefix = "OPENAI_API_KEY='old-local-key'\n" if existing else ""
    settings.env.write_text(prefix + "JUDGE_API_KEY=\u0024{OPENAI_API_KEY}\nUNRELATED='keep me'\n", encoding="utf-8")
    settings.surface.write_application_secret("openai_api_key", "replacement-key")
    restarted = settings.environment(settings.owner.restart_environment())
    assert restarted.secrets.openai_api_key == "replacement-key"
    assert restarted.secrets.judge_api_key == "replacement-key"
    assert "UNRELATED='keep me'" in settings.env.read_text()


@pytest.mark.parametrize("saved_backend,draft_backend", [
    ("qwen_cloud", "qwen_asr"), ("qwen_asr", "qwen_cloud"),
])
def test_saving_secret_restores_stt_draft_controls(settings, qapp, saved_backend, draft_backend):
    from PySide6.QtWidgets import QWidget
    from ui.widgets.settings_panel import SettingsPanel
    from ui.controllers.application_settings_controller import ApplicationSettingsController
    settings.surface.write_application_secret("dashscope_api_key", "test-cloud-key")
    settings.surface.write_application_settings({"stt": {"backend": saved_backend}})
    window = QWidget()
    window.host = SimpleNamespace(management_surface=settings.surface)
    panel = SettingsPanel(window)
    controller = ApplicationSettingsController(window, panel)
    page = panel.application_page
    backend = page.fields["stt.backend"]
    backend.setCurrentIndex(backend.findData(draft_backend))
    controller.save_secret("openai_api_key", "replacement-key")
    _wait_for_controller(qapp, controller)
    assert backend.currentData() == draft_backend
    for field in ("model", "worker_python", "device", "compute_type", "warmup_on_startup"):
        assert page.fields[f"stt.{field}"].isEnabled() is (draft_backend == "qwen_asr")
    assert page.patch() == {"stt": {"backend": draft_backend}}
    window.deleteLater()


def test_connection_probe_uses_the_same_network_route_as_chat(settings, monkeypatch):
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from spica.host.agent_assembly import build_llm_client

    paths = []
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            paths.append(self.path)
            proxy_request = self.path.startswith("http://")
            payload = {"error": {"message": "fixture proxy requires authentication"}} if proxy_request else {
                "object": "list", "data": [{"id": "test-model", "object": "model", "created": 0, "owned_by": "fixture"}],
            }
            body = json.dumps(payload).encode()
            self.send_response(407 if proxy_request else 200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
    thread.start()
    address = f"http://127.0.0.1:{server.server_port}"
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        monkeypatch.setenv(name, address)
    for name in ("NO_PROXY", "no_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.setenv(name, "")
    try:
        with build_llm_client("synthetic-key", address + "/v1") as client:
            assert client.models.list().data[0].id == "test-model"
        result = settings.surface.test_application_connection(
            base_url=address + "/v1", model="test-model", api_key="synthetic-key",
        )
        assert "连接成功" in result["message"]
        assert paths == ["/v1/models", "/v1/models"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_memory_organizer_requires_explicit_opt_in_and_model(settings):
    management = settings.surface
    snapshot = management.read_application_settings()
    assert not snapshot["values"]["memory"]["consolidation_enabled"]
    assert snapshot["values"]["memory"]["consolidation_model"] == ""
    with pytest.raises(ValueError, match="整理"):
        management.write_application_settings({"memory": {"consolidation_enabled": True}})
    management.write_application_settings({"memory": {"consolidation_enabled": True,
        "consolidation_model": "budget-model", "consolidation_min_user_turns": 10,
        "consolidation_min_tokens": 4000}})
    result = management.read_application_settings()["values"]["memory"]
    assert result["consolidation_enabled"] and result["consolidation_model"] == "budget-model"

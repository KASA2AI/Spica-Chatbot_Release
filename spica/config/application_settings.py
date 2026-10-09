"""The desktop application's small, non-character configuration surface."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from spica.config.manager import ConfigManager
from spica.config.schema import AppConfig
from spica.config.secrets import LoadedSecrets


APPLICATION_FIELDS = {
    "llm": ("base_url", "model", "reasoning_effort"),
    "stt": ("backend", "language", "model", "device", "compute_type", "warmup_on_startup",
            "worker_python", "mic_backend", "input_device", "cloud_base_url", "cloud_model", "cloud_timeout_seconds"),
    "tts": ("enabled", "daily_enabled", "output_device_id"),
    "memory": ("consolidation_enabled", "consolidation_model", "consolidation_min_user_turns",
               "consolidation_min_tokens"),
    "screen": ("enabled",),
    "song": ("enabled",),
    "anime": ("enabled",),
    "galgame": ("reaction_mode",),
    "home": (
        "enabled", "daily_detection_enabled", "output_device_id", "output_fallback_device_id",
        "mqtt_host", "mqtt_port", "mqtt_base_topic",
        "presence_device", "door_device", "camera_device", "camera_width", "camera_height", "camera_fps",
        "model_directory", "data_directory", "display", "xauthority", "monitor_output", "monitor_name", "windows_monitor_id",
        "lights.on_device", "lights.off_device", "lights.dark_lux",
        "wake.enabled", "wake.power_control_enabled", "wake.suspend_margin_seconds",
        "wake.initial_volume", "wake.maximum_volume",
    ),
}


def get_setting(data: dict[str, Any], path: str) -> Any:
    for part in path.split("."):
        data = data[part]
    return data


def set_setting(data: dict[str, Any], path: str, value: Any) -> None:
    parts = path.split(".")
    for part in parts[:-1]:
        data = data.setdefault(part, {})
    data[parts[-1]] = value


def setting_leaves(data: dict[str, Any], prefix: str = ""):
    for key, value in data.items():
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            yield from setting_leaves(value, path)
        else:
            yield path, value


def application_defaults() -> dict[str, Any]:
    data = AppConfig().model_dump()
    from agent_tools.function_tools.song.config import DEFAULT_CONFIG
    data["song"]["enabled"] = DEFAULT_CONFIG["enabled"]
    return _select_fields(data)


def _select_fields(data: dict[str, Any]) -> dict[str, Any]:
    selected = {}
    for section, keys in APPLICATION_FIELDS.items():
        for key in keys:
            path = f"{section}.{key}"
            set_setting(selected, path, get_setting(data, path))
    return selected


def read_application_settings(manager: ConfigManager, owner: LoadedSecrets) -> dict[str, Any]:
    with manager._update_lock:
        environment = owner.refresh()
        raw = manager._read_yaml(manager.config_path)
        resolution = manager.resolve_snapshot(raw, environment.environment_snapshot)
        data = resolution.to_app_config().model_dump()
        defaults = application_defaults()
        # Song has an existing legacy JSON fallback; use its production resolver.
        from agent_tools.function_tools.song.config import DEFAULT_CONFIG_PATH as song_legacy, resolve_effective_song_config, song_enabled
        from agent_tools.function_tools.screen.config import DEFAULT_CONFIG_PATH as screen_legacy, resolve_effective_screen_config
        data["song"]["enabled"] = song_enabled(resolve_effective_song_config(config=resolution.to_app_config()))
        if screen_legacy.exists():
            data["screen"]["enabled"] = resolve_effective_screen_config(config=resolution.to_app_config()).enabled
        values = _select_fields(data)
        overrides = {}
        if song_legacy.exists():
            overrides["song.enabled"] = str(song_legacy.name)
        if screen_legacy.exists():
            overrides["screen.enabled"] = str(screen_legacy.name)
        for section, fields in APPLICATION_FIELDS.items():
            for field in fields:
                if section == "song" and field not in raw.get("song", {}):
                    continue
                leaf = resolution.resolved_at(tuple(f"{section}.{field}".split(".")))
                if leaf.source.environment_variable:
                    overrides[f"{section}.{field}"] = leaf.source.environment_variable
                value = get_setting(values, f"{section}.{field}")
                if (leaf.source.kind == "secret_tainted_env_override"
                        or isinstance(value, str) and environment.contains_secret_material(value)):
                    set_setting(values, f"{section}.{field}", None)
                    overrides[f"{section}.{field}"] = "包含密钥的配置来源"
        return {
            "values": values, "defaults": defaults, "overrides": overrides,
            "connection_test_available": not any(
                key.startswith("llm.") and source == "包含密钥的配置来源"
                for key, source in overrides.items()
            ),
            "asr_key_configured": bool(environment.secrets.dashscope_api_key),
            "asr_key_source": environment.secret_source("dashscope_api_key"),
            "api_key_configured": bool(environment.secrets.openai_api_key),
            "api_key_source": environment.secret_source("openai_api_key"),
            "config_directory": str(manager.config_path.parent),
        }


def validate_application_patch(patch: dict[str, Any]) -> None:
    if not isinstance(patch, dict):
        raise ValueError("应用设置必须是字段修改。")
    for section, fields in patch.items():
        if section not in APPLICATION_FIELDS or not isinstance(fields, dict):
            raise ValueError("应用设置不能修改角色包或其他配置。")
        if set(path for path, _ in setting_leaves(fields)) - set(APPLICATION_FIELDS[section]):
            raise ValueError("包含此页面不支持的配置项。")
        # Empty/unknown nested objects must not bypass the leaf whitelist.
        def check_objects(node, prefix=""):
            for key, value in node.items():
                path = f"{prefix}.{key}" if prefix else key
                if isinstance(value, dict):
                    if not any(name.startswith(path + ".") for name in APPLICATION_FIELDS[section]):
                        raise ValueError("包含此页面不支持的配置项。")
                    check_objects(value, path)
        check_objects(fields)
    llm = patch.get("llm", {})
    if "base_url" in llm:
        validate_api_base_url(llm["base_url"])
    if "model" in llm and not str(llm["model"] or "").strip():
        raise ValueError("请填写模型名称。")
    if "song" in patch and "enabled" in patch["song"] and type(patch["song"]["enabled"]) is not bool:
        raise ValueError("唱歌开关必须是布尔值。")
    if "model" in patch.get("stt", {}) and not str(patch["stt"]["model"] or "").strip():
        raise ValueError("识别模型名称不能为空。")


def save_application_settings(manager: ConfigManager, owner: LoadedSecrets, patch: dict[str, Any]) -> dict[str, Any]:
    validate_application_patch(patch)
    with manager._update_lock:
        locked = read_application_settings(manager, owner)["overrides"]
        for path, _ in setting_leaves(patch):
            if source := locked.get(path):
                raise ValueError(f"{path} 由 {source} 覆盖，请先移除该配置来源。")
        environment = owner.refresh()
        candidate = manager.resolve_snapshot(
            manager.merge(manager.migrate(manager._read_yaml(manager.config_path)), patch),
            environment.environment_snapshot,
        ).to_app_config()
        if ("stt" in patch and candidate.stt.backend == "qwen_asr"
                and candidate.stt.device == "cpu" and candidate.stt.compute_type != "float32"):
            raise ValueError("CPU 语音识别请使用 float32 精度。")
        if "stt" in patch and candidate.stt.backend == "qwen_cloud" and not environment.secrets.dashscope_api_key:
            raise ValueError("请先保存百炼语音识别 API Key，再启用云端识别。")
        if "memory" in patch and candidate.memory.consolidation_enabled and not candidate.memory.consolidation_model.strip():
            raise ValueError("请先填写用于记忆整理的模型名称，再启用自动整理。")
        if "home" in patch and candidate.home.enabled:
            import sys
            from spica.config.schema import fold_platform
            if fold_platform(candidate.platform.os, sys.platform) not in {'linux', 'windows'}:
                raise ValueError('Home 需要受支持的 Linux 或 Windows 平台。')
            if not candidate.home.camera_device.strip():
                raise ValueError('启用 Home 前请先选择相机并设置房间区域。')
        if any(isinstance(value, str) and environment.contains_secret_material(value)
               for _, value in setting_leaves(patch)):
            raise ValueError('密钥只能通过密钥输入框保存。')
        manager.update(patch, environment_snapshot=environment.environment_snapshot, reject_overrides=True)
        return read_application_settings(manager, owner)


def validate_api_base_url(value: str | None) -> None:
    if not value:
        return
    try:
        url = urlsplit(value)
        valid = (url.scheme in {"http", "https"} and url.hostname and url.port != 0
                 and not url.username and not url.password and not url.query and not url.fragment)
    except (TypeError, ValueError):
        valid = False
    if not valid:
        raise ValueError("API 地址应为 http(s) 地址，不含密钥、查询参数或账号密码。")

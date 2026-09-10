"""The desktop application's small, non-character configuration surface."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from spica.config.manager import ConfigManager
from spica.config.schema import AppConfig
from spica.config.secrets import LoadedSecrets


APPLICATION_FIELDS = {
    "llm": ("base_url", "model", "reasoning_effort"),
    "stt": ("backend", "language", "model", "device", "compute_type", "warmup_on_startup"),
    "tts": ("enabled",),
    "screen": ("enabled",),
    "song": ("enabled",),
    "anime": ("enabled",),
    "galgame": ("reaction_mode",),
}


def application_defaults() -> dict[str, Any]:
    data = AppConfig().model_dump()
    from agent_tools.function_tools.song.config import DEFAULT_CONFIG
    data["song"]["enabled"] = DEFAULT_CONFIG["enabled"]
    return _select_fields(data)


def _select_fields(data: dict[str, Any]) -> dict[str, Any]:
    return {
        section: {key: data.get(section, {}).get(key) for key in keys}
        for section, keys in APPLICATION_FIELDS.items()
    }


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
                leaf = resolution.resolved_at((section, field))
                if leaf.source.environment_variable:
                    overrides[f"{section}.{field}"] = leaf.source.environment_variable
                value = values[section][field]
                if (leaf.source.kind == "secret_tainted_env_override"
                        or isinstance(value, str) and environment.contains_secret_material(value)):
                    values[section][field] = None
                    overrides[f"{section}.{field}"] = "包含密钥的配置来源"
        return {
            "values": values, "defaults": defaults, "overrides": overrides,
            "connection_test_available": not any(
                key.startswith("llm.") and source == "包含密钥的配置来源"
                for key, source in overrides.items()
            ),
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
        if set(fields) - set(APPLICATION_FIELDS[section]):
            raise ValueError("包含此页面不支持的配置项。")
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
        for section, fields in patch.items():
            for field in fields:
                if source := locked.get(f"{section}.{field}"):
                    raise ValueError(f"{section}.{field} 由 {source} 覆盖，请先移除该配置来源。")
        environment = owner.refresh()
        candidate = manager.resolve_snapshot(
            manager.merge(manager._read_yaml(manager.config_path), patch),
            environment.environment_snapshot,
        ).to_app_config()
        if ("stt" in patch and candidate.stt.backend == "faster_whisper"
                and candidate.stt.device == "cpu" and candidate.stt.compute_type in {"float16", "int8_float16"}):
            raise ValueError("CPU 语音识别请使用 int8、float32 或自动精度。")
        for fields in patch.values():
            if any(isinstance(value, str) and environment.contains_secret_material(value) for value in fields.values()):
                raise ValueError("密钥只能通过密钥输入框保存。")
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

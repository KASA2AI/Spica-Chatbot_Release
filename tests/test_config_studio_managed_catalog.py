from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
import yaml

from spica.adapters.config_studio.platform import platform_capabilities_for
from spica.config.environment_snapshot import EnvironmentSnapshot
from spica.config.env_roster import RESPEAKER_ENV_MAP, RUNTIME_CACHE_ENV_MAP
from spica.config.secrets import Secrets
from spica.config_studio.managed_catalog import FixedFileRead, read_fixed_regular_file
from spica.config_studio.services import ReadOnlyConfigStudioServices


def _platform(root: Path):
    return platform_capabilities_for(
        os_family="posix",
        runtime_name="linux",
        user_id=os.getuid(),
        temp_directory=root.parent / "platform-tmp",
    )


def test_fixed_file_read_repr_never_contains_document_bytes() -> None:
    canary = b"synthetic-fixed-reader-private-bytes"

    rendered = repr(FixedFileRead(canary, "healthy", None))

    assert canary.decode() not in rendered


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _repository(tmp_path: Path, app_yaml: str = "{}\n") -> Path:
    root = tmp_path / "synthetic-repo"
    _write(root / "data" / "config" / "app.yaml", app_yaml)
    _write(
        root / "data" / "config" / "tts.yaml",
        """provider: text_only
tts_params:
  speed: 1.25
emotions:
  happy:
    label: synthetic-happy
""",
    )
    _write(
        root / "data" / "config" / "visual.yaml",
        """enabled: true
selection:
  enable_smoothing: false
character:
  default_expression_id: '007'
""",
    )
    _write(
        root / "spica_data" / "Spica_skill" / "meta.json",
        json.dumps(
            {
                "slug": "spica",
                "name": "Synthetic Spica",
                "tags": {"temperament": "wind"},
            }
        ),
    )
    _write(
        root / "ui" / "overlay_config.json",
        json.dumps(
            {
                "default_character_scale": 1.2,
                "default_ui_scale": 99,
                "spica_voice_volume": -1,
            }
        ),
    )
    return root


def _services(root: Path) -> ReadOnlyConfigStudioServices:
    return ReadOnlyConfigStudioServices(
        repo_root=root,
        environment_snapshot=EnvironmentSnapshot.from_mapping(
            {}, layer="synthetic"
        ),
        background_health_code=None,
        platform_capabilities=_platform(root),
    )


def _services_with_environment(
    root: Path,
    values: dict[str, str],
) -> ReadOnlyConfigStudioServices:
    return ReadOnlyConfigStudioServices(
        repo_root=root,
        environment_snapshot=EnvironmentSnapshot.from_mapping(
            values,
            layer="synthetic",
        ),
        background_health_code=None,
        platform_capabilities=_platform(root),
    )


def _document(catalog: dict[str, object], document_id: str) -> dict[str, object]:
    return next(
        document
        for document in catalog["managed_documents"]  # type: ignore[index]
        if document["id"] == document_id
    )


def _managed_field(document: dict[str, object], path: str) -> dict[str, object]:
    return next(
        field
        for field in document["fields"]  # type: ignore[index]
        if field["display_path"] == path
    )


def _app_field(catalog: dict[str, object], path: str) -> dict[str, object]:
    return next(
        field
        for field in catalog["fields"]  # type: ignore[index]
        if field["display_path"] == path
    )


def _environment_setting(
    catalog: dict[str, object],
    environment_variable: str,
) -> dict[str, object]:
    return next(
        setting
        for setting in catalog["environment_only_settings"]  # type: ignore[index]
        if setting["environment_variable"] == environment_variable
    )


def _plugin_status(catalog: dict[str, object], name: str) -> dict[str, object]:
    return next(
        status
        for status in catalog["plugin_statuses"]  # type: ignore[index]
        if status["name"] == name
    )


def test_fixed_reader_rejects_a_hardlinked_local_document_without_returning_content(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outside = tmp_path / "outside-character-data.yaml"
    canary = b"private_external_canary: must-not-cross-reader\n"
    outside.write_bytes(canary)
    local = tmp_path / "synthetic-repo" / "data" / "config" / "tts.yaml"
    local.parent.mkdir(parents=True)
    os.link(outside, local)

    import spica.config_studio.managed_catalog as managed_catalog

    with monkeypatch.context() as patch:
        patch.setattr(
            managed_catalog.os,
            "open",
            lambda *_args, **_kwargs: pytest.fail(
                "unsafe hardlink content must not be opened"
            ),
        )
        read = read_fixed_regular_file(
            local,
            platform_capabilities=_platform(local),
        )

    assert local.stat().st_nlink == 2
    assert read.status == "unsafe"
    assert read.code == "MANAGED_DOCUMENT_UNSAFE"
    assert read.content is None
    assert outside.read_bytes() == canary


def test_fixed_reader_rejects_a_posix_document_not_owned_by_the_current_user(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document = tmp_path / "synthetic-repo" / "ui" / "overlay_config.json"
    document.parent.mkdir(parents=True)
    canary = b'{"private_external_canary": true}\n'
    document.write_bytes(canary)
    wrong_owner_platform = platform_capabilities_for(
        os_family="posix",
        runtime_name="linux",
        user_id=os.getuid() + 1,
        temp_directory=tmp_path / "platform-tmp",
    )

    import spica.config_studio.managed_catalog as managed_catalog

    with monkeypatch.context() as patch:
        patch.setattr(
            managed_catalog.os,
            "open",
            lambda *_args, **_kwargs: pytest.fail(
                "wrong-owner content must not be opened"
            ),
        )
        read = read_fixed_regular_file(
            document,
            platform_capabilities=wrong_owner_platform,
        )

    assert read.status == "unsafe"
    assert read.code == "MANAGED_DOCUMENT_UNSAFE"
    assert read.content is None
    assert document.read_bytes() == canary


def test_fixed_reader_rejects_in_place_rewrite_after_descriptor_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document = tmp_path / "synthetic-repo" / "data" / "config" / "app.yaml"
    document.parent.mkdir(parents=True)
    old_content = b"llm:\n  model: first-model\n"
    new_content = b"{}\n"
    document.write_bytes(old_content)
    document_inode = document.stat().st_ino

    import spica.config_studio.managed_catalog as managed_catalog

    real_read = managed_catalog.os.read
    rewritten = False

    def rewrite_after_descriptor_read(descriptor, size):
        nonlocal rewritten
        chunk = real_read(descriptor, size)
        if chunk and os.fstat(descriptor).st_ino == document_inode and not rewritten:
            rewritten = True
            document.write_bytes(new_content)
            assert document.stat().st_ino == document_inode
        return chunk

    monkeypatch.setattr(managed_catalog.os, "read", rewrite_after_descriptor_read)

    read = read_fixed_regular_file(
        document,
        platform_capabilities=_platform(document),
    )

    assert rewritten is True
    assert read.status == "unsafe"
    assert read.code == "MANAGED_DOCUMENT_UNSAFE"
    assert read.content is None
    assert document.read_bytes() == new_content


def test_catalog_lists_non_app_environment_owners_without_exposing_paths(
    tmp_path: Path,
) -> None:
    root = _repository(tmp_path)
    runtime_name = RUNTIME_CACHE_ENV_MAP["cache_root"]
    vad_name = RESPEAKER_ENV_MAP["require_hardware_vad"]
    path_name = RESPEAKER_ENV_MAP["tuning_path"]
    input_name = RESPEAKER_ENV_MAP["input_device_index"]
    catalog = _services_with_environment(
        root,
        {
            runtime_name: "/outside/private-runtime-cache",
            vad_name: "1",
            path_name: "/outside/private-respeaker-owner",
            input_name: "/outside/disguised-as-device-index",
        },
    ).catalog()

    rows = catalog["environment_only_settings"]
    assert {row["environment_variable"] for row in rows} == {
        *RUNTIME_CACHE_ENV_MAP.values(),
        *RESPEAKER_ENV_MAP.values(),
    }
    assert all(row["editable"] is False for row in rows)
    assert all(row["unsupported_reason"] == "no_app_yaml_owner" for row in rows)

    runtime = _environment_setting(catalog, runtime_name)
    assert runtime == {
        "id": "runtime_cache.cache_root",
        "environment_variable": runtime_name,
        "configured": True,
        "configured_value": "<external-path>",
        "source_kind": "env_override",
        "environment_layer": "synthetic",
        "owner": "spica.config.runtime_env",
        "effect_policy": "next_spica_launch",
        "editable": False,
        "unsupported_reason": "no_app_yaml_owner",
    }
    assert _environment_setting(catalog, vad_name)["configured_value"] == "1"
    assert _environment_setting(catalog, path_name)["configured_value"] == (
        "<external-path>"
    )
    assert _environment_setting(catalog, input_name)["configured_value"] == (
        "<external-path>"
    )
    assert _environment_setting(
        catalog, RESPEAKER_ENV_MAP["end_silence_seconds"]
    )["source_kind"] == "default"
    encoded = json.dumps(catalog, ensure_ascii=False)
    assert "/outside/private-runtime-cache" not in encoded
    assert "/outside/private-respeaker-owner" not in encoded
    assert "/outside/disguised-as-device-index" not in encoded


def test_catalog_reports_configured_plugin_next_launch_and_package_health(
    tmp_path: Path,
) -> None:
    root = _repository(
        tmp_path,
        """plugins:
  - name: present_plugin
    enabled: true
  - name: missing_plugin
    enabled: false
""",
    )
    _write(root / "plugins" / "present_plugin" / "__init__.py", "raise canary\n")

    catalog = _services(root).catalog()

    assert _plugin_status(catalog, "present_plugin") == {
        "name": "present_plugin",
        "configured": True,
        "next_launch_enabled": True,
        "package_status": "present",
        "package_health_code": "PLUGIN_PACKAGE_PRESENT",
        "owner": "spica.plugins.manifest",
        "effect_policy": "next_spica_launch",
    }
    assert _plugin_status(catalog, "missing_plugin") == {
        "name": "missing_plugin",
        "configured": True,
        "next_launch_enabled": False,
        "package_status": "missing",
        "package_health_code": "PLUGIN_PACKAGE_MISSING",
        "owner": "spica.plugins.manifest",
        "effect_policy": "next_spica_launch",
    }
    encoded = json.dumps(catalog, ensure_ascii=False)
    assert "raise canary" not in encoded
    assert str(root) not in encoded


def test_catalog_bounds_invalid_plugin_names_before_wire_projection(
    tmp_path: Path,
) -> None:
    root = _repository(
        tmp_path,
        yaml.safe_dump(
            {"plugins": [{"name": "x" * 600_000, "enabled": False}]},
            sort_keys=False,
        ),
    )

    catalog = _services(root).catalog()
    encoded = json.dumps(catalog, ensure_ascii=False).encode("utf-8")

    assert len(encoded) <= 512 * 1024
    assert catalog["plugin_statuses"] == [
        {
            "name": "<invalid-plugin-name>",
            "configured": True,
            "next_launch_enabled": False,
            "package_status": "unsafe",
            "package_health_code": "PLUGIN_PACKAGE_UNSAFE",
            "owner": "spica.plugins.manifest",
            "effect_policy": "next_spica_launch",
        }
    ]


def test_catalog_uses_the_song_owner_defaults_and_strict_master_switch(
    tmp_path: Path,
) -> None:
    root = _repository(tmp_path, "song:\n  enabled: false\n")

    catalog = _services(root).catalog()
    song_fields = [
        field
        for field in catalog["fields"]
        if field["path"][0] == {"kind": "field", "name": "song"}
    ]

    assert len(song_fields) == 38
    assert all("_config_path" not in field["display_path"] for field in song_fields)
    assert _app_field(catalog, "song['enabled']")["next_launch_value"] is False
    assert _app_field(catalog, "song['enabled']")["control"] == "switch"
    assert _app_field(catalog, "song['search']['limit']")["next_launch_value"] == 20
    assert _app_field(catalog, "song['search']['limit']")["editable"] is False
    assert (
        _app_field(catalog, "song['generated_root']")["next_launch_value"]
        == "<external-path>"
    )


def test_read_only_service_checks_schema_paths_against_its_repository_root(
    tmp_path: Path,
) -> None:
    root = _repository(
        tmp_path,
        "anime:\n  download_dir: static/generated_anime\n",
    )
    (root / "static" / "generated_anime").mkdir(parents=True)

    field = _app_field(_services(root).catalog(), "anime.download_dir")

    assert field["path_health"] == {
        "status": "healthy",
        "code": "PATH_HEALTHY",
        "expected_kind": "directory",
    }


def test_invalid_song_switch_is_disabled_without_logging_its_raw_value(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    canary = "invalid-song-secret-canary"
    root = _repository(
        tmp_path,
        "song:\n  enabled:\n    unexpected: " + canary + "\n",
    )

    catalog = _services(root).catalog()

    assert _app_field(catalog, "song['enabled']")["next_launch_value"] is False
    assert canary not in caplog.text


def test_catalog_projects_complete_character_and_overlay_documents(
    tmp_path: Path,
) -> None:
    catalog = _services(_repository(tmp_path)).catalog()

    assert {document["id"] for document in catalog["managed_documents"]} == {
        "character_package",
        "character_tts",
        "character_visual",
        "overlay_preferences",
    }
    package = _document(catalog, "character_package")
    tts = _document(catalog, "character_tts")
    visual = _document(catalog, "character_visual")
    overlay = _document(catalog, "overlay_preferences")

    assert _managed_field(package, "tags.temperament")["current_value"] == "wind"
    assert _managed_field(tts, "tts_params.speed")["current_value"] == 1.25
    assert _managed_field(visual, "character.default_expression_id")[
        "current_value"
    ] == "007"
    assert len(overlay["fields"]) == 7
    assert _managed_field(overlay, "default_character_scale")["current_value"] == 1.2
    assert _managed_field(overlay, "default_ui_scale")["current_value"] == 1.8
    assert _managed_field(overlay, "spica_voice_volume")["current_value"] == 0.0
    assert all(
        document["editable"] is False
        for document in (package, tts, visual)
    )
    assert overlay["editable"] is True
    assert visual["effect_policy"] == "owner_mtime_reload"


def test_missing_default_character_package_does_not_hide_default_asset_documents(
    tmp_path: Path,
) -> None:
    root = _repository(tmp_path)
    (root / "spica_data" / "Spica_skill" / "meta.json").unlink()
    (root / "spica_data" / "Spica_skill").rmdir()

    catalog = _services(root).catalog()

    assert _document(catalog, "character_package")["health"]["status"] == "missing"
    assert _managed_field(_document(catalog, "character_tts"), "provider")[
        "current_value"
    ] == "text_only"
    assert _managed_field(_document(catalog, "character_visual"), "enabled")[
        "current_value"
    ] is True


def test_external_character_documents_expose_only_owner_basename_and_health(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    external = tmp_path / "outside" / "character"
    _write(
        external / "meta.json",
        json.dumps(
            {
                "slug": "external",
                "tts_config_path": "voice.yaml",
                "visual_config_path": "look.yaml",
                "private_path": str(tmp_path / "outside" / "private.bin"),
            }
        ),
    )
    _write(external / "voice.yaml", "provider: text_only\nmarker: package-tts\n")
    _write(external / "look.yaml", "marker: package-visual\n")
    root = _repository(
        tmp_path,
        "character:\n  package_dir: " + json.dumps(str(external)) + "\n",
    )

    import spica.config_studio.managed_catalog as managed_catalog

    read_fixed_regular_file = managed_catalog.read_fixed_regular_file

    def reject_external_read(path, *, platform_capabilities):
        if external in Path(path).parents:
            pytest.fail("external character content must not be read")
        return read_fixed_regular_file(
            path,
            platform_capabilities=platform_capabilities,
        )

    monkeypatch.setattr(
        managed_catalog,
        "read_fixed_regular_file",
        reject_external_read,
    )
    monkeypatch.setattr(
        managed_catalog,
        "load_character_package",
        lambda path: pytest.fail("external character owner must not be constructed"),
    )

    catalog = _services(root).catalog()
    encoded = json.dumps(catalog, ensure_ascii=False)

    package = _document(catalog, "character_package")
    tts = _document(catalog, "character_tts")
    visual = _document(catalog, "character_visual")
    assert package["external"] is True
    assert package["basename"] == "meta.json"
    assert tts["basename"] is None
    assert visual["basename"] is None
    assert all(document["fields"] == [] for document in (package, tts, visual))
    assert all(
        document["unsupported_reason"] == "external_read_only"
        for document in (package, tts, visual)
    )
    assert all(
        document["health"]["status"] == "external_read_only"
        for document in (package, tts, visual)
    )
    assert str(external) not in encoded
    assert str(tmp_path) not in encoded
    assert "package-tts" not in encoded
    assert "package-visual" not in encoded
    assert "private.bin" not in encoded


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlink unavailable")
def test_character_document_reader_rejects_symlinks_without_reading_the_target(
    tmp_path: Path,
) -> None:
    external = tmp_path / "outside" / "character"
    _write(
        external / "meta.json",
        json.dumps({"tts_config_path": "voice.yaml"}),
    )
    _write(external / "target.yaml", "marker: forbidden-canary\n")
    os.symlink(external / "target.yaml", external / "voice.yaml")
    root = _repository(
        tmp_path,
        "character:\n  package_dir: " + json.dumps(str(external)) + "\n",
    )

    catalog = _services(root).catalog()
    tts = _document(catalog, "character_tts")
    encoded = json.dumps(catalog, ensure_ascii=False)

    assert tts["health"] == {
        "status": "external_read_only",
        "code": "EXTERNAL_DOCUMENT_READ_ONLY",
    }
    assert tts["basename"] is None
    assert tts["fields"] == []
    assert tts["fields"] == []
    assert "forbidden-canary" not in encoded
    assert str(external) not in encoded


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlink unavailable")
def test_external_character_package_metadata_symlink_is_never_inspected(
    tmp_path: Path,
) -> None:
    external = tmp_path / "outside" / "character"
    _write(
        external / "target.json",
        json.dumps({"tts_config_path": "voice.yaml", "secret": "meta-canary"}),
    )
    os.symlink(external / "target.json", external / "meta.json")
    _write(external / "voice.yaml", "marker: should-not-be-read\n")
    root = _repository(
        tmp_path,
        "character:\n  package_dir: " + json.dumps(str(external)) + "\n",
    )

    catalog = _services(root).catalog()
    encoded = json.dumps(catalog, ensure_ascii=False)

    assert (
        _document(catalog, "character_package")["health"]["status"]
        == "external_read_only"
    )
    assert _document(catalog, "character_tts")["fields"] == []
    assert "meta-canary" not in encoded
    assert "should-not-be-read" not in encoded


def test_non_mapping_character_metadata_is_a_bounded_health_error(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    _write(root / "spica_data" / "Spica_skill" / "meta.json", "[]\n")

    services = _services(root)
    catalog = services.catalog()
    package = _document(catalog, "character_package")

    assert package["health"] == {
        "status": "invalid",
        "code": "MANAGED_DOCUMENT_INVALID",
    }
    assert _document(catalog, "character_tts")["fields"] == []
    assert services.meta()["health"]["recovery_only"] is False


def test_retired_documents_are_health_only_and_lock_plugin_authoring(
    tmp_path: Path,
) -> None:
    root = _repository(tmp_path)
    _write(root / "data" / "config" / "plugins.yaml", "plugins: []\n")
    _write(root / "config" / "screen_vision_config.json", "{}\n")
    _write(
        root / "agent_tools" / "function_tools" / "song" / "song_config.json",
        json.dumps({"enabled": True, "search": {"limit": 3}}),
    )

    services = _services(root)
    catalog = services.catalog()
    issue_codes = {issue["code"] for issue in services.meta()["health"]["issues"]}

    assert issue_codes == {
        "LEGACY_PLUGINS_DOCUMENT_PRESENT",
        "LEGACY_SCREEN_DOCUMENT_PRESENT",
        "LEGACY_SONG_DOCUMENT_PRESENT",
    }
    plugins = _app_field(catalog, "plugins")
    assert plugins["editable"] is False
    assert plugins["unsupported_reason"] == "legacy_owner_active"
    assert plugins["next_launch_value"] is None
    assert plugins["source_kind"] == "legacy_owner_active"
    screen = _app_field(catalog, "screen.enabled")
    assert screen["editable"] is False
    assert screen["unsupported_reason"] == "legacy_owner_active"
    assert screen["next_launch_value"] is None
    assert screen["source_kind"] == "legacy_owner_active"
    assert _app_field(catalog, "song['enabled']")["next_launch_value"] is True
    assert _app_field(catalog, "song['search']['limit']")["next_launch_value"] == 3


def test_managed_catalog_redacts_secret_keys_and_enforces_response_budgets(
    tmp_path: Path,
) -> None:
    root = _repository(tmp_path)
    huge = "x" * 20_000
    _write(
        root / "data" / "config" / "tts.yaml",
        "api_token: secret-canary\nnotes: " + huge + "\n",
    )

    catalog = _services(root).catalog()
    encoded = json.dumps(catalog, ensure_ascii=False).encode("utf-8")
    tts = _document(catalog, "character_tts")

    assert b"secret-canary" not in encoded
    assert b"<redacted>" in encoded
    assert len(encoded) <= 512 * 1024
    assert tts["truncation"]["strings"] > 0


def test_untyped_managed_numbers_are_always_json_wire_safe(tmp_path: Path) -> None:
    root = _repository(tmp_path, "song:\n  mix:\n    vocal_gain: .nan\n")
    _write(root / "data" / "config" / "tts.yaml", "temperature: .inf\n")

    catalog = _services(root).catalog()

    json.dumps(catalog, ensure_ascii=False, allow_nan=False)
    assert _managed_field(_document(catalog, "character_tts"), "temperature")[
        "current_value"
    ] == "<non-finite-number>"
    assert _app_field(catalog, "song['mix']['vocal_gain']")[
        "next_launch_value"
    ] == "<non-finite-number>"


def test_overlay_catalog_contract_tracks_every_owner_field(tmp_path: Path) -> None:
    from spica.config.overlay_owner import OVERLAY_FIELD_SPECS, OverlayConfig
    from ui.overlay_config import load_overlay_config

    root = _repository(tmp_path)
    overlay = _document(_services(root).catalog(), "overlay_preferences")
    owner = load_overlay_config(root / "ui" / "overlay_config.json")

    assert {field["display_path"] for field in overlay["fields"]} == set(
        OverlayConfig.__dataclass_fields__
    )
    assert {
        field["display_path"]: field["current_value"] for field in overlay["fields"]
    } == {
        name: getattr(owner, name) for name in OverlayConfig.__dataclass_fields__
    }
    assert {
        field["display_path"]: field["default_value"] for field in overlay["fields"]
    } == {
        name: spec.default for name, spec in OVERLAY_FIELD_SPECS.items()
    }
    assert overlay["editable"] is True
    assert overlay["unsupported_reason"] is None
    assert {
        field["display_path"]: (
            field["control"],
            field["minimum"],
            field["maximum"],
            field["editable"],
            field["unsupported_reason"],
        )
        for field in overlay["fields"]
    } == {
        name: ("number", spec.minimum, spec.maximum, True, None)
        for name, spec in OVERLAY_FIELD_SPECS.items()
    }


def test_character_tab_renders_read_only_managed_document_cards() -> None:
    javascript = (
        Path(__file__).resolve().parents[1] / "ui" / "config_studio" / "studio.js"
    ).read_text(encoding="utf-8")

    assert "catalog.managed_documents" in javascript
    assert "renderManagedDocuments" in javascript
    assert 'byId("character-documents")' in javascript
    assert "action.disabled = true" in javascript


def test_short_secret_redaction_preserves_managed_document_wire_keys_and_budget(
    tmp_path: Path,
) -> None:
    root = _repository(tmp_path)
    services = ReadOnlyConfigStudioServices(
        repo_root=root,
        environment_snapshot=EnvironmentSnapshot.from_mapping(
            {}, layer="synthetic"
        ),
        secrets=Secrets(openai_api_key="a"),
        background_health_code=None,
        platform_capabilities=_platform(root),
    )

    catalog = services.catalog()
    encoded = json.dumps(catalog, ensure_ascii=False).encode("utf-8")
    document = catalog["managed_documents"][0]
    field = document["fields"][0]

    assert "managed_documents" in catalog
    assert {
        "id",
        "title",
        "category",
        "owner",
        "effect_policy",
        "source_kind",
        "external",
        "editable",
        "unsupported_reason",
        "health",
        "fields",
        "truncation",
    }.issubset(document)
    assert {"path", "display_path", "current_value", "default_value"}.issubset(
        field
    )
    assert len(encoded) <= 512 * 1024


def test_short_secret_redaction_preserves_environment_and_plugin_dto_keys(
    tmp_path: Path,
) -> None:
    root = _repository(
        tmp_path,
        "plugins:\n  - name: sample\n    enabled: false\n",
    )
    services = ReadOnlyConfigStudioServices(
        repo_root=root,
        environment_snapshot=EnvironmentSnapshot.from_mapping(
            {RESPEAKER_ENV_MAP["require_hardware_vad"]: "a"},
            layer="synthetic",
        ),
        secrets=Secrets(openai_api_key="a"),
        background_health_code=None,
        platform_capabilities=_platform(root),
    )

    catalog = services.catalog()
    environment_row = catalog["environment_only_settings"][0]
    plugin_row = catalog["plugin_statuses"][0]

    assert {
        "id",
        "environment_variable",
        "configured",
        "configured_value",
        "source_kind",
        "environment_layer",
        "owner",
        "effect_policy",
        "editable",
        "unsupported_reason",
    } == set(environment_row)
    assert {
        "name",
        "configured",
        "next_launch_enabled",
        "package_status",
        "package_health_code",
        "owner",
        "effect_policy",
    } == set(plugin_row)
    assert "a" not in plugin_row["name"]
    assert len(json.dumps(catalog, ensure_ascii=False).encode("utf-8")) <= 512 * 1024


def test_plugin_statuses_share_the_total_catalog_budget_after_redaction(
    tmp_path: Path,
) -> None:
    plugins = [
        {"name": f"{'a' * 58}{index:03d}", "enabled": False}
        for index in range(256)
    ]
    root = _repository(
        tmp_path,
        yaml.safe_dump({"plugins": plugins}, sort_keys=False),
    )
    services = ReadOnlyConfigStudioServices(
        repo_root=root,
        environment_snapshot=EnvironmentSnapshot.from_mapping(
            {}, layer="synthetic"
        ),
        secrets=Secrets(openai_api_key="a"),
        background_health_code=None,
        platform_capabilities=_platform(root),
    )

    catalog = services.catalog()

    assert len(json.dumps(catalog, ensure_ascii=False).encode("utf-8")) <= 512 * 1024
    assert catalog["truncation"]["total_bytes"] > 0

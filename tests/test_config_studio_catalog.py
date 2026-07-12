from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Literal

import pytest
import yaml
from pydantic import create_model

from spica.config.environment_snapshot import EnvironmentSnapshot
from spica.config.manager import ConfigManager
from spica.config.schema import AppConfig
from spica.config_studio.catalog import ConfigCatalog
from spica.config_studio.paths import ConfigFieldPath, FieldSegment, MapKeySegment
from spica.config_studio.redaction import enforce_catalog_wire_budget


class FutureAppConfig(AppConfig):
    future_enabled: bool = False


def test_catalog_fields_complete_reports_normal_and_row_limited_snapshots():
    environment = EnvironmentSnapshot.from_mapping({}, layer="synthetic")
    resolution = ConfigManager().resolve_snapshot({}, environment)
    normal = ConfigCatalog(
        model_type=AppConfig,
        raw_document={},
        resolution=resolution,
    ).snapshot().to_wire(max_total_bytes=1024 * 1024)
    large_model = create_model(
        "LargeCatalogModel",
        **{f"field_{index}": (bool, False) for index in range(270)},
    )

    limited = ConfigCatalog(
        model_type=large_model,
        raw_document={},
        resolution=resolution,
    ).snapshot().to_wire(
        max_collection_items=256,
        max_total_bytes=1024 * 1024,
    )

    assert normal["fields_complete"] is True
    assert len(limited["fields"]) == 256
    assert limited["fields_complete"] is False
    assert limited["truncation"]["collections"] == 1


def test_catalog_fields_complete_fails_closed_for_shared_profile_aliases():
    shared_profile = {
        "min": "images:1x3x32x32",
        "opt": "images:1x3x64x64",
        "max": "images:1x3x128x128",
    }
    raw_document = {
        "ocr": {
            "trt": {
                "profiles": {
                    "det": shared_profile,
                    "rec": shared_profile,
                }
            }
        }
    }
    resolution = ConfigManager().resolve_snapshot(
        raw_document,
        EnvironmentSnapshot.from_mapping({}, layer="synthetic"),
    )

    wire = ConfigCatalog(
        model_type=AppConfig,
        raw_document=raw_document,
        resolution=resolution,
    ).snapshot().to_wire(max_total_bytes=1024 * 1024)

    assert wire["truncation"]["aliases"] == 1
    assert wire["fields_complete"] is False


def test_catalog_projects_schema_default_file_value_and_next_launch_source():
    raw_document = {"llm": {"model": "file-model"}}
    resolution = ConfigManager().resolve_snapshot(
        raw_document,
        EnvironmentSnapshot.from_mapping({}, layer="inherited"),
    )
    catalog = ConfigCatalog(
        model_type=AppConfig,
        raw_document=raw_document,
        resolution=resolution,
    )

    model = catalog.snapshot().field(ConfigFieldPath.fields("llm", "model"))

    assert model.control == "text"
    assert model.default_value == "gpt-4.1-mini"
    assert model.file_value == "file-model"
    assert model.next_launch_value == "file-model"
    assert model.source_kind == "file"
    assert model.editable is True


def test_catalog_derives_nullable_scalar_metadata_from_pydantic_annotations():
    resolution = ConfigManager().resolve_snapshot(
        {},
        EnvironmentSnapshot.from_mapping({}, layer="synthetic"),
    )
    snapshot = ConfigCatalog(
        model_type=AppConfig,
        raw_document={},
        resolution=resolution,
    ).snapshot()

    assert snapshot.field(ConfigFieldPath.fields("llm", "base_url")).nullable is True
    assert (
        snapshot.field(
            ConfigFieldPath.fields("galgame", "reaction_judge_base_url")
        ).nullable
        is True
    )
    assert snapshot.field(ConfigFieldPath.fields("llm", "model")).nullable is False

    fields = {
        field["display_path"]: field for field in snapshot.to_wire()["fields"]
    }
    assert fields["llm.base_url"]["nullable"] is True
    assert fields["galgame.reaction_judge_base_url"]["nullable"] is True
    assert fields["llm.model"]["nullable"] is False


def test_structured_app_fields_expose_compact_schema_owned_editor_metadata():
    resolution = ConfigManager().resolve_snapshot(
        {},
        EnvironmentSnapshot.from_mapping({}, layer="synthetic"),
    )
    wire = ConfigCatalog(
        model_type=AppConfig,
        raw_document={},
        resolution=resolution,
    ).snapshot().to_wire()
    fields = {field["display_path"]: field for field in wire["fields"]}

    assert fields["anime.bilibili_spaces"]["structured_schema"] == {
        "type": "array",
        "items": {"type": "string"},
    }
    assert fields["ocr.trt.profiles"]["structured_schema"] == {
        "type": "object",
        "additionalProperties": {
            "type": "object",
            "additionalProperties": {"type": "string"},
        },
    }
    assert fields["plugins"]["structured_schema"] == {
        "type": "array",
        "items": {
            "type": "object",
            "properties": {
                "enabled": {"type": "boolean", "default": True},
                "name": {"type": "string"},
            },
            "required": ["name"],
        },
    }
    reaction_schema = fields["galgame.reaction_table"]["structured_schema"]
    assert {branch.get("type") for branch in reaction_schema["anyOf"]} == {
        "object",
        "null",
    }
    tier = reaction_schema["anyOf"][0]["additionalProperties"]
    assert tier == {
        "type": "object",
        "properties": {
            "cooldown_seconds": {"type": "number"},
            "max_per_window": {"type": "integer"},
            "min_score": {"type": "integer"},
        },
        "required": ["min_score", "max_per_window", "cooldown_seconds"],
    }


@pytest.mark.parametrize(
    ("property_count", "schema_available"),
    ((64, True), (65, False)),
)
def test_structured_authoring_schema_fails_closed_at_required_property_budget(
    property_count: int,
    schema_available: bool,
):
    item_model = create_model(
        f"RequiredPropertyItem{property_count}",
        **{f"property_{index}": (str, ...) for index in range(property_count)},
    )
    catalog_model = create_model(
        f"RequiredPropertyCatalog{property_count}",
        payload=(list[item_model], []),
    )
    resolution = ConfigManager().resolve_snapshot(
        {},
        EnvironmentSnapshot.from_mapping({}, layer="synthetic"),
    )

    row = ConfigCatalog(
        model_type=catalog_model,
        raw_document={},
        resolution=resolution,
    ).snapshot().to_wire(max_total_bytes=1024 * 1024)["fields"][0]

    assert (row["structured_schema"] is not None) is schema_available
    assert row["authoring_complete"] is schema_available
    if schema_available:
        item_schema = row["structured_schema"]["items"]
        assert len(item_schema["properties"]) == 64
        assert len(item_schema["required"]) == 64


def test_structured_field_without_owner_schema_is_not_authoring_complete():
    resolution = ConfigManager().resolve_snapshot(
        {},
        EnvironmentSnapshot.from_mapping({}, layer="synthetic"),
    )
    snapshot = ConfigCatalog(
        model_type=AppConfig,
        raw_document={},
        resolution=resolution,
    ).snapshot()
    structured = next(
        field
        for field in snapshot.fields
        if field.control == "structured" and field.structured_schema is None
    )

    assert structured.authoring_complete is False
    row = next(
        field
        for field in snapshot.to_wire()["fields"]
        if field["display_path"]
        == "song['separator']['extra_kwargs']"
    )
    assert row["authoring_complete"] is False


def test_secret_like_typed_field_projection_is_never_authoring_complete():
    secret_model = create_model(
        "FutureSecretLikeCatalog",
        api_token=(str, "synthetic-default-token"),
    )
    resolution = ConfigManager().resolve_snapshot(
        {},
        EnvironmentSnapshot.from_mapping({}, layer="synthetic"),
    )

    row = ConfigCatalog(
        model_type=secret_model,
        raw_document={"api_token": "synthetic-file-token"},
        resolution=resolution,
    ).snapshot().to_wire(max_total_bytes=1024 * 1024)["fields"][0]

    assert row["default_value"] == "<redacted>"
    assert row["file_value"] == "<redacted>"
    assert row["authoring_complete"] is False


@pytest.mark.parametrize(
    ("choice_count", "schema_available"),
    ((64, True), (65, False)),
)
def test_structured_authoring_schema_fails_closed_at_enum_budget(
    choice_count: int,
    schema_available: bool,
):
    choice_type = Literal.__getitem__(
        tuple(f"choice-{index}" for index in range(choice_count))
    )
    catalog_model = create_model(
        f"EnumCatalog{choice_count}",
        selections=(list[choice_type], []),
    )
    resolution = ConfigManager().resolve_snapshot(
        {},
        EnvironmentSnapshot.from_mapping({}, layer="synthetic"),
    )

    row = ConfigCatalog(
        model_type=catalog_model,
        raw_document={},
        resolution=resolution,
    ).snapshot().to_wire(max_total_bytes=1024 * 1024)["fields"][0]

    assert (row["structured_schema"] is not None) is schema_available
    assert row["authoring_complete"] is schema_available
    if schema_available:
        assert len(row["structured_schema"]["items"]["enum"]) == 64


@pytest.mark.parametrize(
    "invalid_fragment",
    (
        {"anyOf": "not-an-array"},
        {"items": "not-an-object"},
        {"additionalProperties": True},
        {"minLength": -1},
    ),
)
def test_structured_authoring_schema_fails_closed_for_invalid_known_vocabulary(
    invalid_fragment: dict[str, object],
):
    class InvalidSchemaItem(str):
        @classmethod
        def __get_pydantic_core_schema__(cls, _source_type, handler):
            return handler(str)

        @classmethod
        def __get_pydantic_json_schema__(cls, _core_schema, _handler):
            return {"type": "string", **invalid_fragment}

    catalog_model = create_model(
        "InvalidSchemaCatalog",
        selections=(list[InvalidSchemaItem], []),
    )
    resolution = ConfigManager().resolve_snapshot(
        {},
        EnvironmentSnapshot.from_mapping({}, layer="synthetic"),
    )

    row = ConfigCatalog(
        model_type=catalog_model,
        raw_document={},
        resolution=resolution,
    ).snapshot().to_wire(max_total_bytes=1024 * 1024)["fields"][0]

    assert row["structured_schema"] is None
    assert row["authoring_complete"] is False


@pytest.mark.parametrize(
    ("item_count", "projected_count", "authoring_complete", "truncations"),
    ((65, 65, True, 0), (257, 256, False, 2)),
)
def test_structured_field_reports_its_own_authoring_projection_completeness(
    item_count: int,
    projected_count: int,
    authoring_complete: bool,
    truncations: int,
):
    raw_document = {
        "anime": {
            "bilibili_spaces": [f"synthetic-space-{index}" for index in range(item_count)]
        }
    }
    resolution = ConfigManager().resolve_snapshot(
        raw_document,
        EnvironmentSnapshot.from_mapping({}, layer="synthetic"),
    )

    wire = ConfigCatalog(
        model_type=AppConfig,
        raw_document=raw_document,
        resolution=resolution,
    ).snapshot().to_wire(
        max_collection_items=256,
        max_total_bytes=1024 * 1024,
    )
    fields = {field["display_path"]: field for field in wire["fields"]}
    structured = fields["anime.bilibili_spaces"]

    assert len(structured["file_value"]) == projected_count
    assert len(structured["next_launch_value"]) == projected_count
    assert structured["authoring_complete"] is authoring_complete
    assert fields["llm.model"]["authoring_complete"] is True
    assert wire["truncation"]["collections"] == truncations


def test_catalog_never_labels_post_manager_owner_folds_as_resolved_values():
    resolution = ConfigManager().resolve_snapshot(
        {},
        EnvironmentSnapshot.from_mapping({}, layer="inherited"),
    )
    snapshot = ConfigCatalog(
        model_type=AppConfig,
        raw_document={},
        resolution=resolution,
    ).snapshot()

    for path in (
        ("character", "character_id"),
        ("character", "character_profile"),
        ("character", "character_name"),
        ("platform", "os"),
        ("stt", "mic_backend"),
    ):
        field = snapshot.field(ConfigFieldPath.fields(*path))
        assert field.next_launch_value is None
        assert field.source_kind == "owner_derived"
        assert field.effect_policy == "owner_derived_on_next_launch"
        assert field.editable is (path[0] != "character")


def test_catalog_keeps_explicit_platform_and_mic_owner_values_editable():
    raw_document = {
        "platform": {"os": "linux"},
        "stt": {"mic_backend": "generic"},
    }
    resolution = ConfigManager().resolve_snapshot(
        raw_document,
        EnvironmentSnapshot.from_mapping({}, layer="inherited"),
    )
    snapshot = ConfigCatalog(
        model_type=AppConfig,
        raw_document=raw_document,
        resolution=resolution,
    ).snapshot()

    platform = snapshot.field(ConfigFieldPath.fields("platform", "os"))
    mic = snapshot.field(ConfigFieldPath.fields("stt", "mic_backend"))
    assert (platform.next_launch_value, platform.editable) == ("linux", True)
    assert (mic.next_launch_value, mic.editable) == ("generic", True)


def test_schema_declared_repository_path_reports_no_follow_health(tmp_path: Path):
    (tmp_path / "anime-cache").mkdir()
    raw_document = {"anime": {"download_dir": "anime-cache"}}
    resolution = ConfigManager().resolve_snapshot(
        raw_document,
        EnvironmentSnapshot.from_mapping({}, layer="inherited"),
    )

    snapshot = ConfigCatalog(
        model_type=AppConfig,
        raw_document=raw_document,
        resolution=resolution,
        repo_root=tmp_path,
    ).snapshot()
    field = snapshot.field(ConfigFieldPath.fields("anime", "download_dir"))

    assert field.path_health is not None
    assert field.path_health.status == "healthy"
    assert field.path_health.code == "PATH_HEALTHY"
    assert field.path_health.expected_kind == "directory"
    wire = snapshot.to_wire()
    row = next(
        item
        for item in wire["fields"]
        if item["display_path"] == "anime.download_dir"
    )
    assert row["path_health"] == {
        "status": "healthy",
        "code": "PATH_HEALTHY",
        "expected_kind": "directory",
    }


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlink unavailable")
def test_schema_declared_path_health_rejects_symlink_components(tmp_path: Path):
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    os.symlink(outside, tmp_path / "linked-cache")
    raw_document = {"anime": {"download_dir": "linked-cache"}}
    resolution = ConfigManager().resolve_snapshot(
        raw_document,
        EnvironmentSnapshot.from_mapping({}, layer="inherited"),
    )

    field = ConfigCatalog(
        model_type=AppConfig,
        raw_document=raw_document,
        resolution=resolution,
        repo_root=tmp_path,
    ).snapshot().field(ConfigFieldPath.fields("anime", "download_dir"))

    assert field.path_health is not None
    assert (field.path_health.status, field.path_health.code) == (
        "unsafe",
        "PATH_SYMLINK_UNSAFE",
    )


def test_catalog_only_treats_explicit_owner_path_fields_as_paths(tmp_path: Path):
    resolution = ConfigManager().resolve_snapshot(
        {},
        EnvironmentSnapshot.from_mapping({}, layer="inherited"),
    )

    snapshot = ConfigCatalog(
        model_type=AppConfig,
        raw_document={},
        resolution=resolution,
        repo_root=tmp_path,
    ).snapshot()
    declared_paths = {
        ".".join(field.path.field_names())
        for field in snapshot.fields
        if field.path_health is not None
    }

    assert declared_paths == {
        "character.skill_dir",
        "character.package_dir",
        "stt.download_root",
        "ocr.trt.engine_cache_dir",
        "anime.download_dir",
        "anime.cookies_file",
        "anime.library_file",
    }
    assert (
        snapshot.field(ConfigFieldPath.fields("stt", "model")).path_health is None
    )
    assert (
        snapshot.field(ConfigFieldPath.fields("anime", "player_command")).path_health
        is None
    )


@pytest.mark.parametrize(
    ("configured_value", "wire_value", "authoring_complete"),
    (
        ("data/cookies.txt", "data/cookies.txt", True),
        ("{absolute}", "<external-path>", False),
        (r"C:\synthetic\cookies.txt", "<external-path>", False),
    ),
)
def test_schema_path_projection_distinguishes_safe_relative_and_external_values(
    tmp_path: Path,
    configured_value: str,
    wire_value: str,
    authoring_complete: bool,
):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    value = (
        str(tmp_path / "outside" / "cookies.txt")
        if configured_value == "{absolute}"
        else configured_value
    )
    raw_document = {"anime": {"cookies_file": value}}
    resolution = ConfigManager().resolve_snapshot(
        raw_document,
        EnvironmentSnapshot.from_mapping({}, layer="synthetic"),
    )

    row = next(
        field
        for field in ConfigCatalog(
            model_type=AppConfig,
            raw_document=raw_document,
            resolution=resolution,
            repo_root=repo_root,
        ).snapshot().to_wire(max_total_bytes=1024 * 1024)["fields"]
        if field["display_path"] == "anime.cookies_file"
    )

    assert row["file_value"] == wire_value
    assert row["next_launch_value"] == wire_value
    assert row["authoring_complete"] is authoring_complete


def test_absolute_non_path_string_remains_visible_and_authorable(tmp_path: Path):
    value = "/synthetic/model-id"
    raw_document = {"llm": {"model": value}}
    resolution = ConfigManager().resolve_snapshot(
        raw_document,
        EnvironmentSnapshot.from_mapping({}, layer="synthetic"),
    )

    row = next(
        field
        for field in ConfigCatalog(
            model_type=AppConfig,
            raw_document=raw_document,
            resolution=resolution,
            repo_root=tmp_path,
        ).snapshot().to_wire(max_total_bytes=1024 * 1024)["fields"]
        if field["display_path"] == "llm.model"
    )

    assert row["path_health"] is None
    assert row["file_value"] == value
    assert row["next_launch_value"] == value
    assert row["authoring_complete"] is True


def test_structured_map_key_redaction_marks_authoring_projection_incomplete():
    raw_document = {
        "ocr": {
            "trt": {
                "profiles": {
                    "token": {
                        "min": "images:1x3x32x32",
                        "opt": "images:1x3x64x64",
                        "max": "images:1x3x128x128",
                    }
                }
            }
        }
    }
    resolution = ConfigManager().resolve_snapshot(
        raw_document,
        EnvironmentSnapshot.from_mapping({}, layer="synthetic"),
    )

    row = next(
        field
        for field in ConfigCatalog(
            model_type=AppConfig,
            raw_document=raw_document,
            resolution=resolution,
        ).snapshot().to_wire(max_total_bytes=1024 * 1024)["fields"]
        if field["display_path"] == "ocr.trt.profiles"
    )

    assert row["file_value"] == {"token": "<redacted>"}
    assert row["authoring_complete"] is False


def test_external_absolute_paths_are_masked_without_an_existence_oracle(
    tmp_path: Path,
):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    existing_external = tmp_path / "external-existing"
    existing_external.mkdir()
    external_values = (existing_external, tmp_path / "external-missing")

    for external in external_values:
        raw_document = {"anime": {"download_dir": str(external)}}
        resolution = ConfigManager().resolve_snapshot(
            raw_document,
            EnvironmentSnapshot.from_mapping({}, layer="inherited"),
        )
        snapshot = ConfigCatalog(
            model_type=AppConfig,
            raw_document=raw_document,
            resolution=resolution,
            repo_root=repo_root,
        ).snapshot()
        field = snapshot.field(ConfigFieldPath.fields("anime", "download_dir"))
        wire = next(
            item
            for item in snapshot.to_wire()["fields"]
            if item["display_path"] == "anime.download_dir"
        )

        assert field.path_health is not None
        assert (field.path_health.status, field.path_health.code) == (
            "unsafe",
            "PATH_OUTSIDE_ROOT",
        )
        assert wire["file_value"] == "<external-path>"
        assert wire["next_launch_value"] == "<external-path>"
        assert str(external) not in json.dumps(wire)


def test_schema_path_health_distinguishes_missing_and_wrong_target_kind(
    tmp_path: Path,
):
    wrong_kind = tmp_path / "cookies-as-directory"
    wrong_kind.mkdir()
    raw_document = {
        "anime": {
            "download_dir": "missing-download-directory",
            "cookies_file": "cookies-as-directory",
        }
    }
    resolution = ConfigManager().resolve_snapshot(
        raw_document,
        EnvironmentSnapshot.from_mapping({}, layer="inherited"),
    )

    snapshot = ConfigCatalog(
        model_type=AppConfig,
        raw_document=raw_document,
        resolution=resolution,
        repo_root=tmp_path,
    ).snapshot()
    missing = snapshot.field(ConfigFieldPath.fields("anime", "download_dir"))
    wrong = snapshot.field(ConfigFieldPath.fields("anime", "cookies_file"))

    assert missing.path_health is not None
    assert (missing.path_health.status, missing.path_health.code) == (
        "missing",
        "PATH_MISSING",
    )
    assert wrong.path_health is not None
    assert (wrong.path_health.status, wrong.path_health.code) == (
        "unsafe",
        "PATH_KIND_MISMATCH",
    )
    assert wrong.path_health.expected_kind == "file"


def test_path_health_does_not_guess_launch_cwd_or_home_expansion(tmp_path: Path):
    (tmp_path / "looks-present").mkdir()
    raw_document = {
        "character": {"package_dir": "looks-present"},
        "anime": {"download_dir": "~/generated-anime"},
    }
    resolution = ConfigManager().resolve_snapshot(
        raw_document,
        EnvironmentSnapshot.from_mapping({}, layer="inherited"),
    )

    snapshot = ConfigCatalog(
        model_type=AppConfig,
        raw_document=raw_document,
        resolution=resolution,
        repo_root=tmp_path,
    ).snapshot()
    launch_cwd = snapshot.field(
        ConfigFieldPath.fields("character", "package_dir")
    )
    launch_home = snapshot.field(ConfigFieldPath.fields("anime", "download_dir"))

    assert launch_cwd.path_health is not None
    assert (launch_cwd.path_health.status, launch_cwd.path_health.code) == (
        "unavailable",
        "PATH_BASE_UNAVAILABLE",
    )
    assert launch_home.path_health is not None
    assert (launch_home.path_health.status, launch_home.path_health.code) == (
        "unavailable",
        "PATH_BASE_UNAVAILABLE",
    )


def test_new_schema_leaf_is_visible_even_before_resolution_owner_is_updated():
    resolution = ConfigManager().resolve_snapshot(
        {},
        EnvironmentSnapshot.from_mapping({}, layer="inherited"),
    )

    future = ConfigCatalog(
        model_type=FutureAppConfig,
        raw_document={},
        resolution=resolution,
    ).snapshot().field(ConfigFieldPath.fields("future_enabled"))

    assert future.control == "switch"
    assert future.default_value is False
    assert future.editable is False
    assert future.unsupported_reason == "resolution_unavailable"


def test_song_dynamic_values_are_complete_but_only_enabled_is_authorable():
    raw_document = {
        "song": {
            "enabled": False,
            "advanced": {"timeout_seconds": 12},
        }
    }
    resolution = ConfigManager().resolve_snapshot(
        raw_document,
        EnvironmentSnapshot.from_mapping({}, layer="inherited"),
    )
    snapshot = ConfigCatalog(
        model_type=AppConfig,
        raw_document=raw_document,
        resolution=resolution,
    ).snapshot()

    enabled = snapshot.field(
        ConfigFieldPath((FieldSegment("song"), MapKeySegment("enabled")))
    )
    timeout = snapshot.field(
        ConfigFieldPath(
            (
                FieldSegment("song"),
                MapKeySegment("advanced"),
                MapKeySegment("timeout_seconds"),
            )
        )
    )

    assert enabled.control == "switch"
    assert enabled.editable is True
    assert enabled.next_launch_value is False
    assert timeout.next_launch_value == 12
    assert timeout.editable is False
    assert timeout.unsupported_reason == "owner_schema_unavailable"


def test_catalog_exposes_schema_and_owner_numeric_boundaries():
    resolution = ConfigManager().resolve_snapshot(
        {},
        EnvironmentSnapshot.from_mapping({}, layer="inherited"),
    )
    snapshot = ConfigCatalog(
        model_type=AppConfig,
        raw_document={},
        resolution=resolution,
    ).snapshot()

    screen_size = snapshot.field(ConfigFieldPath.fields("screen", "max_side"))
    anime_threshold = snapshot.field(
        ConfigFieldPath.fields("anime", "auto_play_threshold_seconds")
    )

    assert (screen_size.minimum, screen_size.maximum) == (128, 4096)
    assert anime_threshold.minimum == 0.0


def test_catalog_marks_file_value_shadowed_by_explicit_env_override():
    raw_document = {"llm": {"model": "file-model"}}
    resolution = ConfigManager().resolve_snapshot(
        raw_document,
        EnvironmentSnapshot.from_mapping(
            {"MODEL": "env-model"},
            layer="repo_dotenv",
        ),
    )

    model = ConfigCatalog(
        model_type=AppConfig,
        raw_document=raw_document,
        resolution=resolution,
    ).snapshot().field(ConfigFieldPath.fields("llm", "model"))

    assert model.source_kind == "env_override"
    assert model.environment_variable == "MODEL"
    assert model.environment_layer == "repo_dotenv"
    assert model.file_value_shadowed is True


def test_catalog_names_production_owner_and_concrete_effect_policy():
    resolution = ConfigManager().resolve_snapshot(
        {},
        EnvironmentSnapshot.from_mapping({}, layer="inherited"),
    )

    model = ConfigCatalog(
        model_type=AppConfig,
        raw_document={},
        resolution=resolution,
    ).snapshot().field(ConfigFieldPath.fields("llm", "model"))

    assert model.owner == "ConfigManager/AppConfig"
    assert model.effect_policy == "next_spica_launch"


def test_catalog_projects_basic_advanced_level_and_feature_dependencies():
    resolution = ConfigManager().resolve_snapshot(
        {},
        EnvironmentSnapshot.from_mapping({}, layer="inherited"),
    )
    snapshot = ConfigCatalog(
        model_type=AppConfig,
        raw_document={},
        resolution=resolution,
    ).snapshot()

    assert snapshot.field(ConfigFieldPath.fields("tts", "enabled")).level == "basic"
    assert (
        snapshot.field(
            ConfigFieldPath.fields("galgame", "ocr_interval_seconds")
        ).level
        == "advanced"
    )
    anime_path = ConfigFieldPath.fields("anime", "download_dir")
    anime = snapshot.field(anime_path)
    assert [(dependency.path, dependency.expected_value) for dependency in anime.dependencies] == [
        (ConfigFieldPath.fields("anime", "enabled"), True)
    ]

    wire = snapshot.to_wire()
    anime_wire = next(field for field in wire["fields"] if field["display_path"] == "anime.download_dir")
    assert anime_wire["level"] == "advanced"
    assert anime_wire["dependencies"] == [
        {
            "path": [
                {"kind": "field", "name": "anime"},
                {"kind": "field", "name": "enabled"},
            ],
            "display_path": "anime.enabled",
            "expected_value": True,
        }
    ]


def test_catalog_wire_projection_is_explicit_and_reports_bounded_truncation():
    canary = "profile-canary-" + ("x" * 200)
    raw_document = {"character": {"profile_override": canary}}
    resolution = ConfigManager().resolve_snapshot(
        raw_document,
        EnvironmentSnapshot.from_mapping({}, layer="inherited"),
    )

    payload = ConfigCatalog(
        model_type=AppConfig,
        raw_document=raw_document,
        resolution=resolution,
    ).snapshot().to_wire(max_string_chars=32, max_collection_items=128)
    encoded = json.dumps(payload, ensure_ascii=False)

    assert canary not in encoded
    assert payload["truncation"]["strings"] > 0
    model = next(
        field
        for field in payload["fields"]
        if field["display_path"] == "character.profile_override"
    )
    assert model["file_value"].endswith("…")


def test_catalog_surfaces_unrecognized_document_keys_as_read_only_health_rows():
    raw_document = {"future_owner": {"mode": "keep-visible"}}
    resolution = ConfigManager().resolve_snapshot(
        raw_document,
        EnvironmentSnapshot.from_mapping({}, layer="inherited"),
    )

    unknown = ConfigCatalog(
        model_type=AppConfig,
        raw_document=raw_document,
        resolution=resolution,
    ).snapshot().field(
        ConfigFieldPath(
            (MapKeySegment("future_owner"), MapKeySegment("mode"))
        )
    )

    assert unknown.file_value == "keep-visible"
    assert unknown.next_launch_value is None
    assert unknown.editable is False
    assert unknown.unsupported_reason == "owner_unrecognized"


def test_catalog_wire_projection_enforces_a_total_encoded_response_budget():
    raw_document = {
        "future_owner": {
            f"field_{index}": "x" * 80
            for index in range(40)
        }
    }
    resolution = ConfigManager().resolve_snapshot(
        raw_document,
        EnvironmentSnapshot.from_mapping({}, layer="inherited"),
    )

    payload = ConfigCatalog(
        model_type=AppConfig,
        raw_document=raw_document,
        resolution=resolution,
    ).snapshot().to_wire(
        max_string_chars=128,
        max_collection_items=256,
        max_total_bytes=2_000,
    )

    assert len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) <= 2_000
    assert payload["truncation"]["total_bytes"] > 0
    assert payload["fields_complete"] is False
    assert payload["fields"]


def test_redaction_budget_marks_only_top_level_app_field_removal_incomplete():
    resolution = ConfigManager().resolve_snapshot(
        {},
        EnvironmentSnapshot.from_mapping({}, layer="synthetic"),
    )
    payload = ConfigCatalog(
        model_type=AppConfig,
        raw_document={},
        resolution=resolution,
    ).snapshot().to_wire(max_total_bytes=1024 * 1024)
    original_count = len(payload["fields"])

    bounded = enforce_catalog_wire_budget(payload, max_total_bytes=8_000)

    assert len(bounded["fields"]) < original_count
    assert bounded["fields_complete"] is False

    managed_only = {
        "fields": [],
        "fields_complete": True,
        "managed_documents": [
            {
                "fields": [{"display_path": "synthetic", "current_value": "x" * 4000}],
                "truncation": {"total_bytes": 0},
            }
        ],
        "truncation": {"total_bytes": 0},
    }
    enforce_catalog_wire_budget(managed_only, max_total_bytes=1024)

    assert managed_only["managed_documents"][0]["fields"] == []
    assert managed_only["fields_complete"] is True


def test_unknown_secret_named_leaf_is_redacted_after_flattening():
    canaries = ("unknown-api-secret", "unknown-password-secret")
    raw_document = {
        "future_owner": {
            "api_key": canaries[0],
            "nested": {"password": canaries[1]},
        }
    }
    resolution = ConfigManager().resolve_snapshot(
        raw_document,
        EnvironmentSnapshot.from_mapping({}, layer="inherited"),
    )

    payload = ConfigCatalog(
        model_type=AppConfig,
        raw_document=raw_document,
        resolution=resolution,
    ).snapshot().to_wire()
    encoded = json.dumps(payload, ensure_ascii=False)

    assert all(canary not in encoded for canary in canaries)
    secret_fields = [
        field
        for field in payload["fields"]
        if "api_key" in field["display_path"]
        or "password" in field["display_path"]
    ]
    assert {field["file_value"] for field in secret_fields} == {"<redacted>"}


def test_catalog_reports_a_cyclic_unknown_graph_without_recursing_forever():
    cyclic: dict[str, object] = {}
    cyclic["self"] = cyclic
    raw_document = {"future_owner": cyclic}
    resolution = ConfigManager().resolve_snapshot(
        {},
        EnvironmentSnapshot.from_mapping({}, layer="inherited"),
    )

    payload = ConfigCatalog(
        model_type=AppConfig,
        raw_document=raw_document,
        resolution=resolution,
    ).snapshot().to_wire()

    assert payload["truncation"]["cycles"] == 1
    assert payload["truncation"]["aliases"] == 0
    cycle = next(
        field
        for field in payload["fields"]
        if field["display_path"] == "['future_owner']['self']"
    )
    assert cycle["file_value"] == "<cycle-reference>"


def test_catalog_reports_a_reused_yaml_alias_without_expanding_it_twice():
    raw_document = yaml.safe_load(
        """
future_owner:
  first: &shared
    mode: shared-value
  second: *shared
"""
    )
    resolution = ConfigManager().resolve_snapshot(
        {},
        EnvironmentSnapshot.from_mapping({}, layer="inherited"),
    )

    payload = ConfigCatalog(
        model_type=AppConfig,
        raw_document=raw_document,
        resolution=resolution,
    ).snapshot().to_wire()

    assert payload["truncation"]["cycles"] == 0
    assert payload["truncation"]["aliases"] == 1
    assert {
        field["display_path"]: field["file_value"]
        for field in payload["fields"]
        if field["display_path"].startswith("['future_owner']")
    } == {
        "['future_owner']['first']['mode']": "shared-value",
        "['future_owner']['second']": "<alias-reference>",
    }


def test_catalog_truncates_an_excessively_deep_unknown_graph_before_flattening():
    nested: dict[str, object] = {"value": "too-deep"}
    for _ in range(80):
        nested = {"child": nested}
    resolution = ConfigManager().resolve_snapshot(
        {},
        EnvironmentSnapshot.from_mapping({}, layer="inherited"),
    )

    payload = ConfigCatalog(
        model_type=AppConfig,
        raw_document={"future_owner": nested},
        resolution=resolution,
    ).snapshot().to_wire(max_depth=128)

    assert payload["truncation"]["depth"] == 1
    assert any(
        field["file_value"] == "<depth-limit>"
        for field in payload["fields"]
    )


def test_catalog_bounds_untrusted_map_keys_in_paths_and_display_text():
    long_key = "attacker-key-" + ("x" * 200)
    resolution = ConfigManager().resolve_snapshot(
        {},
        EnvironmentSnapshot.from_mapping({}, layer="inherited"),
    )

    payload = ConfigCatalog(
        model_type=AppConfig,
        raw_document={"future_owner": {long_key: "visible"}},
        resolution=resolution,
    ).snapshot().to_wire(max_string_chars=32)
    encoded = json.dumps(payload, ensure_ascii=False)

    assert long_key not in encoded
    assert payload["truncation"]["strings"] >= 2
    unknown = next(
        field
        for field in payload["fields"]
        if field["display_path"].startswith("['future_owner']")
    )
    assert len(unknown["path"][-1]["key"]) <= 32
    assert len(unknown["display_path"]) <= 32


def test_catalog_caps_unknown_collection_items_before_creating_rows():
    raw_document = {
        "future_owner": {
            f"field_{index}": index
            for index in range(300)
        }
    }
    resolution = ConfigManager().resolve_snapshot(
        {},
        EnvironmentSnapshot.from_mapping({}, layer="inherited"),
    )

    payload = ConfigCatalog(
        model_type=AppConfig,
        raw_document=raw_document,
        resolution=resolution,
    ).snapshot().to_wire(
        max_collection_items=512,
        max_total_bytes=1024 * 1024,
    )

    unknown = [
        field
        for field in payload["fields"]
        if field["display_path"].startswith("['future_owner']")
    ]
    assert len(unknown) == 256
    assert payload["truncation"]["collections"] == 1


def test_catalog_stops_projecting_after_the_graph_node_budget():
    raw_document = {
        "future_owner": {
            f"branch_{branch}": {
                f"field_{index}": index
                for index in range(256)
            }
            for branch in range(20)
        }
    }
    resolution = ConfigManager().resolve_snapshot(
        {},
        EnvironmentSnapshot.from_mapping({}, layer="inherited"),
    )

    payload = ConfigCatalog(
        model_type=AppConfig,
        raw_document=raw_document,
        resolution=resolution,
    ).snapshot().to_wire(
        max_collection_items=6000,
        max_total_bytes=4 * 1024 * 1024,
    )

    assert payload["truncation"]["nodes"] == 1
    assert any(
        field["file_value"] == "<node-limit>"
        for field in payload["fields"]
    )


def test_catalog_bounds_a_cyclic_known_structured_field():
    plugins: list[object] = []
    plugins.append(plugins)
    resolution = ConfigManager().resolve_snapshot(
        {},
        EnvironmentSnapshot.from_mapping({}, layer="inherited"),
    )

    payload = ConfigCatalog(
        model_type=AppConfig,
        raw_document={"plugins": plugins},
        resolution=resolution,
    ).snapshot().to_wire()

    assert payload["truncation"]["cycles"] == 1
    plugins_field = next(
        field
        for field in payload["fields"]
        if field["display_path"] == "plugins"
    )
    assert plugins_field["file_value"] == ["<cycle-reference>"]

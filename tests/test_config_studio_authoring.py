from __future__ import annotations

import pytest

from spica.config_studio.authoring import (
    AuthoringError,
    ConfigAuthoringValidator,
    SetValue,
    UnsetValue,
)
from spica.config_studio.paths import (
    ConfigFieldPath,
    FieldSegment,
    ListIndexSegment,
    MapKeySegment,
)


def test_authoring_accepts_native_false_but_rejects_false_string():
    validator = ConfigAuthoringValidator()
    operation = SetValue(ConfigFieldPath.fields("tts", "enabled"), False)

    accepted = validator.validate(
        {"tts": {"enabled": True}},
        {"tts": {"enabled": False}},
        (operation,),
    )

    assert accepted.to_app_config().tts.enabled is False
    with pytest.raises(AuthoringError) as rejected:
        validator.validate(
            {"tts": {"enabled": True}},
            {"tts": {"enabled": "false"}},
            (SetValue(ConfigFieldPath.fields("tts", "enabled"), "false"),),
        )
    assert rejected.value.code == "TYPE_MISMATCH"


def test_candidate_matching_is_type_strict_so_true_does_not_equal_integer_one():
    validator = ConfigAuthoringValidator()

    with pytest.raises(AuthoringError) as rejected:
        validator.validate(
            {"tts": {"enabled": False}},
            {"tts": {"enabled": 1}},
            (SetValue(ConfigFieldPath.fields("tts", "enabled"), True),),
        )

    assert rejected.value.code == "UNDECLARED_CHANGE"


@pytest.mark.parametrize("coerced_value", [True, "4"])
def test_authoring_rejects_coercible_values_for_integer_fields(coerced_value):
    validator = ConfigAuthoringValidator()
    path = ConfigFieldPath.fields("memory", "recent_memory_turns")

    with pytest.raises(AuthoringError) as rejected:
        validator.validate(
            {"memory": {"recent_memory_turns": 3}},
            {"memory": {"recent_memory_turns": coerced_value}},
            (SetValue(path, coerced_value),),
        )

    assert rejected.value.code == "TYPE_MISMATCH"


def test_authoring_preserves_preexisting_unknown_content_verbatim():
    validator = ConfigAuthoringValidator()
    base = {
        "tts": {"enabled": True},
        "future_owner_data": {"mode": "keep-me", "count": 2},
    }
    candidate = {
        "tts": {"enabled": False},
        "future_owner_data": {"mode": "keep-me", "count": 2},
    }

    accepted = validator.validate(
        base,
        candidate,
        (SetValue(ConfigFieldPath.fields("tts", "enabled"), False),),
    )

    assert accepted.candidate_document()["future_owner_data"] == {
        "mode": "keep-me",
        "count": 2,
    }


def test_authoring_unsets_only_the_declared_file_override_and_preserves_unknowns():
    validator = ConfigAuthoringValidator()
    path = ConfigFieldPath.fields("tts", "enabled")
    base = {
        "tts": {"enabled": False},
        "future_owner_data": {"keep": True},
    }
    candidate = {
        "tts": {},
        "future_owner_data": {"keep": True},
    }

    accepted = validator.validate(
        base,
        candidate,
        (UnsetValue(path),),
    )

    assert accepted.to_app_config().tts.enabled is True
    assert accepted.candidate_document() == candidate


@pytest.mark.parametrize(
    "path",
    [
        ConfigFieldPath.fields("character", "character_id"),
        ConfigFieldPath.fields("unknown_owner_field"),
    ],
)
def test_authoring_unset_cannot_remove_read_only_or_unknown_paths(path):
    validator = ConfigAuthoringValidator()

    with pytest.raises(AuthoringError) as rejected:
        validator.validate({}, {}, (UnsetValue(path),))

    assert rejected.value.code in {"READ_ONLY_FIELD", "UNKNOWN_FIELD"}


def test_authoring_reports_new_unknown_field_instead_of_silently_dropping_it():
    validator = ConfigAuthoringValidator()

    with pytest.raises(AuthoringError) as rejected:
        validator.validate(
            {"tts": {"enabled": True}},
            {"tts": {"enabled": False}, "rogue": {"enabled": True}},
            (SetValue(ConfigFieldPath.fields("tts", "enabled"), False),),
        )

    assert rejected.value.code == "UNKNOWN_FIELD"


def test_song_owner_only_allows_native_boolean_enabled_operation():
    validator = ConfigAuthoringValidator()
    enabled_path = ConfigFieldPath(
        (FieldSegment("song"), MapKeySegment("enabled"))
    )
    base = {"song": {"enabled": True, "advanced": {"timeout": 12}}}

    accepted = validator.validate(
        base,
        {"song": {"enabled": False, "advanced": {"timeout": 12}}},
        (SetValue(enabled_path, False),),
    )
    assert accepted.to_app_config().song["enabled"] is False

    with pytest.raises(AuthoringError) as rejected:
        validator.validate(
            base,
            {"song": {"enabled": "false", "advanced": {"timeout": 12}}},
            (SetValue(enabled_path, "false"),),
        )
    assert rejected.value.code == "TYPE_MISMATCH"


def test_plugin_authoring_rejects_coerced_booleans_and_duplicate_names():
    validator = ConfigAuthoringValidator()
    path = ConfigFieldPath.fields("plugins")

    with pytest.raises(AuthoringError) as coerced:
        validator.validate(
            {"plugins": []},
            {"plugins": [{"name": "sample", "enabled": "false"}]},
            (SetValue(path, [{"name": "sample", "enabled": "false"}]),),
        )
    assert coerced.value.code == "TYPE_MISMATCH"

    duplicates = [
        {"name": "sample", "enabled": True},
        {"name": "sample", "enabled": False},
    ]
    with pytest.raises(AuthoringError) as duplicate:
        validator.validate(
            {"plugins": []},
            {"plugins": duplicates},
            (SetValue(path, duplicates),),
        )
    assert duplicate.value.code == "PLUGIN_DUPLICATE"


def test_plugin_authoring_rejects_extra_keys_that_owner_would_drop():
    validator = ConfigAuthoringValidator()
    path = ConfigFieldPath.fields("plugins")
    plugins = [{"name": "sample", "enabled": True, "canary_extra": "drop-me"}]

    with pytest.raises(AuthoringError) as rejected:
        validator.validate(
            {"plugins": []},
            {"plugins": plugins},
            (SetValue(path, plugins),),
        )

    assert rejected.value.code == "UNKNOWN_FIELD"


def test_typed_dynamic_map_rejects_nested_extra_fields_owner_would_drop():
    validator = ConfigAuthoringValidator()
    table = {
        "normal": {
            "min_score": 4,
            "max_per_window": 3,
            "cooldown_seconds": 90,
            "canary_extra": "drop-me",
        }
    }

    with pytest.raises(AuthoringError) as rejected:
        validator.validate(
            {},
            {"galgame": {"reaction_table": table}},
            (
                SetValue(
                    ConfigFieldPath.fields("galgame", "reaction_table"),
                    table,
                ),
            ),
        )

    assert rejected.value.code == "UNKNOWN_FIELD"


def test_authoring_rejects_non_string_value_before_owner_can_coerce_it():
    validator = ConfigAuthoringValidator()

    with pytest.raises(AuthoringError) as rejected:
        validator.validate(
            {"screen": {"provider": "moondream_local"}},
            {"screen": {"provider": 123}},
            (SetValue(ConfigFieldPath.fields("screen", "provider"), 123),),
        )

    assert rejected.value.code == "TYPE_MISMATCH"


def test_authoring_recursively_checks_list_item_types():
    validator = ConfigAuthoringValidator()
    values = ["space-id", 123]

    with pytest.raises(AuthoringError) as rejected:
        validator.validate(
            {},
            {"anime": {"bilibili_spaces": values}},
            (
                SetValue(
                    ConfigFieldPath.fields("anime", "bilibili_spaces"),
                    values,
                ),
            ),
        )

    assert rejected.value.code == "TYPE_MISMATCH"


def test_nested_plugin_edit_revalidates_the_complete_manifest():
    validator = ConfigAuthoringValidator()
    base = {
        "plugins": [
            {"name": "first", "enabled": True},
            {"name": "second", "enabled": True},
        ]
    }
    candidate = {
        "plugins": [
            {"name": "first", "enabled": True},
            {"name": "first", "enabled": True},
        ]
    }
    path = ConfigFieldPath(
        (
            FieldSegment("plugins"),
            ListIndexSegment(1),
            FieldSegment("name"),
        )
    )

    with pytest.raises(AuthoringError) as rejected:
        validator.validate(base, candidate, (SetValue(path, "first"),))

    assert rejected.value.code == "PLUGIN_DUPLICATE"


@pytest.mark.parametrize("unsafe_name", ["../evil", "nested/plugin", ".hidden", "a b"])
def test_plugin_names_are_single_safe_components(unsafe_name):
    validator = ConfigAuthoringValidator()
    plugins = [{"name": unsafe_name, "enabled": True}]

    with pytest.raises(AuthoringError) as rejected:
        validator.validate(
            {"plugins": []},
            {"plugins": plugins},
            (SetValue(ConfigFieldPath.fields("plugins"), plugins),),
        )

    assert rejected.value.code == "PLUGIN_NAME_INVALID"


def test_enabled_plugin_must_be_a_safe_package_under_the_fixed_root(tmp_path):
    plugin_root = tmp_path / "plugins"
    package = plugin_root / "safe_plugin"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("# synthetic package\n", encoding="utf-8")
    validator = ConfigAuthoringValidator(plugin_root=plugin_root)
    plugins = [{"name": "safe_plugin", "enabled": True}]

    accepted = validator.validate(
        {"plugins": []},
        {"plugins": plugins},
        (SetValue(ConfigFieldPath.fields("plugins"), plugins),),
    )

    assert accepted.to_app_config().plugins[0].name == "safe_plugin"


def test_enabled_plugin_rejects_missing_or_symlinked_packages(tmp_path):
    plugin_root = tmp_path / "plugins"
    plugin_root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "__init__.py").write_text("# outside\n", encoding="utf-8")
    (plugin_root / "linked_plugin").symlink_to(outside, target_is_directory=True)
    validator = ConfigAuthoringValidator(plugin_root=plugin_root)

    for name, expected_code in (
        ("missing_plugin", "PLUGIN_PACKAGE_MISSING"),
        ("linked_plugin", "PLUGIN_PACKAGE_UNSAFE"),
    ):
        plugins = [{"name": name, "enabled": True}]
        with pytest.raises(AuthoringError) as rejected:
            validator.validate(
                {"plugins": []},
                {"plugins": plugins},
                (SetValue(ConfigFieldPath.fields("plugins"), plugins),),
            )
        assert rejected.value.code == expected_code


def test_disabled_missing_plugin_can_be_retained_or_removed_as_repair(tmp_path):
    plugin_root = tmp_path / "plugins"
    plugin_root.mkdir()
    validator = ConfigAuthoringValidator(plugin_root=plugin_root)
    disabled = [{"name": "missing_plugin", "enabled": False}]

    accepted = validator.validate(
        {"plugins": [{"name": "missing_plugin", "enabled": True}]},
        {"plugins": disabled},
        (SetValue(ConfigFieldPath.fields("plugins"), disabled),),
    )

    assert accepted.to_app_config().plugins[0].enabled is False


@pytest.mark.parametrize("coerced_value", [True, "2.5"])
def test_authoring_rejects_coercible_values_for_float_fields(coerced_value):
    validator = ConfigAuthoringValidator()
    path = ConfigFieldPath.fields("galgame", "ocr_interval_seconds")

    with pytest.raises(AuthoringError) as rejected:
        validator.validate(
            {"galgame": {"ocr_interval_seconds": 0.3}},
            {"galgame": {"ocr_interval_seconds": coerced_value}},
            (SetValue(path, coerced_value),),
        )

    assert rejected.value.code == "TYPE_MISMATCH"


@pytest.mark.parametrize("outside_owner_range", [64, 5000])
def test_authoring_rejects_screen_size_that_owner_would_silently_clamp(
    outside_owner_range,
):
    validator = ConfigAuthoringValidator()
    path = ConfigFieldPath.fields("screen", "max_side")

    with pytest.raises(AuthoringError) as rejected:
        validator.validate(
            {"screen": {"max_side": 768}},
            {"screen": {"max_side": outside_owner_range}},
            (SetValue(path, outside_owner_range),),
        )

    assert rejected.value.code == "VALUE_OUT_OF_RANGE"


def test_authoring_uses_typed_map_segment_for_key_containing_dot():
    validator = ConfigAuthoringValidator()
    base = {
        "galgame": {
            "reaction_table": {
                "normal.mode": {
                    "min_score": 4,
                    "max_per_window": 3,
                    "cooldown_seconds": 90.0,
                }
            }
        }
    }
    candidate = {
        "galgame": {
            "reaction_table": {
                "normal.mode": {
                    "min_score": 5,
                    "max_per_window": 3,
                    "cooldown_seconds": 90.0,
                }
            }
        }
    }
    path = ConfigFieldPath(
        (
            FieldSegment("galgame"),
            FieldSegment("reaction_table"),
            MapKeySegment("normal.mode"),
            FieldSegment("min_score"),
        )
    )

    accepted = validator.validate(base, candidate, (SetValue(path, 5),))

    assert accepted.to_app_config().galgame.reaction_table["normal.mode"].min_score == 5


def test_authoring_rejects_shared_mapping_identity_before_declared_operation_mutates_it():
    validator = ConfigAuthoringValidator()
    shared_base = {
        "min_score": 4,
        "max_per_window": 3,
        "cooldown_seconds": 90.0,
    }
    shared_candidate = {
        "min_score": 5,
        "max_per_window": 3,
        "cooldown_seconds": 90.0,
    }
    base = {
        "galgame": {
            "reaction_table": {"low": shared_base, "normal": shared_base}
        }
    }
    candidate = {
        "galgame": {
            "reaction_table": {
                "low": shared_candidate,
                "normal": shared_candidate,
            }
        }
    }
    path = ConfigFieldPath(
        (
            FieldSegment("galgame"),
            FieldSegment("reaction_table"),
            MapKeySegment("low"),
            FieldSegment("min_score"),
        )
    )

    with pytest.raises(AuthoringError) as rejected:
        validator.validate(base, candidate, (SetValue(path, 5),))

    assert rejected.value.code == "DOCUMENT_ALIAS_UNSUPPORTED"


def test_runtime_derived_character_fields_are_read_only():
    validator = ConfigAuthoringValidator()
    path = ConfigFieldPath.fields("character", "character_id")

    with pytest.raises(AuthoringError) as rejected:
        validator.validate(
            {"character": {}},
            {"character": {"character_id": "spica"}},
            (SetValue(path, "spica"),),
        )

    assert rejected.value.code == "READ_ONLY_FIELD"


@pytest.mark.parametrize(
    ("base", "candidate", "operation"),
    (
        (
            {"character": {"package_dir": "characters/spica"}},
            {"character": {"character_id": "forged-character"}},
            SetValue(
                ConfigFieldPath.fields("character"),
                {"character_id": "forged-character"},
            ),
        ),
        (
            {"character": {"character_id": "forged-character"}},
            {},
            UnsetValue(ConfigFieldPath.fields("character")),
        ),
    ),
)
def test_runtime_derived_character_fields_cannot_be_bypassed_through_parent_operation(
    base,
    candidate,
    operation,
):
    validator = ConfigAuthoringValidator()

    with pytest.raises(AuthoringError) as rejected:
        validator.validate(base, candidate, (operation,))

    assert rejected.value.code == "READ_ONLY_FIELD"


@pytest.mark.parametrize(
    "replacement",
    (
        {"max_side": 5000},
        {"infer_timeout_sec": -1.0},
    ),
)
def test_nested_model_parent_operation_cannot_rely_on_owner_silent_normalization(
    replacement,
):
    validator = ConfigAuthoringValidator()

    with pytest.raises(AuthoringError) as rejected:
        validator.validate(
            {"screen": {"max_side": 768, "infer_timeout_sec": 30.0}},
            {"screen": replacement},
            (SetValue(ConfigFieldPath.fields("screen"), replacement),),
        )

    assert rejected.value.code == "PATH_INVALID"

from __future__ import annotations

import ast
import json
import os
from pathlib import Path
import stat
from typing import Any

import pytest

from spica.adapters.config_studio.platform import platform_capabilities_for
from spica.config.env_roster import APP_ENV_MAP, SCREEN_ENV_MAP
from spica.config.environment_snapshot import EnvironmentSnapshot
from spica.config.secrets import LoadedSecrets, Secrets, load_secrets


REPO_ROOT = Path(__file__).resolve().parents[1]
_PLATFORM = platform_capabilities_for(
    os_family="posix",
    runtime_name="linux",
    user_id=os.getuid(),
    temp_directory="/synthetic-config-studio-test-tmp",
)


def _synthetic_repository(tmp_path: Path) -> Path:
    repo_root = tmp_path / "synthetic-repo"
    config_dir = repo_root / "data" / "config"
    config_dir.mkdir(parents=True)
    (config_dir / "app.yaml").write_text(
        "llm:\n  model: file-model\ntts:\n  enabled: false\n",
        encoding="utf-8",
    )
    return repo_root


def _loaded_secrets(*, model: str = "snapshot-model") -> LoadedSecrets:
    return LoadedSecrets(
        secrets=Secrets(openai_api_key="synthetic-secret-canary"),
        environment_snapshot=EnvironmentSnapshot.from_mapping(
            {"MODEL": model},
            layer="synthetic_inherited",
        ),
    )


def _field(catalog: dict[str, Any], display_path: str) -> dict[str, Any]:
    return next(
        field
        for field in catalog["fields"]
        if field["display_path"] == display_path
    )


def _managed_override_status(*, defined: frozenset[str] = frozenset()):
    return [
        {
            "environment_variable": environment_variable,
            "affected_fields": [field_path],
            "repo_defined": environment_variable in defined,
        }
        for field_path, environment_variable in (
            *APP_ENV_MAP.items(),
            *((f"screen.{name}", value) for name, value in SCREEN_ENV_MAP.items()),
        )
    ]


def test_sidecar_backend_composition_is_not_owned_by_the_ui_package() -> None:
    assert (
        REPO_ROOT / "spica" / "adapters" / "config_studio" / "composition.py"
    ).is_file()
    assert (
        REPO_ROOT / "spica" / "config_studio" / "overlay_document.py"
    ).is_file()
    assert not (REPO_ROOT / "ui" / "config_studio" / "composition.py").exists()
    assert not (
        REPO_ROOT / "ui" / "config_studio" / "overlay_document.py"
    ).exists()
    script = (REPO_ROOT / "scripts" / "config_studio.py").read_text(
        encoding="utf-8"
    )
    assert "spica.adapters.config_studio.composition" in script
    assert "ui.config_studio.composition" not in script


def test_read_only_services_use_the_explicit_owner_snapshot_and_close_writes(
    tmp_path: Path,
) -> None:
    from spica.config_studio.services import ReadOnlyConfigStudioServices

    services = ReadOnlyConfigStudioServices(
        repo_root=_synthetic_repository(tmp_path),
        environment_snapshot=_loaded_secrets().environment_snapshot,
        background_health_code=None,
        platform_capabilities=_PLATFORM,
    )

    catalog = services.catalog()
    model = _field(catalog, "llm.model")

    assert model["file_value"] == "file-model"
    assert model["next_launch_value"] == "snapshot-model"
    assert model["source_kind"] == "env_override"
    assert model["environment_layer"] == "synthetic_inherited"
    assert services.meta() == {
        "service": "spica-config-studio",
        "mode": "read_only",
        "runtime_truth": "unavailable",
        "effect_policy": "next_spica_launch",
        "capabilities": {
            "app_config_write": False,
            "overlay_write": False,
            "sensitive_write": False,
            "rollback": False,
            "self_check": False,
            "self_check_jobs": False,
        },
        "sensitive_document": {
            "permission_health": "MISSING",
            "parse_health": "MISSING",
            "secret_slots": {
                "openai_api_key": False,
                "judge_api_key": False,
                "bilibili_cookie": False,
                "qbittorrent_password": False,
            },
                "legacy_entries": [],
                "managed_overrides": _managed_override_status(),
                "secret_sources": {
                "openai_api_key": None,
                "judge_api_key": None,
                "bilibili_cookie": None,
                "qbittorrent_password": None,
            },
        },
        "parent_environment_document": {
            "permission_health": "MISSING",
            "parse_health": "MISSING",
            "legacy_entries": [],
        },
        "health": {"recovery_only": False, "issues": []},
    }
    assert services.capability_enabled("app_config_write") is False
    assert services.capability_enabled("unknown") is False
    assert services.list_self_checks() == []

    rendered = json.dumps(
        {"meta": services.meta(), "catalog": catalog},
        ensure_ascii=False,
    )
    assert "synthetic-secret-canary" not in rendered
    assert "synthetic-secret-canary" not in repr(services)


def test_read_only_services_refresh_owner_inputs_for_each_catalog_and_meta_read(
    tmp_path: Path,
) -> None:
    from spica.config_studio.services import ReadOnlyConfigStudioServices

    current = [_loaded_secrets(model="first-snapshot")]
    services = ReadOnlyConfigStudioServices(
        repo_root=_synthetic_repository(tmp_path),
        environment_snapshot=current[0].environment_snapshot,
        background_health_code=None,
        platform_capabilities=_PLATFORM,
        environment_owner=lambda: current[0],
    )

    assert _field(services.catalog(), "llm.model")["next_launch_value"] == (
        "first-snapshot"
    )

    current[0] = LoadedSecrets(
        secrets=Secrets(judge_api_key="synthetic-refreshed-secret"),
        environment_snapshot=EnvironmentSnapshot.from_mapping(
            {"MODEL": "second-snapshot"},
            layer="repo_dotenv",
        ),
        secret_source_layers=(("JUDGE_API_KEY", "parent_dotenv"),),
    )

    refreshed = _field(services.catalog(), "llm.model")
    assert refreshed["next_launch_value"] == "second-snapshot"
    assert refreshed["environment_layer"] == "repo_dotenv"
    slots = services.meta()["sensitive_document"]["secret_slots"]
    assert slots["openai_api_key"] is False
    assert slots["judge_api_key"] is True
    assert services.meta()["sensitive_document"]["secret_sources"] == {
        "openai_api_key": None,
        "judge_api_key": "parent_dotenv",
        "bilibili_cookie": None,
        "qbittorrent_password": None,
    }


def test_self_check_job_observation_remains_available_after_start_latches_unsafe(
    tmp_path: Path,
) -> None:
    from spica.config.secrets import Secrets
    from spica.config_studio.self_check import (
        SelfCheckJobManager,
        SelfCheckMode,
        SelfCheckStderrSummary,
    )
    from spica.config_studio.self_check_service import (
        SelfCheckEnvironmentInputs,
        SelfCheckService,
    )
    from spica.config_studio.services import (
        ConfigStudioServiceError,
        ReadOnlyConfigStudioServices,
    )

    class UncontainedProcess:
        containment_established = False

        def wait(self, timeout_s: float) -> object:
            raise AssertionError("uncontained process must never be waited")

        def cancel(self) -> bool:
            return True

        def stderr_snapshot(self) -> SelfCheckStderrSummary:
            return SelfCheckStderrSummary()

    class UncontainedRunner:
        def start(self, argv: tuple[str, ...], environment: object) -> object:
            return UncontainedProcess()

    class LightCommand:
        mode = SelfCheckMode.LIGHT
        only: tuple[str, ...] = ()
        llm = False
        include_disabled = False
        allow_model_downloads = False

    repo_root = _synthetic_repository(tmp_path)
    script = repo_root / "scripts" / "self_check.py"
    script.parent.mkdir()
    script.write_text("# synthetic self-check owner\n", encoding="utf-8")
    self_check = SelfCheckService(
        script_path=script,
        job_manager=SelfCheckJobManager(runner=UncontainedRunner()),
        environment_inputs=lambda: SelfCheckEnvironmentInputs(
            environment_snapshot=EnvironmentSnapshot.from_mapping(
                {}, layer="synthetic"
            ),
            secrets=Secrets(),
        ),
    )
    services = ReadOnlyConfigStudioServices(
        repo_root=repo_root,
        environment_snapshot=EnvironmentSnapshot.from_mapping(
            {}, layer="synthetic"
        ),
        background_health_code=None,
        platform_capabilities=_PLATFORM,
        self_check_service=self_check,
    )

    terminal = services.start_self_check(LightCommand())
    job_id = str(terminal["job_id"])

    assert terminal["status"] == "INTERNAL_ERROR"
    assert self_check.available is False
    assert services.meta()["capabilities"] == {
        "app_config_write": False,
        "overlay_write": False,
        "sensitive_write": False,
        "rollback": False,
        "self_check": False,
        "self_check_jobs": True,
    }
    assert services.list_self_checks()[0]["job_id"] == job_id
    assert services.get_self_check(job_id)["status"] == "INTERNAL_ERROR"
    assert services.cancel_self_check(job_id)["status"] == "INTERNAL_ERROR"
    with pytest.raises(ConfigStudioServiceError) as disabled:
        services.start_self_check(LightCommand())
    assert disabled.value.code == "CAPABILITY_UNAVAILABLE"


def test_service_projects_sensitive_document_health_without_values(
    tmp_path: Path,
) -> None:
    from spica.config_studio.services import ReadOnlyConfigStudioServices

    repo_root = _synthetic_repository(tmp_path)
    env_path = repo_root / "xiaosan.env"
    env_path.write_bytes(
        b"DEEPSEEK_API_KEY=legacy-secret-canary\n"
        b"BROKEN='unterminated\n"
    )
    env_path.chmod(0o664)
    services = ReadOnlyConfigStudioServices(
        repo_root=repo_root,
        environment_snapshot=EnvironmentSnapshot.from_mapping(
            {}, layer="synthetic"
        ),
        secrets=Secrets(openai_api_key="effective-secret-canary"),
        background_health_code=None,
        platform_capabilities=_PLATFORM,
    )

    meta = services.meta()

    assert meta["sensitive_document"] == {
        "permission_health": "TOO_PERMISSIVE",
        "parse_health": "INVALID",
        "secret_slots": {
            "openai_api_key": True,
            "judge_api_key": False,
            "bilibili_cookie": False,
            "qbittorrent_password": False,
        },
        "legacy_entries": ["DEEPSEEK_API_KEY"],
        "managed_overrides": _managed_override_status(),
        "secret_sources": {
            "openai_api_key": None,
            "judge_api_key": None,
            "bilibili_cookie": None,
            "qbittorrent_password": None,
        },
    }
    assert [item["code"] for item in meta["health"]["issues"]] == [
        "SENSITIVE_DOCUMENT_PERMISSION_TOO_PERMISSIVE",
        "SENSITIVE_DOCUMENT_PARSE_INVALID",
        "LEGACY_ENV_ENTRY_PRESENT",
    ]
    rendered = json.dumps(meta, ensure_ascii=False)
    assert "legacy-secret-canary" not in rendered
    assert "effective-secret-canary" not in rendered


def test_service_projects_parent_env_health_without_paths_values_or_slots(
    tmp_path: Path,
) -> None:
    from spica.config_studio.services import ReadOnlyConfigStudioServices

    repo_root = _synthetic_repository(tmp_path)
    parent_env = repo_root.parent / "xiaosan.env"
    parent_env.write_text(
        "OPENAI_API_KEY=parent-secret-canary\nDEEPSEEK_API_KEY=legacy\n",
        encoding="utf-8",
    )
    parent_env.chmod(0o600)
    services = ReadOnlyConfigStudioServices(
        repo_root=repo_root,
        environment_snapshot=EnvironmentSnapshot.from_mapping(
            {}, layer="synthetic"
        ),
        background_health_code=None,
        platform_capabilities=_PLATFORM,
    )

    meta = services.meta()

    assert meta["parent_environment_document"] == {
        "permission_health": "PRIVATE",
        "parse_health": "VALID",
        "legacy_entries": ["DEEPSEEK_API_KEY"],
    }
    rendered = json.dumps(meta, ensure_ascii=False)
    assert "parent-secret-canary" not in rendered
    assert str(parent_env) not in rendered


def test_read_only_services_fail_closed_to_recovery_catalog_on_invalid_yaml(
    tmp_path: Path,
) -> None:
    from spica.config_studio.services import ReadOnlyConfigStudioServices

    repo_root = _synthetic_repository(tmp_path)
    (repo_root / "data" / "config" / "app.yaml").write_text(
        "llm: [unterminated\n",
        encoding="utf-8",
    )
    services = ReadOnlyConfigStudioServices(
        repo_root=repo_root,
        environment_snapshot=_loaded_secrets().environment_snapshot,
        background_health_code="BACKGROUND_ASSET_INVALID",
        platform_capabilities=_PLATFORM,
    )

    assert services.catalog() == {
        "fields": [],
        "truncation": {
            "strings": 0,
            "collections": 0,
            "depth": 0,
            "unsupported": 0,
            "total_bytes": 0,
        },
        "recovery_only": True,
    }
    assert services.meta()["health"] == {
        "recovery_only": True,
        "issues": [
            {
                "code": "BACKGROUND_ASSET_INVALID",
                "message": "Decorative background failed integrity validation.",
            },
            {
                "code": "CONFIG_RESOLUTION_ERROR",
                "message": "app.yaml cannot be resolved; only recovery is available.",
            },
        ],
    }


def test_read_only_services_treat_non_utf8_app_document_as_recovery_only(
    tmp_path: Path,
) -> None:
    from spica.config_studio.services import ReadOnlyConfigStudioServices

    repo_root = _synthetic_repository(tmp_path)
    (repo_root / "data" / "config" / "app.yaml").write_bytes(b"llm:\n  model: \xff\n")
    services = ReadOnlyConfigStudioServices(
        repo_root=repo_root,
        environment_snapshot=_loaded_secrets().environment_snapshot,
        background_health_code=None,
        platform_capabilities=_PLATFORM,
    )

    assert services.catalog()["recovery_only"] is True
    assert services.meta()["health"]["recovery_only"] is True


@pytest.mark.parametrize("invalid_root", ["[]\n", "null\n"])
def test_service_treats_non_mapping_app_root_as_recovery_only(
    tmp_path: Path,
    invalid_root: str,
) -> None:
    from spica.config_studio.services import ReadOnlyConfigStudioServices

    repo_root = _synthetic_repository(tmp_path)
    (repo_root / "data" / "config" / "app.yaml").write_text(
        invalid_root,
        encoding="utf-8",
    )
    services = ReadOnlyConfigStudioServices(
        repo_root=repo_root,
        environment_snapshot=EnvironmentSnapshot.from_mapping(
            {}, layer="synthetic"
        ),
        background_health_code=None,
        platform_capabilities=_PLATFORM,
    )

    assert services.catalog()["recovery_only"] is True
    assert services.meta()["health"]["recovery_only"] is True


def test_service_app_reader_rejects_symlink_without_exposing_target(tmp_path: Path) -> None:
    from spica.config_studio.services import ReadOnlyConfigStudioServices

    repo_root = _synthetic_repository(tmp_path)
    app_path = repo_root / "data" / "config" / "app.yaml"
    outside = tmp_path / "outside-app.yaml"
    canary = "outside-app-canary"
    outside.write_text(f"llm:\n  model: {canary}\n", encoding="utf-8")
    app_path.unlink()
    app_path.symlink_to(outside)
    services = ReadOnlyConfigStudioServices(
        repo_root=repo_root,
        environment_snapshot=EnvironmentSnapshot.from_mapping(
            {}, layer="synthetic"
        ),
        background_health_code=None,
        platform_capabilities=_PLATFORM,
    )

    encoded = json.dumps(
        {"meta": services.meta(), "catalog": services.catalog()},
        ensure_ascii=False,
    )

    assert services.catalog()["recovery_only"] is True
    assert canary not in encoded
    assert services.meta()["health"]["issues"] == [
        {
            "code": "CONFIG_RESOLUTION_ERROR",
            "message": "app.yaml cannot be resolved; only recovery is available.",
        }
    ]


@pytest.mark.parametrize(
    "unsafe_yaml",
    [
        "future: &cycle\n  child: *cycle\n",
        "future:\n"
        + "".join("  " * depth + "child:\n" for depth in range(1, 71))
        + "  " * 71
        + "value: deep\n",
    ],
)
def test_service_bounds_yaml_alias_cycles_and_excessive_depth(
    tmp_path: Path,
    unsafe_yaml: str,
) -> None:
    from spica.config_studio.services import ReadOnlyConfigStudioServices

    repo_root = _synthetic_repository(tmp_path)
    (repo_root / "data" / "config" / "app.yaml").write_text(
        unsafe_yaml,
        encoding="utf-8",
    )
    services = ReadOnlyConfigStudioServices(
        repo_root=repo_root,
        environment_snapshot=EnvironmentSnapshot.from_mapping(
            {}, layer="synthetic"
        ),
        background_health_code=None,
        platform_capabilities=_PLATFORM,
    )

    assert services.catalog()["recovery_only"] is True
    assert services.meta()["health"]["recovery_only"] is True


def test_service_rejects_duplicate_yaml_mapping_keys_as_recovery_only(
    tmp_path: Path,
) -> None:
    from spica.config_studio.services import ReadOnlyConfigStudioServices

    repo_root = _synthetic_repository(tmp_path)
    (repo_root / "data" / "config" / "app.yaml").write_text(
        "llm:\n  model: first\n  model: silently-wins-with-safe-load\n",
        encoding="utf-8",
    )
    services = ReadOnlyConfigStudioServices(
        repo_root=repo_root,
        environment_snapshot=EnvironmentSnapshot.from_mapping(
            {}, layer="synthetic"
        ),
        background_health_code=None,
        platform_capabilities=_PLATFORM,
    )

    assert services.catalog()["recovery_only"] is True
    assert services.meta()["health"]["recovery_only"] is True


def test_service_keeps_unique_yaml_aliases_for_bounded_read_only_reporting(
    tmp_path: Path,
) -> None:
    from spica.config_studio.services import ReadOnlyConfigStudioServices

    repo_root = _synthetic_repository(tmp_path)
    (repo_root / "data" / "config" / "app.yaml").write_text(
        "future_owner:\n"
        "  first: &shared\n"
        "    mode: visible\n"
        "  second: *shared\n",
        encoding="utf-8",
    )
    services = ReadOnlyConfigStudioServices(
        repo_root=repo_root,
        environment_snapshot=EnvironmentSnapshot.from_mapping(
            {}, layer="synthetic"
        ),
        background_health_code=None,
        platform_capabilities=_PLATFORM,
    )

    catalog = services.catalog()

    assert catalog["recovery_only"] is False
    assert catalog["truncation"]["aliases"] == 1
    fields = {
        field["display_path"]: field["file_value"]
        for field in catalog["fields"]
        if field["display_path"].startswith("['future_owner']")
    }
    assert fields == {
        "['future_owner']['first']['mode']": "visible",
        "['future_owner']['second']": "<alias-reference>",
    }


def test_service_catalog_recursively_redacts_secret_canaries_from_values_and_keys(
    tmp_path: Path,
) -> None:
    from spica.config_studio.services import ReadOnlyConfigStudioServices

    secret = "synthetic-secret-canary"
    services = ReadOnlyConfigStudioServices(
        repo_root=_synthetic_repository(tmp_path),
        environment_snapshot=EnvironmentSnapshot.from_mapping(
            {"MODEL": secret},
            layer="synthetic_inherited",
        ),
        secrets=Secrets(openai_api_key=secret),
        background_health_code=None,
        platform_capabilities=_PLATFORM,
    )

    encoded = json.dumps(services.catalog(), ensure_ascii=False)

    assert secret not in encoded
    assert "REDACTED:OPENAI_API_KEY" in encoded


def test_service_does_not_mislabel_a_quarantined_env_winner_as_file_value(
    tmp_path: Path,
) -> None:
    from spica.config_studio.services import ReadOnlyConfigStudioServices

    services = ReadOnlyConfigStudioServices(
        repo_root=_synthetic_repository(tmp_path),
        environment_snapshot=EnvironmentSnapshot.from_layers(
            inherited={},
            repo_dotenv={},
            parent_dotenv={},
            tainted={"MODEL": "repo_dotenv"},
        ),
        tainted_environment_names=("MODEL",),
        background_health_code=None,
        platform_capabilities=_PLATFORM,
    )

    model = _field(services.catalog(), "llm.model")

    assert model["next_launch_value"] is None
    assert model["source_kind"] == "secret_tainted_env_override"
    assert model["environment_variable"] == "MODEL"
    assert model["environment_layer"] == "repo_dotenv"
    assert model["file_value_shadowed"] is True


def test_short_secret_canary_redacts_data_without_mangling_wire_schema_keys(
    tmp_path: Path,
) -> None:
    from spica.config_studio.services import ReadOnlyConfigStudioServices

    repo_root = _synthetic_repository(tmp_path)
    (repo_root / "data" / "config" / "app.yaml").write_text(
        "llm:\n  model: a\n",
        encoding="utf-8",
    )
    services = ReadOnlyConfigStudioServices(
        repo_root=repo_root,
        environment_snapshot=EnvironmentSnapshot.from_mapping(
            {}, layer="synthetic_inherited"
        ),
        secrets=Secrets(openai_api_key="a"),
        background_health_code=None,
        platform_capabilities=_PLATFORM,
    )

    catalog = services.catalog()
    model = _field(catalog, "llm.model")

    assert "fields" in catalog
    assert "truncation" in catalog
    assert "path" in model
    assert "next_launch_value" in model
    assert model["file_value"] == "«REDACTED:OPENAI_API_KEY»"
    assert model["next_launch_value"] == "«REDACTED:OPENAI_API_KEY»"


def test_retired_legacy_secret_canary_is_redacted_without_becoming_a_secret_slot(
    tmp_path: Path,
) -> None:
    from spica.config_studio.services import ReadOnlyConfigStudioServices

    secret = "legacy-super-secret"
    repo_root = _synthetic_repository(tmp_path)
    (repo_root / "data" / "config" / "app.yaml").write_text(
        f"llm:\n  model: {secret}\n",
        encoding="utf-8",
    )
    services = ReadOnlyConfigStudioServices(
        repo_root=repo_root,
        environment_snapshot=EnvironmentSnapshot.from_mapping(
            {}, layer="synthetic_inherited"
        ),
        legacy_secret_canaries=(("DEEPSEEK_API_KEY", secret),),
        background_health_code=None,
        platform_capabilities=_PLATFORM,
    )

    encoded = json.dumps(services.catalog(), ensure_ascii=False)

    assert secret not in encoded
    assert "REDACTED:DEEPSEEK_API_KEY" in encoded


class _FakeBoundServer:
    def __init__(self, trace: list[object], *, port: int) -> None:
        self.host = "127.0.0.1"
        self.port = port
        self.socket = object()
        self._trace = trace

    def __enter__(self) -> "_FakeBoundServer":
        self._trace.append("server-enter")
        return self

    def __exit__(self, *args: object) -> None:
        self._trace.append("server-close")

    def uvicorn_config(self, app: object) -> tuple[str, object]:
        self._trace.append(("app", app))
        return ("uvicorn-config", app)


class _FakeUvicornServer:
    def __init__(self, trace: list[object], config: object) -> None:
        self._trace = trace
        self._trace.append(("uvicorn", config))

    def run(self, *, sockets: list[object]) -> None:
        self._trace.append(("run", sockets))


def test_cli_loads_snapshot_before_composition_and_opens_only_a_fragment_bootstrap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts import config_studio

    trace: list[object] = []
    bound_servers: list[_FakeBoundServer] = []
    self_check_inputs: list[object] = []
    repo_root = _synthetic_repository(tmp_path)

    def fake_load_secrets(
        *,
        with_environment_snapshot: bool,
        prime_process: bool,
    ) -> LoadedSecrets:
        trace.append(("load-secrets", with_environment_snapshot, prime_process))
        return _loaded_secrets()

    def fake_bind(*, host: str, port: int) -> _FakeBoundServer:
        trace.append(("bind", host, port))
        bound = _FakeBoundServer(trace, port=port)
        bound_servers.append(bound)
        return bound

    def server_factory(config: object) -> _FakeUvicornServer:
        return _FakeUvicornServer(trace, config)

    def browser_open(url: str, *, new: int) -> bool:
        trace.append(("browser", url, new))
        return True

    bootstrap_token = "synthetic-bootstrap-token-opaque"
    generated = iter(
        [bootstrap_token, "unused-session-token-opaque", "unused-csrf-token-opaque"]
    )
    scheduled: list[tuple[float, object]] = []
    monkeypatch.setattr(config_studio, "load_secrets", fake_load_secrets)
    def fake_self_check_service(
        *,
        repo_root,
        environment_inputs,
        platform_capabilities,
        runner,
        base_child_environment,
    ):
        del repo_root, platform_capabilities
        self_check_inputs.append(
            (environment_inputs(), runner, base_child_environment)
        )
        return None

    monkeypatch.setattr(
        config_studio,
        "create_production_self_check_service",
        fake_self_check_service,
    )

    result = config_studio.main(
        ["--port", "9123"],
        repo_root=repo_root,
        server_bind=fake_bind,
        server_factory=server_factory,
        browser_open=browser_open,
        token_factory=lambda: next(generated),
        fallback_scheduler=lambda delay, callback: scheduled.append(
            (delay, callback)
        ),
        background_health_code=None,
        platform_capabilities=_PLATFORM,
    )

    assert result == 0
    assert trace[0] == ("load-secrets", True, False)
    assert trace[1] == ("bind", "127.0.0.1", 9123)
    browser_event = next(
        item
        for item in trace
        if isinstance(item, tuple) and item[0] == "browser"
    )
    assert browser_event == (
        "browser",
        f"http://127.0.0.1:9123/#bootstrap={bootstrap_token}",
        2,
    )
    assert "?bootstrap=" not in browser_event[1]
    run_event = next(
        item for item in trace if isinstance(item, tuple) and item[0] == "run"
    )
    assert run_event[1] == [bound_servers[0].socket]
    assert trace[-1] == "server-close"
    assert len(self_check_inputs) == 1
    environment_inputs = self_check_inputs[0][0]
    assert repr(environment_inputs) == "SelfCheckEnvironmentInputs(<redacted>)"
    assert environment_inputs.secret_material_sanitizer is not None
    sanitized = environment_inputs.secret_material_sanitizer(
        "prefix-synthetic-secret-canary-suffix"
    )
    assert "synthetic-secret-canary" not in sanitized
    assert "«REDACTED:OPENAI_API_KEY»" in sanitized
    assert self_check_inputs[0][1].__class__.__name__ == "SubprocessSelfCheckRunner"
    assert self_check_inputs[0][2]["HOME"]
    assert len(scheduled) == 1
    assert scheduled[0][0] > 0


def test_verified_linux_cli_exposes_only_fixed_owner_write_capabilities(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("ruamel.yaml")
    from fastapi.testclient import TestClient
    from scripts import config_studio

    repo_root = _synthetic_repository(tmp_path)
    overlay_path = repo_root / "ui" / "overlay_config.json"
    overlay_path.parent.mkdir()
    overlay_path.write_bytes(b'{"spica_voice_volume": 0.5}\n')
    repo_env_path = repo_root / "xiaosan.env"
    repo_env_path.write_bytes(
        b"MODEL=repo-model\nOPENAI_BASE_URL=https://repo.invalid/v1\n"
    )
    repo_env_path.chmod(0o600)
    parent_env_path = tmp_path / "sandbox-parent" / "xiaosan.env"
    parent_env_path.parent.mkdir()
    parent_env_path.write_bytes(b"")
    parent_env_path.chmod(0o600)
    loaded = load_secrets(
        with_environment_snapshot=True,
        inherited_environment={},
        repo_env_path=repo_env_path,
        parent_env_path=parent_env_path,
        prime_process=False,
    )
    assert isinstance(loaded, LoadedSecrets)
    monkeypatch.setattr(config_studio, "load_secrets", lambda **_kwargs: loaded)
    monkeypatch.setattr(
        config_studio,
        "create_production_self_check_service",
        lambda **_kwargs: None,
    )
    bootstrap_token = "linux-writer-bootstrap-token"
    generated = iter(
        [
            bootstrap_token,
            "linux-writer-session-token",
            "linux-writer-csrf-token-value",
        ]
    )
    observed: dict[str, object] = {}

    class InspectingServer:
        def __init__(self, config: tuple[str, object]) -> None:
            observed["config"] = config

        def run(self, *, sockets: list[object]) -> None:
            del sockets
            app = observed["config"][1]  # type: ignore[index]
            with TestClient(
                app,
                base_url="http://127.0.0.1:9124",
            ) as client:
                bootstrap = client.post(
                    "/api/v1/session/bootstrap",
                    headers={
                        "Origin": "http://127.0.0.1:9124",
                        "X-Spica-Bootstrap": bootstrap_token,
                    },
                )
                bootstrap.raise_for_status()
                csrf = bootstrap.json()["csrf_token"]
                write_headers = {
                    "Origin": "http://127.0.0.1:9124",
                    "X-Spica-CSRF": csrf,
                }
                observed["meta"] = client.get("/api/v1/meta").json()
                observed["state_absent_before_write"] = not (
                    repo_root / "spica_data" / "config_studio"
                ).exists()
                sensitive_preview = client.post(
                    "/api/v1/sensitive/previews",
                    headers=write_headers,
                    json={
                        "command": {
                            "kind": "clear_mapped_override",
                            "environment_variable": "MODEL",
                        }
                    },
                )
                assert sensitive_preview.status_code == 200, sensitive_preview.json()
                sensitive_commit = client.post(
                    "/api/v1/sensitive/commits",
                    headers=write_headers,
                    json={"preview_id": sensitive_preview.json()["preview_id"]},
                )
                sensitive_commit.raise_for_status()
                app_preview = client.post(
                    "/api/v1/app/previews",
                    headers=write_headers,
                    json={
                        "operations": [
                            {
                                "kind": "set",
                                "path": [
                                    {"kind": "field", "name": "llm"},
                                    {"kind": "field", "name": "model"},
                                ],
                                "value": "new-file-model",
                            },
                            {
                                "kind": "set",
                                "path": [
                                    {"kind": "field", "name": "llm"},
                                    {"kind": "field", "name": "base_url"},
                                ],
                                "value": "https://new-file.invalid/v1",
                            },
                        ]
                    },
                )
                app_preview.raise_for_status()
                observed["app_preview"] = app_preview.json()
                app_commit = client.post(
                    "/api/v1/app/commits",
                    headers=write_headers,
                    json={"preview_id": app_preview.json()["preview_id"]},
                )
                app_commit.raise_for_status()
                base_url_preview = client.post(
                    "/api/v1/sensitive/previews",
                    headers=write_headers,
                    json={
                        "command": {
                            "kind": "clear_mapped_override",
                            "environment_variable": "OPENAI_BASE_URL",
                        },
                    },
                )
                base_url_preview.raise_for_status()
                observed["base_url_preview"] = base_url_preview.json()

    config_studio.main(
        ["--port", "9124", "--no-open-browser"],
        repo_root=repo_root,
        server_bind=lambda *, host, port: _FakeBoundServer([], port=port),
        server_factory=InspectingServer,
        token_factory=lambda: next(generated),
        terminal_write=lambda _message: None,
        background_health_code=None,
        platform_capabilities=_PLATFORM,
    )

    meta = observed["meta"]
    assert isinstance(meta, dict)
    assert meta["mode"] == "owner_backed"
    assert meta["capabilities"] == {
        "app_config_write": True,
        "overlay_write": True,
        "sensitive_write": True,
        "rollback": True,
        "self_check": False,
        "self_check_jobs": False,
    }
    assert observed["state_absent_before_write"] is True
    app_change = observed["app_preview"]["changes"][0]  # type: ignore[index]
    assert app_change["next_launch_value_before"] == "file-model"
    assert app_change["next_launch_value_after"] == "new-file-model"
    base_url_preview = observed["base_url_preview"]
    assert base_url_preview["before_next_launch"] == "https://repo.invalid/v1"  # type: ignore[index]
    assert base_url_preview["after_next_launch"] == "https://new-file.invalid/v1"  # type: ignore[index]
    assert repo_env_path.read_bytes() == (
        b"OPENAI_BASE_URL=https://repo.invalid/v1\n"
    )


def test_linux_cli_does_not_advertise_an_unsafe_overlay_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("ruamel.yaml")
    from fastapi.testclient import TestClient
    from scripts import config_studio

    repo_root = _synthetic_repository(tmp_path)
    outside = tmp_path / "outside-overlay.json"
    outside.write_bytes(b'{"spica_voice_volume": 0.5}\n')
    overlay_path = repo_root / "ui" / "overlay_config.json"
    overlay_path.parent.mkdir()
    overlay_path.symlink_to(outside)
    repo_env_path = repo_root / "xiaosan.env"
    repo_env_path.write_bytes(b"")
    repo_env_path.chmod(0o600)
    parent_env_path = tmp_path / "sandbox-parent" / "xiaosan.env"
    parent_env_path.parent.mkdir()
    parent_env_path.write_bytes(b"")
    parent_env_path.chmod(0o600)
    loaded = load_secrets(
        with_environment_snapshot=True,
        inherited_environment={},
        repo_env_path=repo_env_path,
        parent_env_path=parent_env_path,
        prime_process=False,
    )
    assert isinstance(loaded, LoadedSecrets)
    monkeypatch.setattr(config_studio, "load_secrets", lambda **_kwargs: loaded)
    monkeypatch.setattr(
        config_studio,
        "create_production_self_check_service",
        lambda **_kwargs: None,
    )
    generated = iter(
        [
            "unsafe-overlay-bootstrap-token",
            "unsafe-overlay-session-token",
            "unsafe-overlay-csrf-token-value",
        ]
    )
    observed: dict[str, object] = {}

    class InspectingServer:
        def __init__(self, config: tuple[str, object]) -> None:
            self.app = config[1]

        def run(self, *, sockets: list[object]) -> None:
            del sockets
            with TestClient(
                self.app,
                base_url="http://127.0.0.1:9125",
            ) as client:
                bootstrap = client.post(
                    "/api/v1/session/bootstrap",
                    headers={
                        "Origin": "http://127.0.0.1:9125",
                        "X-Spica-Bootstrap": "unsafe-overlay-bootstrap-token",
                    },
                )
                bootstrap.raise_for_status()
                observed["meta"] = client.get("/api/v1/meta").json()

    config_studio.main(
        ["--port", "9125", "--no-open-browser"],
        repo_root=repo_root,
        server_bind=lambda *, host, port: _FakeBoundServer([], port=port),
        server_factory=InspectingServer,
        token_factory=lambda: next(generated),
        terminal_write=lambda _message: None,
        background_health_code=None,
        platform_capabilities=_PLATFORM,
    )

    capabilities = observed["meta"]["capabilities"]  # type: ignore[index]
    assert capabilities["app_config_write"] is True
    assert capabilities["overlay_write"] is False
    assert capabilities["sensitive_write"] is True
    assert capabilities["rollback"] is True
    assert outside.read_bytes() == b'{"spica_voice_volume": 0.5}\n'
    assert not (repo_root / "spica_data" / "config_studio").exists()


def test_cli_no_open_browser_keeps_default_loopback_port(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts import config_studio

    trace: list[object] = []
    terminal: list[str] = []
    bootstrap_token = "manual-bootstrap-token-opaque"
    monkeypatch.setattr(
        config_studio,
        "load_secrets",
        lambda **_kwargs: _loaded_secrets(),
    )

    config_studio.main(
        ["--no-open-browser"],
        repo_root=_synthetic_repository(tmp_path),
        server_bind=lambda *, host, port: (
            trace.append(("bind", host, port))
            or _FakeBoundServer(trace, port=port)
        ),
        server_factory=lambda config: _FakeUvicornServer(trace, config),
        browser_open=lambda *_args, **_kwargs: trace.append("browser"),
        token_factory=lambda: bootstrap_token,
        terminal_write=terminal.append,
        background_health_code=None,
        platform_capabilities=_PLATFORM,
    )

    assert ("bind", "127.0.0.1", 8765) in trace
    assert "browser" not in trace
    output = "\n".join(terminal)
    assert "http://127.0.0.1:8765/" in output
    assert bootstrap_token in output
    assert "paste" in output.lower()


def test_cli_browser_failure_prints_the_same_manual_grant_immediately(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts import config_studio

    terminal: list[str] = []
    bootstrap_token = "failed-browser-bootstrap-token-opaque"
    monkeypatch.setattr(
        config_studio,
        "load_secrets",
        lambda **_kwargs: _loaded_secrets(),
    )

    config_studio.main(
        [],
        repo_root=_synthetic_repository(tmp_path),
        server_bind=lambda **_kwargs: _FakeBoundServer([], port=8765),
        server_factory=lambda config: _FakeUvicornServer([], config),
        browser_open=lambda *_args, **_kwargs: False,
        token_factory=lambda: bootstrap_token,
        terminal_write=terminal.append,
        fallback_scheduler=lambda _delay, _callback: pytest.fail(
            "a failed browser launch must not wait to show the fallback"
        ),
        background_health_code=None,
        platform_capabilities=_PLATFORM,
    )

    output = "\n".join(terminal)
    assert bootstrap_token in output
    assert "http://127.0.0.1:8765/" in output


def test_delayed_terminal_fallback_is_suppressed_after_grant_redemption() -> None:
    from scripts import config_studio
    from spica.config_studio.security import SecurityContext

    bootstrap_token = "redeemed-bootstrap-token-opaque"
    context = SecurityContext(
        host="127.0.0.1",
        port=8765,
        bootstrap_token=bootstrap_token,
        token_factory=iter(
            ["redeemed-session-token-opaque", "redeemed-csrf-token-opaque"]
        ).__next__,
    )
    assert context.exchange_bootstrap(bootstrap_token) is not None
    terminal: list[str] = []

    config_studio._write_terminal_fallback_if_pending(
        security_context=context,
        bootstrap_token=bootstrap_token,
        terminal_write=terminal.append,
    )

    assert terminal == []


@pytest.mark.parametrize("port", ["0", "1023", "65536", "not-a-port"])
def test_cli_rejects_ports_outside_the_production_range_after_priming(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    port: str,
) -> None:
    from scripts import config_studio

    trace: list[str] = []
    monkeypatch.setattr(
        config_studio,
        "load_secrets",
        lambda **_kwargs: (trace.append("load-secrets") or _loaded_secrets()),
    )

    with pytest.raises(SystemExit) as raised:
        config_studio.main(
            ["--port", port, "--no-open-browser"],
            repo_root=_synthetic_repository(tmp_path),
            server_bind=lambda **_kwargs: trace.append("bind"),
            background_health_code=None,
            platform_capabilities=_PLATFORM,
        )

    assert raised.value.code == 2
    assert trace == ["load-secrets"]


def test_config_studio_entry_primes_first_and_never_reaches_app_host() -> None:
    script_path = REPO_ROOT / "scripts" / "config_studio.py"
    service_path = REPO_ROOT / "spica" / "config_studio" / "services.py"
    script_tree = ast.parse(script_path.read_text(encoding="utf-8"))
    main = next(
        node
        for node in script_tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "main"
    )
    first = main.body[0]
    assert isinstance(first, ast.Assign)
    assert isinstance(first.value, ast.Call)
    assert isinstance(first.value.func, ast.Name)
    assert first.value.func.id == "load_secrets"
    assert any(
        keyword.arg == "with_environment_snapshot"
        and isinstance(keyword.value, ast.Constant)
        and keyword.value.value is True
        for keyword in first.value.keywords
    )

    for path in (script_path, service_path):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imported_modules = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        } | {
            node.module or ""
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        }
        assert "spica.host.app_host" not in imported_modules
        assert not any(
            isinstance(node, ast.Name) and node.id == "AppHost"
            for node in ast.walk(tree)
        )


def _synthetic_composition_repository(tmp_path: Path) -> tuple[Path, LoadedSecrets]:
    from spica.config.secrets import load_secrets

    repo_root = tmp_path / "synthetic-composition-repo"
    app_path = repo_root / "data" / "config" / "app.yaml"
    app_path.parent.mkdir(parents=True)
    app_path.write_text("max_tool_rounds: 2\n", encoding="utf-8")
    overlay_path = repo_root / "ui" / "overlay_config.json"
    overlay_path.parent.mkdir(parents=True)
    overlay_path.write_text('{"spica_voice_volume": 0.5}\n', encoding="utf-8")
    (repo_root / "spica_data").mkdir()
    loaded = load_secrets(
        with_environment_snapshot=True,
        inherited_environment={},
        repo_env_path=repo_root / "xiaosan.env",
        parent_env_path=repo_root.parent / "xiaosan.env",
        prime_process=False,
    )
    assert isinstance(loaded, LoadedSecrets)
    return repo_root, loaded


@pytest.mark.parametrize(
    ("lane", "relative_path"),
    (
        ("app", Path("data/config/app.yaml")),
        ("overlay", Path("ui/overlay_config.json")),
    ),
)
def test_production_composition_closes_only_the_hardlinked_ordinary_writer_lane(
    tmp_path: Path,
    lane: str,
    relative_path: Path,
) -> None:
    from spica.adapters.config_studio.composition import (
        create_production_config_studio_services,
    )

    repo_root, loaded = _synthetic_composition_repository(tmp_path)
    managed_path = repo_root / relative_path
    outside = tmp_path / f"outside-{lane}-document"
    canary = f"private-{lane}-hardlink-canary"
    if lane == "app":
        outside.write_text(f"llm:\n  model: {canary}\n", encoding="utf-8")
    else:
        outside.write_text(
            '{"spica_voice_volume": 0.5, "private_key": "'
            + canary
            + '"}\n',
            encoding="utf-8",
        )
    managed_path.unlink()
    os.link(outside, managed_path)

    services = create_production_config_studio_services(
        repo_root=repo_root,
        loaded_environment=loaded,
        environment_owner=lambda: loaded,
        platform_capabilities=platform_capabilities_for(
            os_family="posix",
            runtime_name="linux",
            user_id=os.getuid(),
            temp_directory=tmp_path / "platform-tmp",
        ),
        background_health_code=None,
        self_check_service=None,
    )
    catalog = services.catalog()
    catalog_text = json.dumps(catalog, ensure_ascii=False)

    capability = "app_config_write" if lane == "app" else "overlay_write"
    assert services.capability_enabled(capability) is False
    other_capability = "overlay_write" if lane == "app" else "app_config_write"
    assert services.capability_enabled(other_capability) is True
    assert services.capability_enabled("rollback") is True
    assert catalog["recovery_only"] is (lane == "app")
    if lane == "overlay":
        overlay = next(
            item
            for item in catalog["managed_documents"]
            if item["id"] == "overlay_preferences"
        )
        assert overlay["health"] == {
            "status": "unsafe",
            "code": "MANAGED_DOCUMENT_UNSAFE",
        }
    assert canary not in catalog_text
    assert managed_path.stat().st_nlink == 2
    assert canary in outside.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("lane", "relative_path"),
    (
        ("app", Path("data/config/app.yaml")),
        ("overlay", Path("ui/overlay_config.json")),
    ),
)
def test_production_composition_closes_only_the_wrong_owner_ordinary_writer_lane(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    lane: str,
    relative_path: Path,
) -> None:
    from spica.adapters.config_studio.composition import (
        create_production_config_studio_services,
    )

    repo_root, loaded = _synthetic_composition_repository(tmp_path)
    managed_path = repo_root / relative_path
    real_lstat = Path.lstat

    def lstat_with_wrong_managed_owner(path: Path):
        result = real_lstat(path)
        if path == managed_path:
            values = list(result)
            values[4] = os.getuid() + 1
            return os.stat_result(values)
        return result

    monkeypatch.setattr(Path, "lstat", lstat_with_wrong_managed_owner)
    services = create_production_config_studio_services(
        repo_root=repo_root,
        loaded_environment=loaded,
        environment_owner=lambda: loaded,
        platform_capabilities=platform_capabilities_for(
            os_family="posix",
            runtime_name="linux",
            user_id=os.getuid(),
            temp_directory=tmp_path / "platform-tmp",
        ),
        background_health_code=None,
        self_check_service=None,
    )

    capability = "app_config_write" if lane == "app" else "overlay_write"
    other_capability = "overlay_write" if lane == "app" else "app_config_write"
    assert services.capability_enabled(capability) is False
    assert services.capability_enabled(other_capability) is True
    assert services.capability_enabled("rollback") is True
    catalog = services.catalog()
    assert catalog["recovery_only"] is (lane == "app")
    if lane == "overlay":
        overlay = next(
            item
            for item in catalog["managed_documents"]
            if item["id"] == "overlay_preferences"
        )
        assert overlay["health"] == {
            "status": "unsafe",
            "code": "MANAGED_DOCUMENT_UNSAFE",
        }


def test_production_composition_closes_all_writes_for_a_symlinked_state_root(
    tmp_path: Path,
) -> None:
    from spica.adapters.config_studio.composition import (
        create_production_config_studio_services,
    )

    repo_root, loaded = _synthetic_composition_repository(tmp_path)
    outside = tmp_path / "outside-state"
    outside.mkdir(mode=0o700)
    state_root = repo_root / "spica_data" / "config_studio"
    state_root.symlink_to(outside, target_is_directory=True)

    services = create_production_config_studio_services(
        repo_root=repo_root,
        loaded_environment=loaded,
        environment_owner=loaded.refresh,
        platform_capabilities=platform_capabilities_for(
            os_family="posix",
            runtime_name="linux",
            user_id=os.getuid(),
            temp_directory=tmp_path / "platform-tmp",
        ),
        background_health_code=None,
        self_check_service=None,
    )

    assert services.meta()["capabilities"] == {
        "app_config_write": False,
        "overlay_write": False,
        "sensitive_write": False,
        "rollback": False,
        "self_check": False,
        "self_check_jobs": False,
    }
    assert state_root.is_symlink()
    assert stat.S_IMODE(outside.stat().st_mode) == 0o700
    assert list(outside.iterdir()) == []


def test_production_composition_closes_all_writes_for_a_symlinked_state_parent(
    tmp_path: Path,
) -> None:
    from spica.adapters.config_studio.composition import (
        create_production_config_studio_services,
    )

    repo_root, loaded = _synthetic_composition_repository(tmp_path)
    (repo_root / "spica_data").rmdir()
    outside = tmp_path / "outside-spica-data"
    outside.mkdir(mode=0o700)
    (repo_root / "spica_data").symlink_to(outside, target_is_directory=True)

    services = create_production_config_studio_services(
        repo_root=repo_root,
        loaded_environment=loaded,
        environment_owner=loaded.refresh,
        platform_capabilities=platform_capabilities_for(
            os_family="posix",
            runtime_name="linux",
            user_id=os.getuid(),
            temp_directory=tmp_path / "platform-tmp",
        ),
        background_health_code=None,
        self_check_service=None,
    )

    assert not any(
        services.capability_enabled(capability)
        for capability in (
            "app_config_write",
            "overlay_write",
            "sensitive_write",
            "rollback",
        )
    )
    assert (repo_root / "spica_data").is_symlink()
    assert list(outside.iterdir()) == []


def test_production_composition_does_not_repair_an_unsafe_existing_backup_root(
    tmp_path: Path,
) -> None:
    from spica.adapters.config_studio.composition import (
        create_production_config_studio_services,
    )

    repo_root, loaded = _synthetic_composition_repository(tmp_path)
    state_root = repo_root / "spica_data" / "config_studio"
    state_root.mkdir(mode=0o700)
    backup_root = state_root / "backups"
    backup_root.mkdir(mode=0o755)

    services = create_production_config_studio_services(
        repo_root=repo_root,
        loaded_environment=loaded,
        environment_owner=loaded.refresh,
        platform_capabilities=platform_capabilities_for(
            os_family="posix",
            runtime_name="linux",
            user_id=os.getuid(),
            temp_directory=tmp_path / "platform-tmp",
        ),
        background_health_code=None,
        self_check_service=None,
    )

    assert not any(
        services.capability_enabled(capability)
        for capability in (
            "app_config_write",
            "overlay_write",
            "sensitive_write",
            "rollback",
        )
    )
    assert stat.S_IMODE(state_root.stat().st_mode) == 0o700
    assert stat.S_IMODE(backup_root.stat().st_mode) == 0o755


@pytest.mark.parametrize(
    ("os_family", "runtime_name", "user_id"),
    (("nt", "win32", None), ("posix", "darwin", 1000)),
)
def test_production_composition_keeps_unverified_platform_writes_closed(
    tmp_path: Path,
    os_family: str,
    runtime_name: str,
    user_id: int | None,
) -> None:
    from spica.adapters.config_studio.composition import (
        create_production_config_studio_services,
    )

    repo_root, loaded = _synthetic_composition_repository(tmp_path)
    state_root = repo_root / "spica_data" / "config_studio"

    services = create_production_config_studio_services(
        repo_root=repo_root,
        loaded_environment=loaded,
        environment_owner=loaded.refresh,
        platform_capabilities=platform_capabilities_for(
            os_family=os_family,
            runtime_name=runtime_name,
            user_id=user_id,
            temp_directory=tmp_path / "platform-tmp",
        ),
        background_health_code=None,
        self_check_service=None,
    )

    assert not any(
        services.capability_enabled(capability)
        for capability in (
            "app_config_write",
            "overlay_write",
            "sensitive_write",
            "rollback",
        )
    )
    assert not state_root.exists()


def test_production_composition_hardlinked_env_closes_only_sensitive_writes(
    tmp_path: Path,
) -> None:
    from fastapi.testclient import TestClient

    from spica.config_studio.api import create_config_studio_app
    from spica.config_studio.security import SecurityContext
    from spica.adapters.config_studio.composition import (
        create_production_config_studio_services,
    )

    repo_root, loaded = _synthetic_composition_repository(tmp_path)
    outside = tmp_path / "synthetic-hardlink-source.env"
    outside.write_text("MODEL=synthetic-model\n", encoding="utf-8")
    os.link(outside, repo_root / "xiaosan.env")

    services = create_production_config_studio_services(
        repo_root=repo_root,
        loaded_environment=loaded,
        environment_owner=lambda: loaded,
        platform_capabilities=platform_capabilities_for(
            os_family="posix",
            runtime_name="linux",
            user_id=os.getuid(),
            temp_directory=tmp_path / "platform-tmp",
        ),
        background_health_code=None,
        self_check_service=None,
    )

    assert services.capability_enabled("app_config_write") is True
    assert services.capability_enabled("overlay_write") is True
    assert services.capability_enabled("rollback") is True
    assert services.capability_enabled("sensitive_write") is False
    assert services.meta()["sensitive_document"]["permission_health"] == (
        "MULTIPLE_LINKS"
    )
    app = create_config_studio_app(
        services,
        SecurityContext(
            host="127.0.0.1",
            port=8765,
            bootstrap_token="synthetic-bootstrap-token",
            token_factory=iter(("synthetic-session", "synthetic-csrf")).__next__,
        ),
    )
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        client.post(
            "/api/v1/session/bootstrap",
            headers={
                "Origin": "http://127.0.0.1:8765",
                "X-Spica-Bootstrap": "synthetic-bootstrap-token",
            },
        ).raise_for_status()
        status = client.get("/api/v1/sensitive/status")

    assert status.status_code == 200
    assert status.json()["permission_health"] == "MULTIPLE_LINKS"
    assert {
        slot["slot"]: slot["configured"]
        for slot in status.json()["secret_slots"]
    } == {
        "openai_api_key": False,
        "judge_api_key": False,
        "bilibili_cookie": False,
        "qbittorrent_password": False,
    }
    assert "synthetic-model" not in status.text
    assert outside.read_text(encoding="utf-8") == "MODEL=synthetic-model\n"


def test_production_composition_wrong_owner_env_closes_only_sensitive_writes(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from spica.config_studio.sensitive_env import (
        SensitiveEnvDocument,
        SensitiveEnvStatus,
    )
    from spica.adapters.config_studio.composition import (
        create_production_config_studio_services,
    )

    repo_root, loaded = _synthetic_composition_repository(tmp_path)
    sensitive_path = repo_root / "xiaosan.env"
    sensitive_path.write_text("MODEL=synthetic-model\n", encoding="utf-8")
    monkeypatch.setattr(
        SensitiveEnvDocument,
        "status",
        lambda _self: SensitiveEnvStatus((), "WRONG_OWNER"),
    )

    services = create_production_config_studio_services(
        repo_root=repo_root,
        loaded_environment=loaded,
        environment_owner=lambda: loaded,
        platform_capabilities=platform_capabilities_for(
            os_family="posix",
            runtime_name="linux",
            user_id=os.getuid(),
            temp_directory=tmp_path / "platform-tmp",
        ),
        background_health_code=None,
        self_check_service=None,
    )

    assert services.capability_enabled("app_config_write") is True
    assert services.capability_enabled("overlay_write") is True
    assert services.capability_enabled("rollback") is True
    assert services.capability_enabled("sensitive_write") is False


def test_production_composition_allows_0664_env_with_permission_hardening_preview(
    tmp_path: Path,
) -> None:
    from spica.config_studio.sensitive_env import SetSecret
    from spica.adapters.config_studio.composition import (
        create_production_config_studio_services,
    )

    repo_root, loaded = _synthetic_composition_repository(tmp_path)
    sensitive_path = repo_root / "xiaosan.env"
    sensitive_path.write_text("KEEP=synthetic\n", encoding="utf-8")
    sensitive_path.chmod(0o664)
    state_root = repo_root / "spica_data" / "config_studio"

    services = create_production_config_studio_services(
        repo_root=repo_root,
        loaded_environment=loaded,
        environment_owner=lambda: loaded,
        platform_capabilities=platform_capabilities_for(
            os_family="posix",
            runtime_name="linux",
            user_id=os.getuid(),
            temp_directory=tmp_path / "platform-tmp",
        ),
        background_health_code=None,
        self_check_service=None,
    )

    assert services.capability_enabled("sensitive_write") is True
    assert services.meta()["sensitive_document"]["permission_health"] == (
        "TOO_PERMISSIVE"
    )
    preview = services.preview_sensitive(
        SetSecret("openai_api_key", "synthetic-write-only-secret"),
        session_id="synthetic-session",
    )
    assert preview["permission_hardening"] is True
    assert stat.S_IMODE(sensitive_path.stat().st_mode) == 0o664
    assert not state_root.exists()

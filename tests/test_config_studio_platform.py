from __future__ import annotations

import ast
import inspect
from pathlib import Path


def test_platform_capabilities_are_explicit_and_fail_closed() -> None:
    from spica.adapters.config_studio.platform import platform_capabilities_for

    linux = platform_capabilities_for(
        os_family="posix",
        runtime_name="linux",
        user_id=1000,
        temp_directory="/synthetic-tmp",
    )
    windows = platform_capabilities_for(
        os_family="nt",
        runtime_name="win32",
        user_id=None,
        temp_directory="C:/synthetic-temp",
    )
    unknown = platform_capabilities_for(
        os_family="unknown",
        runtime_name="mystery",
        user_id=None,
        temp_directory="/synthetic-tmp",
    )
    unverified_posix = platform_capabilities_for(
        os_family="posix",
        runtime_name="darwin",
        user_id=1000,
        temp_directory="/synthetic-tmp",
    )

    assert linux.posix_permissions is True
    assert linux.managed_document_writes is True
    assert linux.sensitive_document_writes is True
    assert linux.self_check_containment is True
    assert linux.default_lock_root == Path(
        "/synthetic-tmp/spica-config-studio-locks-1000"
    )
    assert windows.posix_permissions is False
    assert windows.managed_document_writes is False
    assert windows.sensitive_document_writes is False
    assert windows.self_check_containment is False
    assert windows.default_lock_root == Path(
        "C:/synthetic-temp/spica-config-studio-locks"
    )
    assert unknown.managed_document_writes is False
    assert unknown.sensitive_document_writes is False
    assert unknown.self_check_containment is False
    assert unverified_posix.posix_permissions is True
    assert unverified_posix.managed_document_writes is False
    assert unverified_posix.sensitive_document_writes is False
    assert unverified_posix.self_check_containment is False


def test_config_studio_platform_detection_has_one_owner() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    adapter = "spica/adapters/config_studio/platform.py"
    consumers = (
        "spica/config/document_transaction.py",
        "spica/config_studio/sensitive_status.py",
        "spica/config_studio/sensitive_env.py",
        "spica/config_studio/self_check_service.py",
    )
    forbidden: list[str] = []
    for relative_path in consumers:
        tree = ast.parse((repo_root / relative_path).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Attribute) or not isinstance(
                node.value, ast.Name
            ):
                continue
            if (node.value.id, node.attr) in {
                ("os", "name"),
                ("os", "getuid"),
                ("sys", "platform"),
            }:
                forbidden.append(f"{relative_path}:{node.lineno}")

        for node in ast.walk(tree):
            if isinstance(node, ast.Import) and any(
                alias.name == "fcntl" for alias in node.names
            ):
                forbidden.append(f"{relative_path}:{node.lineno}")
            if isinstance(node, ast.ImportFrom) and node.module == "fcntl":
                forbidden.append(f"{relative_path}:{node.lineno}")

    assert forbidden == []
    adapter_source = (repo_root / adapter).read_text(encoding="utf-8")
    assert "current_platform_capabilities" in adapter_source
    assert "fcntl" in adapter_source
    assert not (repo_root / "spica/config/platform_capabilities.py").exists()


def test_low_level_platform_consumers_require_explicit_injection() -> None:
    from spica.adapters.config_studio.self_check_process import (
        SubprocessSelfCheckRunner,
    )
    from spica.config.document_transaction import ManagedDocumentTransaction
    from spica.config_studio.sensitive_env import SensitiveEnvDocument

    for owner in (
        ManagedDocumentTransaction.__init__,
        SubprocessSelfCheckRunner.__init__,
        SensitiveEnvDocument.__init__,
    ):
        parameter = inspect.signature(owner).parameters["platform_capabilities"]
        assert parameter.default is inspect.Parameter.empty


def test_self_check_platform_injection_uses_the_typed_capability_seam() -> None:
    from spica.adapters.config_studio.self_check_process import (
        SubprocessSelfCheckRunner,
    )
    from spica.config_studio.self_check_service import (
        create_production_self_check_service,
    )

    runner_parameters = inspect.signature(
        SubprocessSelfCheckRunner.__init__
    ).parameters
    factory_parameters = inspect.signature(
        create_production_self_check_service
    ).parameters

    assert "platform_capabilities" in runner_parameters
    assert "platform" not in runner_parameters
    assert "platform_capabilities" in factory_parameters
    assert "platform" not in factory_parameters


def test_posix_self_check_runtime_details_live_only_in_the_adapter() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    process_adapter = (
        repo_root / "spica/adapters/config_studio/self_check_process.py"
    )
    platform_adapter = repo_root / "spica/adapters/config_studio/platform.py"
    service = repo_root / "spica/config_studio/self_check_service.py"

    assert process_adapter.is_file()
    assert not (repo_root / "spica/config_studio/self_check_process.py").exists()
    process_source = process_adapter.read_text(encoding="utf-8")
    platform_source = platform_adapter.read_text(encoding="utf-8")
    service_source = service.read_text(encoding="utf-8")
    assert "start_new_session=True" in process_source
    assert "killpg" in process_source
    assert "import pwd" in platform_source
    assert "import pwd" not in service_source
    assert "SubprocessSelfCheckRunner" not in service_source

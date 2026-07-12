from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_config_studio_direct_dependencies_are_in_windows_install_contract():
    direct = {
        line.strip()
        for line in (ROOT / "requirements-config-studio.txt").read_text(
            encoding="utf-8"
        ).splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    assert direct == {
        "fastapi>=0.112.2,<1",
        "uvicorn>=0.30,<1",
        "ruamel.yaml>=0.18.6,<0.19",
    }

    windows_base = (ROOT / "requirements-windows-base.txt").read_text(encoding="utf-8")
    assert "-r requirements-config-studio.txt" in windows_base

    smoke_tree = ast.parse(
        (ROOT / "scripts" / "windows" / "check_imports.py").read_text(
            encoding="utf-8"
        )
    )
    required_literal = next(
        node.value
        for node in smoke_tree.body
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name)
        and target.id == "REQUIRED"
        and isinstance(node.value, ast.List)
    )
    smoke_entries = {ast.literal_eval(item)[:2] for item in required_literal.elts}
    assert {
        ("fastapi", "fastapi"),
        ("uvicorn", "uvicorn"),
        ("ruamel.yaml", "ruamel.yaml"),
    } <= smoke_entries


def test_config_studio_runtime_state_is_gitignored():
    ignore_rules = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()

    assert "/spica_data/config_studio/" in ignore_rules

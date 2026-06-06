"""Phase 3 guard: business code must not read the environment directly.

INVARIANT (CLAUDE.md #4): configuration flows through ``spica.config`` -- the
``ConfigManager`` (env knobs) and ``secrets`` (API keys). Everywhere else,
direct ``os.getenv`` / ``os.environ`` access is forbidden, so config has a single
validated source of truth.

Scans ``spica/`` + ``agent/`` + ``memory/`` (the conversation core being
platformised). A small allowlist covers the legitimate readers and the one spot
not yet migrated. The scan is AST-based and matches ``os.getenv`` / ``os.environ``
attribute access.
"""

import ast
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCAN_DIRS = ["spica", "agent", "memory"]

# Permitted forever: the config layer is *defined* by reading env.
PERMANENT_ALLOWLIST = {
    "spica/config/manager.py",
    "spica/config/secrets.py",
}
# Permitted temporarily -- delete the entry when the owning phase migrates it.
TEMPORARY_ALLOWLIST = {
    # TODO(Phase 6C): move PLAY_UNIT_* / VISUAL_STREAM_WORKERS into the typed
    # config / orchestrator, then drop this entry.
    "agent/streaming_pipeline.py",
}
ALLOWLIST = PERMANENT_ALLOWLIST | TEMPORARY_ALLOWLIST


def _env_accesses(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    hits: list[str] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "os"
            and node.attr in {"getenv", "environ"}
        ):
            hits.append(f"line {node.lineno}: os.{node.attr}")
    return hits


class NoGetenvGuardTest(unittest.TestCase):
    def test_business_code_does_not_read_env_directly(self):
        offenders: dict[str, list[str]] = {}
        for rel_dir in SCAN_DIRS:
            for path in sorted((REPO_ROOT / rel_dir).rglob("*.py")):
                rel = path.relative_to(REPO_ROOT).as_posix()
                if rel in ALLOWLIST:
                    continue
                hits = _env_accesses(path)
                if hits:
                    offenders[rel] = hits

        self.assertEqual(
            offenders,
            {},
            msg=(
                "Direct os.getenv/os.environ outside the config layer is "
                f"forbidden (CLAUDE.md #4). Route through spica.config. {offenders}"
            ),
        )

    def test_allowlist_entries_point_at_real_files(self):
        for rel in ALLOWLIST:
            self.assertTrue((REPO_ROOT / rel).is_file(), f"Stale allowlist entry: {rel}")


if __name__ == "__main__":
    unittest.main()

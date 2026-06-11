"""Phase 3 guard: business code must not read the environment directly.

INVARIANT (CLAUDE.md #4): configuration flows through ``spica.config`` -- the
``ConfigManager`` (env knobs) and ``secrets`` (API keys). Everywhere else,
direct ``os.getenv`` / ``os.environ`` access is forbidden, so config has a single
validated source of truth.

Scans ``spica/`` + ``memory/`` + ``agent_tools/`` (vendors excluded) + ``ui/``
+ ``hardware/`` (added in P0b step 1 -- the RESPEAKER_* reads lived outside the
old wall). P0a widened the wall after finding the old SCAN_DIRS swept a
long-gone ``agent/`` directory; P0b step 1 cleared the temporary allowlist:
screen/config.py now takes raw env values from ``manager.screen_env_overrides``
and the GPT-SoVITS env shims moved into ``spica/config/runtime_env.py``. The
scan is AST-based and matches ``os.getenv`` / ``os.environ`` attribute access.

Root-level entry files (``webui_qt.py`` etc.) are not scanned: priming the
process environment at the entry point is CLAUDE.md #10 territory (QT_IM_MODULE
/ ALSA shims must run before Qt constructs anything).
"""

import ast
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCAN_DIRS = ["spica", "memory", "agent_tools", "ui", "hardware"]
# Vendored runtimes (GPT-SoVITS etc.) keep their own env handling; not our code.
EXCLUDED_PARTS = {"vendors", "Applio"}

# Permitted forever: the config layer is *defined* by reading/mutating env.
PERMANENT_ALLOWLIST = {
    "spica/config/manager.py",
    "spica/config/secrets.py",
    # D3: process-env WRITER shim for the vendored GPT-SoVITS/HF runtime
    # (HF_HOME/NUMBA_CACHE_DIR/XDG... + proxy strip). Env mutation is a
    # config-layer privilege; business code calls the functions, never os.
    "spica/config/runtime_env.py",
}
# P0b 步1清零 (2026-06): screen/config.py 与 tts/gptsovits/service.py 的两条
# 临时债均已收编。新增 env 读取一律进 manager/env_roster,不准再开临时条目。
TEMPORARY_ALLOWLIST: set[str] = set()
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
        scanned = 0
        for rel_dir in SCAN_DIRS:
            scan_root = REPO_ROOT / rel_dir
            self.assertTrue(scan_root.is_dir(), f"SCAN_DIRS entry is not a directory: {rel_dir}")
            for path in sorted(scan_root.rglob("*.py")):
                if EXCLUDED_PARTS.intersection(path.parts):
                    continue
                scanned += 1
                rel = path.relative_to(REPO_ROOT).as_posix()
                if rel in ALLOWLIST:
                    continue
                hits = _env_accesses(path)
                if hits:
                    offenders[rel] = hits
        # The 2026-06 audit found the old scan silently sweeping a missing dir;
        # a sanity floor makes an empty sweep loud instead of green.
        self.assertGreater(scanned, 100, "guard swept suspiciously few files")

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

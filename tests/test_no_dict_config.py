"""C3b guard (INVARIANT N3-config): the runtime runs on typed deps, not a dict.

After C3b the runtime turn reads configuration from ``deps.config`` (a typed
``AppConfig``) and capability ports from ``deps.llm`` / ``deps.memory`` -- never
``services.config`` (the legacy dict) and never the ``services.llm_adapter`` /
``services.memory_adapter`` dual-field fallback. The single place allowed to
bridge a legacy dict-config services bundle into typed deps is
``spica/runtime/deps.py`` (``TurnDeps.from_legacy_services`` /
``from_services``), allowlisted like ``exec_strategy.py`` is for N4.

Phase 5 (deps single-track) widened the ban from the 3 config/dual-field attrs
to the FULL legacy services surface (8 attrs): the stage/commit layer now reads
``deps.recent`` / ``deps.llm_ready`` / ``deps.available_tool_schema_count`` /
``deps.visual`` / ``deps.tts``, so any new ``services.<port>`` read under
``spica/runtime/`` is regression, not convenience.

AST-based access scan (like ``test_no_getenv`` / ``test_no_raw_threadpool``)
over ``spica/runtime/``.
"""

import ast
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_DIR = REPO_ROOT / "spica" / "runtime"

# Modules allowed to read the legacy services bundle: the deps bridge, plus the
# two unit-job carriers -- visual_job.py / tts_job.py are the D1-REGISTERED
# permanent facade carriers (``services`` as the unit-job parameter shape,
# Phase 5 decision). Listing them is a pre-declared part of a NET TIGHTENING
# (the ban grows 3 -> 8 attrs in the same commit), not a loosening; no new
# reader may join this list.
ALLOWLIST = {
    "spica/runtime/deps.py",
    "spica/runtime/visual_job.py",
    "spica/runtime/tts_job.py",
}
# Phase 5: the full legacy surface -- dict config, the client/adapter dual
# fields, and the per-stage service reads the deps flip retired.
BANNED_ATTRS = {
    "config",
    "llm_adapter",
    "memory_adapter",
    "tts_adapter",
    "visual_tool",
    "recent_memory",
    "llm_client",
    "tool_schemas",
}
# Precise TEMPORARY exemptions: (repo-relative file, LINE, banned attr) --
# line-pinned on purpose: a second read of an exempted attr anywhere else in
# the same file must go red (D1 禁扩散), which a file+attr exemption would
# silently license. EMPTY since Phase 7-c2 settled the last entry
# (tool_round.py:36 ``services.llm_client`` -> ``deps.llm_ready``); any future
# entry needs a written reason and a settling phase, and the liveness test
# below keeps it pinned to the exact line while it lives.
TEMP_EXEMPT: set[tuple[str, int, str]] = set()


def _legacy_services_reads(path: Path, rel: str) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    hits: list[str] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "services"
            and node.attr in BANNED_ATTRS
            and (rel, node.lineno, node.attr) not in TEMP_EXEMPT
        ):
            hits.append(f"line {node.lineno}: services.{node.attr}")
    return hits


class NoDictConfigGuardTest(unittest.TestCase):
    def test_runtime_runs_on_typed_deps_not_dict_config(self):
        offenders: dict[str, list[str]] = {}
        for path in sorted(RUNTIME_DIR.rglob("*.py")):
            rel = path.relative_to(REPO_ROOT).as_posix()
            if rel in ALLOWLIST:
                continue
            hits = _legacy_services_reads(path, rel)
            if hits:
                offenders[rel] = hits

        self.assertEqual(
            offenders,
            {},
            msg=(
                "Runtime must read config/ports/state from typed deps (N3-config, "
                "Phase 5 single-track), not the legacy services bundle. Bridge via "
                f"spica.runtime.deps. {offenders}"
            ),
        )

    def test_allowlist_and_exemptions_point_at_real_files(self):
        for rel in ALLOWLIST | {rel for rel, _, _ in TEMP_EXEMPT}:
            self.assertTrue((REPO_ROOT / rel).is_file(), f"Stale entry: {rel}")

    def test_exempted_read_still_exists_at_the_pinned_line(self):
        # An exemption must die WITH the exact code it excuses: any edit that
        # shifts a pinned line goes red and forces a conscious re-pin or
        # deletion -- an exemption can never rot as a silent loophole.
        # Vacuous while TEMP_EXEMPT is empty (Phase 7-c2 settled the last
        # entry); it re-arms automatically for any future entry.
        for rel, lineno, attr in TEMP_EXEMPT:
            path = REPO_ROOT / rel
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            found = any(
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id == "services"
                and node.attr == attr
                and node.lineno == lineno
                for node in ast.walk(tree)
            )
            self.assertTrue(
                found,
                f"TEMP_EXEMPT ({rel}, line {lineno}, {attr}) no longer matches the "
                "pinned AST node -- delete the stale exemption (Phase 7-c2 cleanup) "
                "or consciously re-pin the line.",
            )


if __name__ == "__main__":
    unittest.main()

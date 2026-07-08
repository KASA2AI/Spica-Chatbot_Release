"""Pure planning + validation for the RVC (Applio) slim runtime (RVC Slim Step1).

Mirrors the TTS B1 split: this is the DECISION logic that the dry-run planner
(``scripts/local_runtime/build_rvc_slim.py``) orchestrates -- NO rsync / no real copy /
no production wiring. Generic glob / path-safety / size-cap / gitignore helpers are
REUSED from the already-tested TTS B1 module so the two slim systems share one engine;
this module adds the RVC manifest schema, the required/loud-failure checks, the
file→category mapping, and the dry-run report.

keep/exclude semantics (inherited): INCLUDED iff a ``keep`` glob matches AND no
``exclude`` glob (exclude wins). ``keep`` is a whitelist. §3.3: no os.getenv here.
"""

from __future__ import annotations

from typing import Any, Iterable

# Reuse the tested generic helpers from TTS B1 (one engine for both slim systems).
from spica.local_runtime.tts.slim_manifest import (  # noqa: F401
    load_manifest,
    matches_any,
    should_include,
    unmatched_keep_globs,
)


# ---- manifest schema validation ----------------------------------------------

def validate_manifest(manifest: dict[str, Any]) -> None:
    """Validate the RVC slim manifest; raise ValueError listing ALL problems."""
    errors: list[str] = []

    def need(cond: bool, msg: str) -> None:
        if not cond:
            errors.append(msg)

    need(isinstance(manifest.get("schema_version"), int), "schema_version: missing/not int")
    need(isinstance(manifest.get("runtime_name"), str), "runtime_name: missing/not str")
    need(isinstance(manifest.get("language_profile"), str), "language_profile: missing/not str")

    src = manifest.get("source")
    if not isinstance(src, dict) or not isinstance(src.get("applio_root"), str):
        errors.append("source.applio_root: missing/not str")

    out = manifest.get("output")
    if not isinstance(out, dict):
        errors.append("output: missing/not mapping")
    else:
        need(out.get("gitignored") is True, "output.gitignored: must be true")
        need("root" in out, "output.root: missing")
        need(isinstance(out.get("size_cap_gb"), (int, float)), "output.size_cap_gb: number required")

    rb = manifest.get("runtime_base")
    if not isinstance(rb, dict):
        errors.append("runtime_base: missing/not mapping")
    else:
        need(isinstance(rb.get("keep"), list) and bool(rb["keep"]), "runtime_base.keep: non-empty list required")
        need(isinstance(rb.get("exclude"), list), "runtime_base.exclude: list required")

    cps = manifest.get("character_packs")
    if not isinstance(cps, dict) or not cps:
        errors.append("character_packs: non-empty mapping required")
    else:
        for name, spec in cps.items():
            if not isinstance(spec, dict):
                errors.append(f"character_packs.{name}: not mapping")
                continue
            for key in ("model", "index"):
                need(key in spec, f"character_packs.{name}.{key}: missing")

    need(isinstance(manifest.get("required"), list) and bool(manifest["required"]),
         "required: non-empty list required")

    for field in ("import_preflight", "parity"):
        sec = manifest.get(field)
        need(isinstance(sec, dict) and "status" in sec, f"{field}.status: missing")

    if errors:
        raise ValueError("invalid rvc slim manifest:\n  - " + "\n  - ".join(errors))


# ---- required / loud-failure (pure) ------------------------------------------

def uncovered_required_by_keep(required: Iterable[str], keep: Iterable[str]) -> list[str]:
    """required paths NOT matched by ANY keep glob -> the manifest would drop a
    load-bearing file. Manifest bug -> the build must abort."""
    keep = list(keep)
    return [r for r in required if not matches_any(r, keep)]


def excluded_required(required: Iterable[str], exclude: Iterable[str]) -> list[str]:
    """required paths that an ``exclude`` glob ALSO matches -> exclude would shadow a
    must-keep (e.g. a broad ``rvc/train/**`` over ``rvc/train/process/model_blender.py``).
    Conflict -> the build must abort (NOT silently drop, NOT silently keep)."""
    exclude = list(exclude)
    return [r for r in required if matches_any(r, exclude)]


def missing_required(required: Iterable[str], present_rels: Iterable[str]) -> list[str]:
    """required paths absent from the enumerated source -> abort."""
    present = set(present_rels)
    return [r for r in required if r not in present]


# ---- file -> category mapping (pure) -----------------------------------------

def categorize(rel: str) -> str:
    """Map a base-relative source path to a dry-run report category."""
    r = rel.replace("\\", "/")
    if r.startswith("rvc/models/embedders/"):
        return "runtime_model_embedder"
    if r == "rvc/models/predictors/rmvpe.pt":
        return "runtime_model_pitch"
    base = r.rsplit("/", 1)[-1]
    if any(base.startswith(p) for p in ("LICENSE", "README", "NOTICE", "COPYING")):
        return "license"
    if r.endswith((".json", ".txt")):
        return "config"
    if r.endswith(".py"):
        return "runtime_python"
    return "other"


# ---- dry-run report ----------------------------------------------------------

def assemble_report(
    *,
    manifest: dict[str, Any],
    character: str,
    source_root: str,
    output_dir: str,
    would_copy: list[dict[str, Any]],
    totals: dict[str, Any],
) -> dict[str, Any]:
    """The dry-run report. parity + import_preflight stay PENDING (never silently
    skipped); the real build (with sha256) is a later cut."""
    by_cat: dict[str, dict[str, int]] = {}
    for e in would_copy:
        agg = by_cat.setdefault(e["category"], {"files": 0, "bytes": 0})
        agg["files"] += 1
        agg["bytes"] += e["size_bytes"]
    return {
        "dry_run": True,
        "schema_version": manifest["schema_version"],
        "runtime_name": manifest["runtime_name"],
        "language_profile": manifest["language_profile"],
        "character": character,
        "source_root": source_root,
        "output_dir": output_dir,  # NOT created in dry-run
        "totals": totals,
        "category_sizes": by_cat,
        "would_copy": would_copy,
        "import_preflight": {"status": manifest.get("import_preflight", {}).get("status", "PENDING")},
        "parity": {"status": manifest.get("parity", {}).get("status", "PENDING")},
    }

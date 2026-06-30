"""GPT-SoVITS slim runtime builder -- DRY-RUN planner (LOCAL_RUNTIME_PLAN B1 step2).

This FIRST version only PLANS. It reads the slim manifest, enumerates the vendored
source tree, applies keep/exclude, resolves the character pack + its reference wav /
prompt from tts.yaml, computes the would-copy list + estimated total size, and runs
the guard rails (gitignore gate, size cap, realpath containment). It copies NOTHING,
writes NO file, and creates NO output directory. Real copy + sha256 + the generated
``character.yaml`` come in a later cut.

Boundaries (§1 / B1): does NOT touch the driver / service.py / TTSPort / run_turn /
orchestrator / ChatEngine, and NEVER switches the default TTS path. No ONNX / TRT /
Genie here. The original vendored tree is the read-only SOURCE and stays the fallback.

The pure decision logic lives in ``spica.local_runtime.tts.slim_manifest`` (synthetic-
tree testable, no torch / transformers / GPU). This script is the thin filesystem +
CLI shell over it; ``plan_build`` is unit-tested against a fake source tree.

  python -m scripts.local_runtime.build_tts_slim [--character spcia] [--json]

ENV NAMES (§3.3): reads NONE.
"""

from __future__ import annotations

import argparse
import json
import os
import posixpath
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from spica.local_runtime.tts.slim_manifest import (  # noqa: E402
    assemble_build_report,
    build_character_config,
    character_reference_files,
    collect_files,
    enumerate_audio_files,
    inp_refs_entries,
    is_safe_rel,
    is_within,
    license_status,
    load_manifest,
    matches_any,
    output_is_gitignored,
    sha256_of,
    should_include,
    unmatched_keep_globs,
    validate_manifest,
    within_size_cap,
)

DEFAULT_MANIFEST = "data/config/tts_slim_manifest.yaml"


class BuildAbort(RuntimeError):
    """A guard refused the build (gitignore / size cap / containment / bad source)."""


# ---- helpers -----------------------------------------------------------------

def _load_yaml_mapping(path: str) -> dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise BuildAbort(f"expected a YAML mapping: {path}")
    return data


def _abspath(p: str, base: Path) -> str:
    q = Path(p)
    return str(q if q.is_absolute() else base / q)


def _resolve_ref(src: str, config_dir: str) -> str:
    """Resolve a tts.yaml ref path EXACTLY as GPTSoVITSTool._resolve_path does:
    absolute stays put, relative resolves against the config file's directory.
    Realpath'd so a symlinked spica_data dir is resolved before stat."""
    p = Path(src)
    target = p if p.is_absolute() else Path(config_dir) / p
    return os.path.realpath(str(target))


def _entry(category: str, source: str, target: str, *, size_bytes: int, exists: bool) -> dict[str, Any]:
    return {
        "category": category,
        "source": source,
        "target": target,
        "size_bytes": size_bytes,
        "exists": exists,
    }


# ---- dry-run planner ---------------------------------------------------------

def plan_build(
    *,
    source_root: str,
    manifest: dict[str, Any],
    tts_yaml: dict[str, Any],
    config_dir: str,
    output_dir: str,
    character: str,
    check_ignore: Callable[[str], bool],
) -> dict[str, Any]:
    """Compute the DRY-RUN build plan. Copies nothing, creates no directory.

    Each guard raises ``BuildAbort``: manifest invalid (ValueError); output dir not
    gitignored; source missing / not a dir; source & output contain each other
    (realpath-resolved -- symlink safe); a target escapes the output root; estimated
    size over the manifest size cap.

    DEPENDENCY-MISSING IS LOUD: every declared + actively-used dependency that is
    absent aborts the dry-run (no "successful plan" with a hole). Blocking deps:
    each ``runtime_base.keep`` glob must match >=1 source file; character gpt+sovits
    weights; each emotion's primary ref_audio_path + prompt_text_path (inline
    prompt_text needs no file); inp_refs dir (declared-but-missing / empty). Missing
    licenses remain a WARNING only.
    """
    validate_manifest(manifest)

    # Gitignore gate -- refuse to run unless the output dir is ignored (so a future
    # real build can never leak the slim artifact into git).
    if not output_is_gitignored(output_dir, check_ignore):
        raise BuildAbort(f"output dir is not gitignored, refusing: {output_dir}")

    # realpath BOTH sides BEFORE any containment reasoning (a symlinked output dir
    # could otherwise escape -- see is_within CONSTRAINT).
    src_real = os.path.realpath(source_root)
    out_real = os.path.realpath(output_dir)
    if not os.path.isdir(src_real):
        raise BuildAbort(f"source vendored root missing / not a dir: {src_real}")
    if is_within(out_real, src_real) or is_within(src_real, out_real):
        raise BuildAbort(
            f"output and source must not contain each other (realpath): "
            f"src={src_real} out={out_real}"
        )

    base_keep = manifest["runtime_base"]["keep"]
    base_exclude = manifest["runtime_base"]["exclude"]
    lic_keep = manifest["licenses"]["keep"]

    would: list[dict[str, Any]] = []
    license_rels: list[str] = []  # un-prefixed source rels, for license_status matching

    # ---- runtime base + licenses (enumerated from the source tree) -----------
    # Layout: base/license -> base/<rel> ; character pack -> characters/<name>/... .
    # This physically splits the shared runtime_base from per-character packs.
    all_rels = collect_files(src_real, follow_symlinks=False)
    for rel in all_rels:
        if matches_any(rel, base_exclude):
            continue  # exclude wins, for both base and license matches
        if matches_any(rel, lic_keep):
            category = "license"
            license_rels.append(rel)
        elif should_include(rel, base_keep, base_exclude):
            category = "base"
        else:
            continue
        full = os.path.join(src_real, rel)
        would.append(_entry(category, full, "base/" + rel, size_bytes=os.path.getsize(full), exists=True))

    # BLOCKING: every required base keep glob must match >=1 source file. A critical
    # load-path asset matching nothing means the slim runtime is broken -> abort.
    unmatched = unmatched_keep_globs(all_rels, base_keep, base_exclude)
    if unmatched:
        raise BuildAbort(f"required base asset glob(s) matched no source file: {unmatched}")

    # ---- character pack: weights + reference wav/prompt (self-contained) -----
    pack = manifest["character_packs"][character]
    pack_root = posixpath.join("characters", character)

    def _add_pack(category: str, src_abs: str, pack_rel: str, *, required: bool = True) -> None:
        target = posixpath.join(pack_root, pack_rel)
        exists = os.path.isfile(src_abs)
        if required and not exists:  # BLOCKING: declared + used dependency absent
            raise BuildAbort(f"missing required {category} source: {src_abs}")
        size = os.path.getsize(src_abs) if exists else 0
        would.append(_entry(category, src_abs, target, size_bytes=size, exists=exists))

    # Weights live in the vendored tree but are EXCLUDED from base; the pack pulls
    # the specific character weight explicitly. Both are REQUIRED.
    _add_pack("character_gpt", os.path.join(src_real, pack["gpt_weight"]),
              "GPT_weights/" + posixpath.basename(pack["gpt_weight"]), required=True)
    _add_pack("character_sovits", os.path.join(src_real, pack["sovits_weight"]),
              "SoVITS_weights/" + posixpath.basename(pack["sovits_weight"]), required=True)
    # primary ref wav + prompt (one level under reference/<emotion>/). character_reference_files
    # yields ref_audio_path + prompt_text_path only (inline prompt_text -> no file). Both REQUIRED.
    for ref in character_reference_files(tts_yaml):
        _add_pack(ref["category"], _resolve_ref(ref["source"], config_dir), ref["target"], required=True)
    # inp_refs: a DECLARED + actively-used v2ProPlus dependency -- get_tts_wav globs
    # the refs dir and fuses each wav via sv_emb into vq_model.decode. LOUD FAILURE
    # if declared-but-missing / declared-but-empty (never a silent defer). Packed
    # under a dedicated reference/<emotion>/refs/ subdir to preserve glob isolation.
    for emotion, spec in (tts_yaml.get("emotions") or {}).items():
        raw = spec.get("inp_refs_path")
        if not raw:
            continue
        refs_dir = _resolve_ref(raw, config_dir)
        if not os.path.isdir(refs_dir):
            raise BuildAbort(
                f"inp_refs_path declared for emotion '{emotion}' but directory is missing: {refs_dir}"
            )
        wavs = enumerate_audio_files(refs_dir)
        if not wavs:
            raise BuildAbort(
                f"inp_refs_path directory for emotion '{emotion}' has no audio files: {refs_dir}"
            )
        for ref in inp_refs_entries(emotion, wavs):
            _add_pack(ref["category"], ref["source"], ref["target"])

    # ---- target safety + containment (defense in depth) ----------------------
    for e in would:
        if not is_safe_rel(e["target"]):
            raise BuildAbort(f"target escapes pack/base root (unsafe rel): {e['target']}")
        full_target = os.path.normpath(os.path.join(out_real, e["target"]))
        if not is_within(full_target, out_real):
            raise BuildAbort(f"target escapes output root: {e['target']}")

    # ---- size cap ------------------------------------------------------------
    total_bytes = sum(e["size_bytes"] for e in would if e["exists"])
    size_cap_gb = manifest["output"]["size_cap_gb"]
    within_cap = within_size_cap(total_bytes, size_cap_gb)
    if not within_cap:
        raise BuildAbort(
            f"estimated size {total_bytes / 1024 ** 3:.2f} GB exceeds cap {size_cap_gb} GB"
        )

    # ---- report --------------------------------------------------------------
    # No missing_sources field: every base/license file is enumerated-from-source
    # (exists), and every character dependency is REQUIRED (a miss already aborted
    # above). A returned report therefore has no holes.
    licenses = license_status(
        manifest["licenses"]["expect_license_for"],
        license_rels,  # un-prefixed rels (targets carry a base/ prefix)
    )
    inp_refs_packed = sum(1 for e in would if e["category"] == "character_inp_refs")

    return {
        "dry_run": True,
        "manifest_version": manifest["version"],
        "language_profile": manifest["language_profile"],
        "character": character,
        "source_root": src_real,
        "output_dir": out_real,  # NOTE: not created in dry-run
        "totals": {
            "file_count": len(would),
            "total_bytes": total_bytes,
            "total_gb": round(total_bytes / 1024 ** 3, 4),
            "size_cap_gb": size_cap_gb,
            "within_cap": within_cap,
        },
        "would_copy": would,
        "generated": [
            posixpath.join(pack_root, "character.yaml"),
            "weight.json  (runtime-written at load -- P0, base root must be writable)",
        ],
        "character_config_preview": build_character_config(pack, tts_yaml),
        "licenses": licenses,  # licenses.missing is a WARNING only -- never blocking
        # inp_refs is a real v2ProPlus inference dependency -- packed into the pack
        # (loud failure upstream if declared-but-missing/empty), never deferred.
        "inp_refs_packed": inp_refs_packed,
        "writable_paths": [f"{w['path']} ({w['risk']})" for w in manifest.get("writable_paths", [])],
        "parity": "PENDING",
    }


# ---- CLI ---------------------------------------------------------------------

def _git_check_ignore(repo_root: Path) -> Callable[[str], bool]:
    def check(path: str) -> bool:
        # The output is a DIRECTORY. A trailing-slash ignore rule (`artifacts/x/`)
        # only matches via check-ignore when the queried path is dir-shaped (trailing
        # slash) or already exists on disk -- a not-yet-created dir queried without a
        # slash returns "not ignored". So query the dir-shaped form too.
        candidates = [path] if path.endswith(os.sep) or path.endswith("/") else [path, path + "/"]
        for cand in candidates:
            result = subprocess.run(
                ["git", "-C", str(repo_root), "check-ignore", "-q", cand],
                capture_output=True,
            )
            if result.returncode == 0:  # 0 = ignored, 1 = not ignored, 128 = error
                return True
        return False
    return check


def _print_summary(report: dict[str, Any]) -> None:
    t = report["totals"]
    print(f"[dry-run] character={report['character']} language_profile={report['language_profile']}")
    print(f"  source: {report['source_root']}")
    print(f"  output: {report['output_dir']}  (NOT created -- dry-run)")
    by_cat: dict[str, list[int]] = {}
    for e in report["would_copy"]:
        agg = by_cat.setdefault(e["category"], [0, 0])
        agg[0] += 1
        agg[1] += e["size_bytes"] if e["exists"] else 0
    for cat in sorted(by_cat):
        n, b = by_cat[cat]
        print(f"  {cat:20s} {n:5d} files  {b / 1024 ** 2:10.1f} MB")
    print(
        f"  TOTAL {t['file_count']} files  {t['total_gb']} GB  "
        f"(cap {t['size_cap_gb']} GB, within={t['within_cap']})"
    )
    if report["licenses"]["missing"]:
        print(f"  WARNING missing license for: {report['licenses']['missing']}")
    print(f"  inp_refs packed: {report['inp_refs_packed']} files")


def execute_build(
    *,
    source_root: str,
    manifest: dict[str, Any],
    tts_yaml: dict[str, Any],
    config_dir: str,
    output_dir: str,
    character: str,
    check_ignore: Callable[[str], bool],
) -> tuple[dict[str, Any], str]:
    """REAL build: plan (runs ALL guards) -> copy every would-copy file into an atomic
    staging dir (sibling of the output, same filesystem) with per-file sha256 -> emit a
    self-contained character.yaml + build_report.json -> ``os.rename`` staging to the
    final output (atomic). ANY error rolls back (rmtree staging); the final dir is only
    created by the final rename, so a failure never leaves a partial output. Refuses to
    clobber an existing output. Returns (build_report, output_abs)."""
    plan = plan_build(
        source_root=source_root, manifest=manifest, tts_yaml=tts_yaml,
        config_dir=config_dir, output_dir=output_dir, character=character,
        check_ignore=check_ignore,
    )
    out_real = os.path.realpath(output_dir)
    if os.path.exists(out_real):
        raise BuildAbort(f"output dir already exists, refusing to clobber: {out_real}  (rm it to rebuild)")
    parent = os.path.dirname(out_real) or "."
    os.makedirs(parent, exist_ok=True)
    staging = tempfile.mkdtemp(prefix="." + os.path.basename(out_real) + ".staging-", dir=parent)
    staging_real = os.path.realpath(staging)
    try:
        files_report: list[dict[str, Any]] = []
        for e in plan["would_copy"]:
            dst = os.path.normpath(os.path.join(staging_real, e["target"]))
            if not is_within(dst, staging_real):  # defense in depth (targets are is_safe_rel already)
                raise BuildAbort(f"refusing to copy outside staging: {e['target']}")
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(e["source"], dst)  # copies content (follows symlinked sources), preserves mtime
            files_report.append({
                "category": e["category"],
                "source": e["source"],
                "target": e["target"],
                "size_bytes": os.path.getsize(dst),
                "sha256": sha256_of(dst),
            })

        # self-contained, pack-relative character.yaml (generated, not copied).
        char_yaml_rel = posixpath.join("characters", character, "character.yaml")
        char_yaml_dst = os.path.join(staging_real, char_yaml_rel)
        os.makedirs(os.path.dirname(char_yaml_dst), exist_ok=True)
        with open(char_yaml_dst, "w", encoding="utf-8") as f:
            yaml.safe_dump(plan["character_config_preview"], f, allow_unicode=True, sort_keys=False)

        total_bytes = sum(f["size_bytes"] for f in files_report)
        base_bytes = sum(f["size_bytes"] for f in files_report if f["category"] in ("base", "license"))
        size_cap_gb = manifest["output"]["size_cap_gb"]
        within_cap = within_size_cap(total_bytes, size_cap_gb)
        if not within_cap:  # re-check on actually-copied bytes
            raise BuildAbort(f"built size {total_bytes / 1024 ** 3:.2f} GB exceeds cap {size_cap_gb} GB")

        report = assemble_build_report(
            manifest=manifest, character=character, files=files_report, licenses=plan["licenses"],
            totals={
                "file_count": len(files_report), "total_bytes": total_bytes,
                "total_gb": round(total_bytes / 1024 ** 3, 4),
                "base_bytes": base_bytes, "character_bytes": total_bytes - base_bytes,
                "size_cap_gb": size_cap_gb, "within_cap": within_cap,
            },
        )
        report["inp_refs_packed"] = plan["inp_refs_packed"]
        report["generated"] = [char_yaml_rel, "build_report.json",
                               "weight.json  (runtime-written at load -- P0, base root must be writable)"]
        with open(os.path.join(staging_real, "build_report.json"), "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2, ensure_ascii=False)

        os.rename(staging_real, out_real)  # atomic publish (same filesystem)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)  # rollback: leave no partial output
        raise
    return report, out_real


def _print_build_summary(report: dict[str, Any], out_real: str) -> None:
    t = report["totals"]
    print(f"[build] character={report['character']} -> {out_real}")
    by_cat: dict[str, list[int]] = {}
    for f in report["files"]:
        agg = by_cat.setdefault(f["category"], [0, 0])
        agg[0] += 1
        agg[1] += f["size_bytes"]
    for cat in sorted(by_cat):
        n, b = by_cat[cat]
        print(f"  {cat:20s} {n:5d} files  {b / 1024 ** 2:10.1f} MB")
    print(
        f"  TOTAL {t['file_count']} files  {t['total_gb']} GB  "
        f"(base {t['base_bytes'] / 1024 ** 2:.1f} MB + char {t['character_bytes'] / 1024 ** 2:.1f} MB; "
        f"cap {t['size_cap_gb']} GB within={t['within_cap']})"
    )
    print(f"  inp_refs packed: {report['inp_refs_packed']} files")
    if report["licenses"]["missing"]:
        print(f"  WARNING missing license for: {report['licenses']['missing']}")
    print(f"  writable_paths: {report['writable_paths']}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="GPT-SoVITS slim runtime builder (B1): dry-run plan, or --build.")
    ap.add_argument("--manifest", default=DEFAULT_MANIFEST)
    ap.add_argument("--character", default=None, help="pack name (default: the sole pack)")
    ap.add_argument("--source", default=None, help="override source_vendored_root")
    ap.add_argument("--output", "--out", default=None, help="override output.default_dir")
    ap.add_argument("--build", action="store_true", help="REAL build (copy files). Default: dry-run plan only.")
    ap.add_argument("--json", action="store_true", help="also print the full report JSON")
    args = ap.parse_args(argv)

    manifest_path = _abspath(args.manifest, _REPO_ROOT)
    manifest = load_manifest(manifest_path)
    validate_manifest(manifest)

    character = args.character
    if character is None:
        packs = list(manifest["character_packs"])
        if len(packs) != 1:
            print(f"error: --character required (packs: {packs})", file=sys.stderr)
            return 2
        character = packs[0]
    if character not in manifest["character_packs"]:
        print(f"error: unknown character pack: {character}", file=sys.stderr)
        return 2

    source_root = _abspath(args.source or manifest["source_vendored_root"], _REPO_ROOT)
    output_dir = _abspath(args.output or manifest["output"]["default_dir"], _REPO_ROOT)

    pack = manifest["character_packs"][character]
    config_source = _abspath(pack["config_source"], _REPO_ROOT)
    tts_yaml = _load_yaml_mapping(config_source)
    config_dir = str(Path(config_source).parent)

    common = dict(
        source_root=source_root,
        manifest=manifest,
        tts_yaml=tts_yaml,
        config_dir=config_dir,
        output_dir=output_dir,
        character=character,
        check_ignore=_git_check_ignore(_REPO_ROOT),
    )

    if args.build:
        try:
            report, out_real = execute_build(**common)
        except BuildAbort as exc:
            print(f"BUILD ABORTED: {exc}", file=sys.stderr)
            return 1
        _print_build_summary(report, out_real)
        print(f"  build_report: {os.path.join(out_real, 'build_report.json')}")
        if args.json:
            print(json.dumps(report, indent=2, ensure_ascii=False))
        return 0

    try:
        report = plan_build(**common)
    except BuildAbort as exc:
        print(f"BUILD ABORTED: {exc}", file=sys.stderr)
        return 1

    _print_summary(report)
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    # DRY-RUN: nothing copied, no directory created, no default switched.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

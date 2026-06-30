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
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from spica.local_runtime.tts.slim_manifest import (  # noqa: E402
    build_character_config,
    character_reference_files,
    collect_files,
    is_safe_rel,
    is_within,
    license_status,
    load_manifest,
    matches_any,
    output_is_gitignored,
    should_include,
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

    # ---- runtime base + licenses (enumerated from the source tree) -----------
    for rel in collect_files(src_real, follow_symlinks=False):
        if matches_any(rel, base_exclude):
            continue  # exclude wins, for both base and license matches
        if matches_any(rel, lic_keep):
            category = "license"
        elif should_include(rel, base_keep, base_exclude):
            category = "base"
        else:
            continue
        full = os.path.join(src_real, rel)
        would.append(_entry(category, full, rel, size_bytes=os.path.getsize(full), exists=True))

    # ---- character pack: weights + reference wav/prompt (self-contained) -----
    pack = manifest["character_packs"][character]
    pack_root = posixpath.join("characters", character)

    def _add_pack(category: str, src_abs: str, pack_rel: str) -> None:
        target = posixpath.join(pack_root, pack_rel)
        exists = os.path.isfile(src_abs)
        size = os.path.getsize(src_abs) if exists else 0
        would.append(_entry(category, src_abs, target, size_bytes=size, exists=exists))

    # Weights live in the vendored tree but are EXCLUDED from base; the pack pulls
    # the specific character weight explicitly.
    _add_pack("character_gpt", os.path.join(src_real, pack["gpt_weight"]),
              "GPT_weights/" + posixpath.basename(pack["gpt_weight"]))
    _add_pack("character_sovits", os.path.join(src_real, pack["sovits_weight"]),
              "SoVITS_weights/" + posixpath.basename(pack["sovits_weight"]))
    for raw_src, pack_rel in character_reference_files(tts_yaml):
        _add_pack("character_reference", _resolve_ref(raw_src, config_dir), pack_rel)

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
    missing = [
        {"category": e["category"], "source": e["source"], "target": e["target"]}
        for e in would if not e["exists"]
    ]
    licenses = license_status(
        manifest["licenses"]["expect_license_for"],
        [e["target"] for e in would if e["category"] == "license"],
    )
    unpacked_inp_refs = [
        {"emotion": emo, "path": spec.get("inp_refs_path")}
        for emo, spec in (tts_yaml.get("emotions") or {}).items()
        if spec.get("inp_refs_path")
    ]

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
        "licenses": licenses,
        "missing_sources": missing,
        # inp_refs are extra per-emotion reference dirs; NOT packed in this cut (the
        # pack is incomplete without them). Surfaced honestly rather than silently.
        "unpacked_inp_refs": unpacked_inp_refs,
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
    if report["missing_sources"]:
        print(f"  WARNING {len(report['missing_sources'])} missing source(s):")
        for m in report["missing_sources"]:
            print(f"    - [{m['category']}] {m['source']}")
    if report["licenses"]["missing"]:
        print(f"  WARNING missing license for: {report['licenses']['missing']}")
    if report["unpacked_inp_refs"]:
        print(f"  NOTE {len(report['unpacked_inp_refs'])} inp_refs dir(s) NOT packed (deferred this cut)")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="GPT-SoVITS slim runtime DRY-RUN planner (B1).")
    ap.add_argument("--manifest", default=DEFAULT_MANIFEST)
    ap.add_argument("--character", default=None, help="pack name (default: the sole pack)")
    ap.add_argument("--source", default=None, help="override source_vendored_root")
    ap.add_argument("--output", default=None, help="override output.default_dir")
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

    try:
        report = plan_build(
            source_root=source_root,
            manifest=manifest,
            tts_yaml=tts_yaml,
            config_dir=config_dir,
            output_dir=output_dir,
            character=character,
            check_ignore=_git_check_ignore(_REPO_ROOT),
        )
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

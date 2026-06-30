"""RVC (Applio) slim runtime builder -- DRY-RUN planner (RVC Slim Step1).

Mirrors build_tts_slim.py: reads the RVC slim manifest, enumerates the Applio source
tree, applies keep/exclude (whitelist), enforces the required/loud-failure rules, pulls
the character model+index, computes the would-copy list + estimated size, and runs the
guards. It copies NOTHING, writes NO file, creates NO output directory. ``--build`` is
NOT implemented in this cut (dry-run only) -- it aborts on purpose.

Boundaries: does NOT touch SongPipeline / sing_song / rvc.py invocation / TTS /
GPT-SoVITS / config / the Applio source. No env / subprocess / Windows / slim copy.

Generic FS/glob/path-safety helpers are reused from the tested TTS B1 module; the RVC
schema + required checks + categories live in spica.local_runtime.rvc.slim_manifest.

  python scripts/local_runtime/build_rvc_slim.py --manifest data/config/rvc_slim_manifest.yaml --character spica

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

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from spica.local_runtime.tts.slim_manifest import (  # noqa: E402  reuse tested generic helpers
    collect_files,
    is_safe_rel,
    is_within,
    load_manifest,
    matches_any,
    output_is_gitignored,
    should_include,
    unmatched_keep_globs,
    within_size_cap,
)
from spica.local_runtime.rvc.slim_manifest import (  # noqa: E402
    assemble_report,
    categorize,
    excluded_required,
    missing_required,
    uncovered_required_by_keep,
    validate_manifest,
)

DEFAULT_MANIFEST = "data/config/rvc_slim_manifest.yaml"


class BuildAbort(RuntimeError):
    """A guard refused the build (gitignore / containment / size cap / required miss)."""


def _entry(category: str, source: str, target: str, size_bytes: int) -> dict[str, Any]:
    return {"category": category, "source": source, "target": target, "size_bytes": size_bytes}


def plan_build(
    *,
    source_root: str,
    manifest: dict[str, Any],
    output_dir: str,
    character: str,
    check_ignore: Callable[[str], bool],
) -> dict[str, Any]:
    """Compute the DRY-RUN plan. Copies nothing, creates no directory.

    Each guard raises ``BuildAbort``: manifest invalid (ValueError); output not
    gitignored; source missing / not a dir; source & output contain each other
    (realpath); a required path is not covered by keep / is shadowed by exclude /
    is missing from source; a keep glob matches nothing; a target escapes the output
    root; estimated size over the cap. (Layout: base/ for runtime_base,
    characters/<name>/{model,index}/ for the pack -- same split as TTS B1.)
    """
    validate_manifest(manifest)

    if not output_is_gitignored(output_dir, check_ignore):
        raise BuildAbort(f"output dir is not gitignored, refusing: {output_dir}")

    src_real = os.path.realpath(source_root)
    out_real = os.path.realpath(output_dir)
    if not os.path.isdir(src_real):
        raise BuildAbort(f"Applio source root missing / not a dir: {src_real}")
    if is_within(out_real, src_real) or is_within(src_real, out_real):
        raise BuildAbort(f"output and source must not contain each other (realpath): src={src_real} out={out_real}")

    base_keep = manifest["runtime_base"]["keep"]
    base_exclude = manifest["runtime_base"]["exclude"]
    lic_keep = manifest.get("licenses", {}).get("keep", [])
    required = manifest["required"]

    # ---- required sanity (manifest-level, before enumerating) ----------------
    bad = uncovered_required_by_keep(required, base_keep)
    if bad:
        raise BuildAbort(f"required path(s) not covered by any keep glob: {bad}")
    bad = excluded_required(required, base_exclude)
    if bad:
        raise BuildAbort(f"required path(s) shadowed by an exclude glob (would be dropped): {bad}")

    # ---- enumerate source + required-exists + keep-coverage ------------------
    all_rels = collect_files(src_real, follow_symlinks=False)
    miss = missing_required(required, all_rels)
    if miss:
        raise BuildAbort(f"required source file(s) missing from Applio: {miss}")
    unmatched = unmatched_keep_globs(all_rels, base_keep, base_exclude)
    if unmatched:
        raise BuildAbort(f"keep glob(s) matched no source file: {unmatched}")

    # ---- runtime base (whitelist + licenses) ---------------------------------
    would: list[dict[str, Any]] = []
    for rel in all_rels:
        if matches_any(rel, base_exclude):
            continue
        if should_include(rel, base_keep, base_exclude) or matches_any(rel, lic_keep):
            full = os.path.join(src_real, rel)
            would.append(_entry(categorize(rel), full, "base/" + rel, os.path.getsize(full)))

    # ---- character pack: model + index (REQUIRED, must exist) ----------------
    pack = manifest["character_packs"][character]
    pack_root = posixpath.join("characters", character)
    for cat, key, sub in (("character_model", "model", "model"), ("character_index", "index", "index")):
        src_abs = os.path.join(src_real, pack[key])
        if not os.path.isfile(src_abs):
            raise BuildAbort(f"character {key} missing: {src_abs}")
        target = posixpath.join(pack_root, sub, posixpath.basename(pack[key]))
        would.append(_entry(cat, src_abs, target, os.path.getsize(src_abs)))

    # ---- target safety + containment (defense in depth) ----------------------
    for e in would:
        if not is_safe_rel(e["target"]):
            raise BuildAbort(f"target escapes pack/base root (unsafe rel): {e['target']}")
        if not is_within(os.path.normpath(os.path.join(out_real, e["target"])), out_real):
            raise BuildAbort(f"target escapes output root: {e['target']}")

    # ---- size cap ------------------------------------------------------------
    total_bytes = sum(e["size_bytes"] for e in would)
    size_cap_gb = manifest["output"]["size_cap_gb"]
    within_cap = within_size_cap(total_bytes, size_cap_gb)
    if not within_cap:
        raise BuildAbort(f"estimated size {total_bytes / 1024 ** 3:.2f} GB exceeds cap {size_cap_gb} GB")

    totals = {
        "file_count": len(would),
        "total_bytes": total_bytes,
        "total_gb": round(total_bytes / 1024 ** 3, 4),
        "size_cap_gb": size_cap_gb,
        "within_cap": within_cap,
        "required_missing": [],  # provably empty (any miss aborted above)
    }
    return assemble_report(
        manifest=manifest, character=character, source_root=src_real,
        output_dir=out_real, would_copy=would, totals=totals,
    )


# ---- CLI ---------------------------------------------------------------------

def _git_check_ignore(repo_root: Path) -> Callable[[str], bool]:
    def check(path: str) -> bool:
        candidates = [path] if path.endswith(os.sep) or path.endswith("/") else [path, path + "/"]
        for cand in candidates:
            r = subprocess.run(["git", "-C", str(repo_root), "check-ignore", "-q", cand], capture_output=True)
            if r.returncode == 0:
                return True
        return False
    return check


def _abspath(p: str, base: Path) -> str:
    q = Path(p)
    return str(q if q.is_absolute() else base / q)


def _print_summary(report: dict[str, Any]) -> None:
    t = report["totals"]
    print(f"[dry-run] runtime={report['runtime_name']} character={report['character']} "
          f"profile={report['language_profile']}")
    print(f"  source: {report['source_root']}")
    print(f"  output: {report['output_dir']}  (NOT created -- dry-run)")
    for cat in sorted(report["category_sizes"]):
        c = report["category_sizes"][cat]
        print(f"  {cat:24s} {c['files']:5d} files  {c['bytes'] / 1024 ** 2:10.1f} MB")
    print(f"  TOTAL {t['file_count']} files  {t['total_gb']} GB  "
          f"(cap {t['size_cap_gb']} GB, within={t['within_cap']})")
    print(f"  required_missing: {t['required_missing']}")
    print(f"  import_preflight: {report['import_preflight']['status']}   parity: {report['parity']['status']}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="RVC (Applio) slim runtime DRY-RUN planner (Step1).")
    ap.add_argument("--manifest", default=DEFAULT_MANIFEST)
    ap.add_argument("--character", default=None, help="pack name (default: the sole pack)")
    ap.add_argument("--source", default=None, help="override source.applio_root")
    ap.add_argument("--output", "--out", default=None, help="override output.root")
    ap.add_argument("--build", action="store_true", help="(NOT implemented in Step1 -- aborts)")
    ap.add_argument("--json", action="store_true", help="also print the full report JSON")
    args = ap.parse_args(argv)

    if args.build:
        print("error: --build is not implemented in RVC Slim Step1 (dry-run planner only).", file=sys.stderr)
        return 2

    manifest = load_manifest(_abspath(args.manifest, _REPO_ROOT))
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

    source_root = _abspath(args.source or manifest["source"]["applio_root"], _REPO_ROOT)
    output_dir = _abspath(args.output or manifest["output"]["root"], _REPO_ROOT)

    try:
        report = plan_build(
            source_root=source_root, manifest=manifest, output_dir=output_dir,
            character=character, check_ignore=_git_check_ignore(_REPO_ROOT),
        )
    except BuildAbort as exc:
        print(f"BUILD ABORTED: {exc}", file=sys.stderr)
        return 1

    _print_summary(report)
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

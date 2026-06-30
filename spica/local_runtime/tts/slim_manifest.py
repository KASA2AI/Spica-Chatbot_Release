"""Pure planning + validation logic for the TTS slim runtime (LOCAL_RUNTIME_PLAN B1).

This is the DECISION logic the (future) ``build_tts_slim.py`` orchestrates -- it does
NO rsync / no real model copy / no production wiring. Everything here is synthetic-tree
testable (operates on provided path lists + small temp files), so the CI tests need no
GPT-SoVITS / GPU / torch / transformers.

keep/exclude semantics: a file is INCLUDED iff it matches a ``keep`` glob AND no
``exclude`` glob (exclude wins). ``**`` spans path segments; ``*`` stays within one.
§3.3: no os.getenv / os.environ here.
"""

from __future__ import annotations

import glob
import hashlib
import os
import posixpath
import re
from typing import Any, Callable, Iterable

import yaml


# ---- manifest load + schema validation ---------------------------------------

def load_manifest(path: str | os.PathLike) -> dict[str, Any]:
    """yaml.safe_load the manifest. Raises on a non-mapping document."""
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise ValueError("tts slim manifest must be a YAML mapping")
    return data


def validate_manifest(manifest: dict[str, Any]) -> None:
    """Validate the manifest schema; raise ValueError listing ALL problems."""
    errors: list[str] = []

    def need(cond: bool, msg: str) -> None:
        if not cond:
            errors.append(msg)

    need(isinstance(manifest.get("version"), int), "version: missing/not int")
    need(manifest.get("language_profile") in ("ja_only",), "language_profile: must be ja_only (this cut)")
    need(isinstance(manifest.get("source_vendored_root"), str), "source_vendored_root: missing/not str")

    rb = manifest.get("runtime_base")
    if not isinstance(rb, dict):
        errors.append("runtime_base: missing/not mapping")
    else:
        need(isinstance(rb.get("keep"), list) and bool(rb["keep"]), "runtime_base.keep: non-empty list required")
        need(isinstance(rb.get("exclude"), list), "runtime_base.exclude: list required")

    lic = manifest.get("licenses")
    if not isinstance(lic, dict):
        errors.append("licenses: missing/not mapping")
    else:
        need(isinstance(lic.get("keep"), list), "licenses.keep: list required")
        need(isinstance(lic.get("expect_license_for"), list), "licenses.expect_license_for: list required")

    cps = manifest.get("character_packs")
    if not isinstance(cps, dict) or not cps:
        errors.append("character_packs: non-empty mapping required")
    else:
        for name, spec in cps.items():
            if not isinstance(spec, dict):
                errors.append(f"character_packs.{name}: not mapping")
                continue
            for key in ("version", "gpt_weight", "sovits_weight", "config_source"):
                need(key in spec, f"character_packs.{name}.{key}: missing")

    wp = manifest.get("writable_paths")
    if not isinstance(wp, list) or not wp:
        errors.append("writable_paths: non-empty list required")
    else:
        for i, entry in enumerate(wp):
            need(isinstance(entry, dict) and "path" in entry and "risk" in entry,
                 f"writable_paths[{i}]: needs path + risk")

    need(isinstance(manifest.get("generated_files"), list), "generated_files: list required")

    val = manifest.get("validation")
    if not isinstance(val, dict):
        errors.append("validation: missing/not mapping")
    else:
        for key in ("comparator", "waveform_rmse_max", "len_ratio_dev_max", "baseline"):
            need(key in val, f"validation.{key}: missing")

    out = manifest.get("output")
    if not isinstance(out, dict):
        errors.append("output: missing/not mapping")
    else:
        need(out.get("must_be_gitignored") is True, "output.must_be_gitignored: must be true")
        need("default_dir" in out, "output.default_dir: missing")
        need(isinstance(out.get("size_cap_gb"), (int, float)), "output.size_cap_gb: number required")

    if errors:
        raise ValueError("invalid tts slim manifest:\n  - " + "\n  - ".join(errors))


# ---- keep / exclude glob matching --------------------------------------------

def _glob_to_regex(glob: str) -> "re.Pattern[str]":
    """``**/`` -> zero-or-more dirs; ``**`` -> any; ``*`` -> within one segment;
    ``?`` -> one non-slash char. Everything else escaped."""
    out = ["^"]
    i, n = 0, len(glob)
    while i < n:
        if glob[i : i + 3] == "**/":
            out.append("(?:.*/)?")
            i += 3
        elif glob[i : i + 2] == "**":
            out.append(".*")
            i += 2
        elif glob[i] == "*":
            out.append("[^/]*")
            i += 1
        elif glob[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(glob[i]))
            i += 1
    out.append("$")
    return re.compile("".join(out))


def matches_any(rel_path: str, globs: Iterable[str]) -> bool:
    rel = rel_path.replace("\\", "/")
    return any(_glob_to_regex(g).match(rel) for g in globs)


def should_include(rel_path: str, keep: Iterable[str], exclude: Iterable[str]) -> bool:
    """INCLUDED iff matches a keep glob AND no exclude glob (exclude wins)."""
    return matches_any(rel_path, keep) and not matches_any(rel_path, exclude)


def plan_includes(rel_paths: Iterable[str], manifest: dict[str, Any]) -> dict[str, list[str]]:
    keep = manifest["runtime_base"]["keep"]
    exclude = manifest["runtime_base"]["exclude"]
    included, excluded = [], []
    for rel in rel_paths:
        (included if should_include(rel, keep, exclude) else excluded).append(rel)
    return {"included": sorted(included), "excluded": sorted(excluded)}


def unmatched_keep_globs(rel_paths: Iterable[str], keep: Iterable[str], exclude: Iterable[str]) -> list[str]:
    """The ``keep`` globs that match NO (non-excluded) path -- i.e. a declared base
    asset is absent from the source. Each keep glob is treated as REQUIRED: every glob
    in ``runtime_base.keep`` is a load-path-confirmed critical asset, so a glob matching
    nothing means the slim runtime would be broken and the build must abort. (licenses
    are NOT required -- a missing license is only a warning.)"""
    rels = list(rel_paths)
    return [g for g in keep if not any(should_include(r, [g], exclude) for r in rels)]


# ---- path safety (no escape, containment, size cap, symlinks) ----------------

def is_safe_rel(rel: str) -> bool:
    """A manifest-relative path that cannot escape its root, regardless of host OS.

    Rejects: empty, embedded NUL, POSIX-absolute (``/x``), UNC (``\\\\server\\share``
    / ``//server/share``), Windows drive paths -- absolute ``C:\\x`` / ``C:/x`` AND
    drive-relative ``C:x`` -- and any ``..`` segment. A drive letter is detected
    cross-platform (``os.path.isabs`` is host-dependent and would miss ``C:\\`` on
    Linux). The build adds a realpath containment check on top of this (defense in
    depth -- see ``is_within`` / build_tts_slim)."""
    if not rel or "\x00" in rel:
        return False
    norm = rel.replace("\\", "/")
    if norm.startswith("/"):  # POSIX-absolute, or UNC (\\server -> //server)
        return False
    if len(rel) >= 2 and rel[0].isalpha() and rel[1] == ":":  # C:\ , C:/ , C: , C:x
        return False
    return ".." not in norm.split("/")


def is_within(child: str, root: str) -> bool:
    """Pure normalized containment: ``child`` lies inside ``root`` after ``normpath``
    (NO symlink resolution).

    CONSTRAINT: any caller acting on real filesystem paths (``build_tts_slim``) MUST
    ``os.path.realpath`` BOTH ``child`` and ``root`` BEFORE calling this -- normpath
    alone does not resolve a symlink that points outside ``root``, so a symlinked
    output dir could escape. The build's ``test_source_target_realpath_containment``
    locks that the realpath is applied first."""
    c = os.path.normpath(child)
    r = os.path.normpath(root)
    return c == r or c.startswith(r + os.sep)


def within_size_cap(total_bytes: int, cap_gb: float) -> bool:
    return total_bytes <= int(cap_gb * (1024 ** 3))


def collect_files(root: str, *, follow_symlinks: bool = False) -> list[str]:
    """List regular files under ``root`` as posix rel paths, SKIPPING symlinks and
    (when follow_symlinks=False) not descending into symlinked dirs. The build uses
    this to enumerate the source; symlinks are never followed (safety)."""
    out: list[str] = []
    root = os.path.abspath(root)
    for dirpath, dirnames, filenames in os.walk(root, followlinks=follow_symlinks):
        if not follow_symlinks:
            dirnames[:] = [d for d in dirnames if not os.path.islink(os.path.join(dirpath, d))]
        for name in filenames:
            full = os.path.join(dirpath, name)
            if os.path.islink(full):
                continue  # never copy a symlink
            out.append(posixpath.relpath(full, root).replace("\\", "/"))
    return sorted(out)


def sha256_of(path: str | os.PathLike) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def output_is_gitignored(out_path: str, check_ignore: Callable[[str], bool]) -> bool:
    """The build must refuse to run unless the output dir is gitignored. The check
    is injected (real = ``git check-ignore``; tests mock it)."""
    return bool(check_ignore(out_path))


# ---- character pack: self-contained, relocatable config ----------------------

def build_character_config(pack_spec: dict[str, Any], tts_yaml: dict[str, Any]) -> dict[str, Any]:
    """Generate a SELF-CONTAINED character.yaml: every path RELATIVE to the pack root
    (``GPT_weights/`` / ``SoVITS_weights/`` / ``reference/<emotion>/``), so the pack
    runs after relocation and never references dev-machine spica_data paths."""
    config: dict[str, Any] = {
        "version": pack_spec["version"],
        "gpt_model_path": "GPT_weights/" + posixpath.basename(pack_spec["gpt_weight"]),
        "sovits_model_path": "SoVITS_weights/" + posixpath.basename(pack_spec["sovits_weight"]),
        "ref_language": tts_yaml.get("ref_language", "日文"),
        "target_language": tts_yaml.get("target_language", "日文"),
        "emotions": {},
    }
    for emotion, spec in (tts_yaml.get("emotions") or {}).items():
        entry: dict[str, Any] = {}
        ref = spec.get("ref_audio_path")
        if ref:
            entry["ref_audio_path"] = f"reference/{emotion}/" + posixpath.basename(ref)
        if spec.get("prompt_text"):
            entry["prompt_text"] = spec["prompt_text"]
        elif spec.get("prompt_text_path"):
            entry["prompt_text_path"] = f"reference/{emotion}/" + posixpath.basename(spec["prompt_text_path"])
        # inp_refs is a DECLARED + actively-used v2ProPlus dependency (glob'd +
        # fused via sv_emb). It lives in a DEDICATED refs/ subdir, kept separate
        # from the primary ref so the runtime's glob(refs/*.wav) matches only these.
        if spec.get("inp_refs_path"):
            entry["inp_refs_path"] = f"reference/{emotion}/refs"
        if spec.get("ref_language"):
            entry["ref_language"] = spec["ref_language"]
        config["emotions"][emotion] = entry
    return config


def character_reference_files(tts_yaml: dict[str, Any]) -> list[dict[str, str]]:
    """{source, pack-relative target, category} for the PRIMARY ref wav + prompt of
    each emotion -- copied INTO the pack so it is self-contained. category is always
    'character_reference'. The per-emotion inp_refs wavs are enumerated separately
    (they need a directory listing -- see ``enumerate_audio_files`` +
    ``inp_refs_entries``) and tagged 'character_inp_refs'."""
    out: list[dict[str, str]] = []
    for emotion, spec in (tts_yaml.get("emotions") or {}).items():
        for key in ("ref_audio_path", "prompt_text_path"):
            src = spec.get(key)
            if src:
                out.append({
                    "source": src,
                    "target": f"reference/{emotion}/" + posixpath.basename(src),
                    "category": "character_reference",
                })
    return out


# inp_refs (multi-reference fusion): the vendored get_tts_wav globs the inp_refs dir
# for these audio extensions (lower- AND upper-case). Single source of truth so the
# slim planner enumerates EXACTLY what the runtime will load.
INP_REFS_AUDIO_EXTS = (".wav", ".mp3", ".m4a", ".flac", ".ogg", ".aac")


def enumerate_audio_files(directory: str) -> list[str]:
    """List audio files in ``directory`` the SAME way the vendored get_tts_wav globs
    an inp_refs dir: each ext, lower- AND upper-case, NON-recursive. Returns absolute
    paths, de-duplicated (case-insensitive FS) and sorted. No ``glob.escape`` -- mirrors
    get_tts_wav exactly (which does not escape); inp_refs dir paths are bracket-free."""
    found: set[str] = set()
    for ext in INP_REFS_AUDIO_EXTS:
        for pattern in (f"*{ext}", f"*{ext.upper()}"):
            for path in glob.glob(os.path.join(directory, pattern)):
                if os.path.isfile(path):
                    found.add(os.path.abspath(path))
    return sorted(found)


def inp_refs_entries(emotion: str, wav_paths: Iterable[str]) -> list[dict[str, str]]:
    """Map enumerated inp_refs wavs to pack targets under the emotion's DEDICATED
    ``refs/`` subdir -- physically separate from the primary ref so the runtime's
    ``glob(reference/<emotion>/refs/*.wav)`` matches exactly these and never the
    primary ref. category = 'character_inp_refs'."""
    return [
        {
            "source": w,
            "target": f"reference/{emotion}/refs/" + posixpath.basename(w),
            "category": "character_inp_refs",
        }
        for w in wav_paths
    ]


# ---- license status + build report -------------------------------------------

def license_status(expect_dirs: Iterable[str], found_license_rels: Iterable[str]) -> dict[str, list[str]]:
    """For each model dir in ``expect_license_for``, is a copied license under it?"""
    found = [p.replace("\\", "/") for p in found_license_rels]
    copied, missing = [], []
    for d in expect_dirs:
        d = d.replace("\\", "/")
        (copied if any(p.startswith(d + "/") for p in found) else missing).append(d)
    return {"copied": sorted(copied), "missing": sorted(missing)}


def assemble_build_report(
    *,
    manifest: dict[str, Any],
    character: str,
    files: list[dict[str, Any]],
    licenses: dict[str, list[str]],
    totals: dict[str, int],
) -> dict[str, Any]:
    """The build report: per-file size+sha256 (integrity / install verify), license
    copied/missing, writable-paths warning, parity PENDING (run audio_diff after)."""
    return {
        "manifest_version": manifest["version"],
        "language_profile": manifest["language_profile"],
        "character": character,
        "totals": totals,
        "files": files,  # each: {category, source, target, size_bytes, sha256}
        "licenses": licenses,
        "writable_paths": [f"{w['path']} ({w['risk']})" for w in manifest.get("writable_paths", [])],
        "parity": "PENDING",
    }

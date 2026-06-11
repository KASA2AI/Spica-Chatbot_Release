"""P0b Layer A: resolved-config snapshot -- the migration gate.

Dumps every EFFECTIVE config value, resolved by the PRODUCTION loaders (never a
re-implementation), into one deterministic JSON. Before/after every P0b step the
dump must be byte-identical (``--diff`` exits 1 on any field change), so a
"value silently replaced by a default" or "env rename silently dropped" cannot
survive a step unnoticed.

Usage:
    python scripts/dump_resolved_config.py --out data/config_migration/baseline_step0.json
    python scripts/dump_resolved_config.py --diff data/config_migration/baseline_step0.json

Design (approved P0b plan, 一-Layer A):
- Entry-order simulation: each resolution pass calls ``load_secrets()`` FIRST,
  exactly like ``qt_overlay.main()`` -- the snapshot path IS the real startup
  path, so no F19-style priming-order divergence can hide here.
- Three passes, each in its own subprocess (clean dotenv/module state):
  FULL (real resolution), NO_ENV (all roster env names masked to ""; loaders
  treat empty as unset -- and "" survives ``load_dotenv(override=False)``
  re-priming because the var still exists), NO_FILE (loaders pointed at a
  nonexistent path). Per-leaf provenance is differential:
      FULL != NO_ENV  -> source=env
      FULL != NO_FILE -> source=file
      else            -> source=default
  NOTE source is a ROBUSTNESS attribution ("which layer effectively provides
  the value"); when env == file == default the source reads "default" even if
  the env var is set -- the separate ``env_set`` flag records that dimension.
  "A loader silently STOPPED reading env" is invisible here when values
  coincide; that failure mode is Layer B's job (synthetic distinct values).
- tts/visual are file-required loaders (raise on a missing file) and consume no
  env: their keys are attributed source=file by rule and skipped in NO_FILE.
- Secrets never land in plaintext: values under *api_key/secret/token/password*
  keys are recorded as sha256:<12 hex> (a swapped key still shows as a diff).
- env_audit: variable NAMES present in xiaosan.env (repo + parent) classified
  against the roster -> consumed / legacy / unconsumed. An env rename that
  drops a value shows twice: a value diff in its domain AND the old name
  falling out of "consumed".
- Baselines live in data/config_migration/ (gitignored: local machine paths +
  hashed secrets; this gate is per-machine by design -- Layer B is the
  committed, machine-independent half).
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

_MISSING_DIR = "/nonexistent/p0b_no_file_pass"
_FILE_REQUIRED_DOMAINS = {"tts", "visual"}
_SECRET_KEY_RE = re.compile(r"(api_key|apikey|secret|token|password)", re.IGNORECASE)
_PASSES = ("full", "no_env", "no_file")


# -- child: resolve one pass ---------------------------------------------------

def _resolve_pass(mode: str) -> dict:
    from spica.config.env_roster import (
        RESPEAKER_ENV_MAP,
        consumed_env_names,
    )

    if mode == "no_env":
        for name in sorted(consumed_env_names()):
            os.environ[name] = ""
    no_file = mode == "no_file"

    # FIRST: same call as qt_overlay.main() (CLAUDE.md #10 / F19).
    from spica.config.secrets import load_secrets

    secrets = load_secrets()

    from spica.config.manager import ConfigManager

    manager = (
        ConfigManager(config_path=f"{_MISSING_DIR}/app.yaml") if no_file else ConfigManager()
    )
    app_config = manager.load()

    domains: dict = {"app": app_config.model_dump()}
    domains["secrets"] = {"openai_api_key": secrets.openai_api_key}

    # P0b step 3: screen/song/plugins go through the SAME carrier switch
    # production uses (legacy file present -> whole old chain; absent ->
    # app.yaml chain). The pass's app_config already reflects masked env /
    # missing app.yaml, and legacy_path is pointed at nowhere in NO_FILE.
    from agent_tools.function_tools.screen.config import resolve_effective_screen_config

    domains["screen"] = dataclasses.asdict(
        resolve_effective_screen_config(
            config=app_config,
            legacy_path=f"{_MISSING_DIR}/screen.json" if no_file else None,
        )
    )

    from agent_tools.function_tools.song.config import resolve_effective_song_config

    domains["song"] = resolve_effective_song_config(
        config=app_config,
        legacy_path=f"{_MISSING_DIR}/song.json" if no_file else None,
    )

    if not no_file:
        # Effective tts/visual files resolved the way app_host.initialize() does
        # (character package override wins over data/config defaults).
        from spica.conversation.character_loader import DEFAULT_SPICA_SKILL_DIR
        from spica.core.character import load_character_package

        package = load_character_package(
            app_config.character.package_dir or DEFAULT_SPICA_SKILL_DIR
        )
        from agent_tools.tts.manager import load_tts_config

        domains["tts"] = (
            load_tts_config(package.tts_config_path)
            if package.tts_config_path
            else load_tts_config()
        )
        from agent_tools.config_io import read_config_file
        from agent_tools.visual.diff_service import DEFAULT_CONFIG_PATH as _VISUAL_DEFAULT

        visual_path = package.visual_config_path or _VISUAL_DEFAULT
        domains["visual"] = read_config_file(visual_path)

    from spica.plugins.manifest import resolve_effective_plugin_entries

    domains["plugins"] = [
        dataclasses.asdict(entry)
        for entry in resolve_effective_plugin_entries(
            config=app_config,
            legacy_path=f"{_MISSING_DIR}/plugins.yaml" if no_file else None,
        )
    ]

    from ui.overlay_config import load_overlay_config

    domains["overlay"] = dataclasses.asdict(
        load_overlay_config(Path(f"{_MISSING_DIR}/overlay.json") if no_file else None)
    )

    # runtime_cache / respeaker have no dedicated loader before P0b step 1; the
    # manager/runtime_env functions exist from step 1 on. Raw passthrough
    # domains normalize "" -> None (every consumer treats them as equally unset).
    try:
        from spica.config.runtime_env import resolve_runtime_cache_root

        cache_root = resolve_runtime_cache_root()
    except ImportError:  # pre-step-1 world: mirror service.py:_configure_runtime_cache_dirs
        from agent_tools.tts.gptsovits.service import DEFAULT_RUNTIME_CACHE_ROOT

        cache_root = Path(os.getenv("SPICA_RUNTIME_CACHE_DIR") or DEFAULT_RUNTIME_CACHE_ROOT).resolve()
    domains["runtime_cache"] = {"cache_root": str(cache_root)}

    try:
        from spica.config.manager import respeaker_env_overrides

        raw_respeaker = respeaker_env_overrides()
    except ImportError:  # pre-step-1 world: mirror hardware/respeaker reads
        raw_respeaker = {field: os.getenv(name) for field, name in RESPEAKER_ENV_MAP.items()}
    domains["respeaker"] = {field: (value or None) for field, value in raw_respeaker.items()}

    _redact_tree(domains)
    return domains


def _redact_tree(node) -> None:
    """In-place: hash any value under a secret-looking key (never plaintext)."""
    if isinstance(node, dict):
        for key, value in node.items():
            if isinstance(value, (dict, list)):
                _redact_tree(value)
            elif _SECRET_KEY_RE.search(str(key)) and value is not None:
                text = str(value)
                node[key] = (
                    "<empty>"
                    if text == ""
                    else "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
                )
    elif isinstance(node, list):
        for item in node:
            _redact_tree(item)


# -- parent: orchestrate, attribute, audit, diff -------------------------------

def _run_pass(mode: str) -> dict:
    result = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "--pass", mode],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
    )
    if result.returncode != 0:
        sys.stderr.write(result.stderr)
        raise RuntimeError(f"resolution pass '{mode}' failed (exit {result.returncode})")
    return json.loads(result.stdout)


def _flatten(node, prefix: str = "") -> dict:
    flat: dict = {}
    if isinstance(node, dict) and node:
        for key, value in node.items():
            flat.update(_flatten(value, f"{prefix}.{key}" if prefix else str(key)))
    elif isinstance(node, list) and node:
        for index, value in enumerate(node):
            flat.update(_flatten(value, f"{prefix}[{index}]"))
    else:
        flat[prefix] = node  # scalar, or empty dict/list as a leaf
    return flat


def _env_attribution() -> tuple[dict, frozenset]:
    from spica.config.env_roster import (
        APP_ENV_MAP,
        RESPEAKER_ENV_MAP,
        RUNTIME_CACHE_ENV_MAP,
        SCREEN_ENV_MAP,
        SECRETS_ENV_MAP,
        consumed_env_names,
    )

    attribution: dict[str, str] = {}
    for domain, mapping in (
        ("app", APP_ENV_MAP),
        # P0b 2a: AppConfig grew the typed screen section; its leaves fold the
        # same SPICA_SCREEN_* env as the loader's screen.* domain.
        ("app.screen", SCREEN_ENV_MAP),
        ("secrets", SECRETS_ENV_MAP),
        ("screen", SCREEN_ENV_MAP),
        ("runtime_cache", RUNTIME_CACHE_ENV_MAP),
        ("respeaker", RESPEAKER_ENV_MAP),
    ):
        for field_path, env_name in mapping.items():
            attribution[f"{domain}.{field_path}"] = env_name
    return attribution, consumed_env_names()


_ENV_LINE_RE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")


def _env_audit() -> dict:
    from spica.config.env_roster import LEGACY_ENV_VARS, consumed_env_names

    consumed = consumed_env_names()
    audit: dict = {}
    for env_file in (REPO_ROOT / "xiaosan.env", REPO_ROOT.parent / "xiaosan.env"):
        if not env_file.is_file():
            continue
        names: list[str] = []
        for line in env_file.read_text(encoding="utf-8").splitlines():
            if line.lstrip().startswith("#"):
                continue
            match = _ENV_LINE_RE.match(line)
            if match:
                names.append(match.group(1))
        audit[str(env_file)] = {
            "consumed": sorted(n for n in names if n in consumed),
            "legacy": sorted(n for n in names if n in LEGACY_ENV_VARS),
            "unconsumed": sorted(
                n for n in names if n not in consumed and n not in LEGACY_ENV_VARS
            ),
        }
    return audit


def build_snapshot() -> dict:
    # The parent computes env_set from its own environment -- prime it the same
    # way the children (and qt_overlay.main()) do, or every env_set reads False.
    from spica.config.secrets import load_secrets

    load_secrets()
    full = _flatten(_run_pass("full"))
    no_env = _flatten(_run_pass("no_env"))
    no_file = _flatten(_run_pass("no_file"))
    attribution, _ = _env_attribution()

    missing = object()
    leaves: dict = {}
    for key in sorted(full):
        value = full[key]
        domain = key.split(".", 1)[0]
        if domain in _FILE_REQUIRED_DOMAINS:
            source = "file"
        elif no_env.get(key, missing) != value:
            source = "env"
        elif no_file.get(key, missing) != value:
            source = "file"
        else:
            source = "default"
        # attribution keys never carry [i] suffixes; direct lookup is enough
        env_var = attribution.get(key)
        leaf = {"value": value, "source": source}
        if env_var is not None:
            leaf["env_var"] = env_var
            leaf["env_set"] = bool(os.getenv(env_var))
        leaves[key] = leaf

    return {"format": 1, "domains": leaves, "env_audit": _env_audit()}


def diff_snapshots(baseline: dict, current: dict) -> list[str]:
    lines: list[str] = []
    base_domains = baseline.get("domains", {})
    cur_domains = current.get("domains", {})
    for key in sorted(set(base_domains) | set(cur_domains)):
        if key not in cur_domains:
            lines.append(f"REMOVED {key}: was {json.dumps(base_domains[key], ensure_ascii=False)}")
        elif key not in base_domains:
            lines.append(f"ADDED   {key}: now {json.dumps(cur_domains[key], ensure_ascii=False)}")
        elif base_domains[key] != cur_domains[key]:
            lines.append(
                f"CHANGED {key}: {json.dumps(base_domains[key], ensure_ascii=False)}"
                f" -> {json.dumps(cur_domains[key], ensure_ascii=False)}"
            )
    base_audit = baseline.get("env_audit", {})
    cur_audit = current.get("env_audit", {})
    if base_audit != cur_audit:
        lines.append(
            f"CHANGED env_audit: {json.dumps(base_audit, ensure_ascii=False)}"
            f" -> {json.dumps(cur_audit, ensure_ascii=False)}"
        )
    return lines


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--pass", dest="pass_mode", choices=_PASSES, help="(internal) child pass")
    parser.add_argument("--out", help="write snapshot JSON to this path")
    parser.add_argument("--diff", help="compare a fresh snapshot against this baseline JSON")
    args = parser.parse_args()

    if args.pass_mode:
        print(json.dumps(_resolve_pass(args.pass_mode), ensure_ascii=False, sort_keys=True))
        return 0

    snapshot = build_snapshot()
    rendered = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, indent=2)

    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(rendered + "\n", encoding="utf-8")
        print(f"snapshot written: {out_path} ({len(snapshot['domains'])} leaves)")

    if args.diff:
        baseline = json.loads(Path(args.diff).read_text(encoding="utf-8"))
        lines = diff_snapshots(baseline, snapshot)
        if lines:
            print(f"RESOLVED-CONFIG DIFF vs {args.diff}: {len(lines)} difference(s)")
            for line in lines:
                print("  " + line)
            return 1
        print(f"resolved config identical to baseline {args.diff} "
              f"({len(snapshot['domains'])} leaves compared)")
        return 0

    if not args.out:
        print(rendered)
    return 0


if __name__ == "__main__":
    sys.exit(main())

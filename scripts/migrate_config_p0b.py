"""P0b step 3-2: migrate the legacy config carriers into data/config/app.yaml.

Moves the FILE CONTENT (the override dicts) -- never the env-folded result;
env overrides stay in the environment. Three legacy carriers:

    config/screen_vision_config.json          -> app.yaml `screen:` section
    agent_tools/.../song/song_config.json     -> app.yaml `song:` section
    data/config/plugins.yaml                  -> app.yaml `plugins:` section

Safety (approved P0b step-3 plan):
- IDEMPOTENCY: refuses to run if app.yaml already holds any target section.
- PREVIEW + DOUBLE ASSERTION before touching anything real:
  * field-level  -- the parsed preview sections equal the legacy contents
    verbatim;
  * effective-level -- the OLD chain resolution equals the NEW chain
    resolution (whole-object compare), so a default silently replacing a
    migrated value has nowhere to hide.
- Text-append into app.yaml (keeps the template's comment header; a full
  yaml dump would drop every comment).
- Legacy files are RENAMED to *.migrated -- never deleted. Rollback =
  revert the commit + rename them back; zero data loss.
- A final post-rename assertion re-checks the PRODUCTION resolvers (no
  injection) against the captured old-chain values.

Run, then gate with the Layer A snapshot:
    python scripts/dump_resolved_config.py --diff data/config_migration/after_step3_1.json
"""

from __future__ import annotations

import dataclasses
import json
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

APP_YAML = REPO_ROOT / "data" / "config" / "app.yaml"
PREVIEW = REPO_ROOT / "data" / "config_migration" / "app.yaml.preview"
MARKER = "# --- migrated by migrate_config_p0b ---"
_MISSING = str(REPO_ROOT / "data" / "config_migration" / "nonexistent_legacy")


def main() -> int:
    # Entry-order simulation: prime env exactly like every other entry point.
    from spica.config.secrets import load_secrets

    load_secrets()

    from agent_tools.function_tools.screen.config import (
        DEFAULT_CONFIG_PATH as SCREEN_JSON,
        _LOCAL_CONFIG_KEYS,
        load_screen_config,
        resolve_effective_screen_config,
    )
    from agent_tools.function_tools.song.config import (
        DEFAULT_CONFIG_PATH as SONG_JSON,
        load_song_config,
        resolve_effective_song_config,
    )
    from spica.config.manager import ConfigManager
    from spica.plugins.manifest import (
        DEFAULT_MANIFEST_PATH as PLUGINS_YAML,
        load_plugin_manifest,
        resolve_effective_plugin_entries,
    )

    legacy_present = {
        "screen": SCREEN_JSON.exists(),
        "song": SONG_JSON.exists(),
        "plugins": PLUGINS_YAML.is_file(),
    }
    if not any(legacy_present.values()):
        print("nothing to migrate: no legacy carrier present")
        return 0
    print(f"legacy carriers present: {[k for k, v in legacy_present.items() if v]}")

    # -- idempotency guard ------------------------------------------------------
    current_data = yaml.safe_load(APP_YAML.read_text(encoding="utf-8")) if APP_YAML.exists() else None
    current_data = current_data if isinstance(current_data, dict) else {}
    already = [k for k in ("screen", "song", "plugins") if k in current_data]
    if already:
        print(f"REFUSING to run: app.yaml already holds section(s) {already}")
        return 2

    # -- capture the OLD chain's effective values (the comparison anchor) -------
    old_screen = load_screen_config() if legacy_present["screen"] else None
    old_song = load_song_config() if legacy_present["song"] else None
    old_plugins = load_plugin_manifest() if legacy_present["plugins"] else None

    # -- read the legacy CONTENTS (override dicts, not resolved values) ---------
    sections: dict = {}
    if legacy_present["screen"]:
        raw = json.loads(SCREEN_JSON.read_text(encoding="utf-8"))
        sections["screen"] = {k: v for k, v in raw.items() if k in _LOCAL_CONFIG_KEYS}
        dropped = sorted(set(raw) - _LOCAL_CONFIG_KEYS)
        if dropped:
            print(f"screen: dropping non-local legacy keys (same filter the loader used): {dropped}")
    if legacy_present["song"]:
        sections["song"] = json.loads(SONG_JSON.read_text(encoding="utf-8"))
    if legacy_present["plugins"]:
        data = yaml.safe_load(PLUGINS_YAML.read_text(encoding="utf-8")) or {}
        raw_list = data.get("plugins") if isinstance(data, dict) else None
        sections["plugins"] = raw_list if isinstance(raw_list, list) else []

    # -- compose the new app.yaml TEXT (append; keep the comment template) ------
    appended = (
        "\n" + MARKER + "\n"
        + yaml.safe_dump(sections, allow_unicode=True, sort_keys=False, default_flow_style=False)
    )
    new_text = APP_YAML.read_text(encoding="utf-8") + appended

    # -- PREVIEW + double assertion BEFORE touching the real file ---------------
    PREVIEW.parent.mkdir(parents=True, exist_ok=True)
    PREVIEW.write_text(new_text, encoding="utf-8")

    parsed = yaml.safe_load(PREVIEW.read_text(encoding="utf-8"))
    for key, source in sections.items():
        assert parsed.get(key) == source, f"FIELD-LEVEL mismatch in section {key!r}"
    print(f"field-level assertion OK: {list(sections)} sections round-trip verbatim")

    preview_config = ConfigManager(PREVIEW).load()
    if old_screen is not None:
        new_screen = resolve_effective_screen_config(
            config=preview_config, legacy_path=_MISSING + "/screen.json"
        )
        assert new_screen == old_screen, (
            f"EFFECTIVE-LEVEL mismatch (screen): old={old_screen} new={new_screen}"
        )
        print("effective-level assertion OK: screen old chain == new chain "
              f"(revision={new_screen.revision!r}, model_id={new_screen.model_id!r})")
    if old_song is not None:
        new_song = resolve_effective_song_config(
            config=preview_config, legacy_path=_MISSING + "/song.json"
        )
        a, b = dict(old_song), dict(new_song)
        label_old, label_new = a.pop("_config_path"), b.pop("_config_path")
        assert a == b, "EFFECTIVE-LEVEL mismatch (song)"
        print(f"effective-level assertion OK: song old chain == new chain "
              f"(carrier label {label_old!r} -> {label_new!r}, registered)")
    if old_plugins is not None:
        new_plugins = resolve_effective_plugin_entries(
            config=preview_config, legacy_path=_MISSING + "/plugins.yaml"
        )
        assert new_plugins == old_plugins, "EFFECTIVE-LEVEL mismatch (plugins)"
        print(f"effective-level assertion OK: plugins ({len(new_plugins)} entries)")

    # -- commit: write the real app.yaml, re-verify, rename legacy --------------
    APP_YAML.write_text(new_text, encoding="utf-8")
    reparsed = yaml.safe_load(APP_YAML.read_text(encoding="utf-8"))
    for key, source in sections.items():
        assert reparsed.get(key) == source, f"POST-WRITE mismatch in section {key!r}"
    print("post-write field-level re-assertion OK")

    renames: list[tuple[Path, Path]] = []
    for present, path in (
        (legacy_present["screen"], SCREEN_JSON),
        (legacy_present["song"], SONG_JSON),
        (legacy_present["plugins"], PLUGINS_YAML),
    ):
        if present:
            target = path.with_name(path.name + ".migrated")
            path.rename(target)
            renames.append((path, target))
            print(f"renamed: {path} -> {target.name}")

    # -- final: the PRODUCTION resolvers (no injection) must now equal old ------
    if old_screen is not None:
        assert resolve_effective_screen_config() == old_screen, "POST-RENAME screen mismatch"
    if old_song is not None:
        final_song = resolve_effective_song_config()
        a, b = dict(old_song), dict(final_song)
        a.pop("_config_path"), b.pop("_config_path")
        assert a == b, "POST-RENAME song mismatch"
    if old_plugins is not None:
        assert resolve_effective_plugin_entries() == old_plugins, "POST-RENAME plugins mismatch"
    print("post-rename PRODUCTION-resolver assertion OK (new chain live, values unchanged)")
    print("\nrollback: revert the commit and rename the *.migrated files back.")
    print("now gate with: python scripts/dump_resolved_config.py "
          "--diff data/config_migration/after_step3_1.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())

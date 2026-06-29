"""Controlled, one-time import of the vendored GPT-SoVITS inference callables (A2).

This is the ONLY place the vendored ``inference_webui`` glue + the sys.path / cwd
setup live now -- moved verbatim out of ``service._lazy_import`` so service.py no
longer touches it (A2 goal). It imports the SAME vendored callables
(``change_gpt_weights`` / ``change_sovits_weights`` / ``get_tts_wav`` / i18n) -- NOT
a rewrite, NOT a model-def copy (D1).

A3 TODO: the sys.path mutation + the import-time ``pushd`` here are the residual
"glue". They are CENTRALIZED + protected (the pushd context manager always restores
cwd); A3 will tighten/eliminate the cwd-relative coupling.

§3.3: NO os.getenv / os.environ here. The env priming the vendored runtime needs is
DELEGATED to the sanctioned config-layer shim ``spica.config.runtime_env`` (this
module never reads/writes env itself), preserving the FINDINGS #19 timing (primed
BEFORE the vendored import). ``os.chdir`` / ``sys.path`` are not env access.
"""

from __future__ import annotations

import contextlib
import os
import sys
from pathlib import Path
from typing import Any

# Cache the imported callables per gptsovits_root (idempotent: import once).
_IMPORT_CACHE: dict[str, tuple] = {}


@contextlib.contextmanager
def pushd(path: Path):
    """cwd <- path for the block, ALWAYS restored (finally). The A3 residual: the
    vendored code does cwd-relative loads, so load/synthesize still run under this."""
    old_cwd = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(old_cwd)


def _ensure_import_paths(gptsovits_root: Path) -> None:
    package_dir = gptsovits_root / "GPT_SoVITS"
    import_paths = [str(gptsovits_root), str(package_dir), str(package_dir / "eres2net")]
    for import_path in reversed(import_paths):
        if import_path in sys.path:
            sys.path.remove(import_path)
        sys.path.insert(0, import_path)


def _is_under_any_root(path: Path, roots: list[Path]) -> bool:
    for root in roots:
        try:
            path.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def _drop_conflicting_module(module_name: str, allowed_roots: list[Path]) -> None:
    """Drop a stdlib/3rd-party ``tools`` / ``utils`` already imported from OUTSIDE the
    vendored tree, so the vendored package's same-named modules win (moved verbatim
    from service._lazy_import)."""
    module = sys.modules.get(module_name)
    if module is None:
        return
    roots = [root.resolve() for root in allowed_roots]
    paths: list[Path] = []
    module_file = getattr(module, "__file__", None)
    if module_file:
        paths.append(Path(module_file).resolve())
    paths.extend(Path(p).resolve() for p in getattr(module, "__path__", []))
    if paths and any(_is_under_any_root(p, roots) for p in paths):
        return
    del sys.modules[module_name]


def import_gptsovits_inference(gptsovits_root: str | Path) -> tuple[Any, Any, Any, Any]:
    """Import (once, cached) the vendored callables, returning
    ``(change_gpt_weights, change_sovits_weights, get_tts_wav, i18n)``.

    Primes the vendored runtime env FIRST (FINDINGS #19), sets up the import paths,
    drops conflicting ``tools``/``utils``, then imports under pushd (the vendored
    import does cwd-relative work)."""
    root = Path(gptsovits_root).resolve()
    key = str(root)
    cached = _IMPORT_CACHE.get(key)
    if cached is not None:
        return cached

    # Env priming via the sanctioned config-layer shim (NOT done in this module).
    from spica.config.runtime_env import (
        prime_vendored_runtime_cache_env,
        strip_proxy_env_for_vendored_runtime,
    )

    prime_vendored_runtime_cache_env()
    strip_proxy_env_for_vendored_runtime()

    _ensure_import_paths(root)
    package_dir = root / "GPT_SoVITS"
    _drop_conflicting_module("tools", [root])
    _drop_conflicting_module("utils", [package_dir])

    with pushd(root):
        from tools.i18n.i18n import I18nAuto
        from GPT_SoVITS.inference_webui import (
            change_gpt_weights,
            change_sovits_weights,
            get_tts_wav,
        )

        i18n = I18nAuto()

    funcs = (change_gpt_weights, change_sovits_weights, get_tts_wav, i18n)
    _IMPORT_CACHE[key] = funcs
    return funcs

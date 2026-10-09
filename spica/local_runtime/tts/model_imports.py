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
import gc
import os
import sys
from pathlib import Path
from typing import Any
from threading import RLock

# Cache the imported callables per gptsovits_root (idempotent: import once).
_IMPORT_CACHE: dict[str, tuple] = {}
_TTS_PIPELINE_CACHE: dict[str, tuple] = {}
# The vendor modules own process-global models and cwd-sensitive weight loaders.
# A complete service load + inference operation must hold this same lock.
INFERENCE_LOCK = RLock()


def restore_inference_frontend(funcs: tuple) -> None:
    """Restore the lazy-releasable HuBERT frontend without reimporting modules."""
    module = sys.modules.get(getattr(funcs[2], "__module__", ""))
    if module is None or module.__name__ != "GPT_SoVITS.inference_webui":
        return
    if module.ssl_model is None:
        model = module.cnhubert.get_model()
        if module.is_half:
            model = model.half()
        module.ssl_model = model.to(module.device)


def release_inference_models(funcs: tuple) -> None:
    """Drop the vendor's owning references, not just the adapter's references."""
    module = sys.modules.get(getattr(funcs[2], "__module__", ""))
    if module is None or module.__name__ != "GPT_SoVITS.inference_webui":
        return
    for name in ("ssl_model", "vq_model", "t2s_model", "bert_model", "tokenizer",
                 "bigvgan_model", "hifigan_model", "sv_cn_model", "sr_model"):
        if hasattr(module, name):
            setattr(module, name, None)
    for name in ("cache", "resample_transform_dict"):
        getattr(module, name, {}).clear()
    release_tensor_caches()


def release_tensor_caches() -> None:
    """Called under INFERENCE_LOCK after the last native operation has returned."""
    for name, fields in (
        ("module.mel_processing", ("mel_basis", "hann_window")),
        ("TTS_infer_pack.TTS", ("resample_transform_dict",)),
    ):
        module = sys.modules.get(name)
        if module is not None:
            for field in fields:
                getattr(module, field, {}).clear()
    gc.collect()
    # Never initialize Torch/CUDA merely because an unused service is closed.
    torch = sys.modules.get("torch")
    if torch is not None and torch.cuda.is_initialized():
        torch.cuda.empty_cache()
    from common.memory import release_native_buffers
    release_native_buffers()


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

    # Import-pushd remains for cnhubert, ./weight.json and the captured sv paths.
    # The shipped inference module freezes bert_path here but loads Chinese BERT
    # only on its first Chinese segment. Gradio is confined to its script entry.
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


def create_megumin_tts_pipeline(
    root: Path, *, gpt_path: str, sovits_path: str, config_path: Path
) -> Any:
    """Load the same vendor TTS class used for the accepted Megumin D samples.

    Kept separate from inference_webui: importing that module would also load
    its global models. Only this explicitly selected profile uses this factory.
    """
    from spica.config.runtime_env import (
        prime_vendored_runtime_cache_env,
        strip_proxy_env_for_vendored_runtime,
    )

    root = root.resolve()
    prime_vendored_runtime_cache_env()
    strip_proxy_env_for_vendored_runtime()
    _ensure_import_paths(root)
    _drop_conflicting_module("tools", [root])
    _drop_conflicting_module("utils", [root / "GPT_SoVITS"])
    with pushd(root):
        import torch

        key = str(root)
        if key not in _TTS_PIPELINE_CACHE:
            from TTS_infer_pack.TTS import TTS, TTS_Config

            _TTS_PIPELINE_CACHE[key] = (TTS, TTS_Config)
        TTS, TTS_Config = _TTS_PIPELINE_CACHE[key]
        pretrained = root / "GPT_SoVITS" / "pretrained_models"
        paths = {
            "t2s_weights_path": gpt_path,
            "vits_weights_path": sovits_path,
            "bert_base_path": str(pretrained / "chinese-roberta-wwm-ext-large"),
            "cnhuhbert_base_path": str(pretrained / "chinese-hubert-base"),
        }
        # TTS_Config silently substitutes base weights when a path is missing.
        # A missing character model must fail instead of changing her voice.
        for path in paths.values():
            if not Path(path).exists():
                raise FileNotFoundError(path)
        cuda = torch.cuda.is_available()
        cfg = TTS_Config({"custom": {
            **paths, "device": "cuda:0" if cuda else "cpu",
            "is_half": cuda, "version": "v2ProPlus",
        }})
        config_path = config_path.resolve()
        config_path.parent.mkdir(parents=True, exist_ok=True)
        cfg.configs_path = str(config_path)
        return TTS(cfg)

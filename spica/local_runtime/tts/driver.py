"""Thin GPT-SoVITS v2pro inference driver (LOCAL_RUNTIME_PLAN cut 2, A2).

Wraps the vendored ``change_*_weights`` / ``get_tts_wav`` (imported once via
``model_imports``) behind a clean Spica-owned object so ``service.py`` stops
touching ``inference_webui`` glue directly. The vendored MODEL CLASSES do the
work -- this is NOT a get_tts_wav rewrite and NOT a model-def copy (D1). v2pro only.

The pushd around load/synthesize is the A3 residual (the vendored code does
cwd-relative loads): a PROTECTED context manager that always restores cwd, held
only while the vendored call runs (synthesis is materialized inside the block, not
across yields).
"""

from __future__ import annotations

from pathlib import Path
from threading import RLock
from typing import Any, Iterator

from spica.local_runtime.tts.model_imports import import_gptsovits_inference, pushd


class GptSovitsV2ProDriver:
    """Owns the vendored inference callables + the loaded-weight cache. Thread-safe.

    Callables can be INJECTED (tests) to bypass the vendored import entirely; left
    as None they are imported lazily once via ``model_imports``."""

    def __init__(
        self,
        gptsovits_root: str | Path,
        *,
        i18n: Any | None = None,
        change_gpt_weights: Any | None = None,
        change_sovits_weights: Any | None = None,
        get_tts_wav: Any | None = None,
    ) -> None:
        self._root = Path(gptsovits_root)
        self._lock = RLock()
        self._loaded_gpt: str | None = None
        self._loaded_sovits: str | None = None
        self._loaded_languages: tuple[str, str] | None = None
        if change_gpt_weights and change_sovits_weights and get_tts_wav:
            self._funcs: tuple | None = (change_gpt_weights, change_sovits_weights, get_tts_wav, i18n)
        else:
            self._funcs = None

    def _resolve_funcs(self) -> tuple:
        if self._funcs is None:
            self._funcs = import_gptsovits_inference(self._root)
        return self._funcs

    @property
    def i18n(self) -> Any:
        return self._resolve_funcs()[3]

    def load(
        self,
        *,
        gpt_path: str,
        sovits_path: str,
        prompt_language: str,
        text_language: str,
        force: bool = False,
    ) -> None:
        """Load GPT + SoVITS weights, caching by path (+ language pair for SoVITS) --
        same change-once semantics as the old ``service._ensure_models``."""
        change_gpt_weights, change_sovits_weights, _, _ = self._resolve_funcs()
        with self._lock, pushd(self._root):  # A3 residual: vendored cwd-relative model loads
            if force or self._loaded_gpt != gpt_path:
                change_gpt_weights(gpt_path=gpt_path)
                self._loaded_gpt = gpt_path
            languages = (prompt_language, text_language)
            if force or self._loaded_sovits != sovits_path or self._loaded_languages != languages:
                for _ in change_sovits_weights(
                    sovits_path=sovits_path,
                    prompt_language=prompt_language,
                    text_language=text_language,
                ):
                    pass
                self._loaded_sovits = sovits_path
                self._loaded_languages = languages

    def synthesize_chunks(self, **kwargs: Any) -> Iterator[tuple[int, Any]]:
        """Run the vendored ``get_tts_wav`` for ONE chunk, returning an iterator of
        ``(sample_rate, audio_ndarray)``. Materialized INSIDE the pushd block so cwd
        is restored before the caller iterates (no context held across yields)."""
        _, _, get_tts_wav, _ = self._resolve_funcs()
        with self._lock, pushd(self._root):  # A3 residual
            results = list(get_tts_wav(**kwargs))
        return iter(results)

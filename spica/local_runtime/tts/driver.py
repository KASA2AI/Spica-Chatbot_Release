"""Thin GPT-SoVITS v2pro inference driver (LOCAL_RUNTIME_PLAN cut 2, A2).

Wraps the vendored ``change_*_weights`` / ``get_tts_wav`` (imported once via
``model_imports``) behind a clean Spica-owned object so ``service.py`` stops
touching ``inference_webui`` glue directly. The vendored MODEL CLASSES do the
work -- this is NOT a get_tts_wav rewrite and NOT a model-def copy (D1). v2pro only.

A3: synthesize is now cwd-FREE (get_tts_wav has no call-time cwd dependency on
Linux). pushd remains ONLY on ``load`` (change_*_weights' cwd-relative
``./weight.json``) and in ``model_imports`` (import-time cnhubert/sv/now_dir
loads and frozen BERT path) -- both one-time, NOT the hot path. Fully removing them is a separate task
(would require patching vendored code).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterator
import weakref

from spica.local_runtime.tts.model_imports import (
    INFERENCE_LOCK, import_gptsovits_inference, pushd, release_inference_models,
    release_tensor_caches, restore_inference_frontend,
)

_active_driver: Any = None


def _claim(driver) -> None:
    """Only one native profile owns the process-global vendor models at a time."""
    global _active_driver
    previous = _active_driver() if _active_driver is not None else None
    if previous is not None and previous is not driver:
        previous.unload()
    _active_driver = weakref.ref(driver)


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
        self._lock = INFERENCE_LOCK
        self._generation = 0
        self._loaded_gpt: str | None = None
        self._loaded_sovits: str | None = None
        self._loaded_languages: tuple[str, str] | None = None
        if change_gpt_weights and change_sovits_weights and get_tts_wav:
            self._funcs: tuple | None = (change_gpt_weights, change_sovits_weights, get_tts_wav, i18n)
        else:
            self._funcs = None

    def _resolve_funcs(self) -> tuple:
        if self._funcs is None:
            _claim(self)
            self._funcs = import_gptsovits_inference(self._root)
        return self._funcs

    @property
    def i18n(self) -> Any:
        with self._lock:
            return self._resolve_funcs()[3]

    @property
    def ready(self) -> bool:
        return self._loaded_gpt is not None and self._loaded_sovits is not None

    @property
    def generation(self) -> int:
        return self._generation

    def unload(self) -> None:
        with self._lock:
            if self._funcs is not None and _active_driver is not None and _active_driver() is self:
                release_inference_models(self._funcs)
            self._loaded_gpt = self._loaded_sovits = self._loaded_languages = None
            self._generation += 1

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
        # A3: load KEEPS pushd. change_*_weights read+write "./weight.json" -- a
        # cwd-RELATIVE path hardcoded in the vendored inference_webui -- at call time,
        # so cwd must be the gptsovits root here (one-time, NOT the hot path).
        # Eliminating it would require patching vendored code: out of A3 scope.
        with self._lock, pushd(self._root):
            _claim(self)
            change_gpt_weights, change_sovits_weights, _, _ = self._resolve_funcs()
            restore_inference_frontend(self._funcs)
            changed = force or self._loaded_gpt != gpt_path or self._loaded_sovits != sovits_path or self._loaded_languages != (prompt_language, text_language)
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
            if changed:
                self._generation += 1

    def synthesize_chunks(self, **kwargs: Any) -> Iterator[tuple[int, Any]]:
        """Run the vendored ``get_tts_wav`` for ONE chunk, returning an iterator of
        ``(sample_rate, audio_ndarray)``.

        A3: NO pushd here -- the cwd-dependency audit found ``get_tts_wav`` has NO
        call-time cwd dependency on Linux: the sv/vocoder paths are frozen at
        import-pushd time (``sv.py``'s ``os.getcwd()`` / ``now_dir``), cnhubert is
        resident and lazy BERT uses an absolute path, the text frontend's cwd-relative code is Windows-only
        (``os.name == "nt"``), and the gpt/sovits checkpoints are absolute. Dropping
        the per-chunk pushd decouples the hot path from cwd; it is gated by
        ``verify_tts_parity --mode driver`` (vendored-direct vs driver-backed must stay
        <= the A1/A2 noise floor, else revert this one line)."""
        with self._lock:
            _, _, get_tts_wav, _ = self._resolve_funcs()
            results = list(get_tts_wav(**kwargs))
        return iter(results)


class MeguminDDriver:
    """Opt-in vendor TTS.run entry for Megumin's accepted D/reunion profile."""

    def __init__(self, gptsovits_root: str | Path, *, config_path: Path,
                 pipeline_factory: Any | None = None) -> None:
        self._root = Path(gptsovits_root).resolve()
        self._config_path = config_path.resolve()
        self._factory = pipeline_factory
        self._pipeline: Any | None = None
        self._loaded_weights: tuple[str, str] | None = None
        self._lock = INFERENCE_LOCK
        self._generation = 0

    @property
    def ready(self) -> bool:
        return self._pipeline is not None and self._loaded_weights is not None

    @property
    def generation(self) -> int:
        return self._generation

    def unload(self) -> None:
        with self._lock:
            loaded = self._pipeline is not None
            self._pipeline = None
            self._loaded_weights = None
            self._generation += 1
            if loaded:
                release_tensor_caches()

    @staticmethod
    def i18n(language: str) -> str:
        return {"日文": "all_ja", "中文": "all_zh", "英文": "en"}.get(language, language)

    def load(self, *, gpt_path: str, sovits_path: str, prompt_language: str,
             text_language: str, force: bool = False) -> None:
        with self._lock:
            _claim(self)
            weights = (gpt_path, sovits_path)
            if self._pipeline is not None and self._loaded_weights == weights and not force:
                return
            factory = self._factory
            if factory is None:
                from spica.local_runtime.tts.model_imports import create_megumin_tts_pipeline

                factory = create_megumin_tts_pipeline
            # Character switching normally creates a new service. A hot-reloaded
            # model also gets a fresh prompt cache, preventing old reference state.
            self._pipeline = None
            self._loaded_weights = None
            self._pipeline = factory(
                self._root, gpt_path=gpt_path, sovits_path=sovits_path,
                config_path=self._config_path,
            )
            self._loaded_weights = weights
            self._generation += 1

    def synthesize_chunks(self, **kwargs: Any) -> Iterator[tuple[int, Any]]:
        with self._lock:
            if self._pipeline is None:
                raise RuntimeError("Megumin D models have not been loaded")
            results = list(self._pipeline.run({
                "text": kwargs["text"], "text_lang": kwargs["text_language"],
                "ref_audio_path": kwargs["ref_wav_path"],
                "prompt_text": kwargs["prompt_text"],
                "prompt_lang": kwargs["prompt_language"],
                "top_k": kwargs["top_k"], "top_p": kwargs["top_p"],
                "temperature": kwargs["temperature"],
                "speed_factor": kwargs["speed"],
                "text_split_method": "cut0", "batch_size": 1,
                "split_bucket": False, "return_fragment": False,
                "fragment_interval": 0.3, "seed": 1234,
                "parallel_infer": True, "repetition_penalty": 1.35,
                "aux_ref_audio_paths": kwargs.get("inp_refs") or [],
            }))
        return iter(results)

from __future__ import annotations

import contextlib
import importlib.util
import os
import sys
import threading
from pathlib import Path
from types import ModuleType
from typing import Iterator


_APPLIO_LOCK = threading.Lock()
_CORE_MODULE: ModuleType | None = None


def infer_spica_vocal(
    input_vocal_path: str,
    output_vocal_path: str,
    model_path: str,
    index_path: str | None,
    f0_method: str = "rmvpe",
    transpose: int = 0,
    index_rate: float = 0.75,
    protect: float = 0.33,
    device: str = "cuda",
    applio_root: str | Path | None = None,
    **kwargs,
) -> str:
    applio_root = Path(applio_root or Path(__file__).resolve().parent / "Applio").resolve()
    with _APPLIO_LOCK:
        with _applio_context(applio_root):
            core = _load_core(applio_root)
            if hasattr(core, "infer_spica_vocal"):
                return str(
                    core.infer_spica_vocal(
                        input_vocal_path=input_vocal_path,
                        output_vocal_path=output_vocal_path,
                        model_path=model_path,
                        index_path=index_path,
                        f0_method=f0_method,
                        transpose=transpose,
                        index_rate=index_rate,
                        protect=protect,
                        device=device,
                        **kwargs,
                    )
                )
            message, output_path = core.run_infer_script(
                pitch=transpose,
                index_rate=index_rate,
                volume_envelope=float(kwargs.get("volume_envelope", 1.0)),
                protect=protect,
                f0_method=f0_method,
                input_path=input_vocal_path,
                output_path=output_vocal_path,
                pth_path=model_path,
                index_path=index_path or "",
                split_audio=bool(kwargs.get("split_audio", False)),
                f0_autotune=bool(kwargs.get("f0_autotune", False)),
                f0_autotune_strength=float(kwargs.get("f0_autotune_strength", 1.0)),
                proposed_pitch=bool(kwargs.get("proposed_pitch", False)),
                proposed_pitch_threshold=float(kwargs.get("proposed_pitch_threshold", 155.0)),
                clean_audio=bool(kwargs.get("clean_audio", False)),
                clean_strength=float(kwargs.get("clean_strength", 0.5)),
                export_format=str(kwargs.get("export_format", "WAV")),
                embedder_model=str(kwargs.get("embedder_model", "contentvec")),
                embedder_model_custom=kwargs.get("embedder_model_custom"),
                sid=int(kwargs.get("sid", 0)),
            )
            del message
            return str(output_path)


def _load_core(applio_root: Path) -> ModuleType:
    global _CORE_MODULE
    if _CORE_MODULE is not None:
        return _CORE_MODULE
    core_path = applio_root / "core.py"
    spec = importlib.util.spec_from_file_location("spica_applio_core", core_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载 Applio core.py：{core_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    _CORE_MODULE = module
    return module


@contextlib.contextmanager
def _applio_context(applio_root: Path) -> Iterator[None]:
    old_cwd = os.getcwd()
    inserted = False
    root_text = str(applio_root)
    if root_text not in sys.path:
        sys.path.insert(0, root_text)
        inserted = True
    os.chdir(root_text)
    try:
        yield
    finally:
        os.chdir(old_cwd)
        if inserted:
            with contextlib.suppress(ValueError):
                sys.path.remove(root_text)

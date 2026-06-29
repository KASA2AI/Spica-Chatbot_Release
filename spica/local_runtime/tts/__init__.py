"""GPT-SoVITS v2pro local-runtime TTS driver (LOCAL_RUNTIME_PLAN cut 2, A2).

A THIN, controlled boundary over the vendored inference: ``model_imports`` does a
one-time, centralized import of the vendored ``change_*_weights`` / ``get_tts_wav``
/ i18n (the sys.path / cwd setup that used to be inline in service._lazy_import
lives HERE now), and ``driver.GptSovitsV2ProDriver`` wraps load + synthesize so
``service.py`` stops touching the ``inference_webui`` glue directly.

A2 scope: NOT a get_tts_wav rewrite, NOT a model-def copy, v2pro only. The pushd
around load/synthesize is a PROTECTED (context-manager) residual marked for A3.
"""

from spica.local_runtime.tts.driver import GptSovitsV2ProDriver

__all__ = ["GptSovitsV2ProDriver"]

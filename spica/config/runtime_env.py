"""Process-env shims for the vendored GPT-SoVITS / HuggingFace runtime.

PERMANENT member of the test_no_getenv allowlist (P0b step 1, decision D3):
mutating the process environment for vendored code is a config-layer job --
the same privilege manager.py/secrets.py hold for reads. Moving these shims
here (from agent_tools/tts/gptsovits/service.py) is what lets the TEMPORARY
allowlist reach zero without scattering env writes around business code.

TIMING CONSTRAINT (FINDINGS #19): ``prime_vendored_runtime_cache_env`` must run
BEFORE transformers/huggingface_hub first resolve a model path, and before the
vendored runtime imports anything that reads XDG_CACHE_HOME. The caller
(``service._lazy_import``) invokes both functions at exactly the spot the
inline code used to live -- do not defer or hoist these calls.
"""

from __future__ import annotations

import os
from pathlib import Path

DEFAULT_RUNTIME_CACHE_ROOT = Path("/tmp/spica_chatbot_cache")
_PROXY_ENV_KEYS = ("all_proxy", "ALL_PROXY", "http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY")


def resolve_runtime_cache_root() -> Path:
    """Pure resolution (no writes) -- shared by the primer below and the
    Layer A snapshot, so the dumped value and the effective value cannot drift."""
    return Path(os.getenv("SPICA_RUNTIME_CACHE_DIR") or DEFAULT_RUNTIME_CACHE_ROOT).resolve()


def prime_vendored_runtime_cache_env() -> None:
    cache_root = resolve_runtime_cache_root()
    # Pin the HF cache to the user's PERSISTENT cache BEFORE hijacking
    # XDG_CACHE_HOME below. huggingface_hub resolves its cache as
    # HF_HOME -> $XDG_CACHE_HOME/huggingface -> ~/.cache/huggingface; the
    # XDG redirect (meant for the vendored runtime's numba/matplotlib junk)
    # used to swallow that chain too, so the 3.85G Moondream weights landed
    # under /tmp and were re-downloaded after every reboot (FINDINGS #19).
    if not os.environ.get("HF_HOME"):
        hf_home = Path.home() / ".cache" / "huggingface"
        hf_home.mkdir(parents=True, exist_ok=True)
        os.environ["HF_HOME"] = str(hf_home)
    for env_key, dirname in (
        ("NUMBA_CACHE_DIR", "numba"),
        ("MPLCONFIGDIR", "matplotlib"),
        ("XDG_CACHE_HOME", "xdg"),
    ):
        if os.environ.get(env_key):
            continue
        cache_dir = cache_root / dirname
        cache_dir.mkdir(parents=True, exist_ok=True)
        os.environ[env_key] = str(cache_dir)


def strip_proxy_env_for_vendored_runtime() -> None:
    """The vendored runtime must talk to localhost services directly; a stray
    system proxy breaks it. Same pop-list the inline service code used."""
    for key in _PROXY_ENV_KEYS:
        os.environ.pop(key, None)

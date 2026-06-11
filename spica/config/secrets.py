"""Secret configuration -- the only permanent reader of secret env vars.

INVARIANT (CLAUDE.md #4): API keys and other secrets live in the environment /
``xiaosan.env``, never in plain config files. Business code obtains them via
``load_secrets()``; only this module (and ``manager.py``) may read ``os.getenv``.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from spica.config.env_roster import LEGACY_ENV_VARS

_REPO_ROOT = Path(__file__).resolve().parents[2]
logger = logging.getLogger(__name__)


def _ensure_env_loaded() -> None:
    load_dotenv(_REPO_ROOT / "xiaosan.env")
    load_dotenv(_REPO_ROOT.parent / "xiaosan.env", override=False)


@dataclass(frozen=True)
class Secrets:
    openai_api_key: str | None = None


def load_secrets() -> Secrets:
    _ensure_env_loaded()
    # P0b step 1: the DeepSeek dual-name died with B2 (the second LLM classifier
    # was its only reader). Residual legacy lines must warn, never sit silent.
    for legacy_name in LEGACY_ENV_VARS:
        if os.getenv(legacy_name):
            logger.warning(
                "legacy env var %s is set but no longer read by any code "
                "(密钥已统一为 OPENAI_API_KEY，请从 xiaosan.env 删除该行)",
                legacy_name,
            )
    return Secrets(openai_api_key=os.getenv("OPENAI_API_KEY"))

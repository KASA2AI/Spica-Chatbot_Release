"""Secret configuration -- the only permanent reader of secret env vars.

INVARIANT (CLAUDE.md #4): API keys and other secrets live in the environment /
``xiaosan.env``, never in plain config files. Business code obtains them via
``load_secrets()``; only this module (and ``manager.py``) may read ``os.getenv``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _ensure_env_loaded() -> None:
    load_dotenv(_REPO_ROOT / "xiaosan.env")
    load_dotenv(_REPO_ROOT.parent / "xiaosan.env", override=False)


@dataclass(frozen=True)
class Secrets:
    openai_api_key: str | None = None


def load_secrets() -> Secrets:
    _ensure_env_loaded()
    return Secrets(openai_api_key=os.getenv("OPENAI_API_KEY"))

"""Compatibility shim (C4): moved to ``spica.runtime.sync_chain``.

Re-exports ``run_voice_pipeline`` so existing ``agent.runtime`` importers keep
working until C4-5 repoints them; the ``agent/`` package is deleted in C4-6.
"""

from spica.runtime.sync_chain import run_voice_pipeline  # noqa: F401

"""Compatibility shim (C4): moved to ``spica.conversation.prompt_builder``.

Re-exports the public surface so existing ``agent.prompt_builder`` importers keep
working until C4-5 repoints them; the ``agent/`` package is deleted in C4-6.
"""

from spica.conversation.prompt_builder import *  # noqa: F401,F403

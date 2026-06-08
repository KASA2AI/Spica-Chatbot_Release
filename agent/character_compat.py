"""Compatibility shim (C4): moved to ``spica.conversation.character_compat``.

Re-exports the public surface so existing ``agent.character_compat`` importers keep
working until C4-5 repoints them; the ``agent/`` package is deleted in C4-6.
"""

from spica.conversation.character_compat import *  # noqa: F401,F403

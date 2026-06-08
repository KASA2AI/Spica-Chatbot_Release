"""Compatibility shim (C4): moved to ``spica.conversation.time_context``.

Re-exports the public surface so existing ``agent.time_context`` importers keep
working until C4-5 repoints them; the ``agent/`` package is deleted in C4-6.
"""

from spica.conversation.time_context import *  # noqa: F401,F403

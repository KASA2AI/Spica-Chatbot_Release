"""Compatibility shim (C4): moved to ``spica.conversation.character_loader``.

Re-exports the public surface (``__all__``, incl. the names it re-exports from
character_compat) so existing ``agent.character_loader`` importers keep working
until C4-5 repoints them; the ``agent/`` package is deleted in C4-6.
"""

from spica.conversation.character_loader import *  # noqa: F401,F403

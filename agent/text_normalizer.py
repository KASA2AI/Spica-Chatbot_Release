"""Compatibility shim (C4): moved to ``spica.conversation.text_normalizer``.

Re-exports the public surface so existing ``agent.text_normalizer`` importers keep
working until C4-5 repoints them; the ``agent/`` package is deleted in C4-6.
"""

from spica.conversation.text_normalizer import *  # noqa: F401,F403

"""Compatibility shim (C4): moved to ``spica.conversation.reply_parser``.

Re-exports the public surface so existing ``agent.reply_parser`` importers keep
working until C4-5 repoints them; the ``agent/`` package is deleted in C4-6.
"""

from spica.conversation.reply_parser import *  # noqa: F401,F403

"""Compatibility shim (C4): moved to ``spica.runtime.services``.

Re-exports ``AgentServices`` so existing ``agent.state`` importers keep working
until C4-5 repoints them; the ``agent/`` package is deleted in C4-6.
"""

from spica.runtime.services import AgentServices  # noqa: F401

"""Compatibility shim (C4): moved to ``spica.runtime.stages``.

Re-exports the public stage functions (and ``_compact_tool_history_for_prompt``,
used by the tool round) so existing ``agent.nodes`` importers keep working until
C4-5 repoints them; the ``agent/`` package is deleted in C4-6.
"""

from spica.runtime.stages import *  # noqa: F401,F403
from spica.runtime.stages import _compact_tool_history_for_prompt  # noqa: F401

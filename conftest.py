"""Root pytest configuration (W5, WINDOWS_COMPAT_PLAN §5-W5).

The sanctioned command is ``python -m pytest tests -q`` on BOTH platforms -- the
explicit ``tests`` arg scopes collection to ``tests/`` (superseding the
``testpaths`` in pytest.ini), so it runs the same suite on Linux and Windows.

Defensive collection policy: on Windows, drop the ``hardware/respeaker`` path
from any bare/IDE ``pytest`` invocation. ReSpeaker is Linux/USB microphone
hardware (W3b, optional) -- not a Windows component. It carries NO test files
today (the ReSpeaker unit tests live under ``tests/`` and are mock-based), so
this is currently a no-op that codifies the platform intent. Linux is unchanged.
NB: bare ``pytest`` stays forbidden on both platforms (it recurses into the
vendored GPT-SoVITS runtime) -- always use ``python -m pytest tests -q``.
"""

import sys

collect_ignore = []
if sys.platform == "win32":
    collect_ignore.append("hardware/respeaker")

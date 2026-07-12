"""Suite-wide test hygiene.

Root cause this guards against (Runtime Cutover Rehearsal review, 2026-07-02):
``ConfigManager.load()`` calls ``_ensure_env_loaded()`` ->
``load_dotenv(xiaosan.env)``, and the vendored dotenv writes EVERY key in the
developer's local ``xiaosan.env`` (incl. the whole ``SPICA_SCREEN_*`` override
block) into the process-global ``os.environ`` -- permanently, with no cleanup.

So any test that triggers a real config load (directly, or indirectly via
``resolve_effective_screen_config()`` inside e.g. ``analyze_screen_attachment``)
leaks the developer's local env into every LATER test in the same process. That
is exactly how ``SPICA_SCREEN_PROVIDER=moondream_hf`` from a local ``xiaosan.env``
bled into the screen-config tests and flipped their ``moondream_local``
assertions -- a machine-dependent failure that stays hidden whenever the local
override happens to match the asserted value.

The autouse fixture below snapshots ``os.environ`` before each test and restores
it verbatim afterwards. It does NOT clear or mask env DURING a test, so every
test still sees exactly what it saw before; it only stops one test's env
mutations (the dotenv leak included) from bleeding into the next one. This keeps
the suite hermetic and independent of whatever the local ``xiaosan.env`` holds.
"""

from __future__ import annotations

import os

import pytest

from spica.config.env_roster import LEGACY_ENV_VARS, consumed_env_names


@pytest.fixture(autouse=True)
def _restore_os_environ():
    saved = dict(os.environ)
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(saved)


@pytest.fixture(autouse=True)
def _isolate_config_studio_environment(request):
    """Config Studio tests must never observe real roster or legacy values."""

    if not request.node.path.name.startswith("test_config_studio_"):
        yield
        return
    names = consumed_env_names() | frozenset(LEGACY_ENV_VARS)
    previous = {name: os.environ[name] for name in names if name in os.environ}
    for name in names:
        os.environ.pop(name, None)
    try:
        yield
    finally:
        for name in names:
            os.environ.pop(name, None)
        os.environ.update(previous)

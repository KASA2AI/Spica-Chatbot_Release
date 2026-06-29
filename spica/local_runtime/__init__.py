"""Spica Local Runtime (Phase 2 / engineering reset).

The single home for Spica's OWN local-inference implementations, model export,
and build scripts -- the layer that lets Spica shed vendored third-party runtime
code and pin its own deployment boundary (LOCAL_RUNTIME_PLAN §1, §4).

What lives here:
- inference implementations behind the EXISTING ``spica/ports`` (never a second
  port layer -- LOCAL_RUNTIME_PLAN §3.1);
- the model-agnostic parity harness (``parity/``), the quality backstop that the
  golden event tests cannot provide (§6);
- device probing (``device.py``), error codes (``errors.py``), manifest parse
  (``manifest.py``).

HARD CONSTRAINT (§3.3 / CLAUDE.md #4): production runtime code under this package
MUST NOT read ``os.getenv`` / ``os.environ``. Device/capability detection goes
through ``import`` probes, ``subprocess``, ``platform``, or injected typed config
-- never env. Only ``scripts/local_runtime/`` CLIs (outside ``spica/``) may read
env, and their env names are documented centrally there.
"""

"""Spica platform core.

Framework-agnostic host, runtime, config, ports, adapters and memory layers for
the Spica character-performance platform. See ``docs/REFACTOR_PLAN.md``.

INVARIANT (CLAUDE.md #1): nothing under ``spica/`` may import PySide / Qt / any
GUI library. A guard test enforces this from Phase 2 onward.
"""

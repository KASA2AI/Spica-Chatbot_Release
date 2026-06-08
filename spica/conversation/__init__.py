"""Pure conversation domain (C4).

Character-agnostic, side-effect-light domain helpers the conversation core builds
on: prompt assembly, reply parsing, speech-text normalization, local-time context,
and character/profile loading. No threads, no adapters, no Qt -- moved here from
``agent/`` so ``spica`` is self-contained and ``agent/`` can be deleted.
"""

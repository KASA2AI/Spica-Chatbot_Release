"""Anime-watch domain layer (Qt-free, CLAUDE.md #1).

See docs/anime_watch/ANIME_WATCH_PLAN.md. This package is pure domain/logic:
title resolution, source-candidate matching, download library, playback policy.
NO Qt, NO network I/O (adapters do that), NO run_turn coupling.
"""

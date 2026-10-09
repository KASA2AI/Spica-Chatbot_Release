"""Sentence presentations inside one continuously synthesized spoken reply."""

import logging

from spica.conversation.text_normalizer import (
    build_bilingual_display,
    build_tts_text,
    normalize_square_brackets_for_speech,
    spoken_channel_is_paired,
    spoken_channel_or_fallback,
    split_dialog_translation,
)
from spica.runtime.play_unit_splitter import PlayUnitSplitter

logger = logging.getLogger(__name__)


def prepare_speech_segments(raw_text, unit, *, bilingual, max_chars):
    splitter = PlayUnitSplitter(min_chars=1, max_chars=max_chars, bilingual_brackets=bilingual)
    parts, previous = [], []
    for raw in splitter.feed(raw_text) + splitter.flush():
        subtitle = ""
        if bilingual:
            spoken, _ = split_dialog_translation(raw)
            subtitle = build_bilingual_display(raw)
            spoken = spoken_channel_or_fallback(spoken, paired_subtitle=spoken_channel_is_paired(raw))
        else:
            spoken = raw
        spoken = normalize_square_brackets_for_speech(spoken.strip())
        if not spoken:
            continue
        parts.append({
            **unit, "index": len(parts), "display_text": spoken,
            "subtitle_text": subtitle, "tts_text": build_tts_text(spoken),
            "previous_units": list(previous), "full_answer_so_far": "".join(previous + [spoken]),
            "timing": {},
        })
        previous.append(spoken)
    return parts


def time_speech_segments(audio_path, segments):
    """Estimate sentence starts from actual pauses; never edit the audio.

    This is lightweight pause alignment, not word-level forced alignment. Text
    length chooses among pauses; missing pauses fall back to voiced-time shares.
    """
    untimed = [dict(part, start_ms=None, end_ms=None) for part in segments]
    if not audio_path or not segments:
        return untimed
    try:
        import numpy as np
        import soundfile as sf

        audio, sr = sf.read(audio_path, dtype="float32", always_2d=True)
        if not len(audio) or not np.isfinite(audio).all():
            return untimed
        duration_ms = len(audio) * 1000 / sr
        hop = max(1, round(sr * .02))
        power = np.mean(audio * audio, axis=1)
        power = np.pad(power, (0, (-len(power)) % hop))
        rms = np.sqrt(power.reshape(-1, hop).mean(axis=1))
        active = rms > max(.001, float(rms.max()) * .035)
        weights = np.array([
            max(1, sum(2 if "\u4e00" <= c <= "\u9fff" else 1
                       for c in part["tts_text"] if c.isalnum()))
            for part in segments
        ], dtype=float)
        ratios = np.cumsum(weights)[:-1] / weights.sum()
        if not len(ratios):
            return [dict(segments[0], start_ms=0, end_ms=round(duration_ms, 2))]
        cumulative = np.cumsum(active, dtype=float)
        if not cumulative[-1]:
            boundaries = list(ratios * duration_ms)
        else:
            candidates = {}
            quiet = np.flatnonzero(np.diff(np.r_[False, ~active, False])).reshape(-1, 2)
            voiced = np.flatnonzero(active)
            for begin, end in quiet:
                if end - begin >= 5 and begin > voiced[0] and end <= voiced[-1]:
                    position = (begin + end) / 2
                    candidates[position] = (float(cumulative[begin]) / cumulative[-1], 0.)
            # A voice can omit pauses. Add ordered voiced-time estimates only
            # when the real pauses cannot supply every sentence boundary.
            if len(candidates) < len(ratios):
                for ratio in ratios:
                    position = float(np.searchsorted(cumulative, ratio * cumulative[-1]))
                    candidates.setdefault(position, (float(ratio), .08))
            positions = sorted(candidates)
            if len(positions) < len(ratios):
                boundaries = list(ratios * duration_ms)
            else:
                # Ordered minimum-cost matching, O(sentences * pauses).
                costs = np.array([candidates[p] for p in positions])
                previous = (costs[:, 0] - ratios[0]) ** 2 + costs[:, 1]
                parents = []
                for ratio in ratios[1:]:
                    current = np.full(len(positions), np.inf)
                    parent = np.full(len(positions), -1, dtype=int)
                    best = 0
                    for index in range(1, len(positions)):
                        if previous[index - 1] < previous[best]:
                            best = index - 1
                        current[index] = previous[best] + (costs[index, 0] - ratio) ** 2 + costs[index, 1]
                        parent[index] = best
                    parents.append(parent)
                    previous = current
                selected = [int(np.argmin(previous))]
                for parent in reversed(parents):
                    selected.append(int(parent[selected[-1]]))
                boundaries = [positions[index] * hop * 1000 / sr for index in reversed(selected)]
        starts = [0.] + boundaries
        ends = boundaries + [duration_ms]
        return [dict(part, start_ms=round(start, 2), end_ms=round(end, 2))
                for part, start, end in zip(segments, starts, ends)]
    except (OSError, RuntimeError, ValueError):
        logger.debug("event=speech_timing_unavailable", exc_info=True)
        return untimed

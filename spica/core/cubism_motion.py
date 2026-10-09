"""Small, Qt-free playback policy and timed parameter curves for Cubism."""

from bisect import bisect_left
import math


class ParameterEffect:
    """A bounded parameter-only motion, blended out instead of left latched."""

    def __init__(self, motion):
        self.duration = float(motion.get("Meta", {}).get("Duration", 0))
        if not math.isfinite(self.duration) or not 0 < self.duration <= 30:
            raise ValueError("invalid Cubism effect duration")
        curves = motion.get("Curves")
        if not isinstance(curves, list) or len(curves) > 128:
            raise ValueError("invalid Cubism effect curves")
        self.curves = {}
        for curve in curves:
            if curve.get("Target") != "Parameter":
                continue
            key, data = curve.get("Id"), curve.get("Segments", [])
            if not isinstance(key, str) or not key or len(data) < 2 or len(data) > 16384:
                raise ValueError("invalid Cubism parameter curve")
            if not all(isinstance(v, (float, int)) and math.isfinite(v) for v in data):
                raise ValueError("non-finite Cubism curve")
            segments, times = [], []
            previous, offset = tuple(data[:2]), 2
            while offset < len(data):
                kind = data[offset]
                count = {0: 2, 1: 6, 2: 2, 3: 2}.get(kind)
                if count is None or offset + count >= len(data):
                    raise ValueError("truncated Cubism curve")
                points = [previous] + [tuple(data[i:i + 2]) for i in range(offset + 1, offset + count + 1, 2)]
                if any(b[0] < a[0] for a, b in zip(points, points[1:])):
                    raise ValueError("Cubism curve times must increase")
                previous = points[-1]
                segments.append((kind, points))
                times.append(previous[0])
                offset += count + 1
            self.curves[key] = (data[1], times, segments)

    def values(self, seconds):
        if not 0 <= seconds < self.duration:
            return {}
        weight = self.weight(seconds)
        values = {}
        for key, (initial, times, segments) in self.curves.items():
            if not segments:
                value = initial
            else:
                kind, points = segments[min(bisect_left(times, seconds), len(segments) - 1)]
                start, end = points[0], points[-1]
                t = min(1, max(0, (seconds - start[0]) / max(1e-9, end[0] - start[0])))
                if kind == 0:
                    value = start[1] + (end[1] - start[1]) * t
                elif kind in (2, 3):
                    value = start[1] if kind == 2 else end[1]
                else:
                    # Works for both restricted and unrestricted time handles.
                    low, high = 0.0, 1.0
                    for _ in range(18):
                        t = (low + high) / 2
                        if self._bezier(points, t, 0) < seconds:
                            low = t
                        else:
                            high = t
                    value = self._bezier(points, t, 1)
            values[key] = value * weight
        return values

    def weight(self, seconds):
        return max(0, min(1, seconds / .12, (self.duration - seconds) / .2))

    @staticmethod
    def _bezier(points, t, axis):
        u = 1 - t
        return u**3 * points[0][axis] + 3*u*u*t * points[1][axis] + 3*u*t*t * points[2][axis] + t**3 * points[3][axis]


class CubismPlayback:
    """Only the currently presented unit may consume hold time or cooldowns."""

    def __init__(self, spec, hold_seconds=.8):
        self.hold_seconds = hold_seconds
        self._skill_cooldowns = {}
        self.set_model(spec)

    def set_model(self, spec):
        """Switch this character's costume without refreshing its skill budget."""
        self.spec = spec
        self._cooldowns = {}
        self._last_motions = {}
        self.reset()

    def reset(self):
        self._turn, self._index, self._pending = None, -1, None
        self._intent, self._priority, self._changed = None, 0, -math.inf

    def finish(self):
        """Retain the last presented face, discarding any unplayed action."""
        expression = self._pending.get("expression") if self._pending else None
        self.reset()
        return expression

    def queue(self, cue):
        if cue.get("turn") != self._turn:
            self._turn, self._index = cue.get("turn"), -1
        index = cue.get("index", -1)
        if not isinstance(index, int) or index <= self._index:
            return
        self._index, self._pending = index, dict(cue)

    def advance(self, now):
        cue = self._pending
        if cue is None:
            return None
        if cue.get("priority", 0) <= self._priority and now - self._changed < self.hold_seconds:
            return None
        self._pending = None
        if cue.get("intent") == self._intent:
            return None
        self._intent, self._priority, self._changed = cue.get("intent"), cue.get("priority", 0), now
        intent = cue.get("intent")
        choices = list(dict.fromkeys(cue.get("motions") or [cue.get("motion")]))
        choices = [key for key in choices if key in self.spec.motions]
        previous = self._last_motions.get(intent)
        if previous in choices:
            offset = choices.index(previous) + 1
            choices = choices[offset:] + choices[:offset]
        cue["motion"] = None
        for key in choices:
            motion = self.spec.motions[key]
            if now - self._cooldowns.get(key, -math.inf) < motion.cooldown:
                continue
            # Choosing a different skill variant cannot bypass its intent's cooldown.
            if motion.kind == "skill" and now < self._skill_cooldowns.get(intent, -math.inf):
                continue
            cue["motion"] = key
            self._cooldowns[key] = now
            self._last_motions[intent] = key
            if motion.kind == "skill":
                self._skill_cooldowns[intent] = now + motion.cooldown
            break
        if cue["motion"] is None:
            cue["effect"] = None
        return cue

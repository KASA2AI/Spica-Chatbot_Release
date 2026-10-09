"""Fresh per-person association; desk occupancy does not depend on identity.

Short-lived geometry + OSNet appearance matches must be unambiguous in both
 directions. Missing/predicted tracks are never emitted as observed people.
"""
from dataclasses import dataclass
import math

from spica.home.models import MIN_TRACKING_CONFIDENCE, RoomObservation, TrackedPerson


def overlap(a, b):
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    area = max(0, min(ax+aw, bx+bw)-max(ax, bx)) * max(0, min(ay+ah, by+bh)-max(ay, by))
    return area / max(1e-9, aw*ah + bw*bh - area)


@dataclass
class _Track:
    identity: int
    person: object
    at: float
    velocity: tuple[float, float] = (0., 0.)
    face_identity: str = "unknown"


class RoomLocator:
    def __init__(self, config, profile):
        self.config, self.profile = config, profile
        self._next_id = 0
        self.reset()

    def reset(self):
        self._sequence = -1
        self._last_frame = float('-inf')
        self._source = None
        self._tracks = []
        # IDs are not reused after a sensor/error reset within this camera epoch.

    @staticmethod
    def _feature(person):
        import numpy as np
        if person.association_error or len(person.appearance) != 512*4:
            return None
        value = np.frombuffer(person.appearance, dtype=np.float32)
        norm = float(np.linalg.norm(value))
        return value if np.isfinite(value).all() and .99 <= norm <= 1.01 else None

    def _cost(self, track, person, at):
        first, second = self._feature(track.person), self._feature(person)
        if first is None or second is None:
            return math.inf
        if (track.face_identity != 'unknown' and person.identity != 'unknown'
                and track.face_identity != person.identity):
            return math.inf
        x, y, w, h = track.person.box
        dt = at-track.at
        predicted = (x+track.velocity[0]*dt, y+track.velocity[1]*dt, w, h)
        bx, by, bw, bh = person.box
        distance = math.hypot(bx+bw/2-predicted[0]-w/2, by+bh/2-predicted[1]-h/2)
        iou = overlap(predicted, person.box)
        cosine = float(first @ second)
        # Appearance cannot reconnect a far-away person or survive a long gap.
        if cosine < .65 or (iou < .1 and distance > max(.08, math.hypot(w, h)*.5)):
            return math.inf
        return .7*(1-cosine) + .3*(1-iou)

    def _associations(self, people, tracks, at):
        costs = [[self._cost(track, person, at) for person in people] for track in tracks]
        def unique_best(values):
            ordered = sorted((value, index) for index, value in enumerate(values) if math.isfinite(value))
            if not ordered or (len(ordered) > 1 and ordered[1][0]-ordered[0][0] < .08):
                return None
            return ordered[0][1]

        row_best = [unique_best(row) for row in costs]
        links = []
        for index, person in enumerate(people):
            column = [row[index] for row in costs]
            best = unique_best(column)
            track = tracks[best] if best is not None and row_best[best] == index else None
            links.append((person, track, not any(math.isfinite(value) for value in column)))
        # Weak candidates cannot steal a match or resolve a strong ambiguity.
        remaining = [track for track, row in zip(tracks, costs) if not any(math.isfinite(value) for value in row)]
        return links, remaining

    def observe(self, frame, now):
        invalid = (frame.sequence <= self._sequence or frame.captured_mono <= self._last_frame
                   or not 0 <= now-frame.captured_mono <= self.config.frame_ttl_seconds)
        reason = 'stale_frame' if invalid else frame.error
        if self._source is not None and self._source != frame.source:
            self.reset()
            reason = 'camera_source_changed'
        if reason or self.profile is None:
            self._tracks.clear()
            return RoomObservation(captured_at=frame.captured_at, source=frame.source,
                                   reason=reason or 'calibration_required')
        self._sequence, self._last_frame, self._source = frame.sequence, frame.captured_mono, frame.source
        at = frame.captured_mono
        self._tracks = [t for t in self._tracks if at-t.at <= self.config.frame_ttl_seconds]
        threshold = self.config.person_confidence
        strong = [person for person in frame.people if person.confidence >= threshold]
        weak = [person for person in frame.people if min(threshold, MIN_TRACKING_CONFIDENCE) <= person.confidence < threshold]
        strong_regions = [self.profile.bed_region(person.box) for person in strong]
        weak_regions = [self.profile.bed_region(person.box) for person in weak]
        def occupied(region):
            if region in strong_regions:
                return True
            # A weak candidate may block an absence claim, but cannot declare
            # somebody up. H2 never needs a reusable track or a face match.
            if 'unknown' in strong_regions or any(r in {region, 'unknown'} for r in weak_regions):
                return None
            return False
        links, remaining = self._associations(strong, self._tracks, at)
        continuity, _ = self._associations(weak, remaining, at)
        links.extend((person, track, False) for person, track, _ in continuity if track is not None)
        observed, births = [], []
        for person, track, can_create in links:
            identity = None
            face_identity = person.identity
            if track is not None:
                previous, dt = track.person.box, at-track.at
                velocity = ((person.box[0]-previous[0])/dt, (person.box[1]-previous[1])/dt)
                track.velocity = tuple(.5*old+.5*new for old, new in zip(track.velocity, velocity))
                track.person, track.at = person, at
                if person.identity != "unknown":
                    track.face_identity = person.identity
                identity, face_identity = track.identity, track.face_identity
            elif can_create and self._feature(person) is not None:
                self._next_id += 1
                identity = self._next_id
                births.append(_Track(identity, person, at, face_identity=person.identity))
            observed.append(TrackedPerson(identity, person.box, self.profile.region(person.box), face_identity))
        self._tracks.extend(births)
        desk = (True if any(p.location == 'desk' for p in observed) else
                None if not observed or any(p.location == 'unknown' for p in observed) else False)
        return RoomObservation(tuple(observed), desk, frame.captured_at, frame.source,
                               'observed' if frame.people else 'no_person',
                               occupied('bed'), occupied('outside'))


class DeskVisit:
    """One action per continuous desk occupation, irrespective of who occupies it."""
    def __init__(self, config):
        self.config = config
        self.acted = False
        self._desk_since = self._away_since = None
        self._last_at = None
        self._frames = 0

    def vacant(self):
        self.acted = False
        self._desk_since = self._away_since = self._last_at = None
        self._frames = 0

    def observe(self, observation, at):
        if self._last_at is not None and at-self._last_at > self.config.frame_ttl_seconds:
            self._desk_since = self._away_since = None
            self._frames = 0
        self._last_at = at
        if observation.desk_occupied is True:
            self._away_since = None
            if self._desk_since is None:
                self._desk_since = at
            self._frames += 1
            if not self.acted and self._frames >= 3 and at-self._desk_since >= self.config.desk_confirm_seconds:
                self.acted = True  # Claim before I/O, including an uncertain result.
                return True
        else:
            self._desk_since = None
            self._frames = 0
            if observation.desk_occupied is False:
                if self._away_since is None:
                    self._away_since = at
                if at-self._away_since >= self.config.departure_confirm_seconds:
                    self.acted = False
            else:
                self._away_since = None
        return False

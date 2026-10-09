"""Home's ordered sensor inputs, independent of room-stay and camera policies."""
from dataclasses import dataclass

from spica.home.models import SensorObservation


@dataclass(frozen=True)
class SensorChange:
    kind: str
    observation: SensorObservation | None = None
    continuous: bool = True
    rising: bool = False
    falling: bool = False


@dataclass(frozen=True)
class RoomInputBatch:
    at: float
    generation: int
    connection: str
    presence: SensorObservation | None
    door: SensorObservation | None
    illuminance: SensorObservation | None
    changes: tuple[SensorChange, ...]

    def fresh(self, observation):
        return observation is not None and self.at <= observation.valid_until

    def value(self, observation):
        return observation.value if self.fresh(observation) else None

    @property
    def occupied(self):
        return self.value(self.presence)

    @property
    def lux(self):
        return self.value(self.illuminance)

    @property
    def scene_changes(self):
        # Earlier input can explain H1's ordering, but cannot survive the last
        # reset as a pending H3 action or continuous observation.
        start = next((index+1 for index in range(len(self.changes)-1, -1, -1)
                      if self.changes[index].kind == 'reset'), 0)
        return self.changes[start:]

    @property
    def opening(self):
        return next((change.observation for change in reversed(self.scene_changes)
                     if change.kind == 'opening'), None)


class RoomInputs:
    def __init__(self):
        self._generation = 0
        self._connection = 'starting'
        self._presence = self._door = self._illuminance = None
        self._last_presence = None
        self._changes = ()

    def reset(self, connection=None):
        self._generation += 1
        if connection is not None:
            self._connection = connection
        self._presence = self._door = self._illuminance = None
        self._last_presence = None
        self._changes = ()

    def consume(self, events, now):
        changes = []
        for name, observation in events:
            if name == 'connection':
                self.reset(observation)
                changes.append(SensorChange('reset'))
            elif name == 'presence':
                previous = self._presence
                self._presence = observation
                if previous is not None and previous.observed_at == observation.observed_at:
                    continue
                fresh = now <= observation.valid_until
                changes.append(SensorChange('presence', observation,
                    continuous=previous is None or observation.received_at <= previous.valid_until,
                    rising=fresh and observation.value is True and self._last_presence is False,
                    falling=fresh and observation.value is False and self._last_presence is True))
                if fresh:
                    self._last_presence = observation.value
            elif name in {'door', 'door_event'}:
                previous = self._door
                self._door = observation
                if (now <= observation.valid_until and observation.value is False
                        and (previous is None and name == 'door_event'
                             or previous is not None and previous.value is True
                             and observation.observed_at > previous.observed_at)):
                    changes.append(SensorChange('opening', observation))
            elif name == 'illuminance':
                previous = self._illuminance
                self._illuminance = observation
                if previous is None or previous.observed_at != observation.observed_at:
                    changes.append(SensorChange('illuminance', observation,
                        continuous=previous is None or observation.received_at <= previous.valid_until))
        self._changes = tuple(changes)
        return self.snapshot(now)

    def snapshot(self, now):
        return RoomInputBatch(now, self._generation, self._connection,
            self._presence, self._door, self._illuminance, self._changes)

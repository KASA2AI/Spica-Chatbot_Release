"""Small immutable observations; wall time is provenance, monotonic time is age."""
from dataclasses import dataclass, field
from typing import Literal

Box = tuple[float, float, float, float]  # normalized x, y, width, height
Location = Literal["unknown", "desk", "bed", "outside"]
MIN_TRACKING_CONFIDENCE = .25


@dataclass(frozen=True)
class SensorObservation:
    source: str
    value: bool | float
    observed_at: float
    received_at: float
    valid_until: float


@dataclass(frozen=True)
class Person:
    box: Box
    identity: Literal["owner", "other", "unknown"] = "unknown"
    similarity: float | None = None
    # Normalized float32 OSNet features. Worker -> Home only; never status/memory.
    appearance: bytes = field(default=b"", repr=False)
    association_error: str = ""
    confidence: float = 1.


@dataclass(frozen=True)
class VisionObservation:
    source: str
    sequence: int
    captured_at: float
    captured_mono: float
    people: tuple[Person, ...] = ()
    error: str = ""
    inference_seconds: float = 0
    face_error: str = ""


@dataclass(frozen=True)
class TrackedPerson:
    track_id: int | None
    box: Box
    location: Location
    identity: str = "unknown"  # Face evidence carried only by this continuous track.


@dataclass(frozen=True)
class RoomObservation:
    people: tuple[TrackedPerson, ...] = ()
    desk_occupied: bool | None = None
    captured_at: float | None = None
    source: str = ""
    reason: str = "no_observation"
    # Detection-based H2 evidence, independent of face/track association. False
    # means a usable frame had no candidate here; it does not prove vacancy.
    bed_occupied: bool | None = None
    outside_bed_occupied: bool | None = None

    @property
    def has_person(self):
        return bool(self.people) or any(value is True for value in
            (self.desk_occupied, self.bed_occupied, self.outside_bed_occupied))


@dataclass(frozen=True)
class DisplayResult:
    status: Literal["already_on", "wake_requested", "already_off", "blank_requested", "unavailable", "failed"]
    detail: str = ""

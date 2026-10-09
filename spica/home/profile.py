"""Camera-specific regions and optional legacy-compatible local face enrollment."""
import math
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator


class HomeProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    version: int = 1
    camera_device: str
    image_size: tuple[int, int]
    desk: tuple[float, float, float, float]  # x0, y0, x1, y1
    bed: tuple[float, float, float, float]
    embeddings: list[list[float]] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def valid(self):
        if self.version != 1 or min(self.image_size) <= 0:
            raise ValueError("unsupported Home calibration")
        for region in (self.desk, self.bed):
            x0, y0, x1, y1 = region
            if not (0 <= x0 < x1 <= 1 and 0 <= y0 < y1 <= 1):
                raise ValueError("draw valid desk and bed regions")
        if self.embeddings and len(self.embeddings) < 3:
            raise ValueError("optional face enrollment requires at least three samples")
        for embedding in self.embeddings:
            if len(embedding) != 128 or not all(math.isfinite(x) for x in embedding):
                raise ValueError("invalid SFace registration")
            if not .99 <= math.sqrt(sum(x*x for x in embedding)) <= 1.01:
                raise ValueError("registration must be normalized")
        if any(sum(a*b for a, b in zip(first, second)) < .4
               for i, first in enumerate(self.embeddings) for second in self.embeddings[i+1:]):
            raise ValueError("registration contains inconsistent identities; register again")
        return self

    def region(self, box):
        # The preview marks this torso anchor; calibration uses the SAME point.
        x, y, w, h = box
        point = (x + w / 2, y + h * .35)
        def contains(region):
            x0, y0, x1, y1 = region
            return x0 <= point[0] <= x1 and y0 <= point[1] <= y1
        desk, bed = contains(self.desk), contains(self.bed)
        if desk and bed:
            return "unknown"
        return "desk" if desk else "bed" if bed else "outside"

    def bed_region(self, box):
        """Keep anchors just outside the bed boundary out of exit evidence."""
        x, y, w, h = box
        px, py = x+w/2, y+h*.35
        x0, y0, x1, y1 = self.bed
        if x0 <= px <= x1 and y0 <= py <= y1:
            return "bed"
        # Five percent of each calibrated bed dimension is a small uncertainty
        # band. H1 keeps its existing desk anchor and region semantics.
        dx, dy = (x1-x0)*.05, (y1-y0)*.05
        if x0-dx <= px <= x1+dx and y0-dy <= py <= y1+dy:
            return "unknown"
        return "outside"


def load_profile(directory, device):
    path = Path(directory) / "profile.json"
    profile = HomeProfile.model_validate_json(path.read_text(encoding="utf-8"))
    if profile.camera_device != device:
        raise ValueError("camera changed; recalibrate Home before recognition")
    return profile


def save_profile(directory, profile):
    path = Path(directory)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path / "profile.json.tmp"
    # Exclusive creation avoids truncating a previous interrupted registration.
    with temporary.open("x", encoding="utf-8") as stream:
        stream.write(profile.model_dump_json(indent=2))
    temporary.replace(path / "profile.json")

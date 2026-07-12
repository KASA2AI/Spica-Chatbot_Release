from __future__ import annotations

import hashlib
import struct
from pathlib import Path

from spica.config_studio.assets import BackgroundAsset


ROOT = Path(__file__).resolve().parents[1]
BACKGROUND = (
    ROOT
    / "ui"
    / "config_studio"
    / "assets"
    / "0125_4_CG_GE01_0202_waifu2x_2x_3n_png.png"
)


def test_committed_background_has_accepted_bytes_and_png_shape():
    loaded = BackgroundAsset(BACKGROUND).load()

    assert loaded.health_code is None
    assert loaded.content is not None
    assert len(loaded.content) == 10_281_151
    assert hashlib.sha256(loaded.content).hexdigest() == (
        "0a1116f8fb71f48156b0fd29d6beba9478bee6890b847c58b37c0d36c186eb68"
    )
    assert loaded.content[:8] == b"\x89PNG\r\n\x1a\n"
    assert loaded.content[12:16] == b"IHDR"
    assert struct.unpack(">IIBB", loaded.content[16:26]) == (3840, 2160, 8, 6)


def test_invalid_background_is_not_exposed_and_reports_health(tmp_path):
    invalid = tmp_path / "background.png"
    invalid.write_bytes(b"not-the-accepted-image")

    loaded = BackgroundAsset(invalid).load()

    assert loaded.content is None
    assert loaded.health_code == "BACKGROUND_ASSET_INVALID"

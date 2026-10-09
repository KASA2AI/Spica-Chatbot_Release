"""Native link fixtures without requiring administrator privileges."""
import os
from pathlib import Path

import pytest


def symlink_or_skip(link, target, *, target_is_directory=False):
    link, target = Path(link), Path(target)
    directory = target_is_directory or target.is_dir()
    try:
        link.symlink_to(target, target_is_directory=directory)
    except OSError as error:
        if os.name != "nt" or getattr(error, "winerror", None) != 1314:
            raise
        if not directory:
            pytest.skip("Windows file symlinks require a privilege; directory junctions run natively")
        import _winapi
        _winapi.CreateJunction(str(target), str(link))

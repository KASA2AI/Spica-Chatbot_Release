"""CLI for the local layered sprite bundle builder."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_tools.visual.layered_bundle_builder import build_layered_bundle


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--src", type=Path, default=Path("spica_data/diffs"))
    parser.add_argument("--out", type=Path, default=Path("spica_data/derived"))
    parser.add_argument("--px", type=int, default=2048)
    args = parser.parse_args()
    bundle = build_layered_bundle(args.src, args.out, px=args.px)
    print(bundle)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Standalone OCR text-stream demo (Phase 7 real-machine acceptance).

Runs the REAL continuous OCR loop against an open game window and prints each
committed dialogue line + per-cycle timing + pause/recover, so you can see whether
(on your CPU + HiDPI) it holds the interval and cleanly collects lines.

Example (anemoi already open, X11 session):

    python galgame_ocr_stream_demo.py --title anemoi \
        --dialog 0.08,0.72,0.84,0.22 --interval 1.0

  --title    substring to match the game window title (required)
  --dialog   dialogue region ratios "x,y,w,h" within the window (default bottom band)
  --speaker  optional name region ratios "x,y,w,h"
  --interval seconds to wait after each cycle (default 1.0)

Advance the game and watch lines stream in. Switch focus to another app -> it
prints "[paused] reason=WINDOW_NOT_FOCUSED" (a NORMAL focus pause, not a bug);
switch back -> "[recovered]". Ctrl-C to stop.

Needs: mss, Pillow, rapidocr-onnxruntime, wmctrl, xprop -- on an X11 session.
"""

from __future__ import annotations

import argparse
import sys
import tempfile
import time
from pathlib import Path


def _ratios(text: str) -> tuple[float, float, float, float]:
    parts = [float(p) for p in text.split(",")]
    if len(parts) != 4:
        raise argparse.ArgumentTypeError("ratios must be 'x,y,w,h'")
    return (parts[0], parts[1], parts[2], parts[3])


def main() -> None:
    parser = argparse.ArgumentParser(description="Phase 7 galgame OCR text-stream demo")
    # --title XOR --window-id: title substring is convenient, but flatpak+wine opens
    # several windows that can share an identical title (a small window + the big
    # render window) -- in that case bind by exact id so you don't grab the small one.
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--title", help="substring of the game window title to bind")
    target.add_argument("--window-id", help="exact X11 window id to bind, e.g. 0x13800001")
    parser.add_argument(
        "--focus-keyword", default=None,
        help="keyword to verify focus is on the game (default: the bound window's title, or --title)",
    )
    parser.add_argument("--dialog", type=_ratios, default=(0.08, 0.72, 0.84, 0.22), help="dialogue region x,y,w,h")
    parser.add_argument("--speaker", type=_ratios, default=None, help="name region x,y,w,h (optional)")
    parser.add_argument("--interval", type=float, default=1.0, help="wait seconds after each cycle")
    args = parser.parse_args()

    from spica.adapters.game_memory.sqlite import GameMemorySqliteAdapter
    from spica.adapters.ocr import RapidOcrAdapter
    from spica.adapters.screen_capture import MssScreenCapture
    from spica.adapters.window_locator import LinuxX11WindowLocator
    from spica.galgame.models import WindowMatchRule
    from spica.galgame.ocr_loop import OcrStreamRunner
    from spica.galgame.session import GalgameCompanionSession

    locator = LinuxX11WindowLocator()
    enumeration = locator.enumerate_windows()
    if not enumeration.available:
        print(f"[demo] cannot enumerate windows: {enumeration.reason_code} -- {enumeration.reason}")
        sys.exit(1)
    by_id = {w.window_id: w for w in enumeration.windows}

    def _list_windows() -> None:
        print("[demo] open windows (id  [WxH]  title):")
        for w in enumeration.windows:
            g = locator.get_window_geometry(w.window_id)
            size = f"{g.width}x{g.height}" if g else "??x??"
            print(f"   {w.window_id}  [{size}]  {w.title}")

    if args.window_id:
        window_id = args.window_id
        geom = locator.get_window_geometry(window_id)  # bind by exact id (skip title match)
        if geom is None:
            print(f"[demo] window id {window_id!r} has no geometry (wrong id format?).")
            _list_windows()
            sys.exit(1)
        title = by_id[window_id].title if window_id in by_id else "(not in wmctrl list)"
    else:
        match = next((w for w in enumeration.windows if args.title.lower() in (w.title or "").lower()), None)
        if match is None:
            print(f"[demo] no window title contains {args.title!r}.")
            _list_windows()
            sys.exit(1)
        window_id = match.window_id
        geom = locator.get_window_geometry(window_id)
        title = match.title

    # Focus is verified by title keyword (§17.3): the focused inner render window has a
    # different id but its title still contains the keyword, so it counts as "on game".
    focus_keyword = args.focus_keyword or (title if title and not title.startswith("(") else None) or args.title
    if not focus_keyword:
        print("[demo] WARNING: no focus keyword resolved -- pass --focus-keyword (e.g. --focus-keyword anemoi), "
              "else the loop stays paused (can't verify focus).")
    size = f"{geom.width}x{geom.height} @ ({geom.x},{geom.y})" if geom else "unknown"
    print(f"[demo] bound window {window_id}  title={title!r}")
    print(f"[demo] geometry = {size}   <- confirm this is the BIG render window, not the small one")
    print(f"[demo] focus keyword = {focus_keyword!r}")

    mem = GameMemorySqliteAdapter(Path(tempfile.mkdtemp()) / "galgame_demo.sqlite3")

    def sink(event) -> None:
        kind = getattr(event, "kind", "")
        if kind == "galgame_stable_line_committed":
            speaker = getattr(event, "speaker", None)
            print(f"  ✔ [{speaker or '—'}] {getattr(event, 'text', '')}")
        elif kind == "galgame_window_lost":
            print(f"[paused] reason={getattr(event, 'reason', '')}")
        elif kind == "galgame_window_recovered":
            print("[recovered] OCR resumed")

    session = GalgameCompanionSession(mem, emit=sink)
    session.bind_game("demo_anemoi")
    session.start()

    match_rule = WindowMatchRule(title_keywords=[focus_keyword] if focus_keyword else [])
    runner = OcrStreamRunner(session, MssScreenCapture(), locator, RapidOcrAdapter(), interval_seconds=args.interval)
    runner.start(window_id, dialog_ratios=args.dialog, match_rule=match_rule, speaker_ratios=args.speaker)
    print("[demo] loop running. Advance the game to see lines commit. Ctrl-C to stop.\n")
    try:
        while True:
            time.sleep(2.0)
            print(f"[demo] last ocr_cycle_ms={runner.last_cycle_ms:.0f}  state={session.state.value}")
    except KeyboardInterrupt:
        print("\n[demo] stopping...")
    finally:
        runner.stop()
        # end() commits the final pending line (§16.4); robust across the active states.
        try:
            session.end()
        except Exception as exc:  # noqa: BLE001 -- demo shutdown, just report
            print(f"[demo] end skipped: {exc}")
        print(f"[demo] committed lines in buffer this session: {len(session.unsummarized_line_ids)}")


if __name__ == "__main__":
    main()

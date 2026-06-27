"""Path B stage 1 acceptance: companion controller on the REAL persistent DB.

Unlike galgame_summary_demo.py (tempfile + hardcoded game_id), this drives the
host-wired GalgameCompanionController -- so OCR'd lines, summaries, progress and
relations land in the REAL store ``spica_data/galgame.sqlite3`` under a dynamic
game_id, and survive the process exiting.

Acceptance (LimeLight running, X11 session, dialogue LLM configured):

    python galgame_companion_demo.py --window-id 0x13800001 \
        --dialog 0.08,0.72,0.84,0.22 --trigger 400 --interval 1.0
    # (game_id defaults to the window title's leading word, e.g. "limelight";
    #  override with --game-id)

Play a scene, Ctrl-C to stop (final summary), then re-run with --query to confirm
the data is still there:

    python galgame_companion_demo.py --query --game-id limelight

Needs: mss, Pillow, rapidocr-onnxruntime, wmctrl, xprop + a working dialogue LLM.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time


def _ratios(text: str):
    parts = [float(p) for p in text.split(",")]
    if len(parts) != 4:
        raise argparse.ArgumentTypeError("ratios must be 'x,y,w,h'")
    return (parts[0], parts[1], parts[2], parts[3])


def _collect_providers(obj, _depth=0, _seen=None):
    """Walk a RapidOCR engine's sub-objects for onnxruntime sessions' actual
    providers. Bounded + type-guarded (only recurse into rapidocr/onnxruntime
    objects) so it can't wander into numpy / huge graphs. Diagnostic only."""
    if _seen is None:
        _seen = set()
    if _depth > 3 or id(obj) in _seen:
        return set()
    _seen.add(id(obj))
    found = set()
    getp = getattr(obj, "get_providers", None)
    if callable(getp):
        try:
            found.update(getp())
        except Exception:  # noqa: BLE001
            pass
    for name in dir(obj):
        if name.startswith("__"):
            continue
        try:
            child = getattr(obj, name)
        except Exception:  # noqa: BLE001
            continue
        if isinstance(child, (str, bytes, int, float, bool, list, tuple, dict, set)) or callable(child):
            continue
        if (type(child).__module__ or "").startswith(("rapidocr", "onnxruntime")):
            found |= _collect_providers(child, _depth + 1, _seen)
    return found


def _ocr_providers():
    """(available providers, providers ACTUALLY used by the shared RapidOCR engine).
    Reuses backends.rapidocr._get_engine() -- the SAME engine the OCR loop uses --
    so it does not double-load the model."""
    import onnxruntime

    from agent_tools.function_tools.screen.backends import rapidocr as ocr_backend

    available = list(onnxruntime.get_available_providers())
    used: list[str] = []
    try:
        used = sorted(_collect_providers(ocr_backend._get_engine()))
    except Exception as exc:  # noqa: BLE001
        print(f"[demo] could not introspect OCR providers: {exc}")
    return available, used


def _ask(host, question: str, label: str) -> None:
    """Stage-2 acceptance probe: drive ONE normal dialogue turn through the regular
    ChatEngine entry (run_voice) -- no galgame params passed here at all. While
    companion play is active, the host-wired provider must land the turn in the
    galgame conversation (story injected); after stop() it must revert to plain
    chat. The payload's conversation_id is the hard evidence either way."""
    print(f"\n[demo:{label}] Q: {question}")
    try:
        result = host.chat_engine.run_voice(question)
    except Exception as exc:  # noqa: BLE001 -- the probe must not kill the demo
        print(f"[demo:{label}] turn failed: {exc}")
        return
    print(f"[demo:{label}] turn conversation_id = {result.get('conversation_id')!r}")
    if result.get("error"):
        print(f"[demo:{label}] turn error = {result.get('error')}")
    print(f"[demo:{label}] A: {result.get('answer')}\n")


def _query(game_id: str) -> None:
    # Re-open the SAME real DB in a fresh process to prove persistence.
    from pathlib import Path

    from spica.adapters.game_memory.sqlite import GameMemorySqliteAdapter

    repo_root = Path(__file__).resolve().parent
    mem = GameMemorySqliteAdapter(repo_root / "spica_data" / "galgame.sqlite3")
    lines = mem.committed_story_lines(game_id)
    summaries = mem.recent_summaries(game_id, limit=10)
    print(f"[query] DB = {repo_root / 'spica_data' / 'galgame.sqlite3'}")
    print(f"[query] game_id={game_id!r}: {len(lines)} committed lines, {len(summaries)} summaries")
    for line in lines[-10:]:
        print(f"   ✔ [{line.speaker or '—'}] {line.text}")
    for summary in summaries:
        print(f"   [摘要] {summary.summary_zh}")
    progress = mem.get_progress_state(game_id)
    if progress is not None:
        print(f"   进度: route={progress.route} chapter={progress.chapter}")


def main() -> None:
    if os.environ.get("QT_QPA_PLATFORM") == "offscreen":
        os.environ.pop("QT_QPA_PLATFORM")
    # Diagnostics (Path B stage-1 perf probe): a root handler at INFO so ocr_loop's
    # "ocr_cycle_ms=N exceeded interval_ms" WARNING is not swallowed. No logic change.
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    parser = argparse.ArgumentParser(description="Path B stage 1 companion demo (real persistent DB)")
    parser.add_argument("--query", action="store_true", help="just query the real DB for --game-id and exit")
    parser.add_argument("--game-id", default=None, help="explicit game_id (else guessed from the window title)")
    target = parser.add_mutually_exclusive_group(required=False)
    target.add_argument("--title")
    target.add_argument("--window-id")
    parser.add_argument("--dialog", type=_ratios, default=(0.08, 0.72, 0.84, 0.22))
    parser.add_argument("--speaker", type=_ratios, default=None)
    parser.add_argument("--interval", type=float, default=0.3)  # reaches the runner now (was dropped before)
    parser.add_argument("--trigger", type=int, default=None, help="override summary trigger chars")
    parser.add_argument("--ask", default=None, help="stage-2 probe: ask this once during play (after --ask-delay) and once after stop")
    parser.add_argument("--ask-delay", type=float, default=20.0, help="seconds of play before the --ask probe fires")
    args = parser.parse_args()

    if args.query:
        if not args.game_id:
            print("[demo] --query needs --game-id")
            sys.exit(1)
        _query(args.game_id)
        return

    if not args.title and not args.window_id:
        print("[demo] need --title or --window-id (or --query --game-id)")
        sys.exit(1)

    from spica.host.app_host import AppHost

    host = AppHost()
    host.initialize()  # builds the REAL services incl. spica_data/galgame.sqlite3
    if args.trigger is not None:
        host.config.galgame.summary_trigger_chars = args.trigger

    locator = host.services.window_locator_adapter
    enumeration = locator.enumerate_windows()
    if not enumeration.available:
        print(f"[demo] cannot enumerate windows: {enumeration.reason_code} -- {enumeration.reason}")
        sys.exit(1)
    by_id = {w.window_id: w for w in enumeration.windows}
    if args.window_id:
        window_id, title = args.window_id, (by_id[args.window_id].title if args.window_id in by_id else None)
    else:
        match = next((w for w in enumeration.windows if args.title.lower() in (w.title or "").lower()), None)
        if match is None:
            print(f"[demo] no window title contains {args.title!r}")
            sys.exit(1)
        window_id, title = match.window_id, match.title
    geom = locator.get_window_geometry(window_id)
    if geom is None:
        print(f"[demo] no geometry for window {window_id!r}")
        sys.exit(1)

    def sink(event) -> None:
        kind = getattr(event, "kind", "")
        if kind == "galgame_stable_line_committed":
            print(f"  ✔ [{getattr(event, 'speaker', None) or '—'}] {getattr(event, 'text', '')}")
        elif kind == "galgame_summary_done":
            print("[summary] done" if getattr(event, "summary_id", None) else "[summary] failed (folds + retries)")
        elif kind == "galgame_window_lost":
            print(f"[paused] reason={getattr(event, 'reason', '')}")
        elif kind == "galgame_window_recovered":
            print("[recovered]")

    host.attach_companion_sink(sink)
    # Stage 2: MUST go through the host accessor (the singleton the ChatEngine
    # provider reads) -- a new_companion_controller() instance would never be seen
    # by the dialogue auto-injection.
    controller = host.companion_controller()
    game_id = controller.start(window_id, game_id=args.game_id, window_title=title, dialog_ratios=args.dialog, speaker_ratios=args.speaker, interval_seconds=args.interval)
    print(f"[demo] started: game_id={game_id!r} window={window_id} geometry={geom.width}x{geom.height}")
    # Diagnostic: is OCR actually on CUDA in THIS (full-stack) process, or did it fall
    # back to CPU? Builds the shared engine the OCR loop will reuse (no double-load).
    available, used = _ocr_providers()
    print(f"[demo] onnxruntime available = {available}")
    print(f"[demo] OCR providers = {used}   <- CUDAExecutionProvider = GPU; only CPUExecutionProvider = fell back to CPU")
    print("[demo] data persists to spica_data/galgame.sqlite3. Play a scene; Ctrl-C to stop.\n")
    try:
        asked = False
        t0 = time.monotonic()
        while True:
            time.sleep(2.0)
            if args.ask and not asked and time.monotonic() - t0 >= args.ask_delay:
                asked = True
                _ask(host, args.ask, "playing")  # expect galgame:: conversation + story-aware answer
            runner, session = controller._runner, controller.session  # diagnostic read (demo only)
            if runner is not None and session is not None:
                print(f"[demo] last ocr_cycle_ms={runner.last_cycle_ms:.0f}  state={session.state.value}")
    except KeyboardInterrupt:
        print("\n[demo] stopping (final summary)...")
    finally:
        controller.stop()
        if args.ask:
            _ask(host, args.ask, "after-stop")  # expect conversation_id='default', no story injection
        print(f"[demo] stopped. Verify persistence with:  python {os.path.basename(__file__)} --query --game-id {game_id}")


if __name__ == "__main__":
    main()

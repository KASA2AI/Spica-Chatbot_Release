"""Standalone OCR -> summary demo (Phase 8 real-machine acceptance).

Strings together: continuous OCR collection -> background/end summarization (real
LLM via the dialogue endpoint) -> prints the Chinese summary + characters +
progress/relations. Use it on a real anemoi scene to judge summary quality.

Example (anemoi open, X11 session, dialogue LLM configured in xiaosan.env):

    python galgame_summary_demo.py --window-id 0x13800001 \
        --dialog 0.08,0.72,0.84,0.22 --trigger 400 --interval 1.0

  --title / --window-id : bind the game window (id when titles collide), like the
                          OCR stream demo
  --trigger             : background summary fires ~every this many committed chars
                          (set low, e.g. 400, to see a summary within one scene)
  --dialog / --speaker / --interval : as in galgame_ocr_stream_demo.py

Advance through a scene (e.g. 麦和六花去真澄镇); a summary prints when the buffer
crosses --trigger, and a final summary prints on Ctrl-C (end). Needs: mss, Pillow,
rapidocr-onnxruntime, wmctrl, xprop, AND a working dialogue LLM endpoint.
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
import time
from pathlib import Path

GAME_ID = "demo_summary"


def _ratios(text: str):
    parts = [float(p) for p in text.split(",")]
    if len(parts) != 4:
        raise argparse.ArgumentTypeError("ratios must be 'x,y,w,h'")
    return (parts[0], parts[1], parts[2], parts[3])


def main() -> None:
    if os.environ.get("QT_QPA_PLATFORM") == "offscreen":
        os.environ.pop("QT_QPA_PLATFORM")

    parser = argparse.ArgumentParser(description="Phase 8 galgame OCR -> summary demo")
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--title")
    target.add_argument("--window-id")
    parser.add_argument("--focus-keyword", default=None)
    parser.add_argument("--dialog", type=_ratios, default=(0.08, 0.72, 0.84, 0.22))
    parser.add_argument("--speaker", type=_ratios, default=None)
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--trigger", type=int, default=400, help="summary every ~N committed chars")
    args = parser.parse_args()

    import httpx
    from openai import OpenAI

    from spica.adapters.game_memory.sqlite import GameMemorySqliteAdapter
    from spica.adapters.llm import OpenAICompatibleAdapter
    from spica.adapters.ocr import RapidOcrAdapter
    from spica.adapters.screen_capture import MssScreenCapture
    from spica.adapters.window_locator import LinuxX11WindowLocator
    from spica.config.manager import ConfigManager
    from spica.config.secrets import load_secrets
    from spica.galgame.models import WindowMatchRule
    from spica.galgame.ocr_loop import OcrStreamRunner
    from spica.galgame.session import GalgameCompanionSession
    from spica.galgame.summarizer import GalgameSummarizer
    from spica.runtime.jobs import ThreadJobRunner

    # --- resolve window (same as the stream demo) ---
    locator = LinuxX11WindowLocator()
    enumeration = locator.enumerate_windows()
    if not enumeration.available:
        print(f"[demo] cannot enumerate windows: {enumeration.reason_code} -- {enumeration.reason}")
        sys.exit(1)
    by_id = {w.window_id: w for w in enumeration.windows}
    if args.window_id:
        window_id = args.window_id
        geom = locator.get_window_geometry(window_id)
        title = by_id[window_id].title if window_id in by_id else "(not in wmctrl list)"
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
    focus_keyword = args.focus_keyword or (title if title and not title.startswith("(") else None) or args.title
    print(f"[demo] bound {window_id}  title={title!r}  geometry={geom.width}x{geom.height}  focus_keyword={focus_keyword!r}")

    # --- build the real summarizer over the dialogue endpoint ---
    config = ConfigManager().load()
    secrets = load_secrets()
    if not secrets.openai_api_key:
        print("[demo] no OPENAI_API_KEY (check xiaosan.env) -- summary needs the dialogue LLM endpoint.")
        sys.exit(1)
    client = OpenAI(api_key=secrets.openai_api_key, base_url=config.llm.base_url, http_client=httpx.Client(trust_env=False, timeout=60))
    summary_model = config.galgame.summary_model or config.llm.model
    summarizer = GalgameSummarizer(OpenAICompatibleAdapter(client), summary_model)
    print(f"[demo] summary model = {summary_model}")

    mem = GameMemorySqliteAdapter(Path(tempfile.mkdtemp()) / "galgame_summary_demo.sqlite3")

    def print_latest_summary() -> None:
        recent = mem.recent_summaries(GAME_ID, limit=1)
        if recent:
            s = recent[0]
            print("\n========== 剧情摘要 ==========")
            print(f"摘要: {s.summary_zh}")
            print(f"角色: {s.characters}")
            print(f"关键事件: {s.major_events}")
            print(f"未解伏笔: {s.unresolved_threads}")
        progress = mem.get_progress_state(GAME_ID)
        if progress is not None:
            print(f"进度: route={progress.route}  chapter={progress.chapter}")
        for rel in mem.character_relations(GAME_ID):
            print(f"关系: {rel.character_a} - {rel.character_b}: {rel.relation_summary} (置信度 {rel.confidence})")
        print("==============================\n")

    def sink(event) -> None:
        kind = getattr(event, "kind", "")
        if kind == "galgame_stable_line_committed":
            print(f"  ✔ [{getattr(event, 'speaker', None) or '—'}] {getattr(event, 'text', '')}")
        elif kind == "galgame_summary_started":
            print("[summary] 后台整理剧情中...")
        elif kind == "galgame_summary_done":
            if getattr(event, "summary_id", None) is None:
                print("[summary] 本次总结失败（行已保留，下次折叠重试）")
            else:
                print_latest_summary()
        elif kind == "galgame_window_lost":
            print(f"[paused] reason={getattr(event, 'reason', '')}")
        elif kind == "galgame_window_recovered":
            print("[recovered]")

    session = GalgameCompanionSession(mem, emit=sink, jobs=ThreadJobRunner(), summarizer=summarizer, summary_trigger_chars=args.trigger)
    session.bind_game(GAME_ID)
    session.start()
    runner = OcrStreamRunner(session, MssScreenCapture(), locator, RapidOcrAdapter(), interval_seconds=args.interval)
    runner.start(window_id, dialog_ratios=args.dialog, match_rule=WindowMatchRule(title_keywords=[focus_keyword] if focus_keyword else []), speaker_ratios=args.speaker)
    print("[demo] loop running -- advance the scene; a summary prints at the threshold. Ctrl-C to end.\n")
    try:
        while True:
            time.sleep(2.0)
    except KeyboardInterrupt:
        print("\n[demo] ending -- final summary...")
    finally:
        runner.stop()
        try:
            session.end()  # commits pending + final summary (prints via the sink)
        except Exception as exc:  # noqa: BLE001
            print(f"[demo] end error: {exc}")


if __name__ == "__main__":
    main()

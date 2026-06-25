"""Real-machine timing capture launcher (telemetry-only, BEHAVIOUR-NEUTRAL).

The app logs at INFO (ui/qt_overlay.py basicConfig), so the existing DEBUG timing
instrumentation is suppressed on console. This launcher adds a DEBUG FileHandler
to the timing + reaction loggers, THEN starts the normal app -- so a real play
session is captured to a file for analysis. It changes nothing else: same app,
same behaviour, real memory (real data is the point). Console stays normal INFO
(the DEBUG lines are filtered there by the root handler's level; only the file
gets them).

What lands in the file (one parseable line each):
  - PER TURN (user AND proactive):
      [TIMING] step=chat_stream_done duration_ms=<total> interaction_mode=chat|system
        prompt_input_chars=.. tts_total_ms=<完整回答TTS> agent_rounds=.. agent_tool_local_ms=..
        first_llm_delta_ms=<首token> first_unit_ready_ms=<她开口> first_tts_done_ms=..
        llm_stream_fallback_used=.. llm_stream_fallback_reason=..  conversation_id=..
      (interaction_mode=system + galgame:: conversation_id == a proactive reaction speak turn)
  - PER JUDGE CALL (every real judge LLM call):
      [TIMING] step=reaction_judge duration_ms=<judge耗时> worth=<0-10> angle=.. degraded=true|false
  - REACTION GATE DECISION (did the worth get selected / dropped / refunded):
      reaction decision: ReactionDecision(kind='spoke'|'below_threshold'|'cooldown_drop'|...)
  - REAL STREAM BREAK (the fallback-rate signal -- NOT the benign deepseek bool):
      [TIMING] step=llm_stream_fallback ... reason=stream_create_error|stream_iteration_error|...
      [TIMING] step=llm_chat_stream_error ...
  - TOOL EXECUTION: folded into the chat_stream_done line (agent_rounds>1 + agent_tool_local_ms>0).

Fallback-rate note: deepseek ALWAYS sets llm_stream_fallback_used=true +
llm_stream_fallback_reason=chat_completions_compatible_client (benign chat-completions
routing). A REAL break = a separate `step=llm_stream_fallback` / `step=llm_chat_stream_error`
line, OR a chat_stream_done whose fallback_reason != chat_completions_compatible_client.

Usage:
    python scripts/play_with_timing.py [LOGFILE]
    # default: spica_data/timing_<YYYYmmdd_HHMMSS>.log
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

# The two loggers that carry the structured telemetry. common.timing = every
# log_timing line (turns / judge / fallback / tool / stages); spica.galgame.reaction
# = the reaction gate decision trail (spoke / below_threshold / cooldown_drop / ...).
TELEMETRY_LOGGERS = ("common.timing", "spica.galgame.reaction")


def main() -> int:
    if len(sys.argv) > 1:
        logfile = Path(sys.argv[1])
    else:
        logfile = REPO / "spica_data" / f"timing_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    logfile.parent.mkdir(parents=True, exist_ok=True)

    handler = logging.FileHandler(logfile, encoding="utf-8")
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(logging.Formatter("%(asctime)s %(name)s %(message)s"))
    for name in TELEMETRY_LOGGERS:
        lg = logging.getLogger(name)
        lg.setLevel(logging.DEBUG)  # own level -> emits DEBUG to its handlers
        lg.addHandler(handler)      # file handler captures; root (INFO) still filters console

    print(f"[timing capture] DEBUG timing -> {logfile}")
    print("[timing capture] launching app (console stays normal; file gets the full trace)")

    # Hand the app a clean argv (it does QApplication(sys.argv)); our LOGFILE arg
    # must not reach Qt.
    sys.argv = [sys.argv[0]]
    from ui.qt_overlay import main as app_main  # imported late: Qt only when launching

    return int(app_main() or 0)


if __name__ == "__main__":
    raise SystemExit(main())

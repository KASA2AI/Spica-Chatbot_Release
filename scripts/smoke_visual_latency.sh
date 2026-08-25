#!/usr/bin/env bash
set -euo pipefail

SPICA_SMOKE_REPO_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
SPICA_SMOKE_LOG="${SPICA_DESKTOP_LOG:-/data/tmp/spica-desktop-logs/desktop.log}"
SPICA_SMOKE_PYTHON="${SPICA_SMOKE_PYTHON:-python3}"

usage() {
  printf '%s\n' \
    "用法：" \
    "  ./scripts/smoke_visual_latency.sh                 # 真人操作后分析新增日志" \
    "  ./scripts/smoke_visual_latency.sh --current-boot  # 分析本次开机已有日志" \
    "" \
    "只检查立绘切换的 UI 延迟；不会重启、停止或修改 Spica。"
}

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  usage
  exit 0
fi
if [[ $# -gt 1 || ( $# -eq 1 && "$1" != "--current-boot" ) ]]; then
  usage >&2
  exit 2
fi
if [[ ! -r "$SPICA_SMOKE_LOG" ]]; then
  printf 'UNKNOWN: 无法读取桌面日志：%s\n' "$SPICA_SMOKE_LOG" >&2
  exit 2
fi

analyze_log() {
  local mode="$1"
  local start_offset="$2"
  "$SPICA_SMOKE_PYTHON" - "$SPICA_SMOKE_LOG" "$mode" "$start_offset" <<'PY'
from __future__ import annotations

import math
import re
import sys
from pathlib import Path
from statistics import median


log_path = Path(sys.argv[1])
mode = sys.argv[2]
start_offset = int(sys.argv[3])
raw = log_path.read_bytes()

if mode == "current_boot":
    marker = "===== spica desktop 启动".encode("utf-8")
    marker_index = raw.rfind(marker)
    if marker_index < 0:
        print("UNKNOWN: 日志中没有本次启动标记。", file=sys.stderr)
        raise SystemExit(2)
    sample = raw[marker_index:]
else:
    if len(raw) < start_offset:
        print("UNKNOWN: smoke 期间日志发生轮转，请重新执行。", file=sys.stderr)
        raise SystemExit(2)
    sample = raw[start_offset:]

text = sample.decode("utf-8", errors="replace")
voice_units = len(re.findall(r"Input #0, wav, from '.*generated_voice", text))
pattern = re.compile(
    r"WARNING ui\.qt_overlay: event=set_character_image_slow[^\n]*"
    r"path='([^']+)' duration_ms=([0-9.]+)"
)
rows = [(Path(path), float(duration)) for path, duration in pattern.findall(text)]

if not rows:
    if voice_units == 0:
        print("UNKNOWN: 没有采集到回复语音或立绘切换，请完成提示中的对话后再按回车。")
        raise SystemExit(2)
    print(f"PASS: voice_units={voice_units}, slow_sprite_switches=0")
    raise SystemExit(0)

durations = sorted(duration for _path, duration in rows)
p95_index = max(0, math.ceil(len(durations) * 0.95) - 1)
same_path = sum(
    rows[index][0] == rows[index - 1][0]
    for index in range(1, len(rows))
)
pose_counts: dict[str, int] = {}
for path, _duration in rows:
    pose = path.parent.name
    pose_counts[pose] = pose_counts.get(pose, 0) + 1

print(
    "RED: "
    f"voice_units={voice_units}, "
    f"slow_sprite_switches={len(rows)}, "
    f"median_ms={median(durations):.2f}, "
    f"p95_ms={durations[p95_index]:.2f}, "
    f"max_ms={max(durations):.2f}, "
    f"consecutive_same_path={same_path}, "
    f"poses={pose_counts}"
)
raise SystemExit(1)
PY
}

if [[ "${1:-}" == "--current-boot" ]]; then
  analyze_log current_boot 0
  exit $?
fi

if ! pgrep -f "$SPICA_SMOKE_REPO_DIR/webui_qt.py" >/dev/null; then
  printf 'UNKNOWN: 没有发现当前仓库的 Spica 桌面进程，请先启动应用。\n' >&2
  exit 2
fi

SPICA_SMOKE_START_OFFSET="$(stat -c %s "$SPICA_SMOKE_LOG")"
printf '%s\n' \
  "请在已经打开的 Spica 中依次发送以下四句话：" \
  "  1. 早上好，Spica。" \
  "  2. 请用三步解释梯度下降，每一步单独一句。" \
  "  3. 哼，你是不是吃醋了？" \
  "  4. 我今天有点难过，陪我说几句。" \
  "" \
  "等最后一句的语音和立绘播放完毕后，回到这里按 Enter。"
read -r

analyze_log new_sample "$SPICA_SMOKE_START_OFFSET"

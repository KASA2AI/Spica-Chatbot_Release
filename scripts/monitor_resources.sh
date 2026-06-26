#!/usr/bin/env bash
# Spica unattended-run resource + liveness monitor.
#
# Run this in a SECOND terminal when you start a long/auto galgame session and
# walk away. The in-app timing log does NOT capture: (a) a process crash time,
# (b) slow VRAM/RAM creep over hours, (c) a hang (app alive but frozen). This does.
#
# Every 30s appends one line to spica_data/monitor_<ts>.log:
#   GPU mem/util/temp + the app's RSS + ALIVE/GONE.
# Doubles as a death-clock: the last ALIVE line and the first GONE line bracket
# exactly when the app died -- decisive if it crashes while you're out.
# Auto-stops ~5 min after the app exits, or after a ~3.2h hard cap.
#
# Usage:  bash scripts/monitor_resources.sh
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1

TS=$(date +%Y%m%d_%H%M%S)
OUT="spica_data/monitor_${TS}.log"
echo "[monitor] writing ${OUT}  (Ctrl-C to stop)"
echo "# Spica resource/liveness monitor @ ${TS} (30s interval)" > "${OUT}"

gone=0
iters=0
while [ "${iters}" -lt 400 ]; do          # 400 * 30s ≈ 3.3h hard cap
  iters=$((iters + 1))
  now=$(date '+%Y-%m-%d %H:%M:%S')
  # bracket trick: never match this script's own command line
  pid=$(pgrep -f "[p]lay_with_timing\.py" | head -1)
  gpu=$(nvidia-smi --query-gpu=memory.used,utilization.gpu,temperature.gpu \
        --format=csv,noheader,nounits 2>/dev/null | head -1 | tr -d ' ')
  if [ -n "${pid}" ]; then
    rss=$(awk '/VmRSS/{printf "%d", $2/1024}' /proc/"${pid}"/status 2>/dev/null)
    echo "${now} ALIVE pid=${pid} rss_mb=${rss:-?} gpu[memMiB,util%,tempC]=${gpu}" >> "${OUT}"
    gone=0
  else
    gone=$((gone + 1))
    echo "${now} GONE  gpu[memMiB,util%,tempC]=${gpu}" >> "${OUT}"
    [ "${gone}" -ge 10 ] && { echo "${now} monitor stop (app gone ~5min)" >> "${OUT}"; break; }
  fi
  sleep 30
done
echo "[monitor] done -> ${OUT}"

#!/usr/bin/env bash
# Capture diagnostics when the Spica app is FROZEN / unresponsive.
#
# Run this in a SECOND terminal WHILE the app is hung -- BEFORE you kill it.
# It writes everything to spica_data/freeze_dump_<timestamp>.txt; send that file.
#
# What it grabs:
#   1) py-spy thread dump  -- WHERE each thread is stuck (OCR/Moondream/TTS/a lock?).
#      Two dumps ~2s apart: identical stacks across both = genuinely hung.
#   2) nvidia-smi          -- GPU util + per-process VRAM (is the GPU maxed/stuck?).
#   3) dmesg Xid           -- GPU driver-level errors (Xid = a real GPU fault).
#
# Usage:  bash scripts/dump_when_frozen.sh

set -uo pipefail
cd "$(dirname "$0")/.." || exit 1   # repo root

TS=$(date +%Y%m%d_%H%M%S)
OUT="spica_data/freeze_dump_${TS}.txt"
APP_PID=$(pgrep -f "play_with_timing.py" | head -1)

{
  echo "===== Spica freeze dump @ ${TS} ====="
  echo "app pid (play_with_timing.py): ${APP_PID:-NOT FOUND}"
  echo

  echo "===== 1) py-spy thread dump (the decisive one: where is it stuck?) ====="
  PYSPY="py-spy"
  PTRACE=$(cat /proc/sys/kernel/yama/ptrace_scope 2>/dev/null || echo 1)
  if [ "${PTRACE}" != "0" ]; then
    PYSPY="sudo py-spy"   # ptrace restricted -> need root to inspect another process
    echo "(ptrace_scope=${PTRACE} -> 用 sudo py-spy,可能要输一次密码)"
  fi
  if ! command -v py-spy >/dev/null 2>&1; then
    echo "py-spy NOT installed -> run:  pip install py-spy   then re-run this script."
  elif [ -z "${APP_PID}" ]; then
    echo "app process not found via 'pgrep -f play_with_timing.py'. Is it still running?"
  else
    [ "${PYSPY}" = "sudo py-spy" ] && sudo -v   # prime sudo creds before the timed dumps
    for i in 1 2; do
      echo "--- py-spy dump #${i} (pid ${APP_PID}) ---"
      if ! ${PYSPY} dump --pid "${APP_PID}" 2>&1; then
        echo "(py-spy failed -- try manually:  sudo py-spy dump --pid ${APP_PID})"
      fi
      echo
      [ "${i}" = 1 ] && sleep 2
    done
  fi
  echo

  echo "===== 2) nvidia-smi (GPU util + memory) ====="
  nvidia-smi 2>&1 || echo "nvidia-smi unavailable"
  echo
  echo "--- per-process GPU memory (which model holds what) ---"
  nvidia-smi --query-compute-apps=pid,process_name,used_memory --format=csv 2>&1 || true
  echo

  echo "===== 3) GPU driver errors (dmesg Xid/NVRM) ====="
  if dmesg 2>/dev/null | grep -i -E "xid|nvrm" | tail -20; then
    :
  else
    echo "(no Xid/NVRM lines, or dmesg needs root -> try:  sudo dmesg | grep -i xid | tail)"
  fi
} 2>&1 | tee "${OUT}"

echo
echo "[saved] ${OUT}   <- 把这个文件发给我"

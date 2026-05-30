#!/usr/bin/env bash
set -e

cd /home/san/ai_code/Spica-Chatbot

export QT_IM_MODULE=ibus
export GTK_IM_MODULE=ibus
export XMODIFIERS=@im=ibus
export ALSA_PLUGIN_DIR=/usr/lib/x86_64-linux-gnu/alsa-lib

ibus-daemon -drx 2>/dev/null || true
ibus engine libpinyin 2>/dev/null || true

exec /home/san/anaconda3/envs/gptsovits/bin/python3.11 /home/san/ai_code/Spica-Chatbot/webui_qt.py

#!/usr/bin/env bash
# ربات را همیشه روشن نگه می‌دارد: اگر کرش کرد، بعد از ۱۰ ثانیه دوباره اجرا می‌شود.
# اجرا:  ./run_forever.sh      (یا در کرون با @reboot)
# توقف:  Ctrl+C
set -u
cd "$(dirname "$0")"

PY=".venv/bin/python"
[ -x "$PY" ] || PY="python3"

while true; do
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] starting bot..."
  "$PY" rubika_bot.py >> rubika_bot.log 2>&1
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] bot stopped; restarting in 10s..."
  sleep 10
done

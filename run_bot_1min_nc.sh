#!/usr/bin/env bash
# Starts bot_1min_nc.py (no cooldown) in the background.

set -u
BOT_DIR="/Users/yashthakkar/trading-bot/alpacabot"
PYTHON="$BOT_DIR/.venv/bin/python3"

cd "$BOT_DIR"

if [ -f .env ]; then
    set -a; source .env; set +a
fi

if [ -f bot_1min_nc.pid ] && kill -0 "$(cat bot_1min_nc.pid 2>/dev/null)" 2>/dev/null; then
    echo "[$(date)] Bot 1min_nc already running with PID $(cat bot_1min_nc.pid). Aborting." >&2
    exit 1
fi

mkdir -p logs
LOGDATE=$(date +%Y%m%d)
LOGFILE="logs/bot_1min_nc_${LOGDATE}.log"

echo "[$(date)] Starting bot_1min_nc.py → $LOGFILE"
nohup "$PYTHON" bot_1min_nc.py >> "$LOGFILE" 2>&1 &
PID=$!
echo "$PID" > bot_1min_nc.pid

nohup caffeinate -dimsu -w "$PID" >> "$LOGFILE" 2>&1 &
disown

echo "[$(date)] Bot 1min_nc started with PID $PID (caffeinated)"

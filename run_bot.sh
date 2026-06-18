#!/usr/bin/env bash
# Starts bot.py in the background and writes its PID to bot.pid.
# Refuses to start if a previous instance is still running.

set -u
BOT_DIR="/Users/manalithakkar/Documents/alpacabot"
PYTHON="/Library/Frameworks/Python.framework/Versions/3.10/bin/python3"

cd "$BOT_DIR"

# Load credentials from .env if present
if [ -f .env ]; then
    set -a; source .env; set +a
fi

# Refuse to start if already running
if [ -f bot.pid ] && kill -0 "$(cat bot.pid 2>/dev/null)" 2>/dev/null; then
    echo "[$(date)] Bot already running with PID $(cat bot.pid). Aborting." >&2
    exit 1
fi

mkdir -p logs
LOGDATE=$(date +%Y%m%d)
LOGFILE="logs/bot_${LOGDATE}.log"

echo "[$(date)] Starting bot.py → $LOGFILE"
nohup "$PYTHON" bot.py >> "$LOGFILE" 2>&1 &
PID=$!
echo "$PID" > bot.pid

# Keep Mac awake (display + system) for the bot's lifetime only.
# `-w $PID` ties the no-sleep assertion to the bot — exits when bot exits.
nohup caffeinate -dimsu -w "$PID" >> "$LOGFILE" 2>&1 &
disown

echo "[$(date)] Bot started with PID $PID (caffeinated)"

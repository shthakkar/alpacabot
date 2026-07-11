#!/usr/bin/env bash
# Starts the 5m5mcall bot. Sleeps 5s to let IEX publish the 9:30 candle.

set -u

# Set BOT_DIR to the directory containing this script
BOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="${PYTHON:-python3}"  # Use PYTHON env var or fallback to python3

cd "$BOT_DIR"

# Load .env if it exists
if [ -f .env ]; then
    set -a
    source .env
    set +a
fi

# Refuse to start if already running
if [ -f bot.pid ] && kill -0 "$(cat bot.pid 2>/dev/null)" 2>/dev/null; then
    echo "[$(date)] 5m5mcall already running with PID $(cat bot.pid). Aborting." >&2
    exit 1
fi

mkdir -p logs
LOGDATE=$(date +%Y%m%d)
LOGFILE="logs/bot_${LOGDATE}.log"

echo "[$(date)] Sleeping 5s before start..."
sleep 5

echo "[$(date)] Starting bot.py → $LOGFILE"
nohup "$PYTHON" bot.py >> "$LOGFILE" 2>&1 &
PID=$!
echo "$PID" > bot.pid

# Optional: keep Mac awake during trading (requires caffeinate; skip if on Linux)
if command -v caffeinate &> /dev/null; then
    nohup caffeinate -dimsu -w "$PID" >> "$LOGFILE" 2>&1 &
    disown
fi

echo "[$(date)] 5m5mcall started with PID $PID"

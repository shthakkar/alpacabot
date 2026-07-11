#!/usr/bin/env bash
# Stops the 5m5mcall bot cleanly via SIGTERM.

BOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

cd "$BOT_DIR"

if [ ! -f bot.pid ]; then
    echo "[$(date)] No bot.pid found — bot may not be running." >&2
    exit 0
fi

PID=$(cat bot.pid)
if kill -0 "$PID" 2>/dev/null; then
    echo "[$(date)] Sending SIGTERM to PID $PID"
    kill -TERM "$PID"
else
    echo "[$(date)] PID $PID not running — cleaning up stale bot.pid"
fi

rm -f bot.pid

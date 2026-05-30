#!/usr/bin/env bash
# Sends SIGTERM to bot.py and waits for it to finish the current cycle
# (graceful shutdown). Falls back to SIGKILL after 90 seconds.

set -u
BOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$BOT_DIR"

if [ ! -f bot.pid ]; then
    echo "[$(date)] No bot.pid — nothing to stop."
    exit 0
fi

PID=$(cat bot.pid)
if ! kill -0 "$PID" 2>/dev/null; then
    echo "[$(date)] PID $PID is not running — removing stale bot.pid."
    rm -f bot.pid
    exit 0
fi

echo "[$(date)] Sending SIGTERM to PID $PID..."
kill -TERM "$PID"

# Wait up to 90 seconds for the bot to finish its cycle and exit
for i in $(seq 1 90); do
    if ! kill -0 "$PID" 2>/dev/null; then
        echo "[$(date)] Bot stopped cleanly after ${i}s."
        rm -f bot.pid
        exit 0
    fi
    sleep 1
done

echo "[$(date)] Bot didn't exit in 90s — sending SIGKILL." >&2
kill -KILL "$PID" 2>/dev/null || true
rm -f bot.pid
exit 1

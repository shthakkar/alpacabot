#!/usr/bin/env bash
set -u
BOT_DIR="/Users/yashthakkar/trading-bot/alpacabot"
cd "$BOT_DIR"

if [ ! -f bot_1min_nc.pid ]; then
    echo "[$(date)] No bot_1min_nc.pid — nothing to stop."
    exit 0
fi

PID=$(cat bot_1min_nc.pid)
if ! kill -0 "$PID" 2>/dev/null; then
    echo "[$(date)] PID $PID is not running — removing stale bot_1min_nc.pid."
    rm -f bot_1min_nc.pid
    exit 0
fi

echo "[$(date)] Sending SIGTERM to PID $PID..."
kill -TERM "$PID"

for i in $(seq 1 90); do
    if ! kill -0 "$PID" 2>/dev/null; then
        echo "[$(date)] Bot 1min_nc stopped cleanly after ${i}s."
        rm -f bot_1min_nc.pid
        exit 0
    fi
    sleep 1
done

echo "[$(date)] Bot didn't exit in 90s — sending SIGKILL." >&2
kill -KILL "$PID" 2>/dev/null || true
rm -f bot_1min_nc.pid
exit 1

# SPY Options Mean-Reversion Bot

An automated paper-trading bot for SPY options on [Alpaca](https://alpaca.markets/).
It watches SPY on 5-minute bars and enters short-dated options on Bollinger Band +
RSI mean-reversion signals, then manages the position with avg-ups, a stop, a target,
and a hard time exit.

> ⚠️ **Paper trading by default.** The bot refuses to run against a live account unless
> you explicitly set `ALPACA_PAPER=false`. Options trading carries real risk — use at
> your own discretion. This is not financial advice.

---

## Strategy at a glance

- **Signal** — mean reversion on 5-min bars: **CALL** if SPY < lower Bollinger Band *and* RSI < 35; **PUT** if SPY > upper band *and* RSI > 65.
- **Entry** — market buy 1 contract, nearest OTM strike, 2nd-soonest expiry.
- **Avg-ups** — at +1.5% and +3.0% on premium.
- **Stop** — −8% on average cost (recalculated after each avg-up).
- **Target** — +6% on entry.
- **Time exit** — close 30 minutes after entry.
- **Active hours** — 10:00–15:45 ET; last entry 15:15 ET.
- **One trade at a time** — a state machine forbids overlapping trades.

See [`RUNBOOK.md`](RUNBOOK.md) for full operating procedures, monitoring, and troubleshooting.

---

## Requirements

- Python 3.10+
- An Alpaca account with API keys (paper keys recommended)
- macOS or Linux (the launcher uses `caffeinate` on macOS to keep the machine awake — harmless elsewhere)

---

## Setup

```bash
# 1. Clone and enter
git clone https://github.com/<your-username>/alpacabot.git
cd alpacabot

# 2. (Recommended) virtual environment
python3 -m venv .venv && source .venv/bin/activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Add your credentials
cp .env.example .env
#   then edit .env and paste your Alpaca PAPER keys
```

### Credentials

Credentials are **never hardcoded** — they're read from environment variables:

| Variable | Meaning |
|---|---|
| `ALPACA_API_KEY` | Your Alpaca API key ID |
| `ALPACA_API_SECRET` | Your Alpaca API secret |
| `ALPACA_PAPER` | `true` (paper, default) or `false` (live) |

Put them in `.env` (gitignored) or export them in your shell. Verify they work:

```bash
set -a; . ./.env; set +a   # load .env into the shell
python3 alpaca_verify.py    # should report account access + a SPY quote
```

---

## Running manually

```bash
./run_bot.sh     # starts bot.py in the background, writes bot.pid, logs to logs/
./stop_bot.sh    # graceful SIGTERM, falls back to SIGKILL after 90s
```

Watch it live:

```bash
tail -f logs/bot_$(date +%Y%m%d).log
```

`run_bot.sh` automatically loads `.env`, resolves its own directory (so it works from
any path, including cron), and refuses to start if a previous instance is still running.

---

## Automating with cron

Once you've supervised at least one good session, schedule the start/stop with cron.
The bot is a long-running process during market hours — cron simply **starts** it before
the open and **stops** it after the close.

Open your crontab:

```bash
crontab -e
```

Add these lines, **using the absolute path to your clone** (Mon–Fri). The times below
are examples — adjust to your timezone. The strategy's active window is 10:00–15:45 ET.

```cron
# Start 5 min before the active window (example: 6:55 AM PT = 9:55 AM ET)
55  6  *  *  1-5  /ABSOLUTE/PATH/TO/alpacabot/run_bot.sh  >> /ABSOLUTE/PATH/TO/alpacabot/logs/cron.log 2>&1

# Stop after the EOD force-close (example: 1:00 PM PT = 4:00 PM ET)
 0 13  *  *  1-5  /ABSOLUTE/PATH/TO/alpacabot/stop_bot.sh >> /ABSOLUTE/PATH/TO/alpacabot/logs/cron.log 2>&1
```

Cron format reminder: `minute hour day-of-month month day-of-week`. `1-5` = Mon–Fri.

Verify it's installed:

```bash
crontab -l        # should list your two lines
tail logs/cron.log
```

Notes:
- cron picks up changes automatically — no restart needed.
- Make sure the scripts are executable: `chmod +x run_bot.sh stop_bot.sh`.
- cron runs without your interactive shell, so credentials must be in `.env` (the launcher loads it for you).
- On market holidays cron still fires, but the bot's staleness check refuses to trade when the market is closed. To stay silent, comment out the cron lines the night before.

---

## Project layout

| File | Purpose |
|---|---|
| `config.py` | Credentials (from env) + strategy parameters |
| `data.py` | SPY bar fetcher |
| `indicators.py` | Bollinger Bands + RSI |
| `signals.py` | Mean-reversion signal detector |
| `options.py` | Option chain → contract resolution + quotes |
| `orders.py` | Alpaca order submission / management |
| `trade_manager.py` | In-trade state machine |
| `bot.py` | Main live runner |
| `run_bot.sh` / `stop_bot.sh` | Background launcher / stopper |
| `alpaca_verify.py` | Credential + connectivity check |
| `backtest*.py` | Historical analysis scripts (not used by the live bot) |
| `test_step*.py` | Step-by-step development tests |
| `RUNBOOK.md` | Full operating runbook |

---

## Disclaimer

For educational and personal paper-trading use. Trading options involves substantial risk
of loss. Nothing here is financial advice. You are responsible for any orders this software
places on your account.

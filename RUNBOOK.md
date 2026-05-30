# SPY Mean-Reversion Bot — Runbook

Operating instructions for the paper-trading bot in this directory.

---

## Strategy at a glance

- **Signal**: Mean reversion. CALL if SPY < lower BB AND RSI < 35; PUT if SPY > upper BB AND RSI > 65 (65/35 thresholds, 5-min bars)
- **Entry**: MARKET BUY 1 contract — nearest OTM strike, 2nd-soonest expiry
- **Avg-ups**: +1.5% and +3.0% on option premium (stop-limit BUY, limit +$0.02 above trigger)
- **Stop**: −8% on avg_cost (market stop — recalculated after each avg-up)
- **Target**: +6% on initial entry (LIMIT sell — locked, qty grows as avg-ups fill)
- **Time exit**: 30 min after entry
- **Active hours**: 10:00–15:45 ET (7:00 AM – 12:45 PM PT)
- **Last entry**: 15:15 ET (12:15 PT) — every accepted trade gets the full 30-min runway
- **One trade at a time**: state machine forbids overlap

Backtested over 60 days: ~$868 P&L, 60% win rate, +$8.27 avg per trade. Both months profitable.

---

## ⚙️ Pre-flight checks (do once before first run)

```bash
cd /Users/manalithakkar/Documents/alpacabot

# 1. Verify PAPER mode is on (refuses to run otherwise)
grep "^PAPER" config.py
# expect: PAPER = True

# 2. Run dry-run test of state machine
python3 test_step7_dryrun.py
# expect: 🎉 ALL DRY-RUN SCENARIOS PASSED

# 3. Verify scripts are executable
ls -la run_bot.sh stop_bot.sh
# expect: -rwxr-xr-x ...
```

---

## 🌅 First day — manual operation (Wed)

Run these in a Terminal window. Do **not** start cron yet — get one supervised session under your belt first.

### 6:30 AM PT — validate order plumbing

```bash
cd /Users/manalithakkar/Documents/alpacabot
python3 test_step6.py
```

This places a real BUY then SELL on 1 contract. Expected outcome: small loss ($5–15) from the bid/ask spread. **If this fails, do not start the bot** — fix order plumbing first.

### 6:55 AM PT — start the bot manually

```bash
./run_bot.sh
```

The script:
- Refuses to start if a bot.pid already exists with a live process
- Writes `bot.pid`
- Logs to `logs/bot_YYYYMMDD.log`

### Monitor

In a separate Terminal pane:

```bash
tail -f /Users/manalithakkar/Documents/alpacabot/logs/bot_$(date +%Y%m%d).log
```

What to look for:

| Log line | What it means |
|---|---|
| `⏸ outside hours (HH:MM:SS)` | Before 10:00 ET — normal pre-market |
| `IDLE  SPY=... signal=none` | Inside active hours, no signal yet |
| `🚨 SIGNAL: CALL @ SPY=...` | A trade is about to be entered |
| `📥 BUY submitted  id=...` | Market BUY placed |
| `✅ ENTRY FILLED @ $X.XX` | Entry filled |
| `📋 Standing orders submitted: ...` | All 4 management orders placed |
| `📈 AVG_UP_1 FILLED @ ...` | Avg-up #1 fired |
| `🎯 TARGET FILLED @ ...` | Winner closed |
| `🛑 STOP FILLED @ ...` | Loser closed |
| `⏰ TIME_EXIT after 30.0 min` | 30-min timer fired |
| `🕞 EOD force-close (now 15:45:xx)` | End-of-day force-close |
| `✅ trade closed: ... P&L=$+X.XX` | Trade summary |

### 12:45 PM PT — bot does its own EOD close

At 15:45 ET, any open trade is force-closed. The bot then logs `⏸ outside hours` every 30s until you stop it.

### 1:00 PM PT — stop the bot manually

```bash
./stop_bot.sh
```

Sends SIGTERM, waits up to 90s for graceful shutdown, falls back to SIGKILL only if necessary. Removes `bot.pid`.

### After-action review

```bash
# Full day's log
less logs/bot_$(date +%Y%m%d).log

# Or just the trade summaries
grep "trade closed" logs/bot_$(date +%Y%m%d).log
```

Compare actual P&L to backtest expectations:
- ~1–3 actionable trades per day
- ~60% win rate over 60 days (any single day can vary widely)

---

## 🔁 Thursday onwards — cron automation

Once you've supervised at least one good session, automate with cron.

### Install (one time)

Open your crontab:

```bash
crontab -e
```

Paste these two lines (Mon–Fri):

```cron
# SPY bot — start 5 min before active window (6:55 AM PT = 9:55 AM ET)
55  6  *  *  1-5  /Users/manalithakkar/Documents/alpacabot/run_bot.sh   >> /Users/manalithakkar/Documents/alpacabot/logs/cron.log 2>&1

# SPY bot — stop 15 min after EOD force-close (1:00 PM PT = 4:00 PM ET)
 0 13  *  *  1-5  /Users/manalithakkar/Documents/alpacabot/stop_bot.sh  >> /Users/manalithakkar/Documents/alpacabot/logs/cron.log 2>&1
```

Save and exit (`:wq` in vim).

### Verify cron installed

```bash
crontab -l
```

Should show your two lines. cron does NOT need a restart — it picks up changes automatically.

### Verify cron triggered after first auto-start (Thursday morning)

```bash
# After 6:56 AM PT Thursday:
ls -la /Users/manalithakkar/Documents/alpacabot/bot.pid
# expect: file exists, recently modified

tail /Users/manalithakkar/Documents/alpacabot/logs/cron.log
# expect: "Bot started with PID NNNN"
```

### Disable cron temporarily (e.g., for a market holiday you want to skip)

```bash
# Comment out both lines, save:
crontab -e

# Or remove all cron entries (nuclear option):
crontab -r
```

Holidays: cron will still trigger M-F. The bot's freshness check (data > 5 min old) will refuse to trade if market is closed, so it's safe — just noisy in the logs. To suppress entirely, comment out cron the night before.

---

## 📊 Monitoring during the trading day

### Live tail
```bash
tail -f /Users/manalithakkar/Documents/alpacabot/logs/bot_$(date +%Y%m%d).log
```

### Check current position via Alpaca dashboard
[https://app.alpaca.markets/paper](https://app.alpaca.markets/paper) → Positions tab

### Check via CLI (programmatically)
```bash
python3 -c "
from orders import list_positions, list_open_orders
print('Positions:')
for p in list_positions(): print(f'  {p.symbol} qty={p.qty} mv=\${float(p.market_value):.2f}')
print('Open orders:')
for o in list_open_orders(): print(f'  {o.symbol} {o.side} {o.order_type} status={o.status}')
"
```

---

## 🚨 Troubleshooting

### Bot won't start: "Bot already running with PID …"

Either it really is running, or there's a stale pid file. Check:

```bash
ps -p $(cat /Users/manalithakkar/Documents/alpacabot/bot.pid)
```

- If a python process is shown → bot IS running. Use `./stop_bot.sh` to stop it.
- If "no such process" → stale pid file. Remove it:
  ```bash
  rm /Users/manalithakkar/Documents/alpacabot/bot.pid
  ```

### Bot crashed mid-trade

If the bot dies hard while a position is open, the 4 standing orders are still at Alpaca. The stop and target will still protect the position. To clean up:

```bash
# 1. Check what's open
python3 -c "
from orders import list_positions, list_open_orders, cancel_all_orders, close_any_position
print('Positions:', [p.symbol for p in list_positions()])
print('Open orders:', [(o.symbol, o.side, o.order_type) for o in list_open_orders()])
"

# 2. Cancel orders for a specific symbol (replace SYMBOL)
python3 -c "from orders import cancel_all_orders; print(cancel_all_orders('SPY260528C00750000'))"

# 3. Close the position
python3 -c "from orders import close_any_position; print(close_any_position('SPY260528C00750000'))"
```

Then you can `./run_bot.sh` again. The bot's startup checks will refuse to start if any SPY-option position or order remains.

### "Refusing to start — existing SPY option position(s)"

Carry-over from a previous run. Either close the position manually (Alpaca dashboard or the script above) or wait until you've actually exited it.

### Logs show signals but no entries

Check the time. If past 15:15 ET (12:15 PT), the wind-down rule kicks in — signals are logged but not acted on. This is by design.

### Spread too wide

Log will show `⚠ spread $0.XX > $0.50 — skipping entry`. Liquidity issue on that specific contract; bot waits for the next signal. Tune `MAX_SPREAD` in `bot.py` if this fires too often.

### Order didn't fill in 30s

Log will show `❌ entry order didn't fill in 30s`. Market order should always fill on liquid SPY options — investigate the bid/ask spread, market state, or Alpaca status.

---

## 🛑 Stopping during a trading session

```bash
./stop_bot.sh
```

**Note**: this stops the polling loop but does NOT close open positions. If you need to close a position immediately, use the Alpaca dashboard or the cleanup commands above.

---

## 📁 File reference

| File | Purpose |
|---|---|
| `config.py` | All strategy params (RSI, BB, target %, etc.) |
| `data.py` | SPY bar fetcher |
| `indicators.py` | BB + RSI math |
| `signals.py` | Mean-reversion signal detector |
| `options.py` | Option chain → contract resolution + quote |
| `orders.py` | Alpaca order submission/management |
| `trade_manager.py` | IN_TRADE state machine |
| `bot.py` | Main runner — start with `./run_bot.sh` |
| `run_bot.sh` | Launcher: starts `bot.py` in background, writes `bot.pid` |
| `stop_bot.sh` | Stopper: SIGTERM → wait 90s → SIGKILL fallback |
| `logs/bot_YYYYMMDD.log` | Daily bot log |
| `logs/cron.log` | cron job output |
| `bot.pid` | PID of running bot (auto-managed) |
| `backtest*.py` | Historical analysis scripts (not used by live bot) |

---

## 🔒 Safety rails baked in

| Guard | Behavior |
|---|---|
| `PAPER=False` check | Bot refuses to start |
| Existing SPY position | Bot refuses to start |
| Existing SPY option order | Bot refuses to start |
| Data > 5 min stale | Cycle skipped (handles holidays/closures) |
| Spread > $0.50 | Entry skipped |
| One-trade-at-a-time | State machine enforced |
| Last entry at 15:15 ET | Every trade gets full 30 min |
| 30-min time exit | Hard timer per trade |
| EOD force-close at 15:45 ET | Anything open gets market-sold |
| SIGTERM grace | Finishes current cycle before exit |

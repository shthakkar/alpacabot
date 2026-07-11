# AlpacaBot 5m-5m CALL

A **live trading bot** for SPY 0–2 DTE options. Polls every 30 seconds, detects a simple market-opening signal, and trades via the Alpaca API.

## The 5-Minute 5-Minute CALL Strategy

### Signal
- **Every trading day** at market open (9:30 AM ET), the bot fetches the opening 5-minute candle.
- **If the candle closed higher than it opened** (green candle) → **trigger a CALL trade**.
- If red → exit, no trade today.

### Position Sizing (Extreme Back-Loading)
The bot uses a **1-X-X sizing model** to minimize entry risk while maximizing avg-up upside:
- **Entry:** Always 1 contract (minimal risk exposure)
- **Avg-ups allowed:** Up to 2 additional 1-contract buys (contingent on position size)
- **Safety:** Never risk more than 50% of buying power per trade

### Entry Order
- Market BUY at the bid-ask midpoint or better
- 30-second fill timeout; abort if unfilled

### Stop Loss
- **Price:** Average cost × 0.912 (−8.8% loss cutoff)
- **Type:** Standing market stop at Alpaca (live protection if bot crashes)
- **Backup poll:** Every 30s, if bid ≤ stop price → force-sell

### Average-Ups (Pyramiding Up)
Two optional levels, triggered **poll-based** (every 30s if bid meets the threshold):

| Avg-Up | Trigger | Action |
|--------|---------|--------|
| #1 | Bid ≥ entry × 1.015 (+1.5%) | Market BUY 1 contract |
| #2 | Bid ≥ entry × 1.030 (+3.0%) | Market BUY 1 contract |

After each avg-up fill:
- Recalculate average cost across all fills
- Re-set stop loss to new avg cost × 0.912
- Update stop order qty to reflect new contract count

**Win rate:** ~88.9% on avg-ups (empirically measured).

### Profit Targets
Two layers:
1. **Poll target (primary):** Every 30s, if bid ≥ initial entry × 1.06 (+6%) → **force-sell at market**
2. **Time exit:** After 15 minutes in trade → force-sell at market
3. **EOD exit:** At 15:45 ET (30-min buffer before close) → force-sell at market (overnight carry forbidden)

### Example Trade Flow
```
09:30 AM   9:30 candle GREEN → resolve contract, market BUY 1 call @ $2.50
09:30     Standing orders: stop @ $2.28 (−8.8%)
09:31     Bid reaches $2.54 (+1.5%) → avg-up #1 fills 1 contract @ $2.52
09:31     New avg = $2.51, stop re-set to $2.29
09:32     Bid reaches $2.58 (+3.0% from entry) → avg-up #2 fills 1 contract @ $2.57
09:32     Final avg = $2.53, stop re-set to $2.31, now holding 3 contracts
09:33     Bid reaches $2.65 (+6.0% from entry) → force-sell 3 contracts @ market
          Exit: $2.65, avg cost $2.53 → P&L = +$360 (3 × $12 × 100)
```

---

## Setup

### Prerequisites
- Python 3.8+
- Alpaca API account with **options level 2+** enabled
- Buying power for options trading

### Installation

1. **Clone & navigate:**
   ```bash
   cd alpacabot-5m5mcall
   ```

2. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

3. **Set up credentials:**
   ```bash
   cp .env.example .env
   # Edit .env with your Alpaca API keys
   ```

4. **Verify setup:**
   ```bash
   python3 -c "from config import API_KEY, API_SECRET; print('✅ Credentials loaded')"
   ```

---

## Running

### Start the bot
```bash
bash run_5m5mcall.sh
```
- Waits 5 seconds for IEX to publish the 9:30 candle
- Saves stdout to `logs/bot_YYYYMMDD.log`
- Logs events (entry, fills, exits) to `logs/events_YYYYMMDD.log`

### Stop the bot
```bash
bash stop_5m5mcall.sh
```
- Sends SIGTERM to the running bot process
- Bot closes any open position cleanly

### Check logs
```bash
tail -f logs/events_*.log
```

---

## Configuration

Edit `config.py` to tune:

| Parameter | Default | Purpose |
|-----------|---------|---------|
| `PAPER` | `true` | Paper trading (set `false` for live) |
| `TIME_EXIT_MIN` | 15 | Exit minutes in trade |
| `AVG_UP_1_PCT` | 0.015 | Avg-up #1 trigger (+1.5%) |
| `AVG_UP_2_PCT` | 0.030 | Avg-up #2 trigger (+3.0%) |
| `STOP_PCT` | 0.088 | Stop loss (−8.8%) |
| `TARGET_PCT` | 0.060 | Profit target (+6%) |
| `MAX_SPREAD` | 0.50 | Skip entry if spread > $0.50 |
| `OPTION_CHAIN_DAYS` | 7 | Chain window (pick 2nd-soonest expiry) |

---

## Files

| File | Purpose |
|------|---------|
| `bot.py` | Main loop, signal detection, entry |
| `trade_manager.py` | Position lifecycle: stops, avg-ups, exits |
| `orders.py` | Alpaca API order submission + polling |
| `options.py` | Contract resolution + bid/ask quotes |
| `data.py` | Fetch 9:30 ET candle with retry |
| `config.py` | Strategy knobs & credentials |
| `logs/` | Event logs (auto-created) |

---

## Notes

- **One trade per day:** The bot triggers on the 9:30 candle only, then exits (sleep/crash-safe)
- **No bracket orders:** Alpaca rejects complex orders; we use polling + standing stop instead
- **Wash-trade safe:** Stop is submitted before avg-up BUYs to avoid Alpaca's "wash trade" rejection
- **Overnight:** EOD exits at 15:45 ET; no position carry-over

---

## Monitoring

### In paper trading
- Use Alpaca dashboard to verify orders, fills, and position P&L
- Check `logs/events_*.log` for real-time trade flow

### In live trading
- Keep bot running in a reliable environment (server, VPS, or Mac with caffeinate)
- Monitor logs daily for fills, stops, and exits
- Alert if bot crashes (missing heartbeat logs)

---

## Troubleshooting

| Issue | Fix |
|-------|-----|
| "Missing Alpaca credentials" | Check `.env` has `ALPACA_API_KEY` and `ALPACA_API_SECRET` |
| "9:30 candle not available" | IEX feed may be slow; check market is open |
| "Options not enabled" | Upgrade account to options level 2+ on Alpaca |
| "No future expirations in chain" | Increase `OPTION_CHAIN_DAYS` in `config.py` |
| Position not closed | Check for stale orders at Alpaca dashboard; manually cancel & close if needed |

---

## License

Use at your own risk. This is for educational/research purposes.

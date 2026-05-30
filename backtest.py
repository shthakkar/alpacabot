# ================================================================
# BACKTEST — replay last N trading days, simulate strategy
# Step 6.5 (analytical only — no orders sent)
#
# Output: backtest_trades.csv (one row per simulated trade)
# Use:    python3 backtest.py        (writes CSV)
#         python3 backtest_report.py (renders HTML from CSV)
#
# Strategy logic (mirrors live bot per spec):
#   • Signal: close > upper BB + RSI > 65 → PUT
#             close < lower BB + RSI < 35 → CALL
#   • Entry market BUY (assumed fill at next 1-min bar OPEN)
#   • Avg-up #1 at +1.5% of avg cost, #2 at +3% (stop-limit BUY)
#   • Stop loss = MARKET stop at -8% of running avg
#   • Profit target = LIMIT sell at initial-entry × 1.06 (LOCKED)
#   • 30-min max time in trade
#   • IDLE/IN_TRADE state: signals ignored while a trade is open
# ================================================================
import argparse
import csv
import math
import sys
import time
from datetime import datetime, date, timedelta, timezone

import pandas as pd

from alpaca.data.historical          import StockHistoricalDataClient
from alpaca.data.historical.option   import OptionHistoricalDataClient
from alpaca.data.requests            import StockBarsRequest, OptionBarsRequest
from alpaca.data.timeframe           import TimeFrame, TimeFrameUnit

from config     import (API_KEY, API_SECRET, ET, BB_PERIOD, BB_STD,
                         RSI_PERIOD, RSI_OB, RSI_OS,
                         AVG_UP_1_PCT, AVG_UP_2_PCT, STOP_PCT, TARGET_PCT,
                         TIME_EXIT_MIN)
from indicators import add_indicators

# ---------- backtest knobs ----------
LOOKBACK_DAYS      = 30                  # trading days to simulate
TRADE_WINDOW_MIN   = TIME_EXIT_MIN       # 30
SESSION_START_ET   = (10, 0)             # 10:00 ET — skip first 30 min
SESSION_END_ET     = (15, 45)            # 15:45 ET — leave buffer before close
AVG_UP_LIMIT_OFFSET = 0.02               # $0.02 above trigger for stop-limit BUY

# Module-level (overridable via CLI in main()):
CSV_PATH        = "backtest_trades.csv"
SIGNAL_INVERSE  = False     # if True, breakout → CALL, breakdown → PUT (momentum)
RSI_OB_OVERRIDE = None
RSI_OS_OVERRIDE = None


def _ob():  return RSI_OB_OVERRIDE if RSI_OB_OVERRIDE is not None else RSI_OB
def _os():  return RSI_OS_OVERRIDE if RSI_OS_OVERRIDE is not None else RSI_OS

# US market holidays in 2026 (only the ones inside our 30-day window matter)
MARKET_HOLIDAYS = {
    date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16), date(2026, 4, 3),
    date(2026, 5, 25), date(2026, 6, 19), date(2026, 7, 3), date(2026, 9, 7),
    date(2026, 11, 26), date(2026, 12, 25),
}

# Clients (re-use)
_stock_client  = StockHistoricalDataClient(API_KEY, API_SECRET)
_option_client = OptionHistoricalDataClient(API_KEY, API_SECRET)


# ----------------------------------------------------------------
# Calendar helpers (deterministic, no API needed)
# ----------------------------------------------------------------
def is_trading_day(d: date) -> bool:
    return d.weekday() < 5 and d not in MARKET_HOLIDAYS


def trading_days_back(end: date, n: int) -> list:
    out = []
    cursor = end
    while len(out) < n:
        cursor -= timedelta(days=1)
        if is_trading_day(cursor):
            out.append(cursor)
    return sorted(out)


def nth_trading_day_after(d: date, n: int) -> date:
    cursor = d
    found  = 0
    while found < n:
        cursor += timedelta(days=1)
        if is_trading_day(cursor):
            found += 1
    return cursor


# ----------------------------------------------------------------
# Option symbol helpers
# ----------------------------------------------------------------
def occ_symbol(expiry: date, direction: str, strike: int) -> str:
    cp = "C" if direction == "CALL" else "P"
    return f"SPY{expiry.strftime('%y%m%d')}{cp}{strike * 1000:08d}"


def otm_strike(direction: str, spy_price: float) -> int:
    return int(math.ceil(spy_price)) if direction == "CALL" else int(math.floor(spy_price))


# ----------------------------------------------------------------
# Data fetchers
# ----------------------------------------------------------------
def fetch_spy_day(day: date) -> pd.DataFrame:
    """All 5-min SPY bars for one trading day (ET-naive datetimes)."""
    start = datetime.combine(day, datetime.min.time(), tzinfo=ET).astimezone(timezone.utc)
    end   = start + timedelta(days=1)
    req = StockBarsRequest(
        symbol_or_symbols="SPY",
        timeframe=TimeFrame(5, TimeFrameUnit.Minute),
        start=start, end=end, feed="iex",
    )
    bars = _stock_client.get_stock_bars(req).df
    if isinstance(bars.index, pd.MultiIndex):
        bars = bars.xs("SPY", level="symbol")
    bars = bars.sort_index()
    bars.index = bars.index.tz_convert(ET)
    return bars


def fetch_option_minutes(symbol: str, start_et: datetime, end_et: datetime) -> pd.DataFrame:
    """Return 1-min option bars between start_et and end_et (ET datetimes)."""
    req = OptionBarsRequest(
        symbol_or_symbols=symbol,
        timeframe=TimeFrame(1, TimeFrameUnit.Minute),
        start=start_et.astimezone(timezone.utc),
        end  =end_et.astimezone(timezone.utc),
    )
    bars = _option_client.get_option_bars(req).df
    if bars.empty:
        return bars
    if isinstance(bars.index, pd.MultiIndex):
        bars = bars.xs(symbol, level="symbol")
    bars = bars.sort_index()
    bars.index = bars.index.tz_convert(ET)
    return bars


# ----------------------------------------------------------------
# Trade simulation
# ----------------------------------------------------------------
def simulate_trade(signal_time, direction, spy_price, day):
    """
    Simulate one trade. Returns dict with all the fields we want to log,
    or None if option data isn't available.

    Mechanics:
      • Entry fill   = first option bar's OPEN at or after signal_time
      • Each minute, check that bar's HIGH and LOW for trigger hits:
          - LOW  ≤ stop      → stop fires (sell all)
          - HIGH ≥ target    → target fires (sell all)
          - HIGH ≥ avg-up_n  → avg-up fires (buy 1 more)
      • Within a single minute, conservative ordering:
          avg-ups (if hit) → then stop (if hit) → then target (if hit)
      • Run for up to 30 minutes, else time-exit at last bar's close
    """
    expiry = nth_trading_day_after(day, 2)
    strike = otm_strike(direction, spy_price)
    symbol = occ_symbol(expiry, direction, strike)

    # IMPORTANT: 5-min SPY bar timestamps are bar-START. The bar's "close"
    # is the price at bar-end (~5 min later). The live bot would detect
    # the signal AFTER the bar closes, so realistic entry is at
    # signal_time + 5 min (open of the next 5-min window). Anything
    # earlier is look-ahead bias.
    entry_time = signal_time + timedelta(minutes=5)

    # Need bars from entry_time → entry_time + 32 min (small buffer)
    end_et = entry_time + timedelta(minutes=TRADE_WINDOW_MIN + 2)
    bars   = fetch_option_minutes(symbol, entry_time, end_et)
    if bars.empty:
        return {"status": "NODATA", "signal_time": signal_time,
                "direction": direction, "symbol": symbol}

    # Entry = first bar's OPEN (the price right after the signal bar closed)
    entry_bar   = bars.iloc[0]
    entry_price = float(entry_bar["open"])

    fills        = [entry_price]
    avg_cost     = entry_price
    initial_avg  = entry_price                        # locked for target
    target_price = initial_avg * (1 + TARGET_PCT)
    stop_price   = avg_cost * (1 - STOP_PCT)
    avg_up_1_trg = avg_cost * (1 + AVG_UP_1_PCT)
    avg_up_2_trg = avg_cost * (1 + AVG_UP_2_PCT)
    avg_up_1_lim = avg_up_1_trg + AVG_UP_LIMIT_OFFSET
    avg_up_2_lim = avg_up_2_trg + AVG_UP_LIMIT_OFFSET
    avg_up_1_done = False
    avg_up_2_done = False

    exit_reason  = None
    exit_price   = None
    exit_time    = None

    # Iterate minute bars — time elapsed measured from ENTRY, not signal
    for ts, bar in bars.iterrows():
        if (ts - entry_time).total_seconds() / 60 >= TRADE_WINDOW_MIN:
            # Time exit — sell at this bar's open
            exit_reason = "TIME"
            exit_price  = float(bar["open"])
            exit_time   = ts
            break

        high = float(bar["high"])
        low  = float(bar["low"])

        # 1. Avg-up #1
        if not avg_up_1_done and high >= avg_up_1_trg:
            fill = avg_up_1_lim  # assume stop-limit fills at the limit
            fills.append(fill)
            avg_up_1_done = True
            avg_cost     = sum(fills) / len(fills)
            stop_price   = avg_cost * (1 - STOP_PCT)
            # target stays the same (locked)

        # 2. Avg-up #2 (only after #1)
        if avg_up_1_done and not avg_up_2_done and high >= avg_up_2_trg:
            fill = avg_up_2_lim
            fills.append(fill)
            avg_up_2_done = True
            avg_cost     = sum(fills) / len(fills)
            stop_price   = avg_cost * (1 - STOP_PCT)

        # 3. Stop loss (after avg-ups so they fill at the high before stop fires)
        if low <= stop_price:
            exit_reason = "STOP"
            exit_price  = stop_price       # market stop fills at trigger (no slippage assumption)
            exit_time   = ts
            break

        # 4. Profit target
        if high >= target_price:
            exit_reason = "TARGET"
            exit_price  = target_price
            exit_time   = ts
            break

    # Loop exhausted without hit → time exit at last bar's close
    if exit_reason is None:
        last_bar    = bars.iloc[-1]
        exit_reason = "TIME"
        exit_price  = float(last_bar["close"])
        exit_time   = bars.index[-1]

    contracts = len(fills)
    pnl_per_share = sum(exit_price - f for f in fills)
    pnl_dollars   = pnl_per_share * 100   # 1 contract = 100 shares

    return {
        "status":         "OK",
        "day":            day.isoformat(),
        "signal_time":    signal_time.isoformat(),
        "direction":      direction,
        "spy_at_signal":  spy_price,
        "symbol":         symbol,
        "expiry":         expiry.isoformat(),
        "strike":         strike,
        "entry_price":    entry_price,
        "avg_up_1_price": fills[1] if contracts >= 2 else None,
        "avg_up_2_price": fills[2] if contracts >= 3 else None,
        "final_avg_cost": round(avg_cost, 4),
        "stop_at_exit":   round(stop_price, 4),
        "target":         round(target_price, 4),
        "exit_reason":    exit_reason,
        "exit_price":     round(exit_price, 4),
        "exit_time":      exit_time.isoformat() if exit_time is not None else None,
        "contracts":      contracts,
        "pnl_per_share":  round(pnl_per_share, 4),
        "pnl_dollars":    round(pnl_dollars, 2),
    }


# ----------------------------------------------------------------
# Per-day walker (live-bot replay)
# ----------------------------------------------------------------
def backtest_day(day: date) -> list:
    print(f"  → {day.isoformat()}", flush=True)
    try:
        bars = fetch_spy_day(day)
    except Exception as e:
        print(f"      ⚠ couldn't fetch SPY bars: {e}", flush=True)
        return []

    if bars.empty:
        print(f"      ⚠ no SPY bars for {day}", flush=True)
        return []

    # Compute indicators across the whole day's bars
    bars = add_indicators(bars)

    # Walk forward — only inside active hours
    session_start = bars.index[0].replace(hour=SESSION_START_ET[0], minute=SESSION_START_ET[1])
    session_end   = bars.index[0].replace(hour=SESSION_END_ET[0],   minute=SESSION_END_ET[1])
    active = bars[(bars.index >= session_start) & (bars.index < session_end)]

    results = []
    in_trade_until = None
    for ts, row in active.iterrows():
        if in_trade_until is not None and ts < in_trade_until:
            continue                       # still IN_TRADE — ignore signals
        if pd.isna(row.get("bb_upper")) or pd.isna(row.get("rsi")):
            continue                       # warmup

        close = row["close"]
        rsi   = row["rsi"]
        upper = row["bb_upper"]
        lower = row["bb_lower"]

        direction = None
        if close > upper and rsi > _ob():
            direction = "CALL" if SIGNAL_INVERSE else "PUT"
        elif close < lower and rsi < _os():
            direction = "PUT"  if SIGNAL_INVERSE else "CALL"

        if direction is None:
            continue

        result = simulate_trade(ts, direction, float(close), day)
        if result is None:
            continue

        if result.get("status") == "OK":
            in_trade_until = ts + timedelta(minutes=TRADE_WINDOW_MIN)
            print(f"     {ts.strftime('%H:%M')} {direction}  "
                  f"{result['contracts']}c  {result['exit_reason']}  "
                  f"P&L=${result['pnl_dollars']:+.2f}",
                  flush=True)
        else:
            print(f"     {ts.strftime('%H:%M')} {direction} — NODATA "
                  f"({result.get('symbol')})", flush=True)
        results.append(result)
        # mild rate-limit padding
        time.sleep(0.05)

    return results


# ----------------------------------------------------------------
# Entry point
# ----------------------------------------------------------------
def main():
    global CSV_PATH, SIGNAL_INVERSE, RSI_OB_OVERRIDE, RSI_OS_OVERRIDE

    ap = argparse.ArgumentParser()
    ap.add_argument("--csv",     default=CSV_PATH, help="output CSV path")
    ap.add_argument("--inverse", action="store_true",
                    help="flip signal direction (momentum mode)")
    ap.add_argument("--rsi-ob",  type=float, default=None, help="override RSI overbought threshold")
    ap.add_argument("--rsi-os",  type=float, default=None, help="override RSI oversold threshold")
    args = ap.parse_args()

    CSV_PATH         = args.csv
    SIGNAL_INVERSE   = args.inverse
    RSI_OB_OVERRIDE  = args.rsi_ob
    RSI_OS_OVERRIDE  = args.rsi_os

    mode = "MOMENTUM (inverse)" if SIGNAL_INVERSE else "MEAN REVERSION"
    print(f"Mode: {mode}", flush=True)
    print(f"RSI: OB={_ob()}  OS={_os()}", flush=True)

    today = datetime.now(ET).date()
    days  = trading_days_back(today, LOOKBACK_DAYS)

    print(f"Backtesting {len(days)} trading days: {days[0]} → {days[-1]}", flush=True)
    print(f"Writing CSV → {CSV_PATH}\n", flush=True)

    fieldnames = [
        "day", "signal_time", "direction", "spy_at_signal", "symbol",
        "expiry", "strike", "entry_price",
        "avg_up_1_price", "avg_up_2_price",
        "final_avg_cost", "stop_at_exit", "target",
        "exit_reason", "exit_price", "exit_time",
        "contracts", "pnl_per_share", "pnl_dollars", "status",
    ]
    with open(CSV_PATH, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for day in days:
            for r in backtest_day(day):
                row = {k: r.get(k) for k in fieldnames}
                w.writerow(row)
                f.flush()

    print(f"\n✅ Done. Open backtest_trades.csv or run backtest_report.py", flush=True)


if __name__ == "__main__":
    main()

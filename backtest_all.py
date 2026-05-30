# ================================================================
# BACKTEST ALL — run 6 variants in one process with disk caching
# Produces a single comparison HTML for fast side-by-side reading.
#
# Variants:
#   1. Mean Reversion — BB + RSI (current strategy)
#   2. Momentum       — BB + RSI
#   3. Mean Reversion — RSI only
#   4. Momentum       — RSI only
#   5. Mean Reversion — BB only
#   6. Momentum       — BB only
#
# Caching: SPY day bars + option contract minute bars are pickled to
# .cache/ — re-runs (after the first) are near-instant. Safe to delete
# .cache/ to force a fresh pull from Alpaca.
#
# Run:
#   python3 backtest_all.py
# Opens backtest_compare.html when done.
# ================================================================
import argparse
import csv
import math
import os
import pickle
import time
import webbrowser
from datetime   import datetime, date, timedelta, timezone
from pathlib    import Path

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
from backtest   import (is_trading_day, trading_days_back,
                         nth_trading_day_after, occ_symbol, otm_strike,
                         LOOKBACK_DAYS, TRADE_WINDOW_MIN,
                         SESSION_START_ET, SESSION_END_ET,
                         AVG_UP_LIMIT_OFFSET)

# ----------------------------------------------------------------
# Disk cache (pickled DataFrames)
# ----------------------------------------------------------------
CACHE_DIR = Path(".cache")
CACHE_DIR.mkdir(exist_ok=True)


def _cache_get(key):
    p = CACHE_DIR / f"{key}.pkl"
    if p.exists():
        try:
            return pickle.loads(p.read_bytes())
        except Exception:
            return None
    return None


def _cache_set(key, value):
    (CACHE_DIR / f"{key}.pkl").write_bytes(pickle.dumps(value))


# ----------------------------------------------------------------
# Cached fetchers
# ----------------------------------------------------------------
_stock_client  = StockHistoricalDataClient(API_KEY, API_SECRET)
_option_client = OptionHistoricalDataClient(API_KEY, API_SECRET)


def fetch_spy_day(day: date) -> pd.DataFrame:
    key = f"spy_{day.isoformat()}"
    cached = _cache_get(key)
    if cached is not None:
        return cached

    start = datetime.combine(day, datetime.min.time(), tzinfo=ET).astimezone(timezone.utc)
    end   = start + timedelta(days=1)
    req = StockBarsRequest(
        symbol_or_symbols="SPY",
        timeframe=TimeFrame(5, TimeFrameUnit.Minute),
        start=start, end=end, feed="iex",
    )
    df = _stock_client.get_stock_bars(req).df
    if isinstance(df.index, pd.MultiIndex):
        df = df.xs("SPY", level="symbol")
    df = df.sort_index()
    df.index = df.index.tz_convert(ET)

    _cache_set(key, df)
    return df


def fetch_option_window(symbol: str, entry_time, minutes: int) -> pd.DataFrame:
    """
    Fetch 1-min option bars for `minutes` minutes starting at entry_time.
    Cached by (symbol, entry_time, minutes).
    """
    key = f"opt_{symbol}_{entry_time.strftime('%Y%m%d%H%M')}_{minutes}"
    cached = _cache_get(key)
    if cached is not None:
        return cached

    end_et = entry_time + timedelta(minutes=minutes + 2)
    req = OptionBarsRequest(
        symbol_or_symbols=symbol,
        timeframe=TimeFrame(1, TimeFrameUnit.Minute),
        start=entry_time.astimezone(timezone.utc),
        end  =end_et.astimezone(timezone.utc),
    )
    df = _option_client.get_option_bars(req).df
    if not df.empty:
        if isinstance(df.index, pd.MultiIndex):
            df = df.xs(symbol, level="symbol")
        df = df.sort_index()
        df.index = df.index.tz_convert(ET)

    _cache_set(key, df)
    return df


# ----------------------------------------------------------------
# Signal detection — supports 3 modes × 2 directions
# ----------------------------------------------------------------
SIGNAL_MODES   = ("bb_rsi", "rsi_only", "bb_only")
DIRECTION_MODES = ("meanrev", "momentum")


def detect_signal(row, signal_mode: str, direction_mode: str,
                  rsi_ob: float = RSI_OB, rsi_os: float = RSI_OS):
    close = row["close"]
    upper = row["bb_upper"]
    lower = row["bb_lower"]
    rsi   = row["rsi"]

    above_band = close > upper
    below_band = close < lower
    rsi_high   = rsi > rsi_ob
    rsi_low    = rsi < rsi_os

    if signal_mode == "bb_rsi":
        bullish_break = above_band and rsi_high
        bearish_break = below_band and rsi_low
    elif signal_mode == "rsi_only":
        bullish_break = rsi_high
        bearish_break = rsi_low
    elif signal_mode == "bb_only":
        bullish_break = above_band
        bearish_break = below_band
    else:
        raise ValueError(signal_mode)

    if direction_mode == "meanrev":
        if bullish_break: return "PUT"
        if bearish_break: return "CALL"
    else:                       # momentum
        if bullish_break: return "CALL"
        if bearish_break: return "PUT"
    return None


# ----------------------------------------------------------------
# Trade simulation (same mechanics as backtest.py, look-ahead fixed)
# ----------------------------------------------------------------
def simulate_trade(signal_time, direction, spy_price, day, target_pct=TARGET_PCT,
                   target_mode="initial"):
    """
    target_mode:
      "initial" — target locked at initial entry × (1+target_pct) (current design)
      "average" — target recomputed to avg_cost × (1+target_pct) after each avg-up
    """
    expiry = nth_trading_day_after(day, 2)
    strike = otm_strike(direction, spy_price)
    symbol = occ_symbol(expiry, direction, strike)

    entry_time = signal_time + timedelta(minutes=5)
    bars = fetch_option_window(symbol, entry_time, TRADE_WINDOW_MIN)
    if bars.empty:
        return {"status": "NODATA"}

    entry_bar   = bars.iloc[0]
    entry_price = float(entry_bar["open"])

    fills        = [entry_price]
    avg_cost     = entry_price
    initial_avg  = entry_price
    target_price = initial_avg * (1 + target_pct)
    stop_price   = avg_cost * (1 - STOP_PCT)
    avg_up_1_trg = avg_cost * (1 + AVG_UP_1_PCT)
    avg_up_2_trg = avg_cost * (1 + AVG_UP_2_PCT)
    avg_up_1_lim = avg_up_1_trg + AVG_UP_LIMIT_OFFSET
    avg_up_2_lim = avg_up_2_trg + AVG_UP_LIMIT_OFFSET
    avg_up_1_done = False
    avg_up_2_done = False

    exit_reason = None
    exit_price  = None

    for ts, bar in bars.iterrows():
        if (ts - entry_time).total_seconds() / 60 >= TRADE_WINDOW_MIN:
            exit_reason = "TIME"
            exit_price  = float(bar["open"])
            break

        high = float(bar["high"]); low = float(bar["low"])

        if not avg_up_1_done and high >= avg_up_1_trg:
            fills.append(avg_up_1_lim)
            avg_up_1_done = True
            avg_cost   = sum(fills) / len(fills)
            stop_price = avg_cost * (1 - STOP_PCT)
            if target_mode == "average":
                target_price = avg_cost * (1 + target_pct)
        if avg_up_1_done and not avg_up_2_done and high >= avg_up_2_trg:
            fills.append(avg_up_2_lim)
            avg_up_2_done = True
            avg_cost   = sum(fills) / len(fills)
            stop_price = avg_cost * (1 - STOP_PCT)
            if target_mode == "average":
                target_price = avg_cost * (1 + target_pct)

        if low <= stop_price:
            exit_reason = "STOP"
            exit_price  = stop_price
            break
        if high >= target_price:
            exit_reason = "TARGET"
            exit_price  = target_price
            break

    if exit_reason is None:
        exit_reason = "TIME"
        exit_price  = float(bars.iloc[-1]["close"])

    contracts = len(fills)
    pnl       = sum(exit_price - f for f in fills) * 100
    return {
        "status": "OK",
        "contracts": contracts,
        "exit_reason": exit_reason,
        "pnl": pnl,
    }


# ----------------------------------------------------------------
# Run one variant — returns aggregated stats
# ----------------------------------------------------------------
def run_variant(label, signal_mode, direction_mode, days, day_bars_map,
                target_pct=TARGET_PCT, target_mode="initial"):
    print(f"\n→ {label}", flush=True)
    trades = []

    for day in days:
        bars = day_bars_map[day]
        if bars.empty:
            continue

        session_start = bars.index[0].replace(hour=SESSION_START_ET[0], minute=SESSION_START_ET[1])
        session_end   = bars.index[0].replace(hour=SESSION_END_ET[0],   minute=SESSION_END_ET[1])
        active = bars[(bars.index >= session_start) & (bars.index < session_end)]

        in_trade_until = None
        for ts, row in active.iterrows():
            if in_trade_until is not None and ts < in_trade_until:
                continue
            if pd.isna(row.get("bb_upper")) or pd.isna(row.get("rsi")):
                continue

            direction = detect_signal(row, signal_mode, direction_mode)
            if direction is None:
                continue

            result = simulate_trade(ts, direction, float(row["close"]), day,
                                     target_pct=target_pct, target_mode=target_mode)
            if result.get("status") != "OK":
                continue

            in_trade_until = ts + timedelta(minutes=TRADE_WINDOW_MIN)
            trades.append(result)

    return trades


def aggregate(trades):
    if not trades:
        return None
    pnls = [t["pnl"] for t in trades]
    wins = sum(1 for p in pnls if p > 0)
    case_counts = {}
    for t in trades:
        k = f"{t['contracts']}c+{t['exit_reason']}"
        case_counts[k] = case_counts.get(k, 0) + 1
    return {
        "trades":  len(trades),
        "wins":    wins,
        "win_rate": wins / len(trades),
        "total_pnl": sum(pnls),
        "avg_pnl":   sum(pnls) / len(pnls),
        "max_win":   max(pnls),
        "max_loss":  min(pnls),
        "case_3_target": case_counts.get("3c+TARGET", 0),
        "case_1_stop":   case_counts.get("1c+STOP", 0),
        "cases":     case_counts,
    }


# ----------------------------------------------------------------
# Two-period comparison HTML (this month vs last month)
# ----------------------------------------------------------------
def render_two_period(rows_by_period):
    def m(x):
        cls = "pos" if x > 0 else ("neg" if x < 0 else "")
        return f'<span class="{cls}">${x:+,.2f}</span>'

    def p(x):
        cls = "pos" if x >= 0.5 else "neg"
        return f'<span class="{cls}">{x*100:.1f}%</span>'

    period_names = list(rows_by_period.keys())
    p1, p2 = period_names[0], period_names[1]

    # Build a row-per-variant dict
    by_variant = {}
    for period_name, rows in rows_by_period.items():
        for r in rows:
            by_variant.setdefault(r["label"], {})[period_name] = r["stats"]

    def cell_stats(stats):
        if stats is None: return "<td>no data</td>" * 4
        return (f"<td>{stats['trades']}</td>"
                f"<td>{p(stats['win_rate'])}</td>"
                f"<td>{m(stats['total_pnl'])}</td>"
                f"<td>{m(stats['avg_pnl'])}</td>")

    def consistency(s1, s2):
        if s1 is None or s2 is None: return "—"
        same_sign = (s1["total_pnl"] > 0) == (s2["total_pnl"] > 0)
        both_pos  = s1["total_pnl"] > 0 and s2["total_pnl"] > 0
        if both_pos: return '<span class="pos">✓ Both profitable</span>'
        if same_sign and s1["total_pnl"] < 0: return '<span class="neg">✗ Both losing</span>'
        return '<span style="color:#888">~ Inconsistent</span>'

    body_rows = ""
    for label, periods in by_variant.items():
        s1 = periods.get(p1); s2 = periods.get(p2)
        body_rows += (
            f"<tr><td><b>{label}</b></td>"
            + cell_stats(s1) + cell_stats(s2)
            + f"<td>{consistency(s1, s2)}</td></tr>"
        )

    html = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>SPY Bot — Two-Period Comparison</title>
<style>
  body {{font-family:-apple-system,Helvetica,sans-serif;max-width:1400px;margin:30px auto;padding:0 20px;color:#222;}}
  h1 {{border-bottom:2px solid #333;padding-bottom:8px;}}
  table {{border-collapse:collapse;width:100%;margin-top:10px;font-size:13px;}}
  th,td {{padding:7px 10px;border-bottom:1px solid #e2e2e2;text-align:left;}}
  th {{background:#fafafa;font-weight:600;}}
  th.section1 {{background:#e8f4fd;}}
  th.section2 {{background:#f3e8fd;}}
  tr:hover {{background:#fbfbfd;}}
  .pos {{color:#1a8f3c;font-weight:600;}}
  .neg {{color:#cc2222;font-weight:600;}}
  .small {{font-size:12px;color:#777;}}
</style></head><body>

<h1>SPY Bot — This Month vs Last Month</h1>
<p class="small">Same 6 variants, two consecutive 30-trading-day windows. Look-ahead removed, cached data.
   The "Consistency" column is the headline: only edges that hold across regimes are real.</p>

<table>
  <tr>
    <th rowspan="2">Variant</th>
    <th colspan="4" class="section1">{p1}</th>
    <th colspan="4" class="section2">{p2}</th>
    <th rowspan="2">Consistency</th>
  </tr>
  <tr>
    <th class="section1">Trades</th><th class="section1">WR</th>
    <th class="section1">Total P&amp;L</th><th class="section1">Avg</th>
    <th class="section2">Trades</th><th class="section2">WR</th>
    <th class="section2">Total P&amp;L</th><th class="section2">Avg</th>
  </tr>
  {body_rows}
</table>

</body></html>"""
    return html


# ----------------------------------------------------------------
# Target mode comparison (initial-locked vs avg-recomputed)
# ----------------------------------------------------------------
def render_target_mode_compare(results):
    """
    results: { (period_label, target_mode): stats }
    """
    def m(x):
        cls = "pos" if x > 0 else ("neg" if x < 0 else "")
        return f'<span class="{cls}">${x:+,.2f}</span>'

    def p(x):
        cls = "pos" if x >= 0.5 else "neg"
        return f'<span class="{cls}">{x*100:.1f}%</span>'

    # Build a row per mode showing both periods + combined
    period_labels = sorted({pl for pl, _ in results.keys()}, reverse=True)
    p1, p2 = period_labels[0], period_labels[1]   # this month, last month

    mode_rows = ""
    summary = {}
    for mode in ("initial", "average"):
        s1 = results[(p1, mode)]
        s2 = results[(p2, mode)]
        combined = s1["total_pnl"] + s2["total_pnl"]
        avg = combined / (s1["trades"] + s2["trades"])
        summary[mode] = {"combined": combined, "avg": avg, "s1": s1, "s2": s2}

        case_cells = ""
        all_cases = sorted(set(s1["cases"]) | set(s2["cases"]))
        case_breakdown = "".join(
            f"<tr><td>{k}</td><td>{s1['cases'].get(k,0)}</td><td>{s2['cases'].get(k,0)}</td></tr>"
            for k in all_cases
        )

        label = f"Target = initial entry × 1.06 (locked)" if mode == "initial" \
                else f"Target = avg cost × 1.06 (recomputed each avg-up)"

        mode_rows += f"""
<h2>{label}</h2>
<table>
  <tr>
    <th></th>
    <th>{p1}</th>
    <th>{p2}</th>
    <th>Combined</th>
  </tr>
  <tr><td>Trades</td>
      <td>{s1['trades']}</td>
      <td>{s2['trades']}</td>
      <td>{s1['trades']+s2['trades']}</td></tr>
  <tr><td>Win rate</td>
      <td>{p(s1['win_rate'])}</td>
      <td>{p(s2['win_rate'])}</td>
      <td>{p((s1['wins']+s2['wins'])/(s1['trades']+s2['trades']))}</td></tr>
  <tr><td>Total P&amp;L</td>
      <td>{m(s1['total_pnl'])}</td>
      <td>{m(s2['total_pnl'])}</td>
      <td><b>{m(combined)}</b></td></tr>
  <tr><td>Avg / trade</td>
      <td>{m(s1['avg_pnl'])}</td>
      <td>{m(s2['avg_pnl'])}</td>
      <td>{m(avg)}</td></tr>
  <tr><td>Max win</td>
      <td>{m(s1['max_win'])}</td>
      <td>{m(s2['max_win'])}</td>
      <td>{m(max(s1['max_win'], s2['max_win']))}</td></tr>
  <tr><td>Max loss</td>
      <td>{m(s1['max_loss'])}</td>
      <td>{m(s2['max_loss'])}</td>
      <td>{m(min(s1['max_loss'], s2['max_loss']))}</td></tr>
</table>
<h4>Case breakdown</h4>
<table>
  <tr><th>Case</th><th>{p1}</th><th>{p2}</th></tr>
  {case_breakdown}
</table>
"""

    init = summary["initial"]
    avgm = summary["average"]
    winner = "initial" if init["combined"] > avgm["combined"] else "average"
    winner_label = "Target locked at initial entry" if winner == "initial" \
                   else "Target follows avg cost"

    delta = init["combined"] - avgm["combined"]

    html = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>SPY Bot — Target Mode Comparison</title>
<style>
  body {{font-family:-apple-system,Helvetica,sans-serif;max-width:1100px;margin:30px auto;padding:0 20px;color:#222;}}
  h1 {{border-bottom:2px solid #333;padding-bottom:8px;}}
  h2 {{margin-top:36px;background:#f5f7fa;padding:10px 14px;border-left:4px solid #4a90e2;}}
  table {{border-collapse:collapse;width:100%;margin-top:10px;font-size:14px;}}
  th,td {{padding:8px 12px;border-bottom:1px solid #e2e2e2;text-align:left;}}
  th {{background:#fafafa;font-weight:600;}}
  .pos {{color:#1a8f3c;font-weight:600;}}
  .neg {{color:#cc2222;font-weight:600;}}
  .verdict {{background:#fff7d6;padding:14px 18px;border-radius:8px;
              border-left:4px solid #d49b00;font-size:15px;margin:20px 0;}}
</style></head><body>

<h1>SPY Bot — Target on Initial vs Target on Average</h1>
<p>Same signal (Mean Rev BB+RSI 65/35), same stop (avg×0.92), same avg-up triggers.
   Only the target's reference price changes.</p>

<div class="verdict">
  Verdict: <b>{winner_label}</b> wins over 60 days, by {m(abs(delta))}.<br>
  &nbsp;&nbsp;Target locked at initial: <b>{m(init['combined'])}</b><br>
  &nbsp;&nbsp;Target on avg cost:        <b>{m(avgm['combined'])}</b>
</div>

{mode_rows}

</body></html>"""
    return html


# ----------------------------------------------------------------
# Two-period target-sweep HTML (variants × target % × 2 periods)
# ----------------------------------------------------------------
def render_target_sweep_two_periods(target_pcts, results_by_period_target, p1_label, p2_label):
    """
    results_by_period_target: { period_label: { target_pct: [rows] } }
    """
    def m(x):
        if x is None: return "—"
        cls = "pos" if x > 0 else ("neg" if x < 0 else "")
        return f'<span class="{cls}">${x:+,.2f}</span>'

    def p(x):
        if x is None: return "—"
        cls = "pos" if x >= 0.5 else "neg"
        return f'<span class="{cls}">{x*100:.1f}%</span>'

    # Find max combined P&L (best consistent) for highlight
    rows_data = []
    for label, signal_mode, direction_mode in VARIANTS:
        for t in target_pcts:
            s1 = next((r["stats"] for r in results_by_period_target[p1_label][t]
                       if r["label"] == label), None)
            s2 = next((r["stats"] for r in results_by_period_target[p2_label][t]
                       if r["label"] == label), None)
            combined = (s1["total_pnl"] if s1 else 0) + (s2["total_pnl"] if s2 else 0)
            both_pos = (s1 and s1["total_pnl"] > 0) and (s2 and s2["total_pnl"] > 0)
            rows_data.append({
                "label": label, "target": t,
                "s1": s1, "s2": s2,
                "combined": combined, "both_pos": both_pos,
            })

    # Star for the best "both positive" row
    best_both_pos = max(
        (r["combined"] for r in rows_data if r["both_pos"]),
        default=None,
    )

    body = ""
    last_label = None
    for r in rows_data:
        sep = ""
        if last_label is not None and last_label != r["label"]:
            sep = '<tr><td colspan="11" style="background:#fafafa;height:6px;padding:0;border:0;"></td></tr>'
        last_label = r["label"]

        is_star = (r["both_pos"] and best_both_pos is not None
                   and abs(r["combined"] - best_both_pos) < 0.01)
        consistency = ""
        if r["both_pos"]:
            consistency = '<span class="pos">✓ Both +</span>'
        elif r["s1"] and r["s2"] and r["s1"]["total_pnl"] < 0 and r["s2"]["total_pnl"] < 0:
            consistency = '<span class="neg">✗ Both −</span>'
        elif r["s1"] and r["s2"]:
            consistency = '<span style="color:#888">~ Mixed</span>'

        row_style = ' style="background:#fff7d6;font-weight:600;"' if is_star else ""

        s1, s2 = r["s1"], r["s2"]
        body += sep + (
            f'<tr{row_style}>'
            f'<td>{r["label"]}</td>'
            f'<td>{int(r["target"]*100)}%</td>'
            f'<td>{s1["trades"] if s1 else "—"}</td>'
            f'<td>{p(s1["win_rate"]) if s1 else "—"}</td>'
            f'<td>{m(s1["total_pnl"]) if s1 else "—"}</td>'
            f'<td>{s2["trades"] if s2 else "—"}</td>'
            f'<td>{p(s2["win_rate"]) if s2 else "—"}</td>'
            f'<td>{m(s2["total_pnl"]) if s2 else "—"}</td>'
            f'<td>{m(r["combined"])}</td>'
            f'<td>{consistency}</td>'
            '</tr>'
        )

    html = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>SPY Bot — Target Sweep Across Two Periods</title>
<style>
  body {{font-family:-apple-system,Helvetica,sans-serif;max-width:1500px;margin:30px auto;padding:0 20px;color:#222;}}
  h1 {{border-bottom:2px solid #333;padding-bottom:8px;}}
  table {{border-collapse:collapse;width:100%;margin-top:10px;font-size:13px;}}
  th,td {{padding:7px 9px;border-bottom:1px solid #e2e2e2;text-align:left;}}
  th {{background:#fafafa;font-weight:600;}}
  th.s1 {{background:#e8f4fd;}}
  th.s2 {{background:#f3e8fd;}}
  th.sum {{background:#fef5d6;}}
  tr:hover {{background:#fbfbfd;}}
  .pos {{color:#1a8f3c;font-weight:600;}}
  .neg {{color:#cc2222;font-weight:600;}}
  .small {{font-size:12px;color:#777;}}
</style></head><body>

<h1>SPY Bot — Target % Sweep Across Two Periods</h1>
<p class="small">6 variants × 3 target percentages × 2 periods = 36 backtests.
   Highlighted row = best combined P&amp;L among strategies that were profitable in BOTH months.</p>

<table>
  <tr>
    <th rowspan="2">Variant</th>
    <th rowspan="2">Target</th>
    <th colspan="3" class="s1">{p1_label}</th>
    <th colspan="3" class="s2">{p2_label}</th>
    <th rowspan="2" class="sum">Combined P&amp;L</th>
    <th rowspan="2">Consistency</th>
  </tr>
  <tr>
    <th class="s1">Trades</th><th class="s1">WR</th><th class="s1">P&amp;L</th>
    <th class="s2">Trades</th><th class="s2">WR</th><th class="s2">P&amp;L</th>
  </tr>
  {body}
</table>

</body></html>"""
    return html


# ----------------------------------------------------------------
# Target-sweep HTML (variants × target %)
# ----------------------------------------------------------------
def render_target_sweep(target_pcts, results_by_target):
    """results_by_target: { target_pct: [rows] }"""
    def m(x):
        cls = "pos" if x > 0 else ("neg" if x < 0 else "")
        return f'<span class="{cls}">${x:+,.2f}</span>'

    def p(x):
        cls = "pos" if x >= 0.5 else "neg"
        return f'<span class="{cls}">{x*100:.1f}%</span>'

    # Build flat row list: (variant_label, target, stats)
    flat = []
    for t in target_pcts:
        for r in results_by_target[t]:
            flat.append((r["label"], t, r["stats"]))

    # Find best total P&L for highlighting
    best_pnl = max((s["total_pnl"] for (_, _, s) in flat if s is not None), default=0)

    body_rows = ""
    last_variant = None
    for label, t, s in flat:
        is_best = s is not None and abs(s["total_pnl"] - best_pnl) < 0.01
        cls = ' style="background:#fff7d6;font-weight:600;"' if is_best else ""
        sep_row = ""
        if last_variant is not None and last_variant != label:
            sep_row = '<tr><td colspan="7" style="background:#fafafa;height:6px;padding:0;border:0;"></td></tr>'
        last_variant = label
        if s is None:
            body_rows += sep_row + f'<tr{cls}><td>{label}</td><td>{int(t*100)}%</td><td colspan="5">no data</td></tr>'
            continue
        body_rows += sep_row + (
            f'<tr{cls}>'
            f'<td>{label}</td>'
            f'<td>{int(t*100)}%</td>'
            f'<td>{s["trades"]}</td>'
            f'<td>{p(s["win_rate"])}</td>'
            f'<td>{m(s["total_pnl"])}</td>'
            f'<td>{m(s["avg_pnl"])}</td>'
            f'<td>{s["case_3_target"]} ({s["case_3_target"]/s["trades"]*100:.0f}%)</td>'
            '</tr>'
        )

    html = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>SPY Bot — Target % Sweep</title>
<style>
  body {{font-family:-apple-system,Helvetica,sans-serif;max-width:1200px;margin:30px auto;padding:0 20px;color:#222;}}
  h1 {{border-bottom:2px solid #333;padding-bottom:8px;}}
  table {{border-collapse:collapse;width:100%;margin-top:10px;font-size:14px;}}
  th,td {{padding:8px 10px;border-bottom:1px solid #e2e2e2;text-align:left;}}
  th {{background:#fafafa;font-weight:600;}}
  tr:hover {{background:#fbfbfd;}}
  .pos {{color:#1a8f3c;font-weight:600;}}
  .neg {{color:#cc2222;font-weight:600;}}
  .small {{font-size:12px;color:#777;}}
</style></head><body>

<h1>SPY Bot — Profit Target Sweep</h1>
<p class="small">6 variants × 3 target percentages = 18 results.
   Single 30-trading-day period (this month). Stop fixed at 8%, avg-ups at 1.5%/3%.
   Highest total P&amp;L row is highlighted.</p>

<table>
  <tr>
    <th>Variant</th>
    <th>Target</th>
    <th>Trades</th>
    <th>Win rate</th>
    <th>Total P&amp;L</th>
    <th>Avg / trade</th>
    <th>Case A (3+target)</th>
  </tr>
  {body_rows}
</table>

</body></html>"""
    return html


# ----------------------------------------------------------------
# Single-period HTML (original)
# ----------------------------------------------------------------
def render_comparison(rows):
    def m(x):
        cls = "pos" if x > 0 else ("neg" if x < 0 else "")
        return f'<span class="{cls}">${x:+,.2f}</span>'

    def p(x):
        cls = "pos" if x >= 0.5 else ("neg" if x < 0.5 else "")
        return f'<span class="{cls}">{x*100:.1f}%</span>'

    body_rows = ""
    for r in rows:
        s = r["stats"]
        if s is None:
            body_rows += f"<tr><td>{r['label']}</td><td colspan='8'>no trades</td></tr>"
            continue
        body_rows += (
            f"<tr>"
            f"<td>{r['label']}</td>"
            f"<td>{s['trades']}</td>"
            f"<td>{p(s['win_rate'])}</td>"
            f"<td>{m(s['total_pnl'])}</td>"
            f"<td>{m(s['avg_pnl'])}</td>"
            f"<td>{m(s['max_win'])}</td>"
            f"<td>{m(s['max_loss'])}</td>"
            f"<td>{s['case_3_target']} ({s['case_3_target']/s['trades']*100:.0f}%)</td>"
            f"<td>{s['case_1_stop']} ({s['case_1_stop']/s['trades']*100:.0f}%)</td>"
            "</tr>"
        )

    # Per-variant case detail blocks
    detail = ""
    for r in rows:
        s = r["stats"]
        if s is None: continue
        case_rows = "".join(
            f"<tr><td>{k}</td><td>{v}</td>"
            f"<td>{v/s['trades']*100:.1f}%</td></tr>"
            for k, v in sorted(s["cases"].items(), key=lambda x: -x[1])
        )
        detail += (f"<h3>{r['label']}</h3>"
                   f"<table><tr><th>Case</th><th>Count</th><th>%</th></tr>{case_rows}</table>")

    html = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>SPY Bot — Variant Comparison</title>
<style>
  body {{font-family:-apple-system,Helvetica,sans-serif;max-width:1200px;margin:30px auto;padding:0 20px;color:#222;}}
  h1 {{border-bottom:2px solid #333;padding-bottom:8px;}}
  h2 {{margin-top:36px;}}
  table {{border-collapse:collapse;width:100%;margin-top:10px;font-size:14px;}}
  th,td {{padding:8px 10px;border-bottom:1px solid #e2e2e2;text-align:left;}}
  th {{background:#fafafa;font-weight:600;}}
  tr:hover {{background:#fbfbfd;}}
  .pos {{color:#1a8f3c;font-weight:600;}}
  .neg {{color:#cc2222;font-weight:600;}}
  .small {{font-size:12px;color:#777;}}
</style></head><body>

<h1>SPY Bot — Signal Variant Comparison</h1>
<p class="small">30 trading days, look-ahead bias removed, IDLE/IN_TRADE state enforced.
   Cache reused across variants → identical data behind every row.</p>

<h2>Summary</h2>
<table>
 <tr>
   <th>Variant</th>
   <th>Trades</th>
   <th>Win rate</th>
   <th>Total P&amp;L</th>
   <th>Avg / trade</th>
   <th>Max win</th>
   <th>Max loss</th>
   <th>Case A (3+target)</th>
   <th>Case C (1+stop)</th>
 </tr>
 {body_rows}
</table>

<h2>Per-variant case breakdown</h2>
{detail}

</body></html>"""
    return html


# ----------------------------------------------------------------
# Orchestrator
# ----------------------------------------------------------------
VARIANTS = [
    ("Mean Rev — BB + RSI",     "bb_rsi",   "meanrev"),
    ("Momentum — BB + RSI",     "bb_rsi",   "momentum"),
    ("Mean Rev — RSI only",     "rsi_only", "meanrev"),
    ("Momentum — RSI only",     "rsi_only", "momentum"),
    ("Mean Rev — BB only",      "bb_only",  "meanrev"),
    ("Momentum — BB only",      "bb_only",  "momentum"),
]


def _prefetch_and_run(days, label_for_log):
    print(f"\nPre-fetching SPY for {label_for_log}…", flush=True)
    day_bars_map = {}
    for d in days:
        print(f"  · {d}", end="\r", flush=True)
        bars = fetch_spy_day(d)
        if not bars.empty:
            bars = add_indicators(bars)
        day_bars_map[d] = bars

    rows = []
    for label, signal_mode, direction_mode in VARIANTS:
        trades = run_variant(label, signal_mode, direction_mode, days, day_bars_map)
        stats  = aggregate(trades)
        rows.append({"label": label, "stats": stats})
        if stats:
            print(f"   trades={stats['trades']:3d}  WR={stats['win_rate']*100:5.1f}%  "
                  f"P&L=${stats['total_pnl']:+,.2f}", flush=True)
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--two-periods", action="store_true",
                    help="run two consecutive 30-trading-day periods and compare")
    ap.add_argument("--target-sweep", action="store_true",
                    help="run 6 variants at 6%/8%/10% target")
    ap.add_argument("--compare-target-modes", action="store_true",
                    help="compare target=initial vs target=avg for Mean Rev BB+RSI 6% over 2 months")
    args = ap.parse_args()

    today = datetime.now(ET).date()

    if args.compare_target_modes:
        # Just one variant: Mean Rev BB+RSI 6%, but in both target modes,
        # over both 30-day periods.
        all_days = trading_days_back(today, LOOKBACK_DAYS * 2)
        last_month = all_days[:LOOKBACK_DAYS]
        this_month = all_days[LOOKBACK_DAYS:]
        p1_label = f"This month ({this_month[0]} → {this_month[-1]})"
        p2_label = f"Last month ({last_month[0]} → {last_month[-1]})"

        results = {}
        for period_label, days in [(p1_label, this_month), (p2_label, last_month)]:
            print(f"\nPre-fetching SPY for {period_label}…", flush=True)
            day_bars_map = {}
            for d in days:
                print(f"  · {d}", end="\r", flush=True)
                bars = fetch_spy_day(d)
                if not bars.empty:
                    bars = add_indicators(bars)
                day_bars_map[d] = bars

            for mode in ("initial", "average"):
                print(f"\n=== {period_label} | target_mode={mode} ===", flush=True)
                trades = run_variant("Mean Rev — BB + RSI", "bb_rsi", "meanrev",
                                      days, day_bars_map, target_pct=0.06,
                                      target_mode=mode)
                stats = aggregate(trades)
                results[(period_label, mode)] = stats
                if stats:
                    print(f"   trades={stats['trades']:3d}  WR={stats['win_rate']*100:5.1f}%  "
                          f"P&L=${stats['total_pnl']:+,.2f}", flush=True)

        html = render_target_mode_compare(results)
        out  = "backtest_target_mode_compare.html"
        Path(out).write_text(html)
        print(f"\n✅ wrote {out}", flush=True)
        webbrowser.open(f"file://{os.path.abspath(out)}")
        return

    if args.target_sweep:
        target_pcts = [0.06, 0.08, 0.10]

        # ── two-period combined target sweep ────────────────────
        if args.two_periods:
            all_days = trading_days_back(today, LOOKBACK_DAYS * 2)
            last_month = all_days[:LOOKBACK_DAYS]
            this_month = all_days[LOOKBACK_DAYS:]
            p1_label = f"This month  ({this_month[0]} → {this_month[-1]})"
            p2_label = f"Last month  ({last_month[0]} → {last_month[-1]})"

            results_per_period = {}
            for label, days in [(p1_label, this_month), (p2_label, last_month)]:
                print(f"\nPre-fetching SPY for {label}…", flush=True)
                day_bars_map = {}
                for d in days:
                    print(f"  · {d}", end="\r", flush=True)
                    bars = fetch_spy_day(d)
                    if not bars.empty:
                        bars = add_indicators(bars)
                    day_bars_map[d] = bars

                results_by_target = {}
                for tp in target_pcts:
                    print(f"\n=== {label} | Target {int(tp*100)}% ===", flush=True)
                    rows = []
                    for vlabel, signal_mode, direction_mode in VARIANTS:
                        trades = run_variant(vlabel, signal_mode, direction_mode,
                                             days, day_bars_map, target_pct=tp)
                        stats  = aggregate(trades)
                        rows.append({"label": vlabel, "stats": stats})
                        if stats:
                            print(f"   trades={stats['trades']:3d}  WR={stats['win_rate']*100:5.1f}%  "
                                  f"P&L=${stats['total_pnl']:+,.2f}", flush=True)
                    results_by_target[tp] = rows
                results_per_period[label] = results_by_target

            html = render_target_sweep_two_periods(target_pcts, results_per_period,
                                                    p1_label, p2_label)
            out  = "backtest_target_sweep_2p.html"
            Path(out).write_text(html)
            print(f"\n✅ wrote {out}", flush=True)
            webbrowser.open(f"file://{os.path.abspath(out)}")
            return

        # ── single-period target sweep (original) ───────────────
        days = trading_days_back(today, LOOKBACK_DAYS)
        print(f"Target sweep over {len(days)} trading days: {days[0]} → {days[-1]}", flush=True)

        print("\nPre-fetching SPY days…", flush=True)
        day_bars_map = {}
        for d in days:
            print(f"  · {d}", end="\r", flush=True)
            bars = fetch_spy_day(d)
            if not bars.empty:
                bars = add_indicators(bars)
            day_bars_map[d] = bars

        results_by_target = {}
        for tp in target_pcts:
            print(f"\n=== Target {int(tp*100)}% ===", flush=True)
            rows = []
            for label, signal_mode, direction_mode in VARIANTS:
                trades = run_variant(label, signal_mode, direction_mode,
                                      days, day_bars_map, target_pct=tp)
                stats  = aggregate(trades)
                rows.append({"label": label, "stats": stats})
                if stats:
                    print(f"   trades={stats['trades']:3d}  WR={stats['win_rate']*100:5.1f}%  "
                          f"P&L=${stats['total_pnl']:+,.2f}", flush=True)
            results_by_target[tp] = rows

        html = render_target_sweep(target_pcts, results_by_target)
        out  = "backtest_target_sweep.html"
        Path(out).write_text(html)
        print(f"\n✅ wrote {out}", flush=True)
        webbrowser.open(f"file://{os.path.abspath(out)}")
        return

    if args.two_periods:
        # last 60 trading days → split into 2 windows
        all_days = trading_days_back(today, LOOKBACK_DAYS * 2)
        last_month   = all_days[:LOOKBACK_DAYS]                 # earliest 30 = last month
        this_month   = all_days[LOOKBACK_DAYS:]                 # latest 30 = this month

        p1_label = f"This month  ({this_month[0]} → {this_month[-1]})"
        p2_label = f"Last month  ({last_month[0]} → {last_month[-1]})"

        rows_this = _prefetch_and_run(this_month, p1_label)
        rows_last = _prefetch_and_run(last_month, p2_label)

        html = render_two_period({p1_label: rows_this, p2_label: rows_last})
        out  = "backtest_two_periods.html"
        Path(out).write_text(html)
        print(f"\n✅ wrote {out}", flush=True)
        webbrowser.open(f"file://{os.path.abspath(out)}")
        return

    # Single-period mode (original behavior)
    days = trading_days_back(today, LOOKBACK_DAYS)
    print(f"Running 6 variants over {len(days)} trading days: {days[0]} → {days[-1]}", flush=True)
    rows = _prefetch_and_run(days, "current month")

    html = render_comparison(rows)
    out  = "backtest_compare.html"
    Path(out).write_text(html)
    print(f"\n✅ wrote {out}", flush=True)
    webbrowser.open(f"file://{os.path.abspath(out)}")


if __name__ == "__main__":
    main()

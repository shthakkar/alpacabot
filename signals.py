# ================================================================
# SIGNALS — pure detection layer (MEAN REVERSION, no crossover)
# Step 4: check_signal() looks at the latest bar's BB/RSI and
# returns (call_sig, put_sig, current_price). No state. No I/O.
#
# Logic (simple — no crossover requirement):
#   • close > upper BB + RSI > 65 → expect PULLBACK → PUT
#   • close < lower BB + RSI < 35 → expect BOUNCE   → CALL
#
# Re-firing while price stays outside the band is handled by the
# main loop's IDLE/IN_TRADE state guard — see Step 7.
# ================================================================
from typing import Tuple
import pandas as pd

from config     import RSI_OB, RSI_OS
from indicators import add_indicators


def check_signal(df: pd.DataFrame) -> Tuple[bool, bool, float]:
    """
    Evaluate CALL/PUT conditions on the latest bar of `df`.

    Expects an OHLCV DataFrame (raw or with indicators already added).
    If indicators aren't present, they're computed on the fly.

    Returns
    -------
    call_sig : bool   — CALL conditions met on the latest bar
    put_sig  : bool   — PUT  conditions met on the latest bar
    price    : float  — latest close (the "current price" for entries)
    """
    # Need enough bars for indicator warmup
    if len(df) < 21:
        return False, False, float("nan")

    # Add indicators if not already present
    needed = {"bb_upper", "bb_lower", "rsi"}
    if not needed.issubset(df.columns):
        df = add_indicators(df)

    curr = df.iloc[-1]

    # MEAN REVERSION (no crossover — pure position + RSI)
    put_sig  = curr["close"] > curr["bb_upper"] and curr["rsi"] > RSI_OB
    call_sig = curr["close"] < curr["bb_lower"] and curr["rsi"] < RSI_OS

    return bool(call_sig), bool(put_sig), float(curr["close"])


def scan_history(df: pd.DataFrame) -> pd.DataFrame:
    """
    Walk every bar and report which ones would have fired a signal.
    Used by test_step4.py for the dry-run.

    Returns a DataFrame with one row per signal: timestamp, direction,
    price, bb_upper/lower, rsi.
    """
    if not {"bb_upper", "bb_lower", "rsi"}.issubset(df.columns):
        df = add_indicators(df)

    hits = []
    for i in range(len(df)):
        curr = df.iloc[i]

        if pd.isna(curr["bb_upper"]) or pd.isna(curr["rsi"]):
            continue

        # MEAN REVERSION: above upper + overbought → PUT; below lower + oversold → CALL
        put  = curr["close"] > curr["bb_upper"] and curr["rsi"] > RSI_OB
        call = curr["close"] < curr["bb_lower"] and curr["rsi"] < RSI_OS

        if call or put:
            hits.append({
                "timestamp": curr.name,
                "direction": "CALL" if call else "PUT",
                "price":     curr["close"],
                "bb_upper":  curr["bb_upper"],
                "bb_lower":  curr["bb_lower"],
                "rsi":       curr["rsi"],
            })

    return pd.DataFrame(hits)
